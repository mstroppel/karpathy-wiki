#!/bin/sh
set -eu

repository="mstroppel/karpathy-wiki"

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
  if [ -f "$override_file" ]; then
    docker compose \
      --project-directory "$project_dir" \
      -f "$compose_file" \
      -f "$override_file" \
      "$@"
  else
    docker compose \
      --project-directory "$project_dir" \
      -f "$compose_file" \
      "$@"
  fi
}

set_env_version() {
  [ -f "$env_file" ] || die "missing $env_file; create it from .env.example first"
  cp -f "$env_file" "$env_file.bak"
  chmod 0600 "$env_file.bak" 2>/dev/null || true
  sev_tmp="$env_file.tmp.$$"
  if grep -q '^KARPATHY_WIKI_VERSION=' "$env_file"; then
    sed "s/^KARPATHY_WIKI_VERSION=.*/KARPATHY_WIKI_VERSION=$1/" "$env_file" >"$sev_tmp"
  else
    cp "$env_file" "$sev_tmp"
    printf 'KARPATHY_WIKI_VERSION=%s\n' "$1" >>"$sev_tmp"
  fi
  if ! grep -q '^OPENCODE_PASSWORD=.' "$sev_tmp"; then
    sev_password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    if grep -q '^OPENCODE_PASSWORD=' "$sev_tmp"; then
      sed "s/^OPENCODE_PASSWORD=.*/OPENCODE_PASSWORD=$sev_password/" \
        "$sev_tmp" >"$sev_tmp.password"
      mv -f "$sev_tmp.password" "$sev_tmp"
    else
      printf 'OPENCODE_PASSWORD=%s\n' "$sev_password" >>"$sev_tmp"
    fi
  fi
  chmod 0600 "$sev_tmp"
  mv -f "$sev_tmp" "$env_file"
}

cmd_update() {
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
  set_env_version "$cu_target"
  printf 'Pinned KARPATHY_WIKI_VERSION=%s in %s (backup: %s)\n' \
    "$cu_target" "$env_file" "$env_file.bak"
  run_compose pull
  run_compose up -d
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
                                       set) in .env, back up .env, pull images,
                                       and restart (up -d)
  karpathy-wiki.sh version             Print the resolved version
  karpathy-wiki.sh help                Show this help

Environment:
  KARPATHY_WIKI_VERSION        Overrides the version pinned in .env; supports
                               latest and pre in addition to pinned versions
  KARPATHY_WIKI_CHANNEL        Update channel: stable (default) or pre; pre
                               resolves to the newest v*-pre.* pre-release
  KARPATHY_WIKI_PROJECT_DIR    Project directory (default: directory of this script)
  KARPATHY_WIKI_CACHE_DIR      Compose cache directory (default: <project dir>/.cache)
EOF
}

command=${1:-help}
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
  *)
    version=$(resolve_version)
    compose_file_for "$version"
    run_compose "$@"
    ;;
esac
