import { randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import { appendFile, mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import {
  PREPARATION_RE,
  assertCleanIngestWiki,
  assertIngestRolledBack,
  confinedIngestPath,
  verifyIngestCommit,
} from './wiki_ingest_transaction_core.mjs'

import {
  RESULT_NAMES,
  REVISION_RE,
  checkRelativePath,
  scanIngestStatus,
} from './wiki_ingest_status_core.mjs'

// Durable per-source result records ("journal") for wiki ingestion runs.
// The journal keeps verified per-source details out of model context: workers
// write one record per processed source, the orchestrator plans bounded
// batches from it, and the final report is assembled here without loading
// source content into any session. Everything below is model-independent and
// deterministic; token figures are documented estimates (CHARS_PER_TOKEN),
// never measured model tokens.

export const JOURNAL_CONTRACT = 'karpathy-wiki-ingest-run'
export const JOURNAL_VERSION = 1
export const RUN_ID_RE = /^run-\d{8}t\d{6}z-[0-9a-f]{6}$/
export const RUN_STATES = ['running', 'completed']
export const RECORD_STATUSES = ['ingested', 'blocked']
export const JOURNAL_OUTPUT_BUDGET_BYTES = 12 * 1024
export const RECORD_MAX_BYTES = 8192
export const CHUNK_BYTES_DEFAULT = 4096
export const CHUNK_BYTES_MAX = 8192
export const COMMIT_RE = /^[0-9a-f]{7,40}$/

// Working-context budget model (estimates). One worker session costs a fixed
// instruction/tool overhead plus a per-source allowance (index, overview, log,
// diffs, commits, affected pages) plus the estimated source and target-page
// content. Operators raise the budget for larger models; see
// docs/ingest-reports.md.
export const CHARS_PER_TOKEN = 4
export const WORKER_FIXED_OVERHEAD_TOKENS = 4000
export const PER_SOURCE_OVERHEAD_TOKENS = 4000
export const DEFAULT_BUDGET_TOKENS = 32000
export const DEFAULT_MAX_SOURCES_PER_BATCH = 4
export const DEFAULT_MAX_BATCHES_PER_RUN = 12

// One source of truth for budget field limits: core validation and the tool
// input schema both take their bounds from here.
export const BUDGET_LIMITS = {
  budget_tokens: [1000, 1000000],
  max_sources_per_batch: [1, 25],
  max_batches_per_run: [0, 1000],
}

const RUNS_DIRECTORY = 'runs'
const RUN_FILENAME = 'run.json'
const RECORDS_FILENAME = 'records.jsonl'
const REPORT_FILENAME = 'report.md'
const ADAPTER_RE = /^[a-z0-9_-]+$/
const MAX_TEXT_FIELD_CHARACTERS = 4000

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

function checkBudgetField(value, field) {
  const [minimum, maximum] = BUDGET_LIMITS[field]
  return checkInteger(value, field, minimum, maximum)
}

function checkInteger(value, label, minimum, maximum) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${label} muss eine Ganzzahl zwischen ${minimum} und ${maximum} sein`)
  }
  return value
}

function checkText(value, label, { allowEmpty = false, required = true } = {}) {
  if (value === undefined || value === null) {
    if (required) throw new Error(`${label} fehlt`)
    return undefined
  }
  if (typeof value !== 'string') throw new Error(`${label} ist kein Text`)
  // eslint-disable-next-line no-control-regex -- control characters are rejected on purpose
  if (/[\u0000-\u001f\u007f]/.test(value)) throw new Error(`${label} enthält Steuerzeichen`)
  if (!allowEmpty && value.trim() === '') throw new Error(`${label} ist leer`)
  if (Array.from(value).length > MAX_TEXT_FIELD_CHARACTERS) {
    throw new Error(`${label} überschreitet ${MAX_TEXT_FIELD_CHARACTERS} Zeichen`)
  }
  return value
}

function checkAbsolutePath(value, label) {
  checkText(value, label)
  if (!path.isAbsolute(value) || value.includes('\0') || value.split('/').includes('..')) {
    throw new Error(`${label} ist kein absoluter Pfad`)
  }
  return value
}

function nowIso(now) {
  return new Date(now).toISOString()
}

function runIdFor(now, suffix) {
  const stamp = nowIso(now)
    .replace(/[-:]/g, '')
    .replace(/\.\d+Z$/, 'z')
    .toLowerCase()
  return `run-${stamp}-${suffix}`
}

function runDirectory(root, runId) {
  if (typeof runId !== 'string' || !RUN_ID_RE.test(runId)) {
    throw new Error('run_id ist ungültig')
  }
  return path.join(root, RUNS_DIRECTORY, runId)
}

async function readJsonFile(filePath, label) {
  try {
    return JSON.parse(await readFile(filePath, 'utf8'))
  } catch (error) {
    throw new Error(`${label} ist unlesbar (${filePath}): ${errorMessage(error)}`, { cause: error })
  }
}

async function readRunFile(root, runId) {
  const directory = runDirectory(root, runId)
  return readJsonFile(path.join(directory, RUN_FILENAME), 'run.json')
}

function checkRunContract(run, runId) {
  if (
    run === null ||
    typeof run !== 'object' ||
    run.contract !== JOURNAL_CONTRACT ||
    run.version !== JOURNAL_VERSION ||
    run.run_id !== runId
  ) {
    throw new Error(`run.json entspricht nicht ${JOURNAL_CONTRACT} v${JOURNAL_VERSION}`)
  }
  return run
}

async function readRecordLines(root, runId) {
  const filePath = path.join(runDirectory(root, runId), RECORDS_FILENAME)
  let raw
  try {
    raw = await readFile(filePath, 'utf8')
  } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw new Error(`records.jsonl ist unlesbar (${filePath}): ${errorMessage(error)}`, {
      cause: error,
    })
  }
  return raw
    .split('\n')
    .filter((line) => line.trim() !== '')
    .map((line, index) => {
      try {
        return JSON.parse(line)
      } catch (error) {
        throw new Error(`records.jsonl Zeile ${index + 1} ist kein JSON: ${errorMessage(error)}`, {
          cause: error,
        })
      }
    })
}

function recordKey(record) {
  return `${record.adapter}\u0000${record.source_key}\u0000${record.source_revision}`
}

// One entry per source identity, holding the latest write (a re-ingested
// source supersedes its earlier record instead of duplicating it). Insertion
// order follows the first write, which is the processing order.
function effectiveRecords(records) {
  const byKey = new Map()
  for (const [index, record] of records.entries()) {
    const key = recordKey(record)
    const previous = byKey.get(key)
    byKey.set(key, { ...record, record_index: index, writes: (previous?.writes ?? 0) + 1 })
  }
  return [...byKey.values()]
}

function recordCounts(records) {
  const counts = { records: records.length, ingested: 0, blocked: 0 }
  for (const record of records) counts[record.status] += 1
  return counts
}

// The run file never carries the loaded records; every writer drops them here.
function runFileView(run) {
  return {
    contract: run.contract,
    version: run.version,
    run_id: run.run_id,
    created_at: run.created_at,
    updated_at: run.updated_at,
    state: run.state,
    budget: run.budget,
    final_status: run.final_status,
    unfinished: run.unfinished,
    report: run.report,
    recovery: run.recovery,
  }
}

async function writeRunFile(root, run) {
  const directory = runDirectory(root, run.run_id)
  await mkdir(directory, { recursive: true })
  await writeFile(path.join(directory, RUN_FILENAME), `${JSON.stringify(run, null, 2)}\n`, 'utf8')
  return run
}

function normalizeBudget({ budgetTokens, maxSourcesPerBatch, maxBatchesPerRun }) {
  return {
    budget_tokens: checkBudgetField(budgetTokens ?? DEFAULT_BUDGET_TOKENS, 'budget_tokens'),
    max_sources_per_batch: checkBudgetField(
      maxSourcesPerBatch ?? DEFAULT_MAX_SOURCES_PER_BATCH,
      'max_sources_per_batch',
    ),
    max_batches_per_run: checkBudgetField(
      maxBatchesPerRun ?? DEFAULT_MAX_BATCHES_PER_RUN,
      'max_batches_per_run',
    ),
    batches_dispatched: 0,
    worker_fixed_overhead_tokens: WORKER_FIXED_OVERHEAD_TOKENS,
    per_source_overhead_tokens: PER_SOURCE_OVERHEAD_TOKENS,
    chars_per_token: CHARS_PER_TOKEN,
  }
}

export function estimateTokensFromBytes(bytes) {
  checkInteger(bytes, 'bytes', 0, Number.MAX_SAFE_INTEGER)
  return Math.ceil(bytes / CHARS_PER_TOKEN)
}

export function estimateSourceTokens({ sourceBytes = 0, wikiBytes = 0 }) {
  return estimateTokensFromBytes(sourceBytes + wikiBytes) + PER_SOURCE_OVERHEAD_TOKENS
}

async function listRunIds(root) {
  try {
    const entries = await readdir(path.join(root, RUNS_DIRECTORY), { withFileTypes: true })
    return entries
      .filter((entry) => entry.isDirectory() && RUN_ID_RE.test(entry.name))
      .map((entry) => entry.name)
      .sort()
  } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw new Error(`Laufverzeichnis ist unlesbar: ${errorMessage(error)}`, { cause: error })
  }
}

async function loadActiveRun({ root }) {
  const runs = []
  for (const runId of await listRunIds(root)) {
    const run = checkRunContract(await readRunFile(root, runId), runId)
    if (run.state === 'running') runs.push(run)
  }
  if (runs.length === 0) return null
  runs.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
  return runs[0]
}

// Start a run, or adopt the open one so an interrupted run resumes with its
// records intact. Budget values are recorded once at start.
export async function startRun({
  root,
  budgetTokens,
  maxSourcesPerBatch,
  maxBatchesPerRun,
  resume = true,
  now = Date.now(),
  idSuffix,
} = {}) {
  const stamp = new Date(now)
  if (Number.isNaN(stamp.getTime())) throw new Error('now ist kein gültiger Zeitpunkt')
  if (resume) {
    const open = await loadActiveRun({ root })
    if (open) return { run: open, adopted: true }
  }
  const budget = normalizeBudget({ budgetTokens, maxSourcesPerBatch, maxBatchesPerRun })
  const runId = runIdFor(stamp, idSuffix ?? randomBytes(3).toString('hex'))
  const run = {
    contract: JOURNAL_CONTRACT,
    version: JOURNAL_VERSION,
    run_id: runId,
    created_at: nowIso(stamp),
    updated_at: nowIso(stamp),
    state: 'running',
    budget,
    final_status: null,
    unfinished: [],
    report: null,
  }
  await writeRunFile(root, run)
  return { run, adopted: false }
}

export async function loadRun({ root, runId }) {
  const run = checkRunContract(await readRunFile(root, runId), runId)
  const records = await readRecordLines(root, runId)
  return {
    ...run,
    records,
    effective: effectiveRecords(records),
    counts: recordCounts(effectiveRecords(records)),
  }
}

function normalizeRecord(record) {
  if (record === null || typeof record !== 'object' || Array.isArray(record)) {
    throw new Error('record muss ein Objekt sein')
  }
  const status = record.status
  if (!RECORD_STATUSES.includes(status)) throw new Error('status ist ungültig')
  const adapter = checkText(record.adapter, 'adapter')
  if (!ADAPTER_RE.test(adapter)) throw new Error('adapter ist ungültig')
  const normalized = {
    adapter,
    source_key: checkText(record.source_key, 'source_key'),
    source_path: checkAbsolutePath(record.source_path, 'source_path'),
    source_revision: record.source_revision,
    wiki_path: checkAbsolutePath(record.wiki_path, 'wiki_path'),
    status,
    preparation_id: record.preparation_id ?? null,
    commit: null,
    changed_pages: [],
    content: checkText(record.content, 'content', { required: false }) ?? null,
    contradictions: checkText(record.contradictions, 'contradictions', { required: false }) ?? null,
    extraction_limits:
      checkText(record.extraction_limits, 'extraction_limits', { required: false }) ?? null,
    source_unmodified: record.source_unmodified === true,
    blocker: checkText(record.blocker, 'blocker', { required: false }) ?? null,
  }
  if (!REVISION_RE.test(normalized.source_revision)) {
    throw new Error('source_revision ist kein SHA-256-Hash')
  }
  if (record.commit !== undefined && record.commit !== null) {
    if (typeof record.commit !== 'string' || !COMMIT_RE.test(record.commit)) {
      throw new Error('commit ist kein Git-Hash')
    }
    normalized.commit = record.commit
  }
  if (record.changed_pages !== undefined && record.changed_pages !== null) {
    if (!Array.isArray(record.changed_pages)) throw new Error('changed_pages muss eine Liste sein')
    normalized.changed_pages = record.changed_pages.map((page, index) =>
      checkRelativePath(page, `changed_pages[${index}]`),
    )
    if (normalized.changed_pages.length === 0 && status === 'ingested') {
      throw new Error('changed_pages muss eine nicht-leere Liste sein')
    }
  }
  if (status === 'ingested') {
    // An ingested record is the durable evidence of one verified commit.
    // Anything unverified is reported as blocked instead of being softened.
    if (normalized.commit === null) throw new Error('commit fehlt für status ingested')
    if (normalized.changed_pages.length === 0) {
      throw new Error('changed_pages fehlt für status ingested')
    }
    if (normalized.content === null) throw new Error('content fehlt für status ingested')
    if (normalized.contradictions === null) {
      throw new Error('contradictions fehlt für status ingested')
    }
    if (normalized.extraction_limits === null) {
      throw new Error('extraction_limits fehlt für status ingested')
    }
    if (!normalized.source_unmodified) {
      throw new Error('source_unmodified muss für status ingested bestätigt sein')
    }
    if (normalized.blocker !== null) throw new Error('blocker ist mit status ingested unvereinbar')
    if (!PREPARATION_RE.test(normalized.preparation_id)) {
      throw new Error('preparation_id fehlt oder ist ungültig für status ingested')
    }
  } else {
    if (normalized.blocker === null) throw new Error('blocker fehlt für status blocked')
  }
  return normalized
}

// Append one per-source result record. Records are append-only; a later write
// for the same source identity supersedes the earlier one for reporting.
export async function writeRecord({
  root,
  runId,
  record,
  now = Date.now(),
  sourceRoot,
  wikiRoot,
  verify = verifyIngestCommit,
}) {
  const directory = runDirectory(root, runId)
  await confinedIngestPath(root, path.relative(root, path.join(directory, RUN_FILENAME)))
  const run = checkRunContract(await readRunFile(root, runId), runId)
  if (run.state !== 'running') {
    throw new Error(
      `Lauf ${runId} ist ${run.state}; Aufzeichnungen sind nur für laufende Läufe möglich`,
    )
  }
  const normalized = { ...normalizeRecord(record), recorded_at: nowIso(now) }
  let line = JSON.stringify(normalized)
  let bytes = Buffer.byteLength(line, 'utf8')
  if (bytes > RECORD_MAX_BYTES) {
    throw new Error(
      `Datensatz ist ${bytes} Bytes groß und überschreitet das Budget von ${RECORD_MAX_BYTES} Bytes; kürze die Detailtexte, statt Inhalte still zu verlieren`,
    )
  }
  if (normalized.status === 'ingested') {
    const verified = await verify({
      root,
      record: normalized,
      ...(sourceRoot === undefined ? {} : { sourceRoot }),
      ...(wikiRoot === undefined ? {} : { wikiRoot }),
    })
    normalized.commit = verified.commit
    normalized.source_unmodified = verified.source_unmodified
    line = JSON.stringify(normalized)
    bytes = Buffer.byteLength(line, 'utf8')
    if (bytes > RECORD_MAX_BYTES) {
      throw new Error(
        `Verifizierter Datensatz überschreitet das Budget von ${RECORD_MAX_BYTES} Bytes`,
      )
    }
  }
  const recordPath = await confinedIngestPath(
    root,
    path.relative(root, path.join(directory, RECORDS_FILENAME)),
    true,
  )
  await appendFile(recordPath, `${line}\n`, {
    encoding: 'utf8',
    flag: constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | constants.O_NOFOLLOW,
  })
  const records = await readRecordLines(root, runId)
  await writeRunFile(root, { ...runFileView(run), updated_at: nowIso(now) })
  return {
    record_index: records.length - 1,
    bytes,
    counts: recordCounts(effectiveRecords(records)),
  }
}

function compactRecord(record) {
  return {
    record_index: record.record_index,
    adapter: record.adapter,
    source_key: record.source_key,
    source_path: record.source_path,
    source_revision: record.source_revision,
    commit: record.commit,
    status: record.status,
    ...(record.blocker === null ? {} : { blocker: record.blocker }),
    writes: record.writes,
  }
}

// Bounded listing of the effective records, shrunk to the response budget so
// a large backlog can never flood model context.
export async function listRecords({ root, runId, offset = 0, limit = 10 }) {
  checkInteger(offset, 'offset', 0, Number.MAX_SAFE_INTEGER)
  checkInteger(limit, 'limit', 1, 25)
  const { effective, counts } = await loadRun({ root, runId })
  for (let pageLimit = limit; pageLimit > 0; pageLimit -= 1) {
    const records = effective.slice(offset, offset + pageLimit).map(compactRecord)
    const hasMore = effective.length > offset + pageLimit
    const page = {
      counts,
      records,
      page: {
        offset,
        limit: pageLimit,
        has_more: hasMore,
        next_offset: hasMore ? offset + pageLimit : null,
      },
    }
    if (Buffer.byteLength(JSON.stringify(page), 'utf8') <= JOURNAL_OUTPUT_BUDGET_BYTES) return page
  }
  throw new Error(
    'Journalseiten überschreiten das Ausgabebudget; verkleinere limit oder rufe Datensätze stückweise ab',
  )
}

function chunkText(text, { offset, chunkBytes, wrap }) {
  const characters = Array.from(text)
  checkInteger(offset, 'chunk_offset', 0, Number.MAX_SAFE_INTEGER)
  if (offset > characters.length) throw new Error('chunk_offset liegt hinter dem Datensatz')
  let end = offset
  let size = 0
  while (end < characters.length) {
    const characterBytes = Buffer.byteLength(characters[end], 'utf8')
    if (size + characterBytes > chunkBytes) break
    size += characterBytes
    end += 1
  }
  return wrap({
    offset,
    next_offset: end < characters.length ? end : null,
    total_characters: characters.length,
    text: characters.slice(offset, end).join(''),
  })
}

// Chunked retrieval of one raw journal line or of the assembled report, so
// details can be quoted later without ever reloading source content.
export async function readChunk({
  root,
  runId,
  recordIndex,
  report = false,
  offset = 0,
  chunkBytes = CHUNK_BYTES_DEFAULT,
} = {}) {
  checkInteger(chunkBytes, 'chunk_bytes', 4, CHUNK_BYTES_MAX)
  if (report && recordIndex !== undefined) {
    throw new Error('report und record_index können nicht kombiniert werden')
  }
  if (!report && recordIndex === undefined) {
    throw new Error('read erfordert record_index oder report')
  }
  await loadRun({ root, runId })
  let payload
  if (report) {
    const filePath = path.join(runDirectory(root, runId), REPORT_FILENAME)
    let text
    try {
      text = await readFile(filePath, 'utf8')
    } catch (error) {
      throw new Error(`Bericht ist unlesbar (${filePath}): ${errorMessage(error)}`, {
        cause: error,
      })
    }
    payload = chunkText(text, {
      offset,
      chunkBytes,
      wrap: (chunk) => ({ run_id: runId, report: chunk }),
    })
  } else {
    const records = await readRecordLines(root, runId)
    checkInteger(recordIndex, 'record_index', 0, Math.max(0, records.length - 1))
    if (records[recordIndex] === undefined) throw new Error('record_index ist unbekannt')
    payload = chunkText(JSON.stringify(records[recordIndex]), {
      offset,
      chunkBytes,
      wrap: (chunk) => ({ run_id: runId, record: chunk }),
    })
  }
  if (Buffer.byteLength(JSON.stringify(payload), 'utf8') > JOURNAL_OUTPUT_BUDGET_BYTES) {
    throw new Error('chunk_bytes überschreitet das Ausgabebudget')
  }
  return payload
}

function statusLine(finalStatus) {
  if (finalStatus === null) return 'nicht abgeschlossen'
  return RESULT_NAMES.map((name) => `${name}=${finalStatus[name]}`).join(', ')
}

function reportBlock(record, position) {
  const lines = [`## ${position}. ${record.source_path}`, '']
  lines.push(`- **Quellschlüssel:** ${record.adapter}/${record.source_key}`)
  lines.push(`- **Quellrevision:** ${record.source_revision}`)
  lines.push(`- **Commit:** ${record.commit ?? 'Kein Commit'}`)
  lines.push(`- **Geänderte Seiten:** ${record.changed_pages.join(', ') || 'Keine'}`)
  lines.push(`- **Inhalt:** ${record.content ?? 'Nicht ermittelt'}`)
  lines.push(`- **Widersprüche/offene Fragen:** ${record.contradictions ?? 'Nicht ermittelt'}`)
  lines.push(`- **Extraktionsgrenzen:** ${record.extraction_limits ?? 'Nicht ermittelt'}`)
  lines.push(`- **Quelldatei unverändert:** ${record.source_unmodified ? 'ja' : 'nicht bestätigt'}`)
  lines.push(
    `- **Status:** ${record.status === 'ingested' ? 'einglesen' : `blockiert: ${record.blocker}`}`,
  )
  lines.push('')
  return lines.join('\n')
}

export function renderReport({ run, records, counts, finalStatus = null, unfinished = [], now }) {
  const lines = [
    `# Einlesebericht ${run.run_id}`,
    '',
    `- **Lauf:** ${run.run_id} (Start ${run.created_at}, Stand ${now ?? run.updated_at}, ${
      run.state === 'completed' ? 'abgeschlossen' : 'laufend'
    })`,
    `- **Budget:** ${run.budget.budget_tokens} geschätzte Tokens je Arbeitssitzung, ${
      run.budget.max_sources_per_batch
    } Quellen je Batch, ${run.budget.batches_dispatched} Batches, ${
      run.budget.max_batches_per_run
    } Batches je Lauf (0 = unbegrenzt)`,
    `- **Verarbeitete Quellen:** ${counts.records} (einglesen: ${counts.ingested}, blockiert: ${
      counts.blocked
    })`,
    `- **Gesamtstatus:** ${statusLine(finalStatus)}`,
    `- **Unvollständige Quellen:** ${
      unfinished.length === 0 ? 'Keine' : unfinished.map((entry) => entry.source_path).join(', ')
    }`,
    '',
  ]
  if (unfinished.length > 0) {
    for (const entry of unfinished) {
      lines.push(`- ${entry.source_path}: ${entry.blocker}`)
    }
    lines.push('')
  }
  if (records.length === 0) {
    lines.push('Keine Quelle bearbeitet.', '')
  }
  records.forEach((record, index) => lines.push(reportBlock(record, index + 1)))
  return `${lines
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trimEnd()}\n`
}

function normalizeFinalStatus(finalStatus) {
  if (finalStatus === null || typeof finalStatus !== 'object' || Array.isArray(finalStatus)) {
    throw new Error('final_status muss ein Objekt mit allen Statuszahlen sein')
  }
  const normalized = {}
  for (const name of RESULT_NAMES) {
    normalized[name] = checkInteger(finalStatus[name], `final_status.${name}`, 0, 1000000)
  }
  return normalized
}

function normalizeUnfinished(unfinished, blockedRecords) {
  const entries = []
  const seen = new Set()
  const add = (sourcePath, blocker) => {
    const key = `${sourcePath}\u0000${blocker}`
    if (seen.has(key)) return
    seen.add(key)
    entries.push({ source_path: sourcePath, blocker })
  }
  if (unfinished !== undefined && unfinished !== null) {
    if (!Array.isArray(unfinished)) throw new Error('unfinished muss eine Liste sein')
    for (const [index, entry] of unfinished.entries()) {
      if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
        throw new Error(`unfinished[${index}] muss ein Objekt sein`)
      }
      add(
        checkAbsolutePath(entry.source_path, `unfinished[${index}].source_path`),
        checkText(entry.blocker, `unfinished[${index}].blocker`),
      )
    }
  }
  for (const record of blockedRecords) add(record.source_path, record.blocker)
  return entries
}

