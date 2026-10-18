import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import fs from 'node:fs/promises'
import { syncBuiltinESMExports } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { promisify } from 'node:util'
import { ingestPublication } from '../config/tools/wiki_ingest_publication_core.mjs'
import {
  loadPreparation,
  savePreparation,
  verifyIngestCommit,
} from '../config/tools/wiki_ingest_transaction_core.mjs'
import { loadRun } from '../config/tools/wiki_ingest_journal_core.mjs'
import { maintainJournal, verifyAcceptedPublication } from '../publisher/journal_storage.mjs'

const runFile = promisify(execFile)
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex')
const manualIdle = {
  version: 1,
  status: 'idle',
  results: [],
  selected: [],
  completed_sources: 0,
  active: null,
  blocker: null,
}
const authoringIdle = {
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
}

async function snapshot(root) {
  const result = {}
  async function walk(relative) {
    const info = await fs.lstat(path.join(root, relative))
    if (info.isDirectory()) {
      result[`${relative}/`] = [info.mode, 'directory']
      for (const name of (await fs.readdir(path.join(root, relative))).sort())
        await walk(path.join(relative, name))
    } else
      result[relative] = [info.mode, info.size, sha(await fs.readFile(path.join(root, relative)))]
  }
  await walk('')
  return result
}

async function fixture(t) {
  const base = await fs.mkdtemp(path.join(os.tmpdir(), 'journal-storage-'))
  t.after(() => fs.rm(base, { recursive: true, force: true }))
  const opts = {
    root: path.join(base, 'journal'),
    wikiRoot: path.join(base, 'wiki'),
    sourceRoot: path.join(base, 'sources'),
    stateRoot: path.join(base, 'publisher'),
  }
  for (const directory of Object.values(opts)) await fs.mkdir(directory)
  await fs.mkdir(path.join(opts.sourceRoot, 'webdav'))
  const git = async (...args) => (await runFile('git', args, { cwd: opts.wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic Fixture')
  await git('config', 'user.email', 'fixture@example.invalid')
  for (const page of ['overview.md', 'index.md', 'log.md'])
    await fs.writeFile(path.join(opts.wikiRoot, page), `# ${page}\n`)
  await git('add', '.')
  await git('commit', '-qm', 'Synthetic baseline')
  for (const name of ['manual.lock', 'controller-execution.lock', 'writer.lock'])
    await fs.writeFile(path.join(opts.stateRoot, name), '')
  await fs.writeFile(
    path.join(opts.stateRoot, 'control.json'),
    JSON.stringify({
      version: 1,
      active: null,
      inflight: null,
      stop: null,
      finished: [],
      responses: {},
    }),
  )
  await fs.writeFile(path.join(opts.stateRoot, 'manual.json'), JSON.stringify(manualIdle))
  await fs.writeFile(path.join(opts.stateRoot, 'authoring.json'), JSON.stringify(authoringIdle))
  await fs.writeFile(
    path.join(opts.stateRoot, 'controller-initialized.json'),
    JSON.stringify({ version: 1 }),
  )
  await fs.writeFile(
    path.join(opts.stateRoot, 'controller.json'),
    JSON.stringify({ version: 1, boot_id: '1'.repeat(32) }),
  )
  let sequence = 0
  const prepare = async () => {
    const source = `Synthetic source revision ${sequence++}.\n`
    await fs.writeFile(path.join(opts.sourceRoot, 'webdav/notes.md'), source)
    await fs.writeFile(
      path.join(opts.sourceRoot, 'webdav/manifest.json'),
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
            source_revision: sha(source),
            frontmatter: {
              source: 'webdav',
              source_path: 'notes.md',
              source_revision: sha(source),
            },
            claim: { source: 'webdav', source_path: 'notes.md' },
          },
        ],
        revoked: [],
        errors: [],
      }),
    )
    const result = await ingestPublication({
      ...opts,
      operation: 'prepare',
      adapter: 'webdav',
      sourceKey: 'notes.md',
      sourceRevision: sha(source),
    })
    const preparationId = result.preparation_id
    const call = (operation, args = {}) =>
      ingestPublication({ ...opts, preparationId, operation, ...args })
    return {
      preparationId,
      call,
      draft: path.join(opts.root, 'preparations', preparationId),
      receipt: () => loadPreparation({ ...opts, preparationId }),
    }
  }
  const publish = async () => {
    const p = await prepare()
    await p.call('read_source')
    await p.call('stage', { draft: '# Synthetic finding\nSupported evidence.\n' })
    await p.call('review', { page: 'overview.md' })
    p.result = await p.call('publish', {
      title: 'Synthetic finding',
      content: 'Supported evidence, line 1.',
      contradictions: 'None.',
      extractionLimits: 'Read completely.',
    })
    return p
  }
  return { base, opts, git, prepare, publish }
}

