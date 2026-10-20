import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { chmod, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { promisify } from 'node:util'
import { runIngestBatch } from '../config/tools/wiki_ingest_batch_core.mjs'
import { runBatchWorker } from '../config/tools/wiki_ingest_batch_worker.mjs'
import { withIngestLock } from '../config/tools/wiki_ingest_storage.mjs'

const exec = promisify(execFile)
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex')

async function fixture(t, count = 3) {
  const base = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-'))
  t.after(() => rm(base, { recursive: true, force: true }))
  const opts = {
    wikiRoot: path.join(base, 'wiki'),
    sourceRoot: path.join(base, 'sources'),
    root: path.join(base, 'journal'),
  }
  await mkdir(path.join(opts.wikiRoot, 'sources/webdav'), { recursive: true })
  await mkdir(path.join(opts.sourceRoot, 'webdav'), { recursive: true })
  const git = async (...args) => (await exec('git', args, { cwd: opts.wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic test')
  await git('config', 'user.email', 'test@example.invalid')
  for (const name of ['index.md', 'overview.md', 'log.md', 'AGENTS.md'])
    await writeFile(path.join(opts.wikiRoot, name), `# ${name}\n`)
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
    const text = `Synthetic source ${i}.\n`
    await writeFile(path.join(opts.sourceRoot, 'webdav', name), text)
    manifest.items.push({
      source_key: name,
      source_path: name,
      wiki_path: `webdav/${name}`,
      source_revision: sha(text),
      frontmatter: { source: 'webdav', source_path: name, source_revision: sha(text) },
      claim: { source: 'webdav', source_path: name },
    })
  }
  const publish = () =>
    writeFile(path.join(opts.sourceRoot, 'webdav/manifest.json'), JSON.stringify(manifest))
  await publish()
  const worker = async ({ sources }) => {
    assert.equal(sources.length, 1)
    await writeFile(sources[0].wiki_path, '# Supported finding\nSource line 1.\n')
    return '**Befunde:** Source line 1; newly added.\n**Widersprüche:** None observed.\n**Extraktionsgrenzen:** Fully read line 1.'
  }
  return { opts, git, manifest, publish, worker }
}

test('one request drains more than twelve sources without a model continuation loop', async (t) => {
  const f = await fixture(t, 14)
  let calls = 0
  const result = await runIngestBatch(f.opts, async (batch) => {
    calls++
    return f.worker(batch)
  })
  assert.equal(calls, 14)
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(result.ingested, 14)
  assert.equal(result.commit, await f.git('rev-parse', 'HEAD'))
  assert.equal(result.remaining, 0)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '15')
  assert.equal(await f.git('status', '--porcelain'), '')
  const report = await readFile(result.report_path, 'utf8')
  assert.equal((report.match(/Fully read line 1/g) ?? []).length, 14)
  const commits = (await f.git('log', '--format=%H', 'HEAD~14..HEAD')).split('\n')
  assert.ok(commits.every((commit) => report.includes(commit)))
  assert.equal((await runIngestBatch(f.opts, () => assert.fail('already current'))).status, 'done')
})

test('Markdown hard breaks, trailing tabs and blank EOF do not stop ingestion', async (t) => {
  const f = await fixture(t)
  const result = await runIngestBatch(f.opts, async (batch) => {
    const report = await f.worker(batch)
    await writeFile(batch.sources[0].wiki_path, '# Finding  \nLine 1.\t\n\n\n')
    return report
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(result.ingested, 3)
  assert.match(
    await readFile(path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md'), 'utf8'),
    /Finding {2}\n/,
  )
})

test('missing/corrupt source and pre-write worker failure skip only that source', async (t) => {
  for (const kind of ['missing', 'digest', 'worker', 'skip']) {
    await t.test(kind, async (t) => {
      const f = await fixture(t)
      const target = path.join(f.opts.sourceRoot, 'webdav/note-1.md')
      if (kind === 'missing') await rm(target)
      if (kind === 'digest') await writeFile(target, 'Changed outside publication\n')
      const result = await runIngestBatch(f.opts, async (batch) => {
        if (batch.sources[0].source_path === target) {
          if (kind === 'worker') throw new Error('PRIVATE_NATIVE_ERROR')
          if (kind === 'skip') return 'SKIP: No legible source text.'
        }
        return f.worker(batch)
      })
      assert.equal(result.status, 'partial', result.blocker)
      assert.equal(result.ingested, 2)
      assert.equal(result.skipped, 1)
      assert.equal(result.remaining, 1)
      assert.equal(await f.git('status', '--porcelain'), '')
      const report = await readFile(result.report_path, 'utf8')
      assert.ok(report.includes(target))
      assert.doesNotMatch(report, /PRIVATE_NATIVE_ERROR/)
      if (kind === 'skip') assert.match(report, /No legible source text/)
    })
  }
})

test('broken provider and conflicting source do not stop independent sources', async (t) => {
  const f = await fixture(t)
  await mkdir(path.join(f.opts.sourceRoot, 'broken'))
  await writeFile(path.join(f.opts.sourceRoot, 'broken/manifest.json'), '{}')
  const item = f.manifest.items[1]
  const page = path.join(f.opts.wikiRoot, 'sources/webdav/note-1.md')
  const text = `---\nsource: webdav\nsource_path: note-1.md\nsource_revision: ${item.source_revision}\n---\n# Previous\n`
  await writeFile(page, text)
  await writeFile(path.join(f.opts.wikiRoot, 'sources/webdav/duplicate.md'), text)
  await f.git('add', '.')
  await f.git('commit', '-qm', 'conflicting prior source')
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'partial')
  assert.equal(result.ingested, 2)
  assert.equal(result.summary.conflict, 1)
  assert.ok(result.summary.invalid)
  assert.equal(await readFile(page, 'utf8'), text)
  assert.match(await readFile(result.report_path, 'utf8'), /broken/)
})

test('target collisions with conflicted identities never dispatch a worker or stop independent sources', async (t) => {
  const f = await fixture(t)
  const [first, second, independent] = f.manifest.items
  second.wiki_path = first.wiki_path
  await f.publish()
  const page = path.join(f.opts.wikiRoot, 'sources', first.wiki_path)
  const duplicate = path.join(f.opts.wikiRoot, 'sources/webdav/duplicate.md')
  const original = `---\nsource: webdav\nsource_path: ${first.source_path}\nsource_revision: ${first.source_revision}\n---\n# Previous\n`
  await writeFile(page, original)
  await writeFile(duplicate, original)
  await f.git('add', '.')
  await f.git('commit', '-qm', 'conflicting target owner')
  const calls = []
  const result = await runIngestBatch(f.opts, async (batch) => {
    calls.push(batch.sources[0].source_key)
    return f.worker(batch)
  })
  assert.deepEqual(calls, [independent.source_key])
  assert.equal(result.status, 'partial')
  assert.equal(result.ingested, 1)
  assert.equal(result.summary.conflict, 2)
  assert.equal(result.remaining, 0)
  assert.equal(result.blocker, null)
  assert.equal(await readFile(page, 'utf8'), original)
  assert.equal(await readFile(duplicate, 'utf8'), original)
  assert.equal(await f.git('status', '--porcelain'), '')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
})

test('root-conflicted manifests still reserve targets before dispatch', async (t) => {
  const f = await fixture(t, 2)
  const [colliding, independent] = f.manifest.items
  colliding.wiki_path = 'shared/collision.md'
  await f.publish()
  for (const name of ['alpha', 'beta']) {
    await mkdir(path.join(f.opts.sourceRoot, name))
    await writeFile(
      path.join(f.opts.sourceRoot, name, 'manifest.json'),
      JSON.stringify({
        ...f.manifest,
        source: name,
        wiki_root: 'shared',
        items: [
          { ...colliding, wiki_path: name === 'alpha' ? colliding.wiki_path : 'shared/other.md' },
        ],
      }),
    )
  }
  await mkdir(path.join(f.opts.wikiRoot, 'sources/shared'))
  const page = path.join(f.opts.wikiRoot, 'sources', colliding.wiki_path)
  const original = `---\nsource: webdav\nsource_path: ${colliding.source_path}\nsource_revision: ${colliding.source_revision}\n---\n# Previous\n`
  await writeFile(page, original)
  await f.git('add', '.')
  await f.git('commit', '-qm', 'target of root-conflicted provider')
  const calls = []
  const result = await runIngestBatch(f.opts, async (batch) => {
    calls.push(batch.sources[0].source_key)
    return f.worker(batch)
  })
  assert.deepEqual(calls, [independent.source_key])
  assert.equal(result.status, 'partial')
  assert.equal(result.ingested, 1)
  assert.equal(result.summary.conflict, 1)
  assert.equal(result.summary.invalid, 2)
  assert.equal(result.remaining, 0)
  assert.equal(result.blocker, null)
  assert.equal(await readFile(page, 'utf8'), original)
  assert.equal(await f.git('status', '--porcelain'), '')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
})

test('invalid source metadata is skipped without poisoning independent publication', async (t) => {
  const f = await fixture(t)
  const page = path.join(f.opts.wikiRoot, 'sources/webdav/note-1.md')
  const original =
    '---\nsource: webdav\nsource_path: note-1.md\nsource_revision: invalid\n---\n# Prior content\n'
  await writeFile(page, original)
  await f.git('add', '.')
  await f.git('commit', '-qm', 'invalid prior metadata')
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'partial')
  assert.equal(result.ingested, 2)
  assert.equal(result.skipped, 1)
  assert.equal(await readFile(page, 'utf8'), original)
})

test('dirty Git prevents admission and preserves foreign work', async (t) => {
  const f = await fixture(t)
  const file = path.join(f.opts.wikiRoot, 'overview.md')
  await writeFile(file, 'Foreign work\n')
  await assert.rejects(
    runIngestBatch(f.opts, () => assert.fail('must not run')),
    /nicht sauber/,
  )
  assert.equal(await readFile(file, 'utf8'), 'Foreign work\n')
})

test('uncertain live changes, source races and conflict markers retain edits and stop honestly', async (t) => {
  for (const kind of [
    'worker',
    'revision',
    'marker',
    'instructions',
    'symlink',
    'skip-after-edit',
  ]) {
    await t.test(kind, async (t) => {
      const f = await fixture(t)
      let calls = 0
      const result = await runIngestBatch(f.opts, async (batch) => {
        calls++
        const text = await f.worker(batch)
        if (kind === 'worker') throw new Error('PRIVATE_NATIVE_ERROR')
        if (kind === 'revision') {
          f.manifest.items[0].source_revision = 'a'.repeat(64)
          f.manifest.items[0].frontmatter.source_revision = 'a'.repeat(64)
          await f.publish()
        }
        if (kind === 'marker')
          await writeFile(
            batch.sources[0].wiki_path,
            '<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> other\n',
          )
        if (kind === 'instructions')
          await writeFile(path.join(f.opts.wikiRoot, 'AGENTS.md'), 'Changed\n')
        if (kind === 'symlink') {
          await rm(batch.sources[0].wiki_path)
          await symlink(batch.sources[0].source_path, batch.sources[0].wiki_path)
        }
        return kind === 'skip-after-edit' ? 'SKIP: unsafe after edit' : text
      })
      assert.equal(result.status, 'blocked')
      assert.equal(result.commit, null)
      assert.equal(calls, 1)
      assert.notEqual(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
      assert.doesNotMatch(await readFile(result.report_path, 'utf8'), /PRIVATE_NATIVE_ERROR/)
      if (kind === 'marker')
        assert.match(await readFile(result.report_path, 'utf8'), /Git-Konfliktmarker/)
      await withIngestLock(
        path.join(f.opts.wikiRoot, '.git/wiki-ingest-publication.lock'),
        async () => {},
      )
    })
  }
})

test('report storage failures never stop remaining sources or lose commits', async (t) => {
  const f = await fixture(t)
  await writeFile(f.opts.root, 'Not a directory\n')
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'done')
  assert.equal(result.ingested, 3)
  assert.equal(result.commit, await f.git('rev-parse', 'HEAD'))
  assert.equal(result.report_path, null)
  assert.ok(result.report_error)
  assert.equal(await f.git('status', '--porcelain'), '')
})

test('post-commit report failure does not reclassify or repeat a committed source', async (t) => {
  const f = await fixture(t)
  const hook = path.join(f.opts.wikiRoot, '.git/hooks/post-commit')
  await writeFile(
    hook,
    `#!/usr/bin/env python3\nfrom pathlib import Path\nfor directory in Path('../journal/batches').iterdir():\n    for name in ('state.json', 'report.md'):\n        file = directory / name\n        if file.is_file():\n            file.unlink()\n            file.mkdir()\n`,
  )
  await chmod(hook, 0o700)
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'done')
  assert.equal(result.ingested, 3)
  assert.equal(result.commit, await f.git('rev-parse', 'HEAD'))
  assert.equal(result.report_path, null)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '4')
})

test('single-source filter never imports unrelated sources', async (t) => {
  const f = await fixture(t)
  const sourcePath = path.join(f.opts.sourceRoot, 'webdav/note-1.md')
  const result = await runIngestBatch({ ...f.opts, sourcePath }, f.worker)
  assert.equal(result.status, 'done')
  assert.equal(result.ingested, 1)
  assert.equal(result.remaining, 2)
  await assert.rejects(runIngestBatch({ ...f.opts, sourcePath }, f.worker), /nicht new\/outdated/)
})

test('Paperless revisions require an independent digest and matching source metadata', async (t) => {
  const f = await fixture(t, 0)
  const directory = path.join(f.opts.sourceRoot, 'paperless')
  await mkdir(directory)
  await mkdir(path.join(f.opts.wikiRoot, 'sources/paperless'))
  const fields = {
    paperless_id: '1',
    paperless_url: 'https://paperless.invalid/documents/1/',
    source_revision: 'b'.repeat(64),
  }
  const bytes = `---\n${Object.entries(fields)
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join('\n')}\n---\n# Synthetic OCR\n`
  await writeFile(path.join(directory, '1.md'), bytes)
  const item = {
    source_key: '1',
    source_path: '1.md',
    wiki_path: 'paperless/1.md',
    source_revision: fields.source_revision,
    source_sha256: sha(bytes),
    frontmatter: fields,
    claim: { paperless_id: '1' },
  }
  const manifest = { ...f.manifest, source: 'paperless', wiki_root: 'paperless', items: [item] }
  const publish = () => writeFile(path.join(directory, 'manifest.json'), JSON.stringify(manifest))
  await publish()
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'done', result.blocker)
  assert.match(
    await readFile(path.join(f.opts.wikiRoot, 'sources/paperless/1.md'), 'utf8'),
    /source_revision: "bbbb/,
  )
  fields.source_revision = item.source_revision = 'c'.repeat(64)
  delete item.source_sha256
  await publish()
  const missing = await runIngestBatch(f.opts, () => assert.fail('no digest'))
  assert.equal(missing.status, 'partial')
  assert.match(await readFile(missing.report_path, 'utf8'), /source_sha256/)
  item.source_sha256 = sha(bytes)
  await publish()
  const mismatched = await runIngestBatch(f.opts, () => assert.fail('metadata mismatch'))
  assert.equal(mismatched.status, 'partial')
  assert.match(await readFile(mismatched.report_path, 'utf8'), /Manifest/)
})

