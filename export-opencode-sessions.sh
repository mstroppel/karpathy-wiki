#!/bin/sh
set -eu
umask 077

usage() {
  printf 'Usage: %s <session-id> [output-directory]\n' "$0" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
case "$1" in
  ses_*) case "${1#ses_}" in ''|*[!A-Za-z0-9]*) usage ;; esac ;;
  *) usage ;;
esac

command -v jq >/dev/null || { echo 'jq is required' >&2; exit 1; }
command -v docker >/dev/null || { echo 'docker is required' >&2; exit 1; }

# shellcheck disable=SC1007 # Clear CDPATH for these cd calls.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root_id=$1
output_dir=${2:-"$script_dir/../temp/opencode-sessions/$root_id"}
mkdir -p -- "$output_dir"
# shellcheck disable=SC1007 # Clear CDPATH for these cd calls.
output_dir=$(CDPATH= cd -- "$output_dir" && pwd)
work_dir=$(mktemp -d "$output_dir/.export.XXXXXX")
trap 'rm -r -- "$work_dir"' EXIT HUP INT TERM

# Run the CLI in the existing OpenCode service container, against its own
# server and session store.
export_session() (
  id=$1
  file=$output_dir/$id.json
  tmp=$(mktemp "$output_dir/.${id}.XXXXXX")
  # Do not let compose exec consume the remaining child IDs from the loop's stdin.
  if ! "$script_dir/karpathy-wiki.sh" exec -T opencode \
    opencode session export --server http://127.0.0.1:4096 "$id" \
    </dev/null >"$tmp"; then
    rm -f -- "$tmp"
    printf 'Export failed for %s\n' "$id" >&2
    return 1
  fi
  if ! jq -e --arg id "$id" \
    '.info.id == $id and (.messages | type == "array")' "$tmp" >/dev/null; then
    rm -f -- "$tmp"
    printf 'Invalid export for %s\n' "$id" >&2
    return 1
  fi
  mv -- "$tmp" "$file"
  printf 'Exported %s -> %s\n' "$id" "$file"
)

export_tree() (
  id=$1
  [ ! -e "$work_dir/$id.seen" ] || exit 0
  : >"$work_dir/$id.seen"
  export_session "$id"

  jq -r '
    .messages[]?
    | select(.type == "assistant")
    | .content[]?
    | select(.type == "tool" and .name == "subagent")
    | .state.metadata.sessionID // empty
  ' "$output_dir/$id.json" >"$work_dir/$id.children"

  while IFS= read -r child; do
    case "$child" in
      ses_*) case "${child#ses_}" in
        ''|*[!A-Za-z0-9]*) echo "Invalid child session ID: $child" >&2; exit 1 ;;
      esac ;;
      *) echo "Invalid child session ID: $child" >&2; exit 1 ;;
    esac
    export_tree "$child"
  done <"$work_dir/$id.children"
)

export_tree "$root_id"
count=$(find "$work_dir" -name '*.seen' -type f | wc -l | tr -d ' ')
printf 'Done: %s session(s) in %s\n' "$count" "$output_dir"
