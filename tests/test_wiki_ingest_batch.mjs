import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  symlink,
  writeFile,
} from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { promisify } from 'node:util'
import { inspectIngestBatch, runIngestBatch } from '../config/tools/wiki_ingest_batch_core.mjs'
import { createBatchWorker, workerEvidence } from '../config/tools/wiki_ingest_batch_worker.mjs'
import { withIngestLock } from '../config/tools/wiki_ingest_storage.mjs'
import {
  ingestChatSources,
  ingestSourceLabel,
  parseIngestSummary,
} from '../config/tools/wiki_ingest_summary.mjs'

const exec = promisify(execFile)
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex')
const brief = {
  title: 'Tabelle um neue Werte ergänzt',
  summary: ['Die Quellenseite enthält zusätzliche Werte.', 'Eine Mengenangabe bleibt unklar.'],
}
const reportWith = (value) =>
  `Befunde und Fundstellen.\n\n\`\`\`ingest-summary\n${JSON.stringify(value)}\n\`\`\``

test('optional worker brief has bounded plain-text fields and one to five sentences', () => {
  assert.deepEqual(parseIngestSummary(reportWith(brief)), brief)
  for (const value of [
    null,
    {},
    { ...brief, title: '' },
    { ...brief, title: 'x'.repeat(121) },
    { ...brief, summary: [] },
    { ...brief, summary: Array(6).fill('Satz.') },
    { ...brief, summary: ['x'.repeat(401)] },
    { ...brief, summary: ['\nNeue Zeile.'] },
    { ...brief, summary: [3] },
    { ...brief, summary: ['A. B. C. D. E. F.'] },
    { ...brief, summary: ['Erster Satz. Zweiter Satz.'] },
  ])
    assert.equal(parseIngestSummary(reportWith(value)), null)
  for (const content of [
    undefined,
    'No structured report',
    '```ingest-summary\n{bad json}\n```',
    reportWith(brief) + '\n' + reportWith(brief),
    'x'.repeat(32 * 1024) + reportWith(brief),
  ])
    assert.equal(parseIngestSummary(content), null)
  for (const sentence of [
    'Am 23. Oktober sind 870 ml erfasst.',
    'Der Wert ist z. B. unklar.',
    'Die Quelle nennt Dr. Beispiel.',
    '870.5 ml wurden ergänzt.',
    'Eine Mengenangabe bleibt unklar.',
  ])
    assert.deepEqual(parseIngestSummary(reportWith({ ...brief, summary: [sentence] }))?.summary, [
      sentence,
    ])
})

test('source labels are grounded in sanitized headers or code-owned source paths', () => {
  const source = { adapter: 'webdav', source_key: 'folder/note.md', source_path: '/safe/note.md' }
  assert.equal(ingestSourceLabel(source, '# Invented.xlsx\n'), 'folder/note.md')
  const workbook = { ...source, source_key: `xlsx/${'a'.repeat(64)}/workbook.md` }
  assert.equal(
    ingestSourceLabel(workbook, '# Workbook: folder/Tabelle\\.xlsx\n'),
    'folder/Tabelle.xlsx',
  )
  assert.equal(ingestSourceLabel(workbook, 'No heading'), source.source_path)
  const email = { ...source, adapter: 'email' }
  assert.equal(
    ingestSourceLabel(email, '# Terminänderung\n\n- **Date:** 2026-01-02\n\n## Message\nText\n'),
    'E-Mail: Terminänderung · 2026-01-02',
  )
  assert.equal(
    ingestSourceLabel(email, '# Terminänderung\n\n## Message\n- **Date:** Invented\n'),
    'E-Mail: Terminänderung',
  )
  for (const text of [
    'No headers',
    `# ${'x'.repeat(241)}\n\n## Message\n`,
    '# Bad\u0000\n\n## Message\n',
  ])
    assert.equal(ingestSourceLabel(email, text), email.source_path)
  assert.equal(
    ingestSourceLabel({ ...source, adapter: 'custom' }, '# Unverified\n'),
    source.source_path,
  )
})

test('chat briefs respect a serialized byte budget, including multibyte text and changed paths', () => {
  const source = {
    source_path: '/sources/synthetic.md',
    wiki_path: '/wiki/sources/synthetic.md',
    status: 'ingested',
    commit: 'a'.repeat(40),
    attempts: [{}],
    brief: { title: 'Titel', summary: Array(5).fill('漢'.repeat(399) + '。') },
    changed_pages: [],
  }
  const selected = ingestChatSources(Array(10).fill(source))
  assert.ok(selected.length > 0 && selected.length < 10)
  assert.ok(Buffer.byteLength(JSON.stringify(selected)) <= 24 * 1024)
  assert.deepEqual(ingestChatSources([{ ...source, changed_pages: ['x'.repeat(24 * 1024)] }]), [])
})

async function fixture(t, count = 3, adapter = 'webdav') {
  const base = await mkdtemp(path.join(os.tmpdir(), 'wiki-ingest-'))
  t.after(() => rm(base, { recursive: true, force: true }))
  const opts = {
    wikiRoot: path.join(base, 'wiki'),
    sourceRoot: path.join(base, 'sources'),
    root: path.join(base, 'journal'),
  }
  await mkdir(path.join(opts.wikiRoot, 'sources', adapter), { recursive: true })
  await mkdir(path.join(opts.sourceRoot, adapter), { recursive: true })
  const git = async (...args) => (await exec('git', args, { cwd: opts.wikiRoot })).stdout.trim()
  await git('init', '-q')
  await git('config', 'user.name', 'Synthetic test')
  await git('config', 'user.email', 'test@example.invalid')
  for (const name of ['index.md', 'overview.md', 'log.md', 'AGENTS.md'])
    await writeFile(path.join(opts.wikiRoot, name), `# ${name}\n`)
  await git('add', '.')
  await git('commit', '-qm', 'baseline')
  const manifest = {
    contract: 'karpathy-wiki-provider-manifest',
    version: 1,
    source: adapter,
    wiki_root: adapter,
    generated_at: 1760000000,
    items: [],
    revoked: [],
    errors: [],
  }
  for (let i = 0; i < count; i++) {
    const name = `note-${i}.md`
    const text = `Synthetic source ${i}.\n`
    await writeFile(path.join(opts.sourceRoot, adapter, name), text)
    manifest.items.push({
      source_key: name,
      source_path: name,
      wiki_path: `${adapter}/${name}`,
      source_revision: sha(text),
      frontmatter: { source: adapter, source_path: name, source_revision: sha(text) },
      claim: { source: adapter, source_path: name },
    })
  }
  const publish = () =>
    writeFile(path.join(opts.sourceRoot, adapter, 'manifest.json'), JSON.stringify(manifest))
  await publish()
  const worker = async ({ sources }) => {
    assert.equal(sources.length, 1)
    await writeFile(sources[0].wiki_path, '# Supported finding\nSource line 1.\n')
    return {
      content: 'Befunde: source line 1. Widersprüche: none. Extraktionsgrenzen: fully read.',
      source_written: true,
      errors: [],
    }
  }
  const state = async (result) =>
    JSON.parse(
      await readFile(path.join(opts.root, 'batches', result.batch_id, 'state.json'), 'utf8'),
    )
  return { opts, git, manifest, publish, worker, state }
}

