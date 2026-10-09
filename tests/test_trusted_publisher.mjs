import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import fs from 'node:fs/promises'
import { syncBuiltinESMExports } from 'node:module'
import path from 'node:path'
import os from 'node:os'
import test from 'node:test'
import { publisherControl } from '../publisher/control.mjs'
import { loadRun, startRun } from '../config/tools/wiki_ingest_journal_core.mjs'
import { IDENTITY, REPORT, SOURCE, publisherFixture } from './helpers/publisher-fixture.mjs'

const BOOT = 'a'.repeat(32)

async function fixture(t) {
  const base = await mkdtemp(path.join(os.tmpdir(), 'trusted-publisher-'))
  t.after(() => rm(base, { recursive: true, force: true }))
  const f = await publisherFixture(base)
  let sequence = 0
  const call = (input, bootId = BOOT) => publisherControl(input, { ...f, bootId })
  const enqueue = (kind = 'automatic') =>
    call({ operation: 'enqueue', request_id: `req-admit-${sequence++}`, kind, source: IDENTITY })
  const propose = (job, proposal, request_id = `req-step-${sequence++}`) =>
    call({ operation: 'propose', job_id: job.job_id, request_id, proposal })
  const ready = async (kind) => {
    await enqueue(kind)
    const job = await call({ operation: 'activate' })
    await propose(job, { operation: 'read_source' })
    await propose(job, {
      operation: 'stage',
      draft: '# Finding\nEvidence: source line 1.\n$(touch /tmp/must-not-execute)\n',
    })
    await propose(job, { operation: 'stage', page: 'overview.md', reviewed: true })
    return job
  }
  const publish = (job, request_id = 'req-publish') =>
    call({ operation: 'publish', job_id: job.job_id, request_id, report: REPORT })
  return { ...f, call, enqueue, propose, ready, publish }
}

test('authoritative admission, private staging, focused commit/report and exact replay', async (t) => {
  const f = await fixture(t)
  const baseline = await f.git('rev-parse', 'HEAD')
  assert.deepEqual(await f.call({ operation: 'activate' }), { idle: true })
  const input = {
    operation: 'enqueue',
    request_id: 'req-admission',
    kind: 'manual',
    source: IDENTITY,
  }
  const queued = await f.call(input)
  assert.deepEqual(await f.call(input), queued)
  assert.deepEqual(
    await f.call({
      source: IDENTITY,
      kind: 'manual',
      request_id: 'req-admission',
      operation: 'enqueue',
    }),
    queued,
  )
  await assert.rejects(f.call({ ...input, kind: 'automatic' }), /request_id_conflict/)
  const job = await f.ready()
  assert.equal(job.job_id, queued.job_id)
  assert.equal(await f.git('rev-parse', 'HEAD'), baseline)
  assert.equal(await f.git('status', '--porcelain'), '')
  const result = await f.publish(job)
  assert.equal(result.status, 'ingested')
  assert.deepEqual(await f.publish(job), result)
  await assert.rejects(
    f.call({
      operation: 'publish',
      job_id: job.job_id,
      request_id: 'req-publish',
      report: { ...REPORT, title: 'different' },
    }),
    /request_id_conflict/,
  )
  await assert.rejects(f.propose(job, { operation: 'state' }), /stale_owner/)
  assert.equal(await f.git('rev-list', '--count', `${baseline}..HEAD`), '1')
  assert.equal(await f.git('status', '--porcelain'), '')
  const run = await loadRun({ root: f.root, runId: result.run_id })
  assert.equal(run.state, 'completed')
  assert.equal(run.records.length, 1)
  assert.match(await readFile(path.join(f.root, run.report.path), 'utf8'), /Finding at line 1/)
  assert.equal(await readFile(path.join(f.sourceRoot, 'webdav/notes.md'), 'utf8'), SOURCE)
})

