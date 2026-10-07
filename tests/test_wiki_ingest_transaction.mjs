import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, open, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import test from 'node:test'
import { promisify } from 'node:util'
import fs from 'node:fs/promises'
import { syncBuiltinESMExports } from 'node:module'

import {
  applyIngestDraft,
  prepareIngest,
  rollbackIngest,
  validateIngest,
  verifyIngestCommit,
} from '../config/tools/wiki_ingest_transaction_core.mjs'
import {
  loadRun,
  planNextBatch,
  skipBlockedSource,
  startRun,
  writeRecord,
} from '../config/tools/wiki_ingest_journal_core.mjs'
import { parseFrontmatterFields } from '../config/tools/wiki_ingest_status_core.mjs'

const runFile = promisify(execFile)
const PAGE = 'sources/webdav/notes.md'
const SHA = (text) => createHash('sha256').update(text).digest('hex')
const SOURCE = 'Synthetic selected source.\n'

test('real Paperless renderer revisions survive prepare, apply, validate and journal verification', async (t) => {
  const f = await fixture(t, { existing: false })
  const { stdout } = await runFile(
    'python3',
    [
      '-c',
      `
import json
from collections import Counter
from karpathy_wiki_ingest_paperless.documents import source_hash, render_document
doc = {"id": 42, "title": "Synthetic note", "content": "A synthetic observation.", "created": "2026-10-07"}
revision = source_hash(doc, "synthetic-redaction-settings", None, [])
text = render_document(42, doc["title"], doc["content"], "https://example.invalid", Counter(), doc["created"], None, [], 0, revision)
print(json.dumps({"revision": revision, "text": text}))
`,
    ],
    { env: { ...process.env, PYTHONPATH: path.resolve('ingest/paperless/src') } },
  )
  const { revision, text } = JSON.parse(stdout)
  assert.notEqual(SHA(text), revision)
  const page = 'sources/paperless/42.md'
  const frontmatter = {
    paperless_id: 42,
    paperless_url: 'https://example.invalid/documents/42',
    source_revision: revision,
  }
  await mkdir(path.join(f.sourceRoot, 'paperless'))
  await writeFile(path.join(f.sourceRoot, 'paperless', '42.md'), text)
  await writeFile(
    path.join(f.sourceRoot, 'paperless', 'manifest.json'),
    JSON.stringify({
      ...f.manifest,
      source: 'paperless',
      wiki_root: 'paperless',
      items: [
        {
          source_key: '42',
          source_path: '42.md',
          wiki_path: 'paperless/42.md',
          source_revision: revision,
          source_sha256: SHA(text),
          frontmatter,
          claim: { paperless_id: '42' },
        },
      ],
    }),
  )
  const manifestPath = path.join(f.sourceRoot, 'paperless', 'manifest.json')
  const published = JSON.parse(await readFile(manifestPath, 'utf8'))
  await writeFile(
    path.join(f.sourceRoot, 'paperless', '42.md'),
    text + 'Tampered before prepare.\n',
  )
  await assert.rejects(
    f.prepare({ adapter: 'paperless', sourceKey: '42', sourceRevision: revision }),
    /SHA-256/,
  )
  await writeFile(path.join(f.sourceRoot, 'paperless', '42.md'), text)
  const missingDigest = structuredClone(published)
  delete missingDigest.items[0].source_sha256
  await writeFile(manifestPath, JSON.stringify(missingDigest))
  await assert.rejects(
    f.prepare({ adapter: 'paperless', sourceKey: '42', sourceRevision: revision }),
    /benötigt source_sha256/,
  )
  await writeFile(manifestPath, JSON.stringify(published))
  const prepared = await f.prepare({
    adapter: 'paperless',
    sourceKey: '42',
    sourceRevision: revision,
    changedPages: [page, 'overview.md', 'index.md', 'log.md'],
  })
  const input = { ...f.opts, preparationId: prepared.preparation_id }
  await applyIngestDraft({ ...input, draft: '# Synthetic note\nA synthetic observation.\n' })
  await f.complete(input)
  await validateIngest(input)
  // Tampering that leaves the provider revision/frontmatter intact still fails.
  await writeFile(path.join(f.sourceRoot, 'paperless', '42.md'), text + 'Changed body.\n')
  await assert.rejects(validateIngest(input), /SHA-256/)
  await writeFile(path.join(f.sourceRoot, 'paperless', '42.md'), text)
  await f.git('add', '--', page, 'log.md')
  await f.git('commit', '-qm', 'ingest synthetic Paperless export')
  const { run } = await startRun({ root: f.root })
  const result = await writeRecord({
    ...f.opts,
    runId: run.run_id,
    record: f.record(input, await f.git('rev-parse', 'HEAD'), {
      adapter: 'paperless',
      source_key: '42',
      source_revision: revision,
      source_path: path.join(f.sourceRoot, 'paperless', '42.md'),
      wiki_path: path.join(f.wikiRoot, page),
      changed_pages: [page, 'log.md'],
    }),
  })
  assert.equal(result.counts.ingested, 1)
  await writeFile(
    path.join(f.sourceRoot, 'paperless', '42.md'),
    text.replace('paperless_id: 42', 'paperless_id: 43'),
  )
  published.items[0].source_sha256 = SHA(text.replace('paperless_id: 42', 'paperless_id: 43'))
  await writeFile(manifestPath, JSON.stringify(published))
  await assert.rejects(
    f.prepare({ adapter: 'paperless', sourceKey: '42', sourceRevision: revision }),
    /paperless_id/,
  )
})

