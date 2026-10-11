import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { authoringBusy, extractBlock, manualAuthoring } from '../publisher/authoring.mjs'
import { manualIngest } from '../publisher/manual_ingest.mjs'
import { publisherFixture } from './helpers/publisher-fixture.mjs'

// Deterministic renderer stand-in: the real render-analysis contract is
// covered by tests/test_render_analysis.py and the image integration run.
const RENDER_STUB =
  "const fs=require('fs');const p=process.argv[1];" +
  "fs.mkdirSync('assets/analyses',{recursive:true});" +
  "fs.writeFileSync(p.replace(/^analyses\\//,'assets/analyses/').replace(/\\.md$/,'.html')," +
  "'<!doctype html><title>'+fs.readFileSync(p,'utf8').split('\\n')[0].slice(2)+'</title>')"

const ANALYSIS_BLOCK = (target = 'beispiel-analyse') =>
  '```karpathy-authoring\n' +
  JSON.stringify({
    kind: 'analysis',
    target,
    content:
      '# Beispiel & Analyse\nErstellt: 2026-10-11\n\nEin **Befund** mit [[concepts/evidenz|Evidenz]].\n',
  }) +
  '\n```'

const ANSWER_BLOCK = (target = 'antwort-2026-10-11') =>
  '```karpathy-authoring\n' +
  JSON.stringify({
    kind: 'answers',
    target,
    content: '# Bestätigte Antworten\n\n- Frage 1: Antwort A.\n\n<!-- END CONFIRMED ANSWERS -->\n',
  }) +
  '\n```'

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')

function runtimeFixture(texts, notes = []) {
  return {
    session: async (id) =>
      id === 'ses_main'
        ? { location: { directory: '/knowledge/wiki' }, model: { providerID: 'fake', id: 'fake' } }
        : { location: { directory: '/tmp' }, model: { providerID: 'fake', id: 'fake' } },
    messages: async () =>
      texts.map((text) => ({ type: 'assistant', parts: [{ type: 'text', text }] })),
    note: async (...args) => notes.push(args),
  }
}

async function authoringFixtureTest(callback, options = {}) {
  const base = await mkdtemp(path.join(os.tmpdir(), 'trusted-authoring-'))
  try {
    const fixture = await publisherFixture(base)
    await writeFile(
      path.join(fixture.wikiRoot, 'index.md'),
      '# Synthetic Wiki\n\n## Überblick\n\n- [[overview|Überblick]]\n\n## Analysen\n',
    )
    await writeFile(path.join(fixture.wikiRoot, 'log.md'), '# Wiki-Protokoll\n')
    await fixture.git('add', '--', '.')
    await fixture.git('commit', '-qm', 'analysis baseline')
    const notes = []
    const configuration = {
      ...fixture,
      bootId: '1'.repeat(32),
      answersRoot: path.join(base, 'incoming/answers'),
      wikiUrl: options.wikiUrl === undefined ? 'https://wiki.invalid' : options.wikiUrl,
      renderCommand: [process.execPath, '-e', RENDER_STUB],
    }
    const authoring = manualAuthoring({
      configuration,
      runtime: options.runtime ?? runtimeFixture(options.texts ?? [], notes),
    })
    await authoring.initialize()
    await callback({ fixture, authoring, notes, base, configuration })
  } finally {
    await rm(base, { recursive: true, force: true })
  }
}

test('trusted analysis save writes page, print view, index and exactly one commit', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring, notes }) => {
      const accepted = await authoring.save('analysis', {
        session_id: 'ses_main',
        request_id: 'req-first',
      })
      assert.deepEqual(accepted, { accepted: true, request_id: 'req-first' })
      await authoring.wait()
      const state = await authoring.status()
      assert.equal(state.status, 'completed', JSON.stringify(state))
      assert.equal(state.result.kind, 'analysis')
      assert.equal(state.result.name, 'beispiel-analyse')
      assert.deepEqual(state.result.changed_pages, [
        'analyses/beispiel-analyse.md',
        'assets/analyses/beispiel-analyse.html',
        'index.md',
        'log.md',
      ])
      assert.equal(state.result.commit, (await fixture.git('rev-parse', 'HEAD')).trim())
      assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '3')
      assert.equal(
        (await fixture.git('log', '-1', '--pretty=%s')).trim(),
        'feat(analyses): Beispiel & Analyse speichern',
      )
      assert.match(
        await readFile(path.join(fixture.wikiRoot, 'analyses/beispiel-analyse.md'), 'utf8'),
        /# Beispiel & Analyse/,
      )
      assert.match(
        await readFile(
          path.join(fixture.wikiRoot, 'assets/analyses/beispiel-analyse.html'),
          'utf8',
        ),
        /<title>Beispiel & Analyse<\/title>/,
      )
      const index = await readFile(path.join(fixture.wikiRoot, 'index.md'), 'utf8')
      assert.match(index, /## Analysen\n- \[\[analyses\/beispiel-analyse\|Beispiel & Analyse\]\]/)
      const log = await readFile(path.join(fixture.wikiRoot, 'log.md'), 'utf8')
      assert.match(log, /- Analyse gespeichert: \d{4}-\d{2}-\d{2}T/)
      assert.equal(notes.length, 1)
      assert.equal(notes[0][2], true)
      assert.match(notes[0][1], /"commit":"[0-9a-f]{40}"/)
      // Durable controller state stays content-free; only the destination page
      // holds the analysis text.
      const persisted = await readFile(path.join(fixture.stateRoot, 'authoring.json'), 'utf8')
      assert.ok(!persisted.includes('Ein **Befund**'))
      assert.ok(!persisted.includes('Beispiel & Analyse'))
    },
    { texts: [ANALYSIS_BLOCK()] },
  )
})

