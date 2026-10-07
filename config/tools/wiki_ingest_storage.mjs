import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import { open, rename, rm } from 'node:fs/promises'
import path from 'node:path'

const queues = new Map()

// Linux flock is held by a child while its pipe remains open. A killed runtime
// closes the pipe and releases the kernel lock without deleting its inode.
export async function withIngestLock(file, action) {
  const previous = queues.get(file) ?? Promise.resolve()
  const task = previous.then(async () => {
    const child = spawn(
      'python3',
      [
        '-c',
        'import fcntl, os, sys\nf = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)\nfcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nprint("locked", flush=True)\nsys.stdin.buffer.read()\n',
        file,
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
        child.once('exit', () => reject(new Error('Ein anderer Schreiber hält den Ingest-Lock')))
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
