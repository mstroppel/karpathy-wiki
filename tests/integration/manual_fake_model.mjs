import { createServer } from 'node:http'

// Deterministic OpenAI-compatible fixture: no real provider, credentials or cost.
createServer(async (req, res) => {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  const prompt = JSON.stringify(body.messages ?? [])
  let answer = 'Synthetic verified manual ingest summary.'
  if (prompt.includes('Privater Auftrag und Codeantworten')) {
    let output
    if (!prompt.includes('Vorschlag:'))
      output = { proposal: { operation: 'read_source', offset: 1, limit: 80 } }
    else if (!prompt.includes('\\"operation\\":\\"inspect\\"'))
      output = { proposal: { operation: 'inspect', page: 'overview.md', offset: 1, limit: 80 } }
    else if (!prompt.includes('\\"operation\\":\\"stage\\"'))
      output = {
        proposal: {
          operation: 'stage',
          draft: '# Synthetic finding\n\nFinding from source line 1.\n',
        },
      }
    else if (!prompt.includes('\\"reviewed\\":true'))
      output = { proposal: { operation: 'stage', page: 'overview.md', reviewed: true } }
    else
      output = {
        report: {
          title: 'Synthetic finding',
          content: 'Finding at line 1.',
          contradictions: 'None.',
          extraction_limits: 'Line 1 fully read.',
        },
      }
    answer = JSON.stringify(output)
  }
  const base = {
    id: 'chatcmpl-synthetic',
    object: 'chat.completion',
    created: 1760000000,
    model: 'synthetic',
  }
  if (body.stream) {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    res.write(
      `data: ${JSON.stringify({ ...base, object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: answer }, finish_reason: null }] })}\n\n`,
    )
    res.write(
      `data: ${JSON.stringify({ ...base, object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } })}\n\n`,
    )
    res.end('data: [DONE]\n\n')
  } else {
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(
      JSON.stringify({
        ...base,
        choices: [
          { index: 0, message: { role: 'assistant', content: answer }, finish_reason: 'stop' },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      }),
    )
  }
}).listen(4081, '0.0.0.0')
