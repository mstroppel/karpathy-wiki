import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { promisify } from 'node:util'
import { inspectIngestBatch, runIngestBatch } from '../config/tools/wiki_ingest_batch_core.mjs'
import { createBatchWorker, workerEvidence } from '../config/tools/wiki_ingest_batch_worker.mjs'
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
    return {
      content:
        '**Befunde:** Source line 1; newly added.\n**Widersprüche:** None observed.\n**Extraktionsgrenzen:** Fully read line 1.',
      source_written: true,
      errors: [],
    }
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

test('a failed worker leaves only a private draft and independent sources finish', async (t) => {
  const f = await fixture(t, 2)
  const result = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async (batch) => {
    assert.notEqual(batch.wiki_root, f.opts.wikiRoot)
    const result = await f.worker(batch)
    if (batch.sources[0].source_key === 'note-0.md') throw new Error('Synthetic worker failure')
    return result
  })
  assert.equal(result.status, 'paused')
  assert.equal(result.ingested, 1)
  assert.equal(result.questions.length, 1)
  await assert.rejects(readFile(path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')), {
    code: 'ENOENT',
  })
  assert.equal(await f.git('status', '--porcelain'), '')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
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

test('missing/corrupt sources and refusal stay open while independent sources finish', async (t) => {
  for (const kind of ['missing', 'digest', 'worker', 'skip']) {
    await t.test(kind, async (t) => {
      const f = await fixture(t)
      const target = path.join(f.opts.sourceRoot, 'webdav/note-1.md')
      if (kind === 'missing') await rm(target)
      if (kind === 'digest') await writeFile(target, 'Changed outside publication\n')
      const result = await runIngestBatch(f.opts, async (batch) => {
        if (batch.sources[0].source_path === target) {
          if (kind === 'worker') throw new Error('PRIVATE_NATIVE_ERROR')
          if (kind === 'skip')
            return { content: 'SKIP: No legible source text.', source_written: false, errors: [] }
        }
        return f.worker(batch)
      })
      assert.equal(result.status, 'paused', result.blocker)
      assert.equal(result.ingested, 2)
      assert.equal(result.skipped, 0)
      assert.equal(result.awaiting_user, 1)
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
  assert.equal(result.status, 'paused')
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
  assert.equal(result.status, 'paused')
  assert.equal(result.ingested, 1)
  assert.equal(result.summary.conflict, 2)
  assert.equal(result.remaining, 0)
  assert.equal(result.blocker, 'provider_or_metadata_diagnostics')
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
  assert.equal(result.status, 'paused')
  assert.equal(result.ingested, 1)
  assert.equal(result.summary.conflict, 1)
  assert.equal(result.summary.invalid, 2)
  assert.equal(result.remaining, 0)
  assert.equal(result.blocker, 'provider_or_metadata_diagnostics')
  assert.equal(await readFile(page, 'utf8'), original)
  assert.equal(await f.git('status', '--porcelain'), '')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
})

test('invalid source metadata stays open without poisoning independent publication', async (t) => {
  const f = await fixture(t)
  const page = path.join(f.opts.wikiRoot, 'sources/webdav/note-1.md')
  const original =
    '---\nsource: webdav\nsource_path: note-1.md\nsource_revision: invalid\n---\n# Prior content\n'
  await writeFile(page, original)
  await f.git('add', '.')
  await f.git('commit', '-qm', 'invalid prior metadata')
  const result = await runIngestBatch(f.opts, f.worker)
  assert.equal(result.status, 'paused')
  assert.equal(result.ingested, 2)
  assert.equal(result.awaiting_user, 1)
  assert.equal(await readFile(page, 'utf8'), original)
})

test('dirty Git prevents admission and preserves foreign work', async (t) => {
  const f = await fixture(t)
  const file = path.join(f.opts.wikiRoot, 'overview.md')
  await writeFile(file, 'Foreign work\n')
  const result = await runIngestBatch(f.opts, () => assert.fail('must not run'))
  assert.equal(result.status, 'paused')
  assert.match(result.questions[0].reason, /dirty_wiki/)
  assert.deepEqual(result.admission.files, [{ path: 'overview.md', hash: sha('Foreign work\n') }])
  assert.equal(await readFile(file, 'utf8'), 'Foreign work\n')
})

test('unchanged dirty retries preserve decision IDs and a real change refreshes them', async (t) => {
  const f = await fixture(t, 1)
  const file = path.join(f.opts.wikiRoot, 'overview.md')
  await writeFile(file, 'Unattributed work\n')
  const paused = await runIngestBatch(f.opts, () => assert.fail())
  const retried = await answer(f, paused, paused.questions[0], 'retry', () => assert.fail())
  assert.equal(retried.questions[0].id, paused.questions[0].id)
  const again = await answer(f, retried, retried.questions[0], 'retry', () => assert.fail())
  assert.equal(again.questions[0].id, paused.questions[0].id)
  await writeFile(file, 'Changed unattributed work\n')
  const changed = await answer(f, again, again.questions[0], 'retry', () => assert.fail())
  assert.notEqual(changed.questions[0].id, paused.questions[0].id)
  assert.equal(await readFile(file, 'utf8'), 'Changed unattributed work\n')
})

test('failed dirty reprocessing retains exact approval for source retry, not after defer', async (t) => {
  for (const choice of ['retry', 'defer'])
    await t.test(choice, async (t) => {
      const f = await fixture(t, 1)
      const file = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
      await writeFile(file, '# Historical uncommitted page\n')
      const paused = await runIngestBatch(f.opts, () => assert.fail())
      const failed = await answer(
        f,
        paused,
        paused.questions[0],
        'reprocess',
        async (batch) => {
          await f.worker(batch)
          return {
            source_written: true,
            errors: [{ code: 'worker_permission_denied', tool: 'patch' }],
          }
        },
        { maxAttempts: 1 },
      )
      assert.equal(failed.blocker, null)
      assert.equal(failed.questions.length, 1)
      assert.equal(failed.questions[0].reason, 'worker_permission_denied')
      const continued = await answer(f, failed, failed.questions[0], choice)
      if (choice === 'retry') {
        assert.equal(continued.status, 'done', continued.blocker)
        assert.equal(continued.ingested, 1)
        assert.equal(await f.git('status', '--porcelain'), '')
      } else {
        assert.equal(continued.skipped, 1)
        assert.match(continued.blocker, /dirty_wiki/)
        assert.equal(await readFile(file, 'utf8'), '# Historical uncommitted page\n')
      }
    })
})

test('diagnostics find the owner and exact worker failure across chats without writes', async (t) => {
  const f = await fixture(t, 12)
  const paused = await runIngestBatch(
    { ...f.opts, ownerSession: 'ses_original', maxAttempts: 1 },
    async () => ({
      session_id: 'ses_worker',
      source_written: false,
      content: 'PRIVATE_TRANSCRIPT',
      errors: [{ code: 'worker_permission_denied', tool: 'patch' }],
    }),
  )
  await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), 'Unattributed work\n')
  const statePath = path.join(f.opts.root, 'batches', paused.batch_id, 'state.json')
  const before = await readFile(statePath, 'utf8')
  const pointer = await readFile(path.join(f.opts.root, 'active.json'), 'utf8')
  const objects = await f.git('count-objects', '-v')
  const result = await inspectIngestBatch({ ...f.opts, ownerSession: 'ses_new' })
  assert.equal(result.owner_session, 'ses_original')
  assert.equal(result.continuation, 'original_session_only')
  assert.equal(result.sources.length, 10)
  assert.equal(result.sources[0].worker_session, 'ses_worker')
  assert.equal(result.next_offset, 10)
  assert.deepEqual(result.dirty_paths, ['overview.md'])
  assert.deepEqual(result.sources[0].worker_errors, [
    { code: 'worker_permission_denied', tool: 'patch' },
  ])
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE_TRANSCRIPT|decision_id/)
  const next = await inspectIngestBatch({ ...f.opts, offset: result.next_offset })
  assert.equal(next.sources.length, 2)
  assert.equal(next.next_offset, null)
  assert.equal(await readFile(statePath, 'utf8'), before)
  assert.equal(await readFile(path.join(f.opts.root, 'active.json'), 'utf8'), pointer)
  assert.equal(await f.git('count-objects', '-v'), objects)
  await rm(paused.report_path)
  assert.equal((await inspectIngestBatch(f.opts)).report_path, null)
  await assert.rejects(
    answer(f, paused, paused.questions[0], 'defer', () => assert.fail(), {
      ownerSession: 'ses_new',
    }),
    /decision_wrong_session/,
  )
})

test('retained dirty approval cannot mask changed live bytes or unrelated provider errors', async (t) => {
  for (const kind of ['changed', 'provider'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 1)
      const file = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
      await writeFile(file, '# Historical page\n')
      const paused = await runIngestBatch(f.opts, () => assert.fail())
      const failed = await answer(
        f,
        paused,
        paused.questions[0],
        'reprocess',
        async () => ({
          source_written: false,
          errors: [{ code: 'worker_permission_denied', tool: 'patch' }],
        }),
        { maxAttempts: 1 },
      )
      if (kind === 'changed') await writeFile(file, '# Externally changed page\n')
      else {
        await mkdir(path.join(f.opts.sourceRoot, 'broken'))
        await writeFile(path.join(f.opts.sourceRoot, 'broken/manifest.json'), '{}')
      }
      const observed = await runIngestBatch(f.opts, () => assert.fail())
      assert.match(
        observed.blocker,
        kind === 'changed' ? /dirty_wiki/ : /provider_or_metadata_diagnostics/,
      )
      if (kind === 'changed') {
        await assert.rejects(
          answer(f, failed, failed.questions[0], 'retry', () => assert.fail('stale approval')),
          /decision_not_open/,
        )
        assert.equal(await readFile(file, 'utf8'), '# Externally changed page\n')
      }
    })
})