test('repeated request admission is idempotent and never commits twice', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring }) => {
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-same' })
      await authoring.wait()
      const replay = await authoring.save('analysis', {
        session_id: 'ses_main',
        request_id: 'req-same',
      })
      assert.equal(replay.accepted, true)
      assert.equal(replay.commit, (await fixture.git('rev-parse', 'HEAD')).trim())
      assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '3')
    },
    { texts: [ANALYSIS_BLOCK()] },
  )
})

test('updating an analysis refreshes its index title without duplicating the entry', async () => {
  const texts = [ANALYSIS_BLOCK()]
  await authoringFixtureTest(
    async ({ fixture, authoring }) => {
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-original' })
      await authoring.wait()
      texts[0] = ANALYSIS_BLOCK().replace('Beispiel & Analyse', 'Neuer Titel')
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-title' })
      await authoring.wait()
      const state = await authoring.status()
      assert.equal(state.status, 'completed')
      assert.ok(state.result.changed_pages.includes('index.md'))
      const index = await readFile(path.join(fixture.wikiRoot, 'index.md'), 'utf8')
      assert.match(index, /- \[\[analyses\/beispiel-analyse\|Neuer Titel\]\]/)
      assert.equal(index.match(/\[\[analyses\/beispiel-analyse\|/g).length, 1)
      assert.ok(!index.includes('Beispiel & Analyse'))
      // A same-title update must neither duplicate nor rewrite the index.
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-same-title' })
      await authoring.wait()
      assert.equal((await authoring.status()).status, 'completed')
      assert.ok(!(await authoring.status()).result.changed_pages.includes('index.md'))
      assert.equal(await readFile(path.join(fixture.wikiRoot, 'index.md'), 'utf8'), index)
      assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '5')
    },
    { texts },
  )
})

test('analysis index title updates preserve replacement-string tokens literally', async () => {
  const texts = [ANALYSIS_BLOCK()]
  await authoringFixtureTest(
    async ({ fixture, authoring }) => {
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-title-baseline' })
      await authoring.wait()
      const title = "Kosten $& $$ $` $' $1 Nutzen"
      texts[0] = ANALYSIS_BLOCK().replace('Beispiel & Analyse', () => title)
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-dollar-title' })
      await authoring.wait()
      assert.equal((await authoring.status()).status, 'completed')
      const index = await readFile(path.join(fixture.wikiRoot, 'index.md'), 'utf8')
      assert.ok(index.includes(`- [[analyses/beispiel-analyse|${title}]]`))
      assert.equal(index.match(/\[\[analyses\/beispiel-analyse\|/g).length, 1)
    },
    { texts },
  )
})

test('trusted answer save writes only the private inbox file and reports it unpublished', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring, notes, base }) => {
      await authoring.save('answers', { session_id: 'ses_main', request_id: 'req-answer' })
      await authoring.wait()
      const state = await authoring.status()
      assert.equal(state.status, 'completed', JSON.stringify(state))
      assert.equal(state.result.kind, 'answers')
      assert.equal(state.result.commit, null)
      const file = path.join(base, 'incoming/answers/antwort-2026-10-11.md')
      assert.equal(state.result.path, file)
      assert.match(await readFile(file, 'utf8'), /<!-- END CONFIRMED ANSWERS -->/)
      assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '2')
      assert.match(notes[0][1], /"published":false/)
    },
    { texts: [ANSWER_BLOCK()] },
  )
})

