import assert from 'node:assert/strict'

const origin = 'http://127.0.0.1:3000'
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
const body = { name: 'ingest-new', text: '' }
assert.equal((await request('/api/session/ses_forbidden/command', body)).status, 401)
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
assert.equal(
  (
    await request(`/api/session/${session.id}/command`, body, {
      Origin: 'https://untrusted.invalid',
    })
  ).status,
  403,
)
assert.equal(
  (await request(`/api/session/${session.id}/command`, body, { Origin: '' })).status,
  403,
)
const admitted = await request(`/api/session/${session.id}/command`, body)
assert.ok(admitted.ok, `admission HTTP ${admitted.status}`)
const token = process.env.WIKI_INGEST_CONTROL_TOKEN
const status = async () => {
  const response = await fetch('http://manual-ingest:4080/status', {
    headers: { Authorization: `Bearer ${token}` },
  })
  return response.json()
}
const deadline = Date.now() + 120000
let state
do {
  state = await status()
  if (state.status !== 'running') break
  await new Promise((resolve) => setTimeout(resolve, 500))
} while (Date.now() < deadline)
assert.equal(state.status, 'completed', JSON.stringify(state))
assert.equal(state.results.length, process.argv[2] === 'noop' ? 0 : 1)
if (state.results.length) {
  assert.equal(state.results[0].status, 'ingested')
  assert.ok(state.results[0].report.path)
  const path = `/knowledge/incoming/ingest-journal/${state.results[0].report.path}`
  const report = await request(
    `/api/fs/read?${new URLSearchParams({ path, directory: '/knowledge/wiki', allowOutsideWorkspace: 'true' })}`,
  )
  assert.ok(report.ok)
  assert.match(await report.text(), /Finding at line 1/)
}
let summarized
do {
  const messages = await (await request(`/api/session/${session.id}/message`)).json()
  summarized = messages.data.some(
    (message) =>
      message.type === 'assistant' &&
      JSON.stringify(message).includes('Synthetic verified manual ingest summary.'),
  )
  if (summarized) break
  await new Promise((resolve) => setTimeout(resolve, 500))
} while (Date.now() < deadline)
assert.ok(summarized, 'No streamed main-session summary')
const forbidden = await fetch('http://manual-ingest:4080/ingest-new', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id: session.id, request_id: 'req-forged' }),
})
assert.equal(forbidden.status, 401)
console.log(
  'manual-ingest-chat: OK (UI auth/origin, trusted admission, isolated worker, commit/report)',
)
