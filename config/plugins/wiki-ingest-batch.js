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
          'Alle ausgewählten neuen/geänderten Quellen sequenziell einlesen: je eine frische Session und ein privater Entwurf. Isolierte Fehler berichten und fortfahren. batch_id nur zum ausdrücklich gewünschten Abschluss einer unterbrochenen Veröffentlichung.',
        input: {
          type: 'object',
          properties: {
            source_path: {
              type: 'string',
              description: 'Optional: genau diese neue/geänderte Quelle statt Sammelbatch.',
            },
            batch_id: {
              type: 'string',
              description:
                'Nur nach ausdrücklichem Auftrag: vorbereitete Veröffentlichung dieses Batches prüfen und abschließen. Startet keine Worker.',
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
              ownerSession: context.sessionID,
              signal: context.signal,
              authorize: (batchId) =>
                ctx.permission.assert({
                  sessionID: context.sessionID,
                  agent: context.agent,
                  source: { type: 'tool', messageID: context.messageID, id: context.id },
                  action: 'ingest_recovery',
                  resources: [batchId],
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