test('chat and private report share a durable brief with code-owned publication evidence', async (t) => {
  const f = await fixture(t, 1)
  const malicious = {
    ...brief,
    source_label: 'Invented.xlsx',
    status: 'failed',
    commit: 'invented',
    wiki_path: '/invented',
  }
  const result = await runIngestBatch(f.opts, async (batch) => ({
    ...(await f.worker(batch)),
    content: reportWith(malicious),
  }))
  const [item] = result.sources
  assert.equal(result.sources_total, 1)
  assert.equal(result.sources_omitted, 0)
  assert.equal(item.status, 'ingested')
  assert.equal(item.commit, result.commit)
  assert.notEqual(item.wiki_path, '/invented')
  assert.equal(item.source_label, 'note-0.md')
  assert.deepEqual(item.summary, brief.summary)
  assert.deepEqual((await f.state(result)).sources[0].brief, brief)
  const report = await readFile(result.report_path, 'utf8')
  assert.ok(report.indexOf('## Kurzüberblick') < report.indexOf('## Technische Details'))
  assert.ok(report.includes('note&#45;0&#46;md'))
  assert.ok(report.includes('Eine Mengenangabe bleibt unklar&#46;'))
})

test('email labels, literal Markdown/HTML and changed revisions preserve the same brief contract', async (t) => {
  const f = await fixture(t, 1, 'email')
  await runIngestBatch(f.opts, f.worker)
  const email = {
    ...brief,
    title: '<b>[Titel](x)</b>',
  }
  const bytes = '# Terminänderung\n\n- **Date:** 2026-01-02\n\n## Message\nRevised source.\n'
  await writeFile(path.join(f.opts.sourceRoot, 'email/note-0.md'), bytes)
  f.manifest.items[0].source_revision = sha(bytes)
  f.manifest.items[0].frontmatter.source_revision = sha(bytes)
  await f.publish()
  const result = await runIngestBatch(f.opts, async (batch) => ({
    ...(await f.worker(batch)),
    content: reportWith(email),
  }))
  assert.equal(result.ingested, 1)
  assert.equal(result.sources[0].source_label, 'E-Mail: Terminänderung · 2026-01-02')
  assert.equal(result.sources[0].title, email.title)
  assert.equal(result.sources[0].source_path, path.join(f.opts.sourceRoot, 'email/note-0.md'))
  const overview = (await readFile(result.report_path, 'utf8')).split('## Technische Details')[0]
  assert.ok(!overview.includes('<b>'))
  assert.ok(!overview.includes('[Titel](x)'))
})

test('missing or invalid briefs never retry, downgrade a commit or invent content', async (t) => {
  const f = await fixture(t, 2)
  let calls = 0
  const result = await runIngestBatch(f.opts, async (batch) => {
    calls++
    return { ...(await f.worker(batch)), content: calls === 1 ? 'Unstructured' : reportWith({}) }
  })
  assert.equal(calls, 2)
  assert.equal(result.ingested, 2)
  for (const item of result.sources) {
    assert.equal(item.status, 'ingested')
    assert.equal(item.source_label, path.basename(item.source_path))
    assert.deepEqual(item.summary, ['Inhaltliche Zusammenfassung nicht verfügbar.'])
  }
  const empty = await runIngestBatch(f.opts, () => assert.fail('already current'))
  assert.deepEqual(empty.sources, [])
  assert.equal(empty.sources_total, 0)
})

test('failed and never-started sources cannot present draft findings as published', async (t) => {
  for (const mode of ['context', 'provider'])
    await t.test(mode, async (t) => {
      const f = await fixture(t, 2)
      const result = await runIngestBatch(f.opts, async (batch) => ({
        ...(await f.worker(batch)),
        content: reportWith(brief),
        ...(mode === 'context' ? { context_limit: true } : { provider_failure: true }),
      }))
      assert.equal(result.sources[0].status, 'failed')
      assert.equal(result.sources[0].commit, null)
      assert.deepEqual(result.sources[0].changed_pages, [])
      assert.match(result.sources[0].summary.join(' '), /Nicht übernommen/)
      assert.ok(!result.sources[0].summary.includes(brief.summary[0]))
      if (mode === 'provider') assert.equal(result.sources[1].status, 'not_started')
      const overview = (await readFile(result.report_path, 'utf8')).split(
        '## Technische Details',
      )[0]
      assert.ok(!overview.includes(brief.summary[0]))
    })
})