test('diagnostics never create work and reject unsafe pointers or state', async (t) => {
  const f = await fixture(t, 1)
  assert.deepEqual(await inspectIngestBatch(f.opts), { status: 'idle', batch_id: null })
  await assert.rejects(readFile(path.join(f.opts.root, 'active.json')), { code: 'ENOENT' })
  await assert.rejects(inspectIngestBatch({ ...f.opts, offset: -1 }), /invalid_diagnostic_offset/)
  await mkdir(f.opts.root)
  await writeFile(
    path.join(f.opts.root, 'active.json'),
    JSON.stringify({ batch_id: '../../private' }),
  )
  await assert.rejects(inspectIngestBatch(f.opts), /invalid_active_batch/)
  await rm(path.join(f.opts.root, 'active.json'))
  const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async () => ({
    source_written: false,
  }))
  const file = path.join(f.opts.root, 'batches', paused.batch_id, 'state.json')
  await rm(file)
  const privateFile = path.join(f.opts.root, 'private.json')
  await writeFile(privateFile, 'PRIVATE_ERROR_BODY')
  await symlink(privateFile, file)
  await assert.rejects(inspectIngestBatch(f.opts), /unsafe_file/)
  await rm(file)
  await writeFile(file, 'PRIVATE_INVALID_JSON')
  await assert.rejects(inspectIngestBatch(f.opts), (error) => !error.message.includes('PRIVATE'))
})

test('incomplete decision/pagination inputs cannot create or process a batch', async (t) => {
  const f = await fixture(t, 1)
  for (const input of [
    { questionOffset: 0 },
    { answer: 'retry' },
    { decisionId: 'missing' },
    { decisionId: 'missing', answer: 'retry' },
    { batchId: 'batch-1-aaaaaaaaaaaa', questionOffset: 0, decisionId: 'missing', answer: 'retry' },
  ])
    await assert.rejects(
      runIngestBatch({ ...f.opts, ...input }, () => assert.fail('invalid input started worker')),
      /incomplete_decision|invalid_question_request/,
    )
  await assert.rejects(readFile(path.join(f.opts.root, 'active.json')), { code: 'ENOENT' })
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
})

test('dirty admission never offers adoption of a nondispatchable source', async (t) => {
  for (const choice of ['defer', 'unresolved', 'awaiting_user', 'ingested'])
    await t.test(choice, async (t) => {
      const f = await fixture(t, 2)
      const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async (batch) =>
        choice === 'ingested' && batch.sources[0].source_key === 'note-0.md'
          ? f.worker(batch)
          : { source_written: false },
      )
      if (['defer', 'unresolved'].includes(choice))
        await answer(f, paused, paused.questions[0], choice, () => assert.fail())
      const file = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
      await writeFile(file, '# Foreign historical change\n')
      const inspected = await runIngestBatch(f.opts, () => assert.fail())
      const result = await answer(
        f,
        inspected,
        inspected.questions.find((question) => question.source_index === null),
        'retry',
        () => assert.fail('dirty admission must precede workers'),
      )
      const global = result.questions.find((question) => question.source_index === null)
      assert.ok(global)
      assert.ok(!global.options.includes('reprocess'))
      assert.equal(await readFile(file, 'utf8'), '# Foreign historical change\n')
    })
})

test('dirty questions refresh their adoption when the dirty page is removed or replaced', async (t) => {
  for (const kind of ['removed', 'replaced'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 2)
      const first = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
      await writeFile(first, '# Historical draft\n')
      const paused = await runIngestBatch(f.opts, () => assert.fail())
      await rm(first)
      const second = path.join(f.opts.wikiRoot, 'sources/webdav/note-1.md')
      if (kind === 'replaced') await writeFile(second, '# Replacement draft\n')
      const refreshed = await answer(f, paused, paused.questions[0], 'reprocess', () =>
        assert.fail('stale decision must not dispatch'),
      )
      assert.notEqual(refreshed.questions[0].id, paused.questions[0].id)
      assert.equal(refreshed.questions[0].options.includes('reprocess'), kind === 'replaced')
      if (kind === 'replaced') assert.equal(refreshed.questions[0].target.wiki_path, second)
      const calls = []
      const done = await answer(
        f,
        refreshed,
        refreshed.questions[0],
        kind === 'replaced' ? 'reprocess' : 'retry',
        async (batch) => {
          calls.push(batch.sources[0].source_key)
          return f.worker(batch)
        },
      )
      assert.equal(done.status, 'done', done.blocker)
      assert.equal(done.ingested, 2)
      assert.equal(calls[0], kind === 'replaced' ? 'note-1.md' : 'note-0.md')
    })
})

