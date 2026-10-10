import { createHash, randomBytes } from 'node:crypto'
import { mkdir, readFile, readdir, realpath } from 'node:fs/promises'
import path from 'node:path'
import { ingestPublication } from '../config/tools/wiki_ingest_publication_core.mjs'
import { IngestInputError } from '../config/tools/wiki_ingest_errors.mjs'
import {
  loadPreparation,
  assertCleanIngestWiki,
  PREPARATION_RE,
  verifyIngestCommit,
  confinedIngestPath,
} from '../config/tools/wiki_ingest_transaction_core.mjs'
import {
  finishRun,
  loadRun,
  renderReport,
  RUN_ID_RE,
  writeRecord,
} from '../config/tools/wiki_ingest_journal_core.mjs'
import { scanIngestStatus } from '../config/tools/wiki_ingest_status_core.mjs'
import { withIngestLock, writeIngestFile } from '../config/tools/wiki_ingest_storage.mjs'

export const JOB = /^job-[0-9a-f]{32}$/
const REQUEST = /^req-[a-zA-Z0-9_-]{1,96}$/
const MAX_BYTES = 64 * 1024
const hash = (value) =>
  createHash('sha256')
    .update(
      JSON.stringify(value, (_key, item) =>
        item && typeof item === 'object' && !Array.isArray(item)
          ? Object.fromEntries(
              Object.keys(item)
                .sort()
                .map((key) => [key, item[key]]),
            )
          : item,
      ),
    )
    .digest('hex')
const defaults = {
  stateRoot: '/knowledge/publisher',
  root: '/knowledge/incoming/ingest-journal',
  wikiRoot: '/knowledge/wiki',
  sourceRoot: '/knowledge/sources',
}

function object(value, fields, required = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_input')
  if (Object.keys(value).some((key) => !fields.includes(key))) throw new Error('invalid_input')
  if (required.some((key) => !Object.hasOwn(value, key))) throw new Error('invalid_input')
}

function requestId(value) {
  if (typeof value !== 'string' || !REQUEST.test(value)) throw new Error('invalid_request_id')
}

function jobId(value) {
  if (typeof value !== 'string' || !JOB.test(value)) throw new Error('invalid_job_id')
}