test('dry run is byte-for-byte non-mutating; compact preserves verification, journal and reports', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  const before = await snapshot(f.base)
  const dry = await maintainJournal(f.opts)
  assert.equal(dry.candidates, 1, JSON.stringify(dry))
  assert.ok(dry.candidate_bytes > 0)
  assert.ok(dry.storage.proposal_git > 0)
  assert.ok(dry.storage.draft_evidence > 0)
  assert.ok(dry.storage.live_backups > 0)
  assert.deepEqual(await snapshot(f.base), before)
  const accepted = await loadRun({ ...f.opts, runId: p.result.run_id })
  const cleaned = await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal(cleaned.reclaimed_bytes, dry.candidate_bytes)
  await assert.rejects(fs.stat(p.draft), { code: 'ENOENT' })
  assert.deepEqual(await loadRun({ ...f.opts, runId: p.result.run_id }), accepted)
  await verifyIngestCommit({ ...f.opts, record: accepted.records[0] })
  await verifyAcceptedPublication({ ...f.opts, receipt: await p.receipt() })
  assert.equal((await p.call('resume')).commit, p.result.commit)
  assert.equal((await maintainJournal({ ...f.opts, confirmed: true })).reclaimed_bytes, 0)
  assert.equal((await p.receipt()).compaction.state, 'done')
})

test('shallow proposals never duplicate full history or share mutable files; live history stays complete', async (t) => {
  const f = await fixture(t)
  for (let i = 0; i < 4; i++) {
    await fs.writeFile(path.join(f.opts.wikiRoot, 'obsolete.md'), randomBytes(32000))
    await f.git('add', '.')
    await f.git('commit', '-qm', 'Synthetic obsolete revision')
  }
  await fs.unlink(path.join(f.opts.wikiRoot, 'obsolete.md'))
  await f.git('add', '.')
  await f.git('commit', '-qm', 'Drop synthetic obsolete file')
  const p = await f.publish()
  const draftGit = async (...args) =>
    (await runFile('git', args, { cwd: path.join(p.draft, 'wiki') })).stdout.trim()
  assert.equal(await draftGit('rev-list', '--count', 'HEAD'), '1')
  assert.equal(await draftGit('rev-parse', '--is-shallow-repository'), 'true')
  assert.equal(await f.git('rev-parse', '--is-shallow-repository'), 'false')
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '7')
  await assert.rejects(fs.stat(path.join(p.draft, 'wiki/.git/objects/info/alternates')), {
    code: 'ENOENT',
  })
  const live = await fs.stat(path.join(f.opts.wikiRoot, 'overview.md'))
  const draft = await fs.stat(path.join(p.draft, 'wiki/overview.md'))
  assert.notEqual(live.ino, draft.ino)
  assert.equal(draft.nlink, 1)
})

test('hardlinked live recovery backups do not block inspection or unrelated cleanup', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  const backup = path.join(f.opts.wikiRoot, '.git', (await p.receipt()).backups[0].path)
  const recovery = path.join(f.base, 'operator-recovery')
  await fs.link(backup, recovery)
  const before = await snapshot(f.base)
  const dry = await maintainJournal(f.opts)
  assert.equal(dry.candidates, 1, JSON.stringify(dry))
  assert.ok(dry.storage.live_backups > 0)
  assert.deepEqual(await snapshot(f.base), before)
  const result = await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal(result.entries[0].reason, 'compacted')
  assert.equal((await fs.stat(backup)).nlink, 2)
  assert.deepEqual(await fs.readFile(backup), await fs.readFile(recovery))
})

