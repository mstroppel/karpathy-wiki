import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { readFile, writeFile, stat } from 'node:fs/promises'
import fs from 'node:fs/promises'
import { syncBuiltinESMExports } from 'node:module'
import { promisify } from 'node:util'
import { publisherControl } from '/opt/karpathy-wiki/publisher/control.mjs'
import { IDENTITY, REPORT } from './publisher-fixture.mjs'

const cli = '/opt/karpathy-wiki/publisher/cli.mjs'
const runFile = promisify(execFile)
const git = async (...args) =>
  (await runFile('git', args, { cwd: '/knowledge/wiki' })).stdout.trim()
async function call(input, refused = false) {
  const child = execFile('node', [cli])
  const completed = new Promise((resolve, reject) => {
    let stdout = ''
    child.stdout.on('data', (bytes) => {
      stdout += bytes
    })
    child.once('error', reject)
    child.once('close', (code) => {
      try {
        assert.equal(code, refused ? 1 : 0)
        resolve(JSON.parse(stdout))
      } catch (error) {
        reject(error)
      }
    })
  })
  child.stdin.end(JSON.stringify(input))
  const result = await completed
  return result.result
}
const proofFile = '/knowledge/publisher/proof.json'
const propose = (job, request_id, proposal) =>
  call({ operation: 'propose', job_id: job.job_id, request_id, proposal })

if (process.argv[2] === 'start') {
  const baseline = await git('rev-parse', 'HEAD')
  await call({ operation: 'enqueue', request_id: 'req-auto', kind: 'automatic', source: IDENTITY })
  const job = await call({ operation: 'activate' })
  await propose(job, 'req-read', { operation: 'read_source' })
  await propose(job, 'req-stage', {
    operation: 'stage',
    draft: '# Finding\nEvidence at line 1. $(touch /tmp/must-not-execute)\n',
  })
  await propose(job, 'req-overview', { operation: 'stage', page: 'overview.md', reviewed: true })
  await call(
    {
      operation: 'propose',
      job_id: job.job_id,
      request_id: 'req-bypass',
      proposal: { operation: 'publish', root: '/tmp' },
    },
    true,
  )
  assert.equal(await git('rev-parse', 'HEAD'), baseline)
  assert.equal(await git('status', '--porcelain'), '')
  await writeFile(proofFile, JSON.stringify({ baseline, job }))
} else if (process.argv[2] === 'crash') {
  const { job } = JSON.parse(await readFile(proofFile, 'utf8'))
  const bootId = (await readFile('/tmp/publisher-boot-id', 'utf8')).trim()
  const originalRename = fs.rename
  fs.rename = async (...args) => {
    const result = await originalRename(...args)
    if (String(args[1]).includes('/preparations/') && String(args[1]).endsWith('.json')) {
      const receipt = JSON.parse(await readFile(args[1], 'utf8'))
      if (receipt.publication?.phase === 'installing') {
        await writeFile('/tmp/crash-boundary', 'ready\n')
        // The host restarts the entire container at this persisted intent.
        await new Promise(() => {
          setInterval(() => {}, 1000)
        })
      }
    }
    return result
  }
  syncBuiltinESMExports()
  await publisherControl(
    { operation: 'publish', request_id: 'req-publish', job_id: job.job_id, report: REPORT },
    { bootId },
  )
  throw new Error('crash injection did not stop publication')
} else if (process.argv[2] === 'recover') {
  const { job, baseline } = JSON.parse(await readFile(proofFile, 'utf8'))
  const stopped = await call({ operation: 'status' })
  assert.equal(stopped.inflight.operation, 'publish')
  await call({ operation: 'activate' }, true)
  const manual = await call({
    operation: 'enqueue',
    request_id: 'req-manual',
    kind: 'manual',
    source: IDENTITY,
  })
  await call({ operation: 'recover', confirmed: false }, true)
  const result = await call({ operation: 'recover', confirmed: true })
  assert.equal(result.status, 'ingested')
  assert.deepEqual(
    await call({
      operation: 'publish',
      request_id: 'req-publish',
      job_id: job.job_id,
      report: REPORT,
    }),
    result,
  )
  assert.equal(await git('rev-list', '--count', `${baseline}..HEAD`), '1')
  assert.equal(await git('status', '--porcelain'), '')
  const run = JSON.parse(
    await readFile(`/knowledge/incoming/ingest-journal/runs/${job.run_id}/run.json`, 'utf8'),
  )
  assert.equal(run.state, 'completed')
  const lines = (
    await readFile(`/knowledge/incoming/ingest-journal/runs/${job.run_id}/records.jsonl`, 'utf8')
  )
    .trim()
    .split('\n')
  assert.equal(lines.length, 1)
  assert.match(
    await readFile(`/knowledge/incoming/ingest-journal/${run.report.path}`, 'utf8'),
    /Finding at line 1/,
  )
  assert.equal((await call({ operation: 'activate' })).job_id, manual.job_id)
  await assert.rejects(stat('/tmp/must-not-execute'), { code: 'ENOENT' })
  console.log(
    'trusted-publisher: authorized commit/report, crash recovery, replay and manual handover OK',
  )
} else throw new Error('unknown probe mode')
