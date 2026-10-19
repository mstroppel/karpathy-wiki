import { execFile } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { lstat, mkdir, readFile, realpath, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'
import {
  parseFrontmatterFields,
  parseManifest,
  scanIngestStatus,
} from './wiki_ingest_status_core.mjs'
import { withIngestLock, writeIngestFile } from './wiki_ingest_storage.mjs'

const runFile = promisify(execFile)
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex')
const defaults = {
  wikiRoot: '/knowledge/wiki',
  sourceRoot: '/knowledge/sources',
  root: '/knowledge/incoming/ingest-journal',
}

// Only code-owned reasons may cross the failure-report boundary. Native parser,
// filesystem, Git and worker errors can contain source-derived text.
class BatchBlocker extends Error {}

async function git(wikiRoot, args) {
  return (
    await runFile('git', ['--no-replace-objects', '--literal-pathspecs', ...args], {
      cwd: wikiRoot,
      encoding: 'utf8',
      maxBuffer: 16 * 1024 * 1024,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
    })
  ).stdout
}

async function clean(wikiRoot) {
  if ((await git(wikiRoot, ['status', '--porcelain=v1', '--untracked-files=all'])).trim())
    throw new Error('Wiki nicht sauber: Änderungen manuell prüfen; kein automatisches Rücksetzen')
}

async function regularFile(file, root) {
  const resolved = await realpath(file)
  if (!resolved.startsWith(`${await realpath(root)}${path.sep}`))
    throw new BatchBlocker('Datei außerhalb der erlaubten Wurzel')
  const info = await lstat(file)
  if (!info.isFile() || info.nlink !== 1 || resolved !== file)
    throw new BatchBlocker('Keine reguläre, unverlinkte Datei')
  return readFile(file)
}

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
    JSON.parse(
      await regularFile(path.join(sourceRoot, source.adapter, 'manifest.json'), sourceRoot),
    ),
  )
  const item = manifest.items.find((candidate) => candidate.source_key === source.source_key)
  if (
    !item ||
    item.source_revision !== source.source_revision ||
    path.join(sourceRoot, source.adapter, item.source_path) !== source.source_path ||
    path.join(wikiRoot, 'sources', item.wiki_path) !== source.wiki_path ||
    JSON.stringify(item.frontmatter) !== JSON.stringify(source.frontmatter)
  )
    throw new BatchBlocker('Quelle während des Batches geändert; Änderungen manuell prüfen')
  const bytes = await regularFile(source.source_path, sourceRoot)
  const digest = hash(bytes)
  if (item.source_sha256 && item.source_sha256 !== digest)
    throw new BatchBlocker('Quelldatei weicht vom Publisher-Digest ab')
  if (source.adapter === 'paperless') {
    if (!item.source_sha256)
      throw new BatchBlocker('Paperless benötigt source_sha256 vom Publisher')
    const fields = parseFrontmatterFields(bytes.toString('utf8'))
    if (
      Object.entries({ ...item.frontmatter, ...item.claim }).some(
        ([key, value]) => String(fields[key]) !== String(value),
      )
    )
      throw new BatchBlocker('Paperless-Quelle weicht vom Manifest ab')
  } else if (digest !== source.source_revision) throw new BatchBlocker('Quellrevision stimmt nicht')
  return bytes
}

function recordsFrom(text, count) {
  let records
  try {
    records = JSON.parse(text)
  } catch {
    throw new BatchBlocker('Worker lieferte kein gültiges JSON')
  }
  if (!Array.isArray(records) || records.length !== count)
    throw new BatchBlocker('Worker muss genau einen Ergebnisdatensatz pro Quelle liefern')
  return records.map((record, index) => {
    if (record?.index !== index) throw new BatchBlocker('Ergebnisreihenfolge stimmt nicht')
    for (const field of ['content', 'contradictions', 'extraction_limits']) {
      if (typeof record[field] !== 'string' || !record[field].trim() || record[field].length > 4000)
        throw new BatchBlocker(`Ungültiges Ergebnisfeld: ${field}`)
    }
    return {
      index,
      content: record.content,
      contradictions: record.contradictions,
      extraction_limits: record.extraction_limits,
    }
  })
}

