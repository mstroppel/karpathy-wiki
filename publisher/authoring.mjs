import { createHash, randomBytes } from 'node:crypto'
import { execFile } from 'node:child_process'
import { constants } from 'node:fs'
import { lstat, mkdir, open, readFile, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'
import { publisherControl } from './control.mjs'
import { validate as validateManualState } from './manual_ingest.mjs'
import {
  assertCleanIngestWiki,
  confinedIngestPath,
} from '../config/tools/wiki_ingest_transaction_core.mjs'
import { withWikiLock } from '../config/tools/wiki_ingest_publication_core.mjs'
import { withIngestLock } from '../config/tools/wiki_ingest_storage.mjs'
import { controllerFence } from './controller_fence.mjs'

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
const FENCE = /```karpathy-authoring([^\n]*)(?:\n([\s\S]*?)(\n```|$)|$)/g
const KINDS = ['analysis', 'answers']
const STATUSES = ['idle', 'writing', 'completed', 'operator_action_required']
const PHASES = ['extract', 'writing', 'publishing', 'summary']
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

// Every read and write target is confined below its configured root: symlinked
// roots, directories or files (including a tracked symlink planted in the wiki
// or a symlinked answer draft) can never redirect a save into another tree.
// Start at the filesystem root so missing configured ancestors can be checked
// without creating them; even a symlinked inbox chain is refused first.
const confinedFile = (root, relative) => {
  const absolute = path.resolve(root)
  const filesystemRoot = path.parse(absolute).root
  return confinedIngestPath(
    filesystemRoot,
    path.posix.join(path.relative(filesystemRoot, absolute), relative),
    true,
  ).catch(() => {
    fail('unsafe_write_path')
  })
}