// IDs are locators, not credentials. This module is exclusively reachable by
// host/operator exec; never install it as a model tool or expose its stdin/socket.
export async function publisherControl(input, configuration = {}) {
  const opts = { ...defaults, ...configuration }
  if (!/^[0-9a-f]{32}$/.test(opts.bootId ?? '')) throw new Error('invalid_boot_id')
  if (Buffer.byteLength(JSON.stringify(input)) > MAX_BYTES) throw new Error('input_too_large')
  object(
    input,
    ['operation', 'request_id', 'kind', 'source', 'job_id', 'proposal', 'report', 'confirmed'],
    ['operation'],
  )
  await mkdir(opts.stateRoot, { recursive: true, mode: 0o700 })
  if ((await realpath(opts.stateRoot)) !== path.resolve(opts.stateRoot))
    throw new Error('unsafe_state_root')
  await mkdir(path.join(opts.stateRoot, 'queue'), { recursive: true, mode: 0o700 })
  if ((await realpath(path.join(opts.stateRoot, 'queue'))) !== path.join(opts.stateRoot, 'queue'))
    throw new Error('unsafe_queue')
  await withIngestLock(path.join(opts.stateRoot, 'queue.lock'), async () => {
    if (await readJson(path.join(opts.stateRoot, 'control.json'), null)) return
    if (
      (await readdir(path.join(opts.stateRoot, 'queue'))).length ||
      (await readJson(path.join(opts.stateRoot, 'initialized.json'), null))
    )
      throw new Error('missing_state')
    // Persist a marker first: losing control.json must never reset ownership.
    await writeIngestFile(path.join(opts.stateRoot, 'initialized.json'), '{"version":1}\n')
    await saveState(opts, {
      version: 1,
      active: null,
      inflight: null,
      stop: null,
      finished: [],
      responses: {},
    })
  })
  if (input.operation === 'enqueue') return enqueue(opts, input)
  return withIngestLock(path.join(opts.stateRoot, 'writer.lock'), async () => {
    const state = await loadState(opts)
    if (input.operation === 'status') {
      object(input, ['operation'])
      return {
        active: state.active,
        inflight: state.inflight,
        stop: state.stop,
        finished_count: state.finished.length,
        queue: await queued(opts, state),
      }
    }
    if (input.operation === 'recover') return recover(opts, state, input)
    if (state.inflight || state.stop) throw new Error('operator_action_required')
    if (input.operation === 'activate') {
      object(input, ['operation'])
      if (state.active) {
        if (!state.active.preparation_id) throw new Error('operator_action_required')
        return state.active
      }
      const job = await withIngestLock(path.join(opts.stateRoot, 'queue.lock'), async () => {
        const next = (await queued(opts, state))[0]
        if (!next) return null
        state.active = { ...next, preparation_id: null }
        state.inflight = { operation: 'prepare', boot_id: opts.bootId }
        await saveState(opts, state)
        return state.active
      })
      if (!job) return { idle: true }
      return mutate(opts, state, { operation: 'prepare' }, async () => {
        // Never adopt an interrupted run from another authority domain.
        const runIds = await readdir(path.join(opts.root, 'runs')).catch((error) => {
          if (error.code === 'ENOENT') return []
          throw error
        })
        for (const runId of runIds) {
          if (
            RUN_ID_RE.test(runId) &&
            (await loadRun({ root: opts.root, runId })).state !== 'completed'
          )
            throw new Error('unresolved_journal')
        }
        const result = await ingestPublication({
          ...coreOptions(opts),
          operation: 'prepare',
          adapter: job.source.adapter,
          sourceKey: job.source.source_key,
          sourceRevision: job.source.source_revision,
        })
        state.active.preparation_id = result.preparation_id
        state.active.run_id = result.run_id
        return state.active
      })
    }
    if (['propose', 'publish'].includes(input.operation)) {
      requestId(input.request_id)
      const previous = state.responses[input.request_id]
      if (previous) {
        if (previous.digest !== hash(input)) throw new Error('request_id_conflict')
        try {
          return await readResponse(opts, input.request_id, previous)
        } catch (error) {
          state.stop = 'operator_action_required'
          await saveState(opts, state)
          throw error
        }
      }
    }
    jobId(input.job_id)
    if (!state.active || state.active.job_id !== input.job_id) throw new Error('stale_owner')
    if (input.operation === 'block') {
      object(input, ['operation', 'job_id'])
      return mutate(opts, state, { operation: 'block' }, async () => {
        const receipt = await ownedReceipt(opts, state)
        if (receipt.publication?.phase !== 'draft' || receipt.publication.stage_pending)
          throw new Error('uncertain_draft')
        await assertCleanIngestWiki(opts.wikiRoot)
        const blocker =
          'Worker failed before publication; private draft retained for operator review.'
        await writeRecord({
          root: opts.root,
          runId: state.active.run_id,
          record: {
            ...receipt.source,
            preparation_id: receipt.preparation_id,
            status: 'blocked',
            blocker,
            content: 'Not determined; no verified publication.',
            contradictions: 'Not determined.',
            extraction_limits:
              'Complete extraction not verified; private read/staging evidence retained.',
            source_unmodified: false,
          },
        })
        const status = await scanIngestStatus({
          sourceRoot: opts.sourceRoot,
          wikiSourceRoot: path.join(opts.wikiRoot, 'sources'),
        })
        const report = await finishRun({
          root: opts.root,
          runId: state.active.run_id,
          finalStatus: status.summary,
          unfinished: [{ source_path: receipt.source.source_path, blocker }],
        })
        const result = { status: 'blocked', run_id: state.active.run_id, report }
        complete(state)
        return result
      })
    }
    if (input.operation === 'cancel') {
      object(input, ['operation', 'job_id', 'confirmed'], ['confirmed'])
      if (input.confirmed !== true) throw new Error('confirmation_required')
      return mutate(opts, state, { operation: 'cancel' }, async () => {
        const receipt = await ownedReceipt(opts, state)
        if (receipt.publication?.phase !== 'draft' || receipt.publication.stage_pending)
          throw new Error('unsafe_cancel')
        await assertCleanIngestWiki(opts.wikiRoot)
        const status = await scanIngestStatus({
          sourceRoot: opts.sourceRoot,
          wikiSourceRoot: path.join(opts.wikiRoot, 'sources'),
        })
        await finishRun({
          root: opts.root,
          runId: state.active.run_id,
          finalStatus: status.summary,
          unfinished: [
            {
              source_path: receipt.source.source_path,
              blocker: 'Operator cancelled private draft; evidence retained.',
            },
          ],
        })
        // Keep all drafts/receipts as private evidence. No repair or rollback.
        state.finished.push(state.active.job_id)
        state.active = null
        return { cancelled: true }
      })
    }
    if (!['propose', 'publish'].includes(input.operation)) throw new Error('invalid_operation')
    object(
      input,
      ['operation', 'request_id', 'job_id', input.operation === 'propose' ? 'proposal' : 'report'],
      ['request_id'],
    )
    requestId(input.request_id)
    const digest = hash(input)
    let args
    if (input.operation === 'propose') {
      const proposal = input.proposal
      object(
        proposal,
        [
          'operation',
          'page',
          'draft',
          'append',
          'reference',
          'replacement',
          'reviewed',
          'offset',
          'limit',
          'query',
          'changed_pages',
        ],
        ['operation'],
      )
      if (!['read_source', 'inspect', 'stage', 'declare', 'state'].includes(proposal.operation))
        throw new Error('invalid_proposal')
      args = { ...proposal, changedPages: proposal.changed_pages }
    } else {
      object(
        input.report,
        ['title', 'content', 'contradictions', 'extraction_limits'],
        ['title', 'content', 'contradictions', 'extraction_limits'],
      )
      args = {
        operation: 'publish',
        ...input.report,
        extractionLimits: input.report.extraction_limits,
      }
    }
    return mutate(
      opts,
      state,
      { operation: input.operation, request_id: input.request_id, digest },
      async () => {
        await ownedReceipt(opts, state)
        const result = await ingestPublication({
          ...args,
          ...coreOptions(opts),
          preparationId: state.active.preparation_id,
        })
        await saveResponse(opts, state, input.request_id, digest, result)
        if (input.operation === 'publish') {
          await verifyCompleted(opts, state)
          complete(state)
        }
        return result
      },
    )
  })
}

