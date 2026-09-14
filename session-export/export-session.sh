#!/bin/sh

set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="${SESSION_EXPORT_OUTPUT_DIR:-$SCRIPT_DIR/output}"
DIRECTORY="${OPENCODE_DIRECTORY:-/knowledge/wiki}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_DIR/compose.yaml}"
OPENCODE_SERVICE="${OPENCODE_SERVICE:-opencode}"
OPENCODE_URL="${OPENCODE_CONTAINER_URL:-http://127.0.0.1:4096}"

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
	printf 'Usage: %s SESSION_ID\n' "$0" >&2
	exit 2
fi

SESSION_ID="$1"

case "$SESSION_ID" in
	*/*|*'..'*|*[!A-Za-z0-9_-]*)
		printf 'Error: SESSION_ID contains invalid characters.\n' >&2
		exit 2
		;;
esac

if ! command -v docker >/dev/null 2>&1; then
	printf 'Error: docker was not found.\n' >&2
	exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
	printf 'Error: jq was not found.\n' >&2
	exit 1
fi

compose() {
	docker compose \
		--project-directory "$PROJECT_DIR" \
		-f "$COMPOSE_FILE" \
		exec -T "$OPENCODE_SERVICE" "$@"
}

api_get() {
	compose sh -c \
		'exec curl --fail --silent --show-error --user "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD" --get "$1" --data-urlencode "directory=$2"' \
		sh "$OPENCODE_URL$1" "$DIRECTORY"
}

mkdir -p "$OUTPUT_DIR"
umask 077
OUTPUT_FILE="$OUTPUT_DIR/$SESSION_ID.json"
TEMP_DIR="$(mktemp -d "$OUTPUT_DIR/.session-export.XXXXXX")"
TEMP_FILE="$TEMP_DIR/records.ndjson"
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

api_get /session >"$TEMP_DIR/all-sessions.json"
if ! jq -e --arg id "$SESSION_ID" 'any(.[]; .id == $id)' \
	"$TEMP_DIR/all-sessions.json" >/dev/null; then
	printf 'Error: session not found: %s\n' "$SESSION_ID" >&2
	exit 1
fi

: >"$TEMP_FILE"
QUEUE="$SESSION_ID"
VISITED=""

while [ -n "$QUEUE" ]; do
	CURRENT_ID=${QUEUE%%
*}
	QUEUE=${QUEUE#*
}
	if [ "$CURRENT_ID" = "$QUEUE" ]; then
		QUEUE=""
	fi

	case " $VISITED " in
		*" $CURRENT_ID "*) continue ;;
	esac
	VISITED="$VISITED $CURRENT_ID"

	jq -c --arg id "$CURRENT_ID" '.[] | select(.id == $id)' \
		"$TEMP_DIR/all-sessions.json" >"$TEMP_DIR/session.json"
	api_get "/session/$CURRENT_ID/message" >"$TEMP_DIR/messages.json"
	jq -cn --slurpfile session "$TEMP_DIR/session.json" \
		--slurpfile messages "$TEMP_DIR/messages.json" \
		'{session: $session[0], messages: $messages[0]}' >>"$TEMP_FILE"

	CHILDREN="$(jq -r --arg id "$CURRENT_ID" \
		'.[] | select(.parentID == $id) | .id' "$TEMP_DIR/all-sessions.json")"
	if [ -n "$CHILDREN" ]; then
		QUEUE="$QUEUE$CHILDREN
"
	fi
done

jq -s --arg root "$SESSION_ID" --arg directory "$DIRECTORY" \
	'{rootSessionId: $root, directory: $directory, sessions: .}' \
	"$TEMP_FILE" >"$TEMP_FILE.final"
mv "$TEMP_FILE.final" "$OUTPUT_FILE"
trap - EXIT HUP INT TERM

printf 'Session data exported: %s\n' "$OUTPUT_FILE"
