import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { confinedIngestPath } from '../config/tools/wiki_ingest_transaction_core.mjs'
import { withIngestLock, writeIngestFile } from '../config/tools/wiki_ingest_storage.mjs'

// Trusted-controller execution only; never ship this as a runtime tool. The
// container lifetime lock remains the authority for proving a real restart.
// This second lock spans awaited background work, not merely HTTP admission.
export function controllerFence({ stateRoot, bootId }) {
  if (!/^[0-9a-f]{32}$/.test(bootId ?? '')) throw new Error('invalid_boot_id')
  const lock = path.join(stateRoot, 'controller-execution.lock')
  let initialized = false
  const read = async (name) => {
    try {
      return JSON.parse(await readFile(await confinedIngestPath(stateRoot, name), 'utf8'))
    } catch (error) {
      if (error.code === 'ENOENT') return undefined
      throw new Error('invalid_controller_fence', { cause: error })
    }
  }
  const object = (value, keys) =>
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  const load = async () => {
    const marker = await read('controller-initialized.json')
    const state = await read('controller.json')
    if (
      (marker !== undefined && (!object(marker, ['version']) || marker.version !== 1)) ||
      (state !== undefined &&
        (!object(state, ['version', 'boot_id']) ||
          state.version !== 1 ||
          !/^[0-9a-f]{32}$/.test(state.boot_id ?? ''))) ||
      (state !== undefined && marker === undefined) ||
      (marker !== undefined && state === undefined)
    )
      throw new Error('invalid_controller_fence')
    return state
  }
  const assert = async () => {
    const state = await load()
    if (!state || state.boot_id !== bootId) throw new Error('stale_controller')
  }
  return {
    assert,
    initialize: (action) =>
      withIngestLock(lock, async () => {
        if (initialized) await assert()
        const state = await load()
        if (!state)
          await writeIngestFile(
            await confinedIngestPath(stateRoot, 'controller-initialized.json', true),
            '{"version":1}\n',
          )
        await writeIngestFile(
          await confinedIngestPath(stateRoot, 'controller.json', true),
          `${JSON.stringify({ version: 1, boot_id: bootId })}\n`,
        )
        const result = await action()
        initialized = true
        return result
      }),
    run: (action) =>
      withIngestLock(lock, async () => {
        await assert()
        return action()
      }),
  }
}
