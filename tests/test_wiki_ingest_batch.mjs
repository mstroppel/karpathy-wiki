import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { promisify } from 'node:util'
import { runIngestBatch } from '../config/tools/wiki_ingest_batch_core.mjs'
import { runBatchWorker } from '../config/tools/wiki_ingest_batch_worker.mjs'
import { scanIngestStatus } from '../config/tools/wiki_ingest_status_core.mjs'
import { withIngestLock } from '../config/tools/wiki_ingest_storage.mjs'

const exec = promisify(execFile)
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex')

async function fixture(t, count = 2) {
  const base = await mkdtemp(path.join(os.tmpdir(), 'wiki-batch-'))
  t.after(() => rm(base, { recursive: true, force: true }))
  const opts = {
    wikiRoot: path.join(base, 'wiki'),
    sourceRoot: path.join(base, 'sources'),
    root: path.join(base, 'journal'),
  }
  await mkdir(path.join(opts.wikiRoot, 'sources', 'webdav'), { recursive: true })
  await mkdir(path.join(opts.sourceRoot, 'webdav'), { recursive: true })
  const git = async (...args) => (await exec('git', args, { cwd: opts.wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic test')
  await git('config', 'user.email', 'test@example.invalid')
  for (const page of ['index.md', 'overview.md', 'log.md', 'AGENTS.md'])
    await writeFile(path.join(opts.wikiRoot, page), `# ${page}\n`)
  await git('add', '.')
  await git('commit', '-qm', 'baseline')
  const manifest = {
    contract: 'karpathy-wiki-provider-manifest',
    version: 1,
    source: 'webdav',
    wiki_root: 'webdav',
    generated_at: 1760000000,
    items: [],
    revoked: [],
    errors: [],
  }
  for (let i = 0; i < count; i++) {
    const name = `note-${i}.md`
    const content = `Synthetic source ${i}.\n`
    await writeFile(path.join(opts.sourceRoot, 'webdav', name), content)
    manifest.items.push({
      source_key: name,
      source_path: name,
      wiki_path: `webdav/${name}`,
      source_revision: sha(content),
      frontmatter: { source: 'webdav', source_path: name, source_revision: sha(content) },
      claim: { source: 'webdav', source_path: name },
    })
  }
  const publish = () =>
    writeFile(path.join(opts.sourceRoot, 'webdav', 'manifest.json'), JSON.stringify(manifest))
  await publish()
  const worker = async ({ sources }) => {
    for (const source of sources)
      await writeFile(source.wiki_path, '# Supported finding\nSource line 1.\n')
    await writeFile(path.join(opts.wikiRoot, 'overview.md'), '# Combined findings\n')
    return JSON.stringify(
      sources.map((_, index) => ({
        index,
        content: 'Finding from line 1; newly added.',
        contradictions: 'None observed.',
        extraction_limits: 'Fully read line 1.',
      })),
    )
  }
  return { opts, git, manifest, publish, worker }
}

test('one callback and one commit for five sources; code owns identities, index/log and report', async (t) => {
  const f = await fixture(t, 6)
  let calls = 0
  const result = await runIngestBatch(f.opts, async (batch) => {
    calls++
    assert.equal(batch.sources.length, 5)
    return f.worker(batch)
  })
  assert.equal(calls, 1)
  assert.equal(result.status, 'ingested', result.blocker)
  assert.equal(result.remaining, 1)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
  assert.equal(await f.git('status', '--porcelain'), '')
  const status = await scanIngestStatus({
    sourceRoot: f.opts.sourceRoot,
    wikiSourceRoot: path.join(f.opts.wikiRoot, 'sources'),
  })
  assert.equal(status.summary.current, 5)
  const report = await readFile(result.report_path, 'utf8')
  assert.match(report, /Finding from line 1/)
  assert.match(report, new RegExp(result.commit))
  assert.equal(
    (await readFile(path.join(f.opts.wikiRoot, 'log.md'), 'utf8')).match(/Ingest batch-/g).length,
    1,
  )
  assert.equal((await readdir(f.opts.root)).includes('preparations'), false)
  const final = await runIngestBatch(f.opts, f.worker)
  assert.equal(final.remaining, 0)
  assert.equal((await runIngestBatch(f.opts, f.worker)).status, 'done')
})

test('unchanged overview needs no explicit review acknowledgement', async (t) => {
  const f = await fixture(t)
  const original = await readFile(path.join(f.opts.wikiRoot, 'overview.md'), 'utf8')
  const result = await runIngestBatch(f.opts, async (batch) => {
    const records = await f.worker(batch)
    await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), original)
    return records
  })
  assert.equal(result.status, 'ingested')
})

test('dirty wiki blocks before invoking a worker and preserves foreign work', async (t) => {
  const f = await fixture(t)
  await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), 'Foreign work\n')
  await assert.rejects(
    runIngestBatch(f.opts, () => assert.fail('must not run')),
    /nicht sauber/,
  )
  assert.equal(await readFile(path.join(f.opts.wikiRoot, 'overview.md'), 'utf8'), 'Foreign work\n')
})

