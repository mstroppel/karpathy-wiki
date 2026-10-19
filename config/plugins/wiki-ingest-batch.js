import { runIngestBatch } from '/etc/opencode/tools/wiki_ingest_batch_core.mjs'
import { runBatchWorker } from '/etc/opencode/tools/wiki_ingest_batch_worker.mjs'

export default {
  id: 'karpathy-wiki.ingest-batch',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_batch',
        description:
          'Verarbeitet exklusiv bis zu fünf neue/geänderte Quellen: direkter Wiki-Worker, ein Commit und privater Bericht pro Batch. Fehler bleiben zur manuellen Prüfung liegen; kein automatisches Rücksetzen.',
        input: {
          type: 'object',
          properties: {
            source_path: {
              type: 'string',
              description: 'Optional: genau diese neue/geänderte Quelle statt Sammelbatch.',
            },
            max_sources: { type: 'integer', minimum: 1, maximum: 5 },
          },
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args, context) => {
          const result = await runIngestBatch(
            {
              sourcePath: args.source_path,
              maxSources: args.max_sources,
              signal: context.signal,
            },
            (batch) => runBatchWorker(ctx, context, batch),
          )
          return { content: JSON.stringify(result) }
        },
      })
    })
  },
}
