import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'

import {
  MANIFEST_VERSION,
  REVISION_RE,
  RESULT_NAMES,
  checkRelativePath,
  parseFrontmatterFields,
  parseManifest,
} from '../config/tools/wiki_ingest_status_core.mjs'

// Conformance tests for the shared ingest contracts. The JSON fixtures under
// contracts/ are the same files the Python ingest packages run against
// (ingest/tests/test_contract.py and ingest/tests/test_manifest.py).
const ROOT = path.resolve('contracts')

const STATUS_CONTRACT = JSON.parse(
  await readFile(path.join(ROOT, 'ingest-status/v1/contract.json'), 'utf8'),
)
const MANIFEST_CONTRACT = JSON.parse(
  await readFile(path.join(ROOT, 'provider-manifest/v1/contract.json'), 'utf8'),
)

// Topics without a JavaScript implementation are listed here so a new fixture
// file cannot silently go untested: intervals, Paperless ID ranges, and
// revocation lists are implemented on the Python side only, because the
// scanner consumes provider manifests instead of re-deriving provider rules.
const PYTHON_ONLY_TOPICS = new Set(['intervals', 'id-ranges', 'revoked-lists'])

const FIXTURE_DIRECTORIES = ['ingest-status/v1/fixtures', 'provider-manifest/v1/fixtures']

async function loadFixture(relative) {
  return JSON.parse(await readFile(path.join(ROOT, relative), 'utf8'))
}

test('contract versions and status values match the implementation', () => {
  assert.equal(STATUS_CONTRACT.contract, 'karpathy-wiki-ingest-status')
  assert.equal(STATUS_CONTRACT.version, 1)
  assert.deepEqual([...RESULT_NAMES], STATUS_CONTRACT.statusValues)
  assert.equal(MANIFEST_CONTRACT.contract, 'karpathy-wiki-provider-manifest')
  assert.equal(MANIFEST_CONTRACT.version, MANIFEST_VERSION)
})

test('every fixture topic is covered by a runtime', async () => {
  const covered = new Set(['revisions', 'frontmatter-fields', 'provider-manifests'])
  const files = []
  for (const directory of FIXTURE_DIRECTORIES) {
    for (const file of await readdir(path.join(ROOT, directory))) files.push(`${directory}/${file}`)
  }
  assert.ok(files.length >= 6)
  for (const file of files) {
    const document = await loadFixture(file)
    assert.ok(document.version, `${file} must be versioned`)
    if (!PYTHON_ONLY_TOPICS.has(document.topic)) {
      assert.ok(covered.has(document.topic), `${document.topic} must have a JavaScript test`)
    }
  }
})

test('revision cases', async () => {
  const { cases } = await loadFixture('ingest-status/v1/fixtures/revisions.json')
  for (const fixtureCase of cases) {
    assert.equal(REVISION_RE.test(fixtureCase.input), fixtureCase.valid, fixtureCase.name)
  }
})

test('frontmatter field cases', async () => {
  const { cases } = await loadFixture('ingest-status/v1/fixtures/frontmatter-fields.json')
  for (const fixtureCase of cases) {
    if (fixtureCase.valid) {
      const fields = parseFrontmatterFields(fixtureCase.input)
      for (const [name, value] of Object.entries(fixtureCase.expected)) {
        assert.deepEqual(fields[name], value, `${fixtureCase.name}: ${name}`)
      }
    } else {
      assert.throws(() => parseFrontmatterFields(fixtureCase.input), undefined, fixtureCase.name)
    }
  }
})

test('provider manifest cases', async () => {
  const { cases } = await loadFixture('provider-manifest/v1/fixtures/manifests.json')
  for (const fixtureCase of cases) {
    if (fixtureCase.valid) {
      const parsed = parseManifest(fixtureCase.manifest.source, fixtureCase.manifest)
      assert.equal(parsed.source, fixtureCase.manifest.source)
      assert.equal(parsed.items.length, fixtureCase.manifest.items.length)
    } else {
      assert.throws(
        () => parseManifest(fixtureCase.manifest.source, fixtureCase.manifest),
        undefined,
        fixtureCase.name,
      )
    }
  }
})

test('relative path validation rejects escaping paths', () => {
  assert.equal(checkRelativePath('a/b.txt'), 'a/b.txt')
  assert.throws(() => checkRelativePath('../secrets/a.md'))
  assert.throws(() => checkRelativePath('/absolute'))
  assert.throws(() => checkRelativePath('a//b'))
})