test('source and inspection replays stay private; control state and status remain content-free', async (t) => {
  const f = await fixture(t)
  await f.enqueue()
  const job = await f.call({ operation: 'activate' })
  const read = await f.propose(job, { operation: 'read_source' }, 'req-private-read')
  assert.equal(read.text, SOURCE)
  assert.deepEqual(await f.propose(job, { operation: 'read_source' }, 'req-private-read'), read)
  await f.propose(job, {
    operation: 'stage',
    page: 'overview.md',
    append: '\nUnique synthetic inspection text.\n',
  })
  const inspected = await f.propose(
    job,
    { operation: 'inspect', page: 'overview.md' },
    'req-private-inspect',
  )
  assert.match(inspected.text, /Unique synthetic inspection text/)
  assert.deepEqual(
    await f.propose(job, { operation: 'inspect', page: 'overview.md' }, 'req-private-inspect'),
    inspected,
  )
  const control = await readFile(path.join(f.stateRoot, 'control.json'), 'utf8')
  const status = JSON.stringify(await f.call({ operation: 'status' }))
  for (const text of [SOURCE.trim(), 'Unique synthetic inspection text']) {
    assert.ok(!control.includes(text))
    assert.ok(!status.includes(text))
  }
  assert.ok(!Object.hasOwn(await f.call({ operation: 'status' }), 'responses'))
  const replay = JSON.parse(
    await readFile(path.join(f.root, 'publisher-replays/req-private-read.json'), 'utf8'),
  )
  assert.equal(replay.result.text, SOURCE)
  await writeFile(
    path.join(f.root, 'publisher-replays/req-private-read.json'),
    '{"result":{"text":"damaged"}}',
  )
  await assert.rejects(
    f.propose(job, { operation: 'read_source' }, 'req-private-read'),
    /invalid_replay_evidence/,
  )
  assert.equal((await f.call({ operation: 'status' })).stop, 'operator_action_required')
})

test('manual priority at a complete source boundary; no takeover or lease expiry', async (t) => {
  const f = await fixture(t)
  const active = await f.ready()
  const auto = await f.enqueue('automatic')
  const manual = await f.enqueue('manual')
  assert.equal((await f.call({ operation: 'activate' }, 'b'.repeat(32))).job_id, active.job_id)
  await assert.rejects(
    f.propose(manual, { operation: 'stage', draft: 'wrong owner' }),
    /stale_owner/,
  )
  await f.publish(active)
  const next = await f.call({ operation: 'activate' })
  assert.equal(next.job_id, manual.job_id)
  assert.equal((await f.call({ operation: 'status' })).queue[0].job_id, auto.job_id)
  await assert.rejects(
    f.call({ operation: 'cancel', job_id: next.job_id, confirmed: false }),
    /confirmation_required/,
  )
  assert.deepEqual(await f.call({ operation: 'cancel', job_id: next.job_id, confirmed: true }), {
    cancelled: true,
  })
  assert.equal((await f.call({ operation: 'activate' })).job_id, auto.job_id)
})

test('admission remains available while an operation holds the writer lock', async (t) => {
  const f = await fixture(t)
  await f.call({ operation: 'status' })
  const lockModule = new URL('../config/tools/wiki_ingest_storage.mjs', import.meta.url).href
  const child = spawn(
    'node',
    [
      '--input-type=module',
      '-e',
      `
    import { withIngestLock } from ${JSON.stringify(lockModule)};
    await withIngestLock(process.argv[1], async () => {
      console.log('locked');
      await new Promise(resolve => { process.stdin.once('end', resolve); process.stdin.resume(); });
    });
  `,
      path.join(f.stateRoot, 'writer.lock'),
    ],
    { stdio: ['pipe', 'pipe', 'pipe'] },
  )
  const exited = new Promise((resolve) => child.once('close', resolve))
  await new Promise((resolve, reject) => {
    child.once('error', reject)
    child.stdout.once('data', () => resolve())
    child.once('exit', () => reject(new Error('lock child exited early')))
  })
  try {
    const manual = await f.enqueue('manual')
    assert.equal(manual.kind, 'manual')
    await assert.rejects(f.call({ operation: 'activate' }), /Lock|lock/)
  } finally {
    child.stdin.end()
    assert.equal(await exited, 0)
  }
  assert.equal((await f.call({ operation: 'activate' })).kind, 'manual')
})

