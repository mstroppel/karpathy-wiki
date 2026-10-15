#!/bin/sh
set -eu

repository="mstroppel/karpathy-wiki"
# launcher-protocol: 1

# shellcheck disable=SC1007 # CDPATH is intentionally cleared for this command only
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=${KARPATHY_WIKI_PROJECT_DIR:-$script_dir}
env_file=$project_dir/.env
cache_dir=${KARPATHY_WIKI_CACHE_DIR:-$project_dir/.cache}
override_file=$project_dir/compose.override.yaml
latest_pointer=$cache_dir/latest

die() {
  printf 'karpathy-wiki: %s\n' "$*" >&2
  exit 1
}

require_curl() {
  command -v curl >/dev/null 2>&1 || die "curl is required to download Compose files"
}

read_env_value() {
  [ -f "$env_file" ] || return 0
  rev_value=$(sed -n "s/^$1=//p" "$env_file" | tail -n 1 | tr -d '\r')
  rev_value=${rev_value%\"}
  rev_value=${rev_value#\"}
  rev_value=${rev_value%\'}
  rev_value=${rev_value#\'}
  printf '%s' "$rev_value"
}

validate_version() {
  case "$1" in
    ''|*[!0-9A-Za-z.-]*|.*|*..*|*.) return 1 ;;
    *) return 0 ;;
  esac
}

resolve_pre_version() {
  require_curl
  rp_json=$(curl -fsSL \
    "https://api.github.com/repos/$repository/tags?per_page=100") || return 1
  rp_tag=$(printf '%s' "$rp_json" |
    grep -o '"name": *"v[0-9][0-9A-Za-z.-]*-pre\.[0-9]*"' |
    head -n 1 | sed 's/.*"v/v/;s/"//') || rp_tag=""
  [ -n "$rp_tag" ] || return 1
  rp_version=${rp_tag#v}
  validate_version "$rp_version" || return 1
  printf '%s' "$rp_version"
}

resolve_latest_version() {
  require_curl
  rlv_url=$(curl -fsSL -o /dev/null -w '%{url_effective}' \
    "https://github.com/$repository/releases/latest") || return 1
  rlv_tag=${rlv_url##*/}
  rlv_version=${rlv_tag#v}
  validate_version "$rlv_version" || return 1
  printf '%s' "$rlv_version"
}

compose_ref() {
  case "$1" in
    pr-*) printf 'main' ;;
    *) printf 'v%s' "$1" ;;
  esac
}

fetch_compose() {
  require_curl
  fc_ref=$(compose_ref "$1")
  fc_target=$2
  mkdir -p "$cache_dir"
  fc_tmp="$fc_target.tmp.$$"
  if curl -fsSL "https://raw.githubusercontent.com/$repository/$fc_ref/compose.yaml" \
    -o "$fc_tmp"; then
    chmod 0644 "$fc_tmp"
    mv -f "$fc_tmp" "$fc_target"
  else
    rm -f "$fc_tmp"
    return 1
  fi
}

resolve_version() {
  rv_requested=${KARPATHY_WIKI_VERSION:-$(read_env_value KARPATHY_WIKI_VERSION)}
  rv_channel=${KARPATHY_WIKI_CHANNEL:-$(read_env_value KARPATHY_WIKI_CHANNEL)}
  rv_requested=${rv_requested:-latest}
  if [ "$rv_requested" != "latest" ] && [ "$rv_requested" != "pre" ]; then
    validate_version "$rv_requested" ||
      die "invalid KARPATHY_WIKI_VERSION: $rv_requested"
    printf '%s\n' "$rv_requested"
    return 0
  fi
  rv_version=""
  if [ "$rv_channel" = pre ] || [ "$rv_requested" = pre ]; then
    rv_version=$(resolve_pre_version) || rv_version=""
    if [ -n "$rv_version" ]; then
      mkdir -p "$cache_dir"
      printf '%s\n' "$rv_version" >"$latest_pointer"
    fi
  else
    rv_version=$(resolve_latest_version) || rv_version=""
    if [ -n "$rv_version" ]; then
      mkdir -p "$cache_dir"
      printf '%s\n' "$rv_version" >"$latest_pointer"
    fi
  fi
  if [ -z "$rv_version" ] && [ -f "$latest_pointer" ]; then
    rv_version=$(sed -n 1p "$latest_pointer" | tr -d '[:space:]')
    validate_version "$rv_version" || rv_version=""
  fi
  [ -n "$rv_version" ] || die \
    "cannot resolve the latest version (offline?); pin KARPATHY_WIKI_VERSION in $env_file"
  printf '%s\n' "$rv_version"
}

compose_file_for() {
  cff_version=$1
  compose_file=$cache_dir/compose-v$cff_version.yaml
  if [ ! -f "$compose_file" ]; then
    fetch_compose "$cff_version" "$compose_file" || die \
      "cannot download compose.yaml for version $cff_version from $repository"
  fi
}

run_compose() {
  require_backup_runtime
  if [ -f "$override_file" ]; then
    python3 "$script_dir/launcher_backup.py" forward "$project_dir" docker compose \
      --project-directory "$project_dir" \
      -f "$compose_file" \
      -f "$override_file" \
      "$@"
  else
    python3 "$script_dir/launcher_backup.py" forward "$project_dir" docker compose \
      --project-directory "$project_dir" \
      -f "$compose_file" \
      "$@"
  fi
}

cmd_update() {
  require_backup_runtime
  [ -f "$env_file" ] || die "missing .env"
  cu_current=$(resolve_version)
  compose_file_for "$cu_current"
  cu_current_compose=$compose_file
  cu_requested=${KARPATHY_WIKI_CHANNEL:-$(read_env_value KARPATHY_WIKI_CHANNEL)}
  case "${cu_requested:-}" in
    ''|stable) cu_channel=stable ;;
    pre) cu_channel=pre ;;
    *) die "unsupported KARPATHY_WIKI_CHANNEL: $cu_requested" ;;
  esac
  if [ -n "${1:-}" ]; then
    validate_version "$1" || die "invalid version: $1"
    cu_target=$1
  elif [ "$cu_channel" = pre ]; then
    cu_target=$(resolve_pre_version) ||
      die "cannot resolve the latest pre-release"
  else
    cu_target=$(resolve_latest_version) || die "cannot resolve the latest release"
  fi
  # Download before touching .env so a failed update leaves it unchanged.
  compose_file_for "$cu_target"
  exec python3 "$script_dir/launcher_backup.py" update "$project_dir" \
    "$cu_current_compose" "$cu_current" "$cu_target" "$compose_file"
}

