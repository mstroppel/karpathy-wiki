import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { manualIngest } from '../publisher/manual_ingest.mjs'
import { publisherFixture, REPORT } from './helpers/publisher-fixture.mjs'
import { manualRuntime } from '../publisher/manual_runtime.mjs'
import { manualServer } from '../publisher/manual_server.mjs'
import { loadRun } from '../config/tools/wiki_ingest_journal_core.mjs'

test('manual trusted driver publishes one source from a fresh deny-all child', async () => {
  const base = await mkdtemp(path.join(os.tmpdir(), 'manual-ingest-'))
  try {
    const fixture = await publisherFixture(base)
    const notes = []
    const runtime = {
      session: async () => ({
        location: { directory: '/knowledge/wiki' },
        model: { providerID: 'fake', id: 'fake' },
      }),
      create: async (parentID) => ({ id: 'ses_worker', parentID }),
      generate: async (_id, prompt) => {
        if (!prompt.includes('Vorschlag:'))
          return JSON.stringify({ proposal: { operation: 'read_source', offset: 1, limit: 80 } })
        if (!prompt.includes('"operation":"inspect"'))
          return JSON.stringify({
            proposal: { operation: 'inspect', page: 'overview.md', offset: 1, limit: 80 },
          })
        if (!prompt.includes('"operation":"stage"'))
          return JSON.stringify({
            proposal: {
              operation: 'stage',
              draft: '# Synthetic finding\n\nFinding at source line 1.\n',
            },
          })
        if (!prompt.includes('"reviewed":true'))
          return JSON.stringify({
            proposal: { operation: 'stage', page: 'overview.md', reviewed: true },
          })
        return JSON.stringify({ report: REPORT })
      },
      note: async (...args) => notes.push(args),
    }
    const driver = manualIngest({ configuration: { ...fixture, bootId: '1'.repeat(32) }, runtime })
    await driver.initialize()
    await driver.submit({ session_id: 'ses_main', request_id: 'req-first' })
    await driver.wait()
    const state = await driver.status()
    assert.equal(state.status, 'completed', JSON.stringify(state))
    assert.equal(state.results.length, 1)
    assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '2')
    assert.equal(notes.length, 1)
    assert.equal(notes[0][2], true)
    assert.match(
      await readFile(path.join(fixture.wikiRoot, 'sources/webdav/notes.md'), 'utf8'),
      /Synthetic finding/,
    )
    // Source-derived summaries stay in the private journal; controller state
    // keeps content-free references and the summary reuses the rebuilt record.
    const run = await loadRun({ root: fixture.root, runId: state.results[0].run_id })
    const persisted = await readFile(path.join(fixture.stateRoot, 'manual.json'), 'utf8')
    assert.ok(!persisted.includes('Finding at line 1.'))
    assert.ok(!persisted.includes(JSON.stringify(run.effective[0])))
    assert.ok(notes[0][1].includes(JSON.stringify(run.effective[0])))
  } finally {
    await rm(base, { recursive: true, force: true })
  }
})

async function fixtureTest(callback) {
  const base = await mkdtemp(path.join(os.tmpdir(), 'manual-ingest-'))
  try {
    const fixture = await publisherFixture(base)
    const configuration = { ...fixture, bootId: '1'.repeat(32) }
    const calls = []
    const runtime = {
      session: async () => ({
        location: { directory: '/knowledge/wiki' },
        model: { providerID: 'fake', id: 'fake' },
      }),
      create: async (parentID) => {
        calls.push('create')
        return { id: 'ses_worker', parentID }
      },
      generate: async () => {
        calls.push('generate')
        return 'invalid JSON'
      },
      note: async () => calls.push('note'),
    }
    const driver = manualIngest({ configuration, runtime })
    await driver.initialize()
    await callback({ fixture, configuration, runtime, calls, driver })
  } finally {
    await rm(base, { recursive: true, force: true })
  }
}

test('worker failure records a private blocked report; explicit resume verifies without rerunning', () =>
  fixtureTest(async ({ driver, fixture, calls }) => {
    await driver.submit({ session_id: 'ses_main', request_id: 'req-failure' })
    await driver.wait()
    const state = await driver.status()
    assert.equal(state.status, 'operator_action_required')
    assert.equal(state.report_failure, false)
    assert.equal(state.failure.status, 'blocked')
    assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '1')
    await assert.rejects(driver.submit({ session_id: 'ses_main', request_id: 'req-another' }))
    await assert.rejects(driver.resume({ confirmed: false }))
    assert.equal((await driver.resume({ confirmed: true })).verified, true)
    assert.deepEqual(calls, ['create', 'generate', 'note', 'note'])
  }))

