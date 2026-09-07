# Changelog

All notable changes to `figma-to-wp` are documented here. Versioning is semver;
new capability → minor, fix/docs → patch.

## [0.3.1]

- Move the skill to `skills/figma-to-wp/`. It lived at the repo root, which
  Claude Code tolerates but Cowork does not: Cowork indexes only `skills/`, so
  it reported "This plugin doesn't have any skills or agents" and the skill was
  unusable there.
- The CLI locator follows the move — `plugin.json` now sits two levels above the
  script, and `-maxdepth` had to grow because Cowork nests a plugin under two
  session UUIDs, which put the script one level past the old limit.

## [0.3.0]

- Runs on the user's own Mac through a local terminal — the Desktop Commander
  connector in Cowork, the Bash tool in Claude Code. The script resolves itself
  by version across the org-managed, Claude Code and personal install paths, so
  nothing hardcodes a location and the newest copy wins.
- `extract` renders the canvas and crops it to the frame. Designers park pieces
  beside a frame and let them overlap it; a frame-only render was missing three
  feature cards, four product panels and a carousel button.
- `design.json` carries the copy verbatim, every box's geometry, the resolved
  type per text run, and the unresolved Figma comments.
- `diff` screenshots the built page and compares it to the render in a grid of
  bands and columns, and writes a side-by-side to look at.
- `verify` fails a design wider than 1140px whose wrapper does not take the
  width back from the theme's clamp.
- `push` keys its media manifest on file content, so a corrected asset under an
  unchanged filename actually reaches the site.
