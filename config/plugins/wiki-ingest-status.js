import {
  scanIngestStatus,
  selectIngestStatus,
} from '/etc/opencode/tools/wiki_ingest_status_core.mjs'

export default {
  id: 'karpathy-wiki.ingest-status',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_status',
        description:
          'Vergleicht Quellen über ihre Provider-Manifeste mit den Wiki-Seiten. Gibt Statusdetails seitenweise aus; adapter kann allein zum Auflisten einer Quellenart verwendet werden.',
        input: {
          type: 'object',
          properties: {
            include_current: {
              type: 'boolean',
              description: 'Auch bereits aktuelle Quellen ausgeben',
            },
            adapter: {
              type: 'string',
              description:
                'Adapter für eine gezielte Revisionsprüfung oder eine seitenweise Adapterliste',
            },
            source_key: {
              type: 'string',
              description:
                'Quellschlüssel für eine gezielte Revisionsprüfung (zusammen mit adapter)',
            },
            summary_only: {
              type: 'boolean',
              description: 'Nur Zähler und Diagnoseeinträge ausgeben, keine Quellendetails',
            },
            offset: {
              type: 'integer',
              minimum: 0,
              description: 'Eintragsposition der Statusseite (Standard: 0)',
            },
            limit: {
              type: 'integer',
              minimum: 1,
              maximum: 25,
              description: 'Maximale Zahl je Status auf einer Seite (Standard: 10)',
            },
          },
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args) => {
          const result = await scanIngestStatus({
            sourceRoot: '/knowledge/sources',
            wikiSourceRoot: '/knowledge/wiki/sources',
            includeCurrent: args.include_current,
          })
          // The tool definition declares no output schema, so the result must
          // not carry a structured `output` field (OpenCode rejects that with
          // "Tool result declared output without an output schema"). The model
          // receives the complete JSON result through `content`.
          return {
            content: JSON.stringify(
              selectIngestStatus(result, {
                adapter: args.adapter,
                sourceKey: args.source_key,
                summaryOnly: args.summary_only,
                offset: args.offset,
                limit: args.limit,
              }),
            ),
          }
        },
      })
    })
  },
}
