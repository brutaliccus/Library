#!/usr/bin/env bash
# Rename audiobook staging from _unorganized → .unorganized (ABS ignores dot dirs).
# Safe to re-run. Host path defaults to /mnt/Audiobooks.
set -euo pipefail

AUDIOBOOKS_HOST="${AUDIOBOOKS_HOST:-/mnt/Audiobooks}"
NEW="${AUDIOBOOKS_HOST}/.unorganized"
OLD="${AUDIOBOOKS_HOST}/_unorganized"

if [[ ! -d "${AUDIOBOOKS_HOST}" ]]; then
  echo "WARN: ${AUDIOBOOKS_HOST} missing — skip staging migrate"
  exit 0
fi

mkdir -p "${NEW}"
touch "${NEW}/.ignore"

if [[ -d "${OLD}" ]]; then
  shopt -s nullglob
  moved=0
  for d in "${OLD}"/req_*; do
    base="$(basename "$d")"
    dest="${NEW}/${base}"
    if [[ -e "${dest}" ]]; then
      echo "==> Skip (already exists): ${base}"
      continue
    fi
    mv "$d" "${dest}"
    echo "==> Migrated ${base} → .unorganized/"
    moved=$((moved + 1))
  done
  shopt -u nullglob
  # Remove empty legacy root (or root that only has a leftover .ignore)
  if [[ -d "${OLD}" ]]; then
    leftover="$(find "${OLD}" -mindepth 1 -maxdepth 1 ! -name '.ignore' 2>/dev/null | head -1 || true)"
    if [[ -z "${leftover}" ]]; then
      rm -f "${OLD}/.ignore" 2>/dev/null || true
      rmdir "${OLD}" 2>/dev/null || true
      echo "==> Removed empty legacy ${OLD}"
    else
      echo "==> Left non-empty legacy ${OLD} (inspect manually)"
    fi
  fi
  echo "==> Migrated ${moved} staging folder(s)"
else
  echo "==> No legacy ${OLD}"
fi

echo "==> Staging root ready: ${NEW}"
