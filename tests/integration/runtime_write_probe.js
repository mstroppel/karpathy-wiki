// Model-free publisher/journal core probe, installed only in a disposable container.
import { open, writeFile } from 'node:fs/promises'
import { startRun } from '/etc/opencode/tools/wiki_ingest_journal_core.mjs'
import { ingestPublication } from '/etc/opencode/tools/wiki_ingest_publication_core.mjs'

async function denied(action) {
  try {
    await action()
  } catch (error) {
    for (let cause = error; cause; cause = cause.cause) {
      if (cause.code === 'EROFS' || cause.message?.includes('Read-only file system')) return 'EROFS'
    }
    throw new Error('probe failed for a reason other than read-only storage', { cause: error })
  }
  throw new Error('probe unexpectedly wrote protected storage')
}

export default {
  id: 'karpathy-wiki.runtime-write-probe',
  async setup(ctx) {
    await ctx.command.transform((commands) => {
      commands.add({
        name: 'runtime-write-probe',
        async execute() {
          const results = {
            journal_core: await denied(() =>
              startRun({ root: '/knowledge/incoming/ingest-journal', resume: false }),
            ),
            publication_lock: await denied(async () => {
              const handle = await open('/knowledge/wiki/.git/wiki-ingest-publication.lock', 'a+')
              await handle.close()
            }),
          }
          // The current core deliberately maps failed lock acquisition to a
          // generic lock error. Verify its refusal separately from the syscall.
          try {
            await ingestPublication({ operation: 'prepare' })
            throw new Error('publisher unexpectedly admitted publication')
          } catch (error) {
            if (error.message !== 'Ein anderer Schreiber hält den Ingest-Lock') throw error
            results.publisher_core = 'rejected_before_publication'
          }
          await writeFile('/tmp/core-probe-result.json', JSON.stringify(results))
        },
      })
    })
    await writeFile('/tmp/core-probe-ready', 'ready\n')
  },
}