// Assemble the authoritative private report from durable records. The orchestrator
// links it after ingestion (or at rollover); bounded reads are available on request.
export async function assembleReport({
  root,
  runId,
  finalStatus,
  unfinished,
  completed = false,
  now = Date.now(),
} = {}) {
  const stamp = new Date(now)
  if (Number.isNaN(stamp.getTime())) throw new Error('now ist kein gültiger Zeitpunkt')
  const run = await loadRun({ root, runId })
  const normalizedStatus =
    finalStatus === undefined ? run.final_status : normalizeFinalStatus(finalStatus)
  const blockedRecords = run.effective.filter((record) => record.status === 'blocked')
  const normalizedUnfinished = normalizeUnfinished(
    unfinished === undefined ? run.unfinished : unfinished,
    blockedRecords,
  )
  const state = completed ? 'completed' : run.state
  const text = renderReport({
    run: { ...run, state },
    records: run.effective,
    counts: run.counts,
    finalStatus: normalizedStatus,
    unfinished: normalizedUnfinished,
    now: nowIso(stamp),
  })
  const directory = runDirectory(root, runId)
  await mkdir(directory, { recursive: true })
  await writeFile(path.join(directory, REPORT_FILENAME), text, 'utf8')
  const update = runFileView({
    ...run,
    state,
    updated_at: nowIso(stamp),
    final_status: normalizedStatus,
    unfinished: normalizedUnfinished,
    report: {
      path: path.relative(root, path.join(directory, REPORT_FILENAME)),
      bytes: Buffer.byteLength(text, 'utf8'),
      assembled_at: nowIso(stamp),
    },
  })
  await writeRunFile(root, update)
  return {
    run_id: runId,
    state: update.state,
    report: update.report,
    counts: run.counts,
    unfinished: normalizedUnfinished,
    absolute_path: path.join(directory, REPORT_FILENAME),
  }
}

