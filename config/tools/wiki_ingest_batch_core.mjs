import { randomBytes } from 'node:crypto'
import { rm } from 'node:fs/promises'
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
  relativePage,
  removeDraft,
  safeDirectory,
  putFile,
  draftChanges,
  storageBytes,
} from './wiki_ingest_files.mjs'
import { preparePublication, publishPrepared } from './wiki_ingest_publication.mjs'

const defaults = {
  wikiRoot: '/knowledge/wiki',
  sourceRoot: '/knowledge/sources',
  root: '/knowledge/incoming/ingest-journal',
  maxAttempts: 3,
  sourceBudgetMs: 300_000,
  maxDraftBytes: 32 * 1024 * 1024,
  maxRetainedBytes: 256 * 1024 * 1024,
}
const batchPattern = /^batch-[1-9][0-9]{0,15}-[a-f0-9]{12}$/
const attemptPattern = /^attempt-[a-f0-9]{24}$/
const token = () => randomBytes(12).toString('hex')
const reason = (error) => (error instanceof IngestFault ? error.code : 'source_processing_failed')
const dispatchable = new Set(['pending', 'running', 'repairing'])
const dirtyAdmission =
  'dirty_wiki: Änderungen erhalten; gezielt prüfen, niemals pauschal zurücksetzen'
const unfinished = new Set([
  'pending',
  'running',
  'repairing',
  'awaiting_user',
  'awaiting_technical',
  'ineligible',
])

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
  const bytes = await sourceBytes(source, opts)
  if (!/^[a-f0-9]{64}$/.test(expected) || digest(bytes) !== expected)
    throw new IngestFault('source_changed')
  return bytes
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
    `Checkpoint: ${state.checkpoint}`,
    ...state.sources.map((source) =>
      [
        `\n## ${source.source_path}`,
        `Revision: ${source.source_revision}`,
        `Wiki: ${source.wiki_path}`,
        `Status: ${source.status}`,
        `Commit: ${source.commit ?? 'Kein Commit verifiziert'}`,
        `Geänderte Seiten: ${(source.changed_pages ?? []).join(', ')}`,
        `Worker: ${source.worker_session ?? 'Nicht ermittelt'}`,
        `Quellschreibvorgang: ${source.source_written === true ? 'belegt' : 'nicht belegt'}`,
        `Werkzeugfehler: ${JSON.stringify(source.worker_errors ?? [])}`,
        `Versuche: ${JSON.stringify(source.attempts ?? [])}`,
        source.blocker ?? '',
        source.content ??
          (source.commit
            ? 'Befunde und Fundstellen stehen in der verifizierten Quellenseite.'
            : 'Keine veröffentlichte Verarbeitung belegt.'),
      ].join('\n\n'),
    ),
    '\n## Offene Entscheidungen',
    JSON.stringify(state.questions ?? []),
    '\n## Quellenstatus',
    JSON.stringify(state.summary),
    JSON.stringify(state.diagnostics),
    state.blocker ?? '',
    '',
  ].join('\n\n')
}

async function jsonFile(file, root) {
  return JSON.parse(await fileBytes(file, root, { limit: 64 * 1024 * 1024 }))
}

function checkState(state, opts) {
  if (state.version !== 1 || !batchPattern.test(state.batch_id) || !Array.isArray(state.sources))
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
      source.attempts.some((item) => !attemptPattern.test(item.id))
    )
      throw new IngestFault('invalid_batch_state')
  }
}

