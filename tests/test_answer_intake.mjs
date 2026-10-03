import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import {
  scanIngestStatus,
  waitForPublishedSource,
} from '../config/tools/wiki_ingest_status_core.mjs'

test('publication wait retries until the submitted source appears', async () => {
  let scans = 0
  const published = { adapters: { answers: { new: [{ source_key: 'review-1.md' }] } } }
  const result = await waitForPublishedSource(
    async () => (++scans === 1 ? { adapters: {} } : published),
    { adapter: 'answers', sourceKey: 'review-1.md', waitSeconds: 2 },
  )
  assert.equal(result, published)
  assert.equal(scans, 2)
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
      ),
      status,
    )
    assert.equal(scans, 1)
  }
})

test('publication wait is bounded, validates inputs, and respects cancellation', async () => {
  const scan = async () => ({ adapters: {} })
  assert.deepEqual(
    await waitForPublishedSource(scan, {
      adapter: 'answers',
      sourceKey: 'missing.md',
      waitSeconds: 1,
    }),
    { adapters: {} },
  )
  for (const options of [{ waitSeconds: 121 }, { waitSeconds: 1 }, { waitSeconds: -1 }]) {
    await assert.rejects(waitForPublishedSource(scan, options))
  }
  const controller = new AbortController()
  const pending = waitForPublishedSource(scan, {
    adapter: 'answers',
    sourceKey: 'missing.md',
    waitSeconds: 2,
    signal: controller.signal,
  })
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
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