test('restart keeps a persistent stop and never redispatches', () =>
  fixtureTest(async ({ driver, fixture, configuration, runtime, calls }) => {
    await writeFile(
      path.join(fixture.stateRoot, 'manual.json'),
      JSON.stringify({
        version: 1,
        status: 'running',
        request_id: 'req-crash',
        session_id: 'ses_main',
        active: null,
        results: [],
        selected: [],
        completed_sources: 0,
      }),
    )
    const restarted = manualIngest({
      configuration: { ...configuration, bootId: '2'.repeat(32) },
      runtime,
    })
    await restarted.initialize()
    assert.equal((await restarted.status()).status, 'operator_action_required')
    await assert.rejects(restarted.submit({ session_id: 'ses_main', request_id: 'req-new' }))
    assert.equal((await driver.status()).blocker, 'interrupted_controller')
    assert.deepEqual(calls, [])
  }))

test('dirty wiki blocks before model dispatch and must pass explicit verification', () =>
  fixtureTest(async ({ driver, fixture, calls }) => {
    const page = path.join(fixture.wikiRoot, 'overview.md')
    const baseline = await readFile(page, 'utf8')
    await writeFile(page, `${baseline}Unrelated operator change.\n`)
    await driver.submit({ session_id: 'ses_main', request_id: 'req-dirty' })
    await driver.wait()
    assert.equal((await driver.status()).status, 'operator_action_required')
    await assert.rejects(driver.resume({ confirmed: true }))
    assert.deepEqual(calls, ['note'])
    assert.match(await readFile(page, 'utf8'), /Unrelated operator change/)
    await writeFile(page, baseline)
    assert.equal((await driver.resume({ confirmed: true })).verified, true)
  }))

test('missing or corrupt controller evidence cannot reset safety stops', () =>
  fixtureTest(async ({ driver, fixture, configuration, runtime }) => {
    const file = path.join(fixture.stateRoot, 'manual.json')
    await writeFile(file, '{}')
    await assert.rejects(driver.submit({ session_id: 'ses_main', request_id: 'req-corrupt' }))
    await rm(file)
    await assert.rejects(driver.submit({ session_id: 'ses_main', request_id: 'req-missing' }))
    await assert.rejects(
      manualIngest({ configuration, runtime }).initialize(),
      /missing_controller_state/,
    )
  }))

test('caller-supplied authority and source identities are refused', () =>
  fixtureTest(async ({ driver, calls }) => {
    for (const extra of [
      { source: {} },
      { kind: 'automatic' },
      { confirmed: true },
      { root: '/tmp' },
      { proposal: { operation: 'publish' } },
    ])
      await assert.rejects(
        driver.submit({ session_id: 'ses_main', request_id: 'req-forged', ...extra }),
      )
    assert.deepEqual(calls, [])
  }))

test('runtime transport uses a tool-free generation endpoint and deny-all fresh children', async () => {
  const requests = []
  const runtime = manualRuntime({
    url: 'http://runtime.invalid',
    password: 'synthetic',
    fetcher: async (url, options) => {
      requests.push({ url, options, body: JSON.parse(options.body) })
      return Response.json({
        data: url.endsWith('/generate') ? { text: '{}' } : { id: 'ses_worker' },
      })
    },
  })
  await runtime.create('ses_main', { providerID: 'fake', id: 'fake' })
  assert.deepEqual(requests[0].body.permissions, [{ action: '*', resource: '*', effect: 'deny' }])
  assert.equal(requests[0].body.parentID, 'ses_main')
  assert.equal(await runtime.generate('ses_worker', 'synthetic source'), '{}')
  assert.ok(requests[1].url.endsWith('/generate'))
  assert.deepEqual(Object.keys(requests[1].body), ['prompt'])
  assert.equal(requests[1].options.redirect, 'error')
})

