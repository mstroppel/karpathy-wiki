import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import test from 'node:test'
import { promisify } from 'node:util'

import {
  applyIngestDraft,
  prepareIngest,
  validateIngest,
  verifyIngestCommit,
} from '../config/tools/wiki_ingest_transaction_core.mjs'
import { loadRun, startRun, writeRecord } from '../config/tools/wiki_ingest_journal_core.mjs'
import { parseFrontmatterFields } from '../config/tools/wiki_ingest_status_core.mjs'

const runFile = promisify(execFile)
const PAGE = 'sources/webdav/notes.md'
const SHA = (text) => createHash('sha256').update(text).digest('hex')
const SOURCE = 'Synthetic selected source.\n'

async function fixture(t, { existing = true, revision = SHA('previous source') } = {}) {
  const base = await mkdtemp(path.join(os.tmpdir(), 'ingest-transaction-'))
  t.after(() => rm(base, { recursive: true, force: true }))
  const wikiRoot = path.join(base, 'wiki')
  const sourceRoot = path.join(base, 'sources')
  const root = path.join(base, 'journal')
  await mkdir(path.join(wikiRoot, 'sources', 'webdav'), { recursive: true })
  await mkdir(path.join(sourceRoot, 'webdav'), { recursive: true })
  await mkdir(root)
  const git = async (...args) => (await runFile('git', args, { cwd: wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic Fixture')
  await git('config', 'user.email', 'fixture@example.invalid')
  await writeFile(path.join(wikiRoot, 'overview.md'), '# Previous overview\n')
  if (existing) {
    await writeFile(
      path.join(wikiRoot, PAGE),
      `---\nsource: webdav\nsource_path: notes.md\nsource_revision: ${revision}\nextra_field: 'keep me'\n---\n# Previous body\n`,
    )
  }
  await git('add', '--', '.')
  await git('commit', '-qm', 'fixture baseline')
  await writeFile(path.join(sourceRoot, 'webdav', 'notes.md'), SOURCE)
  const manifest = {
    contract: 'karpathy-wiki-provider-manifest',
    version: 1,
    source: 'webdav',
    wiki_root: 'webdav',
    generated_at: 1760000000,
    items: [
      {
        source_key: 'notes.md',
        source_path: 'notes.md',
        wiki_path: 'webdav/notes.md',
        source_revision: SHA(SOURCE),
        frontmatter: { source: 'webdav', source_path: 'notes.md', source_revision: SHA(SOURCE) },
        claim: { source: 'webdav', source_path: 'notes.md' },
      },
    ],
    revoked: [],
    errors: [],
  }
  await writeFile(path.join(sourceRoot, 'webdav', 'manifest.json'), JSON.stringify(manifest))
  const opts = { root, sourceRoot, wikiRoot }
  const prepare = (extra = {}) =>
    prepareIngest({
      ...opts,
      adapter: 'webdav',
      sourceKey: 'notes.md',
      sourceRevision: SHA(SOURCE),
      changedPages: [PAGE, 'overview.md'],
      ...extra,
    })
  const prepared = async () => {
    const result = await prepare()
    return { ...opts, preparationId: result.preparation_id }
  }
  const applied = async () => {
    const input = await prepared()
    await applyIngestDraft({
      ...input,
      draft: '---\nnew_extra: "retain too"\n---\n# Newly read source\nA supported synthesis.\n',
    })
    await writeFile(path.join(wikiRoot, 'overview.md'), '# New overview\n')
    return input
  }
  const validated = async () => {
    const input = await applied()
    await validateIngest(input)
    return input
  }
  const commit = async () => {
    await git('add', '--', PAGE, 'overview.md')
    await git('commit', '-qm', 'ingest explicit selected source')
    return git('rev-parse', 'HEAD')
  }
  const record = (input, commitHash, extra = {}) => ({
    adapter: 'webdav',
    source_key: 'notes.md',
    source_path: path.join(sourceRoot, 'webdav', 'notes.md'),
    source_revision: SHA(SOURCE),
    wiki_path: path.join(wikiRoot, PAGE),
    status: 'ingested',
    preparation_id: input.preparationId,
    commit: commitHash,
    changed_pages: [PAGE, 'overview.md'],
    content: 'Supported synthesis with evidence.',
    contradictions: 'None identified.',
    extraction_limits: 'None identified.',
    source_unmodified: true,
    ...extra,
  })
  return {
    base,
    opts,
    wikiRoot,
    sourceRoot,
    root,
    git,
    prepare,
    prepared,
    applied,
    validated,
    commit,
    record,
    manifest,
  }
}

test('applies fresh structural metadata, preserves extras, verifies committed bytes before journal append', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  const text = await readFile(path.join(f.wikiRoot, PAGE), 'utf8')
  const fields = parseFrontmatterFields(text)
  assert.equal(fields.source_revision, SHA(SOURCE))
  assert.equal(fields.extra_field, 'keep me')
  assert.equal(fields.new_extra, 'retain too')
  const commit = await f.commit()
  const { run } = await startRun({ root: f.root })
  const result = await writeRecord({
    ...f.opts,
    runId: run.run_id,
    record: f.record(input, commit.slice(0, 8)),
  })
  assert.equal(result.counts.ingested, 1)
  const loaded = await loadRun({ root: f.root, runId: run.run_id })
  assert.equal(loaded.records[0].commit, commit)
  assert.equal(loaded.records[0].preparation_id, input.preparationId)
  assert.equal(await readFile(path.join(f.sourceRoot, 'webdav', 'notes.md'), 'utf8'), SOURCE)
})

test('handles a new page without canonical fields and unused declared paths', async (t) => {
  const f = await fixture(t, { existing: false })
  const input = await f.prepared()
  await applyIngestDraft({ ...input, draft: '# New source page\nSynthetic content.\n' })
  const result = await validateIngest(input)
  assert.deepEqual(result.changed_pages, [PAGE])
})

test('creates the initial wiki source directories only when applying an explicit draft', async (t) => {
  const f = await fixture(t, { existing: false })
  await rm(path.join(f.wikiRoot, 'sources'), { recursive: true })
  const input = await f.prepared()
  await assert.rejects(readFile(path.join(f.wikiRoot, PAGE)), /ENOENT/)
  await applyIngestDraft({ ...input, draft: '# First synthesis\n' })
  await validateIngest(input)
})

test('applies every identity claim, including prototype-like field names absent from frontmatter', async (t) => {
  const f = await fixture(t, { existing: false })
  f.manifest.items[0].claim = JSON.parse(
    '{"source":"webdav","source_path":"notes.md","__proto__":"synthetic-identity"}',
  )
  await writeFile(path.join(f.sourceRoot, 'webdav', 'manifest.json'), JSON.stringify(f.manifest))
  const input = await f.validated()
  const fields = parseFrontmatterFields(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'))
  assert.equal(fields.__proto__, 'synthetic-identity')
  const commit = await f.commit()
  await verifyIngestCommit({ ...f.opts, record: f.record(input, commit) })
})

for (const revision of ['a'.repeat(63), 'f'.repeat(64)]) {
  test(`rejects ${revision.length}-character incorrect requested and draft revisions without overwrite`, async (t) => {
    const f = await fixture(t)
    await assert.rejects(f.prepare({ sourceRevision: revision }), /source_revision/)
    const input = await f.prepared()
    const before = await readFile(path.join(f.wikiRoot, PAGE), 'utf8')
    await assert.rejects(
      applyIngestDraft({
        ...input,
        draft: `---\nsource_revision: ${revision}\n---\n# Changed body\n`,
      }),
      /source_revision/,
    )
    assert.equal(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), before)
  })
}

test('rejects supplied wrong source identity and selected status identity', async (t) => {
  const f = await fixture(t)
  await assert.rejects(f.prepare({ sourceKey: 'foreign.md' }), /nicht veröffentlicht/)
  const input = await f.prepared()
  for (const identity of ['source: paperless', 'source_path: foreign.md']) {
    await assert.rejects(
      applyIngestDraft({ ...input, draft: `---\n${identity}\n---\n# Fresh body\n` }),
      /Frontmatter-Feld/,
    )
  }
})

test('refuses to adopt a pristine page claiming another source identity', async (t) => {
  const f = await fixture(t)
  const destination = path.join(f.wikiRoot, PAGE)
  const before = (await readFile(destination, 'utf8')).replace(
    'source_path: notes.md',
    'source_path: foreign.md',
  )
  await writeFile(destination, before)
  await f.git('add', '--', PAGE)
  await f.git('commit', '-qm', 'foreign identity fixture')
  await assert.rejects(f.prepare(), /source_path/)
  assert.equal(await readFile(destination, 'utf8'), before)
})

test('an explicit reread may retain identical findings while refreshing metadata', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  await assert.rejects(validateIngest(input), /apply/)
  await applyIngestDraft({ ...input, draft: '# Previous body\n' })
  await writeFile(path.join(f.wikiRoot, 'overview.md'), '# Confirmed unchanged findings\n')
  await validateIngest(input)
  const commit = await f.commit()
  await verifyIngestCommit({ ...f.opts, record: f.record(input, commit) })
})

test('own draft corrections invalidate validation and preserve foreign edits', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  await applyIngestDraft({ ...input, draft: '# Corrected synthesis\n' })
  const commit = await f.commit()
  await assert.rejects(
    verifyIngestCommit({ ...f.opts, record: f.record(input, commit) }),
    /validate/,
  )
  const g = await fixture(t)
  const other = await g.applied()
  await writeFile(path.join(g.wikiRoot, PAGE), '# Foreign edits\n')
  await assert.rejects(applyIngestDraft({ ...other, draft: '# Correction\n' }))
  assert.equal(await readFile(path.join(g.wikiRoot, PAGE), 'utf8'), '# Foreign edits\n')
})

