import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { lstat, mkdir, readFile, readdir, realpath, rm } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'
import { checkRelativePath } from './wiki_ingest_status_core.mjs'
import { writeIngestFile } from './wiki_ingest_storage.mjs'

const execute = promisify(execFile)
export const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')
export const MAX_FILE_BYTES = 8 * 1024 * 1024

// Only code-owned categories cross the tool boundary; never native error bodies.
export class IngestFault extends Error {
  constructor(code) {
    super(code)
    this.code = code
  }
}

export async function git(root, args, env = {}, encoding = 'utf8') {
  return (
    await execute('git', ['--no-replace-objects', '--literal-pathspecs', ...args], {
      cwd: root,
      encoding,
      maxBuffer: 16 * 1024 * 1024,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0', ...env },
    })
  ).stdout
}

export function relativePage(value) {
  checkRelativePath(value)
  if (value.split('/').some((part) => part.startsWith('.'))) throw new IngestFault('unsafe_path')
  return value
}

export function editablePage(relative, source) {
  try {
    relativePage(relative)
  } catch {
    return false
  }
  return (
    !relative.split('/').includes('AGENTS.md') &&
    (relative === source ||
      relative === 'overview.md' ||
      /^(entities|concepts|analyses)\/.+\.md$/.test(relative))
  )
}

export async function safeDirectory(directory, mode = 0o700) {
  await mkdir(directory, { recursive: true, mode })
  if ((await realpath(directory)) !== directory || !(await lstat(directory)).isDirectory())
    throw new IngestFault('unsafe_directory')
}

export async function fileBytes(file, root, { absent = false, limit = MAX_FILE_BYTES } = {}) {
  try {
    const info = await lstat(file)
    if (
      !info.isFile() ||
      info.nlink !== 1 ||
      info.size > limit ||
      (await realpath(file)) !== file ||
      !file.startsWith(`${await realpath(root)}${path.sep}`)
    )
      throw new IngestFault('unsafe_file')
    return await readFile(file)
  } catch (error) {
    if (absent && error.code === 'ENOENT') {
      // An absent leaf must not disguise a symlink in an existing ancestor.
      let parent = path.dirname(file)
      while (parent.startsWith(root)) {
        try {
          if ((await realpath(parent)) !== parent) throw new IngestFault('unsafe_directory')
          break
        } catch (ancestor) {
          if (ancestor.code !== 'ENOENT') throw ancestor
          parent = path.dirname(parent)
        }
      }
      return null
    }
    throw error
  }
}

export async function putFile(root, relative, bytes) {
  relativePage(relative)
  const file = path.join(root, relative)
  await safeDirectory(path.dirname(file))
  await fileBytes(file, root, { absent: true })
  await writeIngestFile(file, bytes)
}

export async function dirtyPaths(wikiRoot) {
  const tracked = await git(wikiRoot, ['diff', 'HEAD', '--name-only', '-z'])
  const untracked = await git(wikiRoot, ['ls-files', '--others', '--exclude-standard', '-z'])
  return [...new Set((tracked + untracked).split('\0').filter(Boolean))].sort()
}

export async function cleanWiki(wikiRoot) {
  if ((await git(wikiRoot, ['status', '--porcelain=v1', '--untracked-files=all'])).trim())
    throw new IngestFault('dirty_wiki')
}

export async function filesUnder(root, limit = 64 * 1024 * 1024) {
  const files = new Map()
  let bytes = 0
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name)
      if (entry.isDirectory()) await visit(file)
      else {
        const content = await fileBytes(file, root)
        bytes += content.length
        if (bytes > limit || files.size >= 4096) throw new IngestFault('draft_capacity')
        files.set(path.relative(root, file), content)
      }
    }
  }
  await visit(root)
  return { files, bytes }
}

export async function storageBytes(root, limit) {
  let bytes = 0
  let entries = 0
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name)
      const info = await lstat(file)
      bytes += info.size
      if (++entries > 16384 || bytes > limit) throw new IngestFault('draft_capacity')
      // Account for unsafe evidence, but never follow links into other trees.
      if (info.isDirectory()) await visit(file)
    }
  }
  await visit(root)
  return bytes
}

export async function createDraft(opts, directory, base, source) {
  const wiki = path.join(directory, 'wiki')
  await safeDirectory(wiki)
  const names = (await git(opts.wikiRoot, ['ls-tree', '-r', '--name-only', '-z', base]))
    .split('\0')
    .filter(Boolean)
  const baseline = {}
  let size = 0
  for (const name of names) {
    // Other source pages stay read-only in the live wiki. Copy only the selected
    // page and shared editing context; never copy Git history or configuration.
    if (!['index.md', 'log.md', 'AGENTS.md'].includes(name) && !editablePage(name, source)) continue
    relativePage(name)
    const bytes = await git(opts.wikiRoot, ['cat-file', 'blob', `${base}:${name}`], {}, null)
    const live = await fileBytes(path.join(opts.wikiRoot, name), opts.wikiRoot)
    if (digest(live) !== digest(bytes)) throw new IngestFault('foreign_wiki_changes')
    size += bytes.length
    if (size > opts.maxDraftBytes) throw new IngestFault('draft_capacity')
    baseline[name] = digest(bytes)
    await putFile(wiki, name, bytes)
  }
  for (const folder of ['sources', 'entities', 'concepts', 'analyses'])
    await safeDirectory(path.join(wiki, folder))
  return { wiki, baseline }
}

export async function draftChanges(wiki, baseline, source, limit) {
  const { files } = await filesUnder(wiki, limit)
  if (Object.keys(baseline).some((file) => !files.has(file)))
    throw new IngestFault('worker_deleted_file')
  const changed = []
  for (const [file, bytes] of files) {
    if (digest(bytes) === baseline[file]) continue
    if (!editablePage(file, source)) throw new IngestFault('worker_forbidden_path')
    if (/^(?:<{7}|={7}|>{7})(?: |$)/m.test(bytes.toString('utf8')))
      throw new IngestFault('conflict_markers')
    changed.push(file)
  }
  return changed.sort()
}

// Only a generated, journal-owned attempt directory is ever reclaimed.
export async function removeDraft(root, batchId, attempt) {
  if (
    !/^batch-[1-9][0-9]{0,15}-[a-f0-9]{12}$/.test(batchId) ||
    !/^attempt-[a-f0-9]{24}$/.test(attempt)
  )
    throw new IngestFault('unsafe_attempt')
  const directory = path.join(root, 'drafts', batchId, attempt)
  if ((await realpath(directory)) !== directory) throw new IngestFault('unsafe_attempt')
  await rm(directory, { recursive: true })
}
