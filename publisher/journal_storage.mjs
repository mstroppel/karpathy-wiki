import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { lstat, readFile, readdir, realpath, rmdir, unlink } from 'node:fs/promises'
import path from 'node:path'
import { isDeepStrictEqual, promisify } from 'node:util'
import { inflateSync } from 'node:zlib'
import {
  assertCleanIngestWiki,
  checkRetainedBackups,
  confinedIngestPath,
  loadPreparation,
  PREPARATION_RE,
  savePreparation,
} from '../config/tools/wiki_ingest_transaction_core.mjs'
import {
  loadRun,
  normalizeRecord,
  renderReport,
  RUN_ID_RE,
} from '../config/tools/wiki_ingest_journal_core.mjs'
import {
  inventoryIngestTree as inventory,
  withIngestLock,
} from '../config/tools/wiki_ingest_storage.mjs'
import { validate as validateManual } from './manual_ingest.mjs'
import { validate as validateAuthoring } from './authoring.mjs'
import { loadControllerFence } from './controller_fence.mjs'

const runFile = promisify(execFile)
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')
const COMMIT = /^[0-9a-f]{40}$/
const defaults = {
  root: '/knowledge/incoming/ingest-journal',
  wikiRoot: '/knowledge/wiki',
  stateRoot: '/knowledge/publisher',
}
const refuse = (code) => {
  throw new Error(code)
}
const git = async (root, ...args) =>
  (
    await runFile('git', ['--no-replace-objects', '--literal-pathspecs', ...args], {
      cwd: root,
      encoding: 'buffer',
      maxBuffer: 32 * 1024 * 1024,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
    })
  ).stdout

// Historical verification is deliberately independent of today's provider
// revision and HEAD. The accepted commit must still be in the live history;
// every original page hash and the exact journal payload must agree.
export async function verifyAcceptedPublication({ root, wikiRoot, receipt }) {
  const pub = receipt.publication
  if (
    receipt.wiki_root !== path.resolve(wikiRoot) ||
    pub?.phase !== 'done' ||
    !COMMIT.test(pub.commit) ||
    !COMMIT.test(receipt.base_commit) ||
    !receipt.validated ||
    receipt.pending ||
    receipt.rollback_started ||
    receipt.rollback_pending ||
    receipt.rolled_back
  )
    refuse('unresolved_preparation')
  try {
    await checkRetainedBackups(wikiRoot, receipt)
  } catch {
    refuse('live_backup_evidence_changed')
  }
  await git(wikiRoot, 'merge-base', '--is-ancestor', pub.commit, 'HEAD')
  const parents = (await git(wikiRoot, 'rev-list', '--parents', '-n', '1', pub.commit))
    .toString()
    .trim()
    .split(' ')
  if (parents.length !== 2 || parents[1] !== receipt.base_commit) refuse('commit_parent_mismatch')
  const changed = (
    await git(
      wikiRoot,
      'diff-tree',
      '--no-commit-id',
      '--name-only',
      '--no-renames',
      '-r',
      '-z',
      pub.commit,
    )
  )
    .toString()
    .split('\0')
    .filter(Boolean)
    .sort()
  const record = normalizeRecord(pub.record)
  if (
    record.status !== 'ingested' ||
    record.preparation_id !== receipt.preparation_id ||
    record.commit !== pub.commit ||
    !isDeepStrictEqual(changed, Object.keys(receipt.validated).sort()) ||
    !isDeepStrictEqual(changed, [...record.changed_pages].sort())
  )
    refuse('commit_paths_mismatch')
  for (const field of ['adapter', 'source_key', 'source_revision', 'source_path', 'wiki_path'])
    if (record[field] !== receipt.source[field]) refuse('source_identity_mismatch')
  for (const [page, hash] of Object.entries({
    ...receipt.validated,
    [receipt.source.page]: receipt.applied,
  })) {
    const entry = (await git(wikiRoot, 'ls-tree', '-z', pub.commit, '--', page)).toString()
    if (
      !entry.startsWith('100644 blob ') ||
      digest(await git(wikiRoot, 'cat-file', 'blob', `${pub.commit}:${page}`)) !== hash
    )
      refuse('commit_blob_mismatch')
  }
  const run = await loadRun({ root, runId: pub.run_id })
  if (run.state !== 'completed' || run.unfinished?.length) refuse('run_not_successfully_closed')
  const matches = run.records.filter((item) => item.preparation_id === receipt.preparation_id)
  if (!matches.length || matches.some((item) => !isDeepStrictEqual(normalizeRecord(item), record)))
    refuse('journal_evidence_mismatch')
  const report = await readFile(
    await confinedIngestPath(root, `runs/${pub.run_id}/report.md`),
    'utf8',
  )
  if (
    report !==
    renderReport({
      run,
      records: run.effective,
      counts: run.counts,
      finalStatus: run.final_status,
      unfinished: run.unfinished,
    })
  )
    refuse('report_evidence_mismatch')
  return { commit: pub.commit, verified: true, run_completed_at: run.updated_at }
}