function coreOptions(opts) {
  return { root: opts.root, sourceRoot: opts.sourceRoot, wikiRoot: opts.wikiRoot }
}

async function saveResponse(opts, state, requestId, digest, result) {
  // Read/inspection results contain document text. They belong in the private
  // journal, never in content-free coordination state or status output.
  const relative = `publisher-replays/${requestId}.json`
  await mkdir(path.join(opts.root, 'publisher-replays'), { recursive: true, mode: 0o700 })
  const file = await confinedIngestPath(opts.root, relative, true)
  await writeIngestFile(file, `${JSON.stringify({ result })}\n`)
  state.responses[requestId] = { digest, result_hash: hash(result) }
}

async function readResponse(opts, requestId, response) {
  const relative = `publisher-replays/${requestId}.json`
  const payload = await readJson(await confinedIngestPath(opts.root, relative))
  if (!payload || hash(payload.result) !== response.result_hash)
    throw new Error('invalid_replay_evidence')
  return payload.result
}

async function ownedReceipt(opts, state) {
  const receipt = await loadPreparation({
    ...coreOptions(opts),
    preparationId: state.active.preparation_id,
  })
  if (
    receipt.publication?.run_id !== state.active.run_id ||
    ['adapter', 'source_key', 'source_revision'].some(
      (field) => receipt.source[field] !== state.active.source[field],
    )
  )
    throw new Error('conflicting_owner_evidence')
  return receipt
}

