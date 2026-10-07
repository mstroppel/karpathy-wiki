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
          'Ein ausgewählter Quelleinleseauftrag: prepare vor Änderungen im sauberen Wiki, changed_pages enthält Quellseite, overview.md, index.md und log.md. apply schreibt die Quellseite oder neue thematische Seiten mit draft; bestehende thematische Seiten ausschließlich mit exakten edits oder append. edits: [] bestätigt eine unveränderte Seite. log.md ist nur ergänzbar. validate erfordert apply für alle Pflichtseiten und einen neuen Logeintrag vor dem expliziten Git-Commit. Quellen bleiben unverändert. Journal status=ingested prüft den tatsächlichen Commit.',
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
                'Bei prepare: alle vorgesehenen relativen Wiki-Markdown-Pfade einschließlich Quellseite, overview.md, index.md und log.md; nur saubere oder noch nicht vorhandene Ziele',
            },
            preparation_id: { type: 'string', pattern: '^prep-[0-9a-f]{32}$' },
            page: {
              type: 'string',
              description:
                'Bei apply: deklarierter relativer Wiki-Pfad; Standard ist die Quellseite.',
            },
            draft: {
              type: 'string',
              description:
                'Bei apply: vollständiger Quellseiteninhalt oder neue thematische Seite. Bestehende thematische Seiten benötigen edits oder append. Auf der Quellseite Felder aus prepare.canonical_fields weglassen; Frontmatter mit Zusatzfeldern ist optional. Falsche Identität/Revision wird abgelehnt; bestehende Zusatzfelder bleiben erhalten.',
            },
            edits: {
              type: 'array',
              maxItems: 100,
              description:
                'Bei apply für bestehende thematische Seiten: sequenzielle exakte Ersetzungen; old_text muss jeweils genau einmal vorkommen. [] bestätigt nach Prüfung unveränderten Inhalt. Nicht für log.md.',
              items: {
                type: 'object',
                properties: {
                  old_text: { type: 'string', minLength: 1 },
                  new_text: { type: 'string' },
                },
                required: ['old_text', 'new_text'],
                additionalProperties: false,
              },
            },
            append: {
              type: 'string',
              description:
                'Bei apply: Text einschließlich benötigter Zeilenumbrüche exakt an bestehende thematische Seite anhängen; für log.md verpflichtend. Genau eines von draft, edits oder append.',
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
            page: args.page,
            draft: args.draft,
            edits: args.edits,
            append: args.append,
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