test('parallel page calls serialize and preserve both edits without lock errors', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  await Promise.all([
    applyIngestDraft({ ...input, draft: '# Supported synthesis\n' }),
    applyIngestDraft({ ...input, page: 'overview.md', append: '\nFirst finding.\n' }),
    applyIngestDraft({ ...input, page: 'overview.md', append: '\nSecond finding.\n' }),
    applyIngestDraft({ ...input, page: 'index.md', edits: [] }),
    applyIngestDraft({ ...input, page: 'log.md', append: '\n- Ingest.\n' }),
  ])
  assert.match(
    await readFile(path.join(f.wikiRoot, 'overview.md'), 'utf8'),
    /First finding\.\n\nSecond finding/,
  )
  assert.equal((await validateIngest(input)).validated, true)
})

test('queued calls stop when an earlier operation fails', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const results = await Promise.allSettled([
    applyIngestDraft({
      ...input,
      page: 'overview.md',
      edits: [{ old_text: 'absent', new_text: 'invalid' }],
    }),
    applyIngestDraft({ ...input, page: 'log.md', append: '\nMust not run.\n' }),
  ])
  assert.deepEqual(
    results.map((result) => result.status),
    ['rejected', 'rejected'],
  )
  assert.equal(await f.git('status', '--porcelain'), '')
})

for (const existing of [true, false]) {
  test(`confirmed rollback restores the exact clean baseline (existing=${existing}) without a live provider`, async (t) => {
    const f = await fixture(t, { existing })
    const head = await f.git('rev-parse', 'HEAD')
    const input = await f.applied()
    await assert.rejects(rollbackIngest(input), /Bestätigung/)
    await rm(f.sourceRoot, { recursive: true })
    assert.equal((await rollbackIngest({ ...input, confirmed: true })).rolled_back, true)
    assert.equal(await f.git('status', '--porcelain'), '')
    assert.equal(await f.git('rev-parse', 'HEAD'), head)
    assert.equal((await rollbackIngest({ ...input, confirmed: true })).rolled_back, true)
    await assert.rejects(applyIngestDraft({ ...input, draft: '# Stale\n' }), /zurückgesetzt/)
    assert.ok(
      (await readdir(path.join(f.wikiRoot, '.git'))).some((name) =>
        name.startsWith('ingest-backup-'),
      ),
    )
  })
}

test('rollback refuses foreign edits, staged changes and committed work', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  const overview = path.join(f.wikiRoot, 'overview.md')
  const owned = await readFile(overview, 'utf8')
  await writeFile(overview, 'Foreign work\n')
  await assert.rejects(rollbackIngest({ ...input, confirmed: true }), /Fremde/)
  assert.match(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), /Newly read/)
  await writeFile(overview, owned)
  await f.git('add', '--', 'overview.md')
  await assert.rejects(rollbackIngest({ ...input, confirmed: true }), /staged/)
  await f.commit()
  await assert.rejects(rollbackIngest({ ...input, confirmed: true }), /HEAD/)
})

