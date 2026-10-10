import { timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { manualIngest } from './manual_ingest.mjs'
import { manualRuntime } from './manual_runtime.mjs'

export function manualServer({ token, driver }) {
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
        return send(200, await driver.status())
      if (request.method !== 'POST' || !['/ingest-new', '/resume'].includes(request.url))
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
      return send(200, {
        data: await (request.url === '/resume' ? driver.resume(input) : driver.submit(input)),
      })
    } catch {
      return send(409, { error: 'operator_action_required_or_invalid_input' })
    }
  })
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  if (process.env.WIKI_RUNTIME_READ_ONLY !== 'true' || process.getuid() === 0)
    throw new Error('reader_mode_required')
  const bootId = (await readFile('/tmp/publisher-boot-id', 'utf8')).trim()
  const driver = manualIngest({
    configuration: {
      bootId,
      stateRoot: '/knowledge/publisher',
      root: '/knowledge/incoming/ingest-journal',
      sourceRoot: '/knowledge/sources',
      wikiRoot: '/knowledge/wiki',
    },
    runtime: manualRuntime({
      url: 'http://opencode:4096',
      password: process.env.OPENCODE_PASSWORD,
    }),
  })
  await driver.initialize()
  const server = manualServer({ token: process.env.WIKI_INGEST_CONTROL_TOKEN, driver })
  server.listen(4080, '0.0.0.0')
}
