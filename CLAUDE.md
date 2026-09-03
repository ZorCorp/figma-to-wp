<!-- BEGIN zorskill-release (managed by zorskill-dev) -->
## Releasing this plugin

This repo is a **ZorCorp marketplace plugin**, published via the `zorskill` marketplace.
Releases are deliberate and self-service — do **not** hand-edit the version or touch the
marketplace repo.

**To cut a release** (when a change is worth shipping to users):

    gh workflow run release.yml -f version=<x.y.z>    # semver, no leading "v"

That workflow bumps `.claude-plugin/plugin.json`, commits, and tags `v<x.y.z>`. The zorskill
marketplace's drift scanner then carries it in automatically within ~30 min (forward-only,
validated). You never edit `marketplace.json` or open the marketplace repo.

- Semver: **patch** for a fix, **minor** for a feature, **major** for a breaking change.
- Don't bump `plugin.json` by hand — the workflow owns it. A change without a release stays
  in this repo and never reaches users; run the workflow when you want it shipped.

**If Claude Code is assisting here:** after a shippable change is committed, remind the user
they can release with the `gh workflow run release.yml` command above and help them pick the
semver bump. Do not edit the version file or the marketplace directly.
<!-- END zorskill-release (managed by zorskill-dev) -->