test('failure stops planning until confirmed rollback and skip; next failure stops again', async (t) => {
  const f = await fixture(t)
  const other = 'Other synthetic source.\n'
  f.manifest.items.push({
    ...f.manifest.items[0],
    source_key: 'other.md',
    source_path: 'other.md',
    wiki_path: 'webdav/other.md',
    source_revision: SHA(other),
    frontmatter: { source: 'webdav', source_path: 'other.md', source_revision: SHA(other) },
    claim: { source: 'webdav', source_path: 'other.md' },
  })
  await writeFile(path.join(f.sourceRoot, 'webdav', 'manifest.json'), JSON.stringify(f.manifest))
  await writeFile(path.join(f.sourceRoot, 'webdav', 'other.md'), other)
  const input = await f.applied()
  const { run } = await startRun({ root: f.root })
  const args = { ...f.opts, runId: run.run_id, wikiSourceRoot: path.join(f.wikiRoot, 'sources') }
  const blocked = f.record(input, null, {
    status: 'blocked',
    changed_pages: [],
    blocker: 'Synthetic extraction failure',
  })
  await writeRecord({ ...args, record: blocked })
  let plan = await planNextBatch(args)
  assert.equal(plan.recovery_required, true)
  assert.deepEqual(plan.batch, [])
  await assert.rejects(skipBlockedSource({ ...args, recordIndex: 0 }), /Bestätigung/)
  await assert.rejects(skipBlockedSource({ ...args, recordIndex: 0, confirmed: true }), /sauber/)
  await rollbackIngest({ ...input, confirmed: true })
  await skipBlockedSource({ ...args, recordIndex: 0, confirmed: true })
  plan = await planNextBatch(args)
  assert.equal(plan.blocked, false)
  assert.deepEqual(
    plan.batch.map((entry) => entry.source_key),
    ['other.md'],
  )
  assert.equal((await loadRun(args)).counts.blocked, 1)
  // Republishing must not smuggle an explicitly skipped source back into this run.
  f.manifest.items[0].source_revision = SHA('Republished source')
  f.manifest.items[0].frontmatter.source_revision = SHA('Republished source')
  await writeFile(path.join(f.sourceRoot, 'webdav', 'manifest.json'), JSON.stringify(f.manifest))
  assert.deepEqual(
    (await planNextBatch(args)).batch.map((entry) => entry.source_key),
    ['other.md'],
  )
  await writeRecord({ ...args, record: blocked })
  assert.equal((await planNextBatch(args)).recovery_required, true)
})

test('skip rejects a reset receipt belonging to a different blocked source or revision', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  await rollbackIngest({ ...input, confirmed: true })
  const { run } = await startRun({ root: f.root })
  const args = { ...f.opts, runId: run.run_id, confirmed: true }
  for (const [field, value] of Object.entries({
    adapter: 'other',
    source_key: 'other.md',
    source_revision: SHA('other'),
    source_path: '/knowledge/sources/other.md',
    wiki_path: '/knowledge/wiki/sources/other.md',
  })) {
    const result = await writeRecord({
      ...args,
      record: f.record(input, null, {
        status: 'blocked',
        blocker: 'Synthetic failure',
        changed_pages: [],
        [field]: value,
      }),
    })
    await assert.rejects(
      skipBlockedSource({ ...args, recordIndex: result.record_index }),
      /gehört nicht/,
    )
  }
})

for (const existing of [true, false]) {
  test(`rollback resumes after interruption just after displacement (existing=${existing})`, async (t) => {
    const f = await fixture(t, { existing })
    const input = await f.applied()
    const originalRename = fs.rename
    const injected = t.mock.method(fs, 'rename', async (from, to) => {
      await originalRename(from, to)
      if (from === path.join(f.wikiRoot, PAGE)) throw new Error('Synthetic reset interruption')
    })
    syncBuiltinESMExports()
    try {
      await assert.rejects(rollbackIngest({ ...input, confirmed: true }), /reset interruption/)
    } finally {
      injected.mock.restore()
      syncBuiltinESMExports()
    }
    await rollbackIngest({ ...input, confirmed: true })
    assert.equal(await f.git('status', '--porcelain'), '')
  })
}