test('other source pages remain readable live while absent from the private draft', async (t) => {
  const f = await fixture(t, 2)
  const other = 'sources/webdav/note-0.md'
  let context
  const result = await runIngestBatch(f.opts, async (batch) => {
    if (batch.sources[0].source_key === 'note-1.md') {
      await assert.rejects(readFile(path.join(batch.wiki_root, other)), { code: 'ENOENT' })
      context = await readFile(path.join(f.opts.wikiRoot, other), 'utf8')
      assert.ok(context.includes('# Supported finding'))
    }
    return f.worker(batch)
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(result.ingested, 2)
  assert.ok(result.commit)
  assert.ok(context)
  assert.equal(await readFile(path.join(f.opts.wikiRoot, other), 'utf8'), context)
})

test('one request drains fourteen sources with unique drafts and verified focused commits', async (t) => {
  const f = await fixture(t, 14)
  const roots = new Set()
  const result = await runIngestBatch(f.opts, async (batch) => {
    assert.notEqual(batch.wiki_root, f.opts.wikiRoot)
    assert.ok(!roots.has(batch.wiki_root))
    roots.add(batch.wiki_root)
    await assert.rejects(readFile(path.join(batch.wiki_root, '.git/HEAD')), { code: 'ENOENT' })
    // Earlier source pages are read-only live context, not copied into each draft.
    if (roots.size > 1)
      await assert.rejects(readFile(path.join(batch.wiki_root, 'sources/webdav/note-0.md')), {
        code: 'ENOENT',
      })
    return f.worker(batch)
  })
  assert.equal(result.status, 'done', result.blocker)
  assert.equal(roots.size, 14)
  assert.equal(result.ingested, 14)
  assert.equal(result.remaining, 0)
  assert.equal(result.sources.length, 10)
  assert.equal(result.sources_total, 14)
  assert.equal(result.sources_omitted, 4)
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '15')
  assert.equal(await f.git('status', '--porcelain'), '')
  const report = await readFile(result.report_path, 'utf8')
  assert.equal(report.split('## Technische Details')[0].match(/### /g).length, 14)
  for (const source of (await f.state(result)).sources) assert.ok(report.includes(source.commit))
  assert.equal((await runIngestBatch(f.opts, () => assert.fail('already current'))).status, 'done')
})

test('isolated failures never publish or retry; independent sources finish and a new request starts fresh', async (t) => {
  for (const kind of [
    'throw',
    'context',
    'permission',
    'skip',
    'no-write',
    'marker',
    'symlink',
    'read',
    'skill',
  ]) {
    await t.test(kind, async (t) => {
      const f = await fixture(t, 2)
      let failedRoot
      let calls = 0
      const result = await runIngestBatch(f.opts, async (batch) => {
        calls++
        const result = await f.worker(batch)
        if (batch.sources[0].source_key !== 'note-0.md') return result
        failedRoot = batch.wiki_root
        if (kind === 'throw') throw new Error('PRIVATE_NATIVE_ERROR')
        if (kind === 'context') return { ...result, context_limit: true }
        if (kind === 'permission')
          return { ...result, errors: [{ code: 'worker_permission_denied', tool: 'write' }] }
        if (['read', 'skill'].includes(kind))
          return { ...result, errors: [{ code: 'worker_tool_failed', tool: kind }] }
        if (kind === 'skip') return { ...result, content: 'SKIP: unreadable source' }
        if (kind === 'no-write') return { ...result, source_written: false }
        if (kind === 'marker')
          await writeFile(
            batch.sources[0].wiki_path,
            '<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> other\n',
          )
        if (kind === 'symlink') {
          await rm(batch.sources[0].wiki_path)
          await symlink(batch.sources[0].source_path, batch.sources[0].wiki_path)
        }
        return result
      })
      assert.equal(calls, 2)
      assert.equal(result.status, 'partial')
      assert.equal(result.ingested, 1)
      assert.equal(result.failed, 1)
      assert.equal(result.remaining, 1)
      assert.equal(result.questions, undefined)
      assert.equal(await f.git('status', '--porcelain'), '')
      await assert.rejects(readFile(path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')), {
        code: 'ENOENT',
      })
      const state = await f.state(result)
      assert.equal(state.sources[0].attempts.length, 1)
      assert.equal(state.sources[0].commit, null)
      assert.doesNotMatch(await readFile(result.report_path, 'utf8'), /PRIVATE_NATIVE_ERROR/)
      if (kind === 'context') {
        assert.equal(result.context_limited, 1)
        assert.match(result.context_help, /Wiki-Kontext und Werkzeugausgaben/)
        assert.match(result.context_help, /aufteilen, vorverarbeiten/)
      }
      assert.equal((await inspectIngestBatch(f.opts)).status, 'idle')
      const renewed = await runIngestBatch(f.opts, async (batch) => {
        assert.notEqual(batch.wiki_root, failedRoot)
        await assert.rejects(readFile(batch.sources[0].wiki_path), { code: 'ENOENT' })
        return f.worker(batch)
      })
      assert.notEqual(renewed.batch_id, result.batch_id)
      assert.equal(renewed.status, 'done')
      assert.equal(renewed.ingested, 1)
    })
  }
})

test('missing or changed source bytes and changed manifest identity stay unpublished', async (t) => {
  for (const kind of ['missing', 'digest', 'revision', 'revocation'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 2)
      const original = path.join(f.opts.sourceRoot, 'webdav/note-0.md')
      if (kind === 'missing') await rm(original)
      if (kind === 'digest') await writeFile(original, 'Unexpected bytes\n')
      const result = await runIngestBatch(f.opts, async (batch) => {
        const report = await f.worker(batch)
        if (batch.sources[0].source_key === 'note-0.md') {
          if (kind === 'revision') {
            f.manifest.items[0].source_revision = 'a'.repeat(64)
            f.manifest.items[0].frontmatter.source_revision = 'a'.repeat(64)
          }
          if (kind === 'revocation') f.manifest.items.shift()
          await f.publish()
        }
        return report
      })
      assert.equal(result.status, 'partial')
      assert.equal(result.ingested, 1)
      assert.equal(await f.git('status', '--porcelain'), '')
      await assert.rejects(readFile(path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')), {
        code: 'ENOENT',
      })
    })
})

test('conflicting identities and invalid metadata do not block independent sources or overwrite prior pages', async (t) => {
  for (const kind of ['collision', 'duplicate', 'invalid', 'provider'])
    await t.test(kind, async (t) => {
      const f = await fixture(t)
      const item = f.manifest.items[0]
      const page = path.join(f.opts.wikiRoot, 'sources', item.wiki_path)
      const prior =
        kind === 'invalid'
          ? '# Invalid metadata\n'
          : `---\nsource: webdav\nsource_path: ${item.source_path}\nsource_revision: ${item.source_revision}\n---\n# Previous\n`
      if (kind === 'provider') {
        await mkdir(path.join(f.opts.sourceRoot, 'broken'))
        await writeFile(path.join(f.opts.sourceRoot, 'broken/manifest.json'), '{}')
      } else {
        await writeFile(page, prior)
        if (kind === 'collision') {
          f.manifest.items[1].wiki_path = item.wiki_path
          await f.publish()
        }
        if (kind === 'duplicate')
          await writeFile(path.join(f.opts.wikiRoot, 'sources/webdav/duplicate.md'), prior)
        await f.git('add', '.')
        await f.git('commit', '-qm', 'prior synthetic source')
      }
      const result = await runIngestBatch(f.opts, f.worker)
      assert.equal(result.status, 'partial')
      assert.equal(result.ingested, kind === 'collision' ? 1 : kind === 'provider' ? 3 : 2)
      if (kind !== 'provider') assert.equal(await readFile(page, 'utf8'), prior)
      assert.equal(await f.git('status', '--porcelain'), '')
    })
})

test('dirty admission and foreign writes preserve edits and stop later dispatch', async (t) => {
  for (const during of [false, true])
    await t.test(String(during), async (t) => {
      const f = await fixture(t)
      const file = path.join(f.opts.wikiRoot, 'overview.md')
      if (!during) await writeFile(file, 'Unattributed work\n')
      let calls = 0
      const result = await runIngestBatch(f.opts, async (batch) => {
        calls++
        await writeFile(file, 'Unattributed work\n')
        return f.worker(batch)
      })
      assert.equal(result.status, 'blocked')
      assert.equal(result.blocker, 'dirty_wiki')
      assert.equal(calls, during ? 1 : 0)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
      assert.equal(await readFile(file, 'utf8'), 'Unattributed work\n')
    })
})

test('provider/configuration/storage failures stop dispatch without source repair', async (t) => {
  for (const kind of ['provider', 'configuration', 'capacity'])
    await t.test(kind, async (t) => {
      const f = await fixture(t)
      let calls = 0
      const result = await runIngestBatch(
        { ...f.opts, ...(kind === 'capacity' ? { maxRetainedBytes: 1 } : {}) },
        async () => {
          calls++
          return { [`${kind}_failure`]: true }
        },
      )
      assert.equal(result.status, 'blocked')
      assert.equal(calls, kind === 'capacity' ? 0 : 1)
      assert.equal(result.ingested, 0)
      assert.equal(await f.git('status', '--porcelain'), '')
    })
})

test('single-source selection and unchanged findings preserve source revision checks', async (t) => {
  const f = await fixture(t, 2)
  const opts = { ...f.opts, sourcePath: path.join(f.opts.sourceRoot, 'webdav/note-0.md') }
  assert.equal((await runIngestBatch(opts, f.worker)).ingested, 1)
  const page = path.join(f.opts.wikiRoot, 'sources/webdav/note-0.md')
  const prior = await readFile(page, 'utf8')
  const text = 'Updated synthetic source\n'
  await writeFile(opts.sourcePath, text)
  f.manifest.items[0].source_revision = sha(text)
  f.manifest.items[0].frontmatter.source_revision = sha(text)
  await f.publish()
  const refused = await runIngestBatch(opts, async () => ({
    source_written: false,
    errors: [],
    content: 'No action',
  }))
  assert.equal(refused.ingested, 0)
  assert.equal(await readFile(page, 'utf8'), prior)
  const rewritten = await runIngestBatch(opts, async (batch) => {
    await writeFile(batch.sources[0].wiki_path, prior)
    return { source_written: true, errors: [] }
  })
  assert.equal(rewritten.status, 'done')
  assert.match(await readFile(page, 'utf8'), new RegExp(sha(text)))
  assert.equal(rewritten.remaining, 1)
})

test('Paperless processing requires matching metadata and the original independent digest at publication', async (t) => {
  for (const kind of [
    'valid',
    'missing-digest',
    'metadata',
    'changed-after-worker',
    'changed-during-install',
  ])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 0)
      const directory = path.join(f.opts.sourceRoot, 'paperless')
      await mkdir(directory)
      const fields = {
        paperless_id: '1',
        paperless_url: 'https://paperless.invalid/documents/1/',
        source_revision: 'b'.repeat(64),
      }
      const text = `---\n${Object.entries(fields)
        .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
        .join('\n')}\n---\nSynthetic OCR\n`
      const file = path.join(directory, '1.md')
      await writeFile(file, text)
      const item = {
        source_key: '1',
        source_path: '1.md',
        wiki_path: 'paperless/1.md',
        source_revision: fields.source_revision,
        source_sha256: sha(text),
        frontmatter: fields,
        claim: { paperless_id: '1' },
      }
      if (kind === 'missing-digest') delete item.source_sha256
      if (kind === 'metadata') {
        await writeFile(
          file,
          text
            .replace('Synthetic OCR', 'Different OCR')
            .replace('paperless_id: "1"', 'paperless_id: "2"'),
        )
        item.source_sha256 = sha(await readFile(file))
      }
      const manifest = { ...f.manifest, source: 'paperless', wiki_root: 'paperless', items: [item] }
      const publish = () =>
        writeFile(path.join(directory, 'manifest.json'), JSON.stringify(manifest))
      await publish()
      const change = async () => {
        await writeFile(file, text + 'New generation\n')
        item.source_sha256 = sha(await readFile(file))
        await publish()
      }
      const result = await runIngestBatch(
        {
          ...f.opts,
          onPhase: async (phase) => {
            if (kind === 'changed-during-install' && phase === 'file_installed') await change()
          },
        },
        async (batch) => {
          const result = await f.worker(batch)
          if (kind === 'changed-after-worker') await change()
          return result
        },
      )
      assert.equal(result.ingested, kind === 'valid' ? 1 : 0)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), kind === 'valid' ? '2' : '1')
      if (kind === 'changed-during-install') {
        const recovery = await runIngestBatch(
          { ...f.opts, batchId: result.batch_id, authorize: async () => {} },
          () => assert.fail('no worker'),
        )
        assert.equal(recovery.status, 'blocked')
        assert.equal(recovery.ingested, 0)
      }
    })
})

