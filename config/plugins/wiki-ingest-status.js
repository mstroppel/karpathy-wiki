import {
  scanIngestStatus,
  selectIngestStatus,
  waitForPublishedSource,
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
            wait_seconds: {
              type: 'integer',
              minimum: 0,
              maximum: 120,
              description:
                'Mit adapter und source_key auf asynchrone Veröffentlichung warten (Standard: 0). Ein Timeout belegt kein deaktiviertes Profil.',
            },
            status_state: {
              type: 'string',
              enum: ['new', 'outdated', 'current', 'conflict', 'revoked', 'orphaned', 'invalid'],
              description: 'Nur diesen Status ausgeben; ermöglicht unabhängiges Blättern je Status',
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
            record_chunk_offset: {
              type: 'integer',
              minimum: 0,
              description:
                'Zeichenoffset zum Abrufen eines großen Eintrags (mit adapter und source_key)',
            },
            record_chunk_bytes: {
              type: 'integer',
              minimum: 4,
              maximum: 1536,
              description: 'Maximale UTF-8-Bytezahl des Eintragschunks (Standard: 1536)',
            },
            record_chunk_state: {
              type: 'string',
              enum: ['new', 'outdated', 'current', 'conflict', 'revoked', 'orphaned', 'invalid'],
              description: 'Status des Eintrags, der stückweise abgerufen wird',
            },
            record_chunk_index: {
              type: 'integer',
              minimum: 0,
              description: 'Eintragsposition innerhalb von adapter und record_chunk_state',
            },
          },
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args, context) => {
          const result = await waitForPublishedSource(
            () =>
              scanIngestStatus({
                sourceRoot: '/knowledge/sources',
                wikiSourceRoot: '/knowledge/wiki/sources',
                includeCurrent: args.include_current || args.wait_seconds > 0,
              }),
            {
              sourceRoot: '/knowledge/sources',
              adapter: args.adapter,
              sourceKey: args.source_key,
              waitSeconds: args.wait_seconds,
              signal: context.signal,
            },
          )
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
                recordChunkOffset: args.record_chunk_offset,
                recordChunkBytes: args.record_chunk_bytes,
                recordChunkState: args.record_chunk_state,
                recordChunkIndex: args.record_chunk_index,
                statusState: args.status_state,
              }),
            ),
          }
        },
      })
    })
  },
}