test('proposals cannot select roots, identity, owner, run, shell, publish or rollback', async (t) => {
  const f = await fixture(t)
  await f.enqueue()
  const job = await f.call({ operation: 'activate' })
  for (const proposal of [
    { operation: 'publish', title: 'bypass' },
    { operation: 'rollback', confirmed: true },
    { operation: 'prepare', adapter: 'webdav' },
    { operation: 'stage', root: '/tmp' },
    { operation: 'stage', wikiRoot: '/tmp' },
    { operation: 'stage', preparationId: job.preparation_id },
    { operation: 'stage', source_revision: IDENTITY.source_revision },
    { operation: 'stage', runId: job.run_id },
    { operation: 'stage', owner: job.job_id },
    { operation: 'shell', command: 'touch /tmp/must-not-execute' },
  ])
    await assert.rejects(f.propose(job, proposal), /invalid_input|invalid_proposal/)
  const valid = {
    operation: 'propose',
    request_id: 'req-valid',
    job_id: job.job_id,
    proposal: { operation: 'state' },
  }
  await assert.rejects(f.call({ ...valid, owner: 'trusted' }), /invalid_input/)
  const result = await f.call(valid)
  assert.equal(result.phase, 'draft')
  assert.deepEqual(await f.call(valid), result)
  await assert.rejects(
    f.propose(job, { operation: 'inspect', page: '../outside.md' }),
    /relativ|Pfad|declariert|deklariert/,
  )
  assert.equal((await f.call({ operation: 'status' })).active.job_id, job.job_id)
})

for (const fault of ['stale-source', 'dirty-wiki', 'invalid-state']) {
  test(`persistent safety stop refuses takeover (${fault})`, async (t) => {
    const f = await fixture(t)
    const job = await f.ready()
    if (fault === 'stale-source')
      await writeFile(path.join(f.sourceRoot, 'webdav/notes.md'), 'Changed source.\n')
    if (fault === 'dirty-wiki')
      await writeFile(path.join(f.wikiRoot, 'foreign.md'), 'Must be preserved.\n')
    if (fault === 'invalid-state') {
      await writeFile(path.join(f.stateRoot, 'control.json'), '{"version":999}')
      await assert.rejects(f.publish(job), /invalid_state/)
      await assert.rejects(f.call({ operation: 'activate' }), /invalid_state/)
      return
    }
    await assert.rejects(f.publish(job))
    const state = await f.call({ operation: 'status' }, 'b'.repeat(32))
    assert.equal(state.stop, 'operator_action_required')
    assert.equal(state.active.job_id, job.job_id)
    await f.enqueue('manual')
    await assert.rejects(
      f.call({ operation: 'activate' }, 'b'.repeat(32)),
      /operator_action_required/,
    )
    await assert.rejects(
      f.call({ operation: 'recover', confirmed: true }),
      /publisher_restart_required/,
    )
    await assert.rejects(
      f.call({ operation: 'recover', confirmed: true }, 'b'.repeat(32)),
      /manual_maintenance_required/,
    )
    if (fault === 'dirty-wiki')
      assert.equal(
        await readFile(path.join(f.wikiRoot, 'foreign.md'), 'utf8'),
        'Must be preserved.\n',
      )
  })
}