test('control storage is mandatory; failed report storage cannot undo or repeat commits', async (t) => {
  const bad = await fixture(t, 1)
  await writeFile(bad.opts.root, 'Not a directory')
  await assert.rejects(runIngestBatch(bad.opts, () => assert.fail()))
  assert.equal(await bad.git('rev-list', '--count', 'HEAD'), '1')
  const f = await fixture(t)
  const result = await runIngestBatch(
    {
      ...f.opts,
      onPhase: async (phase) => {
        if (phase === 'source_recorded')
          for (const directory of await readdir(path.join(f.opts.root, 'batches')))
            await mkdir(path.join(f.opts.root, 'batches', directory, 'report.md'), {
              recursive: true,
            })
      },
    },
    async (batch) => ({ ...(await f.worker(batch)), content: reportWith(brief) }),
  )
  assert.equal(result.status, 'done')
  assert.equal(result.ingested, 3)
  assert.equal(result.report_path, null)
  assert.equal(result.sources.length, 3)
  assert.ok(result.sources.every((item) => item.status === 'ingested' && item.commit))
  assert.ok(result.sources.every((item) => item.source_label === path.basename(item.source_path)))
  assert.ok(result.sources.every((item) => item.summary.join(' ') === brief.summary.join(' ')))
  assert.equal(await f.git('rev-list', '--count', 'HEAD'), '4')
})