test('worker failure retains live edits, blocks retries and releases lifetime lock', async (t) => {
  const f = await fixture(t)
  const result = await runIngestBatch(f.opts, async () => {
    await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), 'Partial work\n')
    throw new Error('Synthetic interrupted worker')
  })
  assert.equal(result.status, 'blocked')
  assert.equal(result.commit, null)
  assert.match(await readFile(result.report_path, 'utf8'), /Synthetic interrupted worker/)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
  await assert.rejects(runIngestBatch(f.opts, f.worker), /nicht sauber/)
  await withIngestLock(
    path.join(f.opts.wikiRoot, '.git', 'wiki-ingest-publication.lock'),
    async () => {},
  )
})

test('provider revision change during work never commits', async (t) => {
  const f = await fixture(t)
  const result = await runIngestBatch(f.opts, async (batch) => {
    const records = await f.worker(batch)
    f.manifest.items[0].source_revision = 'a'.repeat(64)
    f.manifest.items[0].frontmatter.source_revision = 'a'.repeat(64)
    await f.publish()
    return records
  })
  assert.equal(result.status, 'blocked')
  assert.match(result.blocker, /Quelle während/)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
})

test('Paperless input revisions are distinct from byte digests and require publisher evidence', async (t) => {
  const f = await fixture(t, 1)
  const directory = path.join(f.opts.sourceRoot, 'paperless')
  await mkdir(directory)
  await rm(path.join(f.opts.sourceRoot, 'webdav'), { recursive: true })
  await mkdir(path.join(f.opts.wikiRoot, 'sources', 'paperless'))
  const revision = 'b'.repeat(64)
  const fields = {
    paperless_id: '1',
    paperless_url: 'https://paperless.invalid/documents/1/',
    source_revision: revision,
  }
  const bytes =
    '---\n' +
    Object.entries(fields)
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
      .join('\n') +
    '\n---\n# Synthetic OCR\n'
  await writeFile(path.join(directory, '1.md'), bytes)
  const manifest = {
    ...f.manifest,
    source: 'paperless',
    wiki_root: 'paperless',
    items: [
      {
        source_key: '1',
        source_path: '1.md',
        wiki_path: 'paperless/1.md',
        source_revision: revision,
        source_sha256: sha(bytes),
        frontmatter: fields,
        claim: { paperless_id: '1' },
      },
    ],
  }
  const file = path.join(directory, 'manifest.json')
  await writeFile(file, JSON.stringify(manifest))
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'ingested', result.blocker)
  assert.match(
    await readFile(path.join(f.opts.wikiRoot, 'sources/paperless/1.md'), 'utf8'),
    /source_revision: "bbbb/,
  )
  manifest.items[0].source_revision = 'c'.repeat(64)
  manifest.items[0].frontmatter.source_revision = 'c'.repeat(64)
  delete manifest.items[0].source_sha256
  await writeFile(file, JSON.stringify(manifest))
  await assert.rejects(runIngestBatch(f.opts, f.worker), /source_sha256/)
})

test('cancellation retains edits and never creates a commit', async (t) => {
  const f = await fixture(t)
  const controller = new AbortController()
  const result = await runIngestBatch({ ...f.opts, signal: controller.signal }, async (batch) => {
    const text = await f.worker(batch)
    controller.abort()
    return text
  })
  assert.equal(result.status, 'blocked')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
  assert.notEqual(await f.git('status', '--porcelain'), '')
})