// Close a run: the report must carry a complete final status and every
// unfinished source before the run can be marked completed.
export async function finishRun(options = {}) {
  const { finalStatus } = options
  if (finalStatus === undefined) throw new Error('run_finish erfordert final_status')
  return assembleReport({ ...options, completed: true })
}

function pendingEntries(status) {
  const entries = []
  for (const [adapter, result] of Object.entries(status.adapters)) {
    for (const state of ['new', 'outdated']) {
      for (const entry of result[state]) entries.push({ adapter, state, entry })
    }
  }
  return entries
}

async function fileSize(filePath) {
  try {
    const info = await stat(filePath)
    return info.isFile() ? info.size : 0
  } catch (error) {
    if (error?.code === 'ENOENT') return 0
    throw new Error(`Größe konnte nicht ermittelt werden (${filePath}): ${errorMessage(error)}`, {
      cause: error,
    })
  }
}

// A skip is an explicit recovery decision, not a successful ingestion. It only
// acknowledges this exact record: a later failure stops planning again.
export async function skipBlockedSource({
  root,
  runId,
  recordIndex,
  confirmed,
  wikiRoot = '/knowledge/wiki',
  now = Date.now(),
}) {
  if (confirmed !== true) throw new Error('Auslassen benötigt ausdrückliche Bestätigung')
  checkInteger(recordIndex, 'record_index', 0, Number.MAX_SAFE_INTEGER)
  const run = await loadRun({ root, runId })
  if (run.state !== 'running') throw new Error('Lauf ist bereits abgeschlossen')
  const record = run.effective.find(
    (entry) => entry.record_index === recordIndex && entry.status === 'blocked',
  )
  if (!record) throw new Error('Kein aktueller blockierter Datensatz an record_index')
  await assertCleanIngestWiki(wikiRoot)
  if (record.preparation_id) {
    await assertIngestRolledBack({ root, wikiRoot, preparationId: record.preparation_id })
  }
  const skipped = [...new Set([...(run.recovery?.skipped_records ?? []), recordIndex])]
  await writeRunFile(
    root,
    runFileView({
      ...run,
      updated_at: nowIso(now),
      recovery: { skipped_records: skipped },
    }),
  )
  return {
    run_id: runId,
    skipped_record: recordIndex,
    status: 'blocked',
    recovery_required: run.effective.some(
      (entry) => entry.status === 'blocked' && !skipped.includes(entry.record_index),
    ),
  }
}