test('missing or invalid prepared blocks are refused without durable state', async () => {
  const cases = [
    ['Keine Vorbereitung.', 'analysis', 'prepared_block_missing'],
    ['```karpathy-authoring\n{not json}\n```', 'analysis', 'invalid_prepared_block'],
    [
      '```karpathy-authoring\n' +
        JSON.stringify({ kind: 'other', target: 'x', content: '# T\nErstellt: 2026-10-11\n' }) +
        '\n```',
      'analysis',
      'invalid_prepared_block',
    ],
    [
      '```karpathy-authoring\n' +
        JSON.stringify({
          kind: 'analysis',
          target: 'beispiel',
          content:
            '# Titel\nErstellt: 2026-10-11\n\n[showing 1 of 10; full output saved to /tmp/x]',
        }) +
        '\n```',
      'analysis',
      'invalid_prepared_block',
    ],
    [
      '```karpathy-authoring\n' +
        JSON.stringify({
          kind: 'analysis',
          target: 'beispiel',
          content: '# Titel [mit Klammern]\nErstellt: 2026-10-11\n\nText.\n',
        }) +
        '\n```',
      'analysis',
      'invalid_prepared_block',
    ],
    [
      '```karpathy-authoring\n' +
        JSON.stringify({
          kind: 'analysis',
          target: 'beispiel',
          content: '# Titel ohne Datumszeile\n\nText.\n',
        }) +
        '\n```',
      'analysis',
      'invalid_prepared_block',
    ],
    [
      '```karpathy-authoring\n' +
        JSON.stringify({
          kind: 'answers',
          target: 'antwort',
          content: '# Antworten ohne Abschluss\n',
        }) +
        '\n```',
      'answers',
      'invalid_prepared_block',
    ],
  ]
  for (const [text, kind, expected] of cases) {
    await authoringFixtureTest(
      async ({ authoring }) => {
        await assert.rejects(
          authoring.save(kind, { session_id: 'ses_main', request_id: 'req-bad' }),
          new Error(expected),
        )
        assert.equal((await authoring.status()).status, 'idle')
      },
      { texts: [text] },
    )
  }
})

test('wrong session location and occupied answer names are refused without stops', async () => {
  await authoringFixtureTest(
    async ({ authoring, base }) => {
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_other', request_id: 'req-session' }),
        new Error('invalid_operator_session'),
      )
      assert.equal((await authoring.status()).status, 'idle')
      await mkdir(path.join(base, 'incoming/answers'), { recursive: true })
      await writeFile(
        path.join(base, 'incoming/answers/antwort-2026-10-11.md'),
        'fremder Entwurf\n',
      )
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_main', request_id: 'req-occupied' }),
        new Error('answers_file_exists'),
      )
      assert.equal((await authoring.status()).status, 'idle')
      assert.equal(
        await readFile(path.join(base, 'incoming/answers/antwort-2026-10-11.md'), 'utf8'),
        'fremder Entwurf\n',
      )
    },
    { texts: [ANSWER_BLOCK()] },
  )
})

test('analysis saves require the configured public wiki URL', async () => {
  await authoringFixtureTest(
    async ({ authoring }) => {
      await assert.rejects(
        authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-url' }),
        new Error('wiki_public_url_missing'),
      )
      assert.equal((await authoring.status()).status, 'idle')
    },
    { texts: [ANALYSIS_BLOCK()], wikiUrl: null },
  )
})

test('authoring and ingest admission are mutually exclusive and fail closed', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring, configuration }) => {
      assert.equal(await authoringBusy(fixture.stateRoot), false)
      const driver = manualIngest({ configuration, runtime: runtimeFixture([]) })
      await driver.initialize()
      const idle = await driver.status()
      await writeFile(
        path.join(fixture.stateRoot, 'manual.json'),
        JSON.stringify({
          ...idle,
          status: 'running',
          session_id: 'ses_main',
          request_id: 'req-run',
        }),
      )
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_main', request_id: 'req-busy' }),
        new Error('busy'),
      )
      await writeFile(path.join(fixture.stateRoot, 'manual.json'), JSON.stringify(idle))
      await writeFile(
        path.join(fixture.stateRoot, 'authoring.json'),
        JSON.stringify({ version: 1, status: 'writing' }),
      )
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      assert.equal(configuration.stateRoot, fixture.stateRoot)
      await writeFile(path.join(fixture.stateRoot, 'authoring.json'), '{corrupt')
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      await writeFile(
        path.join(fixture.stateRoot, 'authoring.json'),
        JSON.stringify({ extra: true }),
      )
      assert.equal(await authoringBusy(fixture.stateRoot), true)
    },
    { texts: [ANSWER_BLOCK()] },
  )
})

test('authoring validates all manual evidence before treating idle or completed as quiet', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring, configuration }) => {
      const driver = manualIngest({ configuration, runtime: runtimeFixture([]) })
      await driver.initialize()
      const idle = await driver.status()
      const file = path.join(fixture.stateRoot, 'manual.json')
      const before = await readFile(path.join(fixture.stateRoot, 'authoring.json'), 'utf8')
      const completed = {
        ...idle,
        status: 'completed',
        session_id: 'ses_main',
        request_id: 'req-completed',
      }
      for (const corrupt of [
        null,
        { version: 1, status: 'idle' },
        { ...idle, selected: [null] },
        { ...idle, blocker: 'summary_admission_uncertain' },
        { ...completed, active: { intent: 'publish' } },
        { ...completed, results: [{}] },
        { ...completed, unknown: 'untrusted' },
      ]) {
        const written = JSON.stringify(corrupt)
        await writeFile(file, written)
        await assert.rejects(
          authoring.save('answers', { session_id: 'ses_main', request_id: 'req-invalid-manual' }),
          /invalid_controller_state/,
        )
        assert.equal(await readFile(file, 'utf8'), written)
        assert.equal(await readFile(path.join(fixture.stateRoot, 'authoring.json'), 'utf8'), before)
      }
      await writeFile(file, JSON.stringify({ ...completed, status_delivery_failed: true }))
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_main', request_id: 'req-pending-summary' }),
        /busy/,
      )
      // Fully validated quiet states remain admissible.
      for (const [state, target] of [
        [idle, 'idle'],
        [completed, 'completed'],
      ]) {
        await writeFile(file, JSON.stringify(state))
        const saver = manualAuthoring({
          configuration,
          runtime: runtimeFixture([ANSWER_BLOCK(target)]),
        })
        await saver.save('answers', { session_id: 'ses_main', request_id: `req-${target}` })
        await saver.wait()
        assert.equal((await saver.status()).status, 'completed')
      }
    },
    { texts: [ANSWER_BLOCK()] },
  )
})

