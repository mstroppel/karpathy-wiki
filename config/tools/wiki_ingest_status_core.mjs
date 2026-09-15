import { createHash } from "node:crypto"
import { open, readdir, readFile } from "node:fs/promises"
import path from "node:path"

const REVISION_RE = /^[0-9a-f]{64}$/
const PAPERLESS_RANGE_RE = /^\d+-\d+$/
const RESULT_NAMES = ["new", "outdated", "current", "conflict", "revoked", "orphaned", "invalid"]
const hashCache = new Map()

function emptyResult(enabled = true) {
  return Object.fromEntries([
    ["enabled", enabled],
    ["summary", Object.fromEntries(RESULT_NAMES.map((name) => [name, 0]))],
    ...RESULT_NAMES.map((name) => [name, []]),
  ])
}

async function files(root, { markdownOnly = false, required = false } = {}) {
  const result = []

  async function visit(directory) {
    let entries
    try {
      entries = await readdir(directory, { withFileTypes: true })
    } catch (error) {
      if (error?.code === "ENOENT" && (!required || directory !== root)) return
      throw error
    }
    for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
      const entryPath = path.join(directory, entry.name)
      if (entry.isDirectory()) await visit(entryPath)
      else if (entry.isFile() && (!markdownOnly || entry.name.endsWith(".md"))) result.push(entryPath)
    }
  }

  await visit(root)
  return result
}

function frontmatter(text) {
  const lines = text.replaceAll("\r\n", "\n").split("\n")
  if (lines[0] !== "---") throw new Error("fehlendes Frontmatter")
  const end = lines.indexOf("---", 1)
  if (end === -1) throw new Error("nicht abgeschlossenes Frontmatter")
  return lines.slice(1, end)
}

function field(lines, name) {
  const matches = lines
    .map((line) => line.match(new RegExp(`^${name}:\\s*(.*?)\\s*$`)))
    .filter(Boolean)
  if (matches.length !== 1) throw new Error(`${name} fehlt oder ist mehrfach vorhanden`)
  const value = matches[0][1]
  if (value.startsWith('"')) {
    try {
      return JSON.parse(value)
    } catch {
      throw new Error(`${name} enthält keine gültige Zeichenkette`)
    }
  }
  return value.replace(/^'(.*)'$/, "$1")
}

function revisionField(lines) {
  const revision = field(lines, "source_revision")
  if (!REVISION_RE.test(revision)) throw new Error("source_revision ist ungültig")
  return revision
}

function paperlessMetadata(text, source) {
  const lines = frontmatter(text)
  const idText = field(lines, "paperless_id")
  if (!/^\d+$/.test(idText)) throw new Error("paperless_id ist nicht numerisch")
  const revision = revisionField(lines)
  if (source) {
    if (field(lines, "anonymized") !== "true") throw new Error("anonymized ist nicht true")
    if (!field(lines, "paperless_url").startsWith("https://")) {
      throw new Error("paperless_url verwendet nicht HTTPS")
    }
  }
  return { id: Number(idText), revision }
}

function validateWebdavSourcePath(sourcePath) {
  const segments = sourcePath.split("/")
  if (!sourcePath || sourcePath.startsWith("/") || sourcePath.includes("\\") || /[\u0000-\u001f\u007f]/.test(sourcePath) || segments.some((part) => !part || part === "." || part === "..")) {
    throw new Error("source_path ist kein normalisierter relativer Pfad")
  }
}

function webdavMetadata(text) {
  const lines = frontmatter(text)
  if (field(lines, "source_adapter") !== "webdav") throw new Error("source_adapter ist nicht webdav")
  const sourcePath = field(lines, "source_path")
  validateWebdavSourcePath(sourcePath)
  return { sourcePath, revision: revisionField(lines) }
}

function rangeFor(id) {
  const start = Math.floor(id / 1000) * 1000
  return `${String(start).padStart(4, "0")}-${String(start + 999).padStart(4, "0")}`
}