async function changedPaths(wikiRoot) {
  const tracked = await git(wikiRoot, ['diff', 'HEAD', '--name-only', '-z'])
  const untracked = await git(wikiRoot, ['ls-files', '--others', '--exclude-standard', '-z'])
  return [...new Set((tracked + untracked).split('\0').filter(Boolean))].sort()
}

function reportText(state) {
  const lines = [
    '# Einlesebericht',
    '',
    `Batch: ${state.batch_id}`,
    `Status: ${state.status}`,
    `Checkpoint: ${state.checkpoint}`,
    `Commit: ${state.commit ?? 'Kein Commit verifiziert'}`,
    `Blocker: ${state.blocker ?? 'Keiner'}`,
    '',
    'Geänderte Seiten:',
    ...(state.changed_pages ?? []).map((page) => `- ${page}`),
  ]
  for (const [index, source] of state.sources.entries()) {
    const record = state.records?.[index]
    lines.push(
      '',
      `## ${source.source_path}`,
      '',
      `Revision: ${source.source_revision}`,
      `Wiki: ${source.wiki_path}`,
      `**Inhalt:** ${record?.content ?? 'Nicht ermittelt'}`,
      `**Widersprüche/offene Fragen:** ${record?.contradictions ?? 'Nicht ermittelt'}`,
      `**Extraktionsgrenzen:** ${record?.extraction_limits ?? 'Nicht ermittelt'}`,
    )
  }
  return lines.join('\n') + '\n'
}

