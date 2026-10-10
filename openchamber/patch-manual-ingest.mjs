import { readFile, writeFile } from 'node:fs/promises'

// Fail the image build if the integrity-locked upstream changes this auth slot.
const file = process.argv[2]
const anchor = '  uiAuthController = bootstrapResult.uiAuthController;'
const source = await readFile(file, 'utf8')
if (source.split(anchor).length !== 2) throw new Error('missing_unique_ui_auth_slot')
await writeFile(
  file,
  `import { manualIngestMiddleware } from '/etc/openchamber/manual-ingest.mjs';\n${source.replace(anchor, `${anchor}\n  app.use(manualIngestMiddleware({ token: process.env.WIKI_INGEST_CONTROL_TOKEN, origin: process.env.WIKI_CHAT_ORIGIN, url: process.env.WIKI_INGEST_CONTROL_URL, parseBody: express.json({ limit: '64kb' }) }));`)}`,
)