test('invalid drafts remain private; foreign live changes stop publication without overwrite', async (t) => {
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
      const result = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async (batch) => {
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
        return kind === 'skip-after-edit' ? { ...text, content: 'SKIP: unsafe after edit' } : text
      })
      assert.equal(result.status, 'paused')
      assert.equal(result.ingested, kind === 'revision' ? 2 : 0)
      assert.equal(calls, kind === 'instructions' ? 1 : 3)
      assert.equal(Boolean(await f.git('status', '--porcelain')), kind === 'instructions')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), kind === 'revision' ? '3' : '1')
      assert.doesNotMatch(await readFile(result.report_path, 'utf8'), /PRIVATE_NATIVE_ERROR/)
      if (kind === 'marker')
        assert.match(await readFile(result.report_path, 'utf8'), /conflict_markers/)
      await withIngestLock(
        path.join(f.opts.wikiRoot, '.git/wiki-ingest-publication.lock'),
        async () => {},
      )
    })
  }
})

test('control storage failure prevents any publication', async (t) => {
  const f = await fixture(t)
  await writeFile(f.opts.root, 'Not a directory\n')
  await assert.rejects(runIngestBatch(f.opts, () => assert.fail('no control checkpoint')))
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
  assert.equal(await f.git('status', '--porcelain'), '')
})

test('post-commit report failure does not reclassify or repeat a committed source', async (t) => {
  const f = await fixture(t)
  const result = await runIngestBatch(
    {
      ...f.opts,
      onPhase: async (phase) => {
        if (phase !== 'source_recorded') return
        for (const directory of await readdir(path.join(f.opts.root, 'batches')))
          await mkdir(path.join(f.opts.root, 'batches', directory, 'report.md'), {
            recursive: true,
          })
      },
    },
    f.worker,
  )
  assert.equal(result.status, 'done')
  assert.equal(result.ingested, 3)
  assert.equal(result.commit, await f.git('rev-parse', 'HEAD'))
  assert.equal(result.report_path, null)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '4')
})

async function answer(f, result, question, choice = 'retry', worker = f.worker, extra = {}) {
  return runIngestBatch(
    {
      ...f.opts,
      batchId: result.batch_id,
      decisionId: question.id,
      answer: choice,
      authorize: async () => {},
      ...extra,
    },
    worker,
  )
}

test('repair inspects and continues an owned draft instead of blindly repeating from scratch', async (t) => {
  const f = await fixture(t, 2)
  let first = true
  let repaired = false
  const result = await runIngestBatch(f.opts, async (batch) => {
    if (batch.sources[0].source_key === 'note-0.md' && first) {
      first = false
      await f.worker(batch)
      return { source_written: true, errors: [{ code: 'worker_tool_failed', tool: 'patch' }] }
    }
    if (batch.repair_reason) {
      assert.match(await readFile(batch.sources[0].wiki_path, 'utf8'), /Supported finding/)
      assert.deepEqual(batch.repair_files, ['sources/webdav/note-0.md'])
      repaired = true
    }
    return f.worker(batch)
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(result.repaired, 1)
  assert.equal(repaired, true)
  assert.equal((await readdir(path.join(f.opts.root, 'drafts', result.batch_id))).length, 0)
})

test('bound answers finish only the failed source and cannot be replayed or redirected', async (t) => {
  const f = await fixture(t, 2)
  const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async (batch) =>
    batch.sources[0].source_key === 'note-0.md' ? { source_written: false } : f.worker(batch),
  )
  assert.equal(paused.ingested, 1)
  const inspected = await runIngestBatch(f.opts, () => assert.fail('no unattended resumption'))
  assert.equal(inspected.batch_id, paused.batch_id)
  assert.equal(inspected.questions[0].id, paused.questions[0].id)
  const done = await answer(f, paused, paused.questions[0], 'retry', async (batch) => {
    assert.equal(batch.sources[0].source_key, 'note-0.md')
    return f.worker(batch)
  })
  assert.equal(done.status, 'done', done.blocker)
  assert.equal(done.ingested, 2)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
  await assert.rejects(answer(f, paused, paused.questions[0]), /batch_not_active/)
})

test('changed revision, foreign edits, changed draft and wrong-session answers require a fresh decision', async (t) => {
  for (const kind of ['revision', 'revoked', 'foreign', 'draft', 'session', 'approval-race'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 1)
      const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async (batch) => {
        await f.worker(batch)
        return {
          source_written: true,
          errors: [{ code: 'worker_permission_denied', tool: 'patch' }],
        }
      })
      if (kind === 'revision') {
        const bytes = 'Changed synthetic source.\n'
        await writeFile(path.join(f.opts.sourceRoot, 'webdav/note-0.md'), bytes)
        f.manifest.items[0].source_revision = f.manifest.items[0].frontmatter.source_revision =
          sha(bytes)
        await f.publish()
      }
      if (kind === 'revoked') {
        const item = f.manifest.items.pop()
        f.manifest.revoked.push({ source_key: item.source_key, claim: item.claim })
        await f.publish()
      }
      if (kind === 'foreign')
        await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), 'Foreign\n')
      if (kind === 'draft') {
        const [id] = await readdir(path.join(f.opts.root, 'drafts', paused.batch_id))
        await writeFile(
          path.join(f.opts.root, 'drafts', paused.batch_id, id, 'wiki/sources/webdav/note-0.md'),
          '# Changed proposal\n',
        )
      }
      const extra =
        kind === 'session'
          ? { ownerSession: 'ses_other' }
          : kind === 'approval-race'
            ? {
                authorize: async () =>
                  writeFile(path.join(f.opts.wikiRoot, 'overview.md'), 'Changed during approval\n'),
              }
            : {}
      if (kind === 'session')
        await assert.rejects(
          answer(f, paused, paused.questions[0], 'retry', () => assert.fail(), extra),
          /decision_wrong_session/,
        )
      else {
        const fresh = await answer(
          f,
          paused,
          paused.questions[0],
          'retry',
          () => assert.fail('stale answer started worker'),
          extra,
        )
        assert.notEqual(fresh.questions[0].id, paused.questions[0].id)
      }
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
    })
})

test('explicit defer/unresolved/cancel are not automatic skips and approval rejection stays open', async (t) => {
  for (const choice of ['defer', 'unresolved', 'cancel', 'reject'])
    await t.test(choice, async (t) => {
      const f = await fixture(t, 1)
      const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async () => ({
        source_written: false,
      }))
      if (choice === 'reject') {
        await assert.rejects(
          answer(f, paused, paused.questions[0], 'retry', () => assert.fail(), {
            authorize: () => {
              throw new Error('Synthetic rejection')
            },
          }),
          /Synthetic rejection/,
        )
        const still = await runIngestBatch(f.opts, () => assert.fail())
        assert.equal(still.questions[0].id, paused.questions[0].id)
      } else {
        const result = await answer(f, paused, paused.questions[0], choice, () => assert.fail())
        assert.equal(result.status, choice === 'cancel' ? 'cancelled' : 'partial')
        assert.equal(result.skipped, choice === 'defer' ? 1 : 0)
        assert.equal(result.unresolved, choice === 'unresolved' ? 1 : 0)
      }
    })
})

