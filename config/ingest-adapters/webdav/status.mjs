import { createHash } from "node:crypto"
import { open, readFile } from "node:fs/promises"
import path from "node:path"

import { emptyResult, field, files, frontmatter, revisionField } from "../shared.mjs"

const hashCache = new Map()

function validateSourcePath(sourcePath) {
  const segments = sourcePath.split("/")
  if (!sourcePath || sourcePath.startsWith("/") || sourcePath.includes("\\") || /[\u0000-\u001f\u007f]/.test(sourcePath) || segments.some((part) => !part || part === "." || part === "..")) {
    throw new Error("source_path ist kein normalisierter relativer Pfad")
  }
}

function metadata(text) {
  const lines = frontmatter(text)
  if (field(lines, "source_adapter") !== "webdav") throw new Error("source_adapter ist nicht webdav")
  const sourcePath = field(lines, "source_path")
  validateSourcePath(sourcePath)
  return { sourcePath, revision: revisionField(lines) }
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

export default async function scanWebdav({ sourceRoot, wikiSourceRoot }) {
  const result = emptyResult()
  const wikiAdapterRoot = path.join(wikiSourceRoot, "webdav")
  const sourceByPath = new Map()
  const wikiByPath = new Map()

  for (const sourceFile of await files(sourceRoot, { required: true })) {
    const sourcePath = path.relative(sourceRoot, sourceFile).split(path.sep).join("/")
    try {
      validateSourcePath(sourcePath)
      const sourceRevision = await hashFile(sourceFile)
      sourceByPath.set(sourcePath, {
        source_key: sourcePath,
        source_path: sourceFile,
        source_relative_path: sourcePath,
        source_revision: sourceRevision,
        wiki_path: path.join(wikiAdapterRoot, ...sourcePath.split("/"), "index.md"),
        frontmatter: {
          source_adapter: "webdav",
          source_path: sourcePath,
          source_revision: sourceRevision,
        },
      })
    } catch (error) {
      result.invalid.push({ path: sourceFile, error: error.message })
    }
  }

  for (const wikiPath of await files(wikiAdapterRoot, { markdownOnly: true })) {
    try {
      const item = metadata(await readFile(wikiPath, "utf8"))
      const pages = wikiByPath.get(item.sourcePath) ?? []
      pages.push({ wiki_path: wikiPath, source_revision: item.revision })
      wikiByPath.set(item.sourcePath, pages)
      const expected = path.join(wikiAdapterRoot, ...item.sourcePath.split("/"), "index.md")
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

  return result
}
