import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { manualIngest } from '../publisher/manual_ingest.mjs'
import { publisherFixture, REPORT } from './helpers/publisher-fixture.mjs'
import { manualRuntime } from '../publisher/manual_runtime.mjs'
import { manualServer } from '../publisher/manual_server.mjs'

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
  fixtureTest(async ({ driver, runtime }) => {
    const notes = []
    runtime.note = async (...args) => notes.push(args)
    await driver.submit({ session_id: 'ses_main', request_id: 'req-blocked-overview' })
    await driver.wait()
    const state = await driver.status()
    assert.equal(state.failure.record.status, 'blocked')
    assert.equal(state.failure.record.content, 'Not determined; no verified publication.')
    assert.ok(state.failure.report_path.endsWith('/report.md'))
    assert.ok(notes[0][1].includes(JSON.stringify(state.failure.record)))
    assert.ok(notes[0][1].includes(state.failure.report_path))
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
    const verifiedRecord = JSON.stringify(stopped.results[0].record)
    const snapshot = structuredClone(stopped)
    snapshot.results[0].record.content = 'Synthetic unverified snapshot detail.'
    await writeFile(path.join(fixture.stateRoot, 'manual.json'), JSON.stringify(snapshot))
    const restarted = manualIngest({
      configuration: { ...configuration, bootId: '2'.repeat(32) },
      runtime,
    })
    await restarted.initialize()
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
      results: [],
      active: {
        source: stopped.results[0].source,
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
  }))
