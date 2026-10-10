import { readFile } from 'node:fs/promises'
import { publisherControl } from './control.mjs'

// One bounded JSON document on stdin. stdout is a private operator response;
// stderr and service logs never contain source text, raw errors or credentials.
try {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > 64 * 1024) throw new Error('input_too_large')
    chunks.push(chunk)
  }
  const text = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))
  const bootId = (await readFile('/tmp/publisher-boot-id', 'utf8')).trim()
  const result = await publisherControl(JSON.parse(text), { bootId })
  process.stdout.write(`${JSON.stringify({ result })}\n`)
} catch {
  process.stdout.write(
    '{"error":"publisher_request_refused","hint":"Inspect private status; uncertainty requires operator action."}\n',
  )
  process.exitCode = 1
}