test('uncountable live backup entries mark partial counts without following links or blocking cleanup', async (t) => {
  const f = await fixture(t)
  await f.publish()
  const foreign = path.join(f.opts.wikiRoot, '.git/ingest-backup-abcdef')
  await fs.symlink(f.opts.sourceRoot, foreign)
  const before = await fs.lstat(foreign)
  const dry = await maintainJournal(f.opts)
  assert.equal(dry.storage.live_backups_unreadable, true)
  assert.equal(dry.candidates, 1)
  const result = await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal(result.entries[0].reason, 'compacted')
  assert.equal((await fs.lstat(foreign)).ino, before.ino)
  assert.equal(await fs.readlink(foreign), f.opts.sourceRoot)
})

test('maintenance CLI preserves safe cleanup refusal codes but hides arbitrary errors', async () => {
  for (const reason of [
    'lock_file_missing',
    'Ein anderer Schreiber hält den Ingest-Lock',
    'cleanup_manifest_mismatch',
    'draft_changed_during_cleanup',
    'receipt_changed_during_cleanup',
    'commit_parent_mismatch',
    'commit_paths_mismatch',
    'commit_blob_mismatch',
    'source_identity_mismatch',
    'live_backup_evidence_changed',
    'journal_evidence_mismatch',
    'report_evidence_mismatch',
    'run_not_successfully_closed',
    'private path /synthetic/secret',
  ]) {
    // Inject at the filesystem boundary, not into the CLI's error formatter.
    const preload = `import fs from 'node:fs/promises'; import { syncBuiltinESMExports } from 'node:module'; fs.realpath = async () => { throw new Error(${JSON.stringify(reason)}) }; syncBuiltinESMExports();`
    await assert.rejects(
      runFile(process.execPath, [
        '--import',
        `data:text/javascript,${encodeURIComponent(preload)}`,
        'publisher/journal_maintenance.mjs',
        '--confirm',
      ]),
      (error) => {
        assert.equal(error.code, 1)
        assert.equal(error.stderr, '')
        const output = JSON.parse(error.stdout)
        assert.equal(output.error, 'journal_maintenance_refused')
        assert.equal(
          output.reason,
          reason.startsWith('private ')
            ? 'writer_lock_or_evidence_unavailable'
            : reason === 'Ein anderer Schreiber hält den Ingest-Lock'
              ? 'writer_lock_unavailable'
              : reason,
        )
        assert.ok(!error.stdout.includes('/synthetic/secret'))
        return true
      },
    )
  }
})

test('direct runtime and operator-only publisher modes do not require absent UI controller state', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  for (const name of [
    'manual.json',
    'authoring.json',
    'manual.lock',
    'controller-execution.lock',
    'controller.json',
    'controller-initialized.json',
  ])
    await fs.unlink(path.join(f.opts.stateRoot, name))
  assert.equal((await maintainJournal(f.opts)).candidates, 1)
  // A missing initialized authority is not mistaken for an unused namespace.
  await fs.unlink(path.join(f.opts.stateRoot, 'control.json'))
  await assert.rejects(maintainJournal(f.opts), /publisher_not_idle/)
  await fs.unlink(path.join(f.opts.stateRoot, 'writer.lock'))
  assert.equal((await maintainJournal({ ...f.opts, confirmed: true })).candidates, 1)
  await verifyAcceptedPublication({ ...f.opts, receipt: await p.receipt() })
})

test('report writers share the run lock and block cleanup without mutating data', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  const { withIngestLock } = await import('../config/tools/wiki_ingest_storage.mjs')
  // A different process is used, because same-process calls intentionally queue.
  const { spawn } = await import('node:child_process')
  const file = path.join(f.opts.root, 'runs', p.result.run_id, 'records.lock')
  const child = spawn(
    process.execPath,
    [
      '--input-type=module',
      '-e',
      `import { withIngestLock } from ${JSON.stringify(new URL('../config/tools/wiki_ingest_storage.mjs', import.meta.url).href)}; await withIngestLock(${JSON.stringify(file)}, async () => { console.log('locked'); for await (const chunk of process.stdin) {} });`,
    ],
    { stdio: ['pipe', 'pipe', 'inherit'] },
  )
  const exited = new Promise((resolve) => child.once('close', resolve))
  await new Promise((resolve) => child.stdout.once('data', resolve))
  try {
    const before = await snapshot(f.base)
    const result = await maintainJournal({ ...f.opts, confirmed: true })
    assert.equal(result.candidates, 0)
    assert.deepEqual(await snapshot(f.base), before)
  } finally {
    child.stdin.end()
    await exited
  }
  // The lock itself is a stable inode, not a stale file to remove.
  await withIngestLock(file, async () => {})
})