test('rollback preserves an edit racing displacement and refuses to discard it', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  const originalRename = fs.rename
  const injected = t.mock.method(fs, 'rename', async (from, to) => {
    if (from === path.join(f.wikiRoot, PAGE)) await writeFile(from, '# Racing foreign work\n')
    return originalRename(from, to)
  })
  syncBuiltinESMExports()
  try {
    await assert.rejects(rollbackIngest({ ...input, confirmed: true }), /Fremde/)
  } finally {
    injected.mock.restore()
    syncBuiltinESMExports()
  }
  assert.equal(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), '# Racing foreign work\n')
})

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
  await writeFile(path.join(wikiRoot, 'index.md'), '# Wiki index\n')
  await writeFile(path.join(wikiRoot, 'log.md'), '# Historical log\n')
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
      changedPages: [PAGE, 'overview.md', 'index.md', 'log.md'],
      ...extra,
    })
  const prepared = async () => {
    const result = await prepare()
    return { ...opts, preparationId: result.preparation_id }
  }
  const complete = async (input) => {
    await applyIngestDraft({ ...input, page: 'overview.md', edits: [] })
    await applyIngestDraft({ ...input, page: 'index.md', edits: [] })
    const receipt = JSON.parse(
      await readFile(path.join(root, 'preparations', `${input.preparationId}.json`)),
    )
    if (!Object.hasOwn(receipt.owned, 'log.md'))
      await applyIngestDraft({ ...input, page: 'log.md', append: '\n- Synthetic ingestion.\n' })
  }
  const applied = async () => {
    const input = await prepared()
    await applyIngestDraft({
      ...input,
      draft: '---\nnew_extra: "retain too"\n---\n# Newly read source\nA supported synthesis.\n',
    })
    await applyIngestDraft({
      ...input,
      page: 'overview.md',
      edits: [{ old_text: 'Previous overview', new_text: 'New overview' }],
    })
    await complete(input)
    return input
  }
  const validated = async () => {
    const input = await applied()
    await validateIngest(input)
    return input
  }
  const commit = async () => {
    await git('add', '--', PAGE, 'overview.md', 'index.md', 'log.md')
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
    changed_pages: [PAGE, 'overview.md', 'log.md'],
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
    complete,
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
  await f.complete(input)
  const result = await validateIngest(input)
  assert.deepEqual(result.changed_pages, [PAGE, 'log.md'])
})

test('source-only and unfinished shared-page transactions cannot validate', async (t) => {
  const f = await fixture(t)
  await assert.rejects(f.prepare({ changedPages: [PAGE] }), /Pflichtseite/)
  const input = await f.prepared()
  await applyIngestDraft({ ...input, draft: '# Source synthesis\n' })
  await assert.rejects(validateIngest(input), /Pflichtseite.*overview/)
  await applyIngestDraft({ ...input, page: 'overview.md', edits: [] })
  await assert.rejects(validateIngest(input), /Pflichtseite.*index/)
  await applyIngestDraft({ ...input, page: 'index.md', edits: [] })
  await assert.rejects(validateIngest(input), /Pflichtseite.*log/)
  await assert.rejects(applyIngestDraft({ ...input, page: 'log.md', edits: [] }), /nur ergänzbar/)
  await f.complete(input)
  assert.deepEqual((await validateIngest(input)).changed_pages, [PAGE, 'log.md'])
  const commit = await f.commit()
  await verifyIngestCommit({
    ...f.opts,
    record: f.record(input, commit, { changed_pages: [PAGE, 'log.md'] }),
  })
})

test('targeted edits preserve a long overview and reject truncated drafts before writing', async (t) => {
  const f = await fixture(t)
  const history =
    '# Overview\n\n' + Array.from({ length: 500 }, (_, n) => `Historical finding ${n}.\n`).join('')
  const overview = path.join(f.wikiRoot, 'overview.md')
  await writeFile(overview, history)
  await f.git('add', '--', 'overview.md')
  await f.git('commit', '-qm', 'synthetic long history')
  const input = await f.prepared()
  await assert.rejects(
    applyIngestDraft({
      ...input,
      page: 'overview.md',
      draft: '# Overview\nHistorical content unchanged …\n',
    }),
    /keinen draft/,
  )
  assert.equal(await readFile(overview, 'utf8'), history)
  for (const edits of [
    [{ old_text: 'not present', new_text: 'replacement' }],
    [{ old_text: 'Historical finding', new_text: 'replacement' }],
    [{ old_text: '', new_text: 'replacement' }],
    [
      { old_text: 'Historical finding 1.', new_text: 'changed' },
      { old_text: 'missing', new_text: 'changed' },
    ],
  ]) {
    await assert.rejects(applyIngestDraft({ ...input, page: 'overview.md', edits }))
    assert.equal(await readFile(overview, 'utf8'), history)
  }
  await applyIngestDraft({
    ...input,
    page: 'overview.md',
    edits: [{ old_text: 'Historical finding 42.', new_text: 'Updated finding 42.' }],
  })
  assert.equal(
    await readFile(overview, 'utf8'),
    history.replace('Historical finding 42.', 'Updated finding 42.'),
  )
  await applyIngestDraft({ ...input, page: 'overview.md', append: '\nNew supported finding.\n' })
  assert.equal(
    await readFile(overview, 'utf8'),
    history.replace('Historical finding 42.', 'Updated finding 42.') + '\nNew supported finding.\n',
  )
  await applyIngestDraft({ ...input, draft: '# Source synthesis\n' })
  await f.complete(input)
  await validateIngest(input)
})

