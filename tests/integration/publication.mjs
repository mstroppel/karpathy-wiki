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
assert.equal(status.adapters.webdav[expected].length, 2, JSON.stringify(status.summary))
assert.equal(
  status.adapters.webdav.new.length +
    status.adapters.webdav.outdated.length +
    status.adapters.webdav.current.length,
  2,
)
assert.deepEqual(status.adapters.webdav[expected].map((record) => record.source_key).sort(), [
  'notes.html',
  'notes.md',
])
for (const record of status.adapters.webdav[expected]) {
  const content = await readFile(record.source_path, 'utf8')
  assert.ok(content.includes('[PERSON_1]'), 'source must be redacted before wiki publication')
  assert.ok(!content.includes('Max Mustermann'), 'raw source leaked into the sanitized generation')
  if (record.source_key === 'notes.html') {
    assert.ok(content.startsWith('<!DOCTYPE html>\r\n'))
    assert.ok(content.includes('<p title="[PERSON_1]">[PERSON_1]<b></b>'))
    assert.ok(content.endsWith('<script>/* fixture */</script>\r\n'))
  }

  if (publish === 'publish') {
    const fields = Object.entries(record.frontmatter)
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
      .join('\n')
    await mkdir(path.dirname(record.wiki_path), { recursive: true })
    await writeFile(
      record.wiki_path,
      `---\n${fields}\n---\n\n# Integration note\n\nSource: ${record.source_path}\n\n${
        record.source_key.endsWith('.html') ? `\`\`\`html\n${content}\n\`\`\`` : content
      }`,
    )
  }
}
if (publish === 'publish') {
  const after = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
  assert.equal(after.adapters.webdav.current.length, 2, JSON.stringify(after.summary))
  assert.equal(after.summary.new + after.summary.outdated + after.summary.invalid, 0)
}
