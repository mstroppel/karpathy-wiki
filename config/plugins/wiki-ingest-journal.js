import {
  BUDGET_LIMITS,
  assembleReport,
  finishRun,
  listRecords,
  loadRun,
  planNextBatch,
  readChunk,
  startRun,
  writeRecord,
} from '/etc/opencode/tools/wiki_ingest_journal_core.mjs'

// Journal and report tool for wiki ingestion runs. All state lives in the
// private journal directory; the model only ever sees bounded JSON pages.

const JOURNAL_ROOT = '/knowledge/incoming/ingest-journal'
const SOURCE_ROOT = '/knowledge/sources'
const WIKI_SOURCE_ROOT = '/knowledge/wiki/sources'

function envInteger(name, fallback) {
  const raw = process.env[name]
  if (raw === undefined || raw === '') return fallback
  const value = Number(raw)
  if (!Number.isInteger(value)) {
    throw new Error(`${name} muss eine Ganzzahl sein`)
  }
  return value
}

export default {
  id: 'karpathy-wiki.ingest-journal',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_journal',
        description:
          'Verwaltet Einlese-Läufe: dauerhafte Ergebnisdatensätze je Quelle, planbare Batches mit Kontextbudget und der vollständige Abschlussbericht als Datei. Liefert stets begrenzte JSON-Seiten.',
        input: {
          type: 'object',
          properties: {
            operation: {
              type: 'string',
              enum: [
                'run_start',
                'run_status',
                'next_batch',
                'record',
                'list',
                'read',
                'report',
                'run_finish',
              ],
              description: 'Vorgang',
            },
            run_id: {
              type: 'string',
              description: 'Laufkennung; bei run_start ohne resume der zu startende Lauf',
            },
            resume: {
              type: 'boolean',
              description:
                'Bei run_start einen offenen Lauf fortsetzen statt einen neuen zu starten (Standard: true)',
            },
            budget_tokens: {
              type: 'integer',
              minimum: BUDGET_LIMITS.budget_tokens[0],
              maximum: BUDGET_LIMITS.budget_tokens[1],
              description:
                'Geschätztes Kontextbudget je Arbeitssitzung; Standard aus WIKI_INGEST_BATCH_BUDGET_TOKENS',
            },
            max_sources_per_batch: {
              type: 'integer',
              minimum: BUDGET_LIMITS.max_sources_per_batch[0],
              maximum: BUDGET_LIMITS.max_sources_per_batch[1],
              description:
                'Obergrenze Quellen je Batch; Standard aus WIKI_INGEST_BATCH_MAX_SOURCES',
            },
            max_batches_per_run: {
              type: 'integer',
              minimum: BUDGET_LIMITS.max_batches_per_run[0],
              maximum: BUDGET_LIMITS.max_batches_per_run[1],
              description:
                'Batches je Lauf vor Rollover (0 = unbegrenzt); Standard aus WIKI_INGEST_RUN_MAX_BATCHES',
            },
            record: {
              type: 'object',
              description: 'Ergebnisdatensatz einer Quelle für operation record',
              properties: {
                adapter: { type: 'string' },
                source_key: { type: 'string' },
                source_path: { type: 'string' },
                source_revision: { type: 'string' },
                wiki_path: { type: 'string' },
                status: { type: 'string', enum: ['ingested', 'blocked'] },
                preparation_id: {
                  type: ['string', 'null'],
                  description:
                    'Für ingested erforderlich: Kennung aus wiki_ingest_transaction prepare/apply/validate',
                },
                commit: { type: ['string', 'null'] },
                changed_pages: { type: 'array', items: { type: 'string' } },
                content: { type: ['string', 'null'] },
                contradictions: { type: ['string', 'null'] },
                extraction_limits: { type: ['string', 'null'] },
                source_unmodified: { type: 'boolean' },
                blocker: { type: ['string', 'null'] },
              },
              additionalProperties: false,
            },
            offset: { type: 'integer', minimum: 0, description: 'Listenposition (Standard: 0)' },
            limit: {
              type: 'integer',
              minimum: 1,
              maximum: 25,
              description: 'Maximale Zahl Einträge je Seite (Standard: 10)',
            },
            record_index: {
              type: 'integer',
              minimum: 0,
              description: 'Position der Datensatzzeile für operation read',
            },
            report: {
              type: 'boolean',
              description: 'Bei read den Bericht statt eines Datensatzes abrufen',
            },
            chunk_offset: {
              type: 'integer',
              minimum: 0,
              description: 'Zeichenoffset für stückweises Abrufen (Standard: 0)',
            },
            chunk_bytes: {
              type: 'integer',
              minimum: 4,
              maximum: 8192,
              description: 'Maximale UTF-8-Bytezahl je Chunk (Standard: 4096)',
            },
            final_status: {
              type: 'object',
              description: 'Abschließende Statuszahlen für report und run_finish',
              properties: {
                new: { type: 'integer', minimum: 0 },
                outdated: { type: 'integer', minimum: 0 },
                current: { type: 'integer', minimum: 0 },
                conflict: { type: 'integer', minimum: 0 },
                revoked: { type: 'integer', minimum: 0 },
                orphaned: { type: 'integer', minimum: 0 },
                invalid: { type: 'integer', minimum: 0 },
              },
              additionalProperties: false,
            },
            unfinished: {
              type: 'array',
              description: 'Nicht abgeschlossene Quellen mit konkretem Blocker',
              items: {
                type: 'object',
                properties: {
                  source_path: { type: 'string' },
                  blocker: { type: 'string' },
                },
                required: ['source_path', 'blocker'],
                additionalProperties: false,
              },
            },
          },
          required: ['operation'],
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args) => {
          const root = JOURNAL_ROOT
          let result
          switch (args.operation) {
            case 'run_start': {
              const options = {
                root,
                resume: args.resume !== false,
                budgetTokens:
                  args.budget_tokens ?? envInteger('WIKI_INGEST_BATCH_BUDGET_TOKENS', undefined),
                maxSourcesPerBatch:
                  args.max_sources_per_batch ??
                  envInteger('WIKI_INGEST_BATCH_MAX_SOURCES', undefined),
                maxBatchesPerRun:
                  args.max_batches_per_run ?? envInteger('WIKI_INGEST_RUN_MAX_BATCHES', undefined),
              }
              const started = await startRun(options)
              result = {
                run_id: started.run.run_id,
                adopted: started.adopted,
                state: started.run.state,
                budget: started.run.budget,
              }
              break
            }
            case 'run_status': {
              const run = await loadRun({ root, runId: args.run_id })
              result = {
                run_id: run.run_id,
                state: run.state,
                created_at: run.created_at,
                updated_at: run.updated_at,
                budget: run.budget,
                counts: run.counts,
                final_status: run.final_status,
                unfinished: run.unfinished,
                report: run.report,
              }
              break
            }
            case 'next_batch': {
              result = await planNextBatch({
                sourceRoot: SOURCE_ROOT,
                wikiSourceRoot: WIKI_SOURCE_ROOT,
                root,
                runId: args.run_id,
                budgetTokens: args.budget_tokens,
                maxSourcesPerBatch: args.max_sources_per_batch,
              })
              break
            }
            case 'record': {
              result = await writeRecord({ root, runId: args.run_id, record: args.record })
              break
            }
            case 'list': {
              result = await listRecords({
                root,
                runId: args.run_id,
                offset: args.offset,
                limit: args.limit,
              })
              break
            }
            case 'read': {
              result = await readChunk({
                root,
                runId: args.run_id,
                recordIndex: args.record_index,
                report: args.report,
                offset: args.chunk_offset,
                chunkBytes: args.chunk_bytes,
              })
              break
            }
            case 'report': {
              result = await assembleReport({
                root,
                runId: args.run_id,
                finalStatus: args.final_status,
                unfinished: args.unfinished,
              })
              break
            }
            case 'run_finish': {
              result = await finishRun({
                root,
                runId: args.run_id,
                finalStatus: args.final_status,
                unfinished: args.unfinished,
              })
              break
            }
            default:
              throw new Error(`unbekannte operation: ${args.operation}`)
          }
          // No declared output schema: the model receives the complete JSON
          // result through `content`, never a structured `output` field.
          return { content: JSON.stringify(result) }
        },
      })
    })
  },
}
