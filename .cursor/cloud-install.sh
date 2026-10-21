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

TAILWIND_VERSION="v4.3.3"
if [ -f frontend/src/input.css ] && [ ! -x ./tailwindcss ]; then
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64) asset="tailwindcss-macos-arm64" ;;
    Darwin:x86_64) asset="tailwindcss-macos-x64" ;;
    Linux:x86_64 | Linux:amd64) asset="tailwindcss-linux-x64" ;;
    Linux:aarch64 | Linux:arm64) asset="tailwindcss-linux-arm64" ;;
    *) echo "no Tailwind build for $(uname -s) $(uname -m)" >&2; exit 1 ;;
  esac
  mkdir -p "${HOME}/.local/bin"
  curl -fL --retry 3 -o "${HOME}/.local/bin/tailwindcss" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
  chmod +x "${HOME}/.local/bin/tailwindcss"
  ln -sfn "${HOME}/.local/bin/tailwindcss" tailwindcss
fi
