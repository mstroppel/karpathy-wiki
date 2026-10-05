import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  CHUNK_BYTES_DEFAULT,
  JOURNAL_OUTPUT_BUDGET_BYTES,
  assembleReport,
  estimateSourceTokens,
  estimateTokensFromBytes,
  finishRun,
  listRecords,
  loadRun,
  planNextBatch,
  readChunk,
  renderReport,
  startRun,
  writeRecord,
} from '../config/tools/wiki_ingest_journal_core.mjs'

const REVISION = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
const OTHER_REVISION = 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
const NOW = new Date('2026-10-03T12:00:00.000Z')
const SUFFIX = 'abc123'
const RUN_ID = 'run-20261003t120000z-abc123'

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-journal-'))
  const journalRoot = path.join(root, 'journal')
  const sourceRoot = path.join(root, 'sources')
  const wikiSourceRoot = path.join(root, 'wiki', 'sources')
  await mkdir(journalRoot, { recursive: true })
  await mkdir(sourceRoot, { recursive: true })
  await mkdir(wikiSourceRoot, { recursive: true })
  return { root, journalRoot, sourceRoot, wikiSourceRoot }
}

function record(overrides = {}) {
  return {
    adapter: 'webdav',
    source_key: 'notes.md',
    source_path: '/knowledge/sources/webdav/notes.md',
    source_revision: REVISION,
    wiki_path: '/knowledge/wiki/sources/webdav/notes.md',
    status: 'ingested',
    commit: 'abc1234',
    changed_pages: ['sources/webdav/notes.md', 'overview.md'],
    content: 'Aussage mit Wert 3,5 m.',
    contradictions: 'Keine festgestellt',
    extraction_limits: 'Keine festgestellt',
    source_unmodified: true,
    ...overrides,
  }
}

function blockedRecord(overrides = {}) {
  return record({
    status: 'blocked',
    commit: null,
    changed_pages: [],
    content: null,
    contradictions: null,
    extraction_limits: null,
    source_unmodified: false,
    blocker: 'Quelle nicht lesbar',
    ...overrides,
  })
}

async function runFixture() {
  const paths = await fixture()
  const { run } = await startRun({ root: paths.journalRoot, now: NOW, idSuffix: SUFFIX })
  return { ...paths, run }
}

function manifest(source, items) {
  return {
    contract: 'karpathy-wiki-provider-manifest',
    version: 1,
    source,
    generated_at: 1760000000,
    items,
    revoked: [],
    errors: [],
  }
}

function sourceItem(sourceKey, sourcePath, wikiPath, sourceRevision, frontmatter) {
  return {
    source_key: sourceKey,
    source_path: sourcePath,
    wiki_path: wikiPath,
    source_revision: sourceRevision,
    frontmatter: frontmatter ?? { source_revision: sourceRevision },
    claim: { source_path: sourcePath },
  }
}

async function writeManifest(sourceRoot, source, items) {
  const directory = path.join(sourceRoot, source)
  await mkdir(directory, { recursive: true })
  await writeFile(
    path.join(directory, 'manifest.json'),
    JSON.stringify(manifest(source, items)),
    'utf8',
  )
}

async function writeSource(sourceRoot, source, relativePath, characters) {
  const destination = path.join(sourceRoot, source, relativePath)
  await mkdir(path.dirname(destination), { recursive: true })
  await writeFile(destination, 'x'.repeat(characters), 'utf8')
}

async function writePage(wikiSourceRoot, pagePath, frontmatterLines, characters = 100) {
  const destination = path.join(wikiSourceRoot, pagePath)
  await mkdir(path.dirname(destination), { recursive: true })
  await writeFile(
    destination,
    `---\n${frontmatterLines.join('\n')}\n---\n${'y'.repeat(characters)}`,
    'utf8',
  )
}