test('persistent report failure after commit still returns verified outcome without stale link', async (t) => {
  const f = await fixture(t)
  const hook = path.join(f.opts.wikiRoot, '.git/hooks/post-commit')
  await writeFile(
    hook,
    `#!/usr/bin/env python3
from pathlib import Path
for directory in Path('../journal/batches').iterdir():
    for name in ('state.json', 'report.md'):
        file = directory / name
        file.unlink()
        file.mkdir()
`,
  )
  await chmod(hook, 0o700)
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'report_failed')
  assert.equal(result.commit, await f.git('rev-parse', 'HEAD'))
  assert.equal(result.report_path, null)
  assert.ok(result.report_error)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
  assert.equal(await f.git('status', '--porcelain'), '')
})

test('single-source filter is exact and does not import unrelated sources', async (t) => {
  const f = await fixture(t)
  const sourcePath = path.join(f.opts.sourceRoot, 'webdav', 'note-1.md')
  const result = await runIngestBatch({ ...f.opts, sourcePath }, f.worker)
  assert.equal(result.sources.length, 1)
  assert.equal(result.sources[0].source_path, sourcePath)
  assert.equal(result.remaining, 1)
  await assert.rejects(runIngestBatch({ ...f.opts, sourcePath }, f.worker), /nicht new\/outdated/)
})

test('unsafe edits, malformed reports and symlink pages are retained without commit', async (t) => {
  for (const kind of ['instructions', 'report', 'symlink']) {
    await t.test(kind, async (t) => {
      const f = await fixture(t)
      const result = await runIngestBatch(f.opts, async (batch) => {
        const records = await f.worker(batch)
        if (kind === 'instructions')
          await writeFile(path.join(f.opts.wikiRoot, 'AGENTS.md'), 'Changed\n')
        if (kind === 'symlink') {
          await rm(batch.sources[0].wiki_path)
          await symlink(batch.sources[0].source_path, batch.sources[0].wiki_path)
        }
        return kind === 'report' ? 'not JSON' : records
      })
      assert.equal(result.status, 'blocked')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
    })
  }
})

test('budget reduces a batch without silently skipping oversized first source', async (t) => {
  const f = await fixture(t, 3)
  const result = await runIngestBatch({ ...f.opts, budgetTokens: 16000 }, async (batch) => {
    assert.equal(batch.sources.length, 1)
    return f.worker(batch)
  })
  assert.equal(result.remaining, 2)
})

test('kernel lock excludes a second process for the entire worker lifetime', async (t) => {
  const f = await fixture(t)
  const result = await runIngestBatch(f.opts, async (batch) => {
    await assert.rejects(
      exec('python3', [
        '-c',
        'import fcntl,sys; f=open(sys.argv[1],"r+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
        path.join(f.opts.wikiRoot, '.git', 'wiki-ingest-publication.lock'),
      ]),
    )
    return f.worker(batch)
  })
  assert.equal(result.status, 'ingested')
})

test('session adapter uses plugin domain values, parent model and final assistant text', async () => {
  const controller = new AbortController()
  const events = []
  const ctx = {
    session: {
      get: async ({ sessionID }) =>
        sessionID === 'ses_parent'
          ? { model: { providerID: 'synthetic', id: 'fixture' } }
          : { outcome: 'succeeded' },
      create: async (input) => {
        events.push(['create', input])
        return { id: 'ses_child' }
      },
      prompt: async (input) => {
        events.push(['prompt', input])
      },
      wait: async () => {
        events.push(['wait'])
      },
      context: async () => [
        { type: 'assistant', content: [{ type: 'text', text: '[]' }] },
        { type: 'idle' },
      ],
      interrupt: async () => {
        events.push(['stop'])
      },
    },
  }
  assert.equal(
    await runBatchWorker(
      ctx,
      { sessionID: 'ses_parent', signal: controller.signal },
      { batch_id: 'synthetic', sources: [] },
    ),
    '[]',
  )
  assert.deepEqual(events[0][1].model, { providerID: 'synthetic', id: 'fixture' })
  assert.equal(events[0][1].agent, 'wiki-ingest')
  assert.deepEqual(
    events.slice(-2).map(([name]) => name),
    ['stop', 'wait'],
  )
})
