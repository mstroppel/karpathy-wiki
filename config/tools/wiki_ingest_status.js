import { tool } from "@opencode-ai/plugin"
import { readdir, readFile } from "node:fs/promises"
import path from "node:path"

const SOURCE_ROOT = "/knowledge/sources/paperless"
const WIKI_SOURCE_ROOT = "/knowledge/wiki/sources"
const REVISION_RE = /^[0-9a-f]{64}$/
const PAPERLESS_RANGE_RE = /^\d+-\d+$/

function paperlessEnabled() {
  return (process.env.PAPERLESS_ENABLED ?? "false") === "true"
}

async function markdownFiles(root, required = false) {
  const files = []

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
      else if (entry.isFile() && entry.name.endsWith(".md")) files.push(entryPath)
    }
  }

  await visit(root)
  return files
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
  return matches[0][1].replace(/^(?:"(.*)"|'(.*)')$/, (_match, double, single) => double ?? single)
}

function metadata(text, source) {
  const lines = frontmatter(text)
  const idText = field(lines, "paperless_id")
  if (!/^\d+$/.test(idText)) throw new Error("paperless_id ist nicht numerisch")
  const revision = field(lines, "source_revision")
  if (!REVISION_RE.test(revision)) throw new Error("source_revision ist ungültig")
  if (source) {
    if (field(lines, "anonymized") !== "true") throw new Error("anonymized ist nicht true")
    if (!field(lines, "paperless_url").startsWith("https://")) {
      throw new Error("paperless_url verwendet nicht HTTPS")
    }
  }
  return { id: Number(idText), revision }
}

function rangeFor(id) {
  const start = Math.floor(id / 1000) * 1000
  return `${String(start).padStart(4, "0")}-${String(start + 999).padStart(4, "0")}`
}

async function revokedIds() {
  const revokedPath = path.join(SOURCE_ROOT, "revoked.md")
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

export default tool({
  description: "Vergleicht optional aktivierte anonymisierte Paperless-Quellen mit Wiki-Quellenseiten und meldet ihren Revisionsstatus.",
  args: {
    include_current: tool.schema.boolean().optional().describe("Auch bereits aktuelle Quellen ausgeben"),
  },
  async execute(args) {
    if (!paperlessEnabled()) {
      return JSON.stringify({
        enabled: false,
        summary: { new: 0, outdated: 0, current: 0, conflict: 0, revoked: 0, orphaned: 0, invalid: 0 },
        new: [], outdated: [], current: [], conflict: [], revoked: [], orphaned: [], invalid: [],
      }, null, 2)
    }

    const revoked = await revokedIds()
    const invalid = []
    const sourceById = new Map()
    const wikiById = new Map()

    for (const sourcePath of await markdownFiles(SOURCE_ROOT, true)) {
      if (sourcePath === path.join(SOURCE_ROOT, "revoked.md")) continue
      try {
        const item = metadata(await readFile(sourcePath, "utf8"), true)
        const expected = path.join(SOURCE_ROOT, rangeFor(item.id), `document-${item.id}.md`)
        if (sourcePath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
        if (sourceById.has(item.id)) throw new Error("paperless_id ist in mehreren Quellen vorhanden")
        sourceById.set(item.id, { id: item.id, source_revision: item.revision, source_path: sourcePath })
      } catch (error) {
        invalid.push({ path: sourcePath, error: error.message })
      }
    }

    for (const wikiPath of await markdownFiles(WIKI_SOURCE_ROOT)) {
      const [sourceDirectory] = path.relative(WIKI_SOURCE_ROOT, wikiPath).split(path.sep)
      if (!PAPERLESS_RANGE_RE.test(sourceDirectory)) continue
      try {
        const item = metadata(await readFile(wikiPath, "utf8"), false)
        const pages = wikiById.get(item.id) ?? []
        pages.push({ wiki_path: wikiPath, source_revision: item.revision })
        wikiById.set(item.id, pages)
        const expected = path.join(WIKI_SOURCE_ROOT, rangeFor(item.id), `paperless-${item.id}.md`)
        if (wikiPath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
      } catch (error) {
        invalid.push({ path: wikiPath, error: error.message })
      }
    }

    const result = { new: [], outdated: [], current: [], conflict: [], revoked: [], orphaned: [], invalid }
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

    return JSON.stringify({
      enabled: true,
      summary: Object.fromEntries(Object.entries(result).map(([name, entries]) => [name, entries.length])),
      ...result,
      current: args.include_current ? result.current : [],
    }, null, 2)
  },
})
