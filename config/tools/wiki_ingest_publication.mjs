import path from 'node:path'
import {
  cleanWiki,
  digest,
  dirtyPaths,
  draftChanges,
  fileBytes,
  git,
  IngestFault,
  putFile,
  relativePage,
  editablePage,
} from './wiki_ingest_files.mjs'
import { scanIngestStatus } from './wiki_ingest_status_core.mjs'

export async function preparePublication(opts, source, attempt, baseline) {
  const relative = relativePage(path.relative(opts.wikiRoot, source.wiki_path))
  const changed = await draftChanges(attempt.wiki, baseline, relative, opts.maxDraftBytes)
  const bytes = await fileBytes(path.join(attempt.wiki, relative), attempt.wiki)
  const text = bytes.toString('utf8')
  const body = text.startsWith('---\n') ? text.replace(/^---\n[\s\S]*?\n---\n/, '') : text
  if (!body.trim()) throw new IngestFault('empty_source_page')
  const frontmatter = Object.entries(source.frontmatter)
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join('\n')
  await putFile(attempt.wiki, relative, `---\n${frontmatter}\n---\n${body}`)
  let index = (await fileBytes(path.join(attempt.wiki, 'index.md'), attempt.wiki)).toString('utf8')
  for (const file of new Set([...changed, relative])) {
    const page = file.replace(/\.md$/, '')
    if (/[[\]|<>\r\n]/.test(page)) throw new IngestFault('unsafe_catalog_path')
    if (!index.includes(`[[${page}]]`) && !index.includes(`[[${page}|`))
      index += `\n- [[${page}]]\n`
  }
  await putFile(attempt.wiki, 'index.md', index)
  const log = await fileBytes(path.join(attempt.wiki, 'log.md'), attempt.wiki)
  await putFile(
    attempt.wiki,
    'log.md',
    log + `\n- Ingest ${opts.batchId}: [[${relative.replace(/\.md$/, '')}]].\n`,
  )
  const checked = await scanIngestStatus({
    sourceRoot: opts.sourceRoot,
    wikiSourceRoot: path.join(attempt.wiki, 'sources'),
    includeCurrent: true,
  })
  if (
    !checked.adapters[source.adapter]?.current.some(
      (item) =>
        item.source_key === source.source_key && item.source_revision === source.source_revision,
    )
  )
    throw new IngestFault('source_not_verified')
  // A private index creates exactly the reviewed tree. No live staging, hooks,
  // resets or broad commits; crash recovery can verify the actual commit object.
  const env = { GIT_INDEX_FILE: path.join(path.dirname(attempt.wiki), 'index') }
  await git(opts.wikiRoot, ['read-tree', attempt.base], env)
  const files = []
  for (const file of [...new Set([...changed, relative, 'index.md', 'log.md'])].sort()) {
    const after = await fileBytes(path.join(attempt.wiki, file), attempt.wiki)
    const blob = (
      await git(opts.wikiRoot, ['hash-object', '-w', '--', path.join(attempt.wiki, file)])
    ).trim()
    await git(opts.wikiRoot, ['update-index', '--add', '--cacheinfo', '100644', blob, file], env)
    files.push({ path: file, before: baseline[file] ?? null, after: digest(after), blob })
  }
  const tree = (await git(opts.wikiRoot, ['write-tree'], env)).trim()
  const commit = (
    await git(opts.wikiRoot, [
      'commit-tree',
      tree,
      '-p',
      attempt.base,
      '-m',
      `feat(wiki): ingest source (${opts.batchId})`,
    ])
  ).trim()
  return { base: attempt.base, tree, commit, files, attempt: attempt.id }
}

