import { readdir } from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { REVISION_RE } from '../ingest-adapters/shared.mjs'

export const RESULT_NAMES = [
  'new',
  'outdated',
  'current',
  'conflict',
  'revoked',
  'orphaned',
  'invalid',
]

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

function failedAdapter(sourceRoot, error) {
  return {
    new: [],
    outdated: [],
    current: [],
    conflict: [],
    revoked: [],
    orphaned: [],
    invalid: [{ path: sourceRoot, error: errorMessage(error) }],
  }
}

function inside(root, candidate) {
  if (!path.isAbsolute(candidate) || path.normalize(candidate) !== candidate) return false
  const relative = path.relative(root, candidate)
  return (
    relative !== '' &&
    !relative.startsWith(`..${path.sep}`) &&
    relative !== '..' &&
    !path.isAbsolute(relative)
  )
}

function validateJsonValue(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number' && Number.isFinite(value)) return
  if (typeof value !== 'object') throw new Error('enthält keinen JSON-kompatiblen Wert')
  if (seen.has(value)) throw new Error('enthält eine zyklische Referenz')
  const prototype = Object.getPrototypeOf(value)
  if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null) {
    throw new Error('enthält ein nicht unterstütztes Objekt')
  }
  seen.add(value)
  for (const entry of Array.isArray(value) ? value : Object.values(value))
    validateJsonValue(entry, seen)
  seen.delete(value)
}

function validatePendingItems(result, sourceRoot, wikiSourceRoot) {
  const keys = new Set()
  for (const status of ['new', 'outdated', 'current']) {
    for (const item of result[status]) {
      if (!item || typeof item !== 'object')
        throw new Error(`${status} enthält keinen gültigen Eintrag`)
      if (typeof item.source_key !== 'string' || !item.source_key)
        throw new Error(`${status} enthält keinen source_key`)
      if (keys.has(item.source_key))
        throw new Error(`source_key ist mehrfach vorhanden: ${item.source_key}`)
      keys.add(item.source_key)
      if (typeof item.source_path !== 'string' || !inside(sourceRoot, item.source_path)) {
        throw new Error(`${item.source_key}: source_path liegt außerhalb des Adapters`)
      }
      if (typeof item.wiki_path !== 'string' || !inside(wikiSourceRoot, item.wiki_path)) {
        throw new Error(`${item.source_key}: wiki_path liegt außerhalb der Wiki-Quellen`)
      }
      if (!REVISION_RE.test(item.source_revision))
        throw new Error(`${item.source_key}: source_revision ist ungültig`)
      if (
        !item.frontmatter ||
        typeof item.frontmatter !== 'object' ||
        Array.isArray(item.frontmatter)
      ) {
        throw new Error(`${item.source_key}: frontmatter fehlt`)
      }
      try {
        validateJsonValue(item.frontmatter)
      } catch (error) {
        throw new Error(`${item.source_key}: frontmatter ${errorMessage(error)}`, { cause: error })
      }
      if (item.frontmatter.source_revision !== item.source_revision) {
        throw new Error(`${item.source_key}: Frontmatter-Revision weicht ab`)
      }
    }
  }
}

function finalize(result, sourceRoot, wikiSourceRoot) {
  if (!result || typeof result !== 'object') throw new Error('Adapter lieferte kein Statusergebnis')
  try {
    validateJsonValue(result)
  } catch (error) {
    throw new Error(`Adapterergebnis ${errorMessage(error)}`, { cause: error })
  }
  for (const key of Object.keys(result)) {
    if (!RESULT_NAMES.includes(key)) throw new Error(`unbekanntes Adapterfeld ${key}`)
  }
  const normalized = {}
  for (const name of RESULT_NAMES) {
    const entries = result[name]
    if (!Array.isArray(entries)) throw new Error(`Adapterfeld ${name} ist keine Liste`)
    normalized[name] = entries
  }
  validatePendingItems(normalized, sourceRoot, wikiSourceRoot)
  return normalized
}

function finishAdapters(adapters, includeCurrent) {
  const targets = new Map()
  for (const [adapterName, adapter] of Object.entries(adapters)) {
    for (const status of ['new', 'outdated', 'current']) {
      for (const item of adapter[status]) {
        const entries = targets.get(item.wiki_path) ?? []
        entries.push({ adapterName, status, item })
        targets.set(item.wiki_path, entries)
      }
    }
  }
  for (const [wikiPath, entries] of targets) {
    if (entries.length < 2) continue
    for (const { adapterName, status, item } of entries) {
      const adapter = adapters[adapterName]
      adapter[status] = adapter[status].filter((candidate) => candidate !== item)
      adapter.conflict.push({
        source_key: item.source_key,
        source_path: item.source_path,
        wiki_path: wikiPath,
        error: 'wiki_path wird von mehreren Quellen beansprucht',
      })
    }
  }
  for (const adapter of Object.values(adapters)) {
    adapter.summary = Object.fromEntries(RESULT_NAMES.map((name) => [name, adapter[name].length]))
    if (!includeCurrent) adapter.current = []
  }
}

export async function scanIngestStatus({
  sourceRoot,
  wikiSourceRoot,
  adapterRoot,
  includeCurrent = false,
}) {
  const entries = await readdir(sourceRoot, { withFileTypes: true })
  const sourceDirectories = entries
    .filter((entry) => entry.isDirectory())
    .sort((left, right) => left.name.localeCompare(right.name))
  const adapterEntries = []

  for (const entry of sourceDirectories) {
    const adapterSourceRoot = path.join(sourceRoot, entry.name)
    try {
      const modulePath = path.join(adapterRoot, entry.name, 'status.mjs')
      const adapter = await import(pathToFileURL(modulePath).href)
      if (typeof adapter.default !== 'function')
        throw new Error('Adapter exportiert keine Statusfunktion')
      const result = finalize(
        await adapter.default({
          sourceRoot: adapterSourceRoot,
          wikiSourceRoot,
        }),
        adapterSourceRoot,
        wikiSourceRoot,
      )
      adapterEntries.push([entry.name, result])
    } catch (error) {
      adapterEntries.push([
        entry.name,
        finalize(failedAdapter(adapterSourceRoot, error), adapterSourceRoot, wikiSourceRoot),
      ])
    }
  }

  const adapters = Object.fromEntries(adapterEntries)
  finishAdapters(adapters, includeCurrent)
  const summary = Object.fromEntries(
    RESULT_NAMES.map((name) => [
      name,
      Object.values(adapters).reduce((total, adapter) => total + adapter.summary[name], 0),
    ]),
  )
  return { summary, adapters }
}