test('recovered saves retain ownership until their summary is successfully delivered', async () => {
  for (const kind of ['analysis', 'answers']) {
    const notes = []
    let broken = false
    let saver
    const runtime = runtimeFixture([kind === 'analysis' ? ANALYSIS_BLOCK() : ANSWER_BLOCK()], notes)
    runtime.note = async (...args) => {
      if (broken) {
        assert.equal((await saver.status()).status, 'operator_action_required')
        throw new Error('runtime_request_failed')
      }
      notes.push(args)
    }
    await authoringFixtureTest(
      async ({ fixture, authoring, configuration }) => {
        saver = authoring
        const driver = manualIngest({ configuration, runtime })
        await driver.initialize()
        await saver.save(kind, { session_id: 'ses_main', request_id: 'req-landed' })
        await saver.wait()
        const landed = await saver.status()
        assert.equal(landed.status, 'completed')
        const file = path.join(fixture.stateRoot, 'authoring.json')
        await writeFile(
          file,
          JSON.stringify({
            ...landed,
            status: 'operator_action_required',
            blocker: 'interrupted_authoring',
            result: null,
          }),
        )
        broken = true
        await assert.rejects(saver.resume({ confirmed: true }), /runtime_request_failed/)
        const stopped = await saver.status()
        assert.equal(stopped.status, 'operator_action_required')
        assert.equal(stopped.blocker, 'summary_admission_uncertain')
        assert.equal(stopped.status_delivery_failed, true)
        assert.deepEqual(stopped.result, landed.result)
        assert.equal(await authoringBusy(fixture.stateRoot), true)
        await assert.rejects(
          saver.save(kind, { session_id: 'ses_main', request_id: 'req-overwrite' }),
          /operator_action_required_or_busy/,
        )
        await assert.rejects(
          driver.submit({ session_id: 'ses_main', request_id: 'req-ingest' }),
          /busy/,
        )
        // A further failed explicit resume still preserves the exact evidence.
        await assert.rejects(saver.resume({ confirmed: true }), /runtime_request_failed/)
        assert.deepEqual(await saver.status(), stopped)
        broken = false
        const resumed = await saver.resume({ confirmed: true })
        assert.equal(resumed.status, 'completed')
        assert.deepEqual((await saver.status()).result, landed.result)
        assert.equal((await saver.status()).status_delivery_failed, false)
        assert.equal(await authoringBusy(fixture.stateRoot), false)
        assert.equal(notes.length, 2)
        assert.equal(
          await fixture.git('rev-list', '--count', 'HEAD'),
          kind === 'analysis' ? '3' : '2',
        )
      },
      { runtime },
    )
  }
})

test('summary recovery revalidates destination contents and paths before releasing ownership', async () => {
  for (const kind of ['analysis', 'answers']) {
    const runtime = runtimeFixture([kind === 'analysis' ? ANALYSIS_BLOCK() : ANSWER_BLOCK()])
    let calls = 0
    runtime.note = async () => {
      calls++
      if (calls === 1) throw new Error('lost acknowledgement')
    }
    await authoringFixtureTest(
      async ({ fixture, authoring, base }) => {
        await authoring.save(kind, { session_id: 'ses_main', request_id: 'req-summary-verify' })
        await authoring.wait()
        const stopped = await authoring.status()
        assert.equal(stopped.blocker, 'summary_admission_uncertain')
        const file = stopped.result.path
        const original = await readFile(file)
        await writeFile(file, 'foreign bytes\n')
        await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
        assert.deepEqual(await authoring.status(), stopped)
        assert.equal(calls, 1, 'unverified contents must not be redelivered')
        await rm(file)
        await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
        const outside = path.join(base, 'external-summary-target')
        await writeFile(outside, original)
        await symlink(outside, file)
        await assert.rejects(authoring.resume({ confirmed: true }))
        assert.deepEqual(await authoring.status(), stopped)
        assert.equal(calls, 1)
        await rm(file)
        await writeFile(file, original)
        if (kind === 'analysis') {
          const print = path.join(fixture.wikiRoot, 'assets/analyses/beispiel-analyse.html')
          const rendered = await readFile(print)
          await writeFile(print, 'foreign print\n')
          await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
          await writeFile(print, rendered)
          const overview = path.join(fixture.wikiRoot, 'overview.md')
          const previous = await readFile(overview)
          await writeFile(overview, 'foreign overview\n')
          await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
          await writeFile(overview, previous)
        }
        const resumed = await authoring.resume({ confirmed: true })
        assert.equal(resumed.status, 'completed')
        assert.equal(calls, 2)
        assert.equal(await authoringBusy(fixture.stateRoot), false)
      },
      { runtime },
    )
  }
})

