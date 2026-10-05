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
# Fixtures: the synthetic bad-*.html ship with this repo; the real-page ones
# (export, pushed-before, expected-body) are page data and live in the private
# page repo — pass their folder as FIGMA_WP_PAGE_FIXTURES, or run from inside
# mcailab/figma-page-build, where they are found automatically. Existing copies
# in mcp-wp are kept when they cannot be found.
fx="$dest/tests/fixtures/cd-78436"
mkdir -p "$fx"
cp "$here"/../tests/fixtures/cd-78436/bad-*.html "$fx/"
page="${FIGMA_WP_PAGE_FIXTURES:-$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import page_fixtures as p; print(p.PAGE_FIX)' "$here/../tests")}"
if [ -n "$page" ] && [ -d "$page" ]; then
  cp "$page"/export.html "$page"/pushed-before.html "$page"/expected-body.html "$fx/"
  echo "page fixtures from $page"
else
  echo "page fixtures not found — kept whatever $fx already had" >&2
fi
echo "synced wpsafe.py @ $commit ($sha) and fixtures into $dest"