test('incremental edits reject invalid UTF-8 without changing historical bytes', async (t) => {
  const f = await fixture(t)
  const destination = path.join(f.wikiRoot, 'overview.md')
  const bytes = Buffer.from([0x23, 0x20, 0xff, 0x0a])
  await writeFile(destination, bytes)
  await f.git('add', '--', 'overview.md')
  await f.git('commit', '-qm', 'synthetic invalid encoding')
  const input = await f.prepared()
  await assert.rejects(
    applyIngestDraft({ ...input, page: 'overview.md', append: '\nFinding.\n' }),
    /UTF-8/,
  )
  assert.deepEqual(await readFile(destination), bytes)
})

test('creates the initial wiki source directories only when applying an explicit draft', async (t) => {
  const f = await fixture(t, { existing: false })
  await rm(path.join(f.wikiRoot, 'sources'), { recursive: true })
  const input = await f.prepared()
  await assert.rejects(readFile(path.join(f.wikiRoot, PAGE)), /ENOENT/)
  await applyIngestDraft({ ...input, draft: '# First synthesis\n' })
  await f.complete(input)
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
  await applyIngestDraft({
    ...input,
    page: 'overview.md',
    edits: [{ old_text: 'Previous overview', new_text: 'Confirmed unchanged findings' }],
  })
  await f.complete(input)
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

for (const existing of [true, false]) {
  test(`interrupted draft write preserves ${existing ? 'existing' : 'absent'} page and allows retry`, async (t) => {
    const f = await fixture(t, { existing })
    const input = await f.prepared()
    const destination = path.join(f.wikiRoot, PAGE)
    const before = existing ? await readFile(destination) : null
    const receiptPath = path.join(f.root, 'preparations', `${input.preparationId}.json`)
    const receipt = await readFile(receiptPath)
    const probe = await open(path.join(f.base, 'probe'), 'wx')
    const prototype = Object.getPrototypeOf(probe)
    await probe.close()
    const originalWrite = prototype.write
    const injected = t.mock.method(
      prototype,
      'write',
      async function (buffer, offset, length, position) {
        await originalWrite.call(this, buffer, offset, Math.min(16, length), position)
        throw new Error('Synthetic interrupted write')
      },
    )
    await assert.rejects(
      applyIngestDraft({ ...input, draft: '# Fresh synthetic draft\n' }),
      /Synthetic interrupted write/,
    )
    injected.mock.restore()
    if (existing) assert.deepEqual(await readFile(destination), before)
    else await assert.rejects(readFile(destination), /ENOENT/)
    assert.deepEqual(await readFile(receiptPath), receipt)
    assert.deepEqual(await readdir(path.dirname(destination)), existing ? ['notes.md'] : [])
    await applyIngestDraft({ ...input, draft: '# Fresh synthetic draft\n' })
    await f.complete(input)
    await validateIngest(input)
  })
}

test('writes through an old descriptor remain in retained evidence after publication', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const writer = await open(path.join(f.wikiRoot, PAGE), 'r+')
  try {
    await applyIngestDraft({ ...input, draft: '# Own published draft\n' })
    await writer.truncate(0)
    await writer.writeFile('# Late descriptor edit\n')
  } finally {
    await writer.close()
  }
  assert.match(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), /Own published draft/)
  const backups = (await readdir(path.join(f.wikiRoot, '.git'))).filter((name) =>
    name.startsWith('ingest-backup-'),
  )
  assert.equal(backups.length, 1)
  assert.equal(
    await readFile(path.join(f.wikiRoot, '.git', backups[0], 'page'), 'utf8'),
    '# Late descriptor edit\n',
  )
  await assert.rejects(validateIngest(input), /Fremde Änderung.*Backup/)
})