test('analysis recovery clears only when target paths match the pre-save baseline', async () => {
  for (const existing of [false, true]) {
    await authoringFixtureTest(async ({ fixture, authoring }) => {
      const page = path.join(fixture.wikiRoot, 'analyses/beispiel-analyse.md')
      const print = path.join(fixture.wikiRoot, 'assets/analyses/beispiel-analyse.html')
      await mkdir(path.dirname(page), { recursive: true })
      await mkdir(path.dirname(print), { recursive: true })
      if (existing) {
        await writeFile(page, 'previous analysis\n')
        await writeFile(print, 'previous print\n')
        await fixture.git('add', '--', '.')
        await fixture.git('commit', '-qm', 'existing targets')
      }
      const state = {
        ...(await authoring.status()),
        status: 'operator_action_required',
        blocker: 'interrupted_authoring',
        kind: 'analysis',
        session_id: 'ses_main',
        request_id: 'req-baseline',
        target: 'beispiel-analyse',
        digest: 'a'.repeat(64),
        rendered: 'b'.repeat(64),
        base: (await fixture.git('rev-parse', 'HEAD')).trim(),
      }
      await writeFile(path.join(fixture.stateRoot, 'authoring.json'), JSON.stringify(state))
      if (!existing) {
        const exclude = path.join(fixture.wikiRoot, '.git/info/exclude')
        const previous = await readFile(exclude)
        await writeFile(exclude, `${previous}\n/analyses/beispiel-analyse.md\n`)
        await writeFile(page, 'ignored foreign target\n')
        assert.equal(await fixture.git('status', '--porcelain'), '')
        await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
        assert.deepEqual(await authoring.status(), state)
        await writeFile(exclude, previous)
      }
      await writeFile(page, 'foreign committed partial analysis\n')
      await fixture.git('add', '--', '.')
      await fixture.git('commit', '-qm', 'foreign target content')
      await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
      assert.deepEqual(await authoring.status(), state)
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      // Explicit operator restoration of the original targets proves no
      // retained write needs adopting; unrelated committed work is preserved.
      if (existing) await writeFile(page, 'previous analysis\n')
      else await rm(page)
      await fixture.git('add', '--', '.')
      await fixture.git('commit', '-qm', 'operator restores target baseline')
      assert.deepEqual(await authoring.resume({ confirmed: true }), {
        verified: true,
        status: 'idle',
        request_id: null,
      })
    })
  }
})

test('interrupted saves keep a persistent stop and verified resume adopts the landed commit', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring, configuration }) => {
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-crash' })
      await authoring.wait()
      const landed = await authoring.status()
      assert.equal(landed.status, 'completed')
      // Reconstruct the crash window between the verified commit and the
      // recorded result: content digests alone must identify the commit.
      const page = await readFile(path.join(fixture.wikiRoot, 'analyses/beispiel-analyse.md'))
      const print = await readFile(
        path.join(fixture.wikiRoot, 'assets/analyses/beispiel-analyse.html'),
      )
      await writeFile(
        path.join(fixture.stateRoot, 'authoring.json'),
        JSON.stringify({
          version: 1,
          status: 'operator_action_required',
          session_id: 'ses_main',
          request_id: 'req-crash',
          kind: 'analysis',
          target: 'beispiel-analyse',
          digest: digest(page),
          rendered: digest(print),
          base: (await fixture.git('rev-parse', 'HEAD~1')).trim(),
          phase: null,
          blocker: 'interrupted_authoring',
          result: null,
          status_delivery_failed: false,
        }),
      )
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      await assert.rejects(
        authoring.resume({ confirmed: false }),
        new Error('confirmation_required'),
      )
      const resumed = await authoring.resume({ confirmed: true })
      assert.equal(resumed.status, 'completed')
      assert.equal(resumed.commit, landed.result.commit)
      assert.equal((await authoring.status()).status, 'completed')
      assert.equal(await authoringBusy(fixture.stateRoot), false)
      assert.equal(await fixture.git('rev-list', '--count', 'HEAD'), '3')
      assert.equal(configuration.wikiRoot, fixture.wikiRoot)
    },
    { texts: [ANALYSIS_BLOCK()] },
  )
})

