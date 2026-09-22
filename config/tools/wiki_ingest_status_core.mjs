import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'

// Generic ingest status scanner. It consumes only the versioned provider
// manifest (contracts/provider-manifest/v1/contract.json) written by every
// ingest cycle into its sanitized source root, plus the wiki source pages.
// It contains no provider-specific code: adding a provider never requires
// changing this tool, only writing a manifest.

export const RESULT_NAMES = [
  'new',
  'outdated',
  'current',
  'conflict',
  'revoked',
  'orphaned',
  'invalid',
]

// Contract: contracts/ingest-status/v1/contract.json (revisions.pattern)
export const REVISION_RE = /^[0-9a-f]{64}$/

// Contract: contracts/provider-manifest/v1/contract.json
const MANIFEST_CONTRACT = 'karpathy-wiki-provider-manifest'
export const MANIFEST_VERSION = 1
export const MANIFEST_FILENAME = 'manifest.json'

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

export function checkRelativePath(value, label = 'Pfad') {
  if (
    typeof value !== 'string' ||
    !value ||
    value.startsWith('/') ||
    value.includes('\\') ||
    // eslint-disable-next-line no-control-regex -- control characters are rejected on purpose
    /[\u0000-\u001f\u007f]/.test(value) ||
    value.split('/').some((part) => !part || part === '.' || part === '..')
  ) {
    throw new Error(`${label} ist kein normalisierter relativer Pfad`)
  }
  return value
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

function frontmatterLines(text) {
  const lines = text.replaceAll('\r\n', '\n').split('\n')
  if (lines[0] !== '---') throw new Error('fehlendes Frontmatter')
  const end = lines.indexOf('---', 1)
  if (end === -1) throw new Error('nicht abgeschlossenes Frontmatter')
  return lines.slice(1, end)
}

// Contract: contracts/ingest-status/v1/contract.json (frontmatterFields).
// Mirrors karpathy_wiki_ingest.contract.parse_frontmatter_fields.
export function parseFrontmatterFields(text) {
  const fields = {}
  const pattern = /^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$/
  for (const line of frontmatterLines(text)) {
    const match = line.match(pattern)
    if (!match) throw new Error('ungültige Frontmatter-Zeile')
    const name = match[1]
    if (name in fields) throw new Error(`${name} ist mehrfach vorhanden`)
    const value = match[2]
    if (value.startsWith('"')) {
      try {
        fields[name] = JSON.parse(value)
      } catch {
        throw new Error(`${name} enthält keine gültige Zeichenkette`)
      }
    } else if (value.length >= 2 && value.startsWith("'") && value.endsWith("'")) {
      fields[name] = value.slice(1, -1)
    } else {
      fields[name] = value
    }
  }
  return fields
}

function checkClaim(claim, owner) {
  if (
    !claim ||
    typeof claim !== 'object' ||
    Array.isArray(claim) ||
    Object.keys(claim).length === 0
  ) {
    throw new Error(`${owner}: claim muss ein nicht-leeres Objekt sein`)
  }
  for (const [name, value] of Object.entries(claim)) {
    if (!name || typeof value !== 'string') {
      throw new Error(`${owner}: claim muss nicht-leere Namen auf Zeichenketten abbilden`)
    }
  }
  return claim
}

function claimKey(claim) {
  return JSON.stringify(
    [...Object.entries(claim)].sort(([left], [right]) => (left < right ? -1 : 1)),
  )
}

export function parseManifest(sourceName, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('Manifest ist kein Objekt')
  if (value.contract !== MANIFEST_CONTRACT)
    throw new Error('Manifest gehört nicht zum Provider-Manifest-Vertrag')
  if (value.version !== MANIFEST_VERSION)
    throw new Error(`nicht unterstützte Manifest-Version: ${JSON.stringify(value.version ?? null)}`)
  if (typeof value.source !== 'string' || !/^[a-z0-9_-]+$/.test(value.source))
    throw new Error('source ist ungültig')
  if (value.source !== sourceName)
    throw new Error('source stimmt nicht mit dem Quellverzeichnis überein')
  if (!Number.isInteger(value.generated_at) || value.generated_at <= 0)
    throw new Error('generated_at ist ungültig')
  if (!Array.isArray(value.items)) throw new Error('items ist keine Liste')

  const seenKeys = new Set()
  const seenClaims = new Set()
  const items = value.items.map((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item))
      throw new Error('item ist kein Objekt')
    const source_key = item.source_key
    if (typeof source_key !== 'string' || !source_key)
      throw new Error('source_key ist kein nicht-leerer Text')
    if (seenKeys.has(source_key))
      throw new Error(`source_key ist mehrfach vorhanden: ${source_key}`)
    seenKeys.add(source_key)
    const source_path = checkRelativePath(item.source_path, `${source_key}: source_path`)
    const wiki_path = checkRelativePath(item.wiki_path, `${source_key}: wiki_path`)
    if (typeof item.source_revision !== 'string' || !REVISION_RE.test(item.source_revision))
      throw new Error(`${source_key}: source_revision ist ungültig`)
    const frontmatter = item.frontmatter
    if (!frontmatter || typeof frontmatter !== 'object' || Array.isArray(frontmatter))
      throw new Error(`${source_key}: frontmatter fehlt`)
    validateJsonValue(frontmatter)
    if (frontmatter.source_revision !== item.source_revision)
      throw new Error(`${source_key}: Frontmatter-Revision weicht ab`)
    const claim = checkClaim(item.claim, source_key)
    const identifier = claimKey(claim)
    if (seenClaims.has(identifier))
      throw new Error(`${source_key}: Identität ist mehrfach beansprucht`)
    seenClaims.add(identifier)
    return {
      source_key,
      source_path,
      wiki_path,
      source_revision: item.source_revision,
      frontmatter,
      claim,
    }
  })

  if (!Array.isArray(value.revoked ?? [])) throw new Error('revoked ist keine Liste')
  const revoked = (value.revoked ?? []).map((entry) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry))
      throw new Error('revoked entry ist kein Objekt')
    const source_key = entry.source_key
    if (typeof source_key !== 'string' || !source_key)
      throw new Error('revoked source_key ist kein nicht-leerer Text')
    if (seenKeys.has(source_key))
      throw new Error(`revoked source_key ist auch als item vorhanden: ${source_key}`)
    const claim = checkClaim(entry.claim, source_key)
    const identifier = claimKey(claim)
    if (seenClaims.has(identifier))
      throw new Error(`${source_key}: Identität ist mehrfach beansprucht`)
    seenClaims.add(identifier)
    return { source_key, claim }
  })

  if (!Array.isArray(value.errors ?? [])) throw new Error('errors ist keine Liste')
  const errors = (value.errors ?? []).map((entry) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry))
      throw new Error('error entry ist kein Objekt')
    if (typeof entry.error !== 'string' || !entry.error)
      throw new Error('error entry trägt keinen Fehlertext')
    const normalized = { error: entry.error }
    if (entry.source_key !== undefined) {
      if (typeof entry.source_key !== 'string' || !entry.source_key)
        throw new Error('error source_key ist kein nicht-leerer Text')
      normalized.source_key = entry.source_key
    }
    if (entry.path !== undefined) normalized.path = checkRelativePath(entry.path, 'error path')
    return normalized
  })

  const wiki_root = value.wiki_root === undefined ? '.' : value.wiki_root
  if (wiki_root !== '.') checkRelativePath(wiki_root, 'wiki_root')

  return {
    source: value.source,
    generated_at: value.generated_at,
    wiki_root,
    items,
    revoked,
    errors,
  }
}