test('missing retained evidence blocks validation and commit acceptance', async (t) => {
  const f = await fixture(t)
  const input = await f.validated()
  const receipt = JSON.parse(
    await readFile(path.join(f.root, 'preparations', `${input.preparationId}.json`)),
  )
  await rm(path.join(f.wikiRoot, '.git', receipt.backups[0].path))
  await assert.rejects(validateIngest(input), /fehlendes Backup/)
  const commit = await f.commit()
  await assert.rejects(
    verifyIngestCommit({ ...f.opts, record: f.record(input, commit) }),
    /fehlendes Backup/,
  )
})

test('pending receipt recovery rejects an installed page with missing displaced evidence', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const probe = await open(path.join(f.base, 'probe'), 'wx')
  const prototype = Object.getPrototypeOf(probe)
  await probe.close()
  const original = prototype.write
  const injected = t.mock.method(prototype, 'write', async function (buffer, ...args) {
    if (buffer.toString().includes('"pending":null'))
      throw new Error('Synthetic final receipt failure')
    return original.call(this, buffer, ...args)
  })
  await assert.rejects(
    applyIngestDraft({ ...input, draft: '# Installed draft\n' }),
    /final receipt failure/,
  )
  injected.mock.restore()
  const receiptPath = path.join(f.root, 'preparations', `${input.preparationId}.json`)
  const receipt = await readFile(receiptPath)
  await rm(path.join(f.wikiRoot, '.git', JSON.parse(receipt).pending.backup))
  const page = await readFile(path.join(f.wikiRoot, PAGE))
  await assert.rejects(applyIngestDraft({ ...input, draft: '# Retry\n' }), /Backup fehlt/)
  assert.deepEqual(await readFile(receiptPath), receipt)
  assert.deepEqual(await readFile(path.join(f.wikiRoot, PAGE)), page)
})

test('a failed installation restores the verified prior page on retry', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const original = fs.link
  const injected = t.mock.method(fs, 'link', async (from, to) => {
    if (to === path.join(f.wikiRoot, PAGE)) throw new Error('Synthetic install failure')
    return original(from, to)
  })
  syncBuiltinESMExports()
  try {
    await assert.rejects(
      applyIngestDraft({ ...input, draft: '# Retry draft\n' }),
      /install failure/,
    )
  } finally {
    injected.mock.restore()
    syncBuiltinESMExports()
  }
  await assert.rejects(readFile(path.join(f.wikiRoot, PAGE)), /ENOENT/)
  await applyIngestDraft({ ...input, draft: '# Retry draft\n' })
  await f.complete(input)
  await validateIngest(input)
})

for (const operation of [
  { page: 'log.md', append: '\n- Exactly one synthetic entry.\n' },
  { page: 'overview.md', edits: [{ old_text: 'Previous overview', new_text: 'Updated overview' }] },
]) {
  test(`recovers ${operation.page} without replaying an installed incremental edit`, async (t) => {
    const f = await fixture(t)
    const input = await f.prepared()
    const before = await readFile(path.join(f.wikiRoot, operation.page), 'utf8')
    const probe = await open(path.join(f.base, 'probe'), 'wx')
    const prototype = Object.getPrototypeOf(probe)
    await probe.close()
    const original = prototype.write
    const injected = t.mock.method(prototype, 'write', async function (buffer, ...args) {
      if (buffer.toString().includes('"pending":null'))
        throw new Error('Synthetic final receipt failure')
      return original.call(this, buffer, ...args)
    })
    await assert.rejects(applyIngestDraft({ ...input, ...operation }), /final receipt failure/)
    injected.mock.restore()
    const installed = await readFile(path.join(f.wikiRoot, operation.page), 'utf8')
    assert.notEqual(installed, before)
    await applyIngestDraft({ ...input, ...operation })
    assert.equal(await readFile(path.join(f.wikiRoot, operation.page), 'utf8'), installed)
    await applyIngestDraft({ ...input, draft: '# Source synthesis\n' })
    await f.complete(input)
    await validateIngest(input)
  })
}

test('apply and validate never proceed through an existing preparation lock', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const destination = path.join(f.wikiRoot, PAGE)
  const before = await readFile(destination)
  const lock = path.join(f.root, 'preparations', `${input.preparationId}.json.lock`)
  await writeFile(lock, 'synthetic owner\n')
  await assert.rejects(applyIngestDraft({ ...input, draft: '# Blocked\n' }), /EEXIST/)
  await assert.rejects(validateIngest(input), /EEXIST/)
  assert.deepEqual(await readFile(destination), before)
  assert.equal(await readFile(lock, 'utf8'), 'synthetic owner\n')
})

