import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

// Seed a fresh UI only. Existing user settings are never rewritten.
export async function initializeSettings(directory, wikiName) {
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const settings = {
    projects: [{ id: 'wiki', path: '/knowledge/wiki', label: wikiName, defaultAgent: 'build' }],
    activeProjectId: 'wiki',
    lastDirectory: '/knowledge/wiki',
    agentControlToolEnabled: false,
    agentWebToolEnabled: false,
    agentMemoryToolEnabled: false,
    showOpenCodeUpdateNotifications: false,
  }
  try {
    await writeFile(join(directory, 'settings.json'), `${JSON.stringify(settings, null, 2)}\n`, {
      flag: 'wx',
      mode: 0o600,
    })
  } catch (error) {
    if (error.code !== 'EEXIST') throw error
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await initializeSettings(
    process.env.OPENCHAMBER_DATA_DIR,
    process.env.WIKI_NAME ?? 'Karpathy Wiki',
  )
}
