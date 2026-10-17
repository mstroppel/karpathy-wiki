import { spawn } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, open, readFile, readdir, rename, rm } from 'node:fs/promises'
import path from 'node:path'

const queues = new Map()

// Hash private trees without following links or accepting shared mutable files.
// An absent root is empty; a partially unreadable tree is always an error.
export async function inventoryIngestTree(directory) {
  const result = { files: {}, directories: [], bytes: 0, allocated_bytes: 0 }
  async function walk(relative) {
    const file = path.join(directory, relative)
    const info = await lstat(file)
    if (info.isSymbolicLink()) throw new Error('unsafe_symlink')
    result.allocated_bytes += info.blocks * 512
    if (info.isDirectory()) {
      result.directories.push(relative)
      for (const name of (await readdir(file)).sort()) await walk(path.join(relative, name))
    } else if (info.isFile() && info.nlink === 1) {
      result.files[relative] = createHash('sha256')
        .update(await readFile(file))
        .digest('hex')
      result.bytes += info.size
    } else throw new Error('unsafe_file_type_or_hardlink')
  }
  try {
    await walk('')
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
    const root = await lstat(directory).catch((failure) => {
      if (failure.code === 'ENOENT') return null
      throw failure
    })
    if (root) throw error
    return { files: {}, directories: [], bytes: 0, allocated_bytes: 0 }
  }
  return result
}

// Linux flock is held by a child while its pipe remains open. A killed runtime
// closes the pipe and releases the kernel lock without deleting its inode.
export async function withIngestLock(file, action, { create = true } = {}) {
  const previous = queues.get(file) ?? Promise.resolve()
  const task = previous.then(async () => {
    const child = spawn(
      'python3',
      [
        '-c',
        'import fcntl, os, sys\ntry:\n f = os.open(sys.argv[1], (os.O_CREAT if sys.argv[2] == "create" else 0) | os.O_RDWR | os.O_NOFOLLOW, 0o600)\nexcept FileNotFoundError:\n sys.exit(3)\nfcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nprint("locked", flush=True)\nsys.stdin.buffer.read()\n',
        file,
        create ? 'create' : 'existing',
      ],
      { stdio: ['pipe', 'pipe', 'pipe'] },
    )
    const exited = new Promise((resolve) => child.once('close', resolve))
    try {
      await new Promise((resolve, reject) => {
        child.once('error', reject)
        child.stdout.once('data', (bytes) =>
          bytes.toString().trim() === 'locked'
            ? resolve()
            : reject(new Error('Publikationslock fehlgeschlagen')),
        )
        child.once('exit', (code) =>
          reject(
            new Error(
              code === 3 ? 'lock_file_missing' : 'Ein anderer Schreiber hält den Ingest-Lock',
            ),
          ),
        )
      })
      return await action()
    } finally {
      child.stdin.end()
      await exited
    }
  })
  queues.set(file, task)
  try {
    return await task
  } finally {
    if (queues.get(file) === task) queues.delete(file)
  }
}

// Logical audit appends replace a complete file atomically. A crash cannot
// leave a partial final JSONL record or truncate run/report metadata.
export async function writeIngestFile(file, bytes) {
  const temporary = `${file}.${randomBytes(16).toString('hex')}.tmp`
  const handle = await open(
    temporary,
    constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW,
    0o600,
  )
  try {
    try {
      await handle.writeFile(bytes)
      await handle.sync()
    } finally {
      await handle.close()
    }
    await rename(temporary, file)
    const directory = await open(path.dirname(file), constants.O_RDONLY | constants.O_DIRECTORY)
    try {
      await directory.sync()
    } finally {
      await directory.close()
    }
  } finally {
    await rm(temporary, { force: true })
  }
}
