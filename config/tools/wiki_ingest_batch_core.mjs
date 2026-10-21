import { execFile } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { lstat, mkdir, readFile, realpath, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { isDeepStrictEqual, promisify } from 'node:util'
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
    path.join(wikiRoot, 'sources', item.wiki_path) !== source.wiki_path ||
    !isDeepStrictEqual(item.frontmatter, source.frontmatter)
  )
    throw new BatchBlocker('Quelle während des Batches geändert; Änderungen manuell prüfen')
  // A provider may relocate an unchanged revision to a new generation. Verify
  // the exact bytes given to the worker, not equality of generation paths.
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

async function changedPaths(wikiRoot) {
  const tracked = await git(wikiRoot, ['diff', 'HEAD', '--name-only', '-z'])
  const untracked = await git(wikiRoot, ['ls-files', '--others', '--exclude-standard', '-z'])
  return [...new Set((tracked + untracked).split('\0').filter(Boolean))].sort()
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
        `Werkzeugfehler: ${JSON.stringify(source.worker_errors ?? [])}`,
        source.blocker ?? '',
        source.content ?? 'Nicht ermittelt',
      ].join('\n\n'),
    ),
    '\n## Quellenstatus',
    JSON.stringify(state.summary),
    JSON.stringify(state.diagnostics),
    state.blocker ?? '',
    '',
  ].join('\n\n')
}