test('real process termination resumes verified publication without duplicate commits or logs', async (t) => {
  for (const [phase, count] of [
    ['publication_prepared'],
    ['file_installed', 1],
    ['file_installed', 2],
    ['file_installed', 3],
    ['index_installed'],
    ['commit_installed'],
    ['source_recorded'],
  ])
    await t.test(`${phase}-${count ?? ''}`, async (t) => {
      const f = await fixture(t, 1)
      const config = path.join(f.opts.root, '../opts.json')
      await writeFile(config, JSON.stringify(f.opts))
      await assert.rejects(
        exec(process.execPath, [
          'tests/fixtures/ingest-crash.mjs',
          config,
          phase,
          String(count ?? 0),
        ]),
        (error) => error.signal === 'SIGKILL',
      )
      let result = await runIngestBatch(f.opts, () => assert.fail('resume must inspect first'))
      if (result.status === 'paused')
        result = await answer(
          f,
          result,
          result.questions.find((item) => item.source_index === null),
          'retry',
          () => assert.fail('prepared result must not be reinterpreted'),
        )
      assert.equal(result.status, 'done', result.blocker)
      assert.equal(result.ingested, 1)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.deepEqual(await readdir(path.join(f.opts.root, 'drafts', result.batch_id)), [])
      assert.equal(
        (await readFile(path.join(f.opts.wikiRoot, 'log.md'), 'utf8')).match(/Ingest batch-/g)
          ?.length,
        1,
      )
    })
})

test('foreign edits after interrupted publication are preserved and prevent completion', async (t) => {
  const f = await fixture(t, 1)
  let stop = true
  const paused = await runIngestBatch(
    {
      ...f.opts,
      onPhase: (phase) => {
        if (phase === 'file_installed' && stop) {
          stop = false
          throw new Error('Synthetic power loss')
        }
      },
    },
    f.worker,
  )
  await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), 'Foreign concurrent edit\n')
  const fresh = await runIngestBatch(f.opts, () => assert.fail())
  const result = await answer(
    f,
    fresh,
    fresh.questions.find((item) => item.source_index === null),
    'retry',
    () => assert.fail(),
  )
  assert.equal(result.status, 'paused')
  assert.equal(
    await readFile(path.join(f.opts.wikiRoot, 'overview.md'), 'utf8'),
    'Foreign concurrent edit\n',
  )
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
  assert.equal(paused.ingested, 0)
})

test('cancellation during publication offers only explicitly approved technical completion', async (t) => {
  const f = await fixture(t, 2)
  const controller = new AbortController()
  const cancelled = await runIngestBatch(
    {
      ...f.opts,
      signal: controller.signal,
      onPhase: (phase) => {
        if (phase === 'file_installed') controller.abort()
      },
    },
    f.worker,
  )
  assert.equal(cancelled.status, 'cancelled')
  assert.equal(cancelled.questions.length, 1)
  assert.deepEqual(cancelled.questions[0].options, ['retry'])
  const inspected = await runIngestBatch(f.opts, () => assert.fail('never wake cancelled work'))
  assert.equal(inspected.status, 'cancelled')
  assert.equal(inspected.questions[0].id, cancelled.questions[0].id)
  const recovered = await answer(f, inspected, inspected.questions[0], 'retry', () =>
    assert.fail('only finish prepared publication'),
  )
  assert.equal(recovered.status, 'cancelled')
  assert.equal(recovered.ingested, 1)
  assert.equal(recovered.questions.length, 0)
  assert.equal(await f.git('status', '--porcelain'), '')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
  const next = await runIngestBatch(f.opts, f.worker)
  assert.notEqual(next.batch_id, cancelled.batch_id)
  assert.equal(next.status, 'done')
  assert.equal(next.ingested, 1)
})

test('refreshed questions display and process the same freshly approved revision', async (t) => {
  const f = await fixture(t, 1)
  const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async () => ({
    source_written: false,
  }))
  const bytes = 'Approved new synthetic revision.\n'
  await writeFile(path.join(f.opts.sourceRoot, 'webdav/note-0.md'), bytes)
  f.manifest.items[0].source_revision = f.manifest.items[0].frontmatter.source_revision = sha(bytes)
  await f.publish()
  const refreshed = await answer(f, paused, paused.questions[0], 'retry', () =>
    assert.fail('stale approval'),
  )
  assert.equal(refreshed.questions[0].target.source_revision, sha(bytes))
  const done = await answer(f, refreshed, refreshed.questions[0], 'retry', async (batch) => {
    assert.equal(batch.sources[0].source_revision, refreshed.questions[0].target.source_revision)
    return f.worker(batch)
  })
  assert.equal(done.status, 'done')
})

test('an approved continuation inspects and reuses a bound partial draft after reopening', async (t) => {
  const f = await fixture(t, 1)
  const paused = await runIngestBatch({ ...f.opts, maxAttempts: 1 }, async (batch) => {
    await writeFile(batch.sources[0].wiki_path, '# Retained synthetic partial\n')
    return { source_written: true, errors: [{ code: 'worker_permission_denied', tool: 'write' }] }
  })
  const inspected = await runIngestBatch(f.opts, () =>
    assert.fail('inspection never starts workers'),
  )
  assert.equal(inspected.batch_id, paused.batch_id)
  const result = await answer(f, inspected, inspected.questions[0], 'retry', async (batch) => {
    assert.deepEqual(batch.repair_files, ['sources/webdav/note-0.md'])
    assert.match(await readFile(batch.sources[0].wiki_path, 'utf8'), /Retained synthetic partial/)
    return f.worker(batch)
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
})

test('a permission rejection anywhere in native errors prevents automatic retries', async (t) => {
  const f = await fixture(t, 1)
  let calls = 0
  const result = await runIngestBatch(f.opts, async () => {
    calls++
    return {
      source_written: false,
      errors: [
        { code: 'worker_tool_failed', tool: 'patch' },
        { code: 'worker_permission_denied', tool: 'write' },
      ],
    }
  })
  assert.equal(calls, 1)
  assert.equal(result.status, 'paused')
  assert.equal(result.questions[0].reason, 'worker_permission_denied')
})

test('Paperless preparation and repair remain bound to the original processing digest', async (t) => {
  for (const phase of ['preparation', 'installation', 'repair'])
    await t.test(phase, async (t) => {
      const f = await fixture(t, 1)
      const key = '3246.md'
      await mkdir(path.join(f.opts.sourceRoot, 'paperless'))
      await mkdir(path.join(f.opts.wikiRoot, 'sources/paperless'))
      const original = `---\npaperless_id: "3246"\npaperless_url: "https://paperless.invalid/documents/3246/"\nsource_revision: ${'a'.repeat(64)}\n---\nOriginal synthetic fact.\n`
      const changed = original.replace('Original', 'Changed')
      const manifest = {
        ...f.manifest,
        source: 'paperless',
        wiki_root: 'paperless',
        items: [
          {
            source_key: '3246',
            source_path: key,
            wiki_path: `paperless/${key}`,
            source_revision: 'a'.repeat(64),
            source_sha256: sha(original),
            frontmatter: {
              paperless_id: '3246',
              paperless_url: 'https://paperless.invalid/documents/3246/',
              source_revision: 'a'.repeat(64),
            },
            claim: { paperless_id: '3246' },
          },
        ],
      }
      const sourcePath = path.join(f.opts.sourceRoot, 'paperless', key)
      await writeFile(sourcePath, original)
      const publish = () =>
        writeFile(path.join(f.opts.sourceRoot, 'paperless/manifest.json'), JSON.stringify(manifest))
      await publish()
      const change = async () => {
        await writeFile(sourcePath, changed)
        manifest.items[0].source_sha256 = sha(changed)
        await publish()
      }
      let calls = 0
      const result = await runIngestBatch(
        {
          ...f.opts,
          sourcePath,
          maxAttempts: 2,
          onPhase: async (step) => {
            if (
              (phase === 'preparation' && step === 'publication_prepared') ||
              (phase === 'installation' && step === 'file_installed')
            )
              await change()
          },
        },
        async (batch) => {
          calls++
          if (phase === 'repair' && calls === 1) {
            await f.worker(batch)
            await change()
            return { source_written: true, errors: [{ code: 'worker_tool_failed', tool: 'patch' }] }
          }
          if (calls > 1) {
            assert.deepEqual(batch.repair_files, [])
            await assert.rejects(readFile(batch.sources[0].wiki_path), { code: 'ENOENT' })
          }
          return f.worker(batch)
        },
      )
      assert.equal(result.status, phase === 'repair' ? 'done' : 'paused')
      assert.equal(result.ingested, phase === 'repair' ? 1 : 0)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), phase === 'repair' ? '2' : '1')
      if (phase !== 'repair') {
        const still = await answer(f, result, result.questions[0], 'retry', () =>
          assert.fail('digest mismatch must not reinterpret'),
        )
        assert.equal(still.status, 'paused')
        assert.equal(still.ingested, 0)
        assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
      }
    })
})