test('a dirty wiki after an interrupted save stops for operator action until repaired', async () => {
  await authoringFixtureTest(async ({ fixture, configuration }) => {
    await writeFile(
      path.join(fixture.stateRoot, 'authoring.json'),
      JSON.stringify({
        version: 1,
        status: 'writing',
        session_id: 'ses_main',
        request_id: 'req-partial',
        kind: 'analysis',
        target: 'beispiel-analyse',
        digest: 'a'.repeat(64),
        rendered: 'b'.repeat(64),
        base: (await fixture.git('rev-parse', 'HEAD')).trim(),
        phase: 'writing',
        blocker: null,
        result: null,
        status_delivery_failed: false,
      }),
    )
    const restarted = manualAuthoring({ configuration, runtime: runtimeFixture([]) })
    await restarted.initialize()
    await mkdir(path.join(fixture.wikiRoot, 'analyses'), { recursive: true })
    await writeFile(path.join(fixture.wikiRoot, 'analyses/beispiel-analyse.md'), 'halber Entwurf\n')
    await assert.rejects(
      restarted.resume({ confirmed: true }),
      new Error('manual_maintenance_required'),
    )
    // Explicit operator repair of the interrupted attempt: nothing of it
    // remains, so verified resume clears the stop without inventing a result.
    await fixture.git('checkout', '--', '.')
    await rm(path.join(fixture.wikiRoot, 'analyses'), { recursive: true, force: true })
    const resumed = await restarted.resume({ confirmed: true })
    assert.deepEqual(resumed, { verified: true, status: 'idle', request_id: null })
    assert.equal((await restarted.status()).status, 'idle')
  })
})

test('lost summary delivery is redelivered only by explicit verified resume', async () => {
  const notes = []
  let broken = true
  const runtime = runtimeFixture([ANSWER_BLOCK()], notes)
  runtime.note = async () => {
    if (broken) throw new Error('runtime_request_failed')
  }
  await authoringFixtureTest(
    async ({ fixture, authoring }) => {
      await authoring.save('answers', { session_id: 'ses_main', request_id: 'req-note' })
      await authoring.wait()
      const state = await authoring.status()
      assert.equal(state.status, 'operator_action_required')
      assert.equal(state.blocker, 'summary_admission_uncertain')
      assert.equal(state.status_delivery_failed, true)
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      // A completed snapshot with unacknowledged delivery is not available,
      // even before startup normalization or on an idempotent request replay.
      await writeFile(
        path.join(fixture.stateRoot, 'authoring.json'),
        JSON.stringify({ ...state, status: 'completed', blocker: null }),
      )
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_main', request_id: 'req-note' }),
        /busy/,
      )
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_main', request_id: 'req-new-note' }),
        /operator_action_required_or_busy/,
      )
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      broken = false
      runtime.note = async (...args) => notes.push(args)
      const resumed = await authoring.resume({ confirmed: true })
      assert.equal(resumed.status, 'completed')
      assert.equal(notes.length, 1)
      assert.equal((await authoring.status()).status, 'completed')
    },
    { texts: [ANSWER_BLOCK()], runtime },
  )
})

test('restart turns an interrupted write into a persistent operator stop', async () => {
  await authoringFixtureTest(async ({ fixture, configuration }) => {
    await writeFile(
      path.join(fixture.stateRoot, 'authoring.json'),
      JSON.stringify({
        version: 1,
        status: 'writing',
        session_id: 'ses_main',
        request_id: 'req-restart',
        kind: 'answers',
        target: 'antwort-2026-10-11',
        digest: 'a'.repeat(64),
        rendered: null,
        base: (await fixture.git('rev-parse', 'HEAD')).trim(),
        phase: 'writing',
        blocker: null,
        result: null,
        status_delivery_failed: false,
      }),
    )
    const restarted = manualAuthoring({ configuration, runtime: runtimeFixture([]) })
    await restarted.initialize()
    const state = await restarted.status()
    assert.equal(state.status, 'operator_action_required')
    assert.equal(state.blocker, 'interrupted_authoring')
  })
})

test('extractBlock keeps only the newest prepared block and nothing else', () => {
  // The runtime lists messages newest first (cursor order `desc`).
  const messages = [
    {
      type: 'assistant',
      content: [
        {
          type: 'text',
          text: `Zwei Blöcke:\n${ANALYSIS_BLOCK('erste')}\n${ANALYSIS_BLOCK('zweite')}`,
        },
      ],
    },
    { type: 'assistant', parts: [{ type: 'text', text: 'Erst ' + ANALYSIS_BLOCK('erste') }] },
    { type: 'user', parts: [{ type: 'text', text: ANSWER_BLOCK('alt') }] },
  ]
  const block = extractBlock(messages, 'analysis')
  assert.equal(block.target, 'zweite')
  assert.equal(block.title, 'Beispiel & Analyse')
  assert.equal(extractBlock([{ type: 'assistant', text: 'ohne Block' }], 'analysis'), null)
  assert.equal(extractBlock(messages, 'answers'), null)
})

