import { readFile } from 'node:fs/promises'
import path from 'node:path'

import { emptyResult, field, files, frontmatter, revisionField } from '../shared.mjs'

const RANGE_RE = /^\d+-\d+$/

function metadata(text, source) {
  const lines = frontmatter(text)
  const idText = field(lines, 'paperless_id')
  if (!/^\d+$/.test(idText)) throw new Error('paperless_id ist nicht numerisch')
  const revision = revisionField(lines)
  let paperlessUrl
  if (source) {
    if (field(lines, 'anonymized') !== 'true') throw new Error('anonymized ist nicht true')
    paperlessUrl = field(lines, 'paperless_url')
    if (!paperlessUrl.startsWith('https://')) {
      throw new Error('paperless_url verwendet nicht HTTPS')
    }
  }
  return { id: Number(idText), revision, paperlessUrl }
}

function rangeFor(id) {
  const start = Math.floor(id / 1000) * 1000
  return `${String(start).padStart(4, '0')}-${String(start + 999).padStart(4, '0')}`
}

async function revokedIds(sourceRoot) {
  const revokedPath = path.join(sourceRoot, 'revoked.md')
  let text
  try {
    text = await readFile(revokedPath, 'utf8')
  } catch (error) {
    if (error?.code === 'ENOENT') return new Set()
    throw error
  }
  const lines = text.replaceAll('\r\n', '\n').split('\n')
  if (lines[0] !== '# Widerrufene Paperless-Dokumente') {
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

export default async function scanPaperless({ sourceRoot, wikiSourceRoot }) {
  const result = emptyResult()
  const revoked = await revokedIds(sourceRoot)
  const sourceById = new Map()
  const wikiById = new Map()

  for (const sourcePath of await files(sourceRoot, { markdownOnly: true, required: true })) {
    if (sourcePath === path.join(sourceRoot, 'revoked.md')) continue
    try {
      const item = metadata(await readFile(sourcePath, 'utf8'), true)
      const expected = path.join(sourceRoot, rangeFor(item.id), `document-${item.id}.md`)
      if (sourcePath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
      if (sourceById.has(item.id)) throw new Error('paperless_id ist in mehreren Quellen vorhanden')
      sourceById.set(item.id, {
        id: item.id,
        source_key: String(item.id),
        source_path: sourcePath,
        source_revision: item.revision,
        wiki_path: path.join(wikiSourceRoot, rangeFor(item.id), `paperless-${item.id}.md`),
        frontmatter: {
          paperless_id: item.id,
          paperless_url: item.paperlessUrl,
          source_revision: item.revision,
        },
      })
    } catch (error) {
      result.invalid.push({ path: sourcePath, error: error.message })
    }
  }

  for (const wikiPath of await files(wikiSourceRoot, { markdownOnly: true })) {
    const [sourceDirectory] = path.relative(wikiSourceRoot, wikiPath).split(path.sep)
    if (!RANGE_RE.test(sourceDirectory)) continue
    try {
      const item = metadata(await readFile(wikiPath, 'utf8'), false)
      const pages = wikiById.get(item.id) ?? []
      pages.push({ wiki_path: wikiPath, source_revision: item.revision })
      wikiById.set(item.id, pages)
      const expected = path.join(wikiSourceRoot, rangeFor(item.id), `paperless-${item.id}.md`)
      if (wikiPath !== expected) throw new Error(`erwarteter Pfad: ${expected}`)
    } catch (error) {
      result.invalid.push({ path: wikiPath, error: error.message })
    }
  }

  const allIds = [...new Set([...sourceById.keys(), ...wikiById.keys(), ...revoked])].sort(
    (a, b) => a - b,
  )
  for (const id of allIds) {
    const source = sourceById.get(id)
    const pages = wikiById.get(id) ?? []
    if (revoked.has(id)) {
      result.revoked.push({
        id,
        source_path: source?.source_path ?? null,
        wiki_paths: pages.map((page) => page.wiki_path),
      })
    } else if (pages.length > 1) {
      result.conflict.push({
        id,
        source_path: source?.source_path ?? null,
        wiki_paths: pages.map((page) => page.wiki_path),
      })
    } else if (!source) {
      result.orphaned.push({
        id,
        wiki_path: pages[0].wiki_path,
        wiki_revision: pages[0].source_revision,
      })
    } else if (pages.length === 0) {
      result.new.push(source)
    } else if (pages[0].source_revision !== source.source_revision) {
      result.outdated.push({
        ...source,
        wiki_path: pages[0].wiki_path,
        wiki_revision: pages[0].source_revision,
      })
    } else {
      result.current.push({ ...source, wiki_path: pages[0].wiki_path })
    }
  }

  return result
}