export async function publishPrepared(opts, source, persist, validateSource) {
  const publication = source.publication
  const { base, tree, commit, files } = publication
  if (
    !/^[a-f0-9]{40,64}$/.test(base) ||
    !/^[a-f0-9]{40,64}$/.test(tree) ||
    !/^[a-f0-9]{40,64}$/.test(commit) ||
    !Array.isArray(files) ||
    !files.length
  )
    throw new IngestFault('invalid_publication')
  const parent = (await git(opts.wikiRoot, ['rev-list', '--parents', '-n', '1', commit])).trim()
  if (
    parent !== `${commit} ${base}` ||
    (await git(opts.wikiRoot, ['rev-parse', `${commit}^{tree}`])).trim() !== tree
  )
    throw new IngestFault('publication_commit_mismatch')
  for (const item of files) {
    relativePage(item.path)
    if (
      !['index.md', 'log.md'].includes(item.path) &&
      !editablePage(item.path, path.relative(opts.wikiRoot, source.wiki_path))
    )
      throw new IngestFault('publication_forbidden_path')
    if (
      !/^[a-f0-9]{64}$/.test(item.after) ||
      (item.before !== null && !/^[a-f0-9]{64}$/.test(item.before))
    )
      throw new IngestFault('invalid_publication')
    const object = await git(opts.wikiRoot, ['rev-parse', `${commit}:${item.path}`])
    if (object.trim() !== item.blob) throw new IngestFault('publication_commit_mismatch')
    if (
      digest(await git(opts.wikiRoot, ['cat-file', 'blob', object.trim()], {}, null)) !== item.after
    )
      throw new IngestFault('publication_content_mismatch')
  }
  const actual = (
    await git(opts.wikiRoot, [
      'diff-tree',
      '--no-commit-id',
      '--name-only',
      '-r',
      '-z',
      base,
      commit,
    ])
  )
    .split('\0')
    .filter(Boolean)
    .sort()
  const expected = files
    .filter((item) => item.before !== item.after)
    .map((item) => item.path)
    .sort()
  // A selected outdated page may be explicitly rewritten byte-for-byte. Index
  // and log still change; unchanged entries are allowed in the installation set.
  if (
    actual.some((file) => !files.some((item) => item.path === file)) ||
    expected.some((file) => !actual.includes(file))
  )
    throw new IngestFault('publication_diff_mismatch')
  const head = (await git(opts.wikiRoot, ['rev-parse', 'HEAD'])).trim()
  if (head !== base && head !== commit) throw new IngestFault('wiki_base_changed')
  const baseTree = (await git(opts.wikiRoot, ['rev-parse', `${base}^{tree}`])).trim()
  const indexTree = (await git(opts.wikiRoot, ['write-tree'])).trim()
  if (indexTree !== baseTree && indexTree !== tree) throw new IngestFault('foreign_index_changes')
  const paths = new Set(files.map((file) => file.path))
  if ((await dirtyPaths(opts.wikiRoot)).some((file) => !paths.has(file)))
    throw new IngestFault('foreign_wiki_changes')
  for (const item of files) {
    const current = await fileBytes(path.join(opts.wikiRoot, item.path), opts.wikiRoot, {
      absent: true,
    })
    const hash = current === null ? null : digest(current)
    if (hash !== item.before && hash !== item.after) throw new IngestFault('foreign_wiki_changes')
  }
  const ref = `refs/wiki-ingest/${opts.batchId}/${publication.attempt}`
  await git(opts.wikiRoot, ['update-ref', ref, commit])
  for (const item of files) {
    opts.signal?.throwIfAborted()
    const current = await fileBytes(path.join(opts.wikiRoot, item.path), opts.wikiRoot, {
      absent: true,
    })
    const hash = current === null ? null : digest(current)
    if (hash === item.after) continue
    if (hash !== item.before) throw new IngestFault('foreign_wiki_changes')
    const bytes = await fileBytes(path.join(opts.attemptRoot, 'wiki', item.path), opts.attemptRoot)
    if (digest(bytes) !== item.after) throw new IngestFault('draft_changed')
    await putFile(opts.wikiRoot, item.path, bytes)
    await opts.onPhase?.('file_installed')
  }
  opts.signal?.throwIfAborted()
  await validateSource()
  // The expected old tree guards the normal index. Other Git writers must be idle.
  const nowIndex = (await git(opts.wikiRoot, ['write-tree'])).trim()
  if (nowIndex !== baseTree && nowIndex !== tree) throw new IngestFault('foreign_index_changes')
  await git(opts.wikiRoot, ['read-tree', commit])
  await opts.onPhase?.('index_installed')
  if (head === base) await git(opts.wikiRoot, ['update-ref', 'HEAD', commit, base])
  await opts.onPhase?.('commit_installed')
  await cleanWiki(opts.wikiRoot)
  source.commit = commit
  source.changed_pages = files.map((file) => file.path)
  source.status = 'ingested'
  source.phase = 'complete'
  await persist()
  await opts.onPhase?.('source_recorded')
  await git(opts.wikiRoot, ['update-ref', '-d', ref, commit])
}