test('foreign edits during temporary writing are preserved before replacement', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const probe = await open(path.join(f.base, 'probe'), 'wx')
  const prototype = Object.getPrototypeOf(probe)
  await probe.close()
  const originalWrite = prototype.write
  const injected = t.mock.method(prototype, 'write', async function (...args) {
    const result = await originalWrite.apply(this, args)
    await writeFile(path.join(f.wikiRoot, PAGE), '# Foreign edits during writing\n')
    return result
  })
  await assert.rejects(
    applyIngestDraft({ ...input, draft: '# Fresh synthetic draft\n' }),
    /worktree/,
  )
  injected.mock.restore()
  assert.equal(
    await readFile(path.join(f.wikiRoot, PAGE), 'utf8'),
    '# Foreign edits during writing\n',
  )
  assert.deepEqual(await readdir(path.join(f.wikiRoot, 'sources', 'webdav')), ['notes.md'])
})

test('a foreign page created during a new draft write is never replaced', async (t) => {
  const f = await fixture(t, { existing: false })
  const input = await f.prepared()
  const probe = await open(path.join(f.base, 'probe'), 'wx')
  const prototype = Object.getPrototypeOf(probe)
  await probe.close()
  const originalWrite = prototype.write
  const injected = t.mock.method(prototype, 'write', async function (...args) {
    const result = await originalWrite.apply(this, args)
    await writeFile(path.join(f.wikiRoot, PAGE), '# Concurrent foreign page\n')
    return result
  })
  await assert.rejects(applyIngestDraft({ ...input, draft: '# New synthetic draft\n' }), /EEXIST/)
  injected.mock.restore()
  assert.equal(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), '# Concurrent foreign page\n')
  assert.deepEqual(await readdir(path.join(f.wikiRoot, 'sources', 'webdav')), ['notes.md'])
})

test('an interrupted receipt write preserves parseable evidence and allows validation retry', async (t) => {
  const f = await fixture(t)
  const input = await f.applied()
  const receiptPath = path.join(f.root, 'preparations', `${input.preparationId}.json`)
  const before = await readFile(receiptPath)
  const probe = await open(path.join(f.base, 'probe'), 'wx')
  const prototype = Object.getPrototypeOf(probe)
  await probe.close()
  const originalWrite = prototype.write
  const injected = t.mock.method(
    prototype,
    'write',
    async function (buffer, offset, length, position) {
      await originalWrite.call(this, buffer, offset, Math.min(16, length), position)
      throw new Error('Synthetic interrupted receipt write')
    },
  )
  await assert.rejects(validateIngest(input), /Synthetic interrupted receipt write/)
  injected.mock.restore()
  assert.deepEqual(await readFile(receiptPath), before)
  assert.deepEqual(await readdir(path.dirname(receiptPath)), [`${input.preparationId}.json`])
  await validateIngest(input)
  const commit = await f.commit()
  await verifyIngestCommit({ ...f.opts, record: f.record(input, commit) })
})

test('validate rejects foreign edits to declared thematic pages without adopting them', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  await applyIngestDraft({ ...input, draft: '# Source draft\n' })
  await writeFile(path.join(f.wikiRoot, 'overview.md'), '# Foreign declared-page edit\n')
  await assert.rejects(validateIngest(input), /Fremde worktree/)
  await assert.rejects(
    applyIngestDraft({ ...input, page: 'overview.md', draft: '# Own overview\n' }),
    /Fremde worktree/,
  )
  assert.equal(
    await readFile(path.join(f.wikiRoot, 'overview.md'), 'utf8'),
    '# Foreign declared-page edit\n',
  )
})

test('thematic apply supports new pages and corrections, but rejects undeclared paths', async (t) => {
  const f = await fixture(t)
  const prepared = await f.prepare({
    changedPages: [PAGE, 'new.md', 'overview.md', 'index.md', 'log.md'],
  })
  const input = { ...f.opts, preparationId: prepared.preparation_id }
  await applyIngestDraft({ ...input, draft: '# Source draft\n' })
  await assert.rejects(
    applyIngestDraft({ ...input, page: 'other.md', draft: '# Other\n' }),
    /nicht deklariert/,
  )
  await applyIngestDraft({ ...input, page: 'new.md', draft: '# New theme\n' })
  await applyIngestDraft({
    ...input,
    page: 'new.md',
    edits: [{ old_text: 'New theme', new_text: 'Corrected theme' }],
  })
  await f.complete(input)
  assert.deepEqual((await validateIngest(input)).changed_pages, [PAGE, 'new.md', 'log.md'])
})