test('malformed newest protocol fences never fall back to an older prepared block', () => {
  const multiline =
    '```karpathy-authoring\n' +
    JSON.stringify(
      {
        kind: 'analysis',
        target: 'latest-preview',
        content: '# Latest\nErstellt: 2026-10-11\n',
      },
      null,
      2,
    ) +
    '\n```'
  for (const malformed of [
    multiline,
    '```karpathy-authoring\n{"kind":"analysis"}',
    '```karpathy-authoring',
    '```karpathy-authoring extra\n{}\n```',
    '```karpathy-authoring\n\n{}\n```',
  ]) {
    assert.throws(
      () =>
        extractBlock(
          [
            { type: 'assistant', text: malformed },
            { type: 'assistant', text: ANALYSIS_BLOCK('old-preview') },
          ],
          'analysis',
        ),
      /invalid_prepared_block/,
    )
    assert.throws(
      () =>
        extractBlock(
          [{ type: 'assistant', text: ANALYSIS_BLOCK('old-preview') + '\n' + malformed }],
          'analysis',
        ),
      /invalid_prepared_block/,
    )
  }
})

import { manualIngestMiddleware } from '../openchamber/manual-ingest.mjs'

function routeRequest(middleware, request) {
  const outcome = {}
  const res = {
    status(code) {
      outcome.status = code
      return this
    },
    json(body) {
      outcome.body = body
      return this
    },
  }
  return Promise.resolve(
    middleware(request, res, () => {
      outcome.next = true
    }),
  ).then(() => outcome)
}

test('UI middleware admits only explicit same-origin authoring commands', async () => {
  const calls = []
  const middleware = manualIngestMiddleware({
    token: '1'.repeat(64),
    origin: 'http://127.0.0.1:3000',
    url: 'http://manual-ingest:4080',
    parseBody: (_req, _res, next) => next(),
    fetcher: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) })
      return Response.json({ data: { accepted: true } })
    },
  })
  const base = {
    method: 'POST',
    path: '/api/session/ses_main/command',
    headers: { origin: 'http://127.0.0.1:3000' },
  }
  const admitted = await routeRequest(middleware, {
    ...base,
    body: { name: 'analysis-save', text: '' },
  })
  assert.equal(admitted.status, 200)
  assert.equal(calls[0].url, 'http://manual-ingest:4080/analysis-save')
  assert.match(calls[0].body.request_id, /^req-[0-9a-f]{32}$/)
  assert.equal(calls[0].body.session_id, 'ses_main')
  await routeRequest(middleware, { ...base, body: { name: 'answer-save', text: 'resume' } })
  assert.equal(calls[1].url, 'http://manual-ingest:4080/authoring-resume')
  assert.deepEqual(calls[1].body, { confirmed: true })
  await routeRequest(middleware, { ...base, body: { name: 'ingest-new', text: 'resume' } })
  assert.equal(calls[2].url, 'http://manual-ingest:4080/resume')
  for (const request of [
    { ...base, body: { name: 'analysis-save', text: 'bitte speichern' } },
    { ...base, body: { name: 'analysis-save', text: '   ' } },
    { ...base, body: { name: 'answer-save', text: '\t\n' } },
    { ...base, body: { name: 'answer-save', text: '', files: ['x.md'] } },
    {
      ...base,
      headers: { origin: 'https://untrusted.invalid' },
      body: { name: 'analysis-save', text: '' },
    },
  ]) {
    const refused = await routeRequest(middleware, request)
    assert.equal(refused.status, 403)
    assert.equal(refused.body.error, 'explicit_same_origin_authoring_required')
  }
  const ingest = await routeRequest(middleware, {
    ...base,
    body: { name: 'ingest-new', text: 'mit Argumenten' },
  })
  assert.equal(ingest.body.error, 'explicit_same_origin_ingest_required')
  // ingest-new keeps its documented trim rule for whitespace-only text.
  await routeRequest(middleware, { ...base, body: { name: 'ingest-new', text: '   ' } })
  assert.equal(calls[3].url, 'http://manual-ingest:4080/ingest-new')
  assert.equal(calls.length, 4)
  const unrelated = await routeRequest(middleware, {
    ...base,
    body: { name: 'gap-review', text: '' },
  })
  assert.equal(unrelated.next, true)
  assert.equal(calls.length, 4)
})

test('symlinked write targets cannot redirect a save out of their tree', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring, base }) => {
      const outside = path.join(base, 'outside')
      await mkdir(outside, { recursive: true })
      await rm(path.join(fixture.wikiRoot, 'analyses'), { recursive: true, force: true })
      await symlink(outside, path.join(fixture.wikiRoot, 'analyses'))
      await fixture.git('add', '--', 'analyses')
      await fixture.git('commit', '-qm', 'tracked analyses symlink')
      await assert.rejects(
        authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-symlink' }),
        new Error('unsafe_write_path'),
      )
      assert.equal((await authoring.status()).status, 'idle')
      assert.deepEqual(await readdir(outside), [])
      // A symlinked answer draft redirects nothing either.
      await mkdir(path.join(base, 'incoming/answers'), { recursive: true })
      await writeFile(path.join(outside, 'fremder-entwurf.md'), 'fremd\n')
      await symlink(
        path.join(outside, 'fremder-entwurf.md'),
        path.join(base, 'incoming/answers/antwort-2026-10-11.md'),
      )
      await assert.rejects(
        authoring.save('answers', { session_id: 'ses_main', request_id: 'req-symlink-answers' }),
        new Error('unsafe_write_path'),
      )
      assert.equal((await authoring.status()).status, 'idle')
      assert.equal(await readFile(path.join(outside, 'fremder-entwurf.md'), 'utf8'), 'fremd\n')
    },
    { texts: [ANALYSIS_BLOCK(), ANSWER_BLOCK()] },
  )
})

