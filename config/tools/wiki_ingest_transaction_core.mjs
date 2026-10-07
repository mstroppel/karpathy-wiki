import { execFile } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import { link, lstat, mkdir, open, readdir, realpath, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import { isDeepStrictEqual, promisify } from 'node:util'

import {
  REVISION_RE,
  checkRelativePath,
  parseFrontmatterFields,
  parseManifest,
  scanIngestStatus,
} from './wiki_ingest_status_core.mjs'

const runFile = promisify(execFile)
export const PREPARATION_RE = /^prep-[0-9a-f]{32}$/
const COMMIT_RE = /^[0-9a-f]{7,40}$/
const DEFAULTS = {
  root: '/knowledge/incoming/ingest-journal',
  sourceRoot: '/knowledge/sources',
  wikiRoot: '/knowledge/wiki',
}

function options(input) {
  return { ...DEFAULTS, ...input }
}

function hash(bytes) {
  return createHash('sha256').update(bytes).digest('hex')
}

// Reject symlinks, including intermediate directories and configured roots.
// Missing suffixes are allowed only for new files; no source is ever written.
export async function confinedIngestPath(root, relative, missing = false) {
  checkRelativePath(relative)
  const absoluteRoot = path.resolve(root)
  if ((await realpath(absoluteRoot)) !== absoluteRoot) throw new Error('root enthält Symlinks')
  let current = absoluteRoot
  const parts = relative.split('/')
  for (const [index, part] of parts.entries()) {
    if (part === '.git') throw new Error('.git ist kein erlaubter Inhaltspfad')
    current = path.join(current, part)
    try {
      const info = await lstat(current)
      if (info.isSymbolicLink()) throw new Error(`Symlink ist nicht erlaubt: ${current}`)
      if (index < parts.length - 1 && !info.isDirectory())
        throw new Error('Pfad ist kein Verzeichnis')
      if (index === parts.length - 1 && !info.isFile()) throw new Error('Pfad ist keine Datei')
    } catch (error) {
      if (missing && error.code === 'ENOENT') continue
      throw error
    }
  }
  return current
}

const confined = confinedIngestPath

async function bytesAt(root, relative, missing = false) {
  const destination = await confined(root, relative, missing)
  try {
    const file = await open(destination, constants.O_RDONLY | constants.O_NOFOLLOW)
    try {
      return await file.readFile()
    } finally {
      await file.close()
    }
  } catch (error) {
    if (missing && error.code === 'ENOENT') return null
    throw error
  }
}

async function fileHash(root, relative) {
  const destination = await confined(root, relative)
  const file = await open(destination, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const digest = createHash('sha256')
    for await (const bytes of file.createReadStream({ autoClose: false })) digest.update(bytes)
    return digest.digest('hex')
  } finally {
    await file.close()
  }
}

async function git(wikiRoot, args) {
  const { stdout } = await runFile(
    'git',
    ['--no-replace-objects', '--literal-pathspecs', ...args],
    {
      cwd: wikiRoot,
      encoding: 'buffer',
      maxBuffer: 32 * 1024 * 1024,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
    },
  )
  return stdout
}

async function commitHash(wikiRoot, ref = 'HEAD') {
  if (ref !== 'HEAD' && (typeof ref !== 'string' || !COMMIT_RE.test(ref))) {
    throw new Error('commit ist kein Git-Hash')
  }
  const result = (
    await git(wikiRoot, ['rev-parse', '--verify', '--end-of-options', `${ref}^{commit}`])
  )
    .toString()
    .trim()
  if (!/^[0-9a-f]{40}$/.test(result)) throw new Error('Git-Commit ist ungültig')
  return result
}

async function cleanIndex(wikiRoot) {
  if ((await git(wikiRoot, ['diff', '--cached', '--name-only', '-z'])).length) {
    throw new Error('Fremde staged Änderungen: Index muss leer bleiben')
  }
}

async function freshSource({ sourceRoot, wikiRoot, adapter, sourceKey, sourceRevision }) {
  if (typeof adapter !== 'string' || !/^[a-z0-9_-]+$/.test(adapter)) {
    throw new Error('adapter ist ungültig')
  }
  if (typeof sourceKey !== 'string' || !sourceKey) throw new Error('source_key fehlt')
  if (typeof sourceRevision !== 'string' || !REVISION_RE.test(sourceRevision)) {
    throw new Error('source_revision ist kein SHA-256-Hash')
  }
  const manifest = parseManifest(
    adapter,
    JSON.parse((await bytesAt(sourceRoot, `${adapter}/manifest.json`)).toString('utf8')),
  )
  const item = manifest.items.find((candidate) => candidate.source_key === sourceKey)
  if (!item) throw new Error('Quelle ist nicht veröffentlicht oder widerrufen')
  if (item.source_revision !== sourceRevision)
    throw new Error('source_revision weicht vom frischen Status ab')
  const sourcePath = `${adapter}/${item.source_path}`
  if ((await fileHash(sourceRoot, sourcePath)) !== sourceRevision) {
    throw new Error('Quelldatei hat sich geändert: SHA-256 weicht vom Status ab')
  }
  const wikiSourceRoot = path.join(wikiRoot, 'sources')
  if ((await realpath(wikiRoot)) !== path.resolve(wikiRoot)) {
    throw new Error('Wiki-Wurzel enthält Symlinks')
  }
  try {
    const info = await lstat(wikiSourceRoot)
    if (!info.isDirectory() || info.isSymbolicLink())
      throw new Error('Wiki-Quellwurzel ist kein sicheres Verzeichnis')
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  for (const directory of await readdir(sourceRoot, { withFileTypes: true })) {
    if (directory.isDirectory()) await confined(sourceRoot, `${directory.name}/manifest.json`, true)
  }
  const status = await scanIngestStatus({
    sourceRoot,
    wikiSourceRoot,
    includeCurrent: true,
  })
  if (status.summary.invalid || status.summary.conflict) {
    throw new Error('Frischer Status enthält invalid/conflict; keine Änderungen zulässig')
  }
  const records = ['new', 'outdated', 'current'].flatMap(
    (state) => status.adapters[adapter]?.[state] ?? [],
  )
  const record = records.find((candidate) => candidate.source_key === sourceKey)
  if (!record) throw new Error('Quelle hat keinen eindeutigen frischen Statusdatensatz')
  if (
    record.source_revision !== sourceRevision ||
    record.source_path !== path.join(sourceRoot, sourcePath) ||
    record.wiki_path !== path.join(wikiSourceRoot, item.wiki_path) ||
    !isDeepStrictEqual(record.frontmatter, item.frontmatter) ||
    (await fileHash(sourceRoot, sourcePath)) !== sourceRevision
  ) {
    throw new Error('Quelle/Statusdatensatz hat sich während der Prüfung geändert')
  }
  const expected = { ...record.frontmatter, ...item.claim }
  for (const [name, value] of Object.entries(item.claim)) {
    if (Object.hasOwn(record.frontmatter, name) && String(record.frontmatter[name]) !== value) {
      throw new Error('Manifest-Identität weicht vom Frontmatter ab')
    }
  }
  for (const name of Object.keys(expected)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error('Frontmatter-Feldname ist ungültig')
  }
  return {
    adapter,
    source_key: sourceKey,
    source_path: record.source_path,
    source_revision: sourceRevision,
    wiki_path: record.wiki_path,
    page: `sources/${item.wiki_path}`,
    expected,
  }
}

function receiptPath(preparationId) {
  if (typeof preparationId !== 'string' || !PREPARATION_RE.test(preparationId)) {
    throw new Error('preparation_id ist ungültig')
  }
  return `preparations/${preparationId}.json`
}

async function loadPreparation({ root, preparationId }) {
  return JSON.parse((await bytesAt(root, receiptPath(preparationId))).toString('utf8'))
}

// Publish only complete bytes, never truncate a live page or receipt in place.
// The sibling file keeps installation on the same filesystem; caught failures
// remove only this operation's exclusive temporary file.
async function writeTemporaryFile(root, relative, bytes, mode) {
  const temporary = `${relative}.${randomBytes(16).toString('hex')}.tmp`
  const destination = await confined(root, temporary, true)
  const file = await open(
    destination,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
    mode,
  )
  try {
    try {
      let offset = 0
      while (offset < bytes.length) {
        const { bytesWritten } = await file.write(bytes, offset, bytes.length - offset, offset)
        if (bytesWritten === 0) throw new Error('Datei konnte nicht vollständig geschrieben werden')
        offset += bytesWritten
      }
      await file.sync()
    } finally {
      await file.close()
    }
    return temporary
  } catch (error) {
    await rm(destination, { force: true })
    throw error
  }
}

async function savePreparation(root, receipt, create = false) {
  await mkdir(root, { recursive: true })
  await confined(root, receiptPath(receipt.preparation_id), true)
  await mkdir(path.join(root, 'preparations'), { recursive: true })
  const destination = await confined(root, receiptPath(receipt.preparation_id), true)
  const temporary = await writeTemporaryFile(
    root,
    receiptPath(receipt.preparation_id),
    Buffer.from(`${JSON.stringify(receipt)}\n`),
    0o600,
  )
  const temporaryPath = path.join(root, temporary)
  try {
    await confined(root, temporary)
    await confined(root, receiptPath(receipt.preparation_id), true)
    if (create) await link(temporaryPath, destination)
    else await rename(temporaryPath, destination)
  } finally {
    await rm(temporaryPath, { force: true })
  }
}

async function checkFresh(input, receipt) {
  const source = await freshSource({
    ...input,
    adapter: receipt.source.adapter,
    sourceKey: receipt.source.source_key,
    sourceRevision: receipt.source.source_revision,
  })
  if (!isDeepStrictEqual(source, receipt.source)) {
    throw new Error('Statusdatensatz hat sich seit prepare geändert')
  }
  return source
}

function splitDraft(text) {
  if (typeof text !== 'string' || !text.trim()) throw new Error('draft fehlt oder ist leer')
  const normalized = text.replaceAll('\r\n', '\n')
  if (!normalized.startsWith('---\n')) return { fields: {}, body: normalized }
  const fields = parseFrontmatterFields(normalized)
  const lines = normalized.split('\n')
  return { fields, body: lines.slice(lines.indexOf('---', 1) + 1).join('\n') }
}

function validateFields(text, expected, optional = false) {
  const { fields } = splitDraft(text)
  for (const [name, value] of Object.entries(expected)) {
    if (optional && !Object.hasOwn(fields, name)) continue
    if (!Object.hasOwn(fields, name) || String(fields[name]) !== String(value)) {
      throw new Error(`Frontmatter-Feld ${name} weicht vom frischen Status ab`)
    }
  }
}

// Must run before editing. A pristine repository is mandatory: foreign work
// is never adopted, overwritten, staged, or cleaned up by this tool.
export async function prepareIngest(input) {
  const opts = options(input)
  const { root, wikiRoot, changedPages } = opts
  const source = await freshSource(opts)
  const gitRoot = (await git(wikiRoot, ['rev-parse', '--show-toplevel'])).toString().trim()
  if (gitRoot !== path.resolve(wikiRoot)) throw new Error('wikiRoot muss die Git-Wurzel sein')
  await cleanIndex(wikiRoot)
  if ((await git(wikiRoot, ['status', '--porcelain=v1', '-z', '--untracked-files=all'])).length) {
    throw new Error('Fremde worktree Änderungen: prepare benötigt ein sauberes Wiki')
  }
  if (!Array.isArray(changedPages) || !changedPages.length || changedPages.length > 100) {
    throw new Error('changed_pages muss 1 bis 100 explizite Wiki-Pfade enthalten')
  }
  const pages = [...new Set(changedPages.map((page) => checkRelativePath(page, 'changed_pages')))]
  if (!pages.includes(source.page)) throw new Error('changed_pages enthält die Quellseite nicht')
  const baseline = {}
  for (const page of pages) {
    if (!page.endsWith('.md') || (page.startsWith('sources/') && page !== source.page)) {
      throw new Error('Nur die ausgewählte Quellseite und thematische Markdown-Seiten sind erlaubt')
    }
    const bytes = await bytesAt(wikiRoot, page, true)
    if (page === source.page && bytes !== null) {
      const identity = Object.fromEntries(
        Object.entries(source.expected).filter(([name]) => name !== 'source_revision'),
      )
      validateFields(bytes.toString('utf8'), identity)
      if (!REVISION_RE.test(parseFrontmatterFields(bytes.toString('utf8')).source_revision)) {
        throw new Error('Bestehende Quellseite enthält keine gültige source_revision')
      }
    }
    if (
      (await git(wikiRoot, ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--', page]))
        .length
    ) {
      throw new Error(`Fremde worktree Änderungen überlappen: ${page}`)
    }
    if (bytes !== null) {
      const head = await commitHash(wikiRoot)
      const entry = (await git(wikiRoot, ['ls-tree', '-z', head, '--', page])).toString()
      if (
        !entry.startsWith('100644 blob ') ||
        hash(await git(wikiRoot, ['cat-file', 'blob', `${head}:${page}`])) !== hash(bytes)
      ) {
        throw new Error(`Ziel ist nicht pristine/tracked: ${page}`)
      }
    }
    baseline[page] = bytes === null ? null : hash(bytes)
  }
  const receipt = {
    preparation_id: `prep-${randomBytes(16).toString('hex')}`,
    wiki_root: path.resolve(wikiRoot),
    source,
    base_commit: await commitHash(wikiRoot),
    baseline,
    applied: null,
    owned: {},
    pending: null,
    validated: null,
  }
  await savePreparation(root, receipt, true)
  return {
    preparation_id: receipt.preparation_id,
    adapter: source.adapter,
    source_key: source.source_key,
    source_path: source.source_path,
    source_revision: source.source_revision,
    wiki_path: source.wiki_path,
    canonical_fields: Object.keys(source.expected),
    changed_pages: pages,
  }
}

// No generic metadata repair: apply requires an explicit complete draft.
// Identical findings after a requested reread are legitimate; byte changes do
// not prove semantic extraction. Supplied wrong identity/revision is rejected.
async function applyDraft(input) {
  const opts = options(input)
  const { root, wikiRoot, preparationId, draft } = opts
  const receipt = await loadPreparation(opts)
  if (receipt.wiki_root !== path.resolve(wikiRoot)) throw new Error('Wiki-Wurzel weicht ab')
  const source = await checkFresh(opts, receipt)
  await cleanIndex(wikiRoot)
  if ((await commitHash(wikiRoot)) !== receipt.base_commit)
    throw new Error('HEAD hat sich geändert')
  await recoverPending(opts, receipt)
  const page = opts.page ?? source.page
  if (!Object.hasOwn(receipt.baseline, page)) throw new Error('Seite ist nicht deklariert')
  const current = await bytesAt(wikiRoot, page, true)
  const expectedCurrent = receipt.owned[page] ?? receipt.baseline[page]
  if ((current === null ? null : hash(current)) !== expectedCurrent) {
    throw new Error(`Fremde worktree Änderung an ${page}; kein Überschreiben`)
  }
  if (typeof draft !== 'string' || !draft.trim()) throw new Error('draft fehlt oder ist leer')
  let text = draft
  if (page === source.page) {
    validateFields(draft, source.expected, true)
    const proposed = splitDraft(draft)
    const old = current === null ? { fields: {} } : splitDraft(current.toString('utf8'))
    if (!proposed.body.trim()) throw new Error('Expliziter Quellseiteninhalt fehlt')
    const fields = { ...old.fields, ...proposed.fields, ...source.expected }
    text = `---\n${Object.entries(fields)
      .map(([name, value]) => `${name}: ${JSON.stringify(String(value))}`)
      .join('\n')}\n---\n${proposed.body}`
    validateFields(text, source.expected)
  }
  const destination = await confined(wikiRoot, page, true)
  await mkdir(path.dirname(destination), { recursive: true })
  await confined(wikiRoot, page, true)
  const temporary = await writeTemporaryFile(wikiRoot, page, Buffer.from(text), 0o644)
  const temporaryPath = path.join(wikiRoot, temporary)
  try {
    await confined(wikiRoot, temporary)
    await confined(wikiRoot, page, true)
    if (current !== null) {
      const file = await open(destination, constants.O_RDONLY | constants.O_NOFOLLOW)
      try {
        if ((await file.stat()).nlink !== 1)
          throw new Error('Hardlink ist kein sicherer Schreibpfad')
        if (hash(await file.readFile()) !== expectedCurrent) {
          throw new Error('Fremde worktree Änderung; kein Überschreiben')
        }
      } finally {
        await file.close()
      }
    }
    // Persist intent before changing the page. Never rename over a live page:
    // move its inode into retained private evidence, then install exclusively.
    // A writer racing the move is preserved in the backup; a writer creating
    // the destination in the gap wins, and link fails without overwriting it.
    const backup = current === null ? null : await createBackupSlot(wikiRoot)
    receipt.pending = { page, before: expectedCurrent, after: hash(Buffer.from(text)), backup }
    receipt.validated = null
    await savePreparation(root, receipt)
    if (backup !== null) {
      await rename(destination, path.join(wikiRoot, '.git', backup))
      await checkBackup(wikiRoot, receipt.pending)
    }
    await link(temporaryPath, destination)
  } finally {
    await rm(temporaryPath, { force: true })
  }
  await recoverPending(opts, receipt)
  return {
    preparation_id: preparationId,
    wiki_path: path.join(wikiRoot, page),
    source_revision: source.source_revision,
  }
}

// Snapshot exactly what the external, explicit-path Git commit must contain.
// Index remains untouched. Revalidate after changes; any later difference in
// committed bytes is rejected by the journal writer.
async function validateDraft(input) {
  const opts = options(input)
  const { root, wikiRoot, preparationId } = opts
  const receipt = await loadPreparation(opts)
  if (receipt.wiki_root !== path.resolve(wikiRoot)) throw new Error('Wiki-Wurzel weicht ab')
  const source = await checkFresh(opts, receipt)
  await cleanIndex(wikiRoot)
  if ((await commitHash(wikiRoot)) !== receipt.base_commit)
    throw new Error('HEAD hat sich geändert')
  await recoverPending(opts, receipt)
  if (!receipt.applied) throw new Error('apply mit explizitem Draft fehlt')
  const dirty = (await git(wikiRoot, ['status', '--porcelain=v1', '-z', '--untracked-files=all']))
    .toString()
    .split('\0')
    .filter(Boolean)
  for (const entry of dirty) {
    if (!Object.hasOwn(receipt.baseline, entry.slice(3))) {
      throw new Error('Undeklarierte worktree Änderungen; Commit nicht zulässig')
    }
    if (entry.startsWith('R') || entry.startsWith('C'))
      throw new Error('Renames sind nicht zulässig')
  }
  const snapshot = {}
  for (const [page, baseline] of Object.entries(receipt.baseline)) {
    const bytes = await bytesAt(wikiRoot, page, true)
    if (bytes === null) {
      if ((receipt.owned[page] ?? baseline) !== null)
        throw new Error('Löschen bestehender Wiki-Seiten ist nicht zulässig')
      continue
    }
    const revision = hash(bytes)
    if (revision !== (receipt.owned[page] ?? baseline)) {
      if (page === source.page) validateFields(bytes.toString('utf8'), source.expected)
      throw new Error(`Fremde worktree Änderung an deklarierter Seite: ${page}`)
    }
    if (page === source.page) {
      validateFields(bytes.toString('utf8'), source.expected)
      if (revision !== receipt.applied)
        throw new Error('Quellseiteninhalt weicht vom angewendeten Draft ab')
    }
    if (revision !== baseline) snapshot[page] = revision
  }
  if (!Object.keys(snapshot).length) throw new Error('Keine Wiki-Änderungen für einen Commit')
  receipt.validated = snapshot
  await savePreparation(root, receipt)
  return { preparation_id: preparationId, validated: true, changed_pages: Object.keys(snapshot) }
}

// Backups retain the displaced inode, not just a byte snapshot: even writes
// through a previously opened descriptor remain available for maintenance.
// They are never automatically deleted and are outside Git's content tree.
async function backupRoot(wikiRoot) {
  const directory = path.join(wikiRoot, '.git')
  if ((await realpath(directory)) !== directory || !(await lstat(directory)).isDirectory())
    throw new Error('Git-Verzeichnis ist kein sicherer Backup-Pfad')
  return directory
}

async function createBackupSlot(wikiRoot) {
  const directory = await backupRoot(wikiRoot)
  const name = `ingest-backup-${randomBytes(16).toString('hex')}`
  await mkdir(path.join(directory, name), { mode: 0o700 })
  return `${name}/page`
}

async function checkBackup(wikiRoot, pending) {
  if (pending.backup === null) return
  const directory = await backupRoot(wikiRoot)
  const bytes = await bytesAt(directory, pending.backup, true)
  if (bytes !== null && hash(bytes) !== pending.before) {
    // Restore only into an absent destination, never over a concurrent writer.
    const destination = await confined(wikiRoot, pending.page, true)
    await link(path.join(directory, pending.backup), destination).catch((error) => {
      if (error.code !== 'EEXIST') throw error
    })
    throw new Error(`Fremde worktree Änderung; Backup erhalten: .git/${pending.backup}`)
  }
}

async function recoverPending(opts, receipt) {
  if (!receipt.pending) return
  const pending = receipt.pending
  await checkBackup(opts.wikiRoot, pending)
  let current = await bytesAt(opts.wikiRoot, pending.page, true)
  if (current === null && pending.backup !== null) {
    const directory = await backupRoot(opts.wikiRoot)
    const backup = await bytesAt(directory, pending.backup, true)
    if (backup !== null) {
      const temporary = await writeTemporaryFile(opts.wikiRoot, pending.page, backup, 0o644)
      try {
        await link(
          path.join(opts.wikiRoot, temporary),
          await confined(opts.wikiRoot, pending.page, true),
        )
      } finally {
        await rm(path.join(opts.wikiRoot, temporary), { force: true })
      }
      current = backup
    }
  }
  const revision = current === null ? null : hash(current)
  if (revision === pending.after) {
    receipt.owned[pending.page] = pending.after
    if (pending.page === receipt.source.page) receipt.applied = pending.after
  } else if (revision !== pending.before) {
    throw new Error('Fremde worktree Änderung während apply; kein automatisches Reparieren')
  }
  receipt.pending = null
  receipt.validated = null
  await savePreparation(opts.root, receipt)
}

async function withPreparationLock(input, operation) {
  const opts = options(input)
  const relative = `${receiptPath(opts.preparationId)}.lock`
  const destination = await confined(opts.root, relative, true)
  const lock = await open(
    destination,
    constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW,
    0o600,
  )
  try {
    return await operation(opts)
  } finally {
    await lock.close()
    await rm(destination)
  }
}

export const applyIngestDraft = (input) => withPreparationLock(input, applyDraft)
export const validateIngest = (input) => withPreparationLock(input, validateDraft)

// Required before appending status=ingested. Verify source SHA, fresh identity,
// the actual commit parent, complete changed-path set, and each committed blob.
export async function verifyIngestCommit(input) {
  const opts = options(input)
  const { wikiRoot, record } = opts
  if (typeof record.commit !== 'string' || !COMMIT_RE.test(record.commit)) {
    throw new Error('commit ist kein Git-Hash')
  }
  const receipt = await loadPreparation({ ...opts, preparationId: record.preparation_id })
  if (receipt.wiki_root !== path.resolve(wikiRoot)) throw new Error('Wiki-Wurzel weicht ab')
  const source = await checkFresh(opts, receipt)
  for (const name of ['adapter', 'source_key', 'source_path', 'source_revision', 'wiki_path']) {
    if (record[name] !== source[name])
      throw new Error(`Datensatz ${name} weicht vom frischen Status ab`)
  }
  if (!receipt.validated) throw new Error('validate vor dem Commit fehlt')
  const commit = await commitHash(wikiRoot, record.commit)
  if ((await commitHash(wikiRoot)) !== commit) throw new Error('Commit ist nicht der aktuelle HEAD')
  const parents = (await git(wikiRoot, ['rev-list', '--parents', '-n', '1', commit]))
    .toString()
    .trim()
    .split(' ')
  if (parents.length !== 2 || parents[1] !== receipt.base_commit) {
    throw new Error('Commit gehört nicht zu dieser Vorbereitung oder ist ein Merge')
  }
  const changed = (
    await git(wikiRoot, [
      'diff-tree',
      '--no-commit-id',
      '--name-only',
      '--no-renames',
      '-r',
      '-z',
      commit,
    ])
  )
    .toString()
    .split('\0')
    .filter(Boolean)
    .sort()
  const expected = Object.keys(receipt.validated).sort()
  if (
    JSON.stringify(changed) !== JSON.stringify(expected) ||
    JSON.stringify([...new Set(record.changed_pages)].sort()) !== JSON.stringify(expected)
  ) {
    throw new Error('Commit/changed_pages enthält nicht genau die validierten Wiki-Seiten')
  }
  await cleanIndex(wikiRoot)
  // A reread may leave the source page unchanged and only update the log.
  // Its committed provenance and body still belong to the validated draft.
  const sourceBytes = await git(wikiRoot, ['cat-file', 'blob', `${commit}:${source.page}`])
  const sourceEntry = (await git(wikiRoot, ['ls-tree', '-z', commit, '--', source.page])).toString()
  if (
    !sourceEntry.startsWith('100644 blob ') ||
    hash(sourceBytes) !== receipt.applied ||
    hash(await bytesAt(wikiRoot, source.page)) !== receipt.applied
  ) {
    throw new Error('Commit-Inhalt/Worktree der Quellseite weicht vom validierten Draft ab')
  }
  validateFields(sourceBytes.toString('utf8'), source.expected)
  for (const page of expected) {
    await confined(wikiRoot, page)
    const entry = (await git(wikiRoot, ['ls-tree', '-z', commit, '--', page])).toString()
    if (!entry.startsWith('100644 blob '))
      throw new Error('Commit-Seite ist keine reguläre Markdown-Datei')
    const bytes = await git(wikiRoot, ['cat-file', 'blob', `${commit}:${page}`])
    if (hash(bytes) !== receipt.validated[page])
      throw new Error(`Commit-Inhalt weicht von validate ab: ${page}`)
    if (hash(await bytesAt(wikiRoot, page)) !== receipt.validated[page])
      throw new Error(`Worktree weicht vom Commit ab: ${page}`)
    if (page === source.page) validateFields(bytes.toString('utf8'), source.expected)
  }
  // Providers may publish concurrently; check once more after inspecting the
  // tree, not just before the Git reads, before authorizing the journal append.
  await checkFresh(opts, receipt)
  return { commit, source_unmodified: true }
}