test('single-source filter never imports unrelated sources', async (t) => {
  const f = await fixture(t)
  const sourcePath = path.join(f.opts.sourceRoot, 'webdav/note-1.md')
  const result = await runIngestBatch({ ...f.opts, sourcePath }, f.worker)
  assert.equal(result.status, 'done')
  assert.equal(result.ingested, 1)
  assert.equal(result.remaining, 2)
  await assert.rejects(runIngestBatch({ ...f.opts, sourcePath }, f.worker), /source_not_pending/)
})

test('a refused outdated source never receives a new revision or commit', async (t) => {
  const f = await fixture(t, 1)
  await runIngestBatch(f.opts, f.worker)
  const page = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
  const before = await readFile(page, 'utf8')
  const head = await f.git('rev-parse', 'HEAD')
  const source = path.join(f.opts.sourceRoot, 'webdav/note-0.md')
  await writeFile(source, 'Changed synthetic fact.\n')
  f.manifest.items[0].source_revision = sha(await readFile(source))
  f.manifest.items[0].frontmatter.source_revision = f.manifest.items[0].source_revision
  await f.publish()
  const result = await runIngestBatch(f.opts, async () => ({
    content: 'Keine Änderungen: vorgeschriebene Prüfung nicht verfügbar.',
    source_written: false,
    errors: [],
  }))
  assert.equal(result.status, 'paused')
  assert.equal(result.ingested, 0)
  assert.equal(result.remaining, 1)
  assert.equal(await readFile(page, 'utf8'), before)
  assert.equal(await f.git('rev-parse', 'HEAD'), head)
  assert.match(await readFile(result.report_path, 'utf8'), /worker_no_source_write/)
})

test('an explicit source rewrite may retain unchanged findings for a new revision', async (t) => {
  const f = await fixture(t, 1)
  await runIngestBatch(f.opts, f.worker)
  const file = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
  const before = await readFile(file, 'utf8')
  const source = path.join(f.opts.sourceRoot, 'webdav/note-0.md')
  await writeFile(source, 'Changed synthetic source; same supported finding.\n')
  const revision = sha(await readFile(source))
  f.manifest.items[0].source_revision = revision
  f.manifest.items[0].frontmatter.source_revision = revision
  await f.publish()
  const result = await runIngestBatch(f.opts, async (batch) => {
    await writeFile(batch.sources[0].wiki_path, before)
    return {
      content: 'Fully reread; supported finding unchanged.',
      source_written: true,
      errors: [],
    }
  })
  assert.equal(result.ingested, 1)
  assert.equal(result.remaining, 0)
  assert.match(await readFile(file, 'utf8'), new RegExp(revision))
})

test('worker refusal, permission error and overview-only edits have distinct safe outcomes', async (t) => {
  for (const kind of ['refusal', 'permission', 'overview', 'error-after-write']) {
    await t.test(kind, async (t) => {
      const f = await fixture(t, 1)
      if (kind === 'overview')
        await writeFile(path.join(f.opts.wikiRoot, 'overview.md'), '# Baseline\n')
      if (kind === 'overview') {
        await f.git('add', '.')
        await f.git('commit', '-qm', 'baseline overview')
      }
      const head = await f.git('rev-parse', 'HEAD')
      const result = await runIngestBatch(f.opts, async (batch) => {
        if (kind === 'overview')
          await writeFile(path.join(batch.wiki_root, 'overview.md'), '# Worker partial edit\n')
        if (kind === 'error-after-write') await f.worker(batch)
        return {
          content: 'Synthetic refused or incomplete worker.',
          source_written: kind === 'error-after-write',
          errors:
            kind.includes('permission') || kind === 'error-after-write'
              ? [{ code: 'worker_permission_denied', tool: 'patch' }]
              : [],
        }
      })
      assert.equal(result.ingested, 0)
      assert.equal(result.status, 'paused')
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-parse', 'HEAD'), head)
      const report = await readFile(result.report_path, 'utf8')
      assert.match(
        report,
        kind === 'permission' || kind === 'error-after-write'
          ? /worker_permission_denied/
          : /worker_no_source_write/,
      )
    })
  }
})

