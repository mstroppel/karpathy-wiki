// Runs in the isolated publisher image with only synthetic temporary state.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { controllerFence } from '/opt/karpathy-wiki/publisher/controller_fence.mjs'

const stateRoot = await mkdtemp('/tmp/controller-fence-')
const configuration = { stateRoot, bootId: '1'.repeat(32) }
const fence = controllerFence(configuration)
await fence.initialize(() => {})
const child = spawn(
  process.execPath,
  [
    '--input-type=module',
    '-e',
    `
  import { controllerFence } from '/opt/karpathy-wiki/publisher/controller_fence.mjs';
  const fence = controllerFence(JSON.parse(process.argv[1]));
  await fence.run(async () => {
    process.stdout.write('held');
    await new Promise(resolve => process.stdin.once('data', resolve));
  });
`,
    JSON.stringify(configuration),
  ],
  { stdio: ['pipe', 'pipe', 'pipe'] },
)
const exited = new Promise((resolve) => child.once('close', resolve))
try {
  await new Promise((resolve, reject) => {
    child.stdout.once('data', resolve)
    child.once('error', reject)
    child.once('exit', () => reject(new Error('fence fixture exited before locking')))
  })
  const replacement = controllerFence({ ...configuration, bootId: '2'.repeat(32) })
  await assert.rejects(
    replacement.initialize(() => {}),
    /Ingest-Lock/,
  )
  assert.equal(
    JSON.parse(await readFile(`${stateRoot}/controller.json`, 'utf8')).boot_id,
    configuration.bootId,
  )
  child.stdin.end('finish')
  assert.equal(await exited, 0)
  await replacement.initialize(() => {})
  await assert.rejects(
    fence.run(() => assert.fail('old execution ran')),
    /stale_controller/,
  )
  await assert.rejects(
    fence.initialize(() => {}),
    /stale_controller/,
  )
  console.log('controller-fence: cross-process execution exclusion and stale boot refusal OK')
} finally {
  child.kill()
  await exited
  await rm(stateRoot, { recursive: true, force: true })
}