require_backup_runtime() {
  command -v python3 >/dev/null 2>&1 || die "Python 3.11+ is required for the launcher; install it on the host"
  [ -f "$script_dir/launcher_backup.py" ] || die "missing launcher_backup.py; install the launcher pair"
}

usage() {
  cat <<'EOF'
Karpathy Wiki launcher

Runs Docker Compose with the Compose file matching KARPATHY_WIKI_VERSION in
.env. The file is downloaded on demand and cached per version, so the project
directory only holds configuration and adoptions. A compose.override.yaml
next to this script is applied automatically.

Usage:
  karpathy-wiki.sh <compose args...>   Forward to docker compose (up -d, ps, logs, ...)
  karpathy-wiki.sh update [version]    Pin version (default: latest release, or
                                       latest pre-release when the pre channel is
                                       set); mandatory whole-instance backup,
                                       pull images, restart and verify health
  karpathy-wiki.sh backup list         List local update and rescue snapshots
  karpathy-wiki.sh launcher-accept <version>  Explicitly trust the installed pair
  karpathy-wiki.sh rollback <id>       Confirm full data/config/software rollback;
                                       creates a rescue snapshot first
  karpathy-wiki.sh version             Print the resolved version
  karpathy-wiki.sh help                Show this help

Environment:
  KARPATHY_WIKI_VERSION        Overrides the version pinned in .env; supports
                               latest and pre in addition to pinned versions
  KARPATHY_WIKI_CHANNEL        Update channel: stable (default) or pre; pre
                               resolves to the newest v*-pre.* pre-release
  KARPATHY_WIKI_PROJECT_DIR    Project directory (default: directory of this script)
  KARPATHY_WIKI_CACHE_DIR      Compose cache directory (default: <project dir>/.cache)
  KARPATHY_WIKI_BACKUP_DIR     Private local storage outside DATA_ROOT (default: ./backups)
  KARPATHY_WIKI_BACKUP_EXTRA_PATHS  JSON array of external proxy/config paths to include

Updates stop all instance writers. Backups contain plaintext secrets: use trusted,
preferably encrypted storage. Direct Compose calls bypass this safety guarantee.
Rollback loses all changes since the selected snapshot; no automatic rollback.
EOF
}

command=${1:-help}
# Never import a potentially mixed helper after interrupted activation.
if [ -f "$script_dir/.launcher-activation.json" ]; then
  die "interrupted launcher activation; run: python3 $script_dir/.launcher-stage/launcher_backup.py recover $project_dir"
fi
# Resolve configuration only after acquiring the installation-wide lock.
case "$command" in
  help|-h|--help) ;;
  *)
    require_backup_runtime
    if [ -z "${KARPATHY_WIKI_LOCK_FD:-}" ]; then
      exec python3 "$script_dir/launcher_backup.py" launch "$project_dir" sh "$script_dir/karpathy-wiki.sh" "$@"
    fi
    ;;
esac
case "$command" in
  help|-h|--help)
    usage
    ;;
  version)
    resolve_version
    ;;
  update)
    shift
    [ $# -le 1 ] || die "usage: karpathy-wiki.sh update [version]"
    cmd_update "${1:-}"
    ;;
  launcher-accept)
    shift
    [ $# = 1 ] || die "usage: launcher-accept <installed-launcher-version>"
    validate_version "$1" || die "invalid launcher version: $1"
    exec python3 "$script_dir/launcher_backup.py" accept "$project_dir" "$1"
    ;;
  backup|rollback)
    require_backup_runtime
    shift
    if [ "$command" = backup ]; then
      [ "$#" = 1 ] && [ "$1" = list ] || die "usage: karpathy-wiki.sh backup list"
      backup_action=list
      shift
    else
      [ "$#" = 1 ] || die "usage: karpathy-wiki.sh rollback <backup-id>"
      backup_action=rollback
    fi
    version=$(resolve_version)
    compose_file_for "$version"
    exec python3 "$script_dir/launcher_backup.py" "$backup_action" "$project_dir" "$compose_file" "$version" "$@"
    ;;
  *)
    version=$(resolve_version)
    compose_file_for "$version"
    run_compose "$@"
    ;;
esac
