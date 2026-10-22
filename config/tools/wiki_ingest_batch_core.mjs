import { randomBytes } from 'node:crypto'
import { lstat, realpath, rm } from 'node:fs/promises'
import path from 'node:path'
import { isDeepStrictEqual } from 'node:util'
import {
  parseFrontmatterFields,
  parseManifest,
  scanIngestStatus,
} from './wiki_ingest_status_core.mjs'
import { withIngestLock, writeIngestFile } from './wiki_ingest_storage.mjs'
import {
  cleanWiki,
  createDraft,
  digest,
  dirtyPaths,
  fileBytes,
  filesUnder,
  git,
  IngestFault,
  removeDraft,
  safeDirectory,
  storageBytes,
} from './wiki_ingest_files.mjs'
import { preparePublication, publishPrepared } from './wiki_ingest_publication.mjs'

const defaults = {
  wikiRoot: '/knowledge/wiki',
  sourceRoot: '/knowledge/sources',
  root: '/knowledge/incoming/ingest-journal',
  maxDraftBytes: 32 * 1024 * 1024,
  maxRetainedBytes: 256 * 1024 * 1024,
}
const batchPattern = /^batch-[1-9][0-9]{0,15}-[a-f0-9]{12}$/
const attemptPattern = /^attempt-[a-f0-9]{24}$/
const reason = (error) => (error instanceof IngestFault ? error.code : 'source_processing_failed')
const contextHelp =
  'Die Verarbeitung dieser Quelle erreicht die Kontextgrenze des Modells (Quelle, Wiki-Kontext und Werkzeugausgaben zusammen). Quelle aufteilen, vorverarbeiten oder ein Modell mit größerem Kontext wählen; danach ausdrücklich erneut starten.'

function pending(status) {
  return Object.entries(status.adapters).flatMap(([adapter, states]) =>
    ['new', 'outdated'].flatMap((state) =>
      states[state].map((source) => ({ ...source, adapter, state })),
    ),
  )
}

async function sourceBytes(source, { sourceRoot, wikiRoot }) {
  const manifest = parseManifest(
    source.adapter,
    JSON.parse(await fileBytes(path.join(sourceRoot, source.adapter, 'manifest.json'), sourceRoot)),
  )
  const item = manifest.items.find((candidate) => candidate.source_key === source.source_key)
  if (!item) throw new IngestFault('source_removed_or_revoked')
  if (
    item.source_revision !== source.source_revision ||
    path.join(wikiRoot, 'sources', item.wiki_path) !== source.wiki_path ||
    !isDeepStrictEqual(item.frontmatter, source.frontmatter)
  )
    throw new IngestFault('source_changed')
  const bytes = await fileBytes(source.source_path, sourceRoot)
  const hash = digest(bytes)
  if (item.source_sha256 && item.source_sha256 !== hash)
    throw new IngestFault('source_digest_mismatch')
  if (source.adapter === 'paperless') {
    if (!item.source_sha256) throw new IngestFault('paperless_digest_missing')
    const fields = parseFrontmatterFields(bytes.toString('utf8'))
    if (
      Object.entries({ ...item.frontmatter, ...item.claim }).some(
        ([key, value]) => String(fields[key]) !== String(value),
      )
    )
      throw new IngestFault('paperless_metadata_mismatch')
  } else if (hash !== source.source_revision) throw new IngestFault('source_revision_mismatch')
  return bytes
}

async function verifyDigest(source, opts, expected) {
  if (!/^[a-f0-9]{64}$/.test(expected) || digest(await sourceBytes(source, opts)) !== expected)
    throw new IngestFault('source_changed')
}

function diagnosticsFor(status) {
  return Object.fromEntries(
    Object.entries(status.adapters).map(([adapter, value]) => [
      adapter,
      { invalid: value.invalid, conflict: value.conflict },
    ]),
  )
}

function reportText(state) {
  return [
    '# Einlesebericht',
    `Lauf: ${state.batch_id}`,
    `Status: ${state.status}`,
    ...state.sources.map((source) =>
      [
        `\n## ${source.source_path}`,
        `Revision: ${source.source_revision}`,
        `Wiki: ${source.wiki_path}`,
        `Status: ${source.status}`,
        `Commit: ${source.commit ?? 'Kein Commit verifiziert'}`,
        `Geänderte Seiten: ${source.changed_pages?.join(', ') ?? 'Nicht ermittelt'}`,
        `Worker: ${source.worker_session ?? 'Nicht gestartet'}`,
        `Quellschreibvorgang: ${source.source_written ? 'belegt' : 'nicht belegt'}`,
        `Werkzeugfehler: ${JSON.stringify(source.worker_errors ?? [])}`,
        source.blocker ?? '',
        source.blocker === 'source_context_limit' ? contextHelp : '',
        source.content ?? 'Keine Modellbefunde gespeichert.',
      ].join('\n\n'),
    ),
    '\n## Quellenstatus',
    JSON.stringify(state.summary),
    JSON.stringify(state.diagnostics),
    state.blocker ?? '',
    '',
  ].join('\n\n')
}