test('controller HTTP admits only its separate UI token, never arbitrary publication', async () => {
  const token = '1'.repeat(64)
  const inputs = []
  const server = manualServer({
    token,
    driver: {
      submit: async (input) => {
        inputs.push(input)
        return { accepted: true }
      },
      status: async () => ({ status: 'idle' }),
    },
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    const url = `http://127.0.0.1:${server.address().port}`
    assert.equal((await fetch(`${url}/ingest-new`, { method: 'POST' })).status, 401)
    assert.equal(
      (await fetch(`${url}/status`, { headers: { Authorization: 'Basic synthetic-backend' } }))
        .status,
      401,
    )
    assert.equal(
      (
        await fetch(`${url}/publish`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` },
        })
      ).status,
      404,
    )
    assert.deepEqual(inputs, [])
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('blocked-source notification supplies the authoritative file record and full report', () =>
  fixtureTest(async ({ driver, configuration, runtime }) => {
    const notes = []
    runtime.note = async (...args) => notes.push(args)
    await driver.submit({ session_id: 'ses_main', request_id: 'req-blocked-overview' })
    await driver.wait()
    const state = await driver.status()
    assert.equal(state.failure.status, 'blocked')
    assert.ok(state.failure.report_path.endsWith('/report.md'))
    const run = await loadRun({ root: configuration.root, runId: state.failure.run_id })
    const record = run.effective[0]
    assert.equal(record.status, 'blocked')
    assert.equal(record.content, 'Not determined; no verified publication.')
    assert.ok(notes[0][1].includes(JSON.stringify(record)))
    assert.ok(notes[0][1].includes(state.failure.report_path))
    assert.ok(!JSON.stringify(state).includes('Not determined'))
    assert.match(notes[0][1], /EVERY processed file, including blocked files/)
    assert.match(notes[0][1], /contradictions\/open questions and extraction limits/)
  }))

test('lost summary acknowledgement survives restart; explicit resume delivers verified files without reingest', () =>
  fixtureTest(async ({ driver, fixture, configuration, runtime, calls }) => {
    const proposals = [
      { proposal: { operation: 'read_source', offset: 1, limit: 80 } },
      { proposal: { operation: 'inspect', page: 'overview.md', offset: 1, limit: 80 } },
      { proposal: { operation: 'stage', draft: '# Synthetic finding\n\nFinding at line 1.\n' } },
      { proposal: { operation: 'stage', page: 'overview.md', reviewed: true } },
      { report: REPORT },
    ]
    runtime.generate = async () => {
      calls.push('generate')
      return JSON.stringify(proposals.shift())
    }
    const notes = []
    runtime.note = async (...args) => {
      notes.push(args)
      if (notes.length === 1) throw new Error('synthetic lost acknowledgement')
    }
    await driver.submit({ session_id: 'ses_main', request_id: 'req-summary-loss' })
    await driver.wait()
    const stopped = await driver.status()
    assert.equal(stopped.status, 'operator_action_required')
    assert.equal(stopped.status_delivery_failed, true)
    assert.equal(stopped.report_failure, false)
    assert.equal(notes.length, 1, 'No blind summary retry')
    assert.equal(stopped.results.length, 1)
    const journal = await loadRun({
      root: configuration.root,
      runId: stopped.results[0].run_id,
    })
    const verifiedRecord = JSON.stringify(journal.effective[0])
    const file = path.join(fixture.stateRoot, 'manual.json')
    const snapshot = structuredClone(stopped)
    snapshot.status = 'completed' // Crash while summary admission was in flight.
    snapshot.blocker = null
    // A snapshot that smuggles source-derived detail into controller state is
    // refused outright; nothing trusts, repairs or overwrites it.
    const smuggled = JSON.stringify({
      ...snapshot,
      results: [
        { ...snapshot.results[0], record: { content: 'Synthetic unverified snapshot detail.' } },
      ],
    })
    await writeFile(file, smuggled)
    const corrupt = manualIngest({ configuration, runtime })
    await assert.rejects(corrupt.initialize(), /invalid_controller_state/)
    await assert.rejects(corrupt.status(), /invalid_controller_state/)
    await assert.rejects(
      corrupt.submit({ session_id: 'ses_main', request_id: 'req-smuggled' }),
      /invalid_controller_state/,
    )
    assert.equal(await readFile(file, 'utf8'), smuggled, 'corrupt evidence must stay untouched')
    await writeFile(file, JSON.stringify(snapshot))
    const restarted = manualIngest({
      configuration: { ...configuration, bootId: '2'.repeat(32) },
      runtime,
    })
    await restarted.initialize()
    assert.equal((await restarted.status()).status, 'operator_action_required')
    assert.equal((await restarted.status()).blocker, 'interrupted_summary_admission')
    const reportFile = stopped.results[0].report_path
    const report = await readFile(reportFile, 'utf8')
    await writeFile(reportFile, 'Synthetic corrupt report.\n')
    await assert.rejects(restarted.resume({ confirmed: true }), /unverified_report/)
    assert.equal(notes.length, 1, 'No notification using unverified evidence')
    await writeFile(reportFile, report)
    assert.equal((await restarted.resume({ confirmed: true })).summary_admitted, true)
    assert.equal((await restarted.status()).status_delivery_failed, false)
    assert.equal(notes.length, 2)
    assert.ok(notes[1][1].includes(verifiedRecord))
    assert.ok(!notes[1][1].includes('Synthetic unverified snapshot detail.'))
    assert.deepEqual(calls, ['create', ...Array(5).fill('generate')])
    assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '2')
    // Crash after verified publication but before controller result persistence.
    const interrupted = {
      ...stopped,
      status: 'running',
      blocker: null,
      results: [],
      active: {
        source: stopped.results[0].source,
        intent: 'publish',
        job_id: stopped.selected[0].job_id,
        preparation_id: stopped.results[0].preparation_id,
        run_id: stopped.results[0].run_id,
      },
      status_delivery_failed: false,
    }
    await writeFile(path.join(fixture.stateRoot, 'manual.json'), JSON.stringify(interrupted))
    const postCommitRestart = manualIngest({
      configuration: { ...configuration, bootId: '3'.repeat(32) },
      runtime,
    })
    await postCommitRestart.initialize()
    assert.equal((await postCommitRestart.resume({ confirmed: true })).summary_admitted, true)
    assert.equal(notes.length, 3)
    assert.ok(notes[2][1].includes(verifiedRecord))
    assert.equal((await postCommitRestart.status()).results.length, 1)
    assert.deepEqual(calls, ['create', ...Array(5).fill('generate')])
    assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '2')
    // Selection admission evidence must recover files even when results/active
    // references are missing; losing that evidence too must stop, not omit files.
    const omitted = { ...stopped, results: [], active: null }
    await writeFile(path.join(fixture.stateRoot, 'manual.json'), JSON.stringify(omitted))
    const scopeRecovery = manualIngest({
      configuration: { ...configuration, bootId: '4'.repeat(32) },
      runtime,
    })
    await scopeRecovery.initialize()
    assert.equal((await scopeRecovery.resume({ confirmed: true })).summary_admitted, true)
    assert.equal(notes.length, 4)
    assert.ok(notes[3][1].includes(verifiedRecord))
    const missingScope = {
      ...omitted,
      selected: omitted.selected.map(({ adapter, source_key, source_revision }) => ({
        adapter,
        source_key,
        source_revision,
      })),
    }
    await writeFile(path.join(fixture.stateRoot, 'manual.json'), JSON.stringify(missingScope))
    await assert.rejects(scopeRecovery.resume({ confirmed: true }), /incomplete_selection_evidence/)
    assert.equal(notes.length, 4)
    assert.equal((await scopeRecovery.status()).status, 'operator_action_required')
  }))

test('semantically corrupt controller evidence fails closed and is never overwritten', () =>
  fixtureTest(async ({ driver, fixture, configuration, runtime }) => {
    const identity = { adapter: 'webdav', source_key: 'notes.md', source_revision: 'a'.repeat(64) }
    const admitted = {
      ...identity,
      request_id: 'req-admitted',
      job_id: `job-${'b'.repeat(32)}`,
      preparation_id: `prep-${'c'.repeat(32)}`,
      run_id: 'run-20260101t000000z-abcdef',
    }
    const base = {
      version: 1,
      status: 'completed',
      session_id: 'ses_main',
      request_id: 'req-base',
      results: [
        {
          source: identity,
          preparation_id: admitted.preparation_id,
          run_id: admitted.run_id,
          status: 'ingested',
          commit: 'd'.repeat(40),
          report_path: `/knowledge/incoming/ingest-journal/runs/${admitted.run_id}/report.md`,
        },
      ],
      selected: [admitted],
      completed_sources: 1,
      active: null,
      blocker: null,
      failure: null,
      report_failure: false,
      status_delivery_failed: false,
      resume_verified: true,
      final_status: {
        new: 0,
        outdated: 0,
        current: 1,
        conflict: 0,
        revoked: 0,
        orphaned: 0,
        invalid: 0,
      },
    }
    const file = path.join(fixture.stateRoot, 'manual.json')
    await writeFile(file, JSON.stringify(base))
    assert.equal(
      (await manualIngest({ configuration, runtime }).status()).status,
      'completed',
      'consistent evidence must load',
    )
    const corrupt = [
      {
        name: 'null selection entries with an empty completed run',
        state: { ...base, selected: [null], results: [], completed_sources: 0 },
      },
      {
        name: 'source-derived content smuggled into controller state',
        state: {
          ...base,
          results: [{ ...base.results[0], record: { content: 'Synthetic smuggled content.' } }],
        },
      },
      {
        name: 'unattributable result run',
        state: {
          ...base,
          results: [{ ...base.results[0], run_id: 'run-20260101t000000z-000000' }],
        },
      },
      {
        name: 'completed state with live active evidence',
        state: {
          ...base,
          active: {
            source: identity,
            intent: 'publish',
            job_id: admitted.job_id,
            preparation_id: admitted.preparation_id,
            run_id: admitted.run_id,
          },
        },
      },
      {
        name: 'idle state with recorded work',
        state: { ...base, status: 'idle', session_id: undefined, request_id: undefined },
      },
      {
        name: 'completed state with half-admitted selection',
        state: { ...base, selected: [{ ...identity, request_id: 'req-partial' }] },
      },
      {
        name: 'stop evidence on a completed state',
        state: { ...base, blocker: 'summary_admission_uncertain' },
      },
    ]
    for (const { name, state } of corrupt) {
      const written = JSON.stringify(state)
      await writeFile(file, written)
      await assert.rejects(
        manualIngest({ configuration, runtime }).initialize(),
        /invalid_controller_state/,
        name,
      )
      await assert.rejects(
        driver.submit({ session_id: 'ses_main', request_id: 'req-corrupt-evidence' }),
        /invalid_controller_state/,
        name,
      )
      await assert.rejects(driver.resume({ confirmed: true }), /invalid_controller_state/, name)
      assert.equal(await readFile(file, 'utf8'), written, `${name} must stay untouched`)
    }
  }))

test('status route serves only a content-free operational projection', async () => {
  const token = '1'.repeat(64)
  const identity = { adapter: 'webdav', source_key: 'notes.md', source_revision: 'a'.repeat(64) }
  const runId = 'run-20260101t000000z-abcdef'
  const state = {
    version: 1,
    status: 'completed',
    session_id: 'ses_main',
    request_id: 'req-status',
    selected: [
      {
        ...identity,
        request_id: 'req-admitted',
        job_id: `job-${'b'.repeat(32)}`,
        preparation_id: `prep-${'c'.repeat(32)}`,
        run_id: runId,
      },
    ],
    completed_sources: 1,
    results: [
      {
        source: identity,
        preparation_id: `prep-${'c'.repeat(32)}`,
        run_id: runId,
        status: 'ingested',
        commit: 'd'.repeat(40),
        report_path: `/knowledge/incoming/ingest-journal/runs/${runId}/report.md`,
        source_path: '/knowledge/sources/webdav/notes.md',
        changed_pages: ['webdav/notes.md'],
        record: { content: 'Synthetic smuggled content.' },
        report: { path: `runs/${runId}/report.md` },
      },
    ],
    active: null,
    blocker: null,
    failure: null,
    report_failure: false,
    status_delivery_failed: false,
    resume_verified: true,
    final_status: null,
  }
  const server = manualServer({ token, driver: { status: async () => state } })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    const url = `http://127.0.0.1:${server.address().port}/status`
    const served = await (
      await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    ).json()
    assert.deepEqual(Object.keys(served).sort(), [
      'active',
      'blocker',
      'completed_sources',
      'failure',
      'final_status',
      'report_failure',
      'request_id',
      'results',
      'resume_verified',
      'selected_count',
      'session_id',
      'status',
      'status_delivery_failed',
    ])
    assert.deepEqual(Object.keys(served.results[0]).sort(), [
      'commit',
      'preparation_id',
      'report_path',
      'run_id',
      'source',
      'status',
    ])
    assert.equal(served.results[0].report_path, state.results[0].report_path)
    assert.equal(served.selected_count, 1)
    assert.ok(!JSON.stringify(served).includes('Synthetic smuggled content.'))
    assert.ok(!JSON.stringify(served).includes('source_path'))
    assert.ok(!JSON.stringify(served).includes('changed_pages'))
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})
