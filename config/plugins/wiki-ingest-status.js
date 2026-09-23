import { scanIngestStatus } from '/etc/opencode/tools/wiki_ingest_status_core.mjs'

export default {
  id: 'karpathy-wiki.ingest-status',
  setup: async (ctx) => {
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_ingest_status',
        description:
          'Vergleicht die vorhandenen Quellen über ihre Provider-Manifeste mit ihren Wiki-Quellenseiten und meldet den Status aller Quellen.',
        input: {
          type: 'object',
          properties: {
            include_current: {
              type: 'boolean',
              description: 'Auch bereits aktuelle Quellen ausgeben',
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
          const content = JSON.stringify(result, null, 2)
          return { output: result, content }
        },
      })
    })
  },
}
