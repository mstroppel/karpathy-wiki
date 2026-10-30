// Model-written prose is optional, private report data, never publication evidence.
const sentences = new Intl.Segmenter('de', { granularity: 'sentence' })
function oneSentence(value) {
  // ICU otherwise splits ordinary German ordinals ("23. Oktober") and titles.
  const protectedText = value
    .replace(
      /\b\d{1,2}\.(?=\s+(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\b)/giu,
      (match) => match.replace('.', ''),
    )
    .replace(/\b(?:z\. B|d\. h|u\. a|Dr|Prof|Nr|bzw|ggf|ca|vgl|usw)\./giu, (match) =>
      match.replaceAll('.', ''),
    )
  return [...sentences.segment(protectedText)].length === 1
}

export function ingestSourceLabel(source, text) {
  let label
  if (source.adapter === 'webdav') {
    label = source.source_key
    if (/^xlsx\/[a-f0-9]{64}\/workbook\.md$/.test(source.source_key)) {
      const heading = /^# Workbook: ([^\r\n]+)\r?\n/.exec(text)?.[1]
      // Decode only the current XLSX provider's inline-escaping format.
      label = heading
        ?.replace(/\\([\\`*_{}[\]()#+.!|~-])/g, '$1')
        .replaceAll('&lt;', '<')
        .replaceAll('&gt;', '>')
        .replaceAll('&amp;', '&')
    }
  } else if (source.adapter === 'email') {
    const boundary = text.indexOf('\n## Message\n')
    const headers = boundary < 0 ? '' : text.slice(0, boundary)
    const subject = /^# ([^\r\n]+)\n/.exec(headers)?.[1]
    const date = /^- \*\*Date:\*\* ([^\r\n]+)$/m.exec(headers)?.[1]
    if (subject) label = `E-Mail: ${subject}${date ? ` · ${date}` : ''}`
  }
  // Unknown formats and overlong labels retain the exact code-owned source path.
  return typeof label === 'string' && label.length <= 240 && label.trim() && !/\p{Cc}/u.test(label)
    ? label
    : source.source_path
}

export function parseIngestSummary(content) {
  if (typeof content !== 'string' || content.length > 32 * 1024) return null
  const blocks = [...content.matchAll(/^```ingest-summary\n([\s\S]*?)\n```[ \t]*(?=\r?\n|$)/gm)]
  if (blocks.length !== 1 || content.slice(blocks[0].index + blocks[0][0].length).trim())
    return null
  const text = (value, max) =>
    typeof value === 'string' &&
    value.trim().length > 0 &&
    value.length <= max &&
    // eslint-disable-next-line no-control-regex -- reject control characters in report prose
    !/[\u0000-\u001f\u007f]/u.test(value)
  try {
    const value = JSON.parse(blocks[0][1])
    if (
      !value ||
      !text(value.title, 120) ||
      !Array.isArray(value.summary) ||
      value.summary.length < 1 ||
      value.summary.length > 5 ||
      !value.summary.every((sentence) => text(sentence, 400) && oneSentence(sentence))
    )
      return null
    return {
      title: value.title.trim(),
      summary: value.summary.map((sentence) => sentence.trim()),
    }
  } catch {
    return null
  }
}

export function ingestSourceResult(source) {
  const published = source.status === 'ingested' && Boolean(source.commit)
  const attempted = source.attempts.length > 0
  return {
    source_path: source.source_path,
    wiki_path: source.wiki_path,
    source_label: source.source_label ?? source.source_path,
    title: source.brief?.title ?? 'Kurzfassung nicht verfügbar',
    status: published ? 'ingested' : attempted ? 'failed' : 'not_started',
    blocker: source.blocker ?? null,
    commit: published ? source.commit : null,
    changed_pages: published ? source.changed_pages : [],
    summary: published
      ? (source.brief?.summary ?? ['Inhaltliche Zusammenfassung nicht verfügbar.'])
      : [
          attempted
            ? 'Nicht übernommen; keine Veröffentlichung verifiziert.'
            : 'Nicht gestartet; keine Veröffentlichung verifiziert.',
          ...(source.blocker ? [`Grund: ${source.blocker}.`] : []),
        ],
  }
}

export function ingestChatSources(sources) {
  const items = []
  for (const source of sources.slice(0, 10)) {
    const item = ingestSourceResult(source)
    // Reserve space below the runtime's tool-output byte limit for batch metadata.
    if (Buffer.byteLength(JSON.stringify([...items, item])) > 12 * 1024) break
    items.push(item)
  }
  return items
}

// Keep private source/model text literal in the generated Markdown, including HTML.
const reportLiteral = (value) =>
  String(value).replace(/[&<>\\`*_{}[\]()#+.!|~-]/g, (character) => `&#${character.charCodeAt(0)};`)

export function ingestSummaryMarkdown(source) {
  const item = ingestSourceResult(source)
  const label = {
    ingested: 'Übernommen',
    failed: 'Nicht übernommen',
    not_started: 'Nicht gestartet',
  }
  return [
    `### ${reportLiteral(item.title)} · ${label[item.status]}`,
    `Quelle: ${reportLiteral(item.source_label)}`,
    `Quellpfad: ${reportLiteral(item.source_path)}`,
    item.summary.map(reportLiteral).join(' '),
    ...(item.status === 'ingested' ? [`Wiki: ${reportLiteral(item.wiki_path)}`] : []),
  ].join('\n\n')
}
