import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { scanIngestStatus } from '../../config/tools/wiki_ingest_status_core.mjs'

const [dataRoot, expected, publish] = process.argv.slice(2)
assert.ok(dataRoot && expected)
const sourceRoot = path.join(dataRoot, 'sources')
const wikiSourceRoot = path.join(dataRoot, 'wiki', 'sources')
const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
assert.equal(status.summary.invalid, 0, JSON.stringify(status.summary))
assert.equal(status.summary.conflict, 0, JSON.stringify(status.summary))
const record = status.adapters.answers[expected][0]
assert.equal(record.source_key, 'review-1.md')
const content = await readFile(record.source_path, 'utf8')
assert.ok(content.includes('[PERSON_1]'))
assert.ok(!content.includes('Max Mustermann'))

if (publish === 'publish') {
  const fields = Object.entries(record.frontmatter)
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join('\n')
  await mkdir(path.dirname(record.wiki_path), { recursive: true })
  await writeFile(record.wiki_path, `---\n${fields}\n---\n\n# Answer source\n\n${content}`)
  const after = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
  assert.equal(after.adapters.answers.current.length, 1)
}