async function loadActiveState(opts) {
  const bytes = await fileBytes(path.join(opts.root, 'active.json'), opts.root, { absent: true })
  const active = bytes === null ? null : JSON.parse(bytes)
  if (!active?.batch_id) return null
  if (!batchPattern.test(active.batch_id)) throw new IngestFault('invalid_active_batch')
  const state = JSON.parse(
    await fileBytes(path.join(opts.root, 'batches', active.batch_id, 'state.json'), opts.root, {
      limit: 64 * 1024 * 1024,
    }),
  )
  // No implicit conversion of old recovery state or approvals.
  if (state.version !== 2 || state.batch_id !== active.batch_id || !Array.isArray(state.sources))
    throw new IngestFault('unsupported_batch_state')
  for (const source of state.sources) {
    if (
      !/^[a-z0-9_-]+$/.test(source.adapter) ||
      typeof source.source_key !== 'string' ||
      !/^[a-f0-9]{64}$/.test(source.source_revision) ||
      !source.source_path.startsWith(`${opts.sourceRoot}/`) ||
      !source.wiki_path.startsWith(`${opts.wikiRoot}/sources/`) ||
      path.normalize(source.source_path) !== source.source_path ||
      path.normalize(source.wiki_path) !== source.wiki_path ||
      !Array.isArray(source.attempts) ||
      source.attempts.length > 1 ||
      source.attempts.some((attempt) => !attemptPattern.test(attempt.id))
    )
      throw new IngestFault('invalid_batch_state')
  }
  return state
}

function resultFor(state, report, reportError) {
  const failed = state.sources.filter((source) => source.status === 'failed')
  return {
    status: state.status,
    batch_id: state.batch_id,
    owner_session: state.owner_session,
    commit: state.sources.findLast((source) => source.commit)?.commit ?? null,
    ingested: state.sources.filter((source) => source.status === 'ingested').length,
    failed: failed.length,
    context_limited: failed.filter((source) => source.blocker === 'source_context_limit').length,
    context_help: failed.some((source) => source.blocker === 'source_context_limit')
      ? contextHelp
      : null,
    blocker: state.blocker ?? null,
    report_path: reportError ? null : report,
    report_error: reportError,
    remaining: state.summary.new + state.summary.outdated,
    summary: state.summary,
  }
}

// Diagnosis never starts work; unfinished publication is the only retained active batch.
export async function inspectIngestBatch(input = {}) {
  const opts = { ...defaults, ...input }
  const offset = opts.offset ?? 0
  if (!Number.isInteger(offset) || offset < 0) throw new IngestFault('invalid_diagnostic_offset')
  try {
    const state = await loadActiveState(opts)
    const paths = await dirtyPaths(opts.wikiRoot)
    if (!state)
      return {
        status: 'idle',
        batch_id: null,
        dirty_paths: paths.slice(0, 100),
        dirty_count: paths.length,
      }
    const report = path.join(opts.root, 'batches', state.batch_id, 'report.md')
    const info = await lstat(report).catch((error) => {
      if (error.code !== 'ENOENT') throw error
    })
    return {
      status: state.status,
      batch_id: state.batch_id,
      owner_session: state.owner_session,
      blocker: state.blocker ?? null,
      report_path:
        info?.isFile() && info.nlink === 1 && (await realpath(report)) === report ? report : null,
      observation: 'non_atomic_read_only',
      dirty_paths: paths.slice(0, 100),
      dirty_count: paths.length,
      sources: state.sources.slice(offset, offset + 10).map((source) => ({
        wiki_path: source.wiki_path,
        status: source.status,
        blocker: source.blocker ?? null,
        worker_session: source.worker_session ?? null,
        worker_errors: (source.worker_errors ?? [])
          .slice(0, 20)
          .map(({ code, tool }) => ({ code, tool })),
      })),
      next_offset: offset + 10 < state.sources.length ? offset + 10 : null,
    }
  } catch (error) {
    throw new IngestFault(error instanceof IngestFault ? error.code : 'diagnostic_failed')
  }
}

async function compactDraft(opts, state, attempt, baseline) {
  try {
    const directory = path.join(opts.root, 'drafts', state.batch_id, attempt.id, 'wiki')
    for (const [file, bytes] of (await filesUnder(directory, opts.maxDraftBytes)).files)
      if (baseline[file] === digest(bytes)) await rm(path.join(directory, file))
  } catch {
    /* Keep uncertain evidence. It is never reused by another worker. */
  }
}