test('real process death recovers only prepared publication without duplicate commits/logs', async (t) => {
  for (const [phase, count] of [
    ['publication_prepared'],
    ['file_installed', 1],
    ['file_installed', 2],
    ['file_installed', 3],
    ['index_installed'],
    ['commit_installed'],
    ['source_recorded'],
  ])
    await t.test(`${phase}-${count ?? ''}`, async (t) => {
      const f = await fixture(t, 1)
      const config = path.join(f.opts.root, '../opts.json')
      await writeFile(config, JSON.stringify(f.opts))
      await assert.rejects(
        exec(process.execPath, [
          'tests/fixtures/ingest-crash.mjs',
          config,
          phase,
          String(count ?? 0),
        ]),
        (error) => error.signal === 'SIGKILL',
      )
      let result = await runIngestBatch(f.opts, () => assert.fail('no model work on recovery'))
      if (phase !== 'source_recorded') {
        assert.equal(result.blocker, 'publication_interrupted')
        await assert.rejects(
          runIngestBatch(
            {
              ...f.opts,
              batchId: result.batch_id,
              authorize: async () => {
                throw new Error('Denied')
              },
            },
            () => assert.fail(),
          ),
          /Denied/,
        )
        result = await runIngestBatch(
          { ...f.opts, batchId: result.batch_id, authorize: async () => {} },
          () => assert.fail(),
        )
      }
      assert.equal(result.status, 'done', result.blocker)
      assert.equal(result.ingested, 1)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(
        ((await readFile(path.join(f.opts.wikiRoot, 'log.md'), 'utf8')).match(/Ingest /g) ?? [])
          .length,
        1,
      )
    })
})

test('completed draft cleanup tolerates absence but reports unsafe leftovers without losing commits', async (t) => {
  for (const kind of ['absent', 'unsafe'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 2)
      let directory
      let calls = 0
      const result = await runIngestBatch(
        {
          ...f.opts,
          onPhase: async (phase) => {
            if (phase !== 'source_recorded' || calls !== 1) return
            if (kind === 'absent') await rm(directory, { recursive: true })
            else {
              await rename(directory, `${directory}-retained`)
              await symlink(`${directory}-retained`, directory)
            }
          },
        },
        async (batch) => {
          calls++
          directory = path.dirname(batch.wiki_root)
          return f.worker(batch)
        },
      )
      assert.equal(result.status, kind === 'absent' ? 'done' : 'blocked')
      assert.equal(calls, kind === 'absent' ? 2 : 1)
      assert.equal(result.ingested, calls)
      assert.equal(result.blocker, kind === 'absent' ? null : 'draft_cleanup_failed')
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), String(calls + 1))
      assert.equal((await f.state(result)).sources[0].status, 'ingested')
      if (kind === 'unsafe') {
        assert.match(await readFile(result.report_path, 'utf8'), /draft_cleanup_failed/)
        assert.match(
          await readFile(
            path.join(`${directory}-retained`, 'wiki/sources/webdav/note-0.md'),
            'utf8',
          ),
          /Supported finding/,
        )
      }
    })
})

test('recovery and interrupted close finish bookkeeping when completed drafts cannot be cleaned', async (t) => {
  for (const kind of ['recovery', 'close'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 2)
      if (kind === 'recovery') {
        const result = await runIngestBatch(
          {
            ...f.opts,
            onPhase: (phase) => {
              if (phase === 'publication_prepared') throw new Error('Stop')
            },
          },
          f.worker,
        )
        assert.equal(result.blocker, 'publication_interrupted')
        const attempt = (await f.state(result)).sources[0].attempts[0].id
        const directory = path.join(f.opts.root, 'drafts', result.batch_id, attempt)
        await chmod(directory, 0o500)
        try {
          const recovered = await runIngestBatch(
            { ...f.opts, batchId: result.batch_id, authorize: async () => {} },
            () => assert.fail('no worker'),
          )
          assert.equal(recovered.status, 'blocked')
          assert.equal(recovered.blocker, 'draft_cleanup_failed')
          assert.equal(recovered.ingested, 1)
          const state = await f.state(recovered)
          assert.equal(state.sources[0].status, 'ingested')
          assert.equal(state.sources[1].blocker, 'interrupted_batch')
          assert.match(
            await readFile((await runIngestBatch(f.opts, f.worker)).report_path, 'utf8'),
            /note-1\.md/,
          )
        } finally {
          await chmod(directory, 0o700).catch(() => {})
        }
      } else {
        const config = path.join(f.opts.root, '../opts.json')
        await writeFile(config, JSON.stringify(f.opts))
        await assert.rejects(
          exec(process.execPath, ['tests/fixtures/ingest-crash.mjs', config, 'source_recorded']),
          (error) => error.signal === 'SIGKILL',
        )
        const [batchId] = await readdir(path.join(f.opts.root, 'batches'))
        const attempt = JSON.parse(
          await readFile(path.join(f.opts.root, 'batches', batchId, 'state.json'), 'utf8'),
        ).sources[0].attempts[0].id
        const directory = path.join(f.opts.root, 'drafts', batchId, attempt)
        await chmod(directory, 0o500)
        try {
          const closed = await runIngestBatch(f.opts, () => assert.fail('no worker'))
          assert.equal(closed.status, 'blocked')
          assert.equal(closed.blocker, 'draft_cleanup_failed')
          assert.equal(closed.ingested, 1)
          const state = await f.state(closed)
          assert.equal(state.sources[0].status, 'ingested')
          assert.equal(state.sources[1].blocker, 'interrupted_batch')
          assert.match(
            await readFile(path.join(directory, 'wiki/sources/webdav/note-0.md'), 'utf8'),
            /Recovered synthetic finding/,
          )
          assert.match(
            await readFile((await runIngestBatch(f.opts, f.worker)).report_path, 'utf8'),
            /note-1\.md/,
          )
        } finally {
          await chmod(directory, 0o700).catch(() => {})
        }
      }
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '3')
    })
})

