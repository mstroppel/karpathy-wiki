#!/bin/sh
set -eu
umask 077

usage() {
  cat >&2 <<'EOF'
Usage:
  wiki-ingest-transaction.sh begin <<'TXN'
ADAPTER
SOURCE_KEY
SOURCE_REVISION
TXN
  wiki-ingest-transaction.sh write RELATIVE_PATH < CONTENT
  wiki-ingest-transaction.sh commit <<'COMMIT'
feat(wiki): import source
COMMIT
  wiki-ingest-transaction.sh recover
EOF
  exit 2
}

die() {
  printf 'wiki ingest transaction: %s\n' "$*" >&2
  exit 1
}

[ "$#" -ge 1 ] || usage
command=$1
shift

root=$(git rev-parse --show-toplevel 2>/dev/null) \
  || die 'run this from inside the wiki Git repository'
root=$(readlink -f "$root")
git_dir=$(git rev-parse --absolute-git-dir)
state="$git_dir/wiki-ingest-recovery"

hash_file() {
  sha256sum < "$1" | cut -d ' ' -f 1
}

valid_relative_path() {
  relative=$1
  [ -n "$relative" ] || die 'path must be nonempty and relative to the wiki root'
  case "$relative" in
    /*|.|..|../*|*/../*|*/..|*//*|./*|*/./*|*/.|.git|.git/*|*/.git|*/.git/*)
      die 'path must stay inside the wiki root and use a normalized relative path'
      ;;
  esac
  if printf '%s' "$relative" | LC_ALL=C grep -q '[[:cntrl:]]'; then
    die 'path must not contain tabs or newlines'
  fi
  case "$relative" in
    *.md) ;;
    *) die 'only Markdown files outside .git may be written' ;;
  esac
}

validate_parent_path() {
  check_parent=$(dirname -- "$root/$1")
  while [ "$check_parent" != "$root" ]; do
    [ ! -L "$check_parent" ] || die 'path must not contain symlink components'
    check_parent=$(dirname -- "$check_parent")
  done
}

entry_for_path() {
  entry_key=$(printf '%s' "$1" | sha256sum | cut -d ' ' -f 1)
  printf '%s/entries/%s\n' "$state" "$entry_key"
}

literal_pathspec() {
  printf ':(literal)%s\n' "$1"
}

git_mode_for_file_mode() {
  file_mode_value=$((0$1))
  if [ $((file_mode_value & 0111)) -ne 0 ]; then
    printf '100755\n'
  else
    printf '100644\n'
  fi
}

index_mode_for_path() {
  index_mode_path=$1
  index_mode_spec=$(literal_pathspec "$index_mode_path")
  if ! git ls-files --error-unmatch -- "$index_mode_spec" >/dev/null 2>&1; then
    printf 'ABSENT\n'
    return
  fi
  index_mode_lines=$(git ls-files --stage -- "$index_mode_spec")
  case "$index_mode_lines" in
    *'
'*) die "cannot verify unmerged index entry: $index_mode_path" ;;
  esac
  printf '%s\n' "$index_mode_lines" | cut -d ' ' -f 1
}

index_hash_for_path() {
  index_hash_path=$1
  if ! git ls-files --error-unmatch -- "$(literal_pathspec "$index_hash_path")" >/dev/null 2>&1; then
    printf 'ABSENT\n'
    return
  fi
  git show ":$index_hash_path" | sha256sum | cut -d ' ' -f 1
}

head_mode_for_path() {
  head_mode_path=$1
  head_mode_lines=$(git ls-tree -z HEAD -- "$(literal_pathspec "$head_mode_path")" | tr '\000' '\n')
  if [ -z "$head_mode_lines" ]; then
    printf 'ABSENT\n'
    return
  fi
  case "$head_mode_lines" in
    *'
'*) die "cannot verify multiple HEAD entries for: $head_mode_path" ;;
  esac
  printf '%s\n' "$head_mode_lines" | cut -d ' ' -f 1
}

