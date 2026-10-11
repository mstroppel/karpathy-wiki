import { timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { manualIngest } from './manual_ingest.mjs'
import { manualAuthoring } from './authoring.mjs'
import { manualRuntime } from './manual_runtime.mjs'

// The authenticated status route serves only operational status, identifiers
// and report paths. Source-derived record/report text never leaves the private
// journal and its file viewer (docs/data-layout.md).
function statusProjection(state) {
  const reference = (entry) => ({
    source: entry.source,
    preparation_id: entry.preparation_id,
    run_id: entry.run_id,
    status: entry.status,
    commit: entry.commit,
    report_path: entry.report_path,
  })
  return {
    status: state.status,
    session_id: state.session_id ?? null,
    request_id: state.request_id ?? null,
    completed_sources: state.completed_sources ?? 0,
    selected_count: state.selected?.length ?? 0,
    results: (state.results ?? []).map(reference),
    failure: state.failure ? reference(state.failure) : null,
    active: state.active ? { source: state.active.source, intent: state.active.intent } : null,
    blocker: state.blocker ?? null,
    report_failure: state.report_failure ?? false,
    status_delivery_failed: state.status_delivery_failed ?? false,
    resume_verified: state.resume_verified ?? false,
    final_status: state.final_status ?? null,
  }
}

// Authoring state is content-free for the same reason: identifiers, paths and
// verified commits only, never saved page or answer text.
function authoringProjection(state) {
  if (!state) return null
  return {
    status: state.status,
    request_id: state.request_id,
    kind: state.kind,
    target: state.target,
    phase: state.phase,
    blocker: state.blocker,
    result: state.result,
    status_delivery_failed: state.status_delivery_failed,
  }
}

// Known content-free error names pass through for operator feedback; anything
// else stays a generic refusal so internal details never leak.
const ERRORS = {
  invalid_input: 400,
  invalid_prepared_block: 400,
  prepared_block_missing: 404,
  answers_file_exists: 409,
  wiki_public_url_missing: 409,
  busy: 409,
  operator_action_required_or_busy: 409,
  publisher_maintenance_required: 409,
  confirmation_required: 409,
  not_stopped: 409,
  request_id_conflict: 409,
  invalid_operator_session: 403,
}

export function manualServer({ token, driver, authoring }) {
  if (!/^[a-f0-9]{64}$/.test(token ?? '')) throw new Error('invalid_control_token')
  return createServer(async (request, response) => {
    const supplied = Buffer.from(request.headers.authorization ?? '')
    const expected = Buffer.from(`Bearer ${token}`)
    const send = (status, body) => {
      response.writeHead(status, {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
      })
      response.end(JSON.stringify(body))
    }
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected))
      return send(401, { error: 'unauthorized' })
    try {
      if (request.method === 'GET' && request.url === '/status')
        return send(200, {
          ...statusProjection(await driver.status()),
          authoring: authoringProjection(await authoring.status()),
        })
      const routes = [
        '/ingest-new',
        '/resume',
        '/analysis-save',
        '/answer-save',
        '/authoring-resume',
      ]
      if (request.method !== 'POST' || !routes.includes(request.url))
        return send(404, { error: 'not_found' })
      if (request.headers['content-type'] !== 'application/json')
        return send(400, { error: 'invalid_input' })
      const chunks = []
      let bytes = 0
      for await (const chunk of request) {
        bytes += chunk.length
        if (bytes > 4096) return send(413, { error: 'input_too_large' })
        chunks.push(chunk)
      }
      const input = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      const result =
        request.url === '/ingest-new'
          ? await driver.submit(input)
          : request.url === '/resume'
            ? await driver.resume(input)
            : request.url === '/analysis-save'
              ? await authoring.save('analysis', input)
              : request.url === '/answer-save'
                ? await authoring.save('answers', input)
                : await authoring.resume(input)
      return send(200, { data: result })
    } catch (error) {
      return send(ERRORS[error.message] ?? 409, {
        error: ERRORS[error.message] ?? 'operator_action_required_or_invalid_input',
      })
    }
  })
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  if (process.env.WIKI_RUNTIME_READ_ONLY !== 'true' || process.getuid() === 0)
    throw new Error('reader_mode_required')
  const bootId = (await readFile('/tmp/publisher-boot-id', 'utf8')).trim()
  const configuration = {
    bootId,
    stateRoot: '/knowledge/publisher',
    root: '/knowledge/incoming/ingest-journal',
    sourceRoot: '/knowledge/sources',
    wikiRoot: '/knowledge/wiki',
    answersRoot: '/knowledge/incoming/answers',
    wikiUrl: process.env.WIKI_PUBLIC_URL || null,
  }
  const runtime = manualRuntime({
    url: 'http://opencode:4096',
    password: process.env.OPENCODE_PASSWORD,
  })
  const driver = manualIngest({ configuration, runtime })
  const authoring = manualAuthoring({ configuration, runtime })
  await driver.initialize()
  await authoring.initialize()
  const server = manualServer({ token: process.env.WIKI_INGEST_CONTROL_TOKEN, driver, authoring })
  server.listen(4080, '0.0.0.0')
}
