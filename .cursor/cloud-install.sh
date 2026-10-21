#!/usr/bin/env bash
# Idempotent Cloud Agent install. A Build runs this. Later agents start from the disk.
# stario-create owns this file. `stario-create sync` rewrites it.
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

if [ ! -f "${HOME}/.profile" ] || ! grep -q '.local/bin' "${HOME}/.profile"; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.profile"
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

uv python install 3.14
if [ -f uv.lock ]; then
  uv sync --all-groups --locked || { echo "uv.lock is stale; resolving again" >&2; uv sync --all-groups; }
else
  uv sync --all-groups
fi

if ! command -v just >/dev/null 2>&1; then
  uv tool install rust-just
fi

if [ -f frontend/src/input.css ] && just --summary 2>/dev/null | tr ' ' '\n' | grep -qx tailwind; then
  just tailwind
fi
