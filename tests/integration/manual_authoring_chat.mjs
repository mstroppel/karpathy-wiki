import assert from 'node:assert/strict'

const origin = 'http://127.0.0.1:3000'
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
let cookie
async function request(route, body, extra = {}) {
  return fetch(`${origin}${route}`, {
    method: body ? 'POST' : 'GET',
    headers: {
      'Content-Type': 'application/json',
      Origin: origin,
      ...(cookie ? { Cookie: cookie } : {}),
      ...extra,
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30000),
  })
}
async function backend(route, body) {
  return fetch(`http://opencode:4096${route}`, {
    method: body ? 'POST' : 'GET',
    headers: {
      Authorization: `Basic ${Buffer.from(`opencode:${process.env.OPENCODE_PASSWORD}`).toString('base64')}`,
      'Content-Type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30000),
  })
}
const status = async () =>
  (
    await fetch('http://manual-ingest:4080/status', {
      headers: { Authorization: `Bearer ${process.env.WIKI_INGEST_CONTROL_TOKEN}` },
      signal: AbortSignal.timeout(10000),
    })
  ).json()
async function settle(requestId) {
  const deadline = Date.now() + 120000
  let state
  do {
    state = (await status()).authoring
    if (state.request_id === requestId && state.status !== 'writing') return state
    await sleep(500)
  } while (Date.now() < deadline)
  throw new Error(`authoring did not settle: ${JSON.stringify(state)}`)
}

const login = await request('/auth/session', { password: process.env.OPENCHAMBER_UI_PASSWORD })
assert.ok(login.ok)
cookie = login.headers.get('set-cookie').split(';')[0]
const created = await request('/api/session', {
  location: { directory: '/knowledge/wiki' },
  model: { providerID: 'fake', id: 'synthetic' },
  agent: 'build',
})
assert.ok(created.ok)
const { data: session } = await created.json()

// Prepared content arrives as ordinary agent output in the conversation; only
// the explicit UI command admits the trusted write.
assert.ok(
  (
    await backend(`/api/session/${session.id}/generate`, {
      prompt: 'Vorbereite den Analyseblock für die Integration.',
    })
  ).ok,
)
const admitted = await request(`/api/session/${session.id}/command`, {
  name: 'analysis-save',
  text: '',
})
assert.ok(admitted.ok, `analysis admission HTTP ${admitted.status}`)
const analysis = await settle((await admitted.json()).data.request_id)
assert.equal(analysis.status, 'completed', JSON.stringify(analysis))
assert.equal(analysis.result.kind, 'analysis')
assert.equal(analysis.result.name, 'integration-analyse')
assert.match(analysis.result.commit, /^[0-9a-f]{40}$/)
assert.deepEqual(analysis.result.changed_pages, [
  'analyses/integration-analyse.md',
  'assets/analyses/integration-analyse.html',
  'index.md',
  'log.md',
])

for (const body of [
  { name: 'analysis-save', text: 'mit Argumenten' },
  { name: 'answer-save', text: '', files: ['x.md'] },
]) {
  const refused = await request(`/api/session/${session.id}/command`, body)
  assert.equal(refused.status, 403)
  assert.equal((await refused.json()).error, 'explicit_same_origin_authoring_required')
}

assert.ok(
  (
    await backend(`/api/session/${session.id}/generate`, {
      prompt: 'Vorbereite den Antwortblock für die Integration.',
    })
  ).ok,
)
const submitted = await request(`/api/session/${session.id}/command`, {
  name: 'answer-save',
  text: '',
})
assert.ok(submitted.ok, `answer admission HTTP ${submitted.status}`)
const answers = await settle((await submitted.json()).data.request_id)
assert.equal(answers.status, 'completed', JSON.stringify(answers))
assert.equal(answers.result.kind, 'answers')
assert.equal(answers.result.name, 'antwort-integration')
assert.match(answers.result.path, /\/incoming\/answers\/antwort-integration\.md$/)
assert.equal(answers.result.commit, null)

const deadline = Date.now() + 60000
let summarized
do {
  const messages = await (await request(`/api/session/${session.id}/message`)).json()
  summarized = messages.data.some((message) =>
    JSON.stringify(message).includes('Trusted authoring status'),
  )
  if (summarized) break
  await sleep(500)
} while (Date.now() < deadline)
assert.ok(summarized, 'No trusted authoring summary in the main session')

const forged = await fetch('http://manual-ingest:4080/analysis-save', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id: session.id, request_id: 'req-forged' }),
})
assert.equal(forged.status, 401)
console.log('manual-authoring-chat: OK (UI admission, verified analysis commit, answer inbox)')