test('revalidates a corrected draft before accepting its commit', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  await applyIngestDraft({ ...input, draft: '# Corrected synthesis\n' })
  await validateIngest(input)
  const commit = await f.commit()
  await verifyIngestCommit({ ...f.opts, record: f.record(input, commit) })
})

test('current-source reread can commit only the log/overview and still verify its source page', async (t) => {
  const f = await fixture(t, { revision: SHA(SOURCE) })
  let input = await f.applied()
  await validateIngest(input)
  await f.commit()
  input = await f.prepared()
  const page = await readFile(path.join(f.wikiRoot, PAGE), 'utf8')
  await applyIngestDraft({ ...input, draft: page })
  await writeFile(path.join(f.wikiRoot, 'overview.md'), '# Explicit reread confirmed\n')
  const result = await validateIngest(input)
  assert.deepEqual(result.changed_pages, ['overview.md'])
  const commit = await f.commit()
  await verifyIngestCommit({
    ...f.opts,
    record: f.record(input, commit, { changed_pages: ['overview.md'] }),
  })
})

test('refuses any foreign staged work while preserving index and worktree exactly', async (t) => {
  const f = await fixture(t)
  await writeFile(path.join(f.wikiRoot, 'foreign.md'), '# Foreign staged content\n')
  await f.git('add', '--', 'foreign.md')
  const before = await readFile(path.join(f.wikiRoot, '.git', 'index'))
  await assert.rejects(f.prepare(), /staged/)
  assert.deepEqual(await readFile(path.join(f.wikiRoot, '.git', 'index')), before)
  assert.equal(
    await readFile(path.join(f.wikiRoot, 'foreign.md'), 'utf8'),
    '# Foreign staged content\n',
  )
})