test('a mutation immediately before page displacement is retained and blocks publication', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const original = fs.rename
  const injected = t.mock.method(fs, 'rename', async (from, to) => {
    if (from === path.join(f.wikiRoot, PAGE)) await writeFile(from, '# Last-instant foreign edit\n')
    return original(from, to)
  })
  syncBuiltinESMExports()
  try {
    await assert.rejects(applyIngestDraft({ ...input, draft: '# Own draft\n' }), /Backup erhalten/)
  } finally {
    injected.mock.restore()
    syncBuiltinESMExports()
  }
  assert.equal(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), '# Last-instant foreign edit\n')
  const receipt = JSON.parse(
    await readFile(path.join(f.root, 'preparations', `${input.preparationId}.json`)),
  )
  assert.equal(
    await readFile(path.join(f.wikiRoot, '.git', receipt.pending.backup), 'utf8'),
    '# Last-instant foreign edit\n',
  )
  await assert.rejects(validateIngest(input), /Backup erhalten|invalid\/conflict/)
})

test('a destination created after displacement wins the exclusive installation', async (t) => {
  const f = await fixture(t)
  const input = await f.prepared()
  const before = await readFile(path.join(f.wikiRoot, PAGE))
  const original = fs.link
  const injected = t.mock.method(fs, 'link', async (from, to) => {
    if (to === path.join(f.wikiRoot, PAGE)) await writeFile(to, '# Concurrent new inode\n')
    return original(from, to)
  })
  syncBuiltinESMExports()
  try {
    await assert.rejects(applyIngestDraft({ ...input, draft: '# Own draft\n' }), /EEXIST/)
  } finally {
    injected.mock.restore()
    syncBuiltinESMExports()
  }
  assert.equal(await readFile(path.join(f.wikiRoot, PAGE), 'utf8'), '# Concurrent new inode\n')
  const receipt = JSON.parse(
    await readFile(path.join(f.root, 'preparations', `${input.preparationId}.json`)),
  )
  assert.deepEqual(await readFile(path.join(f.wikiRoot, '.git', receipt.pending.backup)), before)
  await assert.rejects(
    applyIngestDraft({ ...input, draft: '# Retry\n' }),
    /Fremde worktree|invalid\/conflict/,
  )
})

for (const existing of [true, false]) {
  test(`apply recovers a failed final receipt write for ${existing ? 'existing' : 'new'} pages`, async (t) => {
    const f = await fixture(t, { existing })
    const input = await f.prepared()
    const probe = await open(path.join(f.base, 'probe'), 'wx')
    const prototype = Object.getPrototypeOf(probe)
    await probe.close()
    const original = prototype.write
    const injected = t.mock.method(prototype, 'write', async function (buffer, ...args) {
      if (buffer.toString().includes('"pending":null'))
        throw new Error('Synthetic final receipt failure')
      return original.call(this, buffer, ...args)
    })
    await assert.rejects(
      applyIngestDraft({ ...input, draft: '# Retryable draft\n' }),
      /final receipt failure/,
    )
    injected.mock.restore()
    await applyIngestDraft({ ...input, draft: '# Retryable draft\n' })
    await f.complete(input)
    const result = await validateIngest(input)
    const commit = await f.commit()
    await verifyIngestCommit({
      ...f.opts,
      record: f.record(input, commit, { changed_pages: result.changed_pages }),
    })
  })
}

test('current-source reread can commit only the log/overview and still verify its source page', async (t) => {
  const f = await fixture(t, { revision: SHA(SOURCE) })
  let input = await f.applied()
  await validateIngest(input)
  await f.commit()
  input = await f.prepared()
  const page = await readFile(path.join(f.wikiRoot, PAGE), 'utf8')
  await applyIngestDraft({ ...input, draft: page })
  await applyIngestDraft({
    ...input,
    page: 'overview.md',
    edits: [{ old_text: 'New overview', new_text: 'Explicit reread confirmed' }],
  })
  await f.complete(input)
  const result = await validateIngest(input)
  assert.deepEqual(result.changed_pages, ['overview.md', 'log.md'])
  const commit = await f.commit()
  await verifyIngestCommit({
    ...f.opts,
    record: f.record(input, commit, { changed_pages: result.changed_pages }),
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