test('retention count and repeated success retain no completed clones, while historical proofs survive later revisions', async (t) => {
  const f = await fixture(t)
  const publications = []
  for (let i = 0; i < 4; i++) {
    publications.push(await f.publish())
    const dry = await maintainJournal({ ...f.opts, keepCompleted: 1 })
    assert.ok(dry.candidates <= 1)
    await maintainJournal({ ...f.opts, confirmed: true, keepCompleted: 1 })
    const dirs = (
      await fs.readdir(path.join(f.opts.root, 'preparations'), { withFileTypes: true })
    ).filter((entry) => entry.isDirectory())
    assert.equal(dirs.length, 1)
  }
  await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal((await maintainJournal(f.opts)).storage.proposal_git, 0)
  for (const p of publications)
    await verifyAcceptedPublication({ ...f.opts, receipt: await p.receipt() })
})

for (const phase of ['draft', 'sealing', 'sealed', 'installing']) {
  test(`unresolved ${phase} preparations are protected regardless of retention`, async (t) => {
    const f = await fixture(t)
    const p = await f.prepare()
    const receipt = await p.receipt()
    receipt.publication.phase = phase
    await savePreparation(f.opts.root, receipt)
    const before = await snapshot(f.base)
    const result = await maintainJournal({ ...f.opts, confirmed: true })
    assert.equal(result.candidates, 0)
    assert.equal(result.entries[0].reason, 'unresolved_preparation')
    assert.deepEqual(await snapshot(f.base), before)
  })
}

for (const tamper of [
  'receipt',
  'report',
  'backup',
  'draft',
  'untracked',
  'journal-extra',
  'git-extra',
  'git-info-extra',
  'git-hooks-extra',
  'git-objects-info-extra',
  'git-pack-extra',
  'git-empty-extra',
  'git-metadata-edit',
  'git-ownership-missing',
  'git-unreachable-object',
  'draft-backup-extra',
  'symlink',
]) {
  test(`tampered ${tamper} evidence cannot authorize deletion`, async (t) => {
    const f = await fixture(t)
    const p = await f.publish()
    if (tamper === 'receipt') {
      const receipt = await p.receipt()
      receipt.validated['log.md'] = '0'.repeat(64)
      await savePreparation(f.opts.root, receipt)
    }
    if (tamper === 'report')
      await fs.appendFile(
        path.join(f.opts.root, 'runs', p.result.run_id, 'report.md'),
        'Foreign report edit.\n',
      )
    if (tamper === 'backup')
      await fs.appendFile(
        path.join(f.opts.wikiRoot, '.git', (await p.receipt()).backups[0].path),
        'Foreign late write.\n',
      )
    if (tamper === 'draft')
      await fs.appendFile(path.join(p.draft, 'wiki/overview.md'), 'Foreign draft edit.\n')
    if (tamper === 'untracked')
      await fs.writeFile(path.join(p.draft, 'wiki/foreign.md'), 'Foreign untracked file.\n')
    if (tamper === 'journal-extra')
      await fs.writeFile(path.join(p.draft, 'journal/foreign'), 'Foreign journal file.\n')
    if (tamper === 'git-extra')
      await fs.writeFile(path.join(p.draft, 'wiki/.git/foreign'), 'Foreign Git file.\n')
    const nestedGitFiles = {
      'git-info-extra': 'info/operator-recovery.txt',
      'git-hooks-extra': 'hooks/operator-recovery.txt',
      'git-objects-info-extra': 'objects/info/operator-recovery.txt',
      'git-pack-extra': 'objects/pack/operator-recovery.txt',
    }
    if (nestedGitFiles[tamper])
      await fs.writeFile(
        path.join(p.draft, 'wiki/.git', nestedGitFiles[tamper]),
        'Foreign recovery evidence.\n',
      )
    if (tamper === 'git-empty-extra')
      await fs.mkdir(path.join(p.draft, 'wiki/.git/info/operator-recovery'))
    if (tamper === 'git-metadata-edit')
      await fs.appendFile(
        path.join(p.draft, 'wiki/.git/info/exclude'),
        '\n# Foreign recovery note.\n',
      )
    if (tamper === 'git-ownership-missing') {
      const receipt = await p.receipt()
      delete receipt.publication.git_baseline
      await savePreparation(f.opts.root, receipt)
    }
    if (tamper === 'git-unreachable-object')
      await runFile('git', ['hash-object', '-w', '--', 'config'], {
        cwd: path.join(p.draft, 'wiki/.git'),
      })
    if (tamper === 'draft-backup-extra') {
      const receipt = await p.receipt()
      const evidence = await loadPreparation({
        root: path.join(p.draft, 'journal'),
        preparationId: receipt.publication.draft_id,
      })
      await fs.writeFile(
        path.join(
          p.draft,
          'wiki/.git',
          path.dirname(evidence.backups[0].path),
          'operator-recovery',
        ),
        'Foreign backup evidence.\n',
      )
    }
    if (tamper === 'symlink') await fs.symlink(f.opts.wikiRoot, path.join(p.draft, 'wiki/foreign'))
    if (tamper === 'symlink')
      await assert.rejects(maintainJournal({ ...f.opts, confirmed: true }), /unsafe_symlink/)
    else {
      const before = await snapshot(f.base)
      for (const confirmed of [false, true]) {
        const result = await maintainJournal({ ...f.opts, confirmed })
        assert.equal(result.candidates, 0)
        assert.deepEqual(await snapshot(f.base), before)
      }
    }
    assert.ok((await fs.stat(p.draft)).isDirectory())
  })
}