test('symlinked inbox ancestors are refused before creating any directories', async () => {
  for (const ancestor of ['incoming', 'incoming/answers']) {
    await authoringFixtureTest(
      async ({ configuration, base }) => {
        const outside = path.join(base, 'outside')
        await mkdir(outside)
        const isolated = path.join(base, 'isolated')
        await mkdir(isolated)
        if (ancestor === 'incoming/answers') await mkdir(path.join(isolated, 'incoming'))
        await symlink(outside, path.join(isolated, ancestor))
        const authoring = manualAuthoring({
          configuration: { ...configuration, answersRoot: path.join(isolated, 'incoming/answers') },
          runtime: runtimeFixture([ANSWER_BLOCK()]),
        })
        await assert.rejects(
          authoring.save('answers', { session_id: 'ses_main', request_id: 'req-inbox-parent' }),
          new Error('unsafe_write_path'),
        )
        assert.deepEqual(await readdir(outside), [])
        assert.equal((await authoring.status()).status, 'idle')
      },
      { texts: [ANSWER_BLOCK()] },
    )
  }
})

test('verified resume retains the stop for foreign contents above an old landed commit', async () => {
  await authoringFixtureTest(
    async ({ fixture, authoring }) => {
      await authoring.save('analysis', { session_id: 'ses_main', request_id: 'req-old' })
      await authoring.wait()
      const landed = await authoring.status()
      assert.equal(landed.status, 'completed')
      const mine = landed.result.commit
      const base_commit = (await fixture.git('rev-parse', `${mine}~1`)).trim()
      // More than the previous 100-commit scan window of foreign commits that
      // touch the same page must not hide the landed commit from recovery.
      const page = path.join(fixture.wikiRoot, 'analyses/beispiel-analyse.md')
      const trees = []
      for (const content of ['fremd A\n', 'fremd B\n']) {
        await writeFile(page, content)
        await fixture.git('add', '--', 'analyses/beispiel-analyse.md')
        trees.push((await fixture.git('write-tree')).trim())
      }
      let head = (await fixture.git('rev-parse', 'HEAD')).trim()
      for (let index = 0; index < 106; index++) {
        const commit = (
          await fixture.git(
            '-c',
            'commit.gpgsign=false',
            'commit-tree',
            trees[index % 2],
            '-p',
            head,
            '-m',
            `fremd ${index}`,
          )
        ).trim()
        await fixture.git('update-ref', 'HEAD', commit, head)
        head = commit
      }
      await writeFile(page, 'fremd B\n')
      await fixture.git('add', '--', 'analyses/beispiel-analyse.md')
      const rendered = createHash('sha256')
        .update(
          await readFile(path.join(fixture.wikiRoot, 'assets/analyses/beispiel-analyse.html')),
        )
        .digest('hex')
      await writeFile(
        path.join(fixture.stateRoot, 'authoring.json'),
        JSON.stringify({
          version: 1,
          status: 'operator_action_required',
          session_id: 'ses_main',
          request_id: 'req-old',
          kind: 'analysis',
          target: 'beispiel-analyse',
          digest: digest(
            '# Beispiel & Analyse\nErstellt: 2026-10-11\n\nEin **Befund** mit [[concepts/evidenz|Evidenz]].\n',
          ),
          rendered,
          base: base_commit,
          phase: null,
          blocker: 'interrupted_authoring',
          result: null,
          status_delivery_failed: false,
        }),
      )
      await assert.rejects(authoring.resume({ confirmed: true }), /manual_maintenance_required/)
      assert.equal((await authoring.status()).status, 'operator_action_required')
      assert.equal(await authoringBusy(fixture.stateRoot), true)
      // A later repair may restore the landed bytes, but merely locating an
      // old commit is not enough while the current destination remains foreign.
      await writeFile(
        page,
        '# Beispiel & Analyse\nErstellt: 2026-10-11\n\nEin **Befund** mit [[concepts/evidenz|Evidenz]].\n',
      )
      await fixture.git('add', '--', 'analyses/beispiel-analyse.md')
      await fixture.git('commit', '-qm', 'operator restores confirmed content')
      const resumed = await authoring.resume({ confirmed: true })
      assert.equal(resumed.status, 'completed')
      assert.equal(resumed.commit, (await fixture.git('rev-parse', 'HEAD')).trim())
    },
    { texts: [ANALYSIS_BLOCK()] },
  )
})
