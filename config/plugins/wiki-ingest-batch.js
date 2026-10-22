import { inspectIngestBatch, runIngestBatch } from '/etc/opencode/tools/wiki_ingest_batch_core.mjs'
import { createBatchWorker } from '/etc/opencode/tools/wiki_ingest_batch_worker.mjs'

export default {
  id: 'karpathy-wiki.ingest-batch',
  setup: async (ctx) => {
    const worker = await createBatchWorker(ctx)
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_diagnostics',
        description:
          'Aktiven Ingest nur lesend diagnostizieren: Ursprungs-Chat, Dirty-Git-Pfade und konkrete Worker-Fehler. Keine Fortsetzung, keine Änderungen, keine Worker.',
        input: {
          type: 'object',
          properties: {
            offset: {
              type: 'integer',
              minimum: 0,
              description: 'Nächste Quellenseite aus next_offset.',
            },
          },
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args) => ({ content: JSON.stringify(await inspectIngestBatch(args)) }),
      })
      tools.add({
        name: 'wiki_ingest_batch',
        description:
          'Manueller Ingest mit privaten Entwürfen, begrenzter Reparatur und dauerhaft gebundenen Rückfragen. Kein Rücksetzen. Offenen Auftrag mit batch_id/decision_id fortsetzen.',
        input: {
          type: 'object',
          properties: {
            source_path: {
              type: 'string',
              description: 'Optional: genau diese neue/geänderte Quelle statt Sammelbatch.',
            },
            batch_id: { type: 'string', description: 'Vom Tool gelieferte offene Batch-ID.' },
            decision_id: {
              type: 'string',
              description: 'Exakte offene Entscheidungs-ID; niemals aus Prosa rekonstruieren.',
            },
            answer: {
              type: 'string',
              enum: ['retry', 'reprocess', 'defer', 'unresolved', 'cancel'],
            },
            question_offset: {
              type: 'integer',
              minimum: 0,
              description: 'Weitere offene Fragen anzeigen, ohne Worker zu starten.',
            },
          },
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args, context) => {
          const result = await runIngestBatch(
            {
              sourcePath: args.source_path,
              batchId: args.batch_id,
              decisionId: args.decision_id,
              answer: args.answer,
              questionOffset: args.question_offset,
              ownerSession: context.sessionID,
              signal: context.signal,
              authorize: async (resource) =>
                ctx.permission.assert({
                  sessionID: context.sessionID,
                  agent: context.agent,
                  source: { type: 'tool', messageID: context.messageID, id: context.id },
                  action: 'ingest_decision',
                  resources: [resource],
                  save: [],
                }),
            },
            (batch) => worker(context, batch),
          )
          return { content: JSON.stringify(result) }
        },
      })
    })
  },
}
