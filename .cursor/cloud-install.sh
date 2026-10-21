#!/usr/bin/env bash
# Idempotent Cloud Agent install. A Build runs this. Later agents start from the disk.
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

if [ ! -f "${HOME}/.profile" ] || ! grep -q '.local/bin' "${HOME}/.profile"; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.profile"
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

uv python install 3.14
uv sync --all-groups

if ! command -v just >/dev/null 2>&1; then
  uv tool install rust-just
fi