test('revocation before dispatch or during a worker never publishes the selected source', async (t) => {
  for (const when of ['before', 'during']) {
    await t.test(when, async (t) => {
      const f = await fixture(t, 1)
      const revoke = async () => {
        const [item] = f.manifest.items.splice(0)
        f.manifest.revoked.push({ source_key: item.source_key, claim: item.claim })
        await f.publish()
      }
      if (when === 'before') await revoke()
      const result = await runIngestBatch(f.opts, async (batch) => {
        const result = await f.worker(batch)
        await revoke()
        return result
      })
      assert.equal(result.ingested ?? 0, 0)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
      assert.equal(result.status, when === 'during' ? 'paused' : 'done')
    })
  }
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
  assert.equal(missing.status, 'paused')
  assert.match(await readFile(missing.report_path, 'utf8'), /paperless_digest_missing/)
  item.source_sha256 = sha(bytes)
  await publish()
  const mismatched = await runIngestBatch(f.opts, () => assert.fail('metadata mismatch'))
  assert.equal(mismatched.status, 'paused')
  assert.match(await readFile(mismatched.report_path, 'utf8'), /paperless|source_sha256/)
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

test('cancellation preserves prior commits and keeps partial work private', async (t) => {
  const f = await fixture(t)
  const controller = new AbortController()
  let calls = 0
  const result = await runIngestBatch({ ...f.opts, signal: controller.signal }, async (batch) => {
    const text = await f.worker(batch)
    if (++calls === 2) controller.abort()
    return text
  })
  assert.equal(result.status, 'cancelled')
  assert.equal(result.ingested, 1)
  assert.equal(calls, 2)
  assert.equal(await f.git('status', '--porcelain'), '')
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

test('session adapter uses the parent model, final text and waits for actual worker exit', async (t) => {
  const admissionRoot = await mkdtemp(path.join(os.tmpdir(), 'ingest-admission-'))
  t.after(() => rm(admissionRoot, { recursive: true, force: true }))
  const controller = new AbortController()
  const events = []
  const ctx = {
    tool: { transform: async () => {} },
    permission: { hook: async () => {} },
    session: {
      hook: async () => {},
      get: async ({ sessionID }) =>
        sessionID === 'ses_parent'
          ? { model: { providerID: 'synthetic', id: 'fixture' } }
          : {
              outcome: 'succeeded',
              parentID: 'ses_parent',
              agent: 'wiki-ingest',
              location: { directory: '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki' },
            },
      move: async () => {},
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
    (
      await (
        await createBatchWorker(ctx, { admissionRoot })
      )(
        { sessionID: 'ses_parent', signal: controller.signal },
        {
          batch_id: 'synthetic',
          wiki_root: '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki',
          sources: [
            {
              source_path: '/knowledge/sources/webdav/a.md',
              wiki_path:
                '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki/sources/webdav/a.md',
            },
          ],
        },
      )
    ).content,
    'Report',
  )
  assert.deepEqual(events[0][1].model, { providerID: 'synthetic', id: 'fixture' })
  assert.equal(events[0][1].agent, 'wiki-ingest')
  assert.deepEqual(
    events.slice(-2).map(([name]) => name),
    ['stop', 'wait'],
  )
})

const nativeTool = (name, input, status = 'completed', error) => ({
  type: 'assistant',
  content: [{ type: 'tool', name, state: { input, status, error } }],
})

test('bounded native error summaries never hide a later explicit denial', () => {
  const source = '/knowledge/wiki/sources/webdav/a.md'
  const failures = Array.from({ length: 25 }, () =>
    nativeTool('read', { path: '/knowledge/wiki/missing.md' }, 'error', {
      type: 'tool.execution',
      message: 'Synthetic failure',
    }),
  )
  const denial = nativeTool('write', { path: source }, 'error', { type: 'permission.rejected' })
  const result = workerEvidence([...failures, denial], source, 'succeeded')
  assert.ok(result.errors.some((item) => item.code === 'worker_permission_denied'))
  assert.ok(result.errors.length <= 21)
})

test('completion requires covered source reads and skill; explicit rejection is never forgiven', () => {
  const source = '/knowledge/wiki/sources/webdav/a.md'
  const original = '/knowledge/sources/webdav/a.md'
  const options = { sourcePath: original, sourceLines: 2 }
  const read = nativeTool('read', { path: original })
  read.content[0].state.content = [{ type: 'text', text: '1: First\n2: Second\n' }]
  const skill = nativeTool('skill', { id: 'wiki-ingest' })
  const write = nativeTool('write', { path: source })
  assert.deepEqual(workerEvidence([skill, read, write], source, 'succeeded', options).errors, [])
  assert.ok(
    workerEvidence([skill, write], source, 'succeeded', options).errors.some(
      (item) => item.code === 'worker_source_not_read',
    ),
  )
  assert.ok(
    workerEvidence([read, write], source, 'succeeded', options).errors.some(
      (item) => item.code === 'worker_skill_not_loaded',
    ),
  )
  const failed = nativeTool('read', { path: original }, 'error', { type: 'tool.execution' })
  assert.deepEqual(
    workerEvidence([failed, skill, read, write], source, 'succeeded', options).errors,
    [],
  )
  const rejected = nativeTool('read', { path: original }, 'error', { type: 'permission.rejected' })
  assert.equal(
    workerEvidence([rejected, skill, read, write], source, 'succeeded', options).errors[0].code,
    'worker_permission_denied',
  )
})

test('stale-context repair needs a fresh exact read; catalog correction is code-owned not a rejected permission', () => {
  const source = '/knowledge/wiki/sources/webdav/a.md'
  const input = {
    patchText: `*** Begin Patch\n*** Update File: ${source}\n@@\n-old\n+new\n*** End Patch`,
  }
  const stale = nativeTool('patch', input, 'error', {
    type: 'tool.execution',
    message: 'patch verification failed: Failed to find expected lines',
  })
  const write = nativeTool('write', { path: source })
  assert.ok(workerEvidence([stale, write], source, 'succeeded').errors.length)
  assert.deepEqual(
    workerEvidence([stale, nativeTool('read', { path: source }), write], source, 'succeeded')
      .errors,
    [],
  )
  const mixed = nativeTool(
    'patch',
    {
      patchText: input.patchText.replace(
        '*** End Patch',
        '*** Update File: index.md\n@@\n-old\n+new\n*** End Patch',
      ),
    },
    'error',
    { type: 'unknown', message: 'ingest_catalog_owned' },
  )
  mixed.content[0].executed = false
  assert.deepEqual(workerEvidence([mixed, write], source, 'succeeded').errors, [])
  mixed.content[0].state.error.type = 'permission.rejected'
  assert.equal(
    workerEvidence([mixed, write], source, 'succeeded').errors[0].code,
    'worker_permission_denied',
  )
})

test('provider/configuration failures pause later dispatch; quotas pause without losing evidence', async (t) => {
  for (const kind of ['provider', 'configuration', 'capacity'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 3)
      let calls = 0
      const result = await runIngestBatch(
        { ...f.opts, ...(kind === 'capacity' ? { maxRetainedBytes: 1 } : {}) },
        async () => {
          calls++
          return {
            source_written: false,
            provider_failure: kind === 'provider',
            configuration_failure: kind === 'configuration',
          }
        },
      )
      assert.equal(result.status, 'paused')
      assert.equal(calls, kind === 'capacity' ? 0 : 1)
      assert.equal(result.awaiting_technical, 3)
      assert.equal(await f.git('status', '--porcelain'), '')
    })
})

test('historical dirty source needs a bound approval and fresh processing, not revision stamping', async (t) => {
  const f = await fixture(t, 2)
  const page = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
  await writeFile(page, '# Historical draft\n')
  const paused = await runIngestBatch(f.opts, () => assert.fail('historical draft is not owned'))
  assert.equal(paused.status, 'paused')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
  const result = await answer(f, paused, paused.questions[0], 'reprocess', async (batch) => {
    if (batch.sources[0].source_key === 'note-0.md')
      assert.match(await readFile(batch.sources[0].wiki_path, 'utf8'), /Historical draft/)
    return f.worker(batch)
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(result.ingested, 2)
  assert.match(await readFile(page, 'utf8'), /source_revision/)
})

test('dirty Paperless adoption displays and binds exact source bytes across approval', async (t) => {
  const f = await fixture(t, 0)
  const directory = path.join(f.opts.sourceRoot, 'paperless')
  await mkdir(directory)
  await mkdir(path.join(f.opts.wikiRoot, 'sources/paperless'))
  const fields = {
    paperless_id: '1',
    paperless_url: 'https://paperless.invalid/documents/1/',
    source_revision: 'b'.repeat(64),
  }
  const original = `---\n${Object.entries(fields)
    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
    .join('\n')}\n---\nOriginal fact.\n`
  const item = {
    source_key: '1',
    source_path: '1.md',
    wiki_path: 'paperless/1.md',
    source_revision: fields.source_revision,
    source_sha256: sha(original),
    frontmatter: fields,
    claim: { paperless_id: '1' },
  }
  const manifest = { ...f.manifest, source: 'paperless', wiki_root: 'paperless', items: [item] }
  const publish = () => writeFile(path.join(directory, 'manifest.json'), JSON.stringify(manifest))
  await writeFile(path.join(directory, '1.md'), original)
  await publish()
  const page = path.join(f.opts.wikiRoot, 'sources/paperless/1.md')
  await writeFile(page, '# Historical draft\n')
  const paused = await runIngestBatch(f.opts, () => assert.fail())
  const fresh = await answer(
    f,
    paused,
    paused.questions[0],
    'reprocess',
    () => assert.fail('changed bytes'),
    {
      authorize: async () => {
        const changed = original.replace('Original', 'Changed')
        await writeFile(path.join(directory, '1.md'), changed)
        item.source_sha256 = sha(changed)
        await publish()
      },
    },
  )
  assert.equal(fresh.status, 'paused')
  assert.notEqual(fresh.questions[0].id, paused.questions[0].id)
  assert.equal(fresh.questions[0].target.wiki_path, page)
  assert.equal(fresh.questions[0].target.source_revision, fields.source_revision)
  const done = await answer(f, fresh, fresh.questions[0], 'reprocess')
  assert.equal(done.status, 'done', done.blocker)
  assert.equal(done.ingested, 1)
})

test('recovered adoption is consumed durably and later sources continue', async (t) => {
  for (const phase of ['file_installed', 'source_recorded'])
    await t.test(phase, async (t) => {
      const f = await fixture(t, 2)
      await writeFile(
        path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md'),
        '# Historical draft\n',
      )
      const paused = await runIngestBatch(f.opts, () => assert.fail())
      const config = path.join(f.opts.root, '../opts.json')
      await writeFile(
        config,
        JSON.stringify({
          ...f.opts,
          batchId: paused.batch_id,
          decisionId: paused.questions[0].id,
          answer: 'reprocess',
        }),
      )
      await assert.rejects(
        exec(process.execPath, ['tests/fixtures/ingest-crash.mjs', config, phase, '1']),
        (error) => error.signal === 'SIGKILL',
      )
      const inspected = await runIngestBatch(f.opts, () => assert.fail('inspect only'))
      const done = await answer(f, inspected, inspected.questions[0], 'retry', async (batch) => {
        assert.equal(batch.sources[0].source_key, 'note-1.md')
        return f.worker(batch)
      })
      assert.equal(done.status, 'done', done.blocker)
      assert.equal(done.ingested, 2)
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
    })
})

test('fully installed recovery tolerates a manually removed draft', async (t) => {
  const f = await fixture(t, 1)
  const paused = await runIngestBatch(
    {
      ...f.opts,
      onPhase: (phase) => {
        if (phase === 'commit_installed') throw new Error('Synthetic interruption')
      },
    },
    f.worker,
  )
  await rm(path.join(f.opts.root, 'drafts', paused.batch_id), { recursive: true })
  const inspected = await runIngestBatch(f.opts, () => assert.fail())
  const done = await answer(f, inspected, inspected.questions[0], 'retry', () => assert.fail())
  assert.equal(done.status, 'done', done.blocker)
  assert.equal(done.questions.length, 0)
  assert.equal(done.ingested, 1)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
})

test('native long-line truncation pauses as an extraction limit, never certifies a full read', async (t) => {
  for (const mixed of [false, true])
    await t.test(mixed ? 'earlier tool failure' : 'read only', async (t) => {
      const f = await fixture(t, 1)
      let calls = 0
      const result = await runIngestBatch(f.opts, async (batch) => {
        calls++
        await f.worker(batch)
        const source = batch.sources[0]
        const read = nativeTool('read', { path: source.source_path })
        // OpenCode 2.1.2 native output for a minified single-line HTML source.
        read.content[0].state.content = [
          {
            type: 'text',
            text: `1: <html>${'x'.repeat(1994)} (line truncated to 2000 chars)\n\n(End of file - total 1 lines)`,
          },
        ]
        const evidence = workerEvidence(
          [
            ...(mixed
              ? [
                  nativeTool('read', { path: source.source_path }, 'error', {
                    type: 'tool.execution',
                    message: 'Synthetic failed read',
                  }),
                ]
              : []),
            nativeTool('skill', { id: 'wiki-ingest' }),
            read,
            nativeTool('write', { path: source.wiki_path }),
          ],
          source.wiki_path,
          'succeeded',
          { sourcePath: source.source_path, sourceLines: 1 },
        )
        assert.equal(evidence.source_read, false)
        return evidence
      })
      assert.equal(calls, 1)
      assert.equal(result.status, 'paused')
      assert.equal(result.questions[0].reason, 'worker_source_truncated')
      assert.equal(result.ingested, 0)
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
    })
})

test('revocation during file installation blocks the commit and retains the recovery checkpoint', async (t) => {
  const f = await fixture(t, 1)
  let revoked = false
  const result = await runIngestBatch(
    {
      ...f.opts,
      onPhase: async (phase) => {
        if (phase !== 'file_installed' || revoked) return
        revoked = true
        const item = f.manifest.items.pop()
        f.manifest.revoked.push({ source_key: item.source_key, claim: item.claim })
        await f.publish()
      },
    },
    f.worker,
  )
  assert.equal(result.status, 'paused')
  assert.equal(result.ingested, 0)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
  assert.ok(result.questions.some((item) => item.source_index === null))
})

test('a missing target read followed by creation commits and continues to the next source', async (t) => {
  for (const mutation of ['write', 'patch']) {
    await t.test(mutation, async (t) => {
      const f = await fixture(t, 2)
      let calls = 0
      const result = await runIngestBatch(f.opts, async (batch) => {
        calls++
        const source = batch.sources[0].wiki_path
        await assert.rejects(readFile(source), { code: 'ENOENT' })
        const report = await f.worker(batch)
        return workerEvidence(
          [
            nativeTool('read', { path: source }, 'error', {
              type: 'tool.execution',
              message: `File not found: ${source}`,
            }),
            nativeTool(
              mutation,
              mutation === 'write'
                ? { path: source }
                : {
                    patchText: `*** Begin Patch\n*** Add File: ${source}\n+# Finding\n*** End Patch`,
                  },
            ),
            { type: 'assistant', content: [{ type: 'text', text: report.content }] },
          ],
          source,
          'succeeded',
          { sourceAbsent: true },
        )
      })
      assert.equal(result.status, 'done', result.blocker)
      assert.equal(result.ingested, 2)
      assert.equal(calls, 2)
      assert.equal(result.remaining, 0)
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
    })
  }
})

test('only an absent exact target read recovered by later creation is nonfatal', () => {
  const source = '/knowledge/wiki/sources/webdav/a.md'
  const missing = (
    target = source,
    type = 'tool.execution',
    message = `File not found: ${target}`,
  ) => nativeTool('read', { path: target }, 'error', { type, message })
  const create = nativeTool('patch', {
    patchText: `*** Begin Patch\n*** Add File: ${source}\n+# Finding\n*** End Patch`,
  })
  const recovered = [missing(), create]
  assert.deepEqual(
    workerEvidence(recovered, source, 'succeeded', { sourceAbsent: true }).errors,
    [],
  )
  for (const messages of [
    [missing()],
    [create, missing()],
    [missing('/knowledge/sources/webdav/a.md'), create],
    [missing('/knowledge/wiki/overview.md'), create],
    [missing(source, 'permission.rejected'), create],
    [missing(source, 'tool.execution', 'Permission denied'), create],
    [missing(), nativeTool('write', { path: source }, 'error', { type: 'tool.execution' })],
    [missing(), nativeTool('edit', { path: source })],
    [missing(), nativeTool('patch', { patchText: `*** Update File: ${source}` })],
    [missing(), nativeTool('patch', { patchText: `*** Delete File: ${source}` })],
    [missing(), nativeTool('write', { path: '/knowledge/wiki/overview.md' })],
  ]) {
    assert.ok(workerEvidence(messages, source, 'succeeded', { sourceAbsent: true }).errors.length)
  }
  assert.equal(workerEvidence(recovered, source, 'succeeded').errors.length, 1)
  assert.equal(
    workerEvidence(recovered, source, 'succeeded', { sourceAbsent: false }).errors.length,
    1,
  )
  assert.deepEqual(workerEvidence(recovered, source, 'failed', { sourceAbsent: true }).errors, [
    { code: 'worker_session_failed', tool: 'session' },
  ])
  const relative = 'sources/webdav/a.md'
  assert.deepEqual(
    workerEvidence(
      [
        missing(relative, 'tool.execution', `File not found: ${source}`),
        nativeTool('write', { path: relative }),
      ],
      source,
      'succeeded',
      { sourceAbsent: true },
    ).errors,
    [],
  )
})

test('native worker evidence distinguishes exact source writes, errors and unrelated edits', () => {
  const source = '/knowledge/wiki/sources/webdav/a.md'
  const tool = (name, input, status = 'completed', error) => ({
    type: 'assistant',
    content: [{ type: 'tool', name, state: { input, status, error } }],
  })
  for (const [name, input] of [
    ['write', { path: source }],
    ['edit', { path: 'sources/webdav/a.md' }],
    [
      'patch',
      {
        patchText:
          '*** Begin Patch\n*** Update File: sources/webdav/a.md\n@@\n-old\n+new\n*** End Patch',
      },
    ],
  ])
    assert.equal(workerEvidence([tool(name, input)], source, 'succeeded').source_written, true)
  assert.equal(
    workerEvidence([tool('write', { path: 'overview.md' })], source, 'succeeded').source_written,
    false,
  )
  const evidence = workerEvidence(
    [
      tool('patch', { patchText: `*** Add File: ${source}` }, 'error', {
        type: 'permission.rejected',
        message: 'PRIVATE',
      }),
      { type: 'assistant', content: [{ type: 'text', text: 'Done!' }] },
    ],
    source,
    'succeeded',
  )
  assert.equal(evidence.source_written, false)
  assert.deepEqual(evidence.errors, [{ code: 'worker_permission_denied', tool: 'patch' }])
  assert.doesNotMatch(JSON.stringify(evidence), /PRIVATE/)
  assert.equal(workerEvidence([], source, 'failed').errors[0].code, 'worker_session_failed')
  assert.equal(
    workerEvidence([tool('read', { path: source })], source, 'succeeded').source_written,
    false,
  )
  const readErrors = Array.from({ length: 25 }, () =>
    tool('read', { path: source }, 'error', { type: 'permission.rejected' }),
  )
  const bounded = workerEvidence(readErrors, source, 'succeeded')
  assert.equal(bounded.errors.length, 20)
  assert.deepEqual(bounded.errors[0], { code: 'worker_permission_denied', tool: 'read' })
})

test('failed reads and skills after a source write prevent publication despite session success', async (t) => {
  for (const name of ['read', 'skill']) {
    await t.test(name, async (t) => {
      const f = await fixture(t, 1)
      const head = await f.git('rev-parse', 'HEAD')
      const result = await runIngestBatch(f.opts, async (batch) => {
        const report = await f.worker(batch)
        return workerEvidence(
          [
            {
              type: 'assistant',
              content: [
                {
                  type: 'tool',
                  name: 'write',
                  state: { status: 'completed', input: { path: batch.sources[0].wiki_path } },
                },
                {
                  type: 'tool',
                  name,
                  state: {
                    status: 'error',
                    error: { type: 'tool.execution', message: 'PRIVATE_NATIVE_ERROR' },
                  },
                },
                { type: 'text', text: report.content },
              ],
            },
          ],
          batch.sources[0].wiki_path,
          'succeeded',
        )
      })
      assert.equal(result.ingested, 0)
      assert.equal(result.status, 'paused')
      assert.equal(await f.git('rev-parse', 'HEAD'), head)
      const report = await readFile(result.report_path, 'utf8')
      assert.match(report, /worker_tool_failed/)
      assert.match(report, new RegExp(`"tool":"${name}"`))
      assert.match(report, /Quellschreibvorgang: belegt/)
      assert.doesNotMatch(report, /PRIVATE_NATIVE_ERROR/)
    })
  }
})

test('admission is session-bound, restricts source targets, and expires after worker exit', async (t) => {
  const admissionRoot = await mkdtemp(path.join(os.tmpdir(), 'ingest-admission-'))
  t.after(() => rm(admissionRoot, { recursive: true, force: true }))
  let permission
  let contextHook
  const ctx = {
    tool: { transform: async () => {} },
    permission: {
      hook: async (_name, callback) => {
        permission = callback
      },
    },
    session: {
      hook: async (name, callback) => {
        if (name === 'context') contextHook = callback
      },
      get: async () => ({
        agent: 'wiki-ingest',
        parentID: 'ses_parent',
        model: { id: 'synthetic' },
        outcome: 'succeeded',
        location: { directory: '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki' },
      }),
      move: async () => {},
      create: async () => ({ id: 'ses_child' }),
      prompt: async () => {
        const event = {
          sessionID: 'ses_child',
          action: 'edit',
          agent: 'wiki-ingest',
          effect: 'allow',
          resources: ['sources/webdav/a.md', 'overview.md'],
        }
        await permission(event)
        assert.equal(event.effect, 'allow')
        for (const resource of ['sources/webdav/other.md', 'index.md', 'log.md', '../escape.md']) {
          const invalid = { ...event, resources: [resource] }
          await permission(invalid)
          assert.equal(invalid.effect, 'deny')
        }
        const outgoing = { sessionID: 'ses_child', system: [] }
        await contextHook(outgoing)
        assert.match(outgoing.system[0].text, /Code-zugelassener/)
      },
      wait: async () => {},
      interrupt: async () => {},
      context: async () => [],
    },
  }
  const worker = await createBatchWorker(ctx, { admissionRoot })
  const standalone = {
    sessionID: 'ses_standalone',
    action: 'edit',
    effect: 'allow',
    resources: ['sources/webdav/a.md'],
  }
  await permission(standalone)
  assert.equal(standalone.effect, 'deny')
  const input = {
    batch_id: 'synthetic',
    wiki_root: '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki',
    sources: [
      {
        source_path: '/knowledge/sources/webdav/a.md',
        wiki_path: '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki/sources/webdav/a.md',
      },
    ],
  }
  await worker({ sessionID: 'ses_parent', signal: new AbortController().signal }, input)
  const after = { ...standalone, sessionID: 'ses_child', effect: 'allow' }
  await permission(after)
  assert.equal(after.effect, 'deny')
  await assert.rejects(
    worker({ signal: new AbortController().signal }, { ...input, sources: [] }),
    /Ungültiger/,
  )
})