// Create missing directories one component at a time, checking existing and
// newly created components without following symlinks. Never recursive mkdir
// through an unchecked configured parent.
async function ensureDirectory(directory, mode) {
  const absolute = path.resolve(directory)
  let current = path.parse(absolute).root
  for (const part of path.relative(current, absolute).split(path.sep)) {
    current = path.join(current, part)
    try {
      await mkdir(current, { mode })
    } catch (error) {
      if (error.code !== 'EEXIST') fail('unsafe_write_path')
    }
    const info = await lstat(current)
    if (info.isSymbolicLink() || !info.isDirectory()) fail('unsafe_write_path')
  }
}

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
    // A validated candidate commit is persisted before ref publication; the
    // result is authoritative only after ancestry/digests verify it landed.
    const hasResult = state.phase === 'publishing' || state.phase === 'summary'
    if (hasResult ? state.result === null : state.result !== null) invalid()
    if (state.phase === 'publishing' && state.kind !== 'analysis') invalid()
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
// preview the agent just showed, never an older draft. The runtime lists
// messages newest first (its cursor order is `desc`), so the scan starts at
// index 0 and takes the last block of a message (later text is newer).
// Extraction is strict and deterministic; no model turn re-derives confirmed
// content. Only agent messages carry prepared content; user, tool and
// synthetic ones never do.
export function extractBlock(messages, kind) {
  for (const message of messages ?? []) {
    const role = message?.type ?? message?.role ?? message?.info?.role ?? message?.info?.type
    if (role !== undefined && role !== 'assistant') continue
    const matches = [...messageText(message).matchAll(FENCE)]
    for (let position = matches.length - 1; position >= 0; position--) {
      // Blocks of the other save kind are unrelated preparation; malformed
      // blocks of this protocol fail closed instead of being skipped.
      const [, header, raw, closing] = matches[position]
      if (header !== '' || closing !== '\n```' || raw.includes('\n')) fail('invalid_prepared_block')
      const block = parseBlock(raw, kind)
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
  const link = `[[analyses/${target}|${title}]]`
  const existingAt = section.findIndex((line) => slug(line) === target)
  if (existingAt >= 0) {
    const previous = section[existingAt]
    section[existingAt] = previous.replace(/\[\[analyses\/[a-z0-9-]+(?:\|[^\]]*)?\]\]/, () => link)
    return {
      text: [...lines.slice(0, start + 1), ...section, ...lines.slice(end)].join('\n'),
      changed: previous !== section[existingAt],
    }
  }
  const position = section.findIndex((line) => {
    const existing = slug(line)
    return existing !== null && existing > target
  })
  let insertAt = position
  if (insertAt < 0) {
    insertAt = section.length
    while (insertAt > 0 && section[insertAt - 1].trim() === '') insertAt--
  }
  section.splice(insertAt, 0, `- ${link}`)
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
  const fence = controllerFence(opts)
  const file = path.join(opts.stateRoot, 'authoring.json')
  const marker = path.join(opts.stateRoot, 'authoring-initialized.json')
  const manualFile = path.join(opts.stateRoot, 'manual.json')
  const manualMarker = path.join(opts.stateRoot, 'manual-initialized.json')
  const lock = path.join(opts.stateRoot, 'manual.lock')
  const control = (input) => publisherControl(input, opts)
  const save = (state) => writeNewFile(file, `${JSON.stringify(validate(state))}\n`, 0o600)
  const gitBytes = async (...args) =>
    (
      await runFile('git', ['--no-replace-objects', '--literal-pathspecs', ...args], {
        cwd: opts.wikiRoot,
        encoding: 'buffer',
        maxBuffer: 32 * 1024 * 1024,
        env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
      })
    ).stdout
  const git = async (...args) => (await gitBytes(...args)).toString('utf8')
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
    await withIngestLock(lock, () =>
      fence.initialize(async () => {
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
      }),
    )
  }

  // No silent overlap and no long writer hold: authoring saves and ingest runs
  // never mutate at the same time, and an uncertain publisher stops both.
  async function assertQuiet() {
    const bytes = await readExisting(manualFile)
    if (bytes === null) {
      if ((await readExisting(manualMarker)) !== null) fail('missing_controller_state')
    } else {
      const manual = JSON.parse(bytes)
      validateManualState(manual)
      if (!['idle', 'completed'].includes(manual.status) || manual.status_delivery_failed)
        fail('busy')
    }
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
      await fence.assert()
      const previous = await load()
      if (!previous) fail('missing_authoring_state')
      if (previous.request_id === input.request_id) {
        if (previous.kind !== kind) fail('request_id_conflict')
        if (previous.status === 'completed' && !previous.status_delivery_failed)
          return response(previous)
        fail('busy')
      }
      if (running) fail('busy')
      if (
        previous.status_delivery_failed ||
        (previous.status !== 'idle' && previous.status !== 'completed')
      )
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
      running = fence
        .run(async () => {
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
        })
        .catch(async () => {
          await withIngestLock(lock, async () => {
            await fence.assert()
            const current = await load()
            if (current?.request_id !== input.request_id) return
            current.status = 'operator_action_required'
            current.blocker = 'authoring_uncertain'
            current.phase = null
            await save(current)
          }).catch(() => {})
          running = null
        })
      return { accepted: true, request_id: input.request_id }
    })
  }

  // Read-only checks before the durable write intent: operator-visible
  // refusals (missing index section, occupied answer name, missing wiki URL)
  // never become safety stops, and the write path repeats every check.
  async function preflight(kind, block) {
    if (kind === 'analysis') {
      publicUrl()
      const indexFile = await confinedFile(opts.wikiRoot, 'index.md')
      const pageFile = await confinedFile(opts.wikiRoot, `analyses/${block.target}.md`)
      const printFile = await confinedFile(opts.wikiRoot, `assets/analyses/${block.target}.html`)
      if (!indexFile || !pageFile || !printFile) fail('unsafe_write_path')
      insertIndex((await readFile(indexFile)).toString('utf8'), block.target, block.title)
      await assertCleanIngestWiki(opts.wikiRoot)
      return
    }
    const target = await confinedFile(opts.answersRoot, `${block.target}.md`)
    if (target && (await readExisting(target)) !== null) fail('answers_file_exists')
  }

  async function writeAnalysis(state, block) {
    const wikiUrl = publicUrl()
    const page = `analyses/${block.target}.md`
    const print = `assets/analyses/${block.target}.html`
    return withWikiLock(opts, async () => {
      await assertCleanIngestWiki(opts.wikiRoot)
      const head = (await git('rev-parse', 'HEAD')).trim()
      if (head !== state.base) fail('authoring_uncertain')
      await ensureDirectory(path.join(opts.wikiRoot, 'analyses'), 0o755)
      await ensureDirectory(path.join(opts.wikiRoot, 'assets', 'analyses'), 0o755)
      const pageFile = await confinedFile(opts.wikiRoot, page)
      const printFile = await confinedFile(opts.wikiRoot, print)
      const indexFile = await confinedFile(opts.wikiRoot, 'index.md')
      const logFile = await confinedFile(opts.wikiRoot, 'log.md')
      if (!pageFile || !printFile || !indexFile || !logFile) fail('unsafe_write_path')
      const existed = (await readExisting(pageFile)) !== null
      await writeNewFile(pageFile, block.content, 0o644)
      const [command, ...prefix] = [].concat(opts.renderCommand)
      await runFile(command, [...prefix, page, '--wiki-url', wikiUrl], {
        cwd: opts.wikiRoot,
        maxBuffer: 32 * 1024 * 1024,
      })
      const rendered = await readExisting(printFile)
      if (!rendered) fail('authoring_uncertain')
      state.rendered = SHA(rendered)
      await save(state)
      const now = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
      const inserted = insertIndex(
        (await readFile(indexFile)).toString('utf8'),
        block.target,
        block.title,
      )
      if (inserted.changed) await writeNewFile(indexFile, inserted.text, 0o644)
      const log = (await readFile(logFile)).toString('utf8')
      await writeNewFile(logFile, log + logEntry(block.target, block.title, now, existed), 0o644)
      const allowed = [page, print, 'index.md', 'log.md']
      return commitPages({
        state,
        head,
        message: `feat(analyses): ${block.title} speichern`,
        allowed,
      })
    })
  }

  async function commitPages({ state, head, message, allowed }) {
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
    // Record the validated candidate before the ref can move. Identical
    // re-saves may change only log.md, leaving no page/print history event;
    // candidate identity makes that publication recoverable without guessing.
    state.result = {
      kind: 'analysis',
      name: state.target,
      commit,
      changed_pages: changed,
      path: path.join(opts.wikiRoot, `analyses/${state.target}.md`),
    }
    state.phase = 'publishing'
    await save(state)
    // Compare-and-swap: a foreign writer that moved HEAD aborts this save.
    await git('update-ref', 'HEAD', commit, head)
    if ((await git('rev-parse', 'HEAD')).trim() !== commit) fail('authoring_uncertain')
    return state.result
  }

  const changedPages = async (commit) =>
    (await git('diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', commit))
      .split('\0')
      .filter(Boolean)
      .sort()

  async function writeAnswers(state, block) {
    await ensureDirectory(opts.answersRoot, 0o700)
    const target = await confinedFile(opts.answersRoot, `${block.target}.md`)
    if (!target) fail('unsafe_write_path')
    if ((await readExisting(target)) !== null) fail('answers_file_exists')
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
      await fence.assert()
      if (running) fail('busy')
      return fence.run(async () => {
        const state = await load()
        if (!state) fail('missing_authoring_state')
        if (state.status === 'completed') {
          if (!state.status_delivery_failed) fail('not_stopped')
          state.status = 'operator_action_required'
          state.blocker = 'summary_admission_uncertain'
          await save(state)
        }
        if (state.status !== 'operator_action_required') fail('not_stopped')
        await assertQuiet()
        if (state.kind === 'analysis' && state.target === null) {
          // Interrupted during extraction: nothing was ever written.
          await save(empty())
          return { verified: true, status: 'idle', request_id: null }
        }
        return state.kind === 'analysis' ? resumeAnalysis(state) : resumeAnswers(state)
      })
    })
  }

  async function resumeAnalysis(state) {
    const clean = !(await git('status', '--porcelain=v1', '-z', '--untracked-files=all')).length
    if (!clean) fail('manual_maintenance_required')
    const page = `analyses/${state.target}.md`
    const print = `assets/analyses/${state.target}.html`
    const candidate = state.result?.commit
    const landed =
      candidate &&
      (await git('merge-base', '--is-ancestor', candidate, 'HEAD').then(
        () => true,
        () => false,
      ))
    const commit =
      (landed ? candidate : null) ??
      ((await git('rev-parse', 'HEAD')).trim() !== state.base
        ? await findCommit(state, page, print)
        : null)
    const pageFile = await confinedFile(opts.wikiRoot, page)
    const printFile = await confinedFile(opts.wikiRoot, print)
    const savedPage = await readExisting(pageFile)
    const savedPrint = await readExisting(printFile)
    if (!commit) {
      // A scan miss is not evidence that a write never landed: only unchanged
      // target paths at the recorded baseline can safely release ownership.
      await git('diff', '--quiet', state.base, 'HEAD', '--', page, print).catch(() =>
        fail('manual_maintenance_required'),
      )
      const matchesBaseline = async (name, bytes) => {
        const entry = await git('ls-tree', '-z', state.base, '--', name)
        if (!entry) return bytes === null
        if (!/^100(?:644|755) blob [0-9a-f]{40}\t/.test(entry)) return false
        return (
          bytes !== null &&
          SHA(bytes) === SHA(await gitBytes('cat-file', 'blob', `${state.base}:${name}`))
        )
      }
      if (!(await matchesBaseline(page, savedPage)) || !(await matchesBaseline(print, savedPrint)))
        fail('manual_maintenance_required')
      await save(empty())
      return { verified: true, status: 'idle', request_id: null }
    }
    if (
      savedPage === null ||
      savedPrint === null ||
      SHA(savedPage) !== state.digest ||
      SHA(savedPrint) !== state.rendered ||
      !(await matchesCommit(state, commit, page, print))
    )
      fail('manual_maintenance_required')
    return finish(state, {
      kind: 'analysis',
      name: state.target,
      commit,
      changed_pages: await changedPages(commit),
      path: path.join(opts.wikiRoot, page),
    })
  }

  // Content digests, not commit messages, prove which commit carries the
  // confirmed content: an operator repair may commit it under any subject. The
  // path-filtered range is scanned completely; a partial window could miss a
  // landed commit and wrongly clear the stop.
  async function findCommit(state, page, print) {
    const hashes = (await git('log', '--format=%H', `${state.base}..HEAD`, '--', page, print))
      .split('\n')
      .filter(Boolean)
    for (const commit of hashes) {
      if (await matchesCommit(state, commit, page, print)) return commit
    }
    return null
  }

  async function matchesCommit(state, commit, page, print) {
    const blob = async (name) => {
      const bytes = await gitBytes('cat-file', 'blob', `${commit}:${name}`).catch(() => null)
      return bytes === null ? null : SHA(bytes)
    }
    return (await blob(page)) === state.digest && (await blob(print)) === state.rendered
  }

  async function resumeAnswers(state) {
    const target = await confinedFile(opts.answersRoot, `${state.target}.md`)
    const written = target ? await readExisting(target) : null
    if (written === null) {
      if (state.result !== null) fail('manual_maintenance_required')
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
    state.status = 'operator_action_required'
    state.blocker = 'summary_admission_uncertain'
    state.phase = null
    await save(state)
    await deliverSummary(state)
    state.status = 'completed'
    state.blocker = null
    await save(state)
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
  return !['idle', 'completed'].includes(state.status) || state.status_delivery_failed
}