head_hash_for_path() {
  head_hash_path=$1
  if [ "$(head_mode_for_path "$head_hash_path")" = ABSENT ]; then
    printf 'ABSENT\n'
    return
  fi
  git show "HEAD:$head_hash_path" | sha256sum | cut -d ' ' -f 1
}

load_paths() {
  [ -d "$state/entries" ] || die 'transaction journal has no entries'
  load_paths_found=false
  for load_entry in "$state"/entries/*; do
    [ -d "$load_entry" ] || continue
    [ -f "$load_entry/path" ] && [ -f "$load_entry/meta" ] \
      || die 'transaction journal entry is incomplete'
    load_relative=$(cat "$load_entry/path")
    valid_relative_path "$load_relative"
    validate_parent_path "$load_relative"
    [ "$(entry_for_path "$load_relative")" = "$load_entry" ] \
      || die 'transaction journal path does not match its entry'
    load_paths_found=true
  done
  [ "$load_paths_found" = true ] || die 'transaction journal has no file entries'
}

entry_meta_value() {
  sed -n "s/^$2=//p" "$1/meta" | head -n 1
}

current_state() {
  current_target=$1
  if [ -L "$current_target" ]; then
    die "refusing to follow symlink: ${current_target#"$root"/}"
  elif [ -f "$current_target" ]; then
    hash_file "$current_target"
  elif [ -e "$current_target" ]; then
    die "refusing to replace a non-file path: ${current_target#"$root"/}"
  else
    printf 'ABSENT\n'
  fi
}

state_is_recorded() {
  grep -Fqx "state=$2" "$1/meta"
}

verify_current_files() {
  for verify_entry in "$state"/entries/*; do
    [ -d "$verify_entry" ] || continue
    verify_relative=$(cat "$verify_entry/path")
    validate_parent_path "$verify_relative"
    verify_target="$root/$verify_relative"
    verify_actual=$(current_state "$verify_target")
    state_is_recorded "$verify_entry" "$verify_actual" \
      || die "file changed outside the transaction; preserving it: $verify_relative"
    verify_indexed=$(index_hash_for_path "$verify_relative") \
      || die "cannot verify staged file; preserving it: $verify_relative"
    state_is_recorded "$verify_entry" "$verify_indexed" \
      || die "staged file changed outside the transaction; preserving it: $verify_relative"
    verify_recorded_mode=$(entry_meta_value "$verify_entry" mode)
    if [ "$verify_actual" != ABSENT ]; then
      verify_mode=$(stat -c '%a' -- "$verify_target")
      [ "$verify_mode" = "$verify_recorded_mode" ] \
        || die "file mode changed outside the transaction; preserving it: $verify_relative"
    fi
    verify_index_mode=$(index_mode_for_path "$verify_relative")
    verify_expected_index_mode=$(git_mode_for_file_mode "$verify_recorded_mode")
    verify_before=$(entry_meta_value "$verify_entry" before)
    if [ "$verify_before" = ABSENT ]; then
      [ "$verify_index_mode" = ABSENT ] || [ "$verify_index_mode" = "$verify_expected_index_mode" ] \
        || die "staged file mode changed outside the transaction; preserving it: $verify_relative"
    else
      [ "$verify_index_mode" = "$verify_expected_index_mode" ] \
        || die "staged file mode changed outside the transaction; preserving it: $verify_relative"
    fi
  done
}

verify_final_files() {
  for final_entry in "$state"/entries/*; do
    [ -d "$final_entry" ] || continue
    final_relative=$(cat "$final_entry/path")
    final_expected=$(sed -n 's/^state=//p' "$final_entry/meta" | tail -n 1)
    final_actual=$(current_state "$root/$final_relative")
    [ "$final_actual" = "$final_expected" ] \
      || die "file changed after the transaction's last write: $final_relative"
    if git ls-files --error-unmatch -- "$(literal_pathspec "$final_relative")" >/dev/null 2>&1; then
      final_indexed=$(git show ":$final_relative" | sha256sum | cut -d ' ' -f 1)
    else
      final_indexed=ABSENT
    fi
    [ "$final_indexed" = "$final_expected" ] \
      || die "staged file differs from the transaction's last write: $final_relative"
    final_mode=$(stat -c '%a' -- "$root/$final_relative")
    final_expected_mode=$(entry_meta_value "$final_entry" mode)
    [ "$final_mode" = "$final_expected_mode" ] \
      || die "file mode changed after the transaction's last write: $final_relative"
    final_index_mode=$(index_mode_for_path "$final_relative")
    [ "$final_index_mode" = "$(git_mode_for_file_mode "$final_expected_mode")" ] \
      || die "staged file mode differs from the transaction's last write: $final_relative"
  done
}

verify_only_transaction_paths_changed() {
  verify_status=$(git status --porcelain=v1 -z --untracked-files=all | tr '\000' '\n')
  printf '%s\n' "$verify_status" | while IFS= read -r verify_line; do
    [ -n "$verify_line" ] || continue
    verify_relative=${verify_line#???}
    verify_matched=false
    for verify_entry in "$state"/entries/*; do
      [ -d "$verify_entry" ] || continue
      if [ "$verify_relative" = "$(cat "$verify_entry/path")" ]; then
        verify_matched=true
        break
      fi
    done
    [ "$verify_matched" = true ] \
      || die "unrelated wiki changes exist; preserving them: $verify_relative"
  done
}

transaction_paths() {
  for path_entry in "$state"/entries/*; do
    [ -d "$path_entry" ] || continue
    path_before=$(entry_meta_value "$path_entry" before)
    path_after=$(sed -n 's/^state=//p' "$path_entry/meta" | tail -n 1)
    [ "$path_before" = "$path_after" ] || cat "$path_entry/path"
  done | LC_ALL=C sort
}

begin_transaction() {
  [ "$#" -eq 0 ] || usage
  [ ! -e "$state" ] && [ ! -L "$state" ] \
    || die 'an interrupted transaction exists; run /ingest-recover first'
  [ -z "$(git status --porcelain=v1 --untracked-files=all)" ] \
    || die 'wiki worktree must be clean before starting a source transaction'
  IFS= read -r begin_adapter || die 'missing adapter in source identity'
  IFS= read -r begin_source_key || die 'missing source key in source identity'
  IFS= read -r begin_revision || die 'missing source revision in source identity'
  if IFS= read -r begin_extra; then
    : "$begin_extra"
    die 'source identity must contain exactly three lines'
  fi
  for begin_value in "$begin_adapter" "$begin_source_key" "$begin_revision"; do
    [ -n "$begin_value" ] \
      || die 'source identity fields must be nonempty single-line values'
    if printf '%s' "$begin_value" | LC_ALL=C grep -q '[[:cntrl:]]'; then
      die 'source identity fields must be nonempty single-line values'
    fi
  done
  begin_baseline=$(git rev-parse HEAD) || die 'wiki repository has no HEAD commit'
  mkdir -m 700 -- "$state"
  mkdir -m 700 -- "$state/entries"
  printf '%s\n' "$begin_baseline" > "$state/baseline"
  printf '%s\n' "$begin_adapter" > "$state/adapter"
  printf '%s\n' "$begin_source_key" > "$state/source-key"
  printf '%s\n' "$begin_revision" > "$state/source-revision"
  : > "$state/ready"
  printf 'Started transaction for %s:%s at %s\n' \
    "$begin_adapter" "$begin_source_key" "$begin_baseline"
}

write_file() {
  [ "$#" -eq 1 ] || usage
  [ -d "$state" ] || die 'no active source transaction; call begin first'
  [ -f "$state/ready" ] || die 'source transaction setup is incomplete; run /ingest-recover'
  write_relative=$1
  valid_relative_path "$write_relative"
  write_target="$root/$write_relative"
  write_parent=$(dirname -- "$write_target")
  validate_parent_path "$write_relative"
  mkdir -p -- "$write_parent"
  validate_parent_path "$write_relative"
  write_entry=$(entry_for_path "$write_relative")
  if [ ! -d "$write_entry" ]; then
    write_before=$(current_state "$write_target")
    if [ "$write_before" != ABSENT ]; then
      write_head_mode=$(head_mode_for_path "$write_relative")
      [ "$write_head_mode" = 100644 ] || [ "$write_head_mode" = 100755 ] \
        || die "refusing to overwrite a path that is not a regular committed file: $write_relative"
      write_head_hash=$(head_hash_for_path "$write_relative")
      [ "$write_before" = "$write_head_hash" ] \
        || die "file changed after the transaction began; preserving it: $write_relative"
      [ "$(index_hash_for_path "$write_relative")" = "$write_head_hash" ] \
        || die "staged file changed after the transaction began; preserving it: $write_relative"
      write_mode=$(stat -c '%a' -- "$write_target")
      [ "$(git_mode_for_file_mode "$write_mode")" = "$write_head_mode" ] \
        || die "file mode changed after the transaction began; preserving it: $write_relative"
      [ "$(index_mode_for_path "$write_relative")" = "$write_head_mode" ] \
        || die "staged file mode changed after the transaction began; preserving it: $write_relative"
    else
      [ "$(index_hash_for_path "$write_relative")" = ABSENT ] \
        || die "refusing to recreate a staged path that disappeared: $write_relative"
      [ "$(head_hash_for_path "$write_relative")" = ABSENT ] \
        || die "refusing to recreate a committed path that disappeared: $write_relative"
      write_mode=644
    fi
    write_entry_temp=$(mktemp -d -- "$state/entries/.pending.XXXXXXXX")
    printf '%s\n' "$write_relative" > "$write_entry_temp/path"
    printf 'before=%s\nmode=%s\nstate=%s\n' \
      "$write_before" "$write_mode" "$write_before" > "$write_entry_temp/meta"
    mv -- "$write_entry_temp" "$write_entry"
  elif [ "$(cat "$write_entry/path")" != "$write_relative" ]; then
    die 'transaction path hash collision'
  else
    write_mode=$(entry_meta_value "$write_entry" mode)
    [ "$(stat -c '%a' -- "$write_target")" = "$write_mode" ] \
      || die "file mode changed during the transaction: $write_relative"
  fi

  write_temp=$(mktemp -- "$write_parent/.wiki-ingest-write.XXXXXXXX")
  trap 'rm -f -- "$write_temp"' EXIT HUP INT TERM
  cat > "$write_temp"
  if [ -e "$write_target" ]; then
    chmod "$write_mode" -- "$write_temp"
  else
    chmod 0644 -- "$write_temp"
  fi
  write_hash=$(hash_file "$write_temp")

  # Journal the expected state before the atomic replacement. Recovery accepts
  # either the previous recorded bytes or this new version if interrupted.
  write_meta_temp=$(mktemp -- "$write_entry/.meta.XXXXXXXX")
  {
    printf 'before=%s\n' "$(entry_meta_value "$write_entry" before)"
    printf 'mode=%s\n' "$(entry_meta_value "$write_entry" mode)"
    sed -n 's/^state=/state=/p' "$write_entry/meta"
    printf 'state=%s\n' "$write_hash"
  } > "$write_meta_temp"
  verify_current_files
  mv -f -- "$write_meta_temp" "$write_entry/meta"
  verify_current_files
  mv -f -- "$write_temp" "$write_target"
  trap - EXIT HUP INT TERM
  printf 'Wrote %s (%s)\n' "$write_relative" "$write_hash"
}

validate_commit_state() {
  load_paths
  [ "$(git rev-parse HEAD)" = "$(cat "$state/baseline")" ] \
    || die 'HEAD changed during the transaction; preserving the journal and worktree'
  verify_only_transaction_paths_changed
  verify_current_files
  for commit_entry in "$state"/entries/*; do
    [ -d "$commit_entry" ] || continue
    commit_relative=$(cat "$commit_entry/path")
    commit_target="$root/$commit_relative"
    commit_actual=$(current_state "$commit_target")
    commit_expected=$(sed -n 's/^state=//p' "$commit_entry/meta" | tail -n 1)
    [ "$commit_actual" = "$commit_expected" ] && [ "$commit_actual" != ABSENT ] \
      || die "transaction output is not the latest recorded write: $commit_relative"
  done
}

verify_commit_paths() {
  verify_expected_paths=$(transaction_paths)
  verify_committed_paths=$(git diff-tree --no-commit-id --name-only -z -r HEAD \
    | tr '\000' '\n' | LC_ALL=C sort)
  [ "$verify_committed_paths" = "$verify_expected_paths" ]
}

verify_commit_blobs() {
  for blob_entry in "$state"/entries/*; do
    [ -d "$blob_entry" ] || continue
    blob_relative=$(cat "$blob_entry/path")
    blob_expected=$(sed -n 's/^state=//p' "$blob_entry/meta" | tail -n 1)
    git cat-file -e "HEAD:$blob_relative" 2>/dev/null || return 1
    blob_actual=$(git show "HEAD:$blob_relative" | sha256sum | cut -d ' ' -f 1) \
      || return 1
    [ "$blob_actual" = "$blob_expected" ] || return 1
  done
}

commit_transaction() {
  [ "$#" -eq 0 ] || usage
  [ -d "$state" ] || die 'no active source transaction'
  IFS= read -r commit_message || die 'missing commit message'
  if IFS= read -r commit_extra; then
    : "$commit_extra"
    die 'commit message must be a single line'
  fi
  printf '%s\n' "$commit_message" \
    | grep -Eq '^(feat|fix|docs|style|refactor|perf|test|build|ci|chore)(\([^()]+\))?: .+' \
    || die 'commit message must follow Conventional Commits'
  validate_commit_state
  [ -n "$(transaction_paths)" ] \
    || die 'source transaction contains no Git changes; no commit was created'
  for commit_entry in "$state"/entries/*; do
    [ -d "$commit_entry" ] || continue
    commit_before=$(entry_meta_value "$commit_entry" before)
    commit_after=$(sed -n 's/^state=//p' "$commit_entry/meta" | tail -n 1)
    [ "$commit_before" = "$commit_after" ] \
      || git add -- "$(literal_pathspec "$(cat "$commit_entry/path")")"
  done
  git -c commit.gpgsign=false commit -m "$commit_message"
  commit_hash=$(git rev-parse HEAD)
  commit_parent=$(git rev-parse HEAD^)
  [ "$commit_parent" = "$(cat "$state/baseline")" ] \
    || die 'created commit does not match transaction baseline'
  verify_commit_paths \
    || die 'commit contains paths outside this source transaction'
  verify_commit_blobs \
    || die 'committed file content does not match transaction output'
  verify_only_transaction_paths_changed
  verify_final_files
  rm -rf -- "$state"
  printf 'Committed source transaction %s\n' "$commit_hash"
}

finalize_committed_transaction() {
  finalize_baseline=$1
  finalize_head=$(git rev-parse HEAD)
  finalize_parent=$(git rev-parse HEAD^ 2>/dev/null || true)
  [ "$finalize_parent" = "$finalize_baseline" ] || return 1
  verify_only_transaction_paths_changed
  verify_current_files
  verify_final_files
  verify_commit_paths || return 1
  verify_commit_blobs || return 1
  rm -rf -- "$state"
  printf 'Finalized already-committed transaction %s\n' "$finalize_head"
}

recover_transaction() {
  [ "$#" -eq 0 ] || usage
  if [ ! -e "$state" ] && [ ! -L "$state" ]; then
    printf 'No interrupted ingest transaction recorded.\n'
    return 0
  fi
  [ -d "$state" ] && [ ! -L "$state" ] \
    || die 'transaction journal is incomplete; preserving it'
  if [ ! -f "$state/ready" ]; then
    for incomplete_entry in "$state"/entries/*; do
      [ -d "$incomplete_entry" ] || continue
      die 'incomplete transaction contains file entries; preserving it'
    done
    if [ -f "$state/baseline" ]; then
      [ "$(git rev-parse HEAD)" = "$(cat "$state/baseline")" ] \
        || die 'repository advanced during incomplete transaction setup; preserving its journal'
    fi
    [ -z "$(git status --porcelain=v1 --untracked-files=all)" ] \
      || die 'incomplete transaction has wiki changes; preserving its journal'
    rm -rf -- "$state"
    printf 'Removed incomplete transaction setup; no wiki changes were made.\n'
    return 0
  fi
  for pending_entry in "$state"/entries/.pending.*; do
    [ -d "$pending_entry" ] || continue
    rm -rf -- "$pending_entry"
  done
  [ -d "$state" ] && [ ! -L "$state" ] && [ -f "$state/baseline" ] \
    || die 'transaction journal is incomplete; preserving it'
  recover_baseline=$(cat "$state/baseline")
  [ -d "$state/entries" ] || die 'transaction journal has no entries directory; preserving it'
  recover_has_entries=false
  for recover_entry in "$state"/entries/*; do
    [ -d "$recover_entry" ] || continue
    recover_has_entries=true
  done
  if [ "$recover_has_entries" = false ]; then
    [ "$(git rev-parse HEAD)" = "$recover_baseline" ] \
      || die 'repository advanced during an empty transaction; preserving its journal'
    [ -z "$(git status --porcelain=v1 --untracked-files=all)" ] \
      || die 'empty transaction has wiki changes; preserving its journal'
    rm -rf -- "$state"
    printf 'Cleared empty source transaction; no wiki files were written.\n'
    return 0
  fi
  load_paths
  if [ "$(git rev-parse HEAD)" != "$recover_baseline" ]; then
    finalize_committed_transaction "$recover_baseline" \
      || die 'repository advanced beyond the transaction; preserving journal and files for manual review'
    return 0
  fi
  verify_only_transaction_paths_changed
  verify_current_files
  printf 'Rolling back interrupted source transaction %s:%s (%s)\n' \
    "$(cat "$state/adapter")" "$(cat "$state/source-key")" \
    "$(cat "$state/source-revision")"
  for recover_entry in "$state"/entries/*; do
    [ -d "$recover_entry" ] || continue
    verify_only_transaction_paths_changed
    verify_current_files
    recover_relative=$(cat "$recover_entry/path")
    recover_before=$(entry_meta_value "$recover_entry" before)
    recover_target="$root/$recover_relative"
    if [ "$recover_before" = ABSENT ]; then
      git reset -q "$recover_baseline" -- "$(literal_pathspec "$recover_relative")"
      rm -f -- "$recover_target"
    else
      git restore --source="$recover_baseline" --staged --worktree \
        -- "$(literal_pathspec "$recover_relative")"
      chmod "$(entry_meta_value "$recover_entry" mode)" -- "$recover_target"
    fi
  done
  rm -rf -- "$state"
  printf 'Interrupted transaction rolled back; wiki worktree is ready to retry.\n'
}

case "$command" in
  begin) begin_transaction "$@" ;;
  write) write_file "$@" ;;
  commit) commit_transaction "$@" ;;
  recover) recover_transaction "$@" ;;
  *) usage ;;
esac