test('process death in private work closes the batch without interpretation or publication', async (t) => {
  const f = await fixture(t, 2)
  const config = path.join(f.opts.root, '../opts.json')
  await writeFile(config, JSON.stringify(f.opts))
  await assert.rejects(
    exec(process.execPath, ['tests/fixtures/ingest-crash.mjs', config, 'worker_written']),
    (error) => error.signal === 'SIGKILL',
  )
  const closed = await runIngestBatch(f.opts, () => assert.fail('close only'))
  assert.equal(closed.status, 'partial')
  assert.equal(closed.failed, 2)
  assert.equal(closed.ingested, 0)
  assert.equal(await f.git('status', '--porcelain'), '')
  const next = await runIngestBatch(f.opts, f.worker)
  assert.notEqual(next.batch_id, closed.batch_id)
  assert.equal(next.ingested, 2)
})

test('cancellation preserves prior commits/private drafts; publication completion never starts remaining workers', async (t) => {
  for (const publishing of [false, true])
    await t.test(String(publishing), async (t) => {
      const f = await fixture(t)
      const controller = new AbortController()
      let calls = 0
      const result = await runIngestBatch(
        {
          ...f.opts,
          signal: controller.signal,
          onPhase: (phase) => {
            if (publishing && phase === 'file_installed') controller.abort()
          },
        },
        async (batch) => {
          calls++
          const report = await f.worker(batch)
          if (!publishing && calls === 2) controller.abort()
          return report
        },
      )
      assert.equal(result.status, 'cancelled')
      if (publishing) {
        const completed = await runIngestBatch(
          { ...f.opts, batchId: result.batch_id, authorize: async () => {} },
          () => assert.fail('only finish publication'),
        )
        assert.equal(completed.ingested, 1)
        assert.equal(completed.remaining, 2)
        assert.equal(completed.status, 'cancelled')
      } else assert.equal(result.ingested, 1)
      assert.equal(await f.git('status', '--porcelain'), '')
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '2')
    })
})

test('foreign edits, source revocation and changed drafts block interrupted publication without overwrite', async (t) => {
  for (const kind of ['live', 'index', 'source', 'draft', 'approval-race'])
    await t.test(kind, async (t) => {
      const f = await fixture(t, 1)
      const result = await runIngestBatch(
        {
          ...f.opts,
          onPhase: (phase) => {
            if (phase === 'publication_prepared') throw new Error('Stop')
          },
        },
        f.worker,
      )
      assert.equal(result.blocker, 'publication_interrupted')
      const state = await f.state(result)
      const overview = path.join(f.opts.wikiRoot, 'overview.md')
      const change = () => writeFile(overview, 'Foreign bytes\n')
      if (kind === 'live') await change()
      if (kind === 'index') {
        await change()
        await f.git('add', 'overview.md')
      }
      if (kind === 'source') {
        f.manifest.items = []
        await f.publish()
      }
      if (kind === 'draft')
        await writeFile(
          path.join(
            f.opts.root,
            'drafts',
            result.batch_id,
            state.sources[0].attempts[0].id,
            'wiki/sources/webdav/note-0.md',
          ),
          'Tampered\n',
        )
      const recovered = await runIngestBatch(
        {
          ...f.opts,
          batchId: result.batch_id,
          authorize: async () => {
            if (kind === 'approval-race') await change()
          },
        },
        () => assert.fail(),
      )
      assert.equal(recovered.status, 'blocked')
      assert.equal(recovered.ingested, 0)
      assert.equal(await f.git('rev-list', '--count', 'HEAD'), '1')
      if (['live', 'index', 'approval-race'].includes(kind))
        assert.equal(await readFile(overview, 'utf8'), 'Foreign bytes\n')
    })
})

test('read-only diagnostics expose active publication evidence without starting or mutating work', async (t) => {
  const f = await fixture(t, 1)
  assert.equal((await inspectIngestBatch(f.opts)).status, 'idle')
  const result = await runIngestBatch(
    {
      ...f.opts,
      ownerSession: 'ses_owner',
      onPhase: (phase) => {
        if (phase === 'publication_prepared') throw new Error('Stop')
      },
    },
    async (batch) => ({ ...(await f.worker(batch)), session_id: 'ses_worker' }),
  )
  const before = await f.state(result)
  const observed = await inspectIngestBatch(f.opts)
  assert.equal(observed.owner_session, 'ses_owner')
  assert.equal(observed.sources[0].worker_session, 'ses_worker')
  assert.deepEqual(await f.state(result), before)
  await assert.rejects(inspectIngestBatch({ ...f.opts, offset: -1 }), /invalid_diagnostic_offset/)
  await writeFile(path.join(f.opts.root, 'active.json'), JSON.stringify({ batch_id: '../escape' }))
  await assert.rejects(inspectIngestBatch(f.opts), /invalid_active_batch/)
})

test('kernel lock excludes another process throughout worker execution', async (t) => {
  const f = await fixture(t, 1)
  await runIngestBatch(f.opts, async (batch) => {
    await assert.rejects(
      exec('python3', [
        '-c',
        'import fcntl, sys; f=open(sys.argv[1], "a"); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)',
        path.join(f.opts.wikiRoot, '.git/wiki-ingest-publication.lock'),
      ]),
    )
    return f.worker(batch)
  })
  await withIngestLock(
    path.join(f.opts.wikiRoot, '.git/wiki-ingest-publication.lock'),
    async () => {},
  )
})