async function verifyGitOwnership(wiki, receipt, evidence, tree) {
  const baseline = receipt.publication.git_baseline
  if (!baseline?.files || !Array.isArray(baseline.directories))
    refuse('draft_git_ownership_missing')
  const expected = { ...baseline.files }
  const directories = new Set(baseline.directories)
  for (const backup of evidence.backups) {
    expected[backup.path] = backup.hash
    for (let parent = path.dirname(backup.path); parent !== '.'; parent = path.dirname(parent))
      directories.add(parent)
  }
  // Only new objects reachable from the verified accepted commit are owned.
  // Unknown/dangling objects (including earlier draft revisions) are retained,
  // not adopted just because they look like Git object filenames.
  const addedObjects = []
  const prefix = 'wiki/.git/'
  for (const [file, hash] of Object.entries(tree.files)) {
    if (!file.startsWith(prefix)) continue
    const relative = file.slice(prefix.length)
    // The index is intentionally changed by staging; verifyDraft separately
    // checks its complete contents and flags against the accepted commit.
    if (relative === 'index') continue
    if (Object.hasOwn(expected, relative)) {
      if (hash !== expected[relative]) refuse('foreign_git_metadata')
      continue
    }
    const object = /^objects\/([0-9a-f]{2})\/([0-9a-f]{38})$/.exec(relative)
    if (!object) refuse('unknown_draft_entries')
    addedObjects.push({ relative, oid: object[1] + object[2] })
    directories.add(`objects/${object[1]}`)
  }
  for (const directory of tree.directories) {
    if (directory.startsWith(prefix) && !directories.has(directory.slice(prefix.length)))
      refuse('unknown_draft_entries')
  }
  for (const file of Object.keys(expected)) {
    if (!Object.hasOwn(tree.files, prefix + file)) refuse('foreign_git_metadata')
  }
  // Check immutable metadata (especially config) before consulting Git.
  const reachable = new Set(
    (await git(wiki, 'rev-list', '--objects', '--no-object-names', receipt.publication.commit))
      .toString()
      .trim()
      .split('\n'),
  )
  for (const { relative, oid } of addedObjects) {
    if (!reachable.has(oid)) refuse('unknown_draft_entries')
    const bytes = inflateSync(await readFile(path.join(wiki, '.git', relative)))
    if (createHash('sha1').update(bytes).digest('hex') !== oid) refuse('foreign_git_metadata')
  }
}

