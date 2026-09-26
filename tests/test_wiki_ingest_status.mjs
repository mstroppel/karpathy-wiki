import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  STATUS_OUTPUT_BUDGET_BYTES,
  scanIngestStatus,
  selectIngestStatus,
} from '../config/tools/wiki_ingest_status_core.mjs'

const REVISION = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
const OTHER_REVISION = 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'

function manifest(source, overrides = {}) {
  return {
    contract: 'karpathy-wiki-provider-manifest',
    version: 1,
    source,
    generated_at: 1760000000,
    items: [],
    revoked: [],
    errors: [],
    ...overrides,
  }
}

function item(sourceKey, sourcePath, wikiPath, sourceRevision = REVISION, frontmatter) {
  return {
    source_key: sourceKey,
    source_path: sourcePath,
    wiki_path: wikiPath,
    source_revision: sourceRevision,
    frontmatter: frontmatter ?? { source_revision: sourceRevision },
    claim: { source_path: sourcePath },
  }
}

function webdavItem(relativePath, sourceRevision = REVISION) {
  return item(relativePath, relativePath, `webdav/${relativePath}/index.md`, sourceRevision, {
    source_adapter: 'webdav',
    source_path: relativePath,
    source_revision: sourceRevision,
  })
}

function paperlessItem(id, sourceRevision = REVISION) {
  return {
    source_key: String(id),
    source_path: `0000-0999/document-${id}.md`,
    wiki_path: `0000-0999/paperless-${id}.md`,
    source_revision: sourceRevision,
    frontmatter: {
      paperless_id: id,
      source_revision: sourceRevision,
    },
    claim: { paperless_id: String(id) },
  }
}

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-status-'))
  const sourceRoot = path.join(root, 'sources')
  const wikiSourceRoot = path.join(root, 'wiki', 'sources')
  await mkdir(sourceRoot, { recursive: true })
  await mkdir(wikiSourceRoot, { recursive: true })
  return { root, sourceRoot, wikiSourceRoot }
}

async function writeManifest(sourceRoot, source, overrides = {}) {
  const directory = path.join(sourceRoot, source)
  await mkdir(directory, { recursive: true })
  await writeFile(
    path.join(directory, 'manifest.json'),
    JSON.stringify(manifest(source, overrides)),
  )
}

async function writePage(wikiSourceRoot, pagePath, frontmatterLines, body = '\n# Quelle\n') {
  const destination = path.join(wikiSourceRoot, pagePath)
  await mkdir(path.dirname(destination), { recursive: true })
  await writeFile(destination, `---\n${frontmatterLines.join('\n')}\n---\n${body}`)
}

