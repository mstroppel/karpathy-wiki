import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { scanIngestStatus } from '../config/tools/wiki_ingest_status_core.mjs'

const ADAPTER_ROOT = path.resolve('config/ingest-adapters')
const revision = (content) => createHash('sha256').update(content).digest('hex')

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-status-'))
  const sourceRoot = path.join(root, 'sources')
  const wikiSourceRoot = path.join(root, 'wiki', 'sources')
  await mkdir(path.join(sourceRoot, 'webdav', 'nested'), { recursive: true })
  await mkdir(path.join(wikiSourceRoot, 'webdav', 'nested'), { recursive: true })
  return { root, sourceRoot, wikiSourceRoot }
}

async function webdavPage(
  wikiSourceRoot,
  relativePath,
  sourceRevision,
  pagePath = path.join(relativePath, 'index.md'),
) {
  const destination = path.join(wikiSourceRoot, 'webdav', pagePath)
  await mkdir(path.dirname(destination), { recursive: true })
  await writeFile(
    destination,
    [
      '---',
      'source_adapter: webdav',
      `source_path: ${JSON.stringify(relativePath)}`,
      `source_revision: ${sourceRevision}`,
      '---',
      '',
      '# Quelle',
      '',
    ].join('\n'),
  )
}

test('tracks WebDAV files by relative path and content hash', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const webdavRoot = path.join(sourceRoot, 'webdav')
    await writeFile(path.join(webdavRoot, 'new.txt'), 'new')
    await writeFile(path.join(webdavRoot, 'changed.pdf'), 'new revision')
    await writeFile(path.join(webdavRoot, 'nested', 'current.md'), 'current')
    await writeFile(path.join(webdavRoot, 'duplicate.txt'), 'duplicate')

    await webdavPage(wikiSourceRoot, 'changed.pdf', revision('old revision'))
    await webdavPage(wikiSourceRoot, 'nested/current.md', revision('current'))
    await webdavPage(wikiSourceRoot, 'removed.docx', revision('removed'))
    await webdavPage(wikiSourceRoot, 'duplicate.txt', revision('duplicate'))
    await webdavPage(wikiSourceRoot, 'duplicate.txt', revision('duplicate'), 'misplaced.md')

    const status = await scanIngestStatus({
      sourceRoot,
      wikiSourceRoot,
      adapterRoot: ADAPTER_ROOT,
      includeCurrent: true,
    })
    const webdav = status.adapters.webdav

    assert.deepEqual(webdav.summary, {
      new: 1,
      outdated: 1,
      current: 1,
      conflict: 1,
      revoked: 0,
      orphaned: 1,
      invalid: 1,
    })
    assert.equal(webdav.new[0].source_relative_path, 'new.txt')
    assert.equal(webdav.new[0].source_key, 'new.txt')
    assert.equal(webdav.new[0].frontmatter.source_revision, webdav.new[0].source_revision)
    assert.match(webdav.new[0].wiki_path, /webdav\/new\.txt\/index\.md$/)
    assert.equal(webdav.outdated[0].source_relative_path, 'changed.pdf')
    assert.equal(webdav.current[0].source_relative_path, 'nested/current.md')
    assert.equal(webdav.conflict[0].source_relative_path, 'duplicate.txt')
    assert.equal(webdav.orphaned[0].source_relative_path, 'removed.docx')
    assert.equal(webdav.invalid[0].path, path.join(wikiSourceRoot, 'webdav', 'misplaced.md'))
    assert.deepEqual(status.summary, webdav.summary)
    assert.equal(status.adapters.paperless, undefined)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('omits current entries but retains their count', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const content = 'same'
    await writeFile(path.join(sourceRoot, 'webdav', 'same.txt'), content)
    await webdavPage(wikiSourceRoot, 'same.txt', revision(content))

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot: ADAPTER_ROOT })

    assert.equal(status.adapters.webdav.summary.current, 1)
    assert.deepEqual(status.adapters.webdav.current, [])
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('uses collision-free pages for file and directory-like source names', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeFile(path.join(sourceRoot, 'webdav', 'foo'), 'file')
    await mkdir(path.join(sourceRoot, 'webdav', 'foo.md'), { recursive: true })
    await writeFile(path.join(sourceRoot, 'webdav', 'foo.md', 'child'), 'child')

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot: ADAPTER_ROOT })

    assert.deepEqual(
      status.adapters.webdav.new.map((item) => item.source_relative_path),
      ['foo', 'foo.md/child'],
    )
    assert.equal(status.adapters.webdav.summary.conflict, 0)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('preserves Paperless revision states', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const paperlessRoot = path.join(sourceRoot, 'paperless')
    const rangeRoot = path.join(paperlessRoot, '0000-0999')
    const wikiRangeRoot = path.join(wikiSourceRoot, '0000-0999')
    await mkdir(rangeRoot, { recursive: true })
    await mkdir(wikiRangeRoot, { recursive: true })
    const writeSource = (id, hash) =>
      writeFile(
        path.join(rangeRoot, `document-${id}.md`),
        `---\npaperless_id: ${id}\nsource_revision: ${hash}\nanonymized: true\npaperless_url: https://paperless.example/documents/${id}\n---\n`,
      )
    const writePage = (id, hash) =>
      writeFile(
        path.join(wikiRangeRoot, `paperless-${id}.md`),
        `---\npaperless_id: ${id}\nsource_revision: ${hash}\n---\n`,
      )
    await Promise.all([
      writeSource(42, 'a'.repeat(64)),
      writePage(42, 'a'.repeat(64)),
      writeSource(43, 'b'.repeat(64)),
      writeSource(44, 'c'.repeat(64)),
      writePage(44, 'd'.repeat(64)),
      writePage(45, 'e'.repeat(64)),
      writeSource(46, 'f'.repeat(64)),
      writeSource(47, 'invalid'),
      writeFile(
        path.join(paperlessRoot, 'revoked.md'),
        '# Widerrufene Paperless-Dokumente\n- 46\n',
      ),
    ])

    const status = await scanIngestStatus({
      sourceRoot,
      wikiSourceRoot,
      adapterRoot: ADAPTER_ROOT,
      includeCurrent: true,
    })
    const paperless = status.adapters.paperless

    assert.deepEqual(paperless.summary, {
      new: 1,
      outdated: 1,
      current: 1,
      conflict: 0,
      revoked: 1,
      orphaned: 1,
      invalid: 1,
    })
    assert.equal(paperless.current[0].id, 42)
    assert.equal(paperless.new[0].id, 43)
    assert.equal(paperless.new[0].source_key, '43')
    assert.equal(paperless.new[0].frontmatter.paperless_id, 43)
    assert.equal(paperless.outdated[0].id, 44)
    assert.equal(paperless.orphaned[0].id, 45)
    assert.equal(paperless.revoked[0].id, 46)
    assert.match(paperless.invalid[0].error, /source_revision/)
    assert.deepEqual(status.summary, paperless.summary)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reports conflicting Paperless pages', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const hash = 'a'.repeat(64)
    const rangeRoot = path.join(sourceRoot, 'paperless', '0000-0999')
    const wikiRangeRoot = path.join(wikiSourceRoot, '0000-0999')
    await mkdir(rangeRoot, { recursive: true })
    await mkdir(wikiRangeRoot, { recursive: true })
    await writeFile(
      path.join(rangeRoot, 'document-42.md'),
      `---\npaperless_id: 42\nsource_revision: ${hash}\nanonymized: true\npaperless_url: https://paperless.example/documents/42\n---\n`,
    )
    const page = `---\npaperless_id: 42\nsource_revision: ${hash}\n---\n`
    await writeFile(path.join(wikiRangeRoot, 'paperless-42.md'), page)
    await writeFile(path.join(wikiRangeRoot, 'duplicate.md'), page)

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot: ADAPTER_ROOT })

    assert.equal(status.adapters.paperless.summary.conflict, 1)
    assert.equal(status.adapters.paperless.summary.invalid, 1)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects an invalid Paperless revocation list', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const paperlessRoot = path.join(sourceRoot, 'paperless')
    await mkdir(paperlessRoot, { recursive: true })
    await writeFile(path.join(paperlessRoot, 'revoked.md'), 'invalid\n')

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot: ADAPTER_ROOT })

    assert.equal(status.adapters.paperless.summary.invalid, 1)
    assert.match(status.adapters.paperless.invalid[0].error, /Ungültige Widerrufsliste/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('discovers adapters by source directory and rejects missing modules', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await mkdir(path.join(sourceRoot, 'unsupported'))

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot: ADAPTER_ROOT })

    assert.deepEqual(Object.keys(status.adapters), ['unsupported', 'webdav'])
    assert.equal(status.adapters.unsupported.summary.invalid, 1)
    assert.match(status.adapters.unsupported.invalid[0].error, /status\.mjs/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('loads a new adapter without changing the generic tool', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-adapter-'))
  try {
    const sourceRoot = path.join(root, 'sources')
    const wikiSourceRoot = path.join(root, 'wiki', 'sources')
    const adapterRoot = path.join(root, 'adapters')
    await mkdir(path.join(sourceRoot, 'custom'), { recursive: true })
    await mkdir(path.join(adapterRoot, 'custom'), { recursive: true })
    await writeFile(path.join(sourceRoot, 'custom', 'item.txt'), 'item')
    await writeFile(
      path.join(adapterRoot, 'custom', 'status.mjs'),
      `
      const revision = "a".repeat(64)
      export default async ({ sourceRoot, wikiSourceRoot }) => ({
        new: [{
          source_key: "item",
          source_path: sourceRoot + "/item.txt",
          source_revision: revision,
          wiki_path: wikiSourceRoot + "/custom/item.md",
          frontmatter: { source_revision: revision },
        }],
        outdated: [], current: [], conflict: [], revoked: [], orphaned: [], invalid: [],
      })
    `,
    )

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot })

    assert.deepEqual(Object.keys(status.adapters), ['custom'])
    assert.equal(status.adapters.custom.summary.new, 1)
    assert.equal(status.adapters.custom.new[0].source_key, 'item')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reports reserved folder names and invalid adapter contracts', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-adapter-'))
  try {
    const sourceRoot = path.join(root, 'sources')
    const wikiSourceRoot = path.join(root, 'wiki', 'sources')
    const adapterRoot = path.join(root, 'adapters')
    await mkdir(path.join(sourceRoot, '__proto__'), { recursive: true })
    await mkdir(path.join(sourceRoot, 'broken'), { recursive: true })
    await mkdir(path.join(sourceRoot, 'pathtrick'), { recursive: true })
    await mkdir(path.join(sourceRoot, 'toxic'), { recursive: true })
    await mkdir(path.join(sourceRoot, 'extraneous'), { recursive: true })
    await mkdir(path.join(adapterRoot, 'broken'), { recursive: true })
    await mkdir(path.join(adapterRoot, 'pathtrick'), { recursive: true })
    await mkdir(path.join(adapterRoot, 'toxic'), { recursive: true })
    await mkdir(path.join(adapterRoot, 'extraneous'), { recursive: true })
    await writeFile(
      path.join(adapterRoot, 'broken', 'status.mjs'),
      'export default async () => ({ new: [] })\n',
    )
    await writeFile(
      path.join(adapterRoot, 'toxic', 'status.mjs'),
      `
      export default async () => ({
        new: [], outdated: [], current: [], conflict: [], revoked: [], orphaned: [],
        invalid: [{ value: 1n }],
      })
    `,
    )
    await writeFile(
      path.join(adapterRoot, 'pathtrick', 'status.mjs'),
      `
      const revision = "a".repeat(64)
      export default async ({ sourceRoot, wikiSourceRoot }) => ({
        new: [{
          source_key: "item",
          source_path: sourceRoot + "/item",
          source_revision: revision,
          wiki_path: wikiSourceRoot + "/x/../item.md",
          frontmatter: { source_revision: revision },
        }],
        outdated: [], current: [], conflict: [], revoked: [], orphaned: [], invalid: [],
      })
    `,
    )
    await writeFile(
      path.join(adapterRoot, 'extraneous', 'status.mjs'),
      `
      export default async () => ({
        new: [], outdated: [], current: [], conflict: [], revoked: [], orphaned: [],
        invalid: [], extra: [],
      })
    `,
    )

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot })

    assert.equal(Object.hasOwn(status.adapters, '__proto__'), true)
    assert.equal(status.adapters.__proto__.summary.invalid, 1)
    assert.equal(status.adapters.broken.summary.invalid, 1)
    assert.match(status.adapters.broken.invalid[0].error, /outdated/)
    assert.equal(status.adapters.toxic.summary.invalid, 1)
    assert.match(status.adapters.toxic.invalid[0].error, /JSON-kompatiblen Wert/)
    assert.equal(status.adapters.pathtrick.summary.invalid, 1)
    assert.match(status.adapters.pathtrick.invalid[0].error, /wiki_path/)
    assert.equal(status.adapters.extraneous.summary.invalid, 1)
    assert.match(status.adapters.extraneous.invalid[0].error, /unbekanntes Adapterfeld extra/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reports wiki path collisions across adapters', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-adapter-'))
  try {
    const sourceRoot = path.join(root, 'sources')
    const wikiSourceRoot = path.join(root, 'wiki', 'sources')
    const adapterRoot = path.join(root, 'adapters')
    for (const name of ['first', 'second']) {
      await mkdir(path.join(sourceRoot, name), { recursive: true })
      await mkdir(path.join(adapterRoot, name), { recursive: true })
      await writeFile(path.join(sourceRoot, name, 'item'), name)
      await writeFile(
        path.join(adapterRoot, name, 'status.mjs'),
        `
        const revision = "${name === 'first' ? 'a' : 'b'}".repeat(64)
        export default async ({ sourceRoot, wikiSourceRoot }) => ({
          new: [{
            source_key: "item",
            source_path: sourceRoot + "/item",
            source_revision: revision,
            wiki_path: wikiSourceRoot + "/shared.md",
            frontmatter: { source_revision: revision },
          }],
          outdated: [], current: [], conflict: [], revoked: [], orphaned: [], invalid: [],
        })
      `,
      )
    }

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, adapterRoot })

    assert.equal(status.summary.conflict, 2)
    assert.equal(status.summary.new, 0)
    assert.equal(
      status.adapters.first.conflict[0].wiki_path,
      path.join(wikiSourceRoot, 'shared.md'),
    )
    assert.equal(
      status.adapters.second.conflict[0].wiki_path,
      path.join(wikiSourceRoot, 'shared.md'),
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