test('verified generation relocation is not mistaken for a changed source', async (t) => {
  const f = await fixture(t, 1)
  const result = await runIngestBatch(f.opts, async (batch) => {
    const text = await f.worker(batch)
    const item = f.manifest.items[0]
    await mkdir(path.join(f.opts.sourceRoot, 'webdav/generation'))
    await writeFile(
      path.join(f.opts.sourceRoot, 'webdav/generation/note-0.md'),
      await readFile(batch.sources[0].source_path),
    )
    item.source_path = 'generation/note-0.md'
    await f.publish()
    return text
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(result.ingested, 1)
})

test('cancellation preserves prior commits and live partial edits', async (t) => {
  const f = await fixture(t)
  const controller = new AbortController()
  let calls = 0
  const result = await runIngestBatch({ ...f.opts, signal: controller.signal }, async (batch) => {
    const text = await f.worker(batch)
    if (++calls === 2) controller.abort()
    return text
  })
  assert.equal(result.status, 'blocked')
  assert.equal(result.ingested, 1)
  assert.equal(calls, 2)
  assert.notEqual(await f.git('status', '--porcelain'), '')
})

test('kernel lock covers every worker and excludes other processes', async (t) => {
  const f = await fixture(t)
  const result = await runIngestBatch(f.opts, async (batch) => {
    await assert.rejects(
      exec('python3', [
        '-c',
        'import fcntl,sys; f=open(sys.argv[1],"r+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
        path.join(f.opts.wikiRoot, '.git/wiki-ingest-publication.lock'),
      ]),
    )
    return f.worker(batch)
  })
  assert.equal(result.status, 'done')
})

test('session adapter uses the parent model, final text and waits for actual worker exit', async () => {
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
        { type: 'assistant', content: [{ type: 'text', text: 'Report' }] },
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
    'Report',
  )
  assert.deepEqual(events[0][1].model, { providerID: 'synthetic', id: 'fixture' })
  assert.equal(events[0][1].agent, 'wiki-ingest')
  assert.deepEqual(
    events.slice(-2).map(([name]) => name),
    ['stop', 'wait'],
  )
})
