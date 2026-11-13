// Offsets and lengths use Unicode code points, not UTF-16 halves. Each page is
// bounded even when a source consists of one enormous OCR line.
const pageCharacters = 4000
const segmentCharacters = 1000

export function sourceTextLines(bytes) {
  const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
  if (text.includes('\0')) throw new Error('ingest_binary_source')
  // Keep terminators in their original line, including CRLF and trailing blanks.
  return text.match(/[^\n]*\n|[^\n]+$/g) ?? ['']
}

export function sourceReadPage(bytes, input) {
  const lines = sourceTextLines(bytes)
  const offset = input.offset ?? 1
  const limit = input.limit ?? 2000
  const character = input.character_offset ?? 0
  if (
    !Number.isSafeInteger(offset) ||
    offset < 1 ||
    offset > lines.length ||
    !Number.isSafeInteger(limit) ||
    limit < 1 ||
    limit > 2000 ||
    !Number.isSafeInteger(character) ||
    character < 0
  )
    throw new Error('ingest_source_offset')
  const segments = []
  const output = []
  let outputBytes = 0
  let remaining = pageCharacters
  let next
  for (let index = offset - 1; index < Math.min(lines.length, offset - 1 + limit); index++) {
    next = undefined
    const chars = Array.from(lines[index])
    let start = index === offset - 1 ? character : 0
    if (start > chars.length || (start === chars.length && start !== 0))
      throw new Error('ingest_source_offset')
    do {
      const end = Math.min(chars.length, start + segmentCharacters, start + remaining)
      // JSON quoting keeps embedded source text separate from code-owned labels.
      const rendered = `Line ${index + 1}, characters ${start}–${end}/${chars.length}: ${JSON.stringify(chars.slice(start, end).join(''))}`
      if (outputBytes + Buffer.byteLength(rendered) > 10000) {
        next = { offset: index + 1, character_offset: start }
        remaining = 0
        break
      }
      segments.push({ line: index + 1, start, end, length: chars.length })
      output.push(rendered)
      outputBytes += Buffer.byteLength(rendered) + 1
      remaining -= Math.max(1, end - start)
      start = end
      if (remaining <= 0 || segments.length >= 64) {
        if (end < chars.length) next = { offset: index + 1, character_offset: end }
        else if (index + 1 < lines.length) next = { offset: index + 2, character_offset: 0 }
        break
      }
    } while (start < chars.length)
    if (remaining <= 0 || segments.length >= 64) break
    if (index + 1 < lines.length) next = { offset: index + 2, character_offset: 0 }
    else next = undefined
  }
  const continuation = next
    ? `Continue read with ${JSON.stringify({ path: input.path, ...next, limit })}`
    : 'End of source.'
  const content = `${output.join('\n')}\n${continuation}`
  if (Buffer.byteLength(content) > 12000) throw new Error('ingest_source_page_limit')
  return {
    content: [{ type: 'text', text: content }],
    metadata: { ingest_source_read: { version: 1, segments } },
  }
}

export function sourceReadCoverage(tools, sourcePath) {
  const lines = new Map()
  for (const part of tools) {
    if (
      part.name !== 'read' ||
      part.state?.status !== 'completed' ||
      part.state.input?.path !== sourcePath
    )
      continue
    const receipt = part.state.metadata?.ingest_source_read
    if (receipt?.version !== 1 || !Array.isArray(receipt.segments)) continue
    // Metadata alone must not certify text removed by the runtime output cap.
    const visible = new Set()
    for (const content of part.state.content ?? []) {
      if (content.type !== 'text') continue
      for (const match of content.text.matchAll(
        /^Line (\d+), characters (\d+)–(\d+)\/(\d+): (.*)$/gm,
      )) {
        try {
          const value = JSON.parse(match[5])
          if (
            typeof value === 'string' &&
            Array.from(value).length === Number(match[3]) - Number(match[2])
          )
            visible.add(match.slice(1, 5).join(':'))
        } catch {
          // A partially retained JSON string is not a delivered segment.
        }
      }
    }
    for (const segment of receipt.segments) {
      const { line, start, end, length } = segment
      if (
        ![line, start, end, length].every(Number.isSafeInteger) ||
        line < 1 ||
        start < 0 ||
        end < start ||
        length < end
      )
        continue
      const entry = lines.get(line) ?? { length, ranges: [] }
      if (!visible.has(`${line}:${start}:${end}:${length}`)) continue
      if (entry.length !== length) continue
      entry.ranges.push([start, end])
      lines.set(line, entry)
    }
  }
  const covered = new Set()
  for (const [line, { length, ranges }] of lines) {
    let end = 0
    for (const [start, stop] of ranges.sort((a, b) => a[0] - b[0])) {
      if (start > end) break
      end = Math.max(end, stop)
    }
    if (end === length) covered.add(line)
  }
  return covered
}
