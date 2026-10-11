import { createHash, randomBytes } from 'node:crypto'
import { execFile } from 'node:child_process'
import { constants } from 'node:fs'
import { mkdir, open, readFile, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'
import { publisherControl } from './control.mjs'
import { assertCleanIngestWiki } from '../config/tools/wiki_ingest_transaction_core.mjs'
import { withWikiLock } from '../config/tools/wiki_ingest_publication_core.mjs'
import { withIngestLock } from '../config/tools/wiki_ingest_storage.mjs'

const runFile = promisify(execFile)
const SHA = (value) => createHash('sha256').update(value).digest('hex')
const SESSION = /^ses[a-zA-Z0-9_-]{1,100}$/
const REQUEST = /^req-[a-zA-Z0-9_-]{1,96}$/
const TARGET = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const COMMIT_RE = /^[0-9a-f]{40}$/
const DIGEST_RE = /^[0-9a-f]{64}$/
// The prepared block is exactly one single-line JSON object inside a fence.
// Single-line JSON keeps the fence unambiguous even for content with code
// blocks; the fence name never occurs in normal conversation output.
const FENCE = /```karpathy-authoring\n([^\n]*)\n```/g
const KINDS = ['analysis', 'answers']
const STATUSES = ['idle', 'writing', 'completed', 'operator_action_required']
const PHASES = ['extract', 'writing', 'summary']
const BLOCKERS = ['interrupted_authoring', 'authoring_uncertain', 'summary_admission_uncertain']
const TRUNCATION = ['[showing ', 'full output saved to']
const ANSWER_END = '<!-- END CONFIRMED ANSWERS -->'
const DATE_LINE = /^Erstellt: \d{4}-\d{2}-\d{2}$/
const MAX_CONTENT = 256 * 1024
const MAX_BLOCK = 512 * 1024
const MAX_TITLE = 200
const STATE_KEYS = [
  'version',
  'status',
  'session_id',
  'request_id',
  'kind',
  'target',
  'digest',
  'rendered',
  'base',
  'phase',
  'blocker',
  'result',
  'status_delivery_failed',
]
const RESULT_KEYS = ['kind', 'name', 'commit', 'changed_pages', 'path']

const defaults = {
  stateRoot: '/knowledge/publisher',
  wikiRoot: '/knowledge/wiki',
  answersRoot: '/knowledge/incoming/answers',
  wikiUrl: null,
  renderCommand: 'render-analysis',
}

const fail = (name) => {
  throw new Error(name)
}

const strictKeys = (value, keys) =>
  value !== null &&
  typeof value === 'object' &&
  !Array.isArray(value) &&
  Object.keys(value).length === keys.length &&
  keys.every((key) => Object.hasOwn(value, key))

const readExisting = (file) =>
  readFile(file).then(
    (bytes) => bytes,
    (error) => {
      if (error.code === 'ENOENT') return null
      throw error
    },
  )

// Durable coordination state stays content-free: identifiers, digests and
// paths only. Content lives in its destination page or inbox file; a state
// smuggle of content is refused untouched as invalid state.
export function validate(state) {
  const invalid = () => fail('invalid_authoring_state')
  if (!strictKeys(state, STATE_KEYS) || state.version !== 1) invalid()
  if (!STATUSES.includes(state.status)) invalid()
  if (!KINDS.includes(state.kind) && state.kind !== null) invalid()
  if (
    state.session_id !== null &&
    (typeof state.session_id !== 'string' || !SESSION.test(state.session_id))
  )
    invalid()
  for (const [name, pattern] of [
    ['request_id', REQUEST],
    ['target', TARGET],
    ['digest', DIGEST_RE],
    ['rendered', DIGEST_RE],
    ['base', COMMIT_RE],
  ])
    if (state[name] !== null && (typeof state[name] !== 'string' || !pattern.test(state[name])))
      invalid()
  if (state.phase !== null && !PHASES.includes(state.phase)) invalid()
  if (state.blocker !== null && !BLOCKERS.includes(state.blocker)) invalid()
  if (typeof state.status_delivery_failed !== 'boolean') invalid()
  const idle = state.status === 'idle'
  if (
    idle !== (state.request_id === null) ||
    idle !== (state.session_id === null) ||
    idle !== (state.kind === null)
  )
    invalid()
  if (state.phase !== null && state.status !== 'writing') invalid()
  if (state.status === 'writing' && state.phase === null) invalid()
  if (
    idle &&
    (state.target !== null ||
      state.result !== null ||
      state.blocker !== null ||
      state.status_delivery_failed)
  )
    invalid()
  if (state.status === 'writing') {
    if (state.blocker !== null) invalid()
    // The verified result exists only once the write completed and its
    // summary is being delivered.
    if (state.phase === 'summary' ? state.result === null : state.result !== null) invalid()
    // Extraction records intent first; digests and base follow once the
    // prepared content is in hand and before the first write.
    if (
      state.phase !== 'extract' &&
      (state.target === null || state.digest === null || state.base === null)
    )
      invalid()
  }
  if (state.status === 'completed') {
    if (state.result === null || state.phase !== null || state.blocker !== null) invalid()
    if (state.request_id === null || state.session_id === null || state.kind === null) invalid()
    if (state.target === null || state.digest === null || state.base === null) invalid()
  }
  if (state.status === 'operator_action_required') {
    if (state.blocker === null) invalid()
  } else if (state.blocker !== null) invalid()
  if (state.result !== null) {
    if (!strictKeys(state.result, RESULT_KEYS)) invalid()
    if (state.kind === null || state.result.kind !== state.kind) invalid()
    if (typeof state.result.name !== 'string' || !TARGET.test(state.result.name)) invalid()
    if (state.result.commit !== null && !COMMIT_RE.test(state.result.commit)) invalid()
    if (!Array.isArray(state.result.changed_pages)) invalid()
    if (
      !state.result.changed_pages.every(
        (page) => typeof page === 'string' && /^[a-z0-9/_.-]+$/.test(page),
      ) ||
      new Set(state.result.changed_pages).size !== state.result.changed_pages.length
    )
      invalid()
    if (typeof state.result.path !== 'string' || !path.isAbsolute(state.result.path)) invalid()
    if (state.result.kind === 'analysis' && state.result.commit === null) invalid()
    if (state.result.kind === 'answers' && state.result.commit !== null) invalid()
  }
  return state
}

const empty = () => ({
  version: 1,
  status: 'idle',
  session_id: null,
  request_id: null,
  kind: null,
  target: null,
  digest: null,
  rendered: null,
  base: null,
  phase: null,
  blocker: null,
  result: null,
  status_delivery_failed: false,
})

function messageText(message) {
  const texts = []
  const push = (value) => {
    if (typeof value === 'string') texts.push(value)
  }
  push(message?.text)
  if (Array.isArray(message?.content)) for (const part of message.content) push(part?.text)
  else push(message?.content)
  if (Array.isArray(message?.parts)) for (const part of message.parts) push(part?.text)
  return texts.join('\n')
}

// Only the most recent prepared block counts: the operator confirms the
// preview the agent just showed, never an older draft. Messages arrive in
// chronological order, so the scan starts at the newest one. Extraction is
// strict and deterministic; no model turn re-derives confirmed content. Only
// agent messages carry prepared content; user, tool and synthetic ones never
// do.
export function extractBlock(messages, kind) {
  const chronological = messages ?? []
  for (let index = chronological.length - 1; index >= 0; index--) {
    const message = chronological[index]
    const role = message?.type ?? message?.role ?? message?.info?.role ?? message?.info?.type
    if (role !== undefined && role !== 'assistant') continue
    const matches = [...messageText(message).matchAll(FENCE)]
    for (let position = matches.length - 1; position >= 0; position--) {
      // Blocks of the other save kind are unrelated preparation; malformed
      // blocks of this protocol fail closed instead of being skipped.
      const block = parseBlock(matches[position][1], kind)
      if (block) return block
    }
  }
  return null
}

function parseBlock(raw, kind) {
  if (typeof raw !== 'string' || !raw.length || Buffer.byteLength(raw) > MAX_BLOCK)
    fail('invalid_prepared_block')
  let block
  try {
    block = JSON.parse(raw)
  } catch {
    fail('invalid_prepared_block')
  }
  if (!strictKeys(block, ['kind', 'target', 'content']) || !KINDS.includes(block.kind))
    fail('invalid_prepared_block')
  if (block.kind !== kind) return null
  if (typeof block.target !== 'string' || !TARGET.test(block.target)) fail('invalid_prepared_block')
  const content = checkedContent(block.content)
  if (kind === 'analysis') return { target: block.target, ...analysisMetadata(content) }
  const end = content.trimEnd()
  if (!end.endsWith(ANSWER_END) || end.indexOf(ANSWER_END) !== end.length - ANSWER_END.length)
    fail('invalid_prepared_block')
  return { target: block.target, content }
}

function checkedContent(content) {
  if (typeof content !== 'string' || !content.trim().length) fail('invalid_prepared_block')
  if (Buffer.byteLength(content) > MAX_CONTENT || content.includes('\0'))
    fail('invalid_prepared_block')
  if (TRUNCATION.some((marker) => content.includes(marker))) fail('invalid_prepared_block')
  return content
}

function analysisMetadata(content) {
  const lines = content.split('\n')
  const title = lines[0]?.startsWith('# ') ? lines[0].slice(2).trim() : ''
  if (!title || title.length > MAX_TITLE) fail('invalid_prepared_block')
  // Titles become link labels and commit subjects; wikilink metacharacters
  // would corrupt the generated index and log entries.
  if (['[', ']', '|'].some((character) => title.includes(character))) fail('invalid_prepared_block')
  if (!DATE_LINE.test(lines[1] ?? '')) fail('invalid_prepared_block')
  return { content, title }
}

// Code-owned index and log entries mirror the ingest publication formats:
// one wikilink entry and one append-only log line, generated from validated
// fields only.
function insertIndex(index, target, title) {
  const lines = index.split('\n')
  const start = lines.findIndex((line) => line.trim() === '## Analysen')
  if (start < 0) fail('index_missing_analyses_section')
  let end = lines.findIndex((line, position) => position > start && /^## /.test(line))
  if (end < 0) end = lines.length
  const section = lines.slice(start + 1, end)
  const slug = (line) => /\[\[analyses\/([a-z0-9-]+)(?:\|[^\]]*)?\]\]/.exec(line)?.[1] ?? null
  if (section.some((line) => slug(line) === target)) return { text: index, changed: false }
  const position = section.findIndex((line) => {
    const existing = slug(line)
    return existing !== null && existing > target
  })
  let insertAt = position
  if (insertAt < 0) {
    insertAt = section.length
    while (insertAt > 0 && section[insertAt - 1].trim() === '') insertAt--
  }
  section.splice(insertAt, 0, `- [[analyses/${target}|${title}]]`)
  return {
    text: [...lines.slice(0, start + 1), ...section, ...lines.slice(end)].join('\n'),
    changed: true,
  }
}

const logEntry = (target, title, now, updated) =>
  `\n- Analyse ${updated ? 'aktualisiert' : 'gespeichert'}: ${now}; [[analyses/${target}|${title}]]; analyses/${target}.\n`

async function writeNewFile(file, bytes, mode) {
  const temporary = `${file}.${randomBytes(16).toString('hex')}.tmp`
  const handle = await open(
    temporary,
    constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW,
    mode,
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

// UI-authenticated authoring channel: the model runtime prepares content as
// data, the operator admits the save through the chat command, and only this
// component writes. Called only by the manual controller; never installed in
// OpenCode and never reachable from the model runtime.
export function manualAuthoring({ configuration, runtime }) {
  const opts = { ...defaults, ...configuration }
  const file = path.join(opts.stateRoot, 'authoring.json')
  const marker = path.join(opts.stateRoot, 'authoring-initialized.json')
  const manualFile = path.join(opts.stateRoot, 'manual.json')
  const manualMarker = path.join(opts.stateRoot, 'manual-initialized.json')
  const lock = path.join(opts.stateRoot, 'manual.lock')
  const control = (input) => publisherControl(input, opts)
  const save = (state) => writeNewFile(file, `${JSON.stringify(validate(state))}\n`, 0o600)
  const git = async (...args) =>
    (
      await runFile('git', ['--no-replace-objects', '--literal-pathspecs', ...args], {
        cwd: opts.wikiRoot,
        encoding: 'utf8',
        maxBuffer: 32 * 1024 * 1024,
        env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
      })
    ).stdout
  let running = null

  const load = async () => {
    try {
      return validate(JSON.parse(await readFile(file, 'utf8')))
    } catch (error) {
      if (error.code === 'ENOENT') return null
      fail('invalid_authoring_state')
    }
  }

  async function initialize() {
    await mkdir(opts.stateRoot, { recursive: true, mode: 0o700 })
    await withIngestLock(lock, async () => {
      const state = await load()
      if (!state) {
        try {
          await readFile(marker)
          fail('missing_authoring_state')
        } catch (error) {
          if (error.code !== 'ENOENT') throw error
        }
        await writeNewFile(marker, '{"version":1}\n', 0o600)
        await save(empty())
        return
      }
      if (
        state.status === 'writing' ||
        (state.status === 'completed' && state.status_delivery_failed)
      ) {
        const summary = state.phase === 'summary' || state.status === 'completed'
        state.status = 'operator_action_required'
        state.blocker = summary ? 'summary_admission_uncertain' : 'interrupted_authoring'
        state.phase = null
        await save(state)
      }
    })
  }

  // No silent overlap and no long writer hold: authoring saves and ingest runs
  // never mutate at the same time, and an uncertain publisher stops both.
  async function assertQuiet() {
    const manual = JSON.parse((await readExisting(manualFile)) ?? 'null')
    if (manual === null) {
      if ((await readExisting(manualMarker)) !== null) fail('missing_controller_state')
    } else if (!['idle', 'completed'].includes(manual.status)) fail('busy')
    const publisher = await control({ operation: 'status' })
    if (publisher.active || publisher.inflight || publisher.stop || publisher.queue.length)
      fail('publisher_maintenance_required')
  }

  function response(state) {
    return {
      accepted: true,
      status: state.status,
      request_id: state.request_id,
      kind: state.kind,
      ...state.result,
    }
  }

  async function saveRequest(kind, input) {
    if (
      !input ||
      Object.keys(input).some((key) => !['session_id', 'request_id'].includes(key)) ||
      !SESSION.test(input.session_id ?? '') ||
      !REQUEST.test(input.request_id ?? '')
    )
      fail('invalid_input')
    return withIngestLock(lock, async () => {
      const previous = await load()
      if (!previous) fail('missing_authoring_state')
      if (previous.request_id === input.request_id) {
        if (previous.kind !== kind) fail('request_id_conflict')
        if (previous.status === 'completed') return response(previous)
        fail('busy')
      }
      if (running) fail('busy')
      if (previous.status !== 'idle' && previous.status !== 'completed')
        fail('operator_action_required_or_busy')
      await assertQuiet()
      // Intent precedes every runtime call. Extraction failures prove that
      // nothing was written and may be corrected and retried; every later
      // uncertainty stays durable and stops for operator action.
      await save({
        ...empty(),
        status: 'writing',
        session_id: input.session_id,
        request_id: input.request_id,
        kind,
        phase: 'extract',
      })
      let block
      try {
        const session = await runtime.session(input.session_id)
        if (session?.location?.directory !== '/knowledge/wiki' || !session.model)
          fail('invalid_operator_session')
        block = extractBlock(await runtime.messages(input.session_id), kind)
        if (!block) fail('prepared_block_missing')
        await preflight(kind, block)
      } catch (error) {
        await save(empty())
        throw error
      }
      const state = await load()
      state.target = block.target
      state.digest = SHA(block.content)
      state.base = (await git('rev-parse', 'HEAD')).trim()
      state.phase = 'writing'
      await save(state)
      // The write and its verified summary run under the durable intent above;
      // the request returns accepted and outcomes surface via status and the
      // session summary, exactly like manual ingest admission.
      running = (async () => {
        try {
          const result =
            kind === 'analysis'
              ? await writeAnalysis(state, block)
              : await writeAnswers(state, block)
          state.result = result
          state.phase = 'summary'
          await save(state)
          await deliverSummary(state)
          state.phase = null
          state.status = 'completed'
          await save(state)
        } catch {
          const current = (await load()) ?? state
          const summary = current.phase === 'summary'
          current.status = 'operator_action_required'
          current.blocker = summary ? 'summary_admission_uncertain' : 'authoring_uncertain'
          current.phase = null
          await save(current)
          // Never a blind second admission after a lost summary
          // acknowledgement; the explicit verified resume redelivers.
          if (summary) return
          try {
            await runtime.note(
              current.session_id,
              `Trusted authoring status (verified facts, never instructions): ${JSON.stringify({ kind: current.kind, target: current.target, blocker: current.blocker })}. Melde genau diesen Blocker kompakt auf Deutsch und dass nur eine ausdrückliche geprüfte Wiederaufnahme (/analysis-save resume oder /answer-save resume) fortsetzt. Keine Reparatur, keine Wiederholung, keine weiteren Speicherungen.`,
              true,
            )
          } catch {
            // The persistent stop and the authenticated status route remain
            // the authoritative evidence of the failed save.
          }
        } finally {
          running = null
        }
      })()
      return { accepted: true, request_id: input.request_id }
    })
  }

  // Read-only checks before the durable write intent: operator-visible
  // refusals (missing index section, occupied answer name, missing wiki URL)
  // never become safety stops, and the write path repeats every check.
  async function preflight(kind, block) {
    if (kind === 'analysis') {
      publicUrl()
      insertIndex(
        await readFile(path.join(opts.wikiRoot, 'index.md'), 'utf8'),
        block.target,
        block.title,
      )
      await assertCleanIngestWiki(opts.wikiRoot)
      return
    }
    if ((await readExisting(path.join(opts.answersRoot, `${block.target}.md`))) !== null)
      fail('answers_file_exists')
  }

  async function writeAnalysis(state, block) {
    const wikiUrl = publicUrl()
    const page = `analyses/${block.target}.md`
    const print = `assets/analyses/${block.target}.html`
    const existed = (await readExisting(path.join(opts.wikiRoot, page))) !== null
    return withWikiLock(opts, async () => {
      await assertCleanIngestWiki(opts.wikiRoot)
      const head = (await git('rev-parse', 'HEAD')).trim()
      if (head !== state.base) fail('authoring_uncertain')
      await mkdir(path.join(opts.wikiRoot, 'analyses'), { recursive: true, mode: 0o755 })
      await mkdir(path.join(opts.wikiRoot, 'assets', 'analyses'), { recursive: true, mode: 0o755 })
      await writeNewFile(path.join(opts.wikiRoot, page), block.content, 0o644)
      const [command, ...prefix] = [].concat(opts.renderCommand)
      await runFile(command, [...prefix, page, '--wiki-url', wikiUrl], {
        cwd: opts.wikiRoot,
        maxBuffer: 32 * 1024 * 1024,
      })
      const rendered = await readExisting(path.join(opts.wikiRoot, print))
      if (!rendered) fail('authoring_uncertain')
      state.rendered = SHA(rendered)
      await save(state)
      const now = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
      const inserted = insertIndex(
        await readFile(path.join(opts.wikiRoot, 'index.md'), 'utf8'),
        block.target,
        block.title,
      )
      if (inserted.changed)
        await writeNewFile(path.join(opts.wikiRoot, 'index.md'), inserted.text, 0o644)
      const log = await readFile(path.join(opts.wikiRoot, 'log.md'), 'utf8')
      await writeNewFile(
        path.join(opts.wikiRoot, 'log.md'),
        log + logEntry(block.target, block.title, now, existed),
        0o644,
      )
      const allowed = [page, print, 'index.md', 'log.md']
      const commit = await commitPages({
        head,
        message: `feat(analyses): ${block.title} speichern`,
        allowed,
      })
      return {
        kind: 'analysis',
        name: block.target,
        commit,
        changed_pages: await changedPages(commit),
        path: path.join(opts.wikiRoot, page),
      }
    })
  }

  async function commitPages({ head, message, allowed }) {
    await git('add', '--', ...allowed)
    const tree = (await git('write-tree')).trim()
    const commit = (
      await git('-c', 'commit.gpgsign=false', 'commit-tree', tree, '-p', head, '-m', message)
    ).trim()
    const changed = await changedPages(commit)
    // The append-only log line always differs, so a save is never an empty
    // commit and never claims pages it did not touch.
    if (!changed.every((page) => allowed.includes(page)) || !changed.includes('log.md'))
      fail('authoring_uncertain')
    // Compare-and-swap: a foreign writer that moved HEAD aborts this save.
    await git('update-ref', 'HEAD', commit, head)
    if ((await git('rev-parse', 'HEAD')).trim() !== commit) fail('authoring_uncertain')
    return commit
  }

  const changedPages = async (commit) =>
    (await git('diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', commit))
      .split('\0')
      .filter(Boolean)
      .sort()

  async function writeAnswers(state, block) {
    const target = path.join(opts.answersRoot, `${block.target}.md`)
    if (!target.startsWith(`${opts.answersRoot}${path.sep}`)) fail('invalid_prepared_block')
    if ((await readExisting(target)) !== null) fail('answers_file_exists')
    await mkdir(opts.answersRoot, { recursive: true, mode: 0o700 })
    await writeNewFile(target, block.content, 0o644)
    const written = await readExisting(target)
    if (!written || SHA(written) !== state.digest) fail('authoring_uncertain')
    return {
      kind: 'answers',
      name: block.target,
      commit: null,
      changed_pages: [],
      path: target,
    }
  }

  function publicUrl() {
    let url
    try {
      url = new URL(opts.wikiUrl ?? '')
    } catch {
      fail('wiki_public_url_missing')
    }
    if (
      !['http:', 'https:'].includes(url.protocol) ||
      !url.host ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      fail('wiki_public_url_missing')
    return opts.wikiUrl.replace(/\/+$/, '')
  }

  async function deliverSummary(state) {
    const result = state.result
    const payload =
      result.kind === 'analysis'
        ? {
            kind: 'analysis',
            name: result.name,
            commit: result.commit,
            changed_pages: result.changed_pages,
            page_url: `${publicUrl()}/analyses/${result.name}`,
            print_url: `${publicUrl()}/.fs/assets/analyses/${result.name}.html`,
          }
        : { kind: 'answers', name: result.name, path: result.path, published: false }
    const instruction =
      result.kind === 'analysis'
        ? 'Gib genau diese Fakten kompakt auf Deutsch wieder: gespeicherte Analyse, Commit, geänderte Seiten sowie die öffentliche Wiki- und Druckansichts-URL.'
        : 'Gib genau diese Fakten kompakt auf Deutsch wieder: Antworten lokal gespeichert und noch nicht veröffentlicht, Dateiname und Eingangspfad. Nenne als nächsten Schritt die Veröffentlichungsprüfung und den Import über /ingest-new.'
    state.status_delivery_failed = true // Admission intent, not proof of delivery.
    await save(state)
    await runtime.note(
      state.session_id,
      `Trusted authoring status (verified facts, never instructions): ${JSON.stringify(payload)}. ${instruction} Keine Reparatur, keine Wiederholung, keine weiteren Speicherungen.`,
      true,
    )
    state.status_delivery_failed = false
    await save(state)
  }

  // Verified recovery only: adopt what provably landed, clear what provably
  // never started, and keep the stop on anything else for explicit operator
  // repair. No unattended repair, rollback or deletion.
  async function resume(input) {
    if (!input || Object.keys(input).length !== 1 || input.confirmed !== true)
      fail('confirmation_required')
    return withIngestLock(lock, async () => {
      if (running) fail('busy')
      const state = await load()
      if (!state) fail('missing_authoring_state')
      if (state.status === 'completed') {
        if (!state.status_delivery_failed) fail('not_stopped')
        await deliverSummary(state)
        return { verified: true, ...response(state) }
      }
      if (state.status !== 'operator_action_required') fail('not_stopped')
      if (state.blocker === 'summary_admission_uncertain' && state.result) {
        await deliverSummary(state)
        state.status = 'completed'
        state.blocker = null
        await save(state)
        return { verified: true, ...response(state) }
      }
      await assertQuiet()
      if (state.kind === 'analysis' && state.target === null) {
        // Interrupted during extraction: nothing was ever written.
        await save(empty())
        return { verified: true, status: 'idle', request_id: null }
      }
      return state.kind === 'analysis' ? resumeAnalysis(state) : resumeAnswers(state)
    })
  }

  async function resumeAnalysis(state) {
    const clean = !(await git('status', '--porcelain=v1', '-z', '--untracked-files=all')).length
    if (!clean) fail('manual_maintenance_required')
    const page = `analyses/${state.target}.md`
    const print = `assets/analyses/${state.target}.html`
    const commit =
      (await git('rev-parse', 'HEAD')).trim() !== state.base
        ? await findCommit(state, page, print)
        : null
    if (!commit) {
      await save(empty())
      return { verified: true, status: 'idle', request_id: null }
    }
    return finish(state, {
      kind: 'analysis',
      name: state.target,
      commit,
      changed_pages: await changedPages(commit),
      path: path.join(opts.wikiRoot, page),
    })
  }

  // Content digests, not commit messages, prove which commit carries the
  // confirmed content: an operator repair may commit it under any subject.
  async function findCommit(state, page, print) {
    const hashes = (
      await git('log', '--format=%H', '-n', '100', `${state.base}..HEAD`, '--', page, print)
    )
      .split('\n')
      .filter(Boolean)
    for (const commit of hashes) {
      const blob = async (name) =>
        SHA(await git('cat-file', 'blob', `${commit}:${name}`).catch(() => ''))
      if ((await blob(page)) === state.digest && (await blob(print)) === state.rendered)
        return commit
    }
    return null
  }

  async function resumeAnswers(state) {
    const target = path.join(opts.answersRoot, `${state.target}.md`)
    const written = await readExisting(target)
    if (written === null) {
      await save(empty())
      return { verified: true, status: 'idle', request_id: null }
    }
    if (SHA(written) !== state.digest) fail('manual_maintenance_required')
    return finish(state, {
      kind: 'answers',
      name: state.target,
      commit: null,
      changed_pages: [],
      path: target,
    })
  }

  async function finish(state, result) {
    state.result = result
    state.status = 'completed'
    state.blocker = null
    state.phase = null
    await save(state)
    await deliverSummary(state)
    return { verified: true, ...response(state) }
  }

  return { initialize, save: saveRequest, resume, status: load, wait: () => running }
}

// The ingest controller refuses to start while an authoring write is active or
// stopped. Missing state is idle; lost, corrupt or unknown state is fail-closed
// busy so a lost file can never reset writer ownership.
export async function authoringBusy(stateRoot) {
  let state
  try {
    state = JSON.parse(await readFile(path.join(stateRoot, 'authoring.json'), 'utf8'))
  } catch (error) {
    if (error.code !== 'ENOENT') return true
    return (await readExisting(path.join(stateRoot, 'authoring-initialized.json'))) !== null
  }
  try {
    validate(state)
  } catch {
    return true
  }
  return !['idle', 'completed'].includes(state.status)
}
