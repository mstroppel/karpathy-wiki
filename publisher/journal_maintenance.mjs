import { maintainJournal } from './journal_storage.mjs'

// Host/operator exec only. Never exposed through chat, HTTP or model tools.
try {
  const args = process.argv.slice(2)
  let confirmed = false
  let keepCompleted = 0
  while (args.length) {
    const arg = args.shift()
    if (arg === '--confirm' && !confirmed) confirmed = true
    else if (arg === '--keep-completed' && /^\d+$/.test(args[0] ?? ''))
      keepCompleted = Number(args.shift())
    else throw new Error('invalid_arguments')
  }
  process.stdout.write(`${JSON.stringify(await maintainJournal({ confirmed, keepCompleted }))}\n`)
} catch (error) {
  const reason =
    error.message === 'Ein anderer Schreiber hält den Ingest-Lock'
      ? 'writer_lock_unavailable'
      : error.message
  const reasons = [
    'invalid_arguments',
    'invalid_retention_count',
    'unsafe_root',
    'unsafe_git_root',
    'unsafe_preparations_root',
    'unsafe_symlink',
    'unsafe_file_type_or_hardlink',
    'publisher_not_idle',
    'controller_not_idle',
    'controller_lock_missing',
    'unreadable_publisher_state',
    'invalid_controller_state',
    'invalid_controller_fence',
    'invalid_authoring_state',
    'wiki_index_lock_requires_operator',
    'lock_file_missing',
    'writer_lock_unavailable',
    'cleanup_manifest_mismatch',
    'draft_changed_during_cleanup',
    'receipt_changed_during_cleanup',
    'unresolved_preparation',
    'live_backup_evidence_changed',
    'commit_parent_mismatch',
    'commit_paths_mismatch',
    'commit_blob_mismatch',
    'source_identity_mismatch',
    'run_not_successfully_closed',
    'journal_evidence_mismatch',
    'report_evidence_mismatch',
    'draft_git_ownership_missing',
    'foreign_git_metadata',
    'unknown_draft_entries',
    'draft_lock_requires_operator',
    'draft_evidence_mismatch',
    'foreign_draft_changes',
    'foreign_draft_head',
    'foreign_draft_refs',
    'hidden_draft_changes',
  ]
  process.stdout.write(
    `${JSON.stringify({ error: 'journal_maintenance_refused', reason: reasons.includes(reason) ? reason : 'writer_lock_or_evidence_unavailable', hint: 'Require idle writers, intact authority state, clean wiki and existing locks; see docs/ingest-journal-storage.md.' })}\n`,
  )
  process.exitCode = 1
}
