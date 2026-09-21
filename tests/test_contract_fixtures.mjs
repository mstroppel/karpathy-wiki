import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'

import {
  REVISION_RE,
  field,
  frontmatter,
  parseRevokedList,
  rangeFor,
} from '../config/ingest-adapters/shared.mjs'
import { RESULT_NAMES } from '../config/tools/wiki_ingest_status_core.mjs'

// Conformance tests for the shared ingest status contract. The JSON fixtures
// in contracts/ingest-status/v1 are the same files the Python ingest packages
// run against (ingest/tests/test_contract.py).
const CONTRACT_DIR = path.resolve('contracts/ingest-status/v1')
const contract = JSON.parse(await readFile(path.join(CONTRACT_DIR, 'contract.json'), 'utf8'))

// Topics without a JavaScript implementation are listed here so a new fixture
// file cannot silently go untested; intervals are parsed by Python only.
const PYTHON_ONLY_TOPICS = new Set(['intervals'])

async function loadFixture(file) {
  return JSON.parse(await readFile(path.join(CONTRACT_DIR, 'fixtures', file), 'utf8'))
}

// Mirrors karpathy_wiki_ingest.contract.parse_frontmatter_fields: parse the
// block with the adapter frontmatter() helper, then resolve every field name
// through field(), which enforces uniqueness and value decoding.
function parseFields(text) {
  const lines = frontmatter(text)
  const names = new Set()
  for (const line of lines) {
    const separator = line.indexOf(':')
    if (separator <= 0) throw new Error('ungültige Frontmatter-Zeile')
    names.add(line.slice(0, separator))
  }
  const fields = {}
  for (const name of names) fields[name] = field(lines, name)
  return fields
}

test('contract version and status values match the implementation', () => {
  assert.equal(contract.contract, 'karpathy-wiki-ingest-status')
  assert.equal(contract.version, 1)
  assert.deepEqual([...RESULT_NAMES], contract.statusValues)
})

test('every fixture topic is covered by a runtime', async () => {
  const files = (await readdir(path.join(CONTRACT_DIR, 'fixtures'))).sort()
  assert.ok(files.length > 0)
  const covered = new Set(['revisions', 'id-ranges', 'revoked-lists', 'frontmatter-fields'])
  for (const file of files) {
    const document = await loadFixture(file)
    assert.ok(document.version, `${file} must be versioned`)
    if (!PYTHON_ONLY_TOPICS.has(document.topic)) {
      assert.ok(covered.has(document.topic), `${document.topic} must have a JavaScript test`)
    }
  }
})

test('revision cases', async () => {
  const { cases } = await loadFixture('revisions.json')
  for (const fixtureCase of cases) {
    assert.equal(REVISION_RE.test(fixtureCase.input), fixtureCase.valid, fixtureCase.name)
  }
})

test('paperless id range cases', async () => {
  const { cases } = await loadFixture('id-ranges.json')
  for (const fixtureCase of cases) {
    assert.equal(rangeFor(fixtureCase.id), fixtureCase.directory, fixtureCase.name)
  }
})

test('revocation list cases', async () => {
  const { cases } = await loadFixture('revoked-lists.json')
  for (const fixtureCase of cases) {
    if (fixtureCase.valid) {
      assert.deepEqual(parseRevokedList(fixtureCase.input), fixtureCase.ids, fixtureCase.name)
    } else {
      assert.throws(() => parseRevokedList(fixtureCase.input), undefined, fixtureCase.name)
    }
  }
})

test('frontmatter field cases', async () => {
  const { cases } = await loadFixture('frontmatter-fields.json')
  for (const fixtureCase of cases) {
    if (fixtureCase.valid) {
      const fields = parseFields(fixtureCase.input)
      for (const [name, value] of Object.entries(fixtureCase.expected)) {
        assert.deepEqual(fields[name], value, `${fixtureCase.name}: ${name}`)
      }
    } else {
      assert.throws(() => parseFields(fixtureCase.input), undefined, fixtureCase.name)
    }
  }
})