// One explicit request, one lock, sequential sources. No scheduler, receipts,
// private clones, rollback or model-driven continuation protocol.
export async function runIngestBatch(input, worker) {
  const opts = { ...defaults, ...input }
  const { wikiRoot, sourceRoot, root, signal } = opts
  const gitRoot = path.join(wikiRoot, '.git')
  if ((await realpath(gitRoot)) !== gitRoot) throw new Error('Unsicheres Git-Verzeichnis')
  return withIngestLock(path.join(gitRoot, 'wiki-ingest-publication.lock'), async () => {
    signal?.throwIfAborted()
    await clean(wikiRoot)
    const scan = () =>
      scanIngestStatus({
        sourceRoot,
        wikiSourceRoot: path.join(wikiRoot, 'sources'),
        includeCurrent: true,
      })
    const status = await scan()
    const candidates = pending(status).filter(
      (source) => !opts.sourcePath || source.source_path === opts.sourcePath,
    )
    if (opts.sourcePath && !candidates.length)
      throw new Error('Angeforderte Quelle ist nicht new/outdated; keine Neuauswertung')
    if (!candidates.length && !status.summary.invalid && !status.summary.conflict)
      return { status: 'done', remaining: 0, summary: status.summary }
    const batchId = `batch-${Date.now()}-${randomBytes(6).toString('hex')}`
    const directory = path.join(root, 'batches', batchId)
    const checkpoint = (await git(wikiRoot, ['rev-parse', 'HEAD'])).trim()
    const state = {
      batch_id: batchId,
      status: 'running',
      checkpoint,
      sources: [],
      summary: status.summary,
      diagnostics: Object.fromEntries(
        Object.entries(status.adapters).map(([adapter, result]) => [
          adapter,
          { invalid: result.invalid, conflict: result.conflict },
        ]),
      ),
    }
    const report = path.join(directory, 'report.md')
    let reportError = null
    const save = async () => {
      try {
        await mkdir(directory, { recursive: true, mode: 0o700 })
        await writeIngestFile(path.join(directory, 'state.json'), JSON.stringify(state, null, 2))
        await writeIngestFile(report, reportText(state))
        reportError = null
      } catch {
        reportError = 'Berichtsspeicherung fehlgeschlagen'
      }
    }
    await save()
    for (const candidate of candidates) {
      const source = { ...candidate, status: 'running', commit: null }
      state.sources.push(source)
      const head = (await git(wikiRoot, ['rev-parse', 'HEAD'])).trim()
      try {
        signal?.throwIfAborted()
        await clean(wikiRoot)
        const adapter = status.adapters[source.adapter]
        if (
          adapter.invalid.some(
            (item) => item.path === source.wiki_path || item.source_key === source.source_key,
          )
        )
          throw new BatchBlocker('Ungültige Quellenseite; übersprungen')
        const digest = hash(await sourceBytes(source, opts))
        const result = await worker({ batch_id: batchId, sources: [candidate] })
        source.content = result?.content
        source.worker_session = result?.session_id
        source.worker_errors = result?.errors
        signal?.throwIfAborted()
        if (result?.errors?.length) {
          const codes = new Set([
            'worker_permission_denied',
            'worker_tool_failed',
            'worker_session_failed',
          ])
          throw new BatchBlocker(
            codes.has(result.errors[0].code) ? result.errors[0].code : 'worker_failed',
          )
        }
        if (typeof source.content === 'string' && source.content.trimStart().startsWith('SKIP:'))
          throw new BatchBlocker('Quelle nicht auswertbar; siehe Extraktionsgrenzen im Bericht')
        if (result?.source_written !== true)
          throw new BatchBlocker(
            'worker_no_source_write: Kein belegter Schreibvorgang an der Quellenseite',
          )
        if (typeof source.content !== 'string' || !source.content.trim())
          throw new BatchBlocker('worker_missing_report: Kein Worker-Bericht')
        if ((await git(wikiRoot, ['rev-parse', 'HEAD'])).trim() !== head)
          throw new BatchBlocker(
            'HEAD während des Einlesens geändert; manuelle Prüfung erforderlich',
          )
        if (hash(await sourceBytes(source, opts)) !== digest)
          throw new BatchBlocker(
            'Quelle während des Einlesens geändert; manuelle Prüfung erforderlich',
          )
        const sourcePage = (await regularFile(source.wiki_path, wikiRoot)).toString('utf8')
        const body = sourcePage.startsWith('---\n')
          ? sourcePage.replace(/^---\n[\s\S]*?\n---\n/, '')
          : sourcePage
        if (!body.trim()) throw new BatchBlocker('Leere Quellenseite')
        const frontmatter = Object.entries(source.frontmatter)
          .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
          .join('\n')
        await writeFile(source.wiki_path, `---\n${frontmatter}\n---\n${body}`)
        // Index/log and source identities belong to code, not the worker.
        const indexPath = path.join(wikiRoot, 'index.md')
        let index = (await regularFile(indexPath, wikiRoot)).toString('utf8')
        const page = path.relative(wikiRoot, source.wiki_path).replace(/\.md$/, '')
        if (/[[\]|<>\r\n]/.test(page)) throw new BatchBlocker('Unsicherer Katalogpfad')
        if (!index.includes(`[[${page}]]`) && !index.includes(`[[${page}|`))
          index += `\n- [[${page}]]\n`
        await writeFile(indexPath, index)
        const logPath = path.join(wikiRoot, 'log.md')
        const log = (await regularFile(logPath, wikiRoot)).toString('utf8')
        await writeFile(logPath, log + `\n- Ingest ${batchId}: [[${page}]].\n`)
        await regularFile(path.join(wikiRoot, 'overview.md'), wikiRoot)
        const changed = await changedPaths(wikiRoot)
        for (const page of changed) {
          if (
            !(
              page === path.relative(wikiRoot, source.wiki_path) ||
              /^(index|overview|log)\.md$/.test(page) ||
              /^(entities|concepts|analyses)\/.+\.md$/.test(page)
            )
          )
            throw new BatchBlocker('Unerlaubter geänderter Wiki-Pfad')
          await regularFile(path.join(wikiRoot, page), wikiRoot)
        }
        const checked = await scan()
        if (
          !checked.adapters[source.adapter]?.current.some(
            (item) =>
              item.source_key === source.source_key &&
              item.source_revision === source.source_revision,
          )
        )
          throw new BatchBlocker('Quellenübernahme nicht verifiziert')
        source.changed_pages = changed
        signal?.throwIfAborted()
        await git(wikiRoot, ['add', '--', ...changed])
        try {
          // Formatting (including Markdown hard breaks) is not an ingest failure.
          await git(wikiRoot, [
            '-c',
            'core.whitespace=-trailing-space,-space-before-tab',
            'diff',
            '--cached',
            '--check',
          ])
        } catch {
          throw new BatchBlocker(
            'Git-Konfliktmarker oder ungültiger Diff; manuelle Prüfung erforderlich',
          )
        }
        await git(wikiRoot, ['commit', '-m', `feat(wiki): ingest source (${batchId})`])
        source.commit = (await git(wikiRoot, ['rev-parse', 'HEAD'])).trim()
        source.status = 'ingested'
      } catch (error) {
        source.blocker =
          error instanceof BatchBlocker
            ? error.message
            : 'Quelle konnte nicht verarbeitet werden; Quelle und Worker prüfen'
        // An isolated failure before writing is not a reason to abandon the run.
        // Unknown live edits/commits cannot safely be attributed or discarded.
        source.status = 'skipped'
        try {
          await clean(wikiRoot)
          if ((await git(wikiRoot, ['rev-parse', 'HEAD'])).trim() !== head)
            throw new Error('HEAD changed', { cause: error })
        } catch {
          source.status = 'blocked'
          state.blocker =
            'Ungeklärte Wiki-Änderungen; manuelle Prüfung erforderlich, nichts zurückgesetzt'
        }
      }
      await save()
      if (state.blocker || signal?.aborted) break
    }
    state.summary = (await scan()).summary
    const remaining = state.summary.new + state.summary.outdated
    if (signal?.aborted) state.blocker ??= 'Einleselauf unterbrochen'
    const incomplete = opts.sourcePath
      ? state.sources.some((source) => source.status !== 'ingested')
      : remaining || state.summary.invalid || state.summary.conflict
    state.status = state.blocker ? 'blocked' : incomplete ? 'partial' : 'done'
    await save()
    return {
      status: state.status,
      batch_id: batchId,
      commit: state.sources.findLast((source) => source.commit)?.commit ?? null,
      ingested: state.sources.filter((source) => source.status === 'ingested').length,
      skipped: state.sources.filter((source) => source.status === 'skipped').length,
      blocker: state.blocker ?? null,
      report_path: reportError ? null : report,
      report_error: reportError,
      remaining,
      summary: state.summary,
    }
  })
}
