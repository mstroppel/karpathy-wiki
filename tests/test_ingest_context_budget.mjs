import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { promisify } from 'node:util'

import {
  applyIngestDraft,
  prepareIngest,
  validateIngest,
} from '../config/tools/wiki_ingest_transaction_core.mjs'

import {
  WORKER_FIXED_OVERHEAD_TOKENS,
  estimateSourceTokens,
  planNextBatch,
  startRun,
  writeRecord,
} from '../config/tools/wiki_ingest_journal_core.mjs'

// Deterministic, model-free before/after evidence for issue #152. Every token
// figure here is a documented estimate (four characters per token) applied to
// synthetic fixtures of known size, never a measured model context. The same
// estimator drives the batch planner that bounds real worker sessions.

const runFile = promisify(execFile)
const NOW = new Date('2026-10-03T12:00:00.000Z')
const BUDGET_TOKENS = 32000
const SOURCE_CHARACTERS = 12 * 1024

async function fixture(count, characters = SOURCE_CHARACTERS) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-context-'))
  const journalRoot = path.join(root, 'journal')
  const sourceRoot = path.join(root, 'sources')
  const wikiSourceRoot = path.join(root, 'wiki', 'sources')
  await mkdir(journalRoot, { recursive: true })
  await mkdir(path.join(sourceRoot, 'webdav'), { recursive: true })
  await mkdir(wikiSourceRoot, { recursive: true })
  const wikiRoot = path.dirname(wikiSourceRoot)
  const git = async (...args) => (await runFile('git', args, { cwd: wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic Fixture')
  await git('config', 'user.email', 'fixture@example.invalid')
  await writeFile(path.join(wikiRoot, 'overview.md'), '# Fixture\n')
  await writeFile(path.join(wikiRoot, 'index.md'), '# Index\n')
  await writeFile(path.join(wikiRoot, 'log.md'), '# Log\n')
  await git('add', '--', 'overview.md', 'index.md', 'log.md')
  await git('commit', '-qm', 'fixture baseline')
  const revision = createHash('sha256').update('x'.repeat(characters)).digest('hex')
  const items = []
  for (let index = 0; index < count; index += 1) {
    const name = `quelle-${index}.md`
    items.push({
      source_key: name,
      source_path: name,
      wiki_path: `webdav/${name}/index.md`,
      source_revision: revision,
      frontmatter: { source_revision: revision },
      claim: { source_path: name },
    })
    await writeFile(path.join(sourceRoot, 'webdav', name), 'x'.repeat(characters), 'utf8')
  }
  await writeFile(
    path.join(sourceRoot, 'webdav', 'manifest.json'),
    JSON.stringify({
      contract: 'karpathy-wiki-provider-manifest',
      version: 1,
      source: 'webdav',
      generated_at: 1760000000,
      items,
      revoked: [],
      errors: [],
    }),
    'utf8',
  )
  return { root, journalRoot, sourceRoot, wikiSourceRoot, wikiRoot, git }
}

// One session of the previous /ingest-new flow carried every source, its pages,
// and its per-source overhead at the same time.
function singleSessionEstimate(count, characters = SOURCE_CHARACTERS) {
  return (
    WORKER_FIXED_OVERHEAD_TOKENS +
    count * estimateSourceTokens({ sourceBytes: characters, wikiBytes: 0 })
  )
}

async function measure(count, characters = SOURCE_CHARACTERS) {
  const { root, journalRoot, sourceRoot, wikiSourceRoot, wikiRoot, git } = await fixture(
    count,
    characters,
  )
  try {
    const { run } = await startRun({
      root: journalRoot,
      now: NOW,
      idSuffix: '000000',
      budgetTokens: BUDGET_TOKENS,
      // Rollover is measured separately; this run plans the whole backlog.
      maxBatchesPerRun: 0,
    })
    const plans = []
    const planned = new Set()
    for (;;) {
      const plan = await planNextBatch({
        sourceRoot,
        wikiSourceRoot,
        root: journalRoot,
        runId: run.run_id,
        now: NOW,
      })
      if (plan.batch.length === 0) break
      plans.push({
        sources: plan.batch.length,
        tokens: plan.batch_estimate_tokens,
        oversized: plan.batch.filter((entry) => entry.oversized === true).length,
      })
      for (const entry of plan.batch) {
        planned.add(entry.source_key)
        // Complete the deterministic worker transaction, so the journal and
        // next planning call observe a real verified Git commit.
        const declaredPages = [
          path.relative(wikiRoot, entry.wiki_path),
          'overview.md',
          'index.md',
          'log.md',
        ]
        const prepared = await prepareIngest({
          root: journalRoot,
          sourceRoot,
          wikiRoot,
          adapter: entry.adapter,
          sourceKey: entry.source_key,
          sourceRevision: entry.source_revision,
          changedPages: declaredPages,
        })
        const transaction = {
          root: journalRoot,
          sourceRoot,
          wikiRoot,
          preparationId: prepared.preparation_id,
        }
        await applyIngestDraft({ ...transaction, draft: '# Synthetic synthesis\n' })
        await applyIngestDraft({ ...transaction, page: 'overview.md', edits: [] })
        await applyIngestDraft({ ...transaction, page: 'index.md', edits: [] })
        await applyIngestDraft({
          ...transaction,
          page: 'log.md',
          append: `\n- Ingest ${entry.source_key}.\n`,
        })
        const { changed_pages: changedPages } = await validateIngest(transaction)
        await git('add', '--', ...changedPages)
        await git('commit', '-qm', 'ingest synthetic source')
        const commit = await git('rev-parse', 'HEAD')
        await writeRecord({
          root: journalRoot,
          sourceRoot,
          wikiRoot,
          runId: run.run_id,
          record: {
            adapter: entry.adapter,
            source_key: entry.source_key,
            source_path: entry.source_path,
            source_revision: entry.source_revision,
            wiki_path: entry.wiki_path,
            status: 'ingested',
            preparation_id: prepared.preparation_id,
            commit,
            changed_pages: changedPages,
            content: 'Synthetische Feststellung.',
            contradictions: 'Keine festgestellt',
            extraction_limits: 'Keine festgestellt',
            source_unmodified: true,
          },
          now: NOW,
        })
      }
    }
    return {
      count,
      characters,
      batches: plans.length,
      peak: Math.max(...plans.map((plan) => plan.tokens)),
      single: singleSessionEstimate(count, characters),
      planned: planned.size,
      plans,
    }
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

// A batch either fits the estimated budget or is exactly one oversized source;
// nothing is ever silently dropped.
function assertPlanShape(row) {
  for (const plan of row.plans) {
    if (plan.oversized > 0) {
      assert.equal(plan.oversized, plan.sources, 'an oversized source runs alone')
    } else {
      assert.ok(plan.tokens <= BUDGET_TOKENS, `batch estimate ${plan.tokens} exceeds the budget`)
    }
  }
}

test('bounds estimated worker context while a single session keeps growing', async () => {
  const rows = []
  for (const count of [10, 25, 50]) {
    const row = await measure(count)
    rows.push(row)
    assertPlanShape(row)
    assert.equal(row.planned, count, 'every source is planned exactly once')
    assert.ok(
      row.peak <= BUDGET_TOKENS,
      `estimated batch peak ${row.peak} stays within the ${BUDGET_TOKENS} token budget`,
    )
    assert.ok(row.single > BUDGET_TOKENS, 'the previous single-session flow exceeds the budget')
    assert.ok(
      count === 10 || row.single > rows[0].single,
      'the single-session estimate grows with the backlog',
    )
    assert.ok(count === 10 || row.peak === rows[0].peak, 'the batch peak stays flat')
  }
  // Printed as the deterministic before/after evidence quoted in
  // docs/ingest-reports.md.
  console.log(
    'geschätzte Tokens je Quelle:',
    estimateSourceTokens({ sourceBytes: SOURCE_CHARACTERS, wikiBytes: 0 }),
  )
  console.log('Quellen | Einzelsitzung (vorher) | Batch-Spitze (nachher) | Batches')
  for (const row of rows) {
    console.log(`${row.count} | ${row.single} | ${row.peak} | ${row.batches}`)
  }
})

test('keeps the batch shape bounded as source sizes grow', async () => {
  const rows = []
  for (const characters of [SOURCE_CHARACTERS, 48 * 1024, 200 * 1024]) {
    const row = await measure(10, characters)
    rows.push(row)
    assertPlanShape(row)
    assert.equal(row.planned, 10, 'every source is planned exactly once')
    assert.ok(row.single > row.peak, 'one session stays larger than one batch session')
    assert.ok(
      row.peak <= BUDGET_TOKENS || row.plans.some((plan) => plan.oversized > 0),
      'sources over budget run alone instead of inflating a batch',
    )
  }
  console.log('Bytes/Quelle | Einzelsitzung (vorher) | Batch-Spitze (nachher) | Batches')
  for (const row of rows) {
    console.log(`${row.characters} | ${row.single} | ${row.peak} | ${row.batches}`)
  }
})
