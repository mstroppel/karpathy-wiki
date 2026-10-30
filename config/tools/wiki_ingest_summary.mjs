// Model-written prose is optional, private report data, never publication evidence.
export function parseIngestSummary(content) {
  if (typeof content !== 'string' || content.length > 32 * 1024) return null
  const blocks = [...content.matchAll(/^```ingest-summary\n([\s\S]*?)\n```\s*$/gm)]
  if (blocks.length !== 1) return null
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
      !text(value.source_label, 240) ||
      !text(value.title, 120) ||
      !Array.isArray(value.summary) ||
      value.summary.length < 1 ||
      value.summary.length > 5 ||
      !value.summary.every((sentence) => text(sentence, 400))
    )
      return null
    return {
      source_label: value.source_label.trim(),
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
    source_label: source.brief?.source_label ?? source.source_path,
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
