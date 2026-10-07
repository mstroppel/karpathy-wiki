import {
  applyIngestDraft,
  prepareIngest,
  validateIngest,
} from '/etc/opencode/tools/wiki_ingest_transaction_core.mjs'

export default {
  id: 'karpathy-wiki.ingest-transaction',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_transaction',
        description:
          'Ein ausgewählter Quelleinleseauftrag: prepare(adapter, source_key, source_revision, changed_pages) vor Änderungen im sauberen Wiki; apply(preparation_id, draft) schreibt den neuen Quellseiteninhalt mit frischem Frontmatter; validate(preparation_id) prüft alle deklarierten Änderungen vor dem expliziten Git-Commit. Quellen bleiben unverändert. Journal status=ingested erfordert preparation_id und prüft den tatsächlichen Commit.',
        input: {
          type: 'object',
          properties: {
            operation: { type: 'string', enum: ['prepare', 'apply', 'validate'] },
            adapter: { type: 'string' },
            source_key: { type: 'string' },
            source_revision: { type: 'string', pattern: '^[0-9a-f]{64}$' },
            changed_pages: {
              type: 'array',
              minItems: 1,
              maxItems: 100,
              items: { type: 'string' },
              description:
                'Bei prepare: alle vorgesehenen relativen Wiki-Markdown-Pfade einschließlich der Quellseite; nur saubere oder noch nicht vorhandene Ziele',
            },
            preparation_id: { type: 'string', pattern: '^prep-[0-9a-f]{32}$' },
            draft: {
              type: 'string',
              description:
                'Bei apply: vollständiger neu erarbeiteter Quellseiteninhalt; Felder aus prepare.canonical_fields im Draft weglassen. Frontmatter mit Zusatzfeldern ist optional. Falsche mitgelieferte Identität/Revision wird abgelehnt, nicht repariert; bestehende Zusatzfelder bleiben erhalten.',
            },
          },
          required: ['operation'],
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args) => {
          const input = {
            adapter: args.adapter,
            sourceKey: args.source_key,
            sourceRevision: args.source_revision,
            changedPages: args.changed_pages,
            preparationId: args.preparation_id,
            draft: args.draft,
          }
          let result
          switch (args.operation) {
            case 'prepare':
              result = await prepareIngest(input)
              break
            case 'apply':
              result = await applyIngestDraft(input)
              break
            case 'validate':
              result = await validateIngest(input)
              break
            default:
              throw new Error('operation ist ungültig')
          }
          return { content: JSON.stringify(result) }
        },
      })
    })
  },
}