test('tracks WebDAV pages by the provider manifest', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await mkdir(path.join(sourceRoot, 'webdav', 'nested'), { recursive: true })
    await writeFile(path.join(sourceRoot, 'webdav', 'new.txt'), 'new')
    await writeFile(path.join(sourceRoot, 'webdav', 'nested', 'current.md'), 'current')
    await writeFile(path.join(sourceRoot, 'webdav', 'changed.pdf'), 'changed')

    await writeManifest(sourceRoot, 'webdav', {
      wiki_root: 'webdav',
      items: [
        webdavItem('new.txt'),
        webdavItem('changed.pdf', OTHER_REVISION),
        webdavItem('nested/current.md'),
        webdavItem('duplicate.txt'),
      ],
    })
    await writePage(wikiSourceRoot, 'webdav/changed.pdf/index.md', [
      'source_adapter: webdav',
      'source_path: "changed.pdf"',
      `source_revision: ${REVISION}`,
    ])
    await writePage(wikiSourceRoot, 'webdav/nested/current.md/index.md', [
      'source_adapter: webdav',
      'source_path: "nested/current.md"',
      `source_revision: ${REVISION}`,
    ])
    await writePage(wikiSourceRoot, 'webdav/duplicate.txt/index.md', [
      'source_adapter: webdav',
      'source_path: "duplicate.txt"',
      `source_revision: ${REVISION}`,
    ])
    await writePage(wikiSourceRoot, 'webdav/misplaced.md', [
      'source_adapter: webdav',
      'source_path: "duplicate.txt"',
      `source_revision: ${REVISION}`,
    ])
    await writePage(wikiSourceRoot, 'webdav/removed.docx/index.md', [
      'source_adapter: webdav',
      'source_path: "removed.docx"',
      `source_revision: ${REVISION}`,
    ])

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
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
    assert.equal(webdav.new[0].source_key, 'new.txt')
    assert.equal(webdav.new[0].frontmatter.source_revision, webdav.new[0].source_revision)
    assert.match(webdav.new[0].wiki_path, /webdav\/new\.txt\/index\.md$/)
    assert.match(webdav.new[0].source_path, /sources\/webdav\/new\.txt$/)
    assert.equal(webdav.outdated[0].source_key, 'changed.pdf')
    assert.equal(webdav.outdated[0].wiki_revision, REVISION)
    assert.equal(webdav.current[0].source_key, 'nested/current.md')
    assert.equal(webdav.conflict[0].source_key, 'duplicate.txt')
    assert.equal(webdav.conflict[0].wiki_paths.length, 2)
    assert.match(webdav.orphaned[0].wiki_path, /removed\.docx\/index\.md$/)
    assert.equal(webdav.orphaned[0].wiki_revision, REVISION)
    assert.match(webdav.invalid[0].path, /misplaced\.md$/)
    assert.match(webdav.invalid[0].error, /erwarteter Pfad/)
    assert.deepEqual(status.summary, webdav.summary)
    assert.equal(status.adapters.paperless, undefined)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('omits current entries but retains their count', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await mkdir(path.join(sourceRoot, 'webdav'), { recursive: true })
    await writeFile(path.join(sourceRoot, 'webdav', 'same.txt'), 'same')
    await writeManifest(sourceRoot, 'webdav', {
      wiki_root: 'webdav',
      items: [webdavItem('same.txt')],
    })
    await writePage(wikiSourceRoot, 'webdav/same.txt/index.md', [
      'source_adapter: webdav',
      'source_path: "same.txt"',
      `source_revision: ${REVISION}`,
    ])

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.equal(status.adapters.webdav.summary.current, 1)
    assert.deepEqual(status.adapters.webdav.current, [])
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('scoped status retains global blockers and counts while narrowing pending work', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'paperless', {
      items: [paperlessItem(42), paperlessItem(43)],
    })
    await writeManifest(sourceRoot, 'webdav', {
      wiki_root: 'webdav',
      items: [webdavItem('note.md')],
      errors: [{ error: 'privacy-validation:PrivacyValidationError' }],
    })
    const full = await scanIngestStatus({ sourceRoot, wikiSourceRoot })
    const scoped = selectIngestStatus(full, { adapter: 'paperless', sourceKey: '42' })

    assert.deepEqual(scoped.summary, full.summary)
    assert.deepEqual(scoped.adapters.paperless.summary, full.adapters.paperless.summary)
    assert.deepEqual(scoped.adapters.paperless.new, [full.adapters.paperless.new[0]])
    assert.deepEqual(scoped.adapters.webdav.new, [])
    assert.deepEqual(scoped.adapters.webdav.invalid, [])
    assert.equal(scoped.adapters.webdav.summary.invalid, 1)

    const finalCheck = selectIngestStatus(full, { summaryOnly: true })
    assert.deepEqual(finalCheck.summary, full.summary)
    assert.deepEqual(finalCheck.adapters.paperless.new, [])
    assert.deepEqual(finalCheck.adapters.webdav.new, [])
    assert.deepEqual(finalCheck.adapters.webdav.invalid, full.adapters.webdav.invalid)
    assert.deepEqual(
      full.adapters.paperless.new.map((entry) => entry.source_key),
      ['42', '43'],
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('scoped status rejects ambiguous filter combinations', () => {
  const status = { summary: {}, adapters: {} }
  assert.throws(() => selectIngestStatus(status, { sourceKey: '42' }), /erfordert adapter/)
  assert.throws(
    () => selectIngestStatus(status, { adapter: 'paperless', sourceKey: '42', summaryOnly: true }),
    /kombiniert/,
  )
})

test('returns adapter-only status pages with stable global counts', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'paperless', {
      items: [paperlessItem(42), paperlessItem(43), paperlessItem(44)],
    })
    await writeManifest(sourceRoot, 'webdav', {
      wiki_root: 'webdav',
      items: [webdavItem('first.txt'), webdavItem('second.txt')],
    })

    const full = await scanIngestStatus({ sourceRoot, wikiSourceRoot })
    const first = selectIngestStatus(full, { adapter: 'paperless', limit: 2 })
    const second = selectIngestStatus(full, { adapter: 'paperless', offset: 2, limit: 2 })

    assert.deepEqual(full.summary, {
      new: 5,
      outdated: 0,
      current: 0,
      conflict: 0,
      revoked: 0,
      orphaned: 0,
      invalid: 0,
    })
    assert.deepEqual(first.summary, full.summary)
    assert.deepEqual(
      first.adapters.paperless.new.map((entry) => entry.source_key),
      ['42', '43'],
    )
    assert.deepEqual(first.adapters.webdav.new, [])
    assert.deepEqual(first.page, { offset: 0, limit: 2, has_more: true, next_offset: 2 })
    assert.deepEqual(
      second.adapters.paperless.new.map((entry) => entry.source_key),
      ['44'],
    )
    assert.deepEqual(second.page, { offset: 2, limit: 2, has_more: false, next_offset: null })
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('bounds diagnostics while retaining their global counts', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'paperless', {
      errors: [{ error: 'first error' }, { error: 'second error' }, { error: 'third error' }],
    })
    await writeManifest(sourceRoot, 'webdav', {
      wiki_root: 'webdav',
      errors: [{ error: 'webdav error' }],
    })

    const full = await scanIngestStatus({ sourceRoot, wikiSourceRoot })
    const first = selectIngestStatus(full, { adapter: 'paperless', summaryOnly: true, limit: 2 })
    const second = selectIngestStatus(full, {
      adapter: 'paperless',
      summaryOnly: true,
      offset: 2,
      limit: 2,
    })

    assert.equal(first.summary.invalid, 4)
    assert.equal(first.adapters.paperless.summary.invalid, 3)
    assert.deepEqual(
      first.adapters.paperless.invalid.map((entry) => entry.error),
      ['first error', 'second error'],
    )
    assert.deepEqual(first.adapters.webdav.invalid, [])
    assert.deepEqual(first.page, { offset: 0, limit: 2, has_more: true, next_offset: 2 })
    assert.deepEqual(
      second.adapters.paperless.invalid.map((entry) => entry.error),
      ['third error'],
    )
    assert.deepEqual(second.page, { offset: 2, limit: 2, has_more: false, next_offset: null })
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('caps serialized multi-adapter pages and reduces the entry count', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    for (const source of ['alpha', 'beta', 'gamma']) {
      await writeManifest(sourceRoot, source, {
        wiki_root: source,
        items: Array.from({ length: 12 }, (_, index) =>
          item(`${source}-${index}`, `${index}.txt`, `${source}/${index}.md`, REVISION, {
            source_revision: REVISION,
            description: 'x'.repeat(1000),
          }),
        ),
      })
    }

    const full = await scanIngestStatus({ sourceRoot, wikiSourceRoot })
    const page = selectIngestStatus(full, { limit: 10 })

    assert.ok(Buffer.byteLength(JSON.stringify(page), 'utf8') <= STATUS_OUTPUT_BUDGET_BYTES)
    assert.ok(page.page.limit < 10)
    assert.equal(page.page.has_more, true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('blocks oversized records and retrieves them in bounded JSON chunks', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const large = {
      ...paperlessItem(42),
      frontmatter: {
        paperless_id: 42,
        source_revision: REVISION,
        description: 'x'.repeat(40_000),
      },
    }
    await writeManifest(sourceRoot, 'paperless', { items: [large] })
    const full = await scanIngestStatus({ sourceRoot, wikiSourceRoot })
    const blocked = selectIngestStatus(full, { adapter: 'paperless', limit: 1 })

    assert.equal(blocked.page.blocked, true)
    assert.deepEqual(blocked.oversized_records, [
      { adapter: 'paperless', state: 'new', source_key: '42' },
    ])
    assert.ok(Buffer.byteLength(JSON.stringify(blocked), 'utf8') <= STATUS_OUTPUT_BUDGET_BYTES)

    const chunks = []
    let offset = 0
    while (true) {
      const response = selectIngestStatus(full, {
        adapter: 'paperless',
        sourceKey: '42',
        recordChunkOffset: offset,
      })
      assert.ok(Buffer.byteLength(JSON.stringify(response), 'utf8') <= STATUS_OUTPUT_BUDGET_BYTES)
      chunks.push(response.record.json)
      if (response.record.next_offset === null) break
      offset = response.record.next_offset
    }

    assert.deepEqual(JSON.parse(chunks.join('')), full.adapters.paperless.new[0])
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('bounds status page sizes and offsets', () => {
  const status = { summary: {}, adapters: {} }
  assert.throws(() => selectIngestStatus(status, { limit: 26 }), /zwischen 1 und 25/)
  assert.throws(() => selectIngestStatus(status, { offset: -1 }), /nicht-negative Ganzzahl/)
})

test('preserves Paperless revision states and revocations', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'paperless', {
      items: [paperlessItem(42), paperlessItem(43), paperlessItem(44, OTHER_REVISION)],
      revoked: [{ source_key: '46', claim: { paperless_id: '46' } }],
      errors: [
        {
          source_key: '47',
          path: '0000-0999/document-47.md',
          error: 'privacy-validation:PrivacyValidationError',
        },
      ],
    })
    await mkdir(path.join(sourceRoot, 'paperless', '0000-0999'), { recursive: true })
    await mkdir(path.join(wikiSourceRoot, '0000-0999'), { recursive: true })
    const page = (id, revision) => [`paperless_id: ${id}`, `source_revision: ${revision}`]
    await Promise.all([
      writePage(wikiSourceRoot, '0000-0999/paperless-42.md', page(42, REVISION)),
      writePage(wikiSourceRoot, '0000-0999/paperless-44.md', page(44, REVISION)),
      writePage(wikiSourceRoot, '0000-0999/paperless-45.md', page(45, OTHER_REVISION)),
      writePage(wikiSourceRoot, '0000-0999/paperless-46.md', page(46, OTHER_REVISION)),
    ])

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
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
    assert.equal(paperless.current[0].source_key, '42')
    assert.equal(paperless.new[0].source_key, '43')
    assert.equal(paperless.new[0].frontmatter.paperless_id, 43)
    assert.equal(paperless.outdated[0].source_key, '44')
    assert.equal(paperless.orphaned[0].wiki_revision, OTHER_REVISION)
    assert.equal(paperless.revoked[0].source_key, '46')
    assert.equal(paperless.revoked[0].wiki_paths.length, 1)
    assert.equal(paperless.invalid[0].source_path, undefined)
    assert.match(paperless.invalid[0].error, /privacy-validation/)
    assert.deepEqual(status.summary, paperless.summary)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reports conflicting Paperless pages', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'paperless', { items: [paperlessItem(42)] })
    const lines = [`paperless_id: 42`, `source_revision: ${REVISION}`]
    await writePage(wikiSourceRoot, '0000-0999/paperless-42.md', lines)
    await writePage(wikiSourceRoot, '0000-0999/duplicate.md', lines)

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.equal(status.adapters.paperless.summary.conflict, 1)
    assert.equal(status.adapters.paperless.summary.invalid, 1)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects sources without a manifest', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await mkdir(path.join(sourceRoot, 'unsupported'), { recursive: true })
    await mkdir(path.join(wikiSourceRoot, 'unsupported'), { recursive: true })

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.deepEqual(Object.keys(status.adapters), ['unsupported'])
    assert.equal(status.adapters.unsupported.summary.invalid, 1)
    assert.match(status.adapters.unsupported.invalid[0].error, /ENOENT|manifest/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects manifests with an unsupported version or wrong source name', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'custom', { version: 2 })
    await writeManifest(sourceRoot, 'mismatched', { source: 'other' })

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.match(status.adapters.custom.invalid[0].error, /nicht unterstützte Manifest-Version/)
    assert.match(status.adapters.mismatched.invalid[0].error, /source stimmt nicht/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reads a third-party manifest without changing the generic tool', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'custom', {
      wiki_root: 'custom',
      items: [item('item', 'item.txt', 'custom/item.md')],
    })
    await mkdir(path.join(sourceRoot, 'custom'), { recursive: true })
    await writeFile(path.join(sourceRoot, 'custom', 'item.txt'), 'item')

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.deepEqual(Object.keys(status.adapters), ['custom'])
    assert.equal(status.adapters.custom.summary.new, 1)
    assert.equal(status.adapters.custom.new[0].source_key, 'item')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reports wiki path collisions across sources', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'alpha', {
      wiki_root: 'alpha',
      items: [item('one', 'one.txt', 'shared/collision.md')],
    })
    await writeManifest(sourceRoot, 'beta', {
      wiki_root: 'beta',
      items: [item('two', 'two.txt', 'shared/collision.md')],
    })

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.equal(status.adapters.alpha.summary.conflict, 1)
    assert.equal(status.adapters.beta.summary.conflict, 1)
    assert.equal(status.summary.conflict, 2)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects duplicate wiki_root values across sources', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'alpha', {
      items: [item('one', 'one.txt', 'notes/one.md')],
    })
    await writeManifest(sourceRoot, 'beta', {
      items: [item('two', 'two.txt', 'notes/two.md')],
    })

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot })

    assert.match(status.adapters.alpha.invalid[0].error, /wiki_root wird von mehreren/)
    assert.match(status.adapters.beta.invalid[0].error, /wiki_root wird von mehreren/)
    assert.equal(status.summary.new, 0)
    assert.equal(status.summary.invalid, 2)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects pages that do not carry the declared frontmatter', async () => {
  const { root, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'webdav', {
      wiki_root: 'webdav',
      items: [webdavItem('doc.txt')],
    })
    // the claim matches, but the page drops the declared `source_adapter`
    await writePage(wikiSourceRoot, 'webdav/doc.txt/index.md', [
      'source_path: "doc.txt"',
      `source_revision: ${REVISION}`,
    ])

    const status = await scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })

    assert.equal(status.adapters.webdav.summary.current, 0)
    assert.equal(status.adapters.webdav.summary.new, 1)
    assert.equal(status.adapters.webdav.summary.invalid, 1)
    assert.match(status.adapters.webdav.invalid[0].error, /source_adapter weicht ab/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