async function revokedIds(sourceRoot) {
  const revokedPath = path.join(sourceRoot, "revoked.md")
  let text
  try {
    text = await readFile(revokedPath, "utf8")
  } catch (error) {
    if (error?.code === "ENOENT") return new Set()
    throw error
  }
  const lines = text.replaceAll("\r\n", "\n").split("\n")
  if (lines[0] !== "# Widerrufene Paperless-Dokumente") {
    throw new Error(`Ungültige Widerrufsliste: ${revokedPath}`)
  }
  const ids = new Set()
  for (const line of lines.slice(1)) {
    if (!line) continue
    const match = line.match(/^- (\d+)$/)
    if (!match) throw new Error(`Ungültige Widerrufsliste: ${revokedPath}`)
    ids.add(Number(match[1]))
  }
  return ids
}

async function hashFile(filePath) {
  const handle = await open(filePath, "r")
  try {
    const before = await handle.stat({ bigint: true })
    const fingerprint = ["dev", "ino", "size", "mtimeNs", "ctimeNs"].map((field) => before[field]).join(":")
    const cached = hashCache.get(filePath)
    if (cached?.fingerprint === fingerprint) return cached.revision
    const hash = createHash("sha256")
    for await (const chunk of handle.createReadStream({ autoClose: false })) hash.update(chunk)
    const after = await handle.stat({ bigint: true })
    for (const field of ["dev", "ino", "size", "mtimeNs", "ctimeNs"]) {
      if (before[field] !== after[field]) throw new Error("Datei wurde während der Prüfsummenbildung geändert")
    }
    const revision = hash.digest("hex")
    hashCache.set(filePath, { fingerprint, revision })
    return revision
  } finally {
    await handle.close()
  }
}

function finalize(result, includeCurrent) {
  result.summary = Object.fromEntries(RESULT_NAMES.map((name) => [name, result[name].length]))
  if (!includeCurrent) result.current = []
  return result
}

