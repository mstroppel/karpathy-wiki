import { tool } from "@opencode-ai/plugin"
import { scanIngestStatus } from "./wiki_ingest_status_core.mjs"

export default tool({
  description: "Vergleicht WebDAV- und optional aktivierte anonymisierte Paperless-Quellen mit Wiki-Quellenseiten und meldet ihren Revisionsstatus.",
  args: {
    include_current: tool.schema.boolean().optional().describe("Auch bereits aktuelle Quellen ausgeben"),
  },
  async execute(args) {
    const result = await scanIngestStatus({
      sourceRoot: "/knowledge/sources",
      wikiSourceRoot: "/knowledge/wiki/sources",
      paperlessEnabled: (process.env.PAPERLESS_ENABLED ?? "false") === "true",
      includeCurrent: args.include_current,
    })
    return JSON.stringify(result, null, 2)
  },
})