async function verifyDraft(opts, receipt, tree) {
  const directory = path.join(opts.root, 'preparations', receipt.preparation_id)
  if (!isDeepStrictEqual((await readdir(directory)).sort(), ['journal', 'wiki']))
    refuse('unknown_draft_entries')
  if (Object.keys(tree.files).some((file) => file.endsWith('.lock')))
    refuse('draft_lock_requires_operator')
  const wiki = path.join(directory, 'wiki')
  const pub = receipt.publication
  const evidence = await loadPreparation({
    root: path.join(directory, 'journal'),
    preparationId: pub.draft_id,
  })
  if (
    !isDeepStrictEqual((await readdir(path.join(directory, 'journal'))).sort(), ['preparations']) ||
    !isDeepStrictEqual((await readdir(path.join(directory, 'journal', 'preparations'))).sort(), [
      `${pub.draft_id}.json`,
    ])
  )
    refuse('unknown_draft_entries')
  if (
    evidence.base_commit !== receipt.base_commit ||
    evidence.pending ||
    evidence.rollback_started ||
    evidence.rollback_pending ||
    !isDeepStrictEqual(evidence.baseline, receipt.baseline) ||
    !isDeepStrictEqual(evidence.validated, receipt.validated) ||
    evidence.applied !== receipt.applied
  )
    refuse('draft_evidence_mismatch')
  await verifyGitOwnership(wiki, receipt, evidence, tree)
  await checkRetainedBackups(wiki, evidence)
  for (const args of [
    ['diff', '--name-only', '-z', pub.commit],
    ['diff', '--cached', '--name-only', '-z', pub.commit],
    ['ls-files', '--others', '--exclude-standard', '-z'],
    ['ls-files', '--others', '--ignored', '--exclude-standard', '-z'],
  ])
    if ((await git(wiki, ...args)).length) refuse('foreign_draft_changes')
  if ((await git(wiki, 'rev-parse', 'HEAD')).toString().trim() !== receipt.base_commit)
    refuse('foreign_draft_head')
  const refs = (await git(wiki, 'for-each-ref', '--format=%(objectname)'))
    .toString()
    .trim()
    .split('\n')
    .filter(Boolean)
  if (refs.some((ref) => ref !== receipt.base_commit)) refuse('foreign_draft_refs')
  if (
    (await git(wiki, 'ls-files', '-v', '-z'))
      .toString()
      .split('\0')
      .filter(Boolean)
      .some((entry) => !entry.startsWith('H '))
  )
    refuse('hidden_draft_changes')
  return evidence
}

function checkRemaining(tree, marker) {
  if (
    marker.version !== 1 ||
    marker.state !== 'deleting' ||
    !Array.isArray(marker.directories) ||
    !marker.files ||
    Object.entries(tree.files).some(([file, hash]) => marker.files[file] !== hash) ||
    tree.directories.some((directory) => !marker.directories.includes(directory))
  )
    refuse('cleanup_manifest_mismatch')
}

async function compact(opts, receipt, tree) {
  const directory = path.join(opts.root, 'preparations', receipt.preparation_id)
  if (!receipt.compaction) {
    const evidence = await verifyDraft(opts, receipt, tree)
    // Durable proof + deletion intent precede the first unlink. Keep the small
    // nested transaction receipt, not a clone or duplicate page bodies.
    receipt.compaction = { version: 1, state: 'deleting', ...tree, draft_evidence: evidence }
    await savePreparation(opts.root, receipt)
  }
  const marker = receipt.compaction
  checkRemaining(await inventory(directory), marker)
  for (const [relative, hash] of Object.entries(tree.files)) {
    const file = path.join(directory, relative)
    const info = await lstat(file)
    if (!info.isFile() || info.nlink !== 1 || digest(await readFile(file)) !== hash)
      refuse('draft_changed_during_cleanup')
    await unlink(file)
  }
  for (const relative of [...tree.directories].sort((a, b) => b.length - a.length))
    await rmdir(path.join(directory, relative))
  receipt.compaction = {
    version: 1,
    state: 'done',
    bytes: marker.bytes,
    allocated_bytes: marker.allocated_bytes,
    draft_evidence: marker.draft_evidence,
  }
  delete receipt.publication.git_baseline
  await savePreparation(opts.root, receipt)
}