// Plan the next bounded batch: a fresh status scan every call, journal-aware
// exclusions, and a volume budget instead of a bare source count. One call
// returns one worker assignment; the batch counter drives run rollover.
export async function planNextBatch({
  sourceRoot,
  wikiSourceRoot,
  root,
  runId,
  budgetTokens,
  maxSourcesPerBatch,
  scan = scanIngestStatus,
  now = Date.now(),
} = {}) {
  const run = await loadRun({ root, runId })
  if (run.state !== 'running') throw new Error(`Lauf ${runId} ist bereits abgeschlossen`)
  const budget = {
    budget_tokens: checkBudgetField(budgetTokens ?? run.budget.budget_tokens, 'budget_tokens'),
    max_sources_per_batch: checkBudgetField(
      maxSourcesPerBatch ?? run.budget.max_sources_per_batch,
      'max_sources_per_batch',
    ),
    fixed_overhead_tokens: run.budget.worker_fixed_overhead_tokens,
    per_source_overhead_tokens: run.budget.per_source_overhead_tokens,
  }

  const status = await scan({ sourceRoot, wikiSourceRoot, includeCurrent: false })
  const summary = status.summary
  const base = {
    run_id: runId,
    summary,
    budget_tokens: budget.budget_tokens,
    batch_estimate_tokens: 0,
    pending_total: summary.new + summary.outdated,
    warnings: [],
    blocked: false,
    rollover: false,
  }
  if (summary.invalid > 0 || summary.conflict > 0) {
    return {
      ...base,
      batch: [],
      remaining: base.pending_total,
      blocked: true,
      reasons: [
        'globale invalid/conflict-Befunde liegen vor; vor Änderungen stoppen und Diagnosen melden',
      ],
    }
  }

  const unacknowledged = run.effective.filter(
    (record) =>
      record.status === 'blocked' && !run.recovery?.skipped_records.includes(record.record_index),
  )
  if (unacknowledged.length) {
    return {
      ...base,
      batch: [],
      remaining: base.pending_total,
      blocked: true,
      recovery_required: true,
      reasons: [
        'Quellenfehler: Reparatur oder bestätigtes Auslassen erforderlich; keine weiteren Batches',
      ],
    }
  }

  const excluded = new Set()
  for (const record of run.effective) {
    if (record.status === 'blocked') excluded.add(recordKey(record))
    if (record.status === 'ingested') {
      // A record whose source is still pending describes a commit that did not
      // stick: reprocess instead of trusting a stale record.
      const key = recordKey(record)
      const stillPending = pendingEntries(status).some(
        (item) => recordKey({ ...item.entry, adapter: item.adapter }) === key,
      )
      if (stillPending)
        base.warnings.push(`veralteter Datensatz ohne Wirkung: ${record.source_path}`)
      else excluded.add(key)
    }
  }

  const open = pendingEntries(status).filter(
    (item) => !excluded.has(recordKey({ ...item.entry, adapter: item.adapter })),
  )
  if (open.length === 0) {
    return {
      ...base,
      batch: [],
      remaining: 0,
      done: true,
    }
  }
  const maxBatches = run.budget.max_batches_per_run ?? 0
  if (maxBatches > 0 && run.budget.batches_dispatched >= maxBatches) {
    return {
      ...base,
      batch: [],
      remaining: open.length,
      rollover: true,
      reasons: [`Lauf hat das Batch-Limit von ${maxBatches} erreicht; mit /ingest-new fortsetzen`],
    }
  }

  const batch = []
  let estimateTokens = budget.fixed_overhead_tokens
  let budgetSpent = false
  let remaining = 0
  for (const { adapter, state, entry } of open) {
    if (batch.length >= budget.max_sources_per_batch || budgetSpent) {
      remaining += 1
      continue
    }
    const sourceBytes = await fileSize(entry.source_path)
    const wikiBytes = await fileSize(entry.wiki_path)
    const sourceTokens = estimateSourceTokens({ sourceBytes, wikiBytes })
    const candidate = {
      ...entry,
      adapter,
      state,
      estimate: { source_bytes: sourceBytes, wiki_bytes: wikiBytes, tokens: sourceTokens },
    }
    if (batch.length === 0 && estimateTokens + sourceTokens > budget.budget_tokens) {
      // A single oversized source is never silently dropped: it runs alone and
      // must use validated staged reading, or report an actionable blocker.
      candidate.oversized = true
      candidate.reason =
        'Quelle überschreitet das Kontextbudget allein; nur stückweises, validiertes Lesen oder ein konkreter Blocker sind zulässig'
      base.warnings.push(`Quelle überschreitet das Kontextbudget: ${entry.source_path}`)
      batch.push(candidate)
      // The estimate reports the truth, even when it exceeds the budget: an
      // oversized source runs alone and pays that cost in its own session.
      estimateTokens += sourceTokens
      budgetSpent = true
      continue
    }
    if (estimateTokens + sourceTokens > budget.budget_tokens) {
      budgetSpent = true
      remaining += 1
      continue
    }
    estimateTokens += sourceTokens
    batch.push(candidate)
  }

  if (batch.length === 0) {
    return {
      ...base,
      batch: [],
      remaining,
      done: remaining === 0,
    }
  }

  const updated = runFileView({
    ...run,
    budget: { ...run.budget, batches_dispatched: run.budget.batches_dispatched + 1 },
    updated_at: nowIso(now),
  })
  await writeRunFile(root, updated)
  return {
    ...base,
    batch,
    batch_estimate_tokens: estimateTokens,
    remaining,
    done: false,
  }
}
