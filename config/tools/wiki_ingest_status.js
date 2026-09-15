import { tool } from "@opencode-ai/plugin"
import { scanIngestStatus } from "./wiki_ingest_status_core.mjs"

export default tool({
  description: "Vergleicht die vorhandenen Quellen mit ihren Wiki-Quellenseiten und meldet den Status aller erkannten Adapter.",
  args: {
    include_current: tool.schema.boolean().optional().describe("Auch bereits aktuelle Quellen ausgeben"),
  },
  async execute(args) {
    const result = await scanIngestStatus({
      sourceRoot: "/knowledge/sources",
      wikiSourceRoot: "/knowledge/wiki/sources",
      adapterRoot: "/etc/opencode/ingest-adapters",
      includeCurrent: args.include_current,
    })
    return JSON.stringify(result, null, 2)
  },
})