test('refuses foreign worktree edits and untracked files on prepare', async (t) => {
  const f = await fixture(t)
  await writeFile(path.join(f.wikiRoot, 'overview.md'), '# Foreign edits\n')
  await assert.rejects(f.prepare(), /worktree/)
  assert.equal(await readFile(path.join(f.wikiRoot, 'overview.md'), 'utf8'), '# Foreign edits\n')
})

test('apply never overwrites changes after prepare', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  await writeFile(path.join(f.wikiRoot, PAGE), '# Foreign changed source page\n')
  await assert.rejects(
    applyIngestDraft({ ...input, draft: '# Freshly selected body\n' }),
    /worktree|invalid/,
  )
  assert.equal(
    await readFile(path.join(f.wikiRoot, PAGE), 'utf8'),
    '# Foreign changed source page\n',
  )
})

test('validate rejects undeclared work, stages nothing and preserves it', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  await writeFile(path.join(f.wikiRoot, 'foreign.md'), '# Foreign untracked\n')
  const before = await readFile(path.join(f.wikiRoot, '.git', 'index'))
  await assert.rejects(validateIngest(input), /Undeklarierte/)
  assert.deepEqual(await readFile(path.join(f.wikiRoot, '.git', 'index')), before)
  assert.equal(await readFile(path.join(f.wikiRoot, 'foreign.md'), 'utf8'), '# Foreign untracked\n')
})