async function collectPages(root) {
  const pages = []
  async function visit(directory) {
    let entries
    try {
      entries = await readdir(directory, { withFileTypes: true })
    } catch (error) {
      if (error?.code === 'ENOENT') return
      throw error
    }
    for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
      const entryPath = path.join(directory, entry.name)
      if (entry.isDirectory()) await visit(entryPath)
      else if (entry.isFile() && entry.name.endsWith('.md')) pages.push(entryPath)
    }
  }
  await visit(root)
  return pages
}

function emptyResult() {
  return {
    new: [],
    outdated: [],
    current: [],
    conflict: [],
    revoked: [],
    orphaned: [],
    invalid: [],
  }
}

function finishAdapters(adapters, includeCurrent) {
  const targets = new Map()
  for (const [sourceName, result] of Object.entries(adapters)) {
    for (const status of ['new', 'outdated', 'current']) {
      for (const item of result[status]) {
        const entries = targets.get(item.wiki_path) ?? []
        entries.push({ sourceName, status, item })
        targets.set(item.wiki_path, entries)
      }
    }
  }
  for (const [wikiPath, entries] of targets) {
    if (entries.length < 2) continue
    for (const { sourceName, status, item } of entries) {
      const result = adapters[sourceName]
      result[status] = result[status].filter((candidate) => candidate !== item)
      result.conflict.push({
        source_key: item.source_key,
        source_path: item.source_path,
        wiki_path: wikiPath,
        error: 'wiki_path wird von mehreren Quellen beansprucht',
      })
    }
  }
  for (const result of Object.values(adapters)) {
    result.summary = Object.fromEntries(RESULT_NAMES.map((name) => [name, result[name].length]))
    if (!includeCurrent) result.current = []
  }
}

