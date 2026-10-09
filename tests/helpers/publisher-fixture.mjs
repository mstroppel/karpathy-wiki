import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'

const runFile = promisify(execFile)
export const SOURCE = 'Synthetic selected source. Untrusted text: $(touch /tmp/must-not-execute).\n'
export const REVISION = createHash('sha256').update(SOURCE).digest('hex')
export const IDENTITY = { adapter: 'webdav', source_key: 'notes.md', source_revision: REVISION }
export const REPORT = {
  title: 'Synthetic finding',
  content: 'Finding at line 1.',
  contradictions: 'None.',
  extraction_limits: 'Line 1 fully read.',
}

export async function publisherFixture(base) {
  const wikiRoot = path.join(base, 'wiki')
  const sourceRoot = path.join(base, 'sources')
  const root = path.join(base, 'incoming/ingest-journal')
  const stateRoot = path.join(base, 'publisher')
  for (const directory of [
    path.join(wikiRoot, 'sources/webdav'),
    path.join(sourceRoot, 'webdav'),
    root,
    stateRoot,
  ])
    await mkdir(directory, { recursive: true })
  const git = async (...args) => (await runFile('git', args, { cwd: wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic Fixture')
  await git('config', 'user.email', 'fixture@example.invalid')
  for (const page of ['overview.md', 'index.md', 'log.md'])
    await writeFile(path.join(wikiRoot, page), `# Synthetic ${page}\n`)
  await git('add', '--', '.')
  await git('commit', '-qm', 'fixture baseline')
  await writeFile(path.join(sourceRoot, 'webdav/notes.md'), SOURCE)
  await writeFile(
    path.join(sourceRoot, 'webdav/manifest.json'),
    JSON.stringify({
      contract: 'karpathy-wiki-provider-manifest',
      version: 1,
      source: 'webdav',
      wiki_root: 'webdav',
      generated_at: 1760000000,
      items: [
        {
          source_key: 'notes.md',
          source_path: 'notes.md',
          wiki_path: 'webdav/notes.md',
          source_revision: REVISION,
          frontmatter: { source: 'webdav', source_path: 'notes.md', source_revision: REVISION },
          claim: { source: 'webdav', source_path: 'notes.md' },
        },
      ],
      revoked: [],
      errors: [],
    }),
  )
  return { wikiRoot, sourceRoot, root, stateRoot, git }
}