async function readState(opts, file) {
  try {
    return JSON.parse(await readFile(await confinedIngestPath(opts.stateRoot, file), 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') return null
    refuse('unreadable_publisher_state')
  }
}

async function inspectAndCompact(opts) {
  const stateNames = await readdir(opts.stateRoot)
  const control = await readState(opts, 'control.json')
  // Empty publisher state is the supported direct/writable runtime mode, not
  // permission to reset missing authority. Any initialized state must verify.
  if (
    stateNames.length &&
    (!control ||
      control.version !== 1 ||
      control.active !== null ||
      control.inflight !== null ||
      control.stop !== null ||
      !Array.isArray(control.finished) ||
      !control.responses ||
      typeof control.responses !== 'object' ||
      Array.isArray(control.responses) ||
      !opts.heldLocks.includes(path.join(opts.stateRoot, 'writer.lock')))
  )
    refuse('publisher_not_idle')
  const controllers = stateNames.some((name) => /^(manual|authoring|controller)[.-]/.test(name))
  if (
    controllers &&
    ['manual.lock', 'controller-execution.lock'].some(
      (name) => !opts.heldLocks.includes(path.join(opts.stateRoot, name)),
    )
  )
    refuse('controller_lock_missing')
  if (controllers) await loadControllerFence(opts.stateRoot, { required: true })
  for (const [file, validate] of [
    ['manual.json', validateManual],
    ['authoring.json', validateAuthoring],
  ]) {
    if (!controllers) continue
    const state = await readState(opts, file)
    if (!state || !['idle', 'completed'].includes(state.status) || state.status_delivery_failed)
      refuse('controller_not_idle')
    validate(state)
  }
  await assertCleanIngestWiki(opts.wikiRoot)
  try {
    await lstat(path.join(opts.wikiRoot, '.git', 'index.lock'))
    refuse('wiki_index_lock_requires_operator')
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  const preparations = path.join(opts.root, 'preparations')
  if ((await realpath(preparations)) !== preparations) refuse('unsafe_preparations_root')
  const names = (await readdir(preparations)).sort()
  const result = {
    dry_run: opts.confirmed !== true,
    keep_completed: opts.keepCompleted,
    candidates: 0,
    candidate_bytes: 0,
    reclaimed_bytes: 0,
    entries: [],
    storage: {
      proposal_files: 0,
      proposal_git: 0,
      draft_evidence: 0,
      receipts: 0,
      runs: 0,
      other: 0,
      live_backups: 0,
    },
  }
  // All output is content-free: IDs, counts, sizes and stable reason codes.
  // No paths derived from sources, source keys, text or raw errors escape.
  const all = await inventory(opts.root)
  for (const file of Object.keys(all.files)) {
    const bytes = (await lstat(path.join(opts.root, file))).size
    const category = /^preparations\/prep-[0-9a-f]{32}\/wiki\/\.git\//.test(file)
      ? 'proposal_git'
      : /^preparations\/prep-[0-9a-f]{32}\/wiki\//.test(file)
        ? 'proposal_files'
        : /^preparations\/prep-[0-9a-f]{32}\/journal\//.test(file)
          ? 'draft_evidence'
          : /^preparations\/prep-[0-9a-f]{32}\.json$/.test(file)
            ? 'receipts'
            : file.startsWith('runs/')
              ? 'runs'
              : 'other'
    result.storage[category] += bytes
  }
  for (const name of await readdir(path.join(opts.wikiRoot, '.git'))) {
    if (/^ingest-backup-[0-9a-f]+$/.test(name))
      result.storage.live_backups += (await inventory(path.join(opts.wikiRoot, '.git', name))).bytes
  }
  const candidates = []
  const receipts = names.filter((name) => /^prep-[0-9a-f]{32}\.json$/.test(name))
  for (const name of receipts) {
    const id = name.slice(0, -5)
    const entry = { preparation_id: id, bytes: 0, reason: 'unverified_evidence' }
    result.entries.push(entry)
    try {
      if (names.includes(`${name}.lock`)) refuse('preparation_lock_requires_operator')
      const receipt = await loadPreparation({ ...opts, preparationId: id })
      if (receipt.preparation_id !== id) refuse('receipt_identity_mismatch')
      const tree = await inventory(path.join(preparations, id))
      entry.bytes = tree.bytes
      // One run lock at a time: thousands of historical runs must not require
      // thousands of lock-helper processes or open descriptors.
      if (receipt.publication?.phase !== 'done' || !RUN_ID_RE.test(receipt.publication.run_id))
        refuse('unresolved_preparation')
      const runLock = await confinedIngestPath(
        opts.root,
        `runs/${receipt.publication.run_id}/records.lock`,
      )
      await withIngestLock(
        runLock,
        async () => {
          const accepted = await verifyAcceptedPublication({ ...opts, receipt })
          if (receipt.compaction?.state === 'done') {
            if (tree.directories.length) refuse('unexpected_compacted_draft')
            entry.reason = 'already_compacted'
            return
          }
          if (receipt.compaction) {
            if (!receipt.publication.git_baseline) refuse('draft_git_ownership_missing')
            checkRemaining(tree, receipt.compaction)
          } else await verifyDraft(opts, receipt, tree)
          candidates.push({ receipt, tree, entry, runLock, completedAt: accepted.run_completed_at })
          entry.reason = 'completed_verified'
        },
        { create: false },
      )
    } catch (error) {
      // Whitelist only our reason codes; Git/filesystem errors may contain
      // private paths or content. Detailed inspection stays operator-local.
      if (/^[a-z_]+$/.test(error.message)) entry.reason = error.message
      if (error.message === 'Ein anderer Schreiber hält den Ingest-Lock')
        entry.reason = 'writer_lock_unavailable'
    }
  }
  // Retention is a count of verified completed drafts, never an age rule for
  // unresolved work. Finish already-started deletion regardless of the count.
  candidates.sort(
    (a, b) =>
      b.completedAt.localeCompare(a.completedAt) ||
      b.receipt.preparation_id.localeCompare(a.receipt.preparation_id),
  )
  let kept = 0
  for (const candidate of candidates) {
    const { receipt, tree, entry } = candidate
    if (!receipt.compaction && kept++ < opts.keepCompleted) {
      entry.reason = 'retention_count'
      continue
    }
    result.candidates += 1
    result.candidate_bytes += tree.bytes
    if (opts.confirmed === true) {
      await withIngestLock(
        candidate.runLock,
        async () => {
          await verifyAcceptedPublication({ ...opts, receipt })
          if (
            !isDeepStrictEqual(
              await loadPreparation({ ...opts, preparationId: receipt.preparation_id }),
              receipt,
            )
          )
            refuse('receipt_changed_during_cleanup')
          await compact(opts, receipt, tree)
        },
        { create: false },
      )
      result.reclaimed_bytes += tree.bytes
      entry.reason = 'compacted'
    }
  }
  result.orphan_directories = names.filter(
    (name) => PREPARATION_RE.test(name) && !names.includes(`${name}.json`),
  ).length
  return result
}

// Operator-only. Match controller lock order, then exclude control/authoring
// publication. Existing lock inodes only: a dry run creates no files. Refuse
// missing initialization instead of manufacturing empty authority state.
export async function maintainJournal(input = {}) {
  const opts = { ...defaults, keepCompleted: 0, ...input }
  if (!Number.isInteger(opts.keepCompleted) || opts.keepCompleted < 0)
    refuse('invalid_retention_count')
  for (const key of ['root', 'wikiRoot', 'stateRoot']) {
    opts[key] = path.resolve(opts[key])
    if ((await realpath(opts[key])) !== opts[key]) refuse('unsafe_root')
  }
  if ((await realpath(path.join(opts.wikiRoot, '.git'))) !== path.join(opts.wikiRoot, '.git'))
    refuse('unsafe_git_root')
  const optionalLocks = [
    path.join(opts.stateRoot, 'manual.lock'),
    path.join(opts.stateRoot, 'controller-execution.lock'),
    path.join(opts.stateRoot, 'writer.lock'),
  ]
  const locks = []
  for (const file of optionalLocks) {
    try {
      await lstat(file)
      locks.push(file)
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
  }
  locks.push(path.join(opts.wikiRoot, '.git', 'wiki-ingest-publication.lock'))
  opts.heldLocks = locks
  const locked = (index) =>
    index === locks.length
      ? inspectAndCompact(opts)
      : withIngestLock(locks[index], () => locked(index + 1), { create: false })
  return locked(0)
}