// One callback, one lifetime lock, one live worktree, one commit. There are no
// per-page receipts, private clones, rollback operations or automatic recovery.
export async function runIngestBatch(input, worker) {
  const opts = { ...defaults, ...input }
  const { wikiRoot, sourceRoot, root, signal } = opts
  const size = opts.maxSources ?? 5
  const budget = opts.budgetTokens ?? 128000
  if (!Number.isInteger(size) || size < 1 || size > 5) throw new Error('Batchgröße muss 1–5 sein')
  if (!Number.isInteger(budget) || budget < 16000 || budget > 1000000)
    throw new Error('Kontextziel muss 16000–1000000 sein')
  const gitRoot = path.join(wikiRoot, '.git')
  if ((await realpath(gitRoot)) !== gitRoot) throw new Error('Unsicheres Git-Verzeichnis')
  return withIngestLock(path.join(gitRoot, 'wiki-ingest-publication.lock'), async () => {
    signal?.throwIfAborted()
    await clean(wikiRoot)
    const scan = () =>
      scanIngestStatus({ sourceRoot, wikiSourceRoot: path.join(wikiRoot, 'sources') })
    const status = await scan()
    if (status.summary.invalid || status.summary.conflict)
      throw new Error('Quellenstatus enthält invalid/conflict; manuelle Prüfung erforderlich')
    const candidates = pending(status).filter(
      (source) => !opts.sourcePath || source.source_path === opts.sourcePath,
    )
    if (opts.sourcePath && !candidates.length)
      throw new Error('Angeforderte Quelle ist nicht new/outdated; keine Neuauswertung')
    const sources = []
    let estimated = 12000
    for (const source of candidates.slice(0, size)) {
      const bytes = await sourceBytes(source, opts)
      const tokens = Math.ceil(bytes.length / 3) + 4000
      if (sources.length && estimated + tokens > budget) break
      estimated += tokens
      sources.push({ ...source, source_sha256: hash(bytes) })
    }
    if (!sources.length) return { status: 'done', remaining: 0, summary: status.summary }
    const batchId = `batch-${Date.now()}-${randomBytes(6).toString('hex')}`
    const directory = path.join(root, 'batches', batchId)
    await mkdir(directory, { recursive: true, mode: 0o700 })
    const checkpoint = (await git(wikiRoot, ['rev-parse', 'HEAD'])).trim()
    const state = { batch_id: batchId, status: 'running', checkpoint, sources, commit: null }
    const report = path.join(directory, 'report.md')
    const save = async () => {
      await writeIngestFile(path.join(directory, 'state.json'), JSON.stringify(state, null, 2))
      await writeIngestFile(report, reportText(state))
    }
    await save()
    try {
      state.records = recordsFrom(
        await worker({
          batch_id: batchId,
          sources,
          budget_tokens: budget,
          estimated_tokens: estimated,
        }),
        sources.length,
      )
      signal?.throwIfAborted()
      if ((await git(wikiRoot, ['rev-parse', 'HEAD'])).trim() !== checkpoint)
        throw new BatchBlocker('HEAD während des Batches geändert; keine automatische Reparatur')
      for (const source of sources) {
        if (hash(await sourceBytes(source, opts)) !== source.source_sha256)
          throw new BatchBlocker('Quelle während des Batches geändert; Änderungen manuell prüfen')
        const page = (await regularFile(source.wiki_path, wikiRoot)).toString('utf8')
        const body = page.startsWith('---\n') ? page.replace(/^---\n[\s\S]*?\n---\n/, '') : page
        if (!body.trim()) throw new BatchBlocker('Leere Quellenseite')
        const frontmatter = Object.entries(source.frontmatter)
          .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
          .join('\n')
        await writeFile(source.wiki_path, `---\n${frontmatter}\n---\n${body}`)
      }
      // index/log are cheap deterministic batch appends, not model round trips.
      const indexPath = path.join(wikiRoot, 'index.md')
      let index = (await regularFile(indexPath, wikiRoot)).toString('utf8')
      for (const source of sources) {
        const page = path.relative(wikiRoot, source.wiki_path).replace(/\.md$/, '')
        if (/[[\]|<>\r\n]/.test(page)) throw new BatchBlocker('Unsicherer Katalogpfad')
        if (!index.includes(`[[${page}]]`) && !index.includes(`[[${page}|`))
          index += `\n- [[${page}]]\n`
      }
      await writeFile(indexPath, index)
      const logPath = path.join(wikiRoot, 'log.md')
      const log = (await regularFile(logPath, wikiRoot)).toString('utf8')
      await writeFile(logPath, log + `\n- Ingest ${batchId}: ${sources.length} Quellen.\n`)
      await regularFile(path.join(wikiRoot, 'overview.md'), wikiRoot)
      const changed = await changedPaths(wikiRoot)
      const selectedPages = new Set(
        sources.map((source) => path.relative(wikiRoot, source.wiki_path)),
      )
      for (const page of changed) {
        if (
          !(
            selectedPages.has(page) ||
            /^(index|overview|log)\.md$/.test(page) ||
            /^(entities|concepts|analyses)\/.+\.md$/.test(page)
          )
        )
          throw new BatchBlocker('Unerlaubter geänderter Wiki-Pfad')
        await regularFile(path.join(wikiRoot, page), wikiRoot)
      }
      const checked = await scan()
      if (checked.summary.invalid || checked.summary.conflict)
        throw new BatchBlocker('Ungültige Wiki-Metadaten nach Batch')
      state.changed_pages = changed
      await save()
      signal?.throwIfAborted()
      await git(wikiRoot, ['add', '--', ...changed])
      try {
        await git(wikiRoot, ['diff', '--cached', '--check'])
      } catch {
        throw new BatchBlocker('Git-Diff-Prüfung fehlgeschlagen; Änderungen manuell prüfen')
      }
      await git(wikiRoot, [
        'commit',
        '-m',
        `feat(wiki): ingest ${sources.length} sources (${batchId})`,
      ])
      state.commit = (await git(wikiRoot, ['rev-parse', 'HEAD'])).trim()
      state.status = 'ingested'
      await save()
      return {
        status: state.status,
        batch_id: batchId,
        commit: state.commit,
        changed_pages: changed,
        report_path: report,
        remaining: checked.summary.new + checked.summary.outdated,
        sources: sources.map((source, i) => ({
          source_path: source.source_path,
          content: state.records[i].content.slice(0, 200),
        })),
      }
    } catch (error) {
      state.status = state.commit ? 'report_failed' : 'blocked'
      state.blocker =
        error instanceof BatchBlocker
          ? error.message
          : 'Batch fehlgeschlagen; Worker, Validierung oder Berichtsspeicherung manuell prüfen'
      let reportError = null
      try {
        await save()
      } catch {
        // Report storage may remain unavailable. Do not lose a verified commit
        // or advertise stale running evidence as the completed report.
        reportError = 'Berichtsspeicherung fehlgeschlagen'
      }
      return {
        status: state.status,
        batch_id: batchId,
        commit: state.commit,
        blocker: state.blocker,
        report_path: reportError ? null : report,
        report_error: reportError,
        sources: sources.map((source) => ({ source_path: source.source_path })),
      }
    }
  })
}