test('starts a run with a stable id and adopts an open run on resume', async () => {
  const { root } = await fixture()
  try {
    const first = await startRun({ root, now: NOW, idSuffix: SUFFIX })
    assert.equal(first.adopted, false)
    assert.equal(first.run.run_id, RUN_ID)
    assert.equal(first.run.state, 'running')
    assert.equal(first.run.budget.batches_dispatched, 0)

    const resumed = await startRun({ root, now: NOW, idSuffix: 'def456' })
    assert.equal(resumed.adopted, true)
    assert.equal(resumed.run.run_id, RUN_ID)

    const fresh = await startRun({ root, now: NOW, idSuffix: 'def456', resume: false })
    assert.equal(fresh.adopted, false)
    assert.equal(fresh.run.run_id, 'run-20261003t120000z-def456')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects invalid budget configuration', async () => {
  const { root } = await fixture()
  try {
    await assert.rejects(
      startRun({ root, now: NOW, idSuffix: SUFFIX, budgetTokens: 10 }),
      /budget_tokens muss eine Ganzzahl/,
    )
    await assert.rejects(
      startRun({ root, now: NOW, idSuffix: SUFFIX, maxSourcesPerBatch: 0 }),
      /max_sources_per_batch muss eine Ganzzahl/,
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('records one verified result per source and supersedes rewrites', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    const first = await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: record(),
      now: NOW,
    })
    assert.equal(first.record_index, 0)
    assert.equal(first.counts.ingested, 1)

    const second = await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: record({ content: 'Korrigierte Aussage: 4,0 m.', commit: 'def5678' }),
      now: NOW,
    })
    assert.equal(second.record_index, 1)
    assert.equal(second.counts.records, 1)

    const loaded = await loadRun({ root: journalRoot, runId: run.run_id })
    assert.equal(loaded.records.length, 2)
    assert.equal(loaded.effective.length, 1)
    assert.equal(loaded.effective[0].content, 'Korrigierte Aussage: 4,0 m.')
    assert.equal(loaded.effective[0].commit, 'def5678')
    assert.equal(loaded.effective[0].writes, 2)
    assert.equal(loaded.effective[0].record_index, 1)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects unverified or oversized records instead of truncating them', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    await assert.rejects(
      writeRecord({ root: journalRoot, runId: run.run_id, record: record({ commit: null }) }),
      /commit fehlt für status ingested/,
    )
    await assert.rejects(
      writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: record({ source_unmodified: false }),
      }),
      /source_unmodified muss für status ingested bestätigt sein/,
    )
    await assert.rejects(
      writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: record({ source_revision: 'kein-hash' }),
      }),
      /source_revision ist kein SHA-256-Hash/,
    )
    await assert.rejects(
      writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: blockedRecord({ blocker: null }),
      }),
      /blocker fehlt für status blocked/,
    )
    await assert.rejects(
      writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: record({ changed_pages: ['../außerhalb.md'] }),
      }),
      /changed_pages\[0\] ist kein normalisierter relativer Pfad/,
    )
    await assert.rejects(
      writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: record({
          content: 'z'.repeat(3900),
          contradictions: 'z'.repeat(3900),
          extraction_limits: 'z'.repeat(3900),
        }),
      }),
      /überschreitet das Budget von \d+ Bytes/,
    )
    const loaded = await loadRun({ root: journalRoot, runId: run.run_id })
    assert.equal(loaded.records.length, 0)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects records for completed runs', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    await writeRecord({ root: journalRoot, runId: run.run_id, record: record(), now: NOW })
    await finishRun({
      root: journalRoot,
      runId: run.run_id,
      finalStatus: {
        new: 0,
        outdated: 0,
        current: 1,
        conflict: 0,
        revoked: 0,
        orphaned: 0,
        invalid: 0,
      },
      now: NOW,
    })
    await assert.rejects(
      writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: record({ source_key: 'andere.md' }),
        now: NOW,
      }),
      /Aufzeichnungen sind nur für laufende Läufe möglich/,
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('lists effective records in bounded pages', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    for (let index = 0; index < 3; index += 1) {
      await writeRecord({
        root: journalRoot,
        runId: run.run_id,
        record: record({ source_key: `quelle-${index}.md` }),
        now: NOW,
      })
    }
    const page = await listRecords({ root: journalRoot, runId: run.run_id, offset: 0, limit: 2 })
    assert.equal(page.counts.records, 3)
    assert.equal(page.records.length, 2)
    assert.equal(page.page.has_more, true)
    assert.equal(page.page.next_offset, 2)

    const rest = await listRecords({
      root: journalRoot,
      runId: run.run_id,
      offset: 2,
      limit: 2,
    })
    assert.equal(rest.records.length, 1)
    assert.equal(rest.page.next_offset, null)
    assert.ok(
      Buffer.byteLength(JSON.stringify(page), 'utf8') <= JOURNAL_OUTPUT_BUDGET_BYTES,
      'list response stays within the output budget',
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reads journal lines and the report back in bounded chunks', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    await writeRecord({ root: journalRoot, runId: run.run_id, record: record(), now: NOW })
    const line = (
      await readFile(path.join(journalRoot, 'runs', run.run_id, 'records.jsonl'), 'utf8')
    )
      .trimEnd()
      .split('\n')[0]

    let offset = 0
    let assembled = ''
    for (;;) {
      const chunk = await readChunk({
        root: journalRoot,
        runId: run.run_id,
        recordIndex: 0,
        offset,
        chunkBytes: 16,
      })
      assembled += chunk.record.text
      if (chunk.record.next_offset === null) break
      offset = chunk.record.next_offset
    }
    assert.equal(assembled, line)

    await assembleReport({
      root: journalRoot,
      runId: run.run_id,
      finalStatus: {
        new: 0,
        outdated: 0,
        current: 0,
        conflict: 0,
        revoked: 0,
        orphaned: 0,
        invalid: 0,
      },
      now: NOW,
    })
    const reportText = await readFile(
      path.join(journalRoot, 'runs', run.run_id, 'report.md'),
      'utf8',
    )
    offset = 0
    let report = ''
    for (;;) {
      const chunk = await readChunk({
        root: journalRoot,
        runId: run.run_id,
        report: true,
        offset,
        chunkBytes: CHUNK_BYTES_DEFAULT,
      })
      report += chunk.report.text
      if (chunk.report.next_offset === null) break
      offset = chunk.report.next_offset
    }
    assert.equal(report, reportText)

    await assert.rejects(
      readChunk({ root: journalRoot, runId: run.run_id, offset: 0 }),
      /read erfordert record_index oder report/,
    )
    await assert.rejects(
      readChunk({ root: journalRoot, runId: run.run_id, recordIndex: 0, report: true }),
      /report und record_index können nicht kombiniert werden/,
    )
    await assert.rejects(
      readChunk({ root: journalRoot, runId: run.run_id, recordIndex: 9 }),
      /record_index/,
    )
    await assert.rejects(
      readChunk({
        root: journalRoot,
        runId: run.run_id,
        recordIndex: 0,
        offset: 100000,
      }),
      /chunk_offset liegt hinter dem Datensatz/,
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reconstructs a multi-chunk report with effective records and Unicode details', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: record({ content: 'Ersetzte Aussage', commit: 'aaa1111' }),
      now: NOW,
    })
    const effective = []
    for (let index = 0; index < 18; index += 1) {
      const item = record({
        source_key: index === 0 ? 'notes.md' : `quelle-${index}.md`,
        source_path: `/knowledge/sources/webdav/${index === 0 ? 'notes' : `quelle-${index}`}.md`,
        content: `Aussage ${index}: Größe 3,5 m 🧪. ${'Prüftext äöü. '.repeat(45)}`,
        contradictions: `Offene Frage ${index}`,
        extraction_limits: `Grenze ${index}`,
      })
      effective.push(item)
      await writeRecord({ root: journalRoot, runId: run.run_id, record: item, now: NOW })
    }
    const blocked = blockedRecord({
      source_key: 'blockiert.md',
      source_path: '/knowledge/sources/webdav/blockiert.md',
    })
    await writeRecord({ root: journalRoot, runId: run.run_id, record: blocked, now: NOW })
    const finished = await finishRun({
      root: journalRoot,
      runId: run.run_id,
      finalStatus: {
        new: 1,
        outdated: 0,
        current: 18,
        conflict: 0,
        revoked: 3,
        orphaned: 7,
        invalid: 0,
      },
      unfinished: [],
      now: NOW,
    })
    let offset = 0
    let text = ''
    let parts = 0
    let totalCharacters
    for (;;) {
      const payload = await readChunk({
        root: journalRoot,
        runId: run.run_id,
        report: true,
        offset,
        chunkBytes: CHUNK_BYTES_DEFAULT,
      })
      const chunk = payload.report
      assert.equal(chunk.offset, offset)
      totalCharacters ??= chunk.total_characters
      assert.equal(chunk.total_characters, totalCharacters)
      assert.ok(Buffer.byteLength(chunk.text, 'utf8') <= CHUNK_BYTES_DEFAULT)
      assert.ok(Buffer.byteLength(JSON.stringify(payload), 'utf8') <= JOURNAL_OUTPUT_BUDGET_BYTES)
      text += chunk.text
      parts += 1
      offset += Array.from(chunk.text).length
      if (chunk.next_offset === null) break
      assert.equal(chunk.next_offset, offset)
    }
    assert.ok(parts > 1)
    assert.equal(offset, totalCharacters)
    assert.equal(text, await readFile(finished.absolute_path, 'utf8'))
    assert.equal(Buffer.byteLength(text, 'utf8'), finished.report.bytes)
    assert.equal((text.match(/^## \d+\./gm) ?? []).length, finished.counts.records)
    assert.equal(finished.counts.records, 19)
    assert.doesNotMatch(text, /Ersetzte Aussage|aaa1111/)
    for (const item of effective) {
      for (const field of ['source_path', 'content', 'contradictions', 'extraction_limits']) {
        assert.ok(text.includes(item[field]), `${item.source_key}: ${field}`)
      }
    }
    assert.ok(text.includes(blocked.source_path))
    assert.match(text, /blockiert: Quelle nicht lesbar/)
    assert.match(text, /\*\*Inhalt:\*\* Nicht ermittelt/)
    assert.match(text, /revoked=3, orphaned=7/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('renders the report with every detail block and the final status', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    await writeRecord({ root: journalRoot, runId: run.run_id, record: record(), now: NOW })
    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: blockedRecord({
        source_key: 'kaputt.md',
        source_path: '/knowledge/sources/webdav/kaputt.md',
        source_revision: OTHER_REVISION,
      }),
      now: NOW,
    })
    const result = await assembleReport({
      root: journalRoot,
      runId: run.run_id,
      finalStatus: {
        new: 1,
        outdated: 2,
        current: 3,
        conflict: 0,
        revoked: 4,
        orphaned: 0,
        invalid: 0,
      },
      unfinished: [{ source_path: '/knowledge/sources/webdav/offen.md', blocker: 'Lauf pausiert' }],
      now: NOW,
    })
    const text = await readFile(path.join(journalRoot, 'runs', run.run_id, 'report.md'), 'utf8')
    assert.equal(result.report.path, path.join('runs', run.run_id, 'report.md'))
    assert.match(text, /# Einlesebericht run-20261003t120000z-abc123/)
    assert.match(text, /Gesamtstatus:.*new=1, outdated=2, current=3, conflict=0, revoked=4/)
    assert.match(text, /## 1. \/knowledge\/sources\/webdav\/notes\.md/)
    assert.match(text, /\*\*Commit:\*\* abc1234/)
    assert.match(text, /\*\*Geänderte Seiten:\*\* sources\/webdav\/notes\.md, overview\.md/)
    assert.match(text, /\*\*Inhalt:\*\* Aussage mit Wert 3,5 m\./)
    assert.match(text, /\*\*Widersprüche\/offene Fragen:\*\* Keine festgestellt/)
    assert.match(text, /\*\*Extraktionsgrenzen:\*\* Keine festgestellt/)
    assert.match(text, /\*\*Quelldatei unverändert:\*\* ja/)
    assert.match(text, /## 2. \/knowledge\/sources\/webdav\/kaputt\.md/)
    assert.match(text, /blockiert: Quelle nicht lesbar/)
    assert.match(text, /Unvollständige Quellen:.*offen\.md/)
    assert.match(text, /- \/knowledge\/sources\/webdav\/offen\.md: Lauf pausiert/)
    assert.equal(result.state, 'running', 'report delivery must not close a paused run')
    assert.equal((await loadRun({ root: journalRoot, runId: run.run_id })).state, 'running')
    const first = await readChunk({ root: journalRoot, runId: run.run_id, report: true })
    assert.ok(first.report.text.includes('# Einlesebericht'))

    await assert.rejects(
      finishRun({ root: journalRoot, runId: run.run_id, now: NOW }),
      /run_finish erfordert final_status/,
    )
    await assert.rejects(
      assembleReport({
        root: journalRoot,
        runId: run.run_id,
        finalStatus: { new: 1 },
        now: NOW,
      }),
      /final_status\.outdated muss eine Ganzzahl/,
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('renders an empty report explicitly', async () => {
  const run = {
    run_id: RUN_ID,
    created_at: NOW.toISOString(),
    updated_at: NOW.toISOString(),
    state: 'running',
    budget: {
      budget_tokens: 32000,
      max_sources_per_batch: 4,
      batches_dispatched: 0,
      max_batches_per_run: 12,
    },
  }
  const text = renderReport({
    run,
    records: [],
    counts: { records: 0, ingested: 0, blocked: 0 },
    finalStatus: null,
    unfinished: [],
  })
  assert.match(text, /Keine Quelle bearbeitet\./)
  assert.match(text, /Gesamtstatus:\*\* nicht abgeschlossen/)
})

test('reads a completed zero-source report through the same delivery API', async () => {
  const { root, journalRoot, run } = await runFixture()
  try {
    const result = await finishRun({
      root: journalRoot,
      runId: run.run_id,
      finalStatus: {
        new: 0,
        outdated: 0,
        current: 0,
        conflict: 0,
        revoked: 0,
        orphaned: 0,
        invalid: 0,
      },
      unfinished: [],
      now: NOW,
    })
    const { report } = await readChunk({ root: journalRoot, runId: run.run_id, report: true })
    assert.equal(report.next_offset, null)
    assert.equal(result.counts.records, 0)
    assert.match(report.text, /Keine Quelle bearbeitet\./)
    assert.equal(report.text, await readFile(result.absolute_path, 'utf8'))
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('estimates working context from measured bytes', async () => {
  assert.equal(estimateTokensFromBytes(800), 200)
  const tokens = estimateSourceTokens({ sourceBytes: 4000, wikiBytes: 4000 })
  assert.equal(tokens, 2000 + 4000)
})

test('plans bounded batches from a fresh status scan', async () => {
  const { root, journalRoot, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const items = []
    for (let index = 0; index < 4; index += 1) {
      const name = `quelle-${index}.md`
      items.push(sourceItem(name, name, `webdav/${name}/index.md`, REVISION))
      await writeSource(sourceRoot, 'webdav', name, 8000)
    }
    await writeManifest(sourceRoot, 'webdav', items)
    const { run } = await startRun({
      root: journalRoot,
      now: NOW,
      idSuffix: SUFFIX,
      budgetTokens: 20000,
      maxSourcesPerBatch: 4,
    })

    const first = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    // 4000 fixed overhead + 4 * (2000 content + 4000 overhead) = 28000 > 20000:
    // the budget, not the source count, decides how many sources fit.
    assert.equal(first.batch.length, 2)
    assert.equal(first.batch_estimate_tokens, 16000)
    assert.equal(first.remaining, 2)
    assert.equal(first.batch[0].source_key, 'quelle-0.md')
    assert.equal(first.batch[0].adapter, 'webdav')
    assert.equal(first.batch[0].estimate.tokens, 6000)

    // The worker ingests the first source; a fresh scan excludes it and the
    // journal keeps its record out of the next batch.
    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: record({
        source_key: 'quelle-0.md',
        source_path: path.join(sourceRoot, 'webdav', 'quelle-0.md'),
        wiki_path: path.join(wikiSourceRoot, 'webdav', 'quelle-0.md', 'index.md'),
      }),
      now: NOW,
    })
    await writePage(wikiSourceRoot, 'webdav/quelle-0.md/index.md', ['source_revision: ' + REVISION])
    items.shift()
    await writeManifest(sourceRoot, 'webdav', items)

    const second = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.deepEqual(
      second.batch.map((entry) => entry.source_key),
      ['quelle-1.md', 'quelle-2.md'],
    )
    assert.equal(second.batch_estimate_tokens, 16000)
    assert.equal(second.remaining, 1)

    const after = await loadRun({ root: journalRoot, runId: run.run_id })
    assert.equal(after.budget.batches_dispatched, 2)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('excludes blocked records and retries stale ingested records', async () => {
  const { root, journalRoot, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const items = [
      sourceItem('a.md', 'a.md', 'webdav/a.md/index.md', REVISION),
      sourceItem('b.md', 'b.md', 'webdav/b.md/index.md', REVISION),
    ]
    await writeManifest(sourceRoot, 'webdav', items)
    await writeSource(sourceRoot, 'webdav', 'a.md', 100)
    await writeSource(sourceRoot, 'webdav', 'b.md', 100)
    const { run } = await startRun({ root: journalRoot, now: NOW, idSuffix: SUFFIX })
    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: blockedRecord({
        source_key: 'a.md',
        source_path: path.join(sourceRoot, 'webdav', 'a.md'),
        wiki_path: path.join(wikiSourceRoot, 'webdav', 'a.md', 'index.md'),
      }),
      now: NOW,
    })
    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: record({
        source_key: 'b.md',
        source_path: path.join(sourceRoot, 'webdav', 'b.md'),
        wiki_path: path.join(wikiSourceRoot, 'webdav', 'b.md', 'index.md'),
      }),
      now: NOW,
    })

    const plan = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.deepEqual(
      plan.batch.map((entry) => entry.source_key),
      ['b.md'],
      'the blocked record is excluded, the stale ingested record is retried',
    )
    assert.ok(
      plan.warnings.some((warning) => warning.includes('veralteter Datensatz')),
      'the stale record is reported',
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('stops planning on global invalid or conflict findings', async () => {
  const { root, journalRoot, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'webdav', [
      sourceItem('a.md', 'a.md', 'webdav/a.md/index.md', REVISION),
      sourceItem('b.md', 'b.md', 'webdav/a.md/index.md', OTHER_REVISION),
    ])
    const { run } = await startRun({ root: journalRoot, now: NOW, idSuffix: SUFFIX })
    const plan = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.equal(plan.blocked, true)
    assert.deepEqual(plan.batch, [])
    assert.ok(plan.summary.conflict > 0)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('runs an oversized source alone with an explicit warning', async () => {
  const { root, journalRoot, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'webdav', [
      sourceItem('riesig.md', 'riesig.md', 'webdav/riesig.md/index.md', REVISION),
      sourceItem('klein.md', 'klein.md', 'webdav/klein.md/index.md', REVISION),
    ])
    await writeSource(sourceRoot, 'webdav', 'riesig.md', 200000)
    await writeSource(sourceRoot, 'webdav', 'klein.md', 100)
    const { run } = await startRun({ root: journalRoot, now: NOW, idSuffix: SUFFIX })
    const plan = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      budgetTokens: 10000,
      now: NOW,
    })
    assert.equal(plan.batch.length, 1)
    assert.equal(plan.batch[0].source_key, 'riesig.md')
    assert.equal(plan.batch[0].oversized, true)
    assert.ok(plan.warnings.some((warning) => warning.includes('überschreitet das Kontextbudget')))
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rolls over at the configured batch limit and finishes when nothing is left', async () => {
  const { root, journalRoot, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    await writeManifest(sourceRoot, 'webdav', [
      sourceItem('a.md', 'a.md', 'webdav/a.md/index.md', REVISION),
      sourceItem('b.md', 'b.md', 'webdav/b.md/index.md', REVISION),
    ])
    await writeSource(sourceRoot, 'webdav', 'a.md', 10)
    await writeSource(sourceRoot, 'webdav', 'b.md', 10)
    const { run } = await startRun({
      root: journalRoot,
      now: NOW,
      idSuffix: SUFFIX,
      maxBatchesPerRun: 1,
      maxSourcesPerBatch: 1,
    })
    const first = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.equal(first.batch.length, 1)

    const rollover = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.equal(rollover.rollover, true)
    assert.deepEqual(rollover.batch, [])
    assert.equal(rollover.remaining, 2)
    assert.ok(rollover.reasons[0].includes('/ingest-new'))

    const empty = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.deepEqual(empty.batch, [])

    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: blockedRecord({
        source_key: 'a.md',
        source_path: path.join(sourceRoot, 'webdav', 'a.md'),
        wiki_path: path.join(wikiSourceRoot, 'webdav', 'a.md', 'index.md'),
      }),
      now: NOW,
    })
    await writeRecord({
      root: journalRoot,
      runId: run.run_id,
      record: blockedRecord({
        source_key: 'b.md',
        source_path: path.join(sourceRoot, 'webdav', 'b.md'),
        wiki_path: path.join(wikiSourceRoot, 'webdav', 'b.md', 'index.md'),
        blocker: 'Quelle zu groß für das Kontextbudget',
      }),
      now: NOW,
    })
    const done = await planNextBatch({
      sourceRoot,
      wikiSourceRoot,
      root: journalRoot,
      runId: run.run_id,
      now: NOW,
    })
    assert.deepEqual(done.batch, [])
    assert.equal(done.remaining, 0)
    assert.equal(done.done, true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('rejects planning for unknown or finished runs', async () => {
  const { root, journalRoot, sourceRoot, wikiSourceRoot } = await fixture()
  try {
    const { run } = await startRun({ root: journalRoot, now: NOW, idSuffix: SUFFIX })
    await assert.rejects(
      planNextBatch({
        sourceRoot,
        wikiSourceRoot,
        root: journalRoot,
        runId: 'run-20261003t120000z-unbek',
        now: NOW,
      }),
      /run_id ist ungültig/,
    )
    await finishRun({
      root: journalRoot,
      runId: run.run_id,
      finalStatus: {
        new: 0,
        outdated: 0,
        current: 0,
        conflict: 0,
        revoked: 0,
        orphaned: 0,
        invalid: 0,
      },
      now: NOW,
    })
    await assert.rejects(
      planNextBatch({
        sourceRoot,
        wikiSourceRoot,
        root: journalRoot,
        runId: run.run_id,
        now: NOW,
      }),
      /bereits abgeschlossen/,
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
