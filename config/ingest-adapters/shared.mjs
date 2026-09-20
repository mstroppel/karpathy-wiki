import { readdir } from 'node:fs/promises'
import path from 'node:path'

export const REVISION_RE = /^[0-9a-f]{64}$/

export function emptyResult() {
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

export async function files(root, { markdownOnly = false, required = false } = {}) {
  const result = []

  async function visit(directory) {
    let entries
    try {
      entries = await readdir(directory, { withFileTypes: true })
    } catch (error) {
      if (error?.code === 'ENOENT' && (!required || directory !== root)) return
      throw error
    }
    for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
      const entryPath = path.join(directory, entry.name)
      if (entry.isDirectory()) await visit(entryPath)
      else if (entry.isFile() && (!markdownOnly || entry.name.endsWith('.md')))
        result.push(entryPath)
    }
  }

  await visit(root)
  return result
}

export function frontmatter(text) {
  const lines = text.replaceAll('\r\n', '\n').split('\n')
  if (lines[0] !== '---') throw new Error('fehlendes Frontmatter')
  const end = lines.indexOf('---', 1)
  if (end === -1) throw new Error('nicht abgeschlossenes Frontmatter')
  return lines.slice(1, end)
}

export function field(lines, name) {
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
  return value.replace(/^'(.*)'$/, '$1')
}

export function revisionField(lines) {
  const revision = field(lines, 'source_revision')
  if (!REVISION_RE.test(revision)) throw new Error('source_revision ist ungültig')
  return revision
}