async function verifyCompleted(opts, state) {
  const receipt = await ownedReceipt(opts, state)
  if (receipt.publication.phase !== 'done') throw new Error('unverified_completion')
  await assertCleanIngestWiki(opts.wikiRoot)
  await verifyIngestCommit({ ...coreOptions(opts), record: receipt.publication.record })
  const run = await loadRun({ root: opts.root, runId: state.active.run_id })
  if (
    run.state !== 'completed' ||
    !run.report ||
    run.records.length !== 1 ||
    run.records[0].preparation_id !== receipt.preparation_id ||
    run.records[0].commit !== receipt.publication.commit ||
    run.records[0].status !== 'ingested'
  )
    throw new Error('unverified_journal')
  const report = await readFile(await confinedIngestPath(opts.root, run.report.path), 'utf8')
  const expected = renderReport({
    run,
    records: run.effective,
    counts: run.counts,
    finalStatus: run.final_status,
    unfinished: run.unfinished,
    now: run.report.assembled_at,
  })
  if (report !== expected || Buffer.byteLength(report) !== run.report.bytes)
    throw new Error('unverified_report')
}

async function enqueue(opts, input) {
  object(input, ['operation', 'request_id', 'kind', 'source'], ['request_id', 'kind', 'source'])
  requestId(input.request_id)
  if (!['manual', 'automatic'].includes(input.kind)) throw new Error('invalid_kind')
  object(
    input.source,
    ['adapter', 'source_key', 'source_revision'],
    ['adapter', 'source_key', 'source_revision'],
  )
  if (
    typeof input.source.adapter !== 'string' ||
    !/^[a-z0-9_-]+$/.test(input.source.adapter) ||
    typeof input.source.source_key !== 'string' ||
    !input.source.source_key ||
    input.source.source_key.length > 4000 ||
    typeof input.source.source_revision !== 'string' ||
    !/^[0-9a-f]{64}$/.test(input.source.source_revision)
  )
    throw new Error('invalid_source')
  return withIngestLock(path.join(opts.stateRoot, 'queue.lock'), async () => {
    const file = path.join(opts.stateRoot, 'queue', `${input.request_id}.json`)
    const previous = await readJson(file, null)
    if (previous) {
      if (previous.digest !== hash(input)) throw new Error('request_id_conflict')
      return previous
    }
    const job = {
      job_id: `job-${randomBytes(16).toString('hex')}`,
      request_id: input.request_id,
      kind: input.kind,
      source: input.source,
      digest: hash(input),
      enqueued_at: new Date().toISOString(),
    }
    await writeIngestFile(file, `${JSON.stringify(job)}\n`)
    return job
  })
}

async function queued(opts, state) {
  const jobs = []
  const finished = new Set(state.finished)
  for (const file of await readdir(path.join(opts.stateRoot, 'queue'))) {
    if (!/^[a-zA-Z0-9_-]{1,100}\.json$/.test(file)) continue
    const job = await readJson(path.join(opts.stateRoot, 'queue', file))
    validateJob(job)
    if (!finished.has(job.job_id) && state.active?.job_id !== job.job_id) jobs.push(job)
  }
  return jobs.sort(
    (a, b) =>
      Number(a.kind !== 'manual') - Number(b.kind !== 'manual') ||
      a.enqueued_at.localeCompare(b.enqueued_at) ||
      a.job_id.localeCompare(b.job_id),
  )
}

function validateJob(job) {
  jobId(job.job_id)
  requestId(job.request_id)
  if (
    !['manual', 'automatic'].includes(job.kind) ||
    typeof job.enqueued_at !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(job.enqueued_at) ||
    !Number.isFinite(Date.parse(job.enqueued_at))
  )
    throw new Error('invalid_queue')
  object(
    job.source,
    ['adapter', 'source_key', 'source_revision'],
    ['adapter', 'source_key', 'source_revision'],
  )
  if (
    job.digest !==
    hash({ operation: 'enqueue', request_id: job.request_id, kind: job.kind, source: job.source })
  )
    throw new Error('invalid_queue')
}

