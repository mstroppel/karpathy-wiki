// Disposable runtime probe only; never installed in the shipped configuration.
import { writeFile } from 'node:fs/promises'

export default {
  id: 'karpathy-wiki.auto-ingest-guard-probe',
  async setup(ctx) {
    await ctx.permission.hook('evaluate', (event) => {
      event.effect = 'deny'
    })
    await ctx.tool.hook('execute.before', () => {
      throw new Error('auto-ingest-guard-probe: denied')
    })
    await ctx.shell.hook('create.before', async () => {
      await writeFile('/tmp/guard-probe/shell-denied', 'denied\n')
      throw new Error('auto-ingest-guard-probe: denied')
    })
    if (ctx.location.directory === '/tmp/guard-probe/wiki') {
      await writeFile('/tmp/guard-probe/wiki/.guard-probe-ready', 'ready\n')
    }
  },
}