async function cleanupDraft(opts, state, attemptId, soft = false) {
  try {
    await removeDraft(opts.root, state.batch_id, attemptId)
    return null
  } catch (error) {
    if (error.code === 'ENOENT') return null
    if (soft) return 'draft_cleanup_failed'
    throw new IngestFault('draft_cleanup_failed')
  }
}

// One attempt per source. Only an interrupted live publication needs a checkpoint;
// failed private interpretations are reported, never repaired or approved in chat.
export async function runIngestBatch(input, worker) {
  const opts = { ...defaults, ...input }
  opts.scan = () =>
    scanIngestStatus({
      sourceRoot: opts.sourceRoot,
      wikiSourceRoot: path.join(opts.wikiRoot, 'sources'),
      includeCurrent: true,
    })
  await safeDirectory(path.join(opts.wikiRoot, '.git'))
  return withIngestLock(path.join(opts.wikiRoot, '.git/wiki-ingest-publication.lock'), async () => {
    await safeDirectory(opts.root)
    const activePath = path.join(opts.root, 'active.json')
    const active = await loadActiveState(opts)
    if (opts.batchId && (!batchPattern.test(opts.batchId) || active?.batch_id !== opts.batchId))
      throw new IngestFault('batch_not_active')
    if (opts.batchId && active.sources.some((source) => source.phase === 'publishing')) {
      if (typeof opts.authorize !== 'function') throw new IngestFault('recovery_requires_approval')
      // Permission rejection must propagate through the native tool boundary.
      await opts.authorize(active.batch_id)
      opts.signal?.throwIfAborted()
    }
    const status = await opts.scan()
    const selection = pending(status).filter(
      (source) => !opts.sourcePath || source.source_path === opts.sourcePath,
    )
    if (!active && opts.sourcePath && !selection.length) throw new IngestFault('source_not_pending')
    const state = active ?? {
      version: 2,
      batch_id: `batch-${Date.now()}-${randomBytes(6).toString('hex')}`,
      owner_session: opts.ownerSession ?? null,
      status: 'running',
      sources: selection.map((source) => ({
        ...source,
        status: 'pending',
        commit: null,
        attempts: [],
      })),
      summary: status.summary,
      diagnostics: diagnosticsFor(status),
    }
    opts.batchId = state.batch_id
    const directory = path.join(opts.root, 'batches', state.batch_id)
    await safeDirectory(directory)
    const report = path.join(directory, 'report.md')
    const persist = () => {
      const bytes = JSON.stringify(state, null, 2)
      if (Buffer.byteLength(bytes) > 64 * 1024 * 1024) throw new IngestFault('control_capacity')
      return writeIngestFile(path.join(directory, 'state.json'), bytes)
    }
    const finish = async () => {
      const fresh = await opts.scan()
      state.summary = fresh.summary
      state.diagnostics = diagnosticsFor(fresh)
      if (state.status !== 'cancelled')
        state.status = state.blocker
          ? 'blocked'
          : state.sources.some((source) => source.status !== 'ingested') ||
              fresh.summary.invalid ||
              fresh.summary.conflict
            ? 'partial'
            : 'done'
      await persist()
      let reportError = null
      try {
        await writeIngestFile(report, reportText(state))
      } catch {
        reportError = 'Berichtsspeicherung fehlgeschlagen'
      }
      if (!state.sources.some((source) => source.phase === 'publishing'))
        await writeIngestFile(activePath, JSON.stringify({ batch_id: null }))
      return resultFor(state, report, reportError)
    }
    await persist()
    await writeIngestFile(activePath, JSON.stringify({ batch_id: state.batch_id }))
    try {
      const publishing = state.sources.find((source) => source.phase === 'publishing')
      if (publishing) {
        state.blocker = 'publication_interrupted'
        // Ordinary new work never silently finishes uncertain live changes.
        if (!input.batchId) return finish()
        opts.attemptRoot = path.join(
          opts.root,
          'drafts',
          state.batch_id,
          publishing.publication.attempt,
        )
        if (!attemptPattern.test(publishing.publication.attempt))
          throw new IngestFault('invalid_publication')
        const validate = () => verifyDigest(publishing, opts, publishing.publication.source_hash)
        await validate()
        await publishPrepared(opts, publishing, persist, validate)
        state.blocker = null
        // Explicit recovery only completes publication, never restarts interpretation.
        for (const source of state.sources)
          if (source.status === 'pending') {
            source.status = 'failed'
            source.blocker = 'interrupted_batch'
          }
        // Bookkeeping completes first; cleanup failure only blocks later dispatch.
        state.blocker ??= await cleanupDraft(opts, state, publishing.publication.attempt, true)
        return finish()
      }
      if (active) {
        // A restarted process cannot prove a previous model attempt finished.
        // Close private-only interrupted work; a later explicit call selects it afresh.
        for (const source of state.sources) {
          if (source.status === 'ingested') {
            for (const attempt of source.attempts)
              state.blocker ??= await cleanupDraft(opts, state, attempt.id, true)
          } else if (['pending', 'running'].includes(source.status)) {
            source.status = 'failed'
            source.blocker = 'interrupted_batch'
          }
        }
        return finish()
      }
      await cleanWiki(opts.wikiRoot)
      for (const source of state.sources) {
        if (opts.signal?.aborted) {
          state.status = 'cancelled'
          break
        }
        await cleanWiki(opts.wikiRoot)
        const attempt = {
          id: `attempt-${randomBytes(12).toString('hex')}`,
          base: (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim(),
        }
        source.attempts = [attempt]
        source.status = 'running'
        source.phase = 'worker'
        await persist()
        const attemptRoot = path.join(opts.root, 'drafts', state.batch_id, attempt.id)
        let baseline = {}
        try {
          const retained = await storageBytes(
            path.join(opts.root, 'drafts'),
            opts.maxRetainedBytes,
          ).catch((error) => {
            if (error.code === 'ENOENT') return 0
            throw error
          })
          if (retained + opts.maxDraftBytes > opts.maxRetainedBytes)
            throw new IngestFault('draft_capacity')
          const fresh = await opts.scan()
          const selected = pending(fresh).find(
            (item) => item.adapter === source.adapter && item.source_key === source.source_key,
          )
          if (!selected || selected.wiki_path !== source.wiki_path)
            throw new IngestFault('source_removed_or_conflicted')
          if (
            fresh.adapters[source.adapter].invalid.some(
              (item) => item.path === source.wiki_path || item.source_key === source.source_key,
            )
          )
            throw new IngestFault('invalid_source_metadata')
          Object.assign(source, selected)
          const bytes = await sourceBytes(source, opts)
          const sourceHash = digest(bytes)
          const draft = await createDraft(
            opts,
            attemptRoot,
            attempt.base,
            path.relative(opts.wikiRoot, source.wiki_path),
          )
          await safeDirectory(
            path.dirname(path.join(draft.wiki, path.relative(opts.wikiRoot, source.wiki_path))),
          )
          baseline = draft.baseline
          const result = await worker({
            batch_id: state.batch_id,
            wiki_root: draft.wiki,
            source_lines: bytes.toString('utf8').trimEnd().split('\n').length,
            sources: [
              {
                ...selected,
                wiki_path: path.join(draft.wiki, path.relative(opts.wikiRoot, source.wiki_path)),
              },
            ],
          })
          source.content = result?.content?.slice(0, 32 * 1024)
          source.worker_session = result?.session_id
          source.worker_errors = result?.errors ?? []
          source.source_written = result?.source_written === true
          attempt.recovered_errors = result?.recovered_errors ?? []
          opts.signal?.throwIfAborted()
          if (result?.context_limit) throw new IngestFault('source_context_limit')
          if (result?.provider_failure) throw new IngestFault('provider_paused')
          if (result?.configuration_failure) throw new IngestFault('worker_configuration_paused')
          if (source.worker_errors.length) throw new IngestFault(source.worker_errors[0].code)
          if (source.content?.trimStart().startsWith('SKIP:'))
            throw new IngestFault('source_unusable')
          if (!source.source_written) throw new IngestFault('worker_no_source_write')
          if ((await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim() !== attempt.base)
            throw new IngestFault('wiki_base_changed')
          await cleanWiki(opts.wikiRoot)
          await verifyDigest(source, opts, sourceHash)
          source.publication = await preparePublication(
            opts,
            source,
            { ...attempt, wiki: draft.wiki },
            baseline,
          )
          source.publication.source_hash = sourceHash
          source.phase = 'publishing'
          await persist()
          await opts.onPhase?.('publication_prepared')
          await verifyDigest(source, opts, sourceHash)
          opts.attemptRoot = attemptRoot
          await publishPrepared(opts, source, persist, () => verifyDigest(source, opts, sourceHash))
          await cleanupDraft(opts, state, attempt.id)
        } catch (error) {
          if (source.phase === 'complete' && source.commit) {
            state.blocker = reason(error)
            break
          }
          source.status = 'failed'
          source.blocker = reason(error)
          if (opts.signal?.aborted) state.status = 'cancelled'
          if (source.phase === 'publishing') {
            state.blocker = 'publication_interrupted'
            break
          }
          await compactDraft(opts, state, attempt, baseline)
          if (
            [
              'provider_paused',
              'worker_configuration_paused',
              'draft_capacity',
              'wiki_base_changed',
              'dirty_wiki',
              'foreign_wiki_changes',
            ].includes(source.blocker)
          ) {
            state.blocker = source.blocker
            break
          }
          await persist()
        }
      }
    } catch (error) {
      state.blocker = reason(error)
    }
    return finish()
  })
}