async function snapshot(opts) {
  const paths = await dirtyPaths(opts.wikiRoot)
  const files = []
  for (const file of paths.slice(0, 100)) {
    try {
      const bytes = await fileBytes(path.join(opts.wikiRoot, relativePage(file)), opts.wikiRoot, {
        absent: true,
      })
      files.push({ path: file, hash: bytes === null ? null : digest(bytes) })
    } catch {
      files.push({ path: file, hash: 'unsafe_or_unavailable' })
    }
  }
  return {
    head: (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim(),
    index: (await git(opts.wikiRoot, ['write-tree'])).trim(),
    files,
    count: paths.length,
  }
}

async function questionSnapshot(opts, state, index) {
  const current = await opts.scan()
  const source = state.sources[index ?? state.adoption?.source_index] ?? null
  const selected = source
    ? ['new', 'outdated', 'current', 'revoked', 'conflict'].flatMap((name) =>
        (current.adapters[source.adapter]?.[name] ?? []).filter(
          (item) => item.source_key === source.source_key,
        ),
      )
    : current
  const selection = source
    ? (pending(current).find(
        (item) =>
          item.adapter === source.adapter &&
          item.source_key === source.source_key &&
          item.wiki_path === source.wiki_path,
      ) ?? null)
    : null
  let sourceHash = null
  if (source) {
    try {
      sourceHash = digest(
        await fileBytes(selection?.source_path ?? source.source_path, opts.sourceRoot),
      )
    } catch {
      sourceHash = 'unavailable'
    }
  }
  const drafts = []
  for (const attempt of source?.attempts ?? []) {
    try {
      const directory = path.join(opts.root, 'drafts', state.batch_id, attempt.id)
      const { files } = await filesUnder(directory, opts.maxRetainedBytes)
      drafts.push([...files].map(([name, bytes]) => [name, digest(bytes)]).sort())
    } catch {
      drafts.push('unavailable')
    }
  }
  const binding = digest(
    JSON.stringify({
      batch: state.batch_id,
      cancelled: state.status === 'cancelled',
      index,
      source,
      selected,
      sourceHash,
      drafts,
      proposal: {
        adoption: state.adoption,
        publications: state.sources.map((item) => item.publication),
        blocker: state.blocker,
      },
      wiki: await snapshot(opts),
    }),
  )
  return { binding, selection, source_hash: sourceHash }
}

async function addQuestions(opts, state) {
  const needs =
    state.status === 'cancelled'
      ? []
      : state.sources.flatMap((source, index) =>
          ['awaiting_user', 'awaiting_technical', 'ineligible'].includes(source.status) &&
          source.phase !== 'publishing'
            ? [index]
            : [],
        )
  if (state.blocker) needs.unshift(null)
  const questions = []
  for (const index of needs) {
    const observation = await questionSnapshot(opts, state, index)
    const { binding, selection } = observation
    const source = state.sources[index ?? state.adoption?.source_index]
    const old = state.questions?.find(
      (item) => item.source_index === index && item.binding === binding,
    )
    questions.push(
      old ?? {
        id: token(),
        source_index: index,
        binding,
        selection,
        source_hash: observation.source_hash,
        reason: index === null ? state.blocker : state.sources[index].blocker,
        target: !source
          ? null
          : {
              adapter: source.adapter,
              source_key: source.source_key,
              source_revision: selection?.source_revision ?? source.source_revision,
              wiki_path: source.wiki_path,
            },
        proposal:
          state.status === 'cancelled'
            ? 'Nur die bereits vorbereitete Veröffentlichung technisch abschließen; der übrige Auftrag bleibt abgebrochen. Keine Worker starten.'
            : index === null
              ? state.adoption
                ? 'Genau diese unveränderte Quellenseite frisch verarbeiten; fremde Änderungen erhalten.'
                : 'Blocker extern klären; denselben gebundenen Auftrag fortsetzen, nichts zurücksetzen.'
              : 'Eigene Entwürfe prüfen und begrenzt weiterbearbeiten; alternativ ausdrücklich zurückstellen oder nach Klärung ungelöst beenden.',
        options:
          state.status === 'cancelled'
            ? ['retry']
            : index === null
              ? state.adoption
                ? ['reprocess', 'retry', 'cancel']
                : ['retry', 'cancel']
              : ['retry', 'defer', 'unresolved', 'cancel'],
      },
    )
  }
  state.questions = questions
}

async function refreshAdmission(opts, state) {
  // Installed publication files are checkpoint-owned, not new dirty admission.
  if (state.sources.some((source) => source.phase === 'publishing')) return
  const admission = await snapshot(opts)
  const dirty =
    admission.count ||
    admission.index !== (await git(opts.wikiRoot, ['rev-parse', 'HEAD^{tree}'])).trim()
  let adoption = null
  if (dirty) {
    const selected = pending(await opts.scan())
    const sourceIndex = state.sources.findIndex(
      (source) =>
        dispatchable.has(source.status) &&
        admission.count === 1 &&
        source.wiki_path === path.join(opts.wikiRoot, admission.files[0].path) &&
        selected.some(
          (item) =>
            item.adapter === source.adapter &&
            item.source_key === source.source_key &&
            item.wiki_path === source.wiki_path,
        ),
    )
    if (sourceIndex !== -1 && /^[a-f0-9]{64}$/.test(admission.files[0].hash))
      adoption = {
        path: admission.files[0].path,
        hash: admission.files[0].hash,
        source_index: sourceIndex,
        approved: false,
      }
    if (
      adoption &&
      isDeepStrictEqual(state.admission, admission) &&
      isDeepStrictEqual({ ...state.adoption, approved: false }, adoption)
    )
      adoption.approved = state.adoption.approved
  }
  state.admission = dirty ? admission : null
  state.adoption = adoption
  if (dirty && !adoption?.approved) state.blocker = dirtyAdmission
  else if (state.blocker === dirtyAdmission) state.blocker = null
}

async function compactDraft(opts, state, attempt, baseline) {
  const directory = path.join(opts.root, 'drafts', state.batch_id, attempt.id, 'wiki')
  try {
    const { files } = await filesUnder(directory, opts.maxDraftBytes)
    for (const [file, bytes] of files) {
      if (baseline[file] === digest(bytes)) await rm(path.join(directory, file))
    }
  } catch {
    // Uncertain evidence is retained, never deleted to get below a quota.
  }
}

function repairFromAttempt(opts, batchId, attempt) {
  if (!attempt?.repair_hashes) return null
  return {
    wiki: path.join(opts.root, 'drafts', batchId, attempt.id, 'wiki'),
    files: Object.keys(attempt.repair_hashes),
    hashes: attempt.repair_hashes,
    base: attempt.base,
    sourceHash: attempt.source_hash,
  }
}

async function retainedBytes(opts) {
  const root = path.join(opts.root, 'drafts')
  try {
    return await storageBytes(root, opts.maxRetainedBytes)
  } catch (error) {
    if (error.code === 'ENOENT') return 0
    throw error
  }
}

function resultFor(state, report, reportError, offset = 0) {
  return {
    status: state.status,
    batch_id: state.batch_id,
    commit: state.sources.findLast((source) => source.commit)?.commit ?? null,
    ingested: state.sources.filter((source) => source.status === 'ingested').length,
    repaired: state.sources.filter(
      (source) => source.status === 'ingested' && source.attempts.length > 1,
    ).length,
    skipped: state.sources.filter((source) => source.status === 'deferred').length,
    awaiting_user: state.sources.filter((source) => source.status === 'awaiting_user').length,
    awaiting_technical: state.sources.filter((source) => source.status === 'awaiting_technical')
      .length,
    unresolved: state.sources.filter((source) => source.status === 'unresolved').length,
    blocker: state.blocker ?? null,
    report_path: reportError ? null : report,
    report_error: reportError,
    remaining: state.summary.new + state.summary.outdated,
    summary: state.summary,
    questions: (state.questions ?? []).slice(offset, offset + 10).map((question) => {
      const visible = { ...question }
      delete visible.binding
      delete visible.selection
      delete visible.source_hash
      return visible
    }),
    next_question_offset: offset + 10 < (state.questions?.length ?? 0) ? offset + 10 : null,
    admission: state.admission ?? null,
  }
}

// One explicit accepted selection. The same implementation owns drafts, finite
// repair, bound decisions and publication; there is no background controller.
export async function runIngestBatch(input, worker) {
  const opts = { ...defaults, ...input }
  if (
    !Number.isInteger(opts.maxAttempts) ||
    opts.maxAttempts < 1 ||
    opts.maxAttempts > 3 ||
    !Number.isInteger(opts.sourceBudgetMs) ||
    opts.sourceBudgetMs < 1 ||
    opts.sourceBudgetMs > 300_000
  )
    throw new IngestFault('invalid_retry_budget')
  if (
    opts.questionOffset !== undefined &&
    (!Number.isInteger(opts.questionOffset) || opts.questionOffset < 0)
  )
    throw new IngestFault('invalid_question_offset')
  if (
    opts.questionOffset !== undefined &&
    (!opts.batchId || opts.decisionId !== undefined || opts.answer !== undefined)
  )
    throw new IngestFault('invalid_question_request')
  if (
    (opts.decisionId !== undefined || opts.answer !== undefined) &&
    (!opts.batchId || !opts.decisionId || !opts.answer)
  )
    throw new IngestFault('incomplete_decision')
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
    const activeBytes = await fileBytes(activePath, opts.root, { absent: true })
    const pointer = activeBytes === null ? null : JSON.parse(activeBytes)
    const active = pointer?.batch_id === null ? null : pointer
    if (active && !batchPattern.test(active.batch_id)) throw new IngestFault('invalid_active_batch')
    if (opts.batchId && (!batchPattern.test(opts.batchId) || active?.batch_id !== opts.batchId))
      throw new IngestFault('batch_not_active')
    const status = await opts.scan()
    let state
    if (active) {
      state = await jsonFile(
        path.join(opts.root, 'batches', active.batch_id, 'state.json'),
        opts.root,
      )
      checkState(state, opts)
    } else {
      if (opts.batchId) throw new IngestFault('batch_not_active')
      const selection = pending(status).filter(
        (source) => !opts.sourcePath || source.source_path === opts.sourcePath,
      )
      if (opts.sourcePath && !selection.length) throw new IngestFault('source_not_pending')
      if (!selection.length && !status.summary.invalid && !status.summary.conflict) {
        try {
          await cleanWiki(opts.wikiRoot)
          return { status: 'done', remaining: 0, summary: status.summary }
        } catch {
          /* Dirty admission still gets a durable, actionable diagnosis. */
        }
      }
      state = {
        version: 1,
        batch_id: `batch-${Date.now()}-${randomBytes(6).toString('hex')}`,
        owner_session: opts.ownerSession ?? null,
        status: 'running',
        checkpoint: (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim(),
        sources: selection.map((source) => ({
          ...source,
          status: 'pending',
          commit: null,
          attempts: [],
        })),
        summary: status.summary,
        questions: [],
        diagnostics: diagnosticsFor(status),
      }
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
    const recordPublication = (source) => {
      if (source.phase === 'complete') {
        source.blocker = null
        state.adoption = null
        state.admission = null
      }
      // Clear consumed admission in the same durable record as source success,
      // before source_recorded can be interrupted.
      return persist()
    }
    const reclaimDrafts = async (source) => {
      for (const attempt of source.attempts)
        await removeDraft(opts.root, state.batch_id, attempt.id).catch((error) => {
          if (error.code !== 'ENOENT') throw error
        })
    }
    let reportError = null
    const finish = async () => {
      if (state.status === 'cancelled')
        for (const source of state.sources)
          if (unfinished.has(source.status)) source.status = 'cancelled'
      const fresh = await opts.scan()
      state.summary = fresh.summary
      state.diagnostics = diagnosticsFor(fresh)
      if (state.status !== 'cancelled') await refreshAdmission(opts, state)
      if (state.status !== 'cancelled' && (state.summary.invalid || state.summary.conflict))
        state.blocker ??= 'provider_or_metadata_diagnostics'
      else if (state.blocker === 'provider_or_metadata_diagnostics') state.blocker = null
      if (
        state.status !== 'cancelled' &&
        state.sources.some((source) => source.status === 'pending')
      )
        state.blocker ??= 'continuation_available'
      if (
        state.status === 'cancelled' &&
        !state.sources.some((source) => source.phase === 'publishing')
      )
        state.questions = []
      else await addQuestions(opts, state)
      if (state.status !== 'cancelled')
        state.status = state.questions.length
          ? 'paused'
          : state.sources.some((source) => source.status !== 'ingested') ||
              state.summary.invalid ||
              state.summary.conflict
            ? 'partial'
            : 'done'
      await persist()
      try {
        await writeIngestFile(report, reportText(state))
      } catch {
        reportError = 'Berichtsspeicherung fehlgeschlagen'
      }
      if (!state.questions.length && !state.sources.some((source) => source.phase === 'publishing'))
        await writeIngestFile(activePath, JSON.stringify({ batch_id: null }))
      return resultFor(state, report, reportError, opts.questionOffset ?? 0)
    }
    // Only the pointer's absence/null denotes no active accepted work.
    await persist()
    await writeIngestFile(activePath, JSON.stringify({ batch_id: state.batch_id }))
    if (state.status === 'cancelled' && !opts.decisionId) return finish()
    if (active && !opts.decisionId) {
      for (const source of state.sources) {
        if (['running', 'repairing'].includes(source.status)) {
          source.status = 'awaiting_user'
          source.blocker = 'interrupted_attempt'
        }
      }
      if (state.sources.some((source) => source.phase === 'publishing'))
        state.blocker = 'publication_interrupted'
      return finish()
    }
    if (opts.decisionId) {
      if (state.owner_session !== (opts.ownerSession ?? null))
        throw new IngestFault('decision_wrong_session')
      const question = state.questions.find((item) => item.id === opts.decisionId)
      if (!question || !question.options.includes(opts.answer))
        throw new IngestFault('decision_not_open')
      if (question.binding !== (await questionSnapshot(opts, state, question.source_index)).binding)
        return finish()
      if (typeof opts.authorize !== 'function') throw new IngestFault('decision_requires_approval')
      await opts.authorize(`${state.batch_id}/${question.id}/${opts.answer}`)
      opts.signal?.throwIfAborted()
      if (question.binding !== (await questionSnapshot(opts, state, question.source_index)).binding)
        return finish()
      if (opts.answer === 'cancel') {
        state.status = 'cancelled'
        state.questions = []
        for (const source of state.sources)
          if (unfinished.has(source.status)) source.status = 'cancelled'
        state.blocker = state.sources.some((source) => source.phase === 'publishing')
          ? 'publication_interrupted'
          : null
        return finish()
      }
      if (question.source_index === null) {
        if (opts.answer === 'reprocess') {
          if (
            !state.adoption ||
            !question.selection ||
            !isDeepStrictEqual(state.admission, await snapshot(opts))
          )
            return finish()
          state.adoption.approved = true
          const source = state.sources[state.adoption.source_index]
          Object.assign(source, question.selection)
          source.approval = {
            revision: question.selection.source_revision,
            frontmatter: question.selection.frontmatter,
            digest: question.source_hash,
          }
        }
        state.blocker = null
      } else {
        const source = state.sources[question.source_index]
        source.status =
          opts.answer === 'retry' ? 'pending' : opts.answer === 'defer' ? 'deferred' : 'unresolved'
        if (opts.answer === 'retry') source.blocker = null
        if (opts.answer === 'retry' && question.selection) {
          Object.assign(source, question.selection)
          source.approval = {
            revision: question.selection.source_revision,
            frontmatter: question.selection.frontmatter,
            digest: question.source_hash,
          }
        }
      }
      state.questions = state.questions.filter((item) => item.id !== question.id)
      await persist()
      if (state.blocker || ['defer', 'unresolved'].includes(opts.answer)) return finish()
    }
    try {
      const publishing = state.sources.find((source) => source.phase === 'publishing')
      if (publishing) {
        await verifyDigest(publishing, opts, publishing.publication.source_hash)
        opts.attemptRoot = path.join(
          opts.root,
          'drafts',
          state.batch_id,
          publishing.publication.attempt,
        )
        await publishPrepared(
          opts,
          publishing,
          () => recordPublication(publishing),
          () => verifyDigest(publishing, opts, publishing.publication.source_hash),
        )
        await reclaimDrafts(publishing)
        if (state.status === 'cancelled') return finish()
      }
      await refreshAdmission(opts, state)
      if (state.admission && !state.adoption?.approved) return finish()
      // A confirmed historical dirty source is freshly processed first, not
      // stamped as success or treated as owned because its path looks familiar.
      const ordered = [...state.sources].sort(
        (left, right) =>
          Number(right.wiki_path === path.join(opts.wikiRoot, state.adoption?.path ?? '')) -
          Number(left.wiki_path === path.join(opts.wikiRoot, state.adoption?.path ?? '')),
      )
      let dispatchPause = null
      for (const source of ordered) {
        if (!dispatchable.has(source.status)) continue
        if (opts.signal?.aborted) {
          state.status = 'cancelled'
          source.status = 'cancelled'
          break
        }
        if (dispatchPause) {
          source.status = 'awaiting_technical'
          source.blocker = dispatchPause
          continue
        }
        const deadline = Date.now() + opts.sourceBudgetMs
        let repairDraft = repairFromAttempt(
          opts,
          state.batch_id,
          source.attempts.findLast((attempt) => attempt.repair_hashes),
        )
        let runtimeRetries = 0
        let modelCalls = 0
        for (let attemptNumber = 0; attemptNumber < opts.maxAttempts; attemptNumber++) {
          const attempt = {
            id: `attempt-${token()}`,
            base: (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim(),
            started_at: Date.now(),
          }
          source.attempts.push(attempt)
          source.status = attemptNumber ? 'repairing' : 'running'
          source.phase = 'worker'
          await persist()
          const attemptRoot = path.join(opts.root, 'drafts', state.batch_id, attempt.id)
          let baseline = {}
          let retry = true
          try {
            if (Date.now() >= deadline) throw new IngestFault('source_time_budget')
            if (modelCalls >= 96) throw new IngestFault('source_model_budget')
            if ((await retainedBytes(opts)) + opts.maxDraftBytes > opts.maxRetainedBytes)
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
              if (!state.adoption?.approved) throw new IngestFault('invalid_source_metadata')
            if (
              source.approval &&
              (selected.source_revision !== source.approval.revision ||
                !isDeepStrictEqual(selected.frontmatter, source.approval.frontmatter))
            )
              throw new IngestFault('source_changed')
            Object.assign(source, selected)
            const sourceHash = digest(await sourceBytes(source, opts))
            if (source.approval && sourceHash !== source.approval.digest)
              throw new IngestFault('source_changed')
            attempt.source_hash = sourceHash
            await persist()
            const draft = await createDraft(
              opts,
              attemptRoot,
              state.adoption?.approved ? state.adoption : null,
              attempt.base,
            )
            await safeDirectory(
              path.dirname(path.join(draft.wiki, path.relative(opts.wikiRoot, source.wiki_path))),
            )
            baseline = draft.baseline
            if (
              repairDraft &&
              repairDraft.base === attempt.base &&
              repairDraft.sourceHash === sourceHash
            ) {
              for (const file of repairDraft.files) {
                const bytes = await fileBytes(path.join(repairDraft.wiki, file), repairDraft.wiki)
                if (digest(bytes) !== repairDraft.hashes[file])
                  throw new IngestFault('draft_changed')
                await putFile(draft.wiki, file, bytes)
              }
            } else repairDraft = null
            const result = await worker({
              batch_id: state.batch_id,
              wiki_root: draft.wiki,
              timeout_ms: Math.min(120_000, deadline - Date.now()),
              source_lines: (await sourceBytes(source, opts)).toString('utf8').trimEnd().split('\n')
                .length,
              repair_reason: source.blocker ?? null,
              repair_files: repairDraft?.files ?? [],
              retry_budget: Math.max(0, 2 - runtimeRetries),
              model_call_budget: 96 - modelCalls,
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
            runtimeRetries += result?.runtime_retries ?? 0
            modelCalls += result?.model_calls ?? 0
            source.source_written = result?.source_written === true
            attempt.errors = source.worker_errors
            attempt.worker_session = result?.session_id
            attempt.model_calls = result?.model_calls ?? 0
            attempt.runtime_retries = result?.runtime_retries ?? 0
            attempt.elapsed_ms = Date.now() - attempt.started_at
            attempt.recovered_errors = result?.recovered_errors ?? []
            opts.signal?.throwIfAborted()
            if (result?.provider_failure) {
              dispatchPause = 'provider_paused'
              throw new IngestFault('provider_paused')
            }
            if (result?.configuration_failure) {
              dispatchPause = 'worker_configuration_paused'
              throw new IngestFault(dispatchPause)
            }
            if (result?.errors?.length)
              throw new IngestFault(
                result.errors.find((item) => item.code === 'worker_permission_denied')?.code ??
                  result.errors[0].code,
              )
            if (source.content?.trimStart().startsWith('SKIP:'))
              throw new IngestFault('source_unusable')
            if (!source.source_written) throw new IngestFault('worker_no_source_write')
            if ((await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim() !== attempt.base)
              throw new IngestFault('wiki_base_changed')
            if (digest(await sourceBytes(source, opts)) !== sourceHash)
              throw new IngestFault('source_changed')
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
            // Source identity/digest are rechecked after durable preparation.
            await verifyDigest(source, opts, sourceHash)
            opts.attemptRoot = attemptRoot
            await publishPrepared(
              opts,
              source,
              () => recordPublication(source),
              () => verifyDigest(source, opts, sourceHash),
            )
            await reclaimDrafts(source)
            break
          } catch (error) {
            if (source.phase === 'complete' && source.commit) {
              source.status = 'ingested'
              break
            }
            source.blocker = reason(error)
            attempt.elapsed_ms = Date.now() - attempt.started_at
            attempt.reason = source.blocker
            source.status = 'awaiting_user'
            if (opts.signal?.aborted) {
              state.status = 'cancelled'
              source.status = 'cancelled'
              retry = false
            }
            if (source.phase === 'publishing') {
              state.blocker = 'publication_interrupted'
              retry = false
            } else {
              repairDraft = null
              try {
                const wiki = path.join(attemptRoot, 'wiki')
                const files = await draftChanges(
                  wiki,
                  baseline,
                  path.relative(opts.wikiRoot, source.wiki_path),
                  opts.maxDraftBytes,
                )
                await verifyDigest(source, opts, attempt.source_hash)
                attempt.repair_hashes = Object.fromEntries(
                  await Promise.all(
                    files.map(async (file) => [
                      file,
                      digest(await fileBytes(path.join(wiki, file), wiki)),
                    ]),
                  ),
                )
                repairDraft = repairFromAttempt(opts, state.batch_id, attempt)
                // If source identity changed, prior interpretation cannot be reused.
                if (source.blocker === 'source_changed') repairDraft = null
              } catch {
                /* Invalid or unbound drafts are never propagated. */
              }
              await compactDraft(opts, state, attempt, baseline)
              if (
                [
                  'worker_permission_denied',
                  'worker_source_truncated',
                  'source_unusable',
                  'source_removed_or_revoked',
                  'source_removed_or_conflicted',
                  'invalid_source_metadata',
                  'provider_paused',
                  'worker_configuration_paused',
                  'draft_capacity',
                ].includes(source.blocker)
              )
                retry = false
              if (
                ['provider_paused', 'worker_configuration_paused', 'draft_capacity'].includes(
                  source.blocker,
                )
              )
                source.status = 'awaiting_technical'
              if (
                ['source_removed_or_revoked', 'source_removed_or_conflicted'].includes(
                  source.blocker,
                )
              )
                source.status = 'ineligible'
              if (source.blocker === 'source_changed') retry = true
            }
            await persist()
            if (!retry || Date.now() >= deadline || attemptNumber + 1 === opts.maxAttempts) break
          }
        }
        if (state.blocker || state.status === 'cancelled' || state.adoption) break
      }
    } catch (error) {
      state.blocker = reason(error)
    }
    return finish()
  })
}
