import { runIngestBatch } from '/etc/opencode/tools/wiki_ingest_batch_core.mjs'
import { runBatchWorker } from '/etc/opencode/tools/wiki_ingest_batch_worker.mjs'

export default {
  id: 'karpathy-wiki.ingest-batch',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_batch',
        description:
          'Verarbeitet alle ausgewählten neuen/geänderten Quellen sequenziell, mit frischem Worker und Commit je Quelle. Isolierte Quellenfehler überspringen; kein Rücksetzen.',
        input: {
          type: 'object',
          properties: {
            source_path: {
              type: 'string',
              description: 'Optional: genau diese neue/geänderte Quelle statt Sammelbatch.',
            },
          },
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (args, context) => {
          const result = await runIngestBatch(
            {
              sourcePath: args.source_path,
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
