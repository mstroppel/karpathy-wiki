import { execFile } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import { link, lstat, mkdir, open, readFile, realpath, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'

import {
  applyPublicationDraft,
  assertCleanIngestWiki,
  checkFresh,
  checkRetainedBackups,
  confinedIngestPath,
  freshSource,
  loadPreparation,
  PREPARATION_RE,
  prepareIngest,
  rollbackPublicationDraft,
  savePreparation,
  validatePublicationDraft,
} from './wiki_ingest_transaction_core.mjs'
import {
  finishRun,
  loadRun,
  normalizeRecord,
  RECORD_MAX_BYTES,
  startRun,
  writeRecord,
} from './wiki_ingest_journal_core.mjs'
import { checkRelativePath, scanIngestStatus } from './wiki_ingest_status_core.mjs'
import { IngestInputError, ingestFailure } from './wiki_ingest_errors.mjs'
import { withIngestLock } from './wiki_ingest_storage.mjs'

const runFile = promisify(execFile)
const SHA = (bytes) => createHash('sha256').update(bytes).digest('hex')
const DEFAULTS = {
  root: '/knowledge/incoming/ingest-journal',
  sourceRoot: '/knowledge/sources',
  wikiRoot: '/knowledge/wiki',
}
const READ_BYTES = 4096

async function git(wikiRoot, args, env = {}) {
  return (
    await runFile('git', ['--no-replace-objects', '--literal-pathspecs', ...args], {
      cwd: wikiRoot,
      encoding: 'buffer',
      maxBuffer: 32 * 1024 * 1024,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0', ...env },
    })
  ).stdout
}

async function withWikiLock(opts, action) {
  const directory = path.join(opts.wikiRoot, '.git')
  if ((await realpath(directory)) !== directory) throw new Error('Unsicheres Git-Verzeichnis')
  return withIngestLock(path.join(directory, 'wiki-ingest-publication.lock'), action)
}

function draftOptions(opts, receipt) {
  const directory = path.join(opts.root, 'preparations', receipt.preparation_id)
  return {
    ...opts,
    root: path.join(directory, 'journal'),
    wikiRoot: path.join(directory, 'wiki'),
    preparationId: receipt.publication.draft_id,
  }
}

async function prepare(opts) {
  const source = await freshSource(opts)
  try {
    if (
      opts.changedPages !== undefined &&
      (!Array.isArray(opts.changedPages) || opts.changedPages.length > 100)
    )
      throw new Error('changed_pages muss eine Liste relativer Wiki-Pfade sein')
    opts.changedPages = [
      ...new Set([
        source.page,
        'overview.md',
        'index.md',
        'log.md',
        ...(opts.changedPages ?? []).map((page) => checkRelativePath(page, 'changed_pages')),
      ]),
    ]
  } catch (error) {
    throw new IngestInputError('invalid_pages', error.message)
  }
  if (opts.runId !== undefined) {
    const run = await loadRun({ root: opts.root, runId: opts.runId })
    if (run.state !== 'running')
      throw new IngestInputError('closed_run', 'Lauf ist bereits geschlossen')
  }
  const budgetTokens = opts.budgetTokens ?? 32000
  if (!Number.isInteger(budgetTokens) || budgetTokens < 1000 || budgetTokens > 1000000)
    throw new IngestInputError('invalid_budget', 'Ungültiges Kontextbudget')
  const result = await prepareIngest(opts)
  opts.preparationId = result.preparation_id
  const receipt = await loadPreparation({ ...opts, preparationId: result.preparation_id })
  const directory = path.join(opts.root, 'preparations', result.preparation_id)
  await mkdir(directory, { mode: 0o700 })
  const wikiRoot = path.join(directory, 'wiki')
  await runFile('git', ['clone', '--quiet', '--no-hardlinks', '--', opts.wikiRoot, wikiRoot])
  if ((await git(wikiRoot, ['rev-parse', 'HEAD'])).toString().trim() !== receipt.base_commit)
    throw new Error('HEAD hat sich während prepare geändert')
  for (const setting of ['user.name', 'user.email']) {
    await git(wikiRoot, [
      'config',
      setting,
      (await git(opts.wikiRoot, ['config', '--get', setting])).toString().trim(),
    ])
  }
  const root = path.join(directory, 'journal')
  const draft = await prepareIngest({ ...opts, root, wikiRoot })
  // Only create an implicit run once all draft setup has succeeded. If the
  // final receipt write fails, close only this call's run, never a supplied one.
  const runId = opts.runId ?? (await startRun({ root: opts.root, resume: false })).run.run_id
  receipt.publication = {
    single_source: opts.runId === undefined,
    draft_id: draft.preparation_id,
    run_id: runId,
    references: {},
    stage_requests: {},
    source_ranges: [],
    phase: 'draft',
    read_bytes: 0,
    proposal_bytes: 0,
    budget_tokens: budgetTokens,
    context_bytes_limit: Math.max(0, budgetTokens - 12000) * 2,
  }
  try {
    await savePreparation(opts.root, receipt)
  } catch (error) {
    await closeSingleRun(opts, receipt.publication, [
      {
        source_path: source.source_path,
        blocker: 'Initialisierung der Publikation fehlgeschlagen',
      },
    ])
    throw error
  }
  return { ...result, run_id: runId, isolation: 'private-draft', read_bytes: READ_BYTES }
}

function spend(receipt, bytes, kind) {
  const pub = receipt.publication
  if (pub.read_bytes + pub.proposal_bytes + bytes > pub.context_bytes_limit)
    throw new Error(
      'Kontext-Arbeitsbudget ausgeschöpft; keine weitere Extraktion behaupten, Quelle als Blocker melden',
    )
  pub[kind] += bytes
}

async function inspect(opts, receipt, source = false) {
  const draft = draftOptions(opts, receipt)
  const page = opts.page ?? receipt.source.page
  if (!source && !Object.hasOwn(receipt.baseline, page))
    throw new IngestInputError('undeclared_page', 'Seite ist nicht deklariert')
  if (source) await checkFresh(opts, receipt)
  const file = source
    ? await confinedIngestPath(
        opts.sourceRoot,
        path.relative(opts.sourceRoot, receipt.source.source_path),
      )
    : await confinedIngestPath(draft.wikiRoot, page, true)
  let bytes
  try {
    bytes = await readFile(file)
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
    bytes = Buffer.alloc(0)
  }
  const text = bytes.toString('utf8')
  if (!Buffer.from(text).equals(bytes)) throw new Error('Wiki-Seite ist kein gültiger UTF-8-Text')
  const lines = text.split(/(?<=\n)/u)
  let offset = opts.offset ?? 1
  const limit = opts.limit ?? 30
  if (
    !Number.isInteger(offset) ||
    offset < 1 ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 80
  )
    throw new IngestInputError(
      'invalid_range',
      'offset >= 1 und limit zwischen 1 und 80 erforderlich',
    )
  if (opts.query !== undefined) {
    if (typeof opts.query !== 'string' || !opts.query)
      throw new IngestInputError('invalid_query', 'query muss nichtleer sein')
    const found = lines.findIndex((line, index) => index >= offset - 1 && line.includes(opts.query))
    if (found < 0) return { page, found: false, next_offset: null }
    offset = found + 1
  }
  const start = lines.slice(0, offset - 1).join('').length
  let selected = ''
  let endLine = offset - 1
  for (const line of lines.slice(offset - 1, offset - 1 + limit)) {
    if (Buffer.byteLength(selected + line) > READ_BYTES) break
    selected += line
    endLine += 1
  }
  if (!selected && start < text.length)
    throw new IngestInputError(
      'oversized_line',
      'Einzelzeile über Lesebudget; Quelle/Seite benötigt gezielte Wartung',
    )
  const reference = SHA(JSON.stringify([page, SHA(bytes), start, selected.length]))
  spend(receipt, Buffer.byteLength(selected), 'read_bytes')
  if (source) {
    receipt.publication.source_ranges.push([start, start + selected.length])
    receipt.publication.source_length = text.length
  } else
    receipt.publication.references[reference] = {
      page,
      hash: SHA(bytes),
      start,
      end: start + selected.length,
    }
  await savePreparation(opts.root, receipt)
  return {
    page: source ? receipt.source.source_path : page,
    found: true,
    offset,
    end_line: endLine,
    text: selected,
    ...(source ? {} : { reference }),
    next_offset: start + selected.length < text.length ? endLine + 1 : null,
    bytes_read: receipt.publication.read_bytes,
  }
}

async function stage(opts, receipt) {
  if (receipt.publication.phase !== 'draft')
    throw new Error('Publikation bereits gestartet; inspect/resume verwenden')
  const draft = draftOptions(opts, receipt)
  const page = opts.page ?? receipt.source.page
  if (page === 'index.md' || page === 'log.md')
    throw new IngestInputError('generated_page', 'Index und Log erzeugt der Publisher')
  const supplied = [
    opts.draft !== undefined,
    opts.append !== undefined,
    opts.reference !== undefined,
    opts.reviewed === true,
  ].filter(Boolean).length
  if (supplied !== 1 || (opts.replacement !== undefined && opts.reference === undefined))
    throw new IngestInputError(
      'invalid_stage',
      'Genau draft, append, reference mit replacement oder reviewed verwenden',
    )
  const requestHash = SHA(
    JSON.stringify([
      page,
      opts.draft,
      opts.append,
      opts.reference,
      opts.replacement,
      opts.reviewed,
    ]),
  )
  const previous = receipt.publication.stage_requests[requestHash]
  if (previous) return previous
  const pendingRequest = receipt.publication.stage_pending
  if (pendingRequest?.request === requestHash && pendingRequest.after) {
    const bytes = await readFile(await confinedIngestPath(draft.wikiRoot, page))
    const draftReceipt = await loadPreparation(draft)
    if (SHA(bytes) === pendingRequest.after && draftReceipt.owned[page] === pendingRequest.after) {
      const result = {
        preparation_id: receipt.preparation_id,
        page,
        staged: true,
        live_unchanged: true,
      }
      receipt.publication.stage_requests[requestHash] = result
      receipt.publication.stage_pending = null
      await savePreparation(opts.root, receipt)
      return result
    }
  }
  let request = { ...draft, page, draft: opts.draft, append: opts.append }
  if (opts.reference !== undefined) {
    const ref = receipt.publication.references[opts.reference]
    if (!ref || ref.page !== page || typeof opts.replacement !== 'string')
      throw new IngestInputError(
        'invalid_reference',
        'Gültige Abschnittsreferenz und replacement erforderlich',
      )
    const text = await readFile(await confinedIngestPath(draft.wikiRoot, page), 'utf8')
    if (SHA(Buffer.from(text)) !== ref.hash)
      throw new IngestInputError(
        'stale_reference',
        'Seite seit inspect geändert; Abschnitt erneut lesen',
      )
    const old = text.slice(ref.start, ref.end)
    // Reading context does not authorize replacing it from memory. Preserve
    // every surrounding nonblank line; one targeted line may change per edit.
    // Broader rewrites require separate narrow references, never inferred loss.
    const replacementLines = opts.replacement.split('\n')
    let cursor = 0
    let removed = 0
    for (const line of old.split('\n').filter((line) => line.trim())) {
      const found = replacementLines.indexOf(line, cursor)
      if (found < 0) removed += 1
      else cursor = found + 1
    }
    if (removed > 1)
      throw new IngestInputError(
        'context_loss',
        'replacement entfernt mehrere Kontextzeilen; Ziel mit inspect limit: 1 neu referenzieren oder alle übrigen Zeilen erhalten',
      )
    // The reference selects a byte-exact range, not a model-reconstructed anchor.
    // Apply the complete server-built page as one unique replacement.
    request = {
      ...draft,
      page,
      edits: [
        {
          old_text: text,
          new_text: text.slice(0, ref.start) + opts.replacement + text.slice(ref.end),
        },
      ],
    }
    if (!old && !text) request = { ...draft, page, append: opts.replacement }
  } else if (opts.reviewed === true) request = { ...draft, page, edits: [] }
  const current = await readFile(
    await confinedIngestPath(draft.wikiRoot, page, true),
    'utf8',
  ).catch((error) => {
    if (error.code === 'ENOENT') return ''
    throw error
  })
  const after =
    request.append !== undefined ? current + request.append : request.edits?.[0]?.new_text
  const pending = receipt.publication.stage_pending
  if (pending && pending.request !== requestHash)
    throw new Error(
      'Ungeklärter früherer stage-Auftrag; Zustand prüfen, keinen neuen Auftrag schreiben',
    )
  if (!pending) {
    spend(
      receipt,
      Buffer.byteLength(opts.draft ?? opts.append ?? opts.replacement ?? ''),
      'proposal_bytes',
    )
    receipt.publication.stage_pending = {
      request: requestHash,
      page,
      after: after === undefined ? null : SHA(Buffer.from(after)),
    }
    await savePreparation(opts.root, receipt)
  }
  const draftReceipt = await loadPreparation(draft)
  if (
    !(
      pending?.after &&
      SHA(Buffer.from(current)) === pending.after &&
      draftReceipt.owned[page] === pending.after
    )
  ) {
    try {
      await applyPublicationDraft(request)
    } catch (error) {
      if (error instanceof IngestInputError) {
        receipt.publication.stage_pending = null
        await savePreparation(opts.root, receipt)
      }
      throw error
    }
  }
  const result = {
    preparation_id: receipt.preparation_id,
    page,
    staged: true,
    live_unchanged: true,
  }
  receipt.publication.stage_requests[requestHash] = result
  receipt.publication.stage_pending = null
  await savePreparation(opts.root, receipt)
  return result
}

async function declare(opts, receipt) {
  if (receipt.publication.phase !== 'draft') throw new Error('Publikation bereits gestartet')
  if (
    !Array.isArray(opts.changedPages) ||
    !opts.changedPages.length ||
    opts.changedPages.length > 100
  )
    throw new IngestInputError('invalid_pages', 'changed_pages muss 1 bis 100 Pfade enthalten')
  await checkFresh(opts, receipt)
  await assertCleanIngestWiki(opts.wikiRoot)
  if ((await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).toString().trim() !== receipt.base_commit)
    throw new Error('HEAD hat sich geändert')
  const draft = draftOptions(opts, receipt)
  const evidence = await loadPreparation(draft)
  for (const page of opts.changedPages) {
    if (Object.hasOwn(receipt.baseline, page)) continue
    if (typeof page !== 'string' || !page.endsWith('.md') || page.startsWith('sources/'))
      throw new IngestInputError(
        'invalid_pages',
        'Nur zusätzliche thematische Markdown-Seiten deklarieren',
      )
    const read = async (wikiRoot) =>
      readFile(await confinedIngestPath(wikiRoot, page, true)).catch((error) => {
        if (error.code === 'ENOENT') return null
        throw error
      })
    const live = await read(opts.wikiRoot)
    const isolated = await read(draft.wikiRoot)
    if (live === null ? isolated !== null : isolated === null || !live.equals(isolated))
      throw new Error('Privater Entwurf weicht von der deklarierbaren Ausgangsseite ab')
    receipt.baseline[page] = live === null ? null : SHA(live)
    evidence.baseline[page] = receipt.baseline[page]
  }
  if (Object.keys(receipt.baseline).length > 100)
    throw new IngestInputError('invalid_pages', 'Höchstens 100 deklarierte Seiten')
  await savePreparation(draft.root, evidence)
  await savePreparation(opts.root, receipt)
  return { preparation_id: receipt.preparation_id, changed_pages: Object.keys(receipt.baseline) }
}

function label(value) {
  if (
    typeof value !== 'string' ||
    !value.trim() ||
    value.length > 200 ||
    Array.from(value).some(
      (char) => char.codePointAt(0) < 32 || char.codePointAt(0) === 127 || '|[]<>'.includes(char),
    )
  )
    throw new IngestInputError(
      'invalid_title',
      'title muss ein einfacher einzeiliger Text mit höchstens 200 Zeichen sein',
    )
  return value.trim()
}

async function seal(opts, receipt) {
  if (receipt.publication.stage_pending)
    throw new Error('Ungeklärter stage-Auftrag; zuerst Zustand prüfen')
  let covered = 0
  for (const [start, end] of [...receipt.publication.source_ranges].sort((a, b) => a[0] - b[0])) {
    if (start > covered) break
    covered = Math.max(covered, end)
  }
  if (
    receipt.publication.source_length === undefined ||
    covered < receipt.publication.source_length
  )
    throw new IngestInputError(
      'incomplete_source_read',
      'Quelle zuerst vollständig über read_source lesen',
    )
  const title = label(opts.title)
  const run = await loadRun({ root: opts.root, runId: opts.runId })
  if (run.state !== 'running')
    throw new IngestInputError('closed_run', 'Lauf ist bereits geschlossen')
  let record
  try {
    record = normalizeRecord({
      ...receipt.source,
      preparation_id: receipt.preparation_id,
      status: 'ingested',
      commit: '0'.repeat(40),
      changed_pages: Object.keys(receipt.baseline),
      content: opts.content,
      contradictions: opts.contradictions,
      extraction_limits: opts.extractionLimits,
      source_unmodified: true,
      blocker: null,
    })
    if (
      Buffer.byteLength(JSON.stringify({ ...record, recorded_at: new Date().toISOString() })) >
      RECORD_MAX_BYTES
    )
      throw new Error('Ergebnisdatensatz überschreitet das Byte-Budget')
  } catch (error) {
    throw new IngestInputError('invalid_record', error.message)
  }
  const draft = draftOptions(opts, receipt)
  const sourcePage = receipt.source.page.replace(/\.md$/, '')
  if (Array.from(sourcePage).some((char) => '[]|<>\r\n'.includes(char)))
    throw new IngestInputError('invalid_link', 'Quellseitenpfad ist kein sicherer Wikilink')
  const link = `[[${sourcePage}|${title}]]`
  const indexFile = await confinedIngestPath(draft.wikiRoot, 'index.md')
  const index = await readFile(indexFile, 'utf8')
  if (!index.includes(`[[${sourcePage}|`) && !index.includes(`[[${sourcePage}]]`))
    await applyPublicationDraft({ ...draft, page: 'index.md', append: `\n- ${link}\n` })
  else await applyPublicationDraft({ ...draft, page: 'index.md', edits: [] })
  const sourceLabel = JSON.stringify(`${receipt.source.adapter}/${receipt.source.source_key}`)
    .replaceAll('[', '\\[')
    .replaceAll(']', '\\]')
  const log = `\n- Quelle eingelesen: ${link}; ${sourceLabel}; Revision ${receipt.source.source_revision}.\n`
  const previousLog = (
    await git(draft.wikiRoot, ['cat-file', 'blob', `${receipt.base_commit}:log.md`])
  ).toString('utf8')
  const currentLog = await readFile(await confinedIngestPath(draft.wikiRoot, 'log.md'), 'utf8')
  if (currentLog !== previousLog + log) {
    if (currentLog !== previousLog)
      throw new Error('Privater Logentwurf hat unerwartete Änderungen')
    await applyPublicationDraft({ ...draft, page: 'log.md', append: log })
  }
  const validated = await validatePublicationDraft(draft)
  record.changed_pages = validated.changed_pages
  receipt.publication = {
    ...receipt.publication,
    phase: 'sealing',
    title,
    record,
    run_id: opts.runId,
  }
  await savePreparation(opts.root, receipt)
  await finishSeal(opts, receipt)
}

async function finishSeal(opts, receipt) {
  const draft = draftOptions(opts, receipt)
  const evidence = await loadPreparation(draft)
  await checkFresh(draft, evidence)
  await checkRetainedBackups(draft.wikiRoot, evidence)
  for (const [page, baseline] of Object.entries(evidence.baseline)) {
    const bytes = await readFile(await confinedIngestPath(draft.wikiRoot, page, true)).catch(
      (error) => {
        if (error.code === 'ENOENT') return null
        throw error
      },
    )
    if ((bytes === null ? null : SHA(bytes)) !== (evidence.owned[page] ?? baseline))
      throw new Error('Privater Entwurf seit Validierung geändert')
  }
  const record = receipt.publication.record
  await git(draft.wikiRoot, ['add', '--', ...record.changed_pages])
  const tree = (await git(draft.wikiRoot, ['write-tree'])).toString().trim()
  const commit = (
    await git(draft.wikiRoot, [
      '-c',
      'commit.gpgsign=false',
      'commit-tree',
      tree,
      '-p',
      receipt.base_commit,
      '-m',
      `ingest(${receipt.source.adapter}): ${receipt.publication.title}`,
    ])
  )
    .toString()
    .trim()
  const changed = (
    await git(draft.wikiRoot, [
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
  if (JSON.stringify(changed) !== JSON.stringify([...record.changed_pages].sort()))
    throw new Error('Versiegelter Commit enthält unerwartete Pfade')
  for (const [page, digest] of Object.entries(evidence.validated)) {
    if (SHA(await git(draft.wikiRoot, ['cat-file', 'blob', `${commit}:${page}`])) !== digest)
      throw new Error('Git-Filter hat den validierten Entwurf verändert')
  }
  receipt.publication = {
    ...receipt.publication,
    phase: 'sealed',
    commit,
    run_id: receipt.publication.run_id,
    record: { ...record, commit },
  }
  await savePreparation(opts.root, receipt)
}

async function publish(opts, receipt) {
  opts.runId ??= receipt.publication.run_id
  if (opts.runId !== receipt.publication.run_id)
    throw new IngestInputError('wrong_run', 'run_id weicht vom vorbereiteten Lauf ab')
  if (receipt.publication.phase === 'done') {
    await closeSingleRun(opts, receipt.publication)
    return receipt.publication.result
  }
  if (receipt.publication.phase === 'draft') {
    await checkFresh(opts, receipt)
    await assertCleanIngestWiki(opts.wikiRoot)
    await seal(opts, receipt)
  }
  if (receipt.publication.phase === 'sealing') await finishSeal(opts, receipt)
  const pub = receipt.publication
  const draft = draftOptions(opts, receipt)
  const head = (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).toString().trim()
  if (head !== pub.commit && head !== receipt.base_commit)
    throw new Error('HEAD hat sich geändert; keine fremde Arbeit übernehmen')
  if (head === receipt.base_commit) {
    await checkFresh(opts, receipt)
    // Inspect every live path before installing even the first proposed page.
    // A resume permits only byte-owned drafts, never unrelated changes.
    const dirty = (
      await git(opts.wikiRoot, ['status', '--porcelain=v1', '-z', '--untracked-files=all'])
    )
      .toString()
      .split('\0')
      .filter(Boolean)
    for (const entry of dirty) {
      if (!entry.startsWith(' M ') && !entry.startsWith('?? '))
        throw new Error('Fremde staged Änderungen')
      if (!Object.hasOwn(receipt.baseline, entry.slice(3)))
        throw new Error('Fremde undeklarierte Änderungen')
    }
    for (const [page, baseline] of Object.entries(receipt.baseline)) {
      const bytes = await readFile(await confinedIngestPath(opts.wikiRoot, page, true)).catch(
        (error) => {
          if (error.code === 'ENOENT') return null
          throw error
        },
      )
      const actual = bytes === null ? null : SHA(bytes)
      const pending = receipt.pending?.page === page ? receipt.pending : null
      if (
        actual !== (receipt.owned[page] ?? baseline) &&
        (!pending || (actual !== pending.before && actual !== pending.after && actual !== null))
      )
        throw new Error(`Fremde worktree Änderung an ${page}`)
    }
    pub.phase = 'installing'
    receipt.publication = pub
    await savePreparation(opts.root, receipt)
    for (const page of Object.keys(receipt.baseline)) {
      let proposed
      try {
        proposed = (
          await git(draft.wikiRoot, ['cat-file', 'blob', `${pub.commit}:${page}`])
        ).toString('utf8')
      } catch (error) {
        if (receipt.baseline[page] === null && !pub.record.changed_pages.includes(page)) continue
        throw error
      }
      const current = await readFile(
        await confinedIngestPath(opts.wikiRoot, page, true),
        'utf8',
      ).catch((error) => {
        if (error.code === 'ENOENT') return null
        throw error
      })
      const currentHash = current === null ? null : SHA(Buffer.from(current))
      const owned = receipt.owned[page]
      if (owned && currentHash === owned && current === proposed) continue
      if (page === receipt.source.page || current === null)
        await applyPublicationDraft({ ...opts, page, draft: proposed })
      else if (page === 'log.md') {
        if (!proposed.startsWith(current)) throw new Error('Log ist nicht nur ergänzt')
        await applyPublicationDraft({ ...opts, page, append: proposed.slice(current.length) })
      } else
        await applyPublicationDraft({
          ...opts,
          page,
          edits: current === proposed ? [] : [{ old_text: current, new_text: proposed }],
        })
      // apply updates the durable receipt; never overwrite its ownership state.
      receipt = await loadPreparation(opts)
    }
    await validatePublicationDraft(opts)
    receipt = await loadPreparation(opts)
    for (const [page, digest] of Object.entries(receipt.validated)) {
      if (SHA(await git(draft.wikiRoot, ['cat-file', 'blob', `${pub.commit}:${page}`])) !== digest)
        throw new Error('Live-Validierung weicht vom versiegelten Entwurf ab')
    }
    await git(opts.wikiRoot, [
      'fetch',
      '--quiet',
      '--no-tags',
      '--no-write-fetch-head',
      '--',
      draft.wikiRoot,
      pub.commit,
    ])
    // A complete index built in the isolated repository accompanies the CAS
    // ref update. Persist bytes/intent before changing HEAD, for restart recovery.
    const indexBytes = await readFile(path.join(draft.wikiRoot, '.git', 'index'))
    const indexFile = path.join(opts.wikiRoot, '.git', 'index')
    if (!(await lstat(indexFile)).isFile() || (await lstat(indexFile)).isSymbolicLink())
      throw new Error('Unsicherer Git-Index')
    // Stat-cache refreshes do not stage content. Pin current pristine index
    // bytes each attempt while HEAD remains at the same recorded base.
    if (
      (await git(opts.wikiRoot, ['diff', '--cached', '--name-only', '-z', receipt.base_commit]))
        .length
    )
      throw new Error('Fremde staged Änderungen vor Veröffentlichung')
    pub.index_before = SHA(await readFile(indexFile))
    pub.index_after = SHA(indexBytes)
    receipt.publication = pub
    await savePreparation(opts.root, receipt)
    const lockFile = path.join(opts.wikiRoot, '.git', 'index.lock')
    if (!pub.index_lock) {
      // Fully prepare and persist inode evidence before occupying index.lock.
      // A crash before the receipt leaves only an inert random candidate; after
      // the receipt, linking is repeatable and ownership is already provable.
      const candidate = `ingest-index-${randomBytes(16).toString('hex')}`
      const handle = await open(
        path.join(opts.wikiRoot, '.git', candidate),
        constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW,
        0o600,
      )
      try {
        await handle.writeFile(indexBytes)
        await handle.sync()
        const info = await handle.stat()
        pub.index_lock = { dev: info.dev, ino: info.ino, candidate }
      } finally {
        await handle.close()
      }
      receipt.publication = pub
      await savePreparation(opts.root, receipt)
    }
    const candidate = path.join(opts.wikiRoot, '.git', pub.index_lock.candidate)
    const candidateInfo = await lstat(candidate)
    if (
      !candidateInfo.isFile() ||
      candidateInfo.dev !== pub.index_lock.dev ||
      candidateInfo.ino !== pub.index_lock.ino ||
      SHA(await readFile(candidate)) !== pub.index_after
    )
      throw new Error('Indexkandidat ist verändert; kein Überschreiben')
    try {
      await link(candidate, lockFile)
    } catch (error) {
      if (error.code !== 'EEXIST') throw error
      const info = await lstat(lockFile)
      if (!info.isFile() || info.dev !== pub.index_lock.dev || info.ino !== pub.index_lock.ino)
        throw new Error('Fremder Index-Lock; bestätigte Wartung erforderlich', { cause: error })
    }
    if (SHA(await readFile(indexFile)) !== pub.index_before)
      throw new Error('Fremde Indexänderung; keine Veröffentlichung')
    await validatePublicationDraft(opts)
    await git(opts.wikiRoot, ['update-ref', 'HEAD', pub.commit, receipt.base_commit])
    await rename(path.join(opts.wikiRoot, '.git', 'index.lock'), indexFile)
  } else if (pub.index_after) {
    const indexFile = path.join(opts.wikiRoot, '.git', 'index')
    const actual = SHA(await readFile(indexFile))
    const matchesCommit = !(
      await git(opts.wikiRoot, ['diff', '--cached', '--name-only', '-z', pub.commit])
    ).length
    if (actual !== pub.index_after && !matchesCommit) {
      if (actual !== pub.index_before) throw new Error('Fremde Indexänderung nach Commit')
      const lockFile = path.join(opts.wikiRoot, '.git', 'index.lock')
      const info = await lstat(lockFile)
      if (
        !info.isFile() ||
        info.dev !== pub.index_lock?.dev ||
        info.ino !== pub.index_lock?.ino ||
        SHA(await readFile(lockFile)) !== pub.index_after
      )
        throw new Error('Indexbeleg fehlt oder ist verändert')
      await rename(lockFile, indexFile)
    }
  }
  const record = pub.record
  const run = await loadRun({ root: opts.root, runId: pub.run_id })
  const already = run.records.find(
    (item) =>
      item.preparation_id === receipt.preparation_id &&
      item.commit === pub.commit &&
      item.status === 'ingested',
  )
  if (!already) await writeRecord({ ...opts, runId: pub.run_id, record })
  const result = {
    preparation_id: receipt.preparation_id,
    source_path: record.source_path,
    commit: pub.commit,
    status: 'ingested',
    changed_pages: record.changed_pages,
    run_id: pub.run_id,
  }
  receipt = await loadPreparation(opts)
  receipt.publication = { ...pub, phase: 'done', result }
  await savePreparation(opts.root, receipt)
  await closeSingleRun(opts, receipt.publication)
  return result
}

async function closeSingleRun(opts, pub, unfinished = []) {
  if (!pub.single_source) return
  const run = await loadRun({ root: opts.root, runId: pub.run_id })
  if (run.state === 'completed') return
  const status = await scanIngestStatus({
    sourceRoot: opts.sourceRoot,
    wikiSourceRoot: path.join(opts.wikiRoot, 'sources'),
  })
  // This run's scope is the explicitly selected source, not the whole backlog.
  await finishRun({
    root: opts.root,
    runId: pub.run_id,
    finalStatus: status.summary,
    unfinished,
  })
}

export async function ingestPublication(input) {
  const opts = { ...DEFAULTS, ...input }
  // Missing/malformed identity is rejected before locks, receipts or wiki
  // writes; it is an input correction, not an unknown publication outcome.
  if (
    opts.operation !== 'prepare' &&
    (typeof opts.preparationId !== 'string' || !PREPARATION_RE.test(opts.preparationId))
  )
    throw new IngestInputError(
      'invalid_preparation',
      'Vollständige preparation_id aus prepare erforderlich',
    )
  return withWikiLock(opts, async () => {
    try {
      if (opts.operation === 'prepare') return await prepare(opts)
      const receipt = await loadPreparation(opts)
      if (receipt.wiki_root !== path.resolve(opts.wikiRoot))
        throw new Error('Vorbereitung gehört nicht zu diesem Publisher/Wiki')
      if (opts.operation === 'rollback') {
        const pub = receipt.publication
        if (
          pub?.index_lock &&
          (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).toString().trim() ===
            receipt.base_commit
        ) {
          const lockFile = path.join(opts.wikiRoot, '.git', 'index.lock')
          const info = await lstat(lockFile)
          if (
            !info.isFile() ||
            info.dev !== pub.index_lock.dev ||
            info.ino !== pub.index_lock.ino ||
            SHA(await readFile(lockFile)) !== pub.index_after
          )
            throw new Error('Fremder/geänderter Index-Lock; kein Rücksetzen')
          if (opts.confirmed !== true)
            throw new Error('Rücksetzen benötigt ausdrückliche Bestätigung')
          await rm(lockFile)
        }
        return await rollbackPublicationDraft(opts)
      }
      if (!receipt.publication)
        throw new Error('Initialisierung unvollständig; bestätigtes Rücksetzen erforderlich')
      if (
        ['stage', 'publish'].includes(opts.operation) &&
        (receipt.publication.input_errors?.[opts.operation] ?? 0) >= 2
      )
        throw new Error('Eingabekorrektur-Limit erreicht; Quelle als Blocker melden')
      if (['inspect', 'read_source', 'stage', 'declare'].includes(opts.operation)) {
        if (receipt.rolled_back || receipt.rollback_started)
          throw new Error('Vorbereitung ist zurückgesetzt')
        receipt.publication.tool_calls = (receipt.publication.tool_calls ?? 0) + 1
        if (receipt.publication.tool_calls > 48)
          throw new Error('Kontext-Arbeitsbudget: Toolaufruf-Limit erreicht')
        await savePreparation(opts.root, receipt)
      }
      switch (opts.operation) {
        case 'declare':
          return await declare(opts, receipt)
        case 'state':
          return {
            preparation_id: receipt.preparation_id,
            phase: receipt.publication.phase,
            commit: receipt.publication.commit ?? null,
            source: receipt.source,
            read_bytes: receipt.publication.read_bytes,
            proposal_bytes: receipt.publication.proposal_bytes,
            context_bytes_limit: receipt.publication.context_bytes_limit,
            stage_pending: receipt.publication.stage_pending ?? null,
          }
        case 'read_source':
          return await inspect(opts, receipt, true)
        case 'inspect':
          return await inspect(opts, receipt)
        case 'stage':
          return await stage(opts, receipt)
        case 'publish':
        case 'resume':
          return await publish(opts, receipt)
        default:
          throw new IngestInputError('invalid_operation', 'Unbekannte Publikationsoperation')
      }
    } catch (error) {
      error.ingest = ingestFailure(error, opts.preparationId)
      if (error instanceof IngestInputError && opts.preparationId) {
        const receipt = await loadPreparation(opts)
        if (receipt.publication) {
          if (['sealing', 'sealed', 'installing', 'done'].includes(receipt.publication.phase)) {
            error.ingest.correctable = false
            error.ingest.write_state = 'unknown'
            throw error
          }
          const counts = receipt.publication.input_errors ?? {}
          counts[opts.operation] = (counts[opts.operation] ?? 0) + 1
          receipt.publication.input_errors = counts
          await savePreparation(opts.root, receipt)
          error.ingest.correctable = counts[opts.operation] <= 1
        }
      }
      throw error
    }
  })
}
