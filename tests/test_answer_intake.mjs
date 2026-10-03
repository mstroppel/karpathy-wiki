import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import {
  probePublishedSource,
  scanIngestStatus,
  waitForPublishedSource,
} from '../config/tools/wiki_ingest_status_core.mjs'

test('publication wait retries until the submitted source appears', async () => {
  let scans = 0
  let probes = 0
  const published = { adapters: { answers: { new: [{ source_key: 'review-1.md' }] } } }
  const result = await waitForPublishedSource(
    async () => {
      scans++
      return published
    },
    { adapter: 'answers', sourceKey: 'review-1.md', waitSeconds: 2 },
    async () => ++probes > 1,
  )
  assert.equal(result, published)
  assert.equal(probes, 2)
  assert.equal(scans, 1)
})

test('publication wait returns current and invalid results immediately', async () => {
  for (const states of [
    { current: [{ source_key: 'review-1.md' }] },
    { invalid: [{ error: 'invalid manifest' }] },
  ]) {
    let scans = 0
    const status = { adapters: { answers: states } }
    assert.equal(
      await waitForPublishedSource(
        async () => {
          scans++
          return status
        },
        {
          adapter: 'answers',
          sourceKey: 'review-1.md',
          waitSeconds: 2,
        },
        async () => true,
      ),
      status,
    )
    assert.equal(scans, 1)
  }
})

test('publication wait is bounded, validates inputs, and respects cancellation', async () => {
  const scan = async () => ({ adapters: {} })
  assert.deepEqual(
    await waitForPublishedSource(
      scan,
      {
        adapter: 'answers',
        sourceKey: 'missing.md',
        waitSeconds: 1,
      },
      async () => false,
    ),
    { adapters: {} },
  )
  for (const options of [{ waitSeconds: 121 }, { waitSeconds: 1 }, { waitSeconds: -1 }]) {
    await assert.rejects(waitForPublishedSource(scan, options))
  }
  const controller = new AbortController()
  const pending = waitForPublishedSource(
    scan,
    {
      adapter: 'answers',
      sourceKey: 'missing.md',
      waitSeconds: 2,
      signal: controller.signal,
    },
    async () => false,
  )
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
})

test('cancellation during a pending probe or full scan rejects matching results', async () => {
  for (const phase of ['probe', 'scan']) {
    const controller = new AbortController()
    let release
    const deferred = new Promise((resolve) => {
      release = resolve
    })
    let started
    const ready = new Promise((resolve) => {
      started = resolve
    })
    const pendingWork = async () => {
      started()
      await deferred
    }
    const pending = waitForPublishedSource(
      async () => {
        if (phase === 'scan') await pendingWork()
        return { adapters: { answers: { new: [{ source_key: 'review-1.md' }] } } }
      },
      { adapter: 'answers', sourceKey: 'review-1.md', waitSeconds: 2, signal: controller.signal },
      async () => {
        if (phase === 'probe') await pendingWork()
        return true
      },
    )
    await ready
    controller.abort()
    release()
    await assert.rejects(pending, { name: 'AbortError' })
  }
})

test('zero wait skips publication probes and performs one scan', async () => {
  let scans = 0
  await waitForPublishedSource(
    async () => {
      scans++
      return { adapters: {} }
    },
    {},
    async () => {
      assert.fail('ordinary status calls must not probe')
    },
  )
  assert.equal(scans, 1)
})

test('publication probe reads only the target manifest and exposes diagnostics', async () => {
  const sourceRoot = await mkdtemp(path.join(os.tmpdir(), 'kw-probe-'))
  try {
    const options = { sourceRoot, adapter: 'answers', sourceKey: 'review-1.md' }
    assert.equal(await probePublishedSource(options), false)
    await mkdir(path.join(sourceRoot, 'answers'))
    const filename = path.join(sourceRoot, 'answers', 'manifest.json')
    const manifest = {
      contract: 'karpathy-wiki-provider-manifest',
      version: 1,
      source: 'answers',
      generated_at: 1,
      items: [],
      revoked: [],
      errors: [],
      wiki_root: 'answers',
    }
    await writeFile(filename, JSON.stringify(manifest))
    assert.equal(await probePublishedSource(options), false)
    manifest.revoked = [{ source_key: 'review-1.md', claim: { source_path: 'review-1.md' } }]
    await writeFile(filename, JSON.stringify(manifest))
    assert.equal(await probePublishedSource(options), true)
    manifest.revoked = []
    manifest.errors = [{ error: 'provider failure' }]
    await writeFile(filename, JSON.stringify(manifest))
    assert.equal(await probePublishedSource(options), true)
    await writeFile(filename, '{')
    assert.equal(await probePublishedSource(options), true)
    await assert.rejects(probePublishedSource({ ...options, adapter: '../answers' }))
  } finally {
    await rm(sourceRoot, { recursive: true, force: true })
  }
})

test('confirmed answer travels through the provider manifest and wiki status', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'kw-answers-'))
  try {
    const inbox = path.join(root, 'incoming')
    const sourceRoot = path.join(root, 'sources')
    const wikiSourceRoot = path.join(root, 'wiki', 'sources')
    const redactions = path.join(root, 'redactions.json')
    await mkdir(inbox)
    await mkdir(wikiSourceRoot, { recursive: true })
    await writeFile(
      redactions,
      JSON.stringify({ people: [{ values: ['Ada Lovelace'], replacement: '[PERSON]' }] }),
    )
    const draft = path.join(inbox, 'review-1.md')
    const submit = async (text) => {
      await writeFile(draft, `${text}\n<!-- END CONFIRMED ANSWERS -->\n`)
      const result = spawnSync(
        'python3',
        [
          '-c',
          'from pathlib import Path; from karpathy_wiki_ingest.answers import publish; import sys; publish(*(Path(p) for p in sys.argv[1:]))',
          inbox,
          path.join(sourceRoot, 'answers'),
          redactions,
        ],
        { encoding: 'utf8' },
      )
      assert.equal(result.status, 0, result.stderr)
    }
    await submit('1. Ada Lovelace answered the question.')
    const scan = () => scanIngestStatus({ sourceRoot, wikiSourceRoot, includeCurrent: true })
    let status = await scan()
    assert.equal(status.summary.invalid, 0)
    assert.equal(status.summary.conflict, 0)
    const record = status.adapters.answers.new[0]
    assert.equal(
      await probePublishedSource({ sourceRoot, adapter: 'answers', sourceKey: 'review-1.md' }),
      true,
    )
    assert.equal(
      await probePublishedSource({ sourceRoot, adapter: 'answers', sourceKey: 'missing.md' }),
      false,
    )
    assert.equal(record.source_key, 'review-1.md')
    assert.equal(record.wiki_path, path.join(wikiSourceRoot, 'answers', 'review-1', 'index.md'))
    assert.ok((await readFile(record.source_path, 'utf8')).includes('[PERSON]'))
    const fields = Object.entries(record.frontmatter)
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
      .join('\n')
    await mkdir(path.dirname(record.wiki_path), { recursive: true })
    await writeFile(record.wiki_path, `---\n${fields}\n---\n\n# Answer source\n`)
    status = await scan()
    assert.equal(status.adapters.answers.current.length, 1)
    await submit('1. Ada Lovelace corrected the answer.')
    status = await scan()
    assert.equal(status.adapters.answers.outdated.length, 1)
    assert.equal(status.summary.invalid, 0)
    await assert.rejects(readFile(record.source_path, 'utf8'), { code: 'ENOENT' })
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