export async function scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent = false }) {
  const entries = await readdir(sourceRoot, { withFileTypes: true })
  const sourceDirectories = entries
    .filter((entry) => entry.isDirectory())
    .sort((left, right) => left.name.localeCompare(right.name))

  const adapters = {}
  for (const entry of sourceDirectories) {
    const sourceDirectoryRoot = path.join(sourceRoot, entry.name)
    try {
      const raw = JSON.parse(
        await readFile(path.join(sourceDirectoryRoot, MANIFEST_FILENAME), 'utf8'),
      )
      adapters[entry.name] = {
        sourceRoot: sourceDirectoryRoot,
        manifest: parseManifest(entry.name, raw),
        state: { pending: new Map(), revokedPages: new Map(), invalid: [], orphaned: [] },
      }
    } catch (error) {
      adapters[entry.name] = { sourceRoot: sourceDirectoryRoot, failure: errorMessage(error) }
    }
  }

  const owned = Object.entries(adapters).filter(([, adapter]) => adapter.manifest)
  for (const pagePath of await collectPages(wikiSourceRoot)) {
    const relative = path.relative(wikiSourceRoot, pagePath)
    let owner = null
    let bestLength = -1
    for (const [, adapter] of owned) {
      const root = adapter.manifest.wiki_root
      const matched = root === '.' || relative === root || relative.startsWith(`${root}/`)
      const length = root === '.' ? 0 : root.length
      if (matched && length > bestLength) {
        owner = adapter
        bestLength = length
      }
    }
    if (!owner) continue

    let fields
    try {
      fields = parseFrontmatterFields(await readFile(pagePath, 'utf8'))
    } catch (error) {
      owner.state.invalid.push({ path: pagePath, error: errorMessage(error) })
      continue
    }
    const matches = []
    for (const item of owner.manifest.items) matches.push({ kind: 'item', item, claim: item.claim })
    for (const entry of owner.manifest.revoked)
      matches.push({ kind: 'revoked', entry, claim: entry.claim })
    const claiming = matches
      .filter(({ claim }) =>
        Object.entries(claim).every(([name, value]) => String(fields[name]) === value),
      )
      .sort((left, right) => Object.keys(right.claim).length - Object.keys(left.claim).length)

    if (claiming.length === 0) {
      const revision = fields.source_revision
      if (typeof revision !== 'string' || !REVISION_RE.test(revision)) {
        owner.state.invalid.push({ path: pagePath, error: 'source_revision ist ungültig' })
      } else {
        owner.state.orphaned.push({ wiki_path: pagePath, wiki_revision: revision })
      }
      continue
    }
    const match = claiming[0]
    if (match.kind === 'revoked') {
      const pages = owner.state.revokedPages.get(match.entry.source_key) ?? []
      pages.push(pagePath)
      owner.state.revokedPages.set(match.entry.source_key, pages)
      continue
    }
    const item = match.item
    if (typeof fields.source_revision !== 'string' || !REVISION_RE.test(fields.source_revision)) {
      owner.state.invalid.push({ path: pagePath, error: 'source_revision ist ungültig' })
      continue
    }
    const pending = owner.state.pending.get(item.source_key) ?? []
    pending.push({ pagePath, source_revision: fields.source_revision })
    owner.state.pending.set(item.source_key, pending)
  }

  const results = {}
  for (const [sourceName, adapter] of Object.entries(adapters)) {
    const result = emptyResult()
    if (adapter.failure) {
      result.invalid.push({ path: adapter.sourceRoot, error: adapter.failure })
      results[sourceName] = result
      continue
    }
    result.invalid.push(...adapter.state.invalid)
    for (const item of adapter.manifest.items) {
      const source_path = path.join(adapter.sourceRoot, item.source_path)
      const wiki_path = path.join(wikiSourceRoot, item.wiki_path)
      const base = {
        source_key: item.source_key,
        source_path,
        source_revision: item.source_revision,
        wiki_path,
        frontmatter: item.frontmatter,
      }
      const pages = adapter.state.pending.get(item.source_key) ?? []
      for (const page of pages) {
        if (page.pagePath !== wiki_path)
          result.invalid.push({ path: page.pagePath, error: `erwarteter Pfad: ${wiki_path}` })
      }
      if (pages.length > 1) {
        result.conflict.push({
          source_key: item.source_key,
          source_path,
          wiki_paths: pages.map((page) => page.pagePath),
        })
      } else if (pages.length === 1 && pages[0].pagePath === wiki_path) {
        const page = pages[0]
        if (page.source_revision === item.source_revision) result.current.push(base)
        else result.outdated.push({ ...base, wiki_revision: page.source_revision })
      } else {
        result.new.push(base)
      }
    }
    for (const entry of adapter.manifest.revoked) {
      result.revoked.push({
        source_key: entry.source_key,
        source_path: null,
        wiki_paths: adapter.state.revokedPages.get(entry.source_key) ?? [],
      })
    }
    for (const orphaned of adapter.state.orphaned) result.orphaned.push(orphaned)
    for (const error of adapter.manifest.errors) {
      result.invalid.push({
        path: error.path ? path.join(adapter.sourceRoot, error.path) : adapter.sourceRoot,
        error: error.error,
      })
    }
    results[sourceName] = result
  }

  finishAdapters(results, includeCurrent)
  const summary = Object.fromEntries(
    RESULT_NAMES.map((name) => [
      name,
      Object.values(results).reduce((total, result) => total + result.summary[name], 0),
    ]),
  )
  return { summary, adapters: results }
}
