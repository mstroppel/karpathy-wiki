import { ingestPublication } from '/etc/opencode/tools/wiki_ingest_publication_core.mjs'
import { ingestFailure } from '/etc/opencode/tools/wiki_ingest_errors.mjs'

export default {
  id: 'karpathy-wiki.ingest-transaction',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_transaction',
        description:
          'Eine Quelle in einem privaten Entwurf bearbeiten: prepare, read_source (vollständig paginiert), inspect (gezielte Wiki-Abschnitte mit Referenz), stage (Quellseiten-draft, neue Seite, append oder Referenz+replacement; reviewed bestätigt unveränderte Übersicht), publish (prüft alle Seiten, erzeugt Index/Log, genau einen Commit und Journal). Keine eigenen Git-/Wiki-Schreibaufrufe. state prüft unterbrochene Arbeit; resume gleicht gespeicherte Publikation ohne doppelte Commits ab. rollback nur bestätigt. Bei error nur correctable Eingaben einmal korrigieren; unbekannte Zustände zuerst prüfen.',
        input: {
          type: 'object',
          properties: {
            operation: {
              type: 'string',
              enum: [
                'prepare',
                'read_source',
                'inspect',
                'stage',
                'publish',
                'state',
                'resume',
                'rollback',
                'declare',
              ],
            },
            adapter: { type: 'string' },
            source_key: { type: 'string' },
            source_revision: { type: 'string', pattern: '^[0-9a-f]{64}$' },
            run_id: { type: 'string' },
            budget_tokens: { type: 'integer', minimum: 1000, maximum: 1000000 },
            changed_pages: {
              type: 'array',
              minItems: 1,
              maxItems: 100,
              items: { type: 'string' },
              description:
                'prepare: Quellseite, overview.md, index.md, log.md und weitere thematische Seiten.',
            },
            preparation_id: { type: 'string', pattern: '^prep-[0-9a-f]{32}$' },
            page: {
              type: 'string',
              description:
                'Relativer deklarierter Wiki-Pfad; Standard Quellseite. Index/Log sind codegeneriert.',
            },
            offset: { type: 'integer', minimum: 1 },
            limit: { type: 'integer', minimum: 1, maximum: 80 },
            query: {
              type: 'string',
              description:
                'inspect: wörtliche Suche ab offset; Antwort ist ein begrenzter Abschnitt, nicht die gesamte Seite.',
            },
            draft: {
              type: 'string',
              description:
                'stage: vollständige Quellseite ohne kanonische Metadaten oder neue thematische Seite.',
            },
            reference: {
              type: 'string',
              pattern: '^[0-9a-f]{64}$',
              description:
                'stage: unveränderte Referenz aus inspect; kein abgeschriebener old_text.',
            },
            replacement: {
              type: 'string',
              description:
                'stage: Ersatz ausschließlich für den referenzierten Abschnitt; übrige Seite bleibt erhalten.',
            },
            append: {
              type: 'string',
              description:
                'stage: an eine bestehende thematische Seite anhängen, mit benötigten Zeilenumbrüchen.',
            },
            reviewed: {
              type: 'boolean',
              description:
                'stage: unveränderte thematische Seite nach gezielter Prüfung bestätigen.',
            },
            title: {
              type: 'string',
              maxLength: 200,
              description:
                'publish: einfacher einzeiliger Katalogtitel; Code erzeugt Index und Log.',
            },
            content: { type: 'string', maxLength: 4000 },
            contradictions: { type: 'string', maxLength: 4000 },
            extraction_limits: { type: 'string', maxLength: 4000 },
            confirmed: {
              type: 'boolean',
              description: 'rollback: nur nach ausdrücklicher Zustimmung true.',
            },
          },
          required: ['operation'],
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args) => {
          try {
            const result = await ingestPublication({
              ...args,
              sourceKey: args.source_key,
              sourceRevision: args.source_revision,
              preparationId: args.preparation_id,
              changedPages: args.changed_pages,
              runId: args.run_id,
              budgetTokens: args.budget_tokens,
              extractionLimits: args.extraction_limits,
            })
            return { content: JSON.stringify(result) }
          } catch (error) {
            return {
              content: JSON.stringify({
                error: error.ingest ?? ingestFailure(error, args.preparation_id),
              }),
            }
          }
        },
      })
    })
  },
}