test('detects changed source bytes even if the manifest revision stays unchanged', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  await writeFile(path.join(f.sourceRoot, 'webdav', 'notes.md'), 'Changed source bytes\n')
  await assert.rejects(validateIngest(input), /SHA-256/)
})

test('detects changed published manifest after prepare', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  f.manifest.items[0].frontmatter.new_field = 'changed'
  await writeFile(path.join(f.sourceRoot, 'webdav', 'manifest.json'), JSON.stringify(f.manifest))
  await assert.rejects(
    applyIngestDraft({ ...input, draft: '# New body\n' }),
    /Statusdatensatz|invalid/,
  )
})

test('validates exact metadata, rejects valid-looking wrong hashes in edited pages', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  const destination = path.join(f.wikiRoot, PAGE)
  const text = await readFile(destination, 'utf8')
  await writeFile(destination, text.replace(SHA(SOURCE), 'f'.repeat(64)))
  await assert.rejects(validateIngest(input), /source_revision/)
})

test('journal rejects actual committed content different from validate, without appending', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  await writeFile(path.join(f.wikiRoot, 'overview.md'), '# Changed after validation\n')
  const commit = await f.commit()
  const { run } = await startRun({ root: f.root })
  await assert.rejects(
    writeRecord({ ...f.opts, runId: run.run_id, record: f.record(input, commit) }),
    /Commit-Inhalt/,
  )
  assert.equal((await loadRun({ root: f.root, runId: run.run_id })).records.length, 0)
})

test('journal rejects committed wrong source metadata even when the worktree is later correct', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  const destination = path.join(f.wikiRoot, PAGE)
  const correct = await readFile(destination, 'utf8')
  await writeFile(destination, correct.replace(SHA(SOURCE), 'f'.repeat(64)))
  const commit = await f.commit()
  await writeFile(destination, correct)
  const { run } = await startRun({ root: f.root })
  await assert.rejects(
    writeRecord({ ...f.opts, runId: run.run_id, record: f.record(input, commit) }),
    /Commit-Inhalt/,
  )
  assert.equal((await loadRun({ root: f.root, runId: run.run_id })).records.length, 0)
})

test('journal rejects record identity mismatch and source changes after commit', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  const commit = await f.commit()
  const { run } = await startRun({ root: f.root })
  for (const overrides of [
    { source_key: 'wrong.md' },
    { source_revision: 'f'.repeat(64) },
    { source_path: '/foreign.md' },
  ]) {
    await assert.rejects(
      writeRecord({ ...f.opts, runId: run.run_id, record: f.record(input, commit, overrides) }),
      /Datensatz/,
    )
  }
  await writeFile(path.join(f.sourceRoot, 'webdav', 'notes.md'), 'Changed after commit\n')
  await assert.rejects(
    writeRecord({ ...f.opts, runId: run.run_id, record: f.record(input, commit) }),
    /SHA-256/,
  )
  assert.equal((await loadRun({ root: f.root, runId: run.run_id })).records.length, 0)
})