async function readJson(file, missing) {
  try {
    return JSON.parse(await readFile(file, 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT' && missing !== undefined) return missing
    throw error
  }
}

async function loadState(opts) {
  const state = await readJson(path.join(opts.stateRoot, 'control.json'))
  if (
    !state ||
    state.version !== 1 ||
    !Array.isArray(state.finished) ||
    !state.responses ||
    typeof state.responses !== 'object' ||
    Array.isArray(state.responses) ||
    !Object.hasOwn(state, 'inflight') ||
    !Object.hasOwn(state, 'stop') ||
    !Object.hasOwn(state, 'active')
  )
    throw new Error('invalid_state')
  if (state.stop !== null && state.stop !== 'operator_action_required')
    throw new Error('invalid_state')
  for (const id of state.finished) jobId(id)
  if (new Set(state.finished).size !== state.finished.length) throw new Error('invalid_state')
  if (state.active !== null) {
    validateJob(state.active)
    if (state.finished.includes(state.active.job_id)) throw new Error('invalid_state')
    if (
      state.active.preparation_id !== null &&
      (!PREPARATION_RE.test(state.active.preparation_id) || !RUN_ID_RE.test(state.active.run_id))
    )
      throw new Error('invalid_state')
  }
  if (state.inflight !== null) {
    if (
      !state.active ||
      !['prepare', 'propose', 'publish', 'cancel', 'block'].includes(state.inflight.operation) ||
      !/^[0-9a-f]{32}$/.test(state.inflight.boot_id)
    )
      throw new Error('invalid_state')
    if (['propose', 'publish'].includes(state.inflight.operation)) {
      requestId(state.inflight.request_id)
      if (!/^[0-9a-f]{64}$/.test(state.inflight.digest)) throw new Error('invalid_state')
    }
  }
  for (const [id, response] of Object.entries(state.responses)) {
    requestId(id)
    object(response, ['digest', 'result_hash'], ['digest', 'result_hash'])
    if (
      ![response.digest, response.result_hash].every(
        (digest) => typeof digest === 'string' && /^[0-9a-f]{64}$/.test(digest),
      )
    )
      throw new Error('invalid_state')
  }
  return state
}

const saveState = (opts, state) =>
  writeIngestFile(path.join(opts.stateRoot, 'control.json'), `${JSON.stringify(state)}\n`)

async function mutate(opts, state, intent, action) {
  state.inflight = { ...intent, boot_id: opts.bootId }
  await saveState(opts, state)
  try {
    const result = await action()
    state.inflight = null
    state.stop = null
    await saveState(opts, state)
    return result
  } catch (error) {
    // Input corrections proven unchanged by the core may be retried; all other
    // uncertainty stays durable and cannot be cleared by an owner assertion.
    const failed = await loadState(opts)
    if (
      error instanceof IngestInputError &&
      error.ingest?.correctable &&
      error.ingest.write_state === 'unchanged'
    )
      failed.inflight = null
    else failed.stop = 'operator_action_required'
    await saveState(opts, failed)
    throw error
  }
}

function complete(state) {
  state.finished.push(state.active.job_id)
  state.active = null
}

async function recover(opts, state, input) {
  object(input, ['operation', 'confirmed'], ['confirmed'])
  if (input.confirmed !== true) throw new Error('confirmation_required')
  if (!state.inflight || !state.active || state.inflight.operation !== 'publish')
    throw new Error('manual_maintenance_required')
  // Restart the whole container first: a killed CLI may leave Git descendants.
  // No lease expiry or wall-clock timeout can prove those processes are gone.
  if (state.inflight.boot_id === opts.bootId) throw new Error('publisher_restart_required')
  const intent = state.inflight
  return mutate(opts, state, { ...intent, operation: 'publish' }, async () => {
    const receipt = await ownedReceipt(opts, state)
    if (!['sealing', 'sealed', 'installing', 'done'].includes(receipt.publication?.phase))
      throw new Error('manual_maintenance_required')
    const result = await ingestPublication({
      ...coreOptions(opts),
      operation: 'resume',
      preparationId: state.active.preparation_id,
    })
    await saveResponse(opts, state, intent.request_id, intent.digest, result)
    await verifyCompleted(opts, state)
    complete(state)
    return result
  })
}
