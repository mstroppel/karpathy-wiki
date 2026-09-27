// Exercise the real provider manifest/status contract with a deterministic
// test publication. No model service or synthetic production publisher is used.
import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { scanIngestStatus } from '../../config/tools/wiki_ingest_status_core.mjs'

const [dataRoot, expected, publish] = process.argv.slice(2)
assert.ok(dataRoot && expected, 'usage: publication.mjs DATA_ROOT new|outdated|current [publish]')

const sourceRoot = path.join(dataRoot, 'sources')
const wikiSourceRoot = path.join(dataRoot, 'wiki', 'sources')
const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
assert.equal(status.summary.invalid, 0, JSON.stringify(status.summary))
assert.equal(status.summary.conflict, 0, JSON.stringify(status.summary))
assert.equal(status.summary[expected], 1, JSON.stringify(status.summary))
assert.equal(status.summary.new + status.summary.outdated + status.summary.current, 1)
const record = status.adapters.webdav[expected][0]
assert.equal(record.source_key, 'notes.md')
const content = await readFile(record.source_path, 'utf8')
assert.ok(content.includes('[PERSON_1]'), 'source must be redacted before wiki publication')
assert.ok(!content.includes('Max Mustermann'), 'raw source leaked into the sanitized generation')

if (publish === 'publish') {
  const fields = Object.entries(record.frontmatter)
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join('\n')
  await mkdir(path.dirname(record.wiki_path), { recursive: true })
  await writeFile(
    record.wiki_path,
    `---\n${fields}\n---\n\n# Integration note\n\nSource: ${record.source_path}\n\n${content}`,
  )
  const after = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
  assert.equal(after.summary.current, 1, JSON.stringify(after.summary))
  assert.equal(after.summary.new + after.summary.outdated + after.summary.invalid, 0)
}