test('lost publish acknowledgement requires restart and confirmed verified recovery, never a second commit', async (t) => {
  const f = await fixture(t)
  const job = await f.ready()
  const baseline = await f.git('rev-parse', 'HEAD')
  const originalRename = fs.rename
  let injected = false
  fs.rename = async (...args) => {
    if (String(args[1]) === path.join(f.stateRoot, 'control.json')) {
      const state = JSON.parse(await readFile(args[0], 'utf8'))
      if (!injected && state.responses['req-publish']) {
        injected = true
        throw new Error('Synthetic lost acknowledgement')
      }
    }
    return originalRename(...args)
  }
  syncBuiltinESMExports()
  try {
    await assert.rejects(f.publish(job), /Synthetic lost acknowledgement/)
  } finally {
    fs.rename = originalRename
    syncBuiltinESMExports()
  }
  assert.equal(injected, true)
  await assert.rejects(f.call({ operation: 'activate' }), /operator_action_required/)
  await assert.rejects(
    f.call({ operation: 'recover', confirmed: false }, 'b'.repeat(32)),
    /confirmation_required/,
  )
  await assert.rejects(
    f.call({ operation: 'recover', confirmed: true }),
    /publisher_restart_required/,
  )
  const result = await f.call({ operation: 'recover', confirmed: true }, 'b'.repeat(32))
  assert.deepEqual(await f.publish(job), result)
  assert.equal(await f.git('rev-list', '--count', `${baseline}..HEAD`), '1')
  assert.equal((await loadRun({ root: f.root, runId: job.run_id })).records.length, 1)
})

test('crash intent during preparation cannot be blindly dispatched again', async (t) => {
  const f = await fixture(t)
  const queued = await f.enqueue()
  await writeFile(
    path.join(f.stateRoot, 'control.json'),
    JSON.stringify({
      version: 1,
      active: { ...queued, preparation_id: null },
      inflight: { operation: 'prepare', boot_id: BOOT },
      stop: null,
      finished: [],
      responses: {},
    }),
  )
  await assert.rejects(
    f.call({ operation: 'activate' }, 'b'.repeat(32)),
    /operator_action_required/,
  )
  await assert.rejects(
    f.call({ operation: 'recover', confirmed: true }, 'b'.repeat(32)),
    /manual_maintenance_required/,
  )
})

test('missing durable ownership cannot silently initialize a new writer', async (t) => {
  const f = await fixture(t)
  await f.ready()
  await rm(path.join(f.stateRoot, 'control.json'))
  await assert.rejects(f.call({ operation: 'activate' }), /missing_state/)
  await assert.rejects(f.enqueue('manual'), /missing_state/)
})

test('an unresolved prior journal run blocks the new authority domain', async (t) => {
  const f = await fixture(t)
  await startRun({ root: f.root })
  await f.enqueue()
  await assert.rejects(f.call({ operation: 'activate' }), /unresolved_journal/)
  assert.equal((await f.call({ operation: 'status' })).stop, 'operator_action_required')
  assert.equal(await f.git('status', '--porcelain'), '')
})

for (const fault of ['report', 'journal', 'worktree', 'owner']) {
  test(`completed-core recovery verifies evidence before releasing ownership (${fault})`, async (t) => {
    const f = await fixture(t)
    const job = await f.ready()
    const result = await f.publish(job)
    // Reproduce the persisted wrapper state of a lost final acknowledgement.
    const file = path.join(f.stateRoot, 'control.json')
    const state = JSON.parse(await readFile(file, 'utf8'))
    const response = state.responses['req-publish']
    state.active = { ...job }
    state.finished = []
    state.inflight = {
      operation: 'publish',
      request_id: 'req-publish',
      digest: response.digest,
      boot_id: BOOT,
    }
    state.stop = 'operator_action_required'
    state.responses = {}
    if (fault === 'owner') state.active.run_id = 'run-20261009t000000z-ffffff'
    await writeFile(file, JSON.stringify(state))
    const run = await loadRun({ root: f.root, runId: job.run_id })
    if (fault === 'report') await writeFile(path.join(f.root, run.report.path), 'Damaged report.\n')
    if (fault === 'journal')
      await writeFile(path.join(f.root, 'runs', job.run_id, 'records.jsonl'), '')
    if (fault === 'worktree')
      await writeFile(path.join(f.wikiRoot, 'foreign.md'), 'Preserve this foreign change.\n')
    await assert.rejects(f.call({ operation: 'recover', confirmed: true }, 'b'.repeat(32)))
    await assert.rejects(f.call({ operation: 'activate' }), /operator_action_required/)
    assert.equal(await f.git('rev-parse', 'HEAD'), result.commit)
  })
}
