// Real process death at durable publication boundaries (synthetic wiki only).
import { readFile, writeFile } from 'node:fs/promises'
import { runIngestBatch } from '../../config/tools/wiki_ingest_batch_core.mjs'

const opts = JSON.parse(await readFile(process.argv[2], 'utf8'))
let files = 0
await runIngestBatch(
  {
    ...opts,
    onPhase: (phase) => {
      if (phase === 'file_installed') files++
      if (
        phase === process.argv[3] &&
        (phase !== 'file_installed' || files === Number(process.argv[4]))
      )
        process.kill(process.pid, 'SIGKILL')
    },
  },
  async (batch) => {
    await writeFile(batch.sources[0].wiki_path, '# Recovered synthetic finding\nSource line 1.\n')
    return { source_written: true, errors: [] }
  },
)
