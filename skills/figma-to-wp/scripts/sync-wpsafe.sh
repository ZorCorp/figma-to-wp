#!/usr/bin/env bash
# Copy wpsafe.py into mcp-wp with a header mcp-wp's test can check.
set -euo pipefail
dest="${1:?usage: sync-wpsafe.sh <path-to-mcp-wp>}"
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/wpsafe.py"
[ -d "$dest/app" ] || { echo "no app/ in $dest" >&2; exit 1; }
commit="$(git -C "$here" rev-parse --short HEAD)"
if ! git -C "$here" diff --quiet -- wpsafe.py; then
  echo "wpsafe.py has uncommitted changes — commit first so source-commit means something" >&2
  exit 1
fi
sha="$(shasum -a 256 "$src" | cut -d' ' -f1)"
{
  echo "# VENDORED from figma-to-wp/scripts/wpsafe.py — edit there, then run scripts/sync-wpsafe.sh"
  echo "# source-commit: $commit"
  echo "# sha256: $sha"
  cat "$src"
} > "$dest/app/wpsafe.py"
mkdir -p "$dest/tests/fixtures"
rm -rf "$dest/tests/fixtures/cd-78436"
cp -R "$here/../tests/fixtures/cd-78436" "$dest/tests/fixtures/"
echo "synced wpsafe.py @ $commit ($sha) and fixtures into $dest"