for (const file of ['controller.json', 'controller-initialized.json']) {
  for (const corruption of ['missing', 'invalid-json', 'invalid-shape']) {
    test(`maintenance preserves all data with ${corruption} ${file}`, async (t) => {
      const f = await fixture(t)
      await f.publish()
      const target = path.join(f.opts.stateRoot, file)
      if (corruption === 'missing') await fs.unlink(target)
      else await fs.writeFile(target, corruption === 'invalid-json' ? '{' : '{"version":2}')
      const before = await snapshot(f.base)
      for (const confirmed of [false, true]) {
        await assert.rejects(maintainJournal({ ...f.opts, confirmed }), /invalid_controller_fence/)
        assert.deepEqual(await snapshot(f.base), before)
      }
    })
  }
}

test('maintenance requires controller authority even when both authority files are missing', async (t) => {
  const f = await fixture(t)
  await f.publish()
  for (const file of ['controller.json', 'controller-initialized.json'])
    await fs.unlink(path.join(f.opts.stateRoot, file))
  const before = await snapshot(f.base)
  await assert.rejects(maintainJournal({ ...f.opts, confirmed: true }), /invalid_controller_fence/)
  assert.deepEqual(await snapshot(f.base), before)
})

test('interrupted deletion can be retried after partial unlink and refuses newly introduced files', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  const unlink = fs.unlink
  let calls = 0
  fs.unlink = async (file) => {
    if (file.startsWith(p.draft) && ++calls === 3) throw new Error('Synthetic crash')
    return unlink(file)
  }
  syncBuiltinESMExports()
  try {
    await assert.rejects(maintainJournal({ ...f.opts, confirmed: true }), /Synthetic crash/)
  } finally {
    fs.unlink = unlink
    syncBuiltinESMExports()
  }
  assert.equal((await p.receipt()).compaction.state, 'deleting')
  const receipt = await p.receipt()
  const baseline = receipt.publication.git_baseline
  delete receipt.publication.git_baseline
  await savePreparation(f.opts.root, receipt)
  const interrupted = await snapshot(f.base)
  const unowned = await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal(unowned.entries[0].reason, 'draft_git_ownership_missing')
  assert.deepEqual(await snapshot(f.base), interrupted)
  receipt.publication.git_baseline = baseline
  await savePreparation(f.opts.root, receipt)
  const extra = path.join(p.draft, 'foreign')
  await fs.writeFile(extra, 'Foreign data after crash.\n')
  assert.equal((await maintainJournal({ ...f.opts, confirmed: true })).candidates, 0)
  await fs.unlink(extra)
  assert.equal((await maintainJournal({ ...f.opts, confirmed: true })).candidates, 1)
  assert.equal((await p.receipt()).compaction.state, 'done')
})