const original = '/knowledge/sources/webdav/a.md'
const source = '/knowledge/wiki/sources/webdav/a.md'
const nativeTool = (name, input, status = 'completed', error) => ({
  type: 'assistant',
  content: [{ type: 'tool', name, state: { input, status, error } }],
})
const read = (text, file = original) => {
  const message = nativeTool('read', { path: file })
  message.content[0].state.content = [{ type: 'text', text }]
  return message
}
const skill = () => nativeTool('skill', { id: 'wiki-ingest' })

test('read coverage requires every untruncated line, including paginated output, and a loaded skill', () => {
  const options = { sourcePath: original, sourceLines: 3 }
  const write = nativeTool('write', { path: source })
  const evidence = (messages) => workerEvidence(messages, source, 'succeeded', options)
  assert.ok(
    evidence([skill(), read('1: A\n2: B\n'), write]).errors.some(
      (e) => e.code === 'worker_source_not_read',
    ),
  )
  assert.deepEqual(evidence([skill(), read('1: A\n2: B\n'), read('3: C\n'), write]).errors, [])
  assert.ok(
    evidence([
      skill(),
      read('1: A\n2: B (line truncated to 2000 chars)\n3: C\n'),
      write,
    ]).errors.some((e) => e.code === 'worker_source_truncated'),
  )
  assert.ok(
    evidence([read('1: A\n2: B\n3: C\n'), write]).errors.some(
      (e) => e.code === 'worker_skill_not_loaded',
    ),
  )
  assert.deepEqual(
    evidence([
      skill(),
      read('1: A (line truncated to 2000 chars)\n'),
      read('1: A\n2: B\n3: C\n'),
      write,
    ]).errors,
    [],
  )
})

test('exact native writes and failures matter, not final model prose', () => {
  for (const mutation of ['write', 'edit', 'patch']) {
    const input =
      mutation === 'patch'
        ? { patchText: `*** Begin Patch\n*** Update File: ${source}\n@@\n-A\n+B\n*** End Patch` }
        : { path: source }
    assert.equal(
      workerEvidence([nativeTool(mutation, input)], source, 'succeeded').source_written,
      true,
    )
    assert.equal(
      workerEvidence(
        [nativeTool(mutation, input, 'error', { type: 'tool.execution' })],
        source,
        'succeeded',
      ).source_written,
      false,
    )
  }
  assert.equal(
    workerEvidence(
      [nativeTool('write', { path: '/knowledge/wiki/overview.md' })],
      source,
      'succeeded',
    ).source_written,
    false,
  )
  assert.ok(
    workerEvidence([nativeTool('write', { path: source })], source, 'failed').errors.some(
      (e) => e.code === 'worker_session_failed',
    ),
  )
})

test('only an absent exact target read recovered by later creation is forgiven; denials survive bounded errors', () => {
  const missing = nativeTool('read', { path: source }, 'error', {
    type: 'tool.execution',
    message: `File not found: ${source}`,
  })
  const write = nativeTool('write', { path: source })
  assert.deepEqual(
    workerEvidence([missing, write], source, 'succeeded', { sourceAbsent: true }).errors,
    [],
  )
  assert.ok(workerEvidence([missing, write], source, 'succeeded').errors.length)
  assert.ok(
    workerEvidence([write, missing], source, 'succeeded', { sourceAbsent: true }).errors.length,
  )
  const denied = nativeTool('write', { path: source }, 'error', { type: 'permission.rejected' })
  const failures = Array.from({ length: 25 }, () => missing)
  assert.ok(
    workerEvidence([...failures, denied, write], source, 'succeeded').errors.some(
      (e) => e.code === 'worker_permission_denied',
    ),
  )
  assert.ok(workerEvidence([denied, write], source, 'succeeded').errors.length)
})

test('missing target recovery normalizes only the exact absent draft target', () => {
  const wikiRoot = '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki'
  const relative = 'sources/email/synthetic/index.md'
  const target = `${wikiRoot}/${relative}`
  const create = nativeTool('patch', {
    patchText: `*** Begin Patch\n*** Add File: ${relative}\n+# Finding\n*** End Patch`,
  })
  for (const input of [relative, target]) {
    for (const missingPath of [relative, target]) {
      const missing = nativeTool('read', { path: input }, 'error', {
        type: 'tool.execution',
        message: `File not found: ${missingPath}`,
      })
      const options = { wikiRoot, sourceAbsent: true }
      const result = workerEvidence([missing, create], target, 'succeeded', options)
      assert.deepEqual(result.errors, [])
      assert.deepEqual(result.recovered_errors, [{ code: 'worker_tool_failed', tool: 'read' }])
      assert.ok(workerEvidence([missing], target, 'succeeded', options).errors.length)
      assert.ok(workerEvidence([create, missing], target, 'succeeded', options).errors.length)
      assert.ok(workerEvidence([missing, create], target, 'succeeded', { wikiRoot }).errors.length)
    }
  }
  for (const [input, message, type] of [
    [relative, 'File not found: sources/email/other/index.md', 'tool.execution'],
    ['sources/email/other/index.md', `File not found: ${relative}`, 'tool.execution'],
    [relative, `File not found: ${relative}`, 'permission.rejected'],
    [relative, `Permission denied: ${relative}`, 'tool.execution'],
  ]) {
    const missing = nativeTool('read', { path: input }, 'error', { type, message })
    assert.ok(
      workerEvidence([missing, create], target, 'succeeded', { wikiRoot, sourceAbsent: true })
        .errors.length,
    )
  }
})

test('mixed add/update stale patch recovery requires creation and rereading existing targets', () => {
  const overview = '/knowledge/wiki/overview.md'
  const input = {
    patchText: `*** Begin Patch\n*** Add File: ${source}\n+# Finding\n*** Update File: ${overview}\n@@\n-Stale\n+Current\n*** End Patch`,
  }
  const failed = nativeTool('patch', input, 'error', {
    type: 'tool.execution',
    message: 'patch verification failed: Failed to find expected lines',
  })
  const corrected = nativeTool('patch', input)
  const reread = read('1: Current\n', overview)
  const result = workerEvidence([failed, reread, corrected], source, 'succeeded')
  assert.deepEqual(result.errors, [])
  assert.deepEqual(result.recovered_errors, [{ code: 'worker_tool_failed', tool: 'patch' }])
  for (const messages of [
    [failed, corrected],
    [reread, failed, corrected],
    [failed, corrected, reread],
    [failed, reread, nativeTool('write', { path: overview })],
    [failed, reread, nativeTool('write', { path: source })],
    [failed, reread, nativeTool('edit', { path: source }), nativeTool('write', { path: overview })],
    [failed, reread, failed],
  ]) {
    assert.ok(workerEvidence(messages, source, 'succeeded').errors.length)
  }
})