test('rejects commits with foreign staged paths', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  await writeFile(path.join(f.wikiRoot, 'foreign.md'), '# Foreign staged\n')
  await f.git('add', '--', 'foreign.md')
  const index = await readFile(path.join(f.wikiRoot, '.git', 'index'))
  await assert.rejects(validateIngest(input), /staged/)
  assert.deepEqual(await readFile(path.join(f.wikiRoot, '.git', 'index')), index)
  const commit = await f.commit()
  await assert.rejects(
    verifyIngestCommit({ ...f.opts, record: f.record(input, commit) }),
    /validierten Wiki-Seiten/,
  )
})

test('rejects incomplete changed-page lists and commits with intervening parents', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  const commit = await f.commit()
  await assert.rejects(
    verifyIngestCommit({ ...f.opts, record: f.record(input, commit, { changed_pages: [PAGE] }) }),
    /validierten Wiki-Seiten/,
  )
  const f2 = await fixture(t)
  const input2 = await f2.validated()
  await f2.git('commit', '--allow-empty', '-qm', 'intervening commit')
  const commit2 = await f2.commit()
  await assert.rejects(
    verifyIngestCommit({ ...f2.opts, record: f2.record(input2, commit2) }),
    /Vorbereitung/,
  )
})

test('ingested journal writes require valid preparation evidence and reject symlink append paths', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  const commit = await f.commit()
  const { run } = await startRun({ root: f.root })
  await assert.rejects(
    writeRecord({
      ...f.opts,
      runId: run.run_id,
      record: f.record(input, commit, { preparation_id: null }),
    }),
    /preparation_id/,
  )
  const outside = path.join(f.base, 'outside.jsonl')
  await writeFile(outside, 'preserve outside\n')
  await symlink(outside, path.join(f.root, 'runs', run.run_id, 'records.jsonl'))
  await assert.rejects(
    writeRecord({ ...f.opts, runId: run.run_id, record: f.record(input, commit) }),
    /Symlink/,
  )
  assert.equal(await readFile(outside, 'utf8'), 'preserve outside\n')
})

test('requires prepare/apply/validate evidence and rejects Git ref injections', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const commit = await f.git('rev-parse', 'HEAD')
  await assert.rejects(
    verifyIngestCommit({ ...f.opts, record: f.record(input, commit) }),
    /validate/,
  )
  await f.applied()
  await assert.rejects(
    prepareIngest({
      ...f.opts,
      adapter: '../foreign',
      sourceKey: 'x',
      sourceRevision: SHA(SOURCE),
    }),
    /adapter/,
  )
  await assert.rejects(validateIngest({ ...f.opts, preparationId: '../foreign' }), /preparation_id/)
  const f2 = await fixture(t)
  const validated = await f2.validated()
  for (const ref of ['HEAD', '--help', 'abc1234:foreign.md', 'abc1234; touch /tmp/injected']) {
    await assert.rejects(
      verifyIngestCommit({ ...f2.opts, record: f2.record(validated, ref) }),
      /Git-Hash/,
    )
  }
})

test('rejects escaping paths and source/wiki/journal symlinks', async (t) => {
  const f = await fixture(t, { existing: false })
  await assert.rejects(f.prepare({ changedPages: [PAGE, '../outside.md'] }), /relativer Pfad/)
  await symlink(path.join(f.sourceRoot, 'webdav', 'notes.md'), path.join(f.wikiRoot, PAGE))
  await assert.rejects(f.prepare(), /worktree|Symlink/)
  const f2 = await fixture(t)
  const input = await f2.prepared()
  await rm(path.join(f2.sourceRoot, 'webdav', 'notes.md'))
  await symlink(
    path.join(f2.wikiRoot, 'overview.md'),
    path.join(f2.sourceRoot, 'webdav', 'notes.md'),
  )
  await assert.rejects(applyIngestDraft({ ...input, draft: '# New body\n' }), /Symlink/)
  const f3 = await fixture(t)
  await symlink(f3.wikiRoot, path.join(f3.root, 'preparations'))
  await assert.rejects(f3.prepare(), /Symlink/)
})