test('evidence changed between scan and deletion refuses before deleting the draft', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  const report = path.join(f.opts.root, 'runs', p.result.run_id, 'report.md')
  const beforeDraft = await snapshot(p.draft)
  const readFile = fs.readFile
  let reads = 0
  fs.readFile = async (file, ...args) => {
    // Inventory hashes it first, then scan verification reads it, then the
    // confirmed deletion pass must verify again under the run lock.
    if (file === report && ++reads === 3) await fs.appendFile(report, 'Synthetic late edit.\n')
    return readFile(file, ...args)
  }
  syncBuiltinESMExports()
  try {
    await assert.rejects(
      maintainJournal({ ...f.opts, confirmed: true }),
      /report_evidence_mismatch/,
    )
  } finally {
    fs.readFile = readFile
    syncBuiltinESMExports()
  }
  assert.equal(reads, 3)
  assert.deepEqual(await snapshot(p.draft), beforeDraft)
  assert.equal((await p.receipt()).compaction, undefined)
})

test('backup/restore retains compact proofs, reports and publication verification', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  await maintainJournal({ ...f.opts, confirmed: true })
  const archive = path.join(f.base, 'backup.tar')
  await runFile('tar', ['-cf', archive, '-C', f.base, 'journal', 'wiki', 'sources', 'publisher'])
  // Restore the same configured mount roots, as required by the stack contract.
  for (const directory of Object.values(f.opts)) await fs.rm(directory, { recursive: true })
  await runFile('tar', ['-xf', archive, '-C', f.base])
  const run = await loadRun({ ...f.opts, runId: p.result.run_id })
  await verifyIngestCommit({ ...f.opts, record: run.records[0] })
  await verifyAcceptedPublication({ ...f.opts, receipt: await p.receipt() })
  assert.equal((await maintainJournal(f.opts)).entries[0].reason, 'already_compacted')
})

test('cleanup retry also succeeds after all draft files were removed but acknowledgement was lost', async (t) => {
  const f = await fixture(t)
  const p = await f.publish()
  const rmdir = fs.rmdir
  fs.rmdir = async (directory) => {
    await rmdir(directory)
    if (directory === p.draft) throw new Error('Synthetic lost acknowledgement')
  }
  syncBuiltinESMExports()
  try {
    await assert.rejects(
      maintainJournal({ ...f.opts, confirmed: true }),
      /Synthetic lost acknowledgement/,
    )
  } finally {
    fs.rmdir = rmdir
    syncBuiltinESMExports()
  }
  assert.equal((await p.receipt()).compaction.state, 'deleting')
  await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal((await p.receipt()).compaction.state, 'done')
})

for (const blocker of ['rollback', 'unfinished', 'blocked']) {
  test(`completed-phase receipt with ${blocker} evidence is retained`, async (t) => {
    const f = await fixture(t)
    const p = await f.publish()
    const receipt = await p.receipt()
    if (blocker === 'rollback') {
      receipt.rollback_started = true
      await savePreparation(f.opts.root, receipt)
    } else {
      const directory = path.join(f.opts.root, 'runs', p.result.run_id)
      if (blocker === 'unfinished') {
        const file = path.join(directory, 'run.json')
        const run = JSON.parse(await fs.readFile(file))
        run.unfinished = [
          { source_path: receipt.source.source_path, blocker: 'Synthetic pending work.' },
        ]
        await fs.writeFile(file, JSON.stringify(run))
      } else {
        const file = path.join(directory, 'records.jsonl')
        const record = JSON.parse((await fs.readFile(file, 'utf8')).trim())
        await fs.writeFile(
          file,
          `${JSON.stringify({ ...record, status: 'blocked', blocker: 'Synthetic failure.' })}\n`,
        )
      }
    }
    const before = await snapshot(f.base)
    assert.equal((await maintainJournal({ ...f.opts, confirmed: true })).candidates, 0)
    assert.deepEqual(await snapshot(f.base), before)
  })
}

