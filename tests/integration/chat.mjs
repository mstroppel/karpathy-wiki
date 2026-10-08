// Transport/discovery only: never prompts a model or submits wiki answers.
import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'

const origin = 'http://127.0.0.1:3000'
const directory = '/knowledge/wiki'
const statePath = `${process.env.OPENCHAMBER_DATA_DIR}/integration-session.json`
const phase = process.argv[2] ?? 'create'
let cookie

const unauthenticatedBackend = await fetch('http://opencode:4096/api/info', {
  signal: AbortSignal.timeout(30000),
})
assert.equal(unauthenticatedBackend.status, 401, 'Backend is not authenticated')
const backend = await fetch('http://opencode:4096/api/info', {
  headers: {
    Authorization: `Basic ${Buffer.from(`opencode:${process.env.OPENCODE_PASSWORD}`).toString('base64')}`,
  },
  signal: AbortSignal.timeout(30000),
})
assert.ok(backend.ok, `Backend login: HTTP ${backend.status}`)

async function request(path, { method = 'GET', body, authenticated = true } = {}) {
  const response = await fetch(`${origin}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'x-opencode-directory': encodeURIComponent(directory),
      ...(authenticated && cookie ? { Cookie: cookie } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(30000),
  })
  return response
}

async function json(path, options) {
  const response = await request(path, options)
  assert.ok(response.ok, `${options?.method ?? 'GET'} ${path}: HTTP ${response.status}`)
  if (response.status === 204) return undefined
  const payload = await response.json()
  return payload.data ?? payload
}

assert.equal((await request('/api/command', { authenticated: false })).status, 401)
const login = await request('/auth/session', {
  method: 'POST',
  body: { password: process.env.OPENCHAMBER_UI_PASSWORD },
  authenticated: false,
})
assert.ok(login.ok, `UI login: HTTP ${login.status}`)
cookie = login.headers.get('set-cookie')?.split(';')[0]
assert.ok(cookie, 'UI login did not issue a cookie')

// File links use the frontend FS API, not the backend filesystem. Reports live
// outside the wiki workspace and must be read by exact path under UI auth.
const reportRoot = '/knowledge/incoming/ingest-journal/runs'
for (const [run, expected] of [
  ['run-smoke-a', '# Synthetic report A\n'],
  ['run-smoke-b', '# Synthetic report B\n'],
]) {
  const query = new URLSearchParams({
    path: `${reportRoot}/${run}/report.md`,
    directory,
    allowOutsideWorkspace: 'true',
  })
  const route = `/api/fs/read?${query}`
  assert.equal((await request(route, { authenticated: false })).status, 401)
  const response = await request(route)
  assert.ok(response.ok, `Report read: HTTP ${response.status}`)
  assert.equal(await response.text(), expected, 'Viewer API returned the wrong report')
}
const missingReport = new URLSearchParams({
  path: `${reportRoot}/run-missing/report.md`,
  directory,
  allowOutsideWorkspace: 'true',
})
assert.equal((await request(`/api/fs/read?${missingReport}`)).status, 404)

await json('/api/opencode/directory', { method: 'POST', body: { path: directory } })
const commands = await json('/api/command')
const agents = await json('/api/agent')
const skills = await json('/api/skill')
const skillPanel = await json(`/api/config/skills?directory=${encodeURIComponent(directory)}`)
assert.notEqual(skillPanel.openCodeSkillsUnavailable, true, 'UI skill panel cannot reach OpenCode')
for (const name of ['ingest-new', 'analysis', 'analysis-save', 'gap-review']) {
  assert.ok(
    commands.some((entry) => entry.name === name),
    `Missing command: ${name}`,
  )
}
for (const name of ['wiki-ingest', 'wiki-analysis', 'wiki-analysis-save', 'wiki-lint']) {
  assert.ok(
    agents.some((entry) => entry.name === name),
    `Missing agent: ${name}`,
  )
}
for (const name of ['wiki-ingest', 'wiki-analysis', 'wiki-analysis-save', 'wiki-gap-review']) {
  assert.ok(
    skills.some((entry) => entry.name === name),
    `Missing skill: ${name}`,
  )
  assert.ok(
    skillPanel.skills.some((entry) => entry.name === name),
    `Missing UI skill: ${name}`,
  )
}

if (phase === 'create') {
  const session = await json('/api/session', {
    method: 'POST',
    body: { title: 'Disposable OpenChamber smoke', agent: 'build', location: { directory } },
  })
  assert.match(session.id, /^ses/)
  await json(`/api/session/${session.id}/agent`, {
    method: 'POST',
    body: { agent: 'wiki-analysis' },
  })
  const saved = await json(`/api/session/${session.id}`)
  assert.equal(saved.agent, 'wiki-analysis')
  assert.equal(saved.location.directory, directory)
  await writeFile(statePath, JSON.stringify({ id: session.id }), { mode: 0o600 })
} else {
  assert.equal(phase, 'restart')
  const { id } = JSON.parse(await readFile(statePath, 'utf8'))
  const saved = await json(`/api/session/${id}`)
  assert.equal(saved.agent, 'wiki-analysis')
  assert.equal(saved.title, 'Disposable OpenChamber smoke')
  assert.equal(saved.location.directory, directory)
}

const controller = new AbortController()
const timeout = setTimeout(() => controller.abort(), 30000)
try {
  const stream = await fetch(`${origin}/api/event`, {
    headers: { Cookie: cookie, Accept: 'text/event-stream' },
    signal: controller.signal,
  })
  assert.ok(stream.ok, `Event stream: HTTP ${stream.status}`)
  assert.match(stream.headers.get('content-type'), /text\/event-stream/)
  const reader = stream.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  async function nextEvent() {
    while (true) {
      const boundary = buffer.indexOf('\n\n')
      if (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = frame.split('\n').filter((line) => line.startsWith('data:'))
        // Proxy heartbeat comments do not prove that a backend event arrived.
        if (data.length === 0) continue
        return JSON.parse(data.map((line) => line.slice(5).trimStart()).join('\n'))
      }
      const chunk = await reader.read()
      assert.ok(!chunk.done, 'Event stream ended before the expected backend event')
      buffer = (buffer + decoder.decode(chunk.value, { stream: true })).replace(/\r\n/g, '\n')
      assert.ok(buffer.length <= 1048576, 'Event stream frame exceeded test limit')
    }
  }
  // Wait for the native subscription marker before making the mutation.
  let event
  do {
    event = await nextEvent()
  } while (event.type !== 'server.connected')
  const probe = await json('/api/session', {
    method: 'POST',
    body: { title: 'Disposable SSE probe', location: { directory } },
  })
  do {
    event = await nextEvent()
  } while (event.type !== 'session.created' || !JSON.stringify(event).includes(`"${probe.id}"`))
  await reader.cancel()
} finally {
  clearTimeout(timeout)
  controller.abort()
}
console.log(`openchamber-smoke: ${phase} OK (auth, exact report reads, discovery, session, SSE)`)
