import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { initializeSettings } from '../openchamber/bootstrap.mjs'

test('new UI selects the wiki, protects settings, and preserves later customization', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'kw-chat-'))
  try {
    await initializeSettings(directory, 'My Wiki')
    const file = join(directory, 'settings.json')
    const settings = JSON.parse(await readFile(file, 'utf8'))
    assert.equal(settings.projects[0].path, '/knowledge/wiki')
    assert.equal(settings.projects[0].label, 'My Wiki')
    assert.equal(settings.activeProjectId, 'wiki')
    assert.equal((await stat(file)).mode & 0o777, 0o600)
    await writeFile(file, '{"custom":true}')
    await initializeSettings(directory, 'Different Wiki')
    assert.equal(await readFile(file, 'utf8'), '{"custom":true}')
  } finally {
    await rm(directory, { recursive: true })
  }
})