export async function scanWebdav(sourceRoot, wikiSourceRoot, includeCurrent = false) {
  const result = emptyResult()
  const sourceByPath = new Map()
  const wikiByPath = new Map()

  for (const sourceFile of await files(sourceRoot, { required: true })) {
    const sourcePath = path.relative(sourceRoot, sourceFile).split(path.sep).join("/")
    try {
      validateWebdavSourcePath(sourcePath)
      sourceByPath.set(sourcePath, {
        source_path: sourceFile,
        source_relative_path: sourcePath,
        source_revision: await hashFile(sourceFile),
      })
    } catch (error) {
      result.invalid.push({ path: sourceFile, error: error.message })
    }
  }

  for (const wikiPath of await files(wikiSourceRoot, { markdownOnly: true })) {
    try {
      const item = webdavMetadata(await readFile(wikiPath, "utf8"))
      const pages = wikiByPath.get(item.sourcePath) ?? []
      pages.push({ wiki_path: wikiPath, source_revision: item.revision })
      wikiByPath.set(item.sourcePath, pages)
      const expected = path.join(wikiSourceRoot, ...item.sourcePath.split("/"), "index.md")
      if (wikiPath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
    } catch (error) {
      result.invalid.push({ path: wikiPath, error: error.message })
    }
  }

  const allPaths = [...new Set([...sourceByPath.keys(), ...wikiByPath.keys()])].sort()
  for (const sourcePath of allPaths) {
    const source = sourceByPath.get(sourcePath)
    const pages = wikiByPath.get(sourcePath) ?? []
    if (pages.length > 1) {
      result.conflict.push({ source_relative_path: sourcePath, source_path: source?.source_path ?? null, wiki_paths: pages.map((page) => page.wiki_path) })
    } else if (!source) {
      result.orphaned.push({ source_relative_path: sourcePath, wiki_path: pages[0].wiki_path, wiki_revision: pages[0].source_revision })
    } else if (pages.length === 0) {
      result.new.push(source)
    } else if (pages[0].source_revision !== source.source_revision) {
      result.outdated.push({ ...source, wiki_path: pages[0].wiki_path, wiki_revision: pages[0].source_revision })
    } else {
      result.current.push({ ...source, wiki_path: pages[0].wiki_path })
    }
  }

  return finalize(result, includeCurrent)
}

export async function scanPaperless(sourceRoot, wikiSourceRoot, enabled, includeCurrent = false) {
  if (!enabled) return emptyResult(false)

  const result = emptyResult()
  const revoked = await revokedIds(sourceRoot)
  const sourceById = new Map()
  const wikiById = new Map()

  for (const sourcePath of await files(sourceRoot, { markdownOnly: true, required: true })) {
    if (sourcePath === path.join(sourceRoot, "revoked.md")) continue
    try {
      const item = paperlessMetadata(await readFile(sourcePath, "utf8"), true)
      const expected = path.join(sourceRoot, rangeFor(item.id), `document-${item.id}.md`)
      if (sourcePath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
      if (sourceById.has(item.id)) throw new Error("paperless_id ist in mehreren Quellen vorhanden")
      sourceById.set(item.id, { id: item.id, source_revision: item.revision, source_path: sourcePath })
    } catch (error) {
      result.invalid.push({ path: sourcePath, error: error.message })
    }
  }

  for (const wikiPath of await files(wikiSourceRoot, { markdownOnly: true })) {
    const [sourceDirectory] = path.relative(wikiSourceRoot, wikiPath).split(path.sep)
    if (!PAPERLESS_RANGE_RE.test(sourceDirectory)) continue
    try {
      const item = paperlessMetadata(await readFile(wikiPath, "utf8"), false)
      const pages = wikiById.get(item.id) ?? []
      pages.push({ wiki_path: wikiPath, source_revision: item.revision })
      wikiById.set(item.id, pages)
      const expected = path.join(wikiSourceRoot, rangeFor(item.id), `paperless-${item.id}.md`)
      if (wikiPath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
    } catch (error) {
      result.invalid.push({ path: wikiPath, error: error.message })
    }
  }

  const allIds = [...new Set([...sourceById.keys(), ...wikiById.keys(), ...revoked])].sort((a, b) => a - b)
  for (const id of allIds) {
    const source = sourceById.get(id)
    const pages = wikiById.get(id) ?? []
    if (revoked.has(id)) {
      result.revoked.push({ id, source_path: source?.source_path ?? null, wiki_paths: pages.map((page) => page.wiki_path) })
    } else if (pages.length > 1) {
      result.conflict.push({ id, source_path: source?.source_path ?? null, wiki_paths: pages.map((page) => page.wiki_path) })
    } else if (!source) {
      result.orphaned.push({ id, wiki_path: pages[0].wiki_path, wiki_revision: pages[0].source_revision })
    } else if (pages.length === 0) {
      result.new.push(source)
    } else if (pages[0].source_revision !== source.source_revision) {
      result.outdated.push({ ...source, wiki_path: pages[0].wiki_path, wiki_revision: pages[0].source_revision })
    } else {
      result.current.push({ ...source, wiki_path: pages[0].wiki_path })
    }
  }

  return finalize(result, includeCurrent)
}

export async function scanIngestStatus({ sourceRoot, wikiSourceRoot, paperlessEnabled, includeCurrent = false }) {
  const adapters = {
    webdav: await scanWebdav(path.join(sourceRoot, "webdav"), path.join(wikiSourceRoot, "webdav"), includeCurrent),
    paperless: await scanPaperless(path.join(sourceRoot, "paperless"), wikiSourceRoot, paperlessEnabled, includeCurrent),
  }
  for (const legacyPath of await files(path.join(wikiSourceRoot, "nextcloud"), { markdownOnly: true })) {
    adapters.webdav.invalid.push({
      path: legacyPath,
      error: "Legacy-Nextcloud-Quellenseite muss nach sources/webdav migriert und um Revisionsmetadaten ergänzt werden",
    })
  }
  const agentsPath = path.resolve(wikiSourceRoot, "..", "AGENTS.md")
  try {
    if ((await readFile(agentsPath, "utf8")).includes("sources/nextcloud")) {
      adapters.webdav.invalid.push({
        path: agentsPath,
        error: "Legacy-Nextcloud-Regeln müssen auf /knowledge/sources/webdav aktualisiert werden",
      })
    }
  } catch (error) {
    if (error?.code !== "ENOENT") throw error
  }
  adapters.webdav.summary.invalid = adapters.webdav.invalid.length
  const summary = Object.fromEntries(RESULT_NAMES.map((name) => [
    name,
    Object.values(adapters).reduce((total, adapter) => total + adapter.summary[name], 0),
  ]))
  return { summary, adapters }
}
