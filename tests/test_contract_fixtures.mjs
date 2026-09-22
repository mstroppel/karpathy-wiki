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
// run against (ingest/tests/test_contract.py). Every topic test iterates all
// fixture files of its topic, so a newly added file cannot silently go
// untested; topics without a JavaScript implementation are listed here and
// are executed by the Python side only.
const CONTRACT_DIR = path.resolve('contracts/ingest-status/v1')
const contract = JSON.parse(await readFile(path.join(CONTRACT_DIR, 'contract.json'), 'utf8'))

const PYTHON_ONLY_TOPICS = new Set(['intervals'])
const JAVASCRIPT_TOPICS = ['revisions', 'id-ranges', 'revoked-lists', 'frontmatter-fields']

const fixtureFiles = (await readdir(path.join(CONTRACT_DIR, 'fixtures'))).sort()
const fixtures = new Map()
for (const file of fixtureFiles) {
  fixtures.set(file, JSON.parse(await readFile(path.join(CONTRACT_DIR, 'fixtures', file), 'utf8')))
}

function topicFiles(topic) {
  return [...fixtures].filter(([, document]) => document.topic === topic).map(([file]) => file)
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

test('every fixture topic is known and covered by a runtime', () => {
  assert.ok(fixtureFiles.length > 0)
  const known = new Set([...JAVASCRIPT_TOPICS, ...PYTHON_ONLY_TOPICS])
  for (const [file, document] of fixtures) {
    assert.ok(document.version, `${file} must be versioned`)
    assert.ok(known.has(document.topic), `${file}: unknown topic ${document.topic}`)
  }
  for (const topic of JAVASCRIPT_TOPICS) {
    assert.ok(topicFiles(topic).length > 0, `no JavaScript fixture for topic ${topic}`)
  }
})

test('revision cases', () => {
  const files = topicFiles('revisions')
  assert.ok(files.length > 0)
  for (const file of files) {
    const { cases } = fixtures.get(file)
    for (const fixtureCase of cases) {
      assert.equal(
        REVISION_RE.test(fixtureCase.input),
        fixtureCase.valid,
        `${file}: ${fixtureCase.name}`,
      )
    }
  }
})

test('paperless id range cases', () => {
  const pattern = new RegExp(contract.paperless.directoryPattern)
  const files = topicFiles('id-ranges')
  assert.ok(files.length > 0)
  for (const file of files) {
    const { cases } = fixtures.get(file)
    for (const fixtureCase of cases) {
      assert.equal(rangeFor(fixtureCase.id), fixtureCase.directory, `${file}: ${fixtureCase.name}`)
      assert.match(fixtureCase.directory, pattern, `${file}: ${fixtureCase.name}`)
    }
  }
})

test('revocation list cases', () => {
  const files = topicFiles('revoked-lists')
  assert.ok(files.length > 0)
  for (const file of files) {
    const { cases } = fixtures.get(file)
    for (const fixtureCase of cases) {
      if (fixtureCase.valid) {
        assert.deepEqual(
          parseRevokedList(fixtureCase.input),
          fixtureCase.ids,
          `${file}: ${fixtureCase.name}`,
        )
      } else {
        assert.throws(
          () => parseRevokedList(fixtureCase.input),
          undefined,
          `${file}: ${fixtureCase.name}`,
        )
      }
    }
  }
})

test('frontmatter field cases', () => {
  const files = topicFiles('frontmatter-fields')
  assert.ok(files.length > 0)
  for (const file of files) {
    const { cases } = fixtures.get(file)
    for (const fixtureCase of cases) {
      if (fixtureCase.valid) {
        const fields = parseFields(fixtureCase.input)
        for (const [name, value] of Object.entries(fixtureCase.expected)) {
          assert.deepEqual(fields[name], value, `${file}: ${fixtureCase.name}: ${name}`)
        }
      } else {
        assert.throws(
          () => parseFields(fixtureCase.input),
          undefined,
          `${file}: ${fixtureCase.name}`,
        )
      }
    }
  }
})