test('maintenance refuses active controller, missing ownership locks and concurrent writers', async (t) => {
  const f = await fixture(t)
  await f.publish()
  await fs.writeFile(
    path.join(f.opts.stateRoot, 'manual.json'),
    JSON.stringify({ version: 1, status: 'operator_action_required' }),
  )
  await assert.rejects(maintainJournal({ ...f.opts, confirmed: true }), /controller_not_idle/)
  await fs.writeFile(path.join(f.opts.stateRoot, 'manual.json'), JSON.stringify(manualIdle))
  const lock = path.join(f.opts.wikiRoot, '.git/wiki-ingest-publication.lock')
  const { spawn } = await import('node:child_process')
  const writer = spawn(
    'python3',
    [
      '-c',
      'import fcntl,sys\nf=open(sys.argv[1],"r+")\nfcntl.flock(f,fcntl.LOCK_EX)\nprint("locked",flush=True)\nsys.stdin.read()',
      lock,
    ],
    { stdio: ['pipe', 'pipe', 'inherit'] },
  )
  const exited = new Promise((resolve) => writer.once('close', resolve))
  await new Promise((resolve) => writer.stdout.once('data', resolve))
  try {
    const before = await snapshot(f.base)
    await assert.rejects(maintainJournal({ ...f.opts, confirmed: true }), /Ingest-Lock/)
    assert.deepEqual(await snapshot(f.base), before)
  } finally {
    writer.stdin.end()
    await exited
  }
  await fs.unlink(lock)
  const before = await snapshot(f.base)
  await assert.rejects(maintainJournal(f.opts), /lock_file_missing/)
  assert.deepEqual(await snapshot(f.base), before)
})

test('synthetic storage and backup benchmark', async (t) => {
  const f = await fixture(t)
  // Six deterministic, incompressible historical versions, absent from HEAD.
  for (let revision = 0; revision < 6; revision++) {
    const data = Buffer.concat(
      Array.from({ length: 4096 }, (_, i) =>
        createHash('sha256').update(`synthetic-${revision}-${i}`).digest(),
      ),
    ).toString('base64')
    await fs.writeFile(path.join(f.opts.wikiRoot, 'obsolete.md'), data)
    await f.git('add', '.')
    await f.git('commit', '-qm', 'Synthetic historical content')
  }
  await fs.unlink(path.join(f.opts.wikiRoot, 'obsolete.md'))
  await f.git('add', '.')
  await f.git('commit', '-qm', 'Remove synthetic historical content')
  const publications = []
  for (let i = 0; i < 6; i++) {
    const p = await f.publish()
    await p.call('resume')
    publications.push(p)
  }
  const archive = async (name) => {
    const file = path.join(f.base, `${name}.tar.gz`)
    const started = process.hrtime.bigint()
    await runFile('tar', ['-czf', file, '-C', f.base, 'journal'])
    return {
      bytes: (await fs.stat(file)).size,
      milliseconds: Number(process.hrtime.bigint() - started) / 1e6,
    }
  }
  const measure = async (directory) =>
    Number((await runFile('du', ['-sk', directory])).stdout.split(/\s/)[0]) * 1024
  const before = await maintainJournal(f.opts)
  const beforeDisk = await measure(f.opts.root)
  const beforeBackup = await archive('before')
  const fullClone = path.join(f.base, 'full-clone')
  await runFile('git', ['clone', '--quiet', '--no-hardlinks', '--', f.opts.wikiRoot, fullClone])
  const fullCloneDisk = await measure(fullClone)
  const shallowCloneDisk = await measure(path.join(publications.at(-1).draft, 'wiki'))
  await maintainJournal({ ...f.opts, confirmed: true })
  const after = await maintainJournal(f.opts)
  const afterDisk = await measure(f.opts.root)
  const afterBackup = await archive('after')
  assert.equal(before.candidates, 6)
  assert.equal(after.storage.proposal_git, 0)
  assert.equal(after.storage.proposal_files, 0)
  assert.ok(afterDisk < beforeDisk / 2)
  assert.ok(afterBackup.bytes < beforeBackup.bytes / 2)
  assert.ok(shallowCloneDisk < fullCloneDisk / 2)
  t.diagnostic(
    JSON.stringify({
      before: { disk_bytes: beforeDisk, backup: beforeBackup, storage: before.storage },
      after: { disk_bytes: afterDisk, backup: afterBackup, storage: after.storage },
      full_clone_disk_bytes: fullCloneDisk,
      shallow_clone_disk_bytes: shallowCloneDisk,
    }),
  )
  // A subsequent successful run and acknowledgement retry add no retained
  // completed snapshot when maintenance is applied after each ingestion.
  const next = await f.publish()
  await next.call('resume')
  await maintainJournal({ ...f.opts, confirmed: true })
  assert.equal((await maintainJournal(f.opts)).storage.proposal_git, 0)
  for (const p of [...publications, next])
    await verifyAcceptedPublication({ ...f.opts, receipt: await p.receipt() })
})