test('safe in-session read/skill/patch correction is auditable; unrelated or late errors stay fatal', () => {
  const failedRead = nativeTool('read', { path: original }, 'error', { type: 'tool.execution' })
  const failedSkill = nativeTool('skill', { id: 'wiki-ingest' }, 'error', {
    type: 'tool.execution',
  })
  const options = { sourcePath: original, sourceLines: 1 }
  const valid = [skill(), read('1: Fully read\n'), nativeTool('write', { path: source })]
  assert.deepEqual(
    workerEvidence([failedRead, failedSkill, ...valid], source, 'succeeded', options).errors,
    [],
  )
  assert.ok(workerEvidence([...valid, failedRead], source, 'succeeded', options).errors.length)
  assert.ok(workerEvidence([...valid, failedSkill], source, 'succeeded', options).errors.length)
  const patch = nativeTool(
    'patch',
    { patchText: `*** Begin Patch\n*** Update File: ${source}\n@@\n-X\n+Y\n*** End Patch` },
    'error',
    { type: 'tool.execution', message: 'patch verification failed: Failed to find expected lines' },
  )
  assert.ok(workerEvidence([patch, ...valid], source, 'succeeded', options).errors.length)
  assert.deepEqual(
    workerEvidence([patch, read('1: Current\n', source), ...valid], source, 'succeeded', options)
      .errors,
    [],
  )
})

test('worker admission allows directory reads but not broader writes; compaction stops only admitted workers', async (t) => {
  const admissionRoot = await mkdtemp(path.join(os.tmpdir(), 'ingest-admission-'))
  t.after(() => rm(admissionRoot, { recursive: true, force: true }))
  const hooks = {}
  let permission
  const root = '/knowledge/incoming/ingest-journal/drafts/synthetic/wiki'
  const events = []
  const ctx = {
    tool: { transform: async () => {} },
    permission: {
      hook: async (_name, callback) => {
        permission = callback
      },
    },
    session: {
      hook: async (name, callback) => {
        hooks[name] = callback
      },
      get: async ({ sessionID }) =>
        sessionID === 'ses_parent'
          ? { model: { providerID: 'synthetic', id: 'fixture' } }
          : {
              agent: 'wiki-ingest',
              parentID: 'ses_parent',
              outcome: 'failed',
              location: { directory: root },
            },
      move: async () => {},
      create: async (input) => {
        events.push(input)
        return { id: 'ses_child' }
      },
      prompt: async ({ text }) => {
        const event = { sessionID: 'ses_child', system: [] }
        await hooks.context(event)
        for (const guidance of [text, event.system[0].text]) {
          assert.ok(
            guidance.includes(
              `Lies andere Quellenseiten gezielt unter /knowledge/wiki/sources/<Wiki-Pfad>, nicht unter ${root}/sources`,
            ),
          )
          assert.ok(
            guidance.includes(
              'Live-Wiki-Kontext ist nur lesbar; alle Änderungen bleiben im Entwurf',
            ),
          )
        }
        for (const resource of [
          root,
          `${root}/`,
          '.',
          '/knowledge/wiki',
          '/knowledge/wiki/',
          '/knowledge/wiki/sources/webdav/other.md',
        ]) {
          const event = {
            sessionID: 'ses_child',
            agent: 'wiki-ingest',
            action: 'read',
            resources: [resource],
            effect: 'allow',
          }
          await permission(event)
          assert.equal(event.effect, 'allow', resource)
        }
        for (const resource of [
          root,
          '/knowledge/wiki',
          '/knowledge/wiki/sources/webdav/other.md',
          `${root}/sources/webdav/other.md`,
          `${root}/index.md`,
          `${root}/AGENTS.md`,
          original,
          '../escape.md',
        ]) {
          const event = {
            sessionID: 'ses_child',
            action: 'edit',
            resources: [resource],
            effect: 'allow',
          }
          await permission(event)
          assert.equal(event.effect, 'deny', resource)
        }
        const valid = {
          sessionID: 'ses_child',
          action: 'edit',
          resources: ['sources/webdav/a.md', 'overview.md'],
          effect: 'allow',
        }
        await permission(valid)
        assert.equal(valid.effect, 'allow')
        await hooks.compaction({ sessionID: 'ses_parent' })
        await assert.rejects(hooks.compaction({ sessionID: 'ses_child' }), /ingest_context_limit/)
      },
      wait: async () => {
        events.push('wait')
      },
      interrupt: async () => {
        events.push('stop')
      },
      context: async () => [{ type: 'assistant', content: [{ type: 'text', text: 'Report' }] }],
    },
  }
  const worker = await createBatchWorker(ctx, { admissionRoot })
  const standalone = {
    sessionID: 'ses_standalone',
    agent: 'wiki-ingest',
    action: 'read',
    resources: [root],
    effect: 'allow',
  }
  await permission(standalone)
  assert.equal(standalone.effect, 'deny')
  const result = await worker(
    { sessionID: 'ses_parent', signal: new AbortController().signal },
    {
      batch_id: 'synthetic',
      wiki_root: root,
      sources: [{ source_path: original, wiki_path: `${root}/sources/webdav/a.md` }],
    },
  )
  assert.equal(result.context_limit, true)
  assert.equal(result.content, 'Report')
  assert.deepEqual(events[0].model, { providerID: 'synthetic', id: 'fixture' })
  assert.deepEqual(events.slice(-2), ['stop', 'wait'])
  const expired = { ...standalone, sessionID: 'ses_child', effect: 'allow' }
  await permission(expired)
  assert.equal(expired.effect, 'deny')
  assert.deepEqual(await readdir(admissionRoot), [])
})
