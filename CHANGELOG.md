# Changelog

All notable changes to `figma-to-wp` are documented here. Versioning is semver;
new capability → minor, fix/docs → patch.

## [0.6.2]

- The plugin's own description still listed the 0.4.0 pipeline. `audit`,
  `preview`, `mobile` and `pull` have shipped since, and none of them appeared
  in `plugin.json`, in the marketplace entry, or in the `SKILL.md` frontmatter
  the model reads to decide whether the skill applies at all. Named them.

## [0.6.1]

- Tagged, and identical to 0.6.0. The description fix meant for it was
  committed onto a detached HEAD in the submodule checkout, so `git push
  origin main` pushed a branch that was already current, said "Everything
  up-to-date", and the release ran against the unchanged remote. Shipped as
  0.6.2 instead.

## [0.6.0]

Five faults, all of them invisible to every check that existed, all found by a
person looking at the page.

- **The canvas render was cropped from the wrong bounds.** `extract` renders the
  canvas and crops to the frame, and it placed the crop with the children's
  `absoluteBoundingBox`. Figma renders from `absoluteRenderBounds`, which
  includes whatever a shadow, blur or outside stroke bleeds past the layout box.
  One parked card bled 3px, so **every design.png this tool has cropped drew the
  whole page 3px right of where the frame puts it.** Nothing noticed:
  design.json is read from the node tree, so every number stayed correct while
  the only picture anyone compares against was wrong, and three separate "fixes"
  were made against it — each moving the page further from the frame while the
  overlay looked like it was closing. Fixing the crop moved a page from 92.6% to
  94.2% without touching a line of CSS, and dissolved two accepted "left side
  bearing" explanations that were never bearing at all.
- **`diff` measures sideways registration once for the whole page** and prints
  `register  design.png and the page agree sideways to the pixel`, or names the
  offset. A shift identical in every band is never a layout mistake. `audit`
  reads design.json and `diff` reads design.png; when the two disagree about
  where the page sits, the picture is the one that moved.
- **A hard break in the file is content, not whitespace.** A `U+2028` or newline
  inside a text node decides where the line ends. The copy check could not see
  one — Python counts U+2028 as whitespace, so `squash()` ate it — and writing
  it back into the markup as `&#8232;` does nothing either, because HTML treats
  it as an ordinary space. Both pricing notes on one page wrapped wherever the
  column ran out, one of them with the character sitting in the source and every
  check green. `verify` now splits each design string on its hard breaks and
  requires the markup to break at the same points, counting `<br>` and block
  boundaries alike.
- **`verify` warns when the page redraws an icon the file exported.** `extract`
  writes every vector to `assets/i*.svg` and nothing ever asked whether the page
  used them. A checklist tick was a rotated CSS border standing in for a 9×7
  polyline: it sat 3px low inside its own disc, the cross beside it was drawn
  grey where the file strokes white, and no geometry check could see any of it
  because the disc around them was the right size in the right place. The
  signature normalises each subpath into a unit box, so an icon inlined as a
  `data:` URI at a different scale still counts as used; `dropped.json` answers
  the warning for an icon the page deliberately does not draw.
- **`preview` serves the behaviour script it has always asked for.** It wrote
  `<script src="wpbuddy-page.js">` into every page and nothing fetched the file.
  Every preview of an interactive page 404'd that tag and ran dead — tabs did
  not switch, accordions did not open, carousels did not move — and it is silent
  by construction, because a carousel at rest looks like a carousel and the
  render still scores. The URL is discovered, not assumed: the enqueue is
  content-gated on `class="ptab` / `acc-item`, so the home page usually does not
  carry the tag.
- **`verify` checks the markup keeps its half of the behaviour contract**: a
  `.ptab[data-p="3"]` with no `#p-3`, a `[data-car]` naming an id that is not in
  the page, a `.mc-loop` with no `.mc-loop__track`.
- **`verify` warns about a `<button>` that states no `:hover`/`:focus`
  background.** `elementor-kit-7676` is on every page on the site and styles
  bare buttons: the site's orange at rest, `#12BF6D` on hover and focus. The
  rest state loses to any two-class rule, so it never appears; the hover and
  focus states carry a pseudo-class and outrank one, so the first click on a
  control turns it green. That shipped — a carousel's arrows, its dots and four
  tabs — and survived the build, the review and the push, because no static
  render ever enters that state.
- **`audit` checks where the copy breaks and which edge it is set against**
  (`lines`, `wrap width`, `align`), and stopped reporting a section's own drift
  as a box error.
- `references/site-rules.md` carries the Elementor kit's button selectors and
  the hard-break rule; `SKILL.md` carries the register check and "click it in
  the preview before calling a page done".

## [0.5.3]

The section gate could be passed by a page with three cards the wrong height,
because nothing was scoring the cards.

- **`audit` reports frames the page has nothing for.** It paired a page box to a
  frame on geometry and dropped anything too far out, which meant the worse a
  box was, the less likely it was to appear in the report — a container that
  shrank to its content, or a rule that was never built, simply vanished. The
  reverse list names the frame, its size, the nearest thing on the page and why
  they did not pair. It found three case cards 13px too tall on the first run.
- **Box findings feed the section gate.** `match` is a pixel measure; a card
  13px too tall moves it by a fraction of a point. `audit` now records per
  section how many boxes differ and how many frames found nothing, and a section
  cannot pass while either is non-zero. Figma's own wrappers — a Margin round a
  Background round an icon — are excluded, since none of them has or should have
  a CSS box.
- **The gate is 93, not 90.** Measured against the reference build's six
  sections: 83.6 / 89.9 / 91.7 / 93.8 / 94.1 / 94.8, median 93. 90 let a section
  through with a trial button at the wrong size and a missing rule.
- **95 is documented as unreachable.** No section of a page that shipped and was
  accepted gets there; the residue is glyph rasterisation, recompressed images
  and gradient dither. A gate that fails a correct page is a gate nobody reads.
  (This entry also blamed "a 3px left side bearing Figma and Chrome disagree
  on". There is no such disagreement — see 0.6.0. The reference was cropped
  3px. Sections now reach 96 and 97.)
- **Reading the overlay is now required for every section, every time** — not,
  as before, only when the numbers stall. Twice on one page the score was
  comfortable and the picture was not, and both times the picture was right.

## [0.5.2]

A page is now built and checked **one section at a time, top to bottom**, and
the section check that made that possible turned out to have been measuring the
wrong thing since it was written.

- **`diff --section` compared two different windows.** It cropped the design
  from its first string for the length of its text span, and the page from its
  section element's top for the height of that element's box — a different
  landmark and a different length. On a page built section by section, reviewed
  band by band and shipped, that fed 602px of design to 1052px of page and
  scored the section at **14%**. Both crops now start at the section's first
  string, which is the one landmark that exists on both sides, and run the same
  distance. The same six sections now score 84.0 / 89.9 / 91.4 / 93.8 / 94.1 /
  94.8 — which is what a correct section actually looks like.
- **A per-section gate at 90%**, calibrated on those numbers, with the 84 (a
  full-height gradient band, two rasterisers dithering differently) as the
  observed floor. A section under 85 with no line in `accepted.json` is a fault.
- **`verify --section` / `--through`.** Building a page one section at a time
  means every string below the one you are on is legitimately absent; a check
  that fails on all of them stops being read, and that is the one habit this
  tool cannot afford to teach. `--section` narrows the copy check to the band in
  hand, `--through` keeps everything above it in scope so an earlier section
  cannot rot while you work further down.
- `SKILL.md` rewrites the workflow around this. The argument for it is not
  tidiness: on a page built all at once, every correct fix to a section's height
  moves everything below and the whole-page score falls — 80.8 to 78.5, 73.5 to
  69.4, 87.3 to 79.9, three sourced fixes each punished by the number. Anyone
  following the number reverts all three. It also means each fault is solved
  once: a gutter putting every block 28px right was diagnosed at the end of one
  build and had to be separated from five other faults; caught in section 1 it
  is one line.

## [0.5.1]

A convergence number, so a page can be worked to a target instead of to "looks
about right", and two fixes to the measurement it rests on.

- `diff` reports **`match`**: how much of the overlay actually agrees, plus a
  per-band breakdown and every band more than 15 points under that page's own
  median. Calibration, so nobody chases the wrong target: a page built to
  `dx +0, dy ±2`, reviewed band by band and shipped, scores **90.9%** whole-sheet
  and **93.9%** at the median band. The last ten points are two rasterisers,
  a recompressed WebP, gradient dithering and a frame taller than the page.
  99% means you are comparing something to itself.
- Read relative, it finds what nothing else does. A band at 45.7% on one page
  was a heading boxed at 900px where the frame says 1051 — so it wrapped to
  three lines instead of two — and a carousel showing three cards where the
  design shows four. `verify` was 140/140 and `audit` found nothing there.
- `accepted.json` — bands that cannot converge, keyed by a y inside the band,
  with the reason. `diff` labels them and stops counting them as unexplained.
  The geometry counterpart of `dropped.json`.
- **Desktop first, phone after it is confirmed.** Every phone rule restates a
  desktop value — the line-heights most of all, each one an absolute px lifted
  from the frame — so a desktop still in motion invalidates them as fast as they
  are written. On one page a hero lead went 18/24 → 20/32 → 18/24 across two
  rounds and every phone override keyed to it was written twice. They are also
  different kinds of work: the desktop converges against a reference, the phone
  is a checklist against the house spec with no Figma reference at all.
  `diff` now writes `diff/state.json`, and `mobile` reads it and says when the
  desktop has not settled.
- `diff` compares against the previous run and says when nothing moved, so a
  stalled loop stops instead of going round again on feel.
- `SKILL.md` documents the loop: gate the instrument, measure, take the lowest
  unexplained band, name what is in it, fix by a **sourced** number, re-measure.
  Stop when every band is explained or two rounds do not move `match`.
- **`audit`'s origin was placed by nodes that cannot vote.** It took the median
  x offset over every matched pair, including strings inside a rotating row —
  whose x is wherever the rotation left it. On one page 35 of 64 otherwise
  unique pairs were carousel cards clustered at +157 and +189, and they alone
  set the origin; the page's real offset was +4, so **every x finding was
  reported 157px wrong** and most of the "distinct fixes" list was that one
  number wearing different names. The origin now uses only pairs that are
  unique on both sides, outside any `.mc-loop`, and inside the frame.
- `measure.js` marks runs inside a rotating container, which is what makes that
  possible.

## [0.5.0]

Everything here comes from one page built end to end. The theme running through
it is measuring the wrong thing: a fallback font, a frozen carousel, a collapsed
accordion, a preview with no site stylesheet, a 1x1 lazyload placeholder, a
viewport that was never the width it claimed. Each one is now a check rather
than a thing you have to remember.

### Measuring what is actually on the screen

- `audit` waits for `document.fonts.ready` before measuring, and reports whether
  the page's own font arrived. Headless Chrome never received the webfont, so
  every rect described a wider fallback and boxes were being widened to fit a
  font that was never missing on the site.
- `preview` freezes what moves: carousel loops stripped, accordions forced open,
  shortcodes substituted for a stub, and the live page's stylesheet fetched into
  `build/<slug>/site/`. A page can only be compared against a still.
- `diff` writes `overlay.png` — design in red, page in cyan, grey where they
  agree — for the point where band scores stop moving and you still cannot see
  why. Read the colour against the background: red is whichever side has the
  *darker* pixel, so it flips on a dark ground.
- `dump_dom` hides scrollbars, so `audit` and `diff` measure the same width.

### Seeing a change instead of guessing at one

- `extract` keeps the previous pull as `design.prev.json` and prints what moved:
  strings added, strings gone, boxes added, boxes gone, and text that kept its
  content but changed position. A section redesign came back as three lines.
- `audit`'s section table gains `drift` (how far a section sits from where the
  frame puts it, same anchor both sides) and `step` (the change against the
  section above). `diff` lets every band find its own best fit, which is why it
  cannot see a section that starts in the wrong place. Deleting a node in Figma
  leaves the hole open; deleting it in HTML collapses everything below.

### The phone

- New `mobile` command: sideways overflow, body copy under 16px, tap targets
  under 40px, content against the screen edge, and images whose rendered box
  contradicts the file's shape.
- It renders into an **iframe** of the requested width. Headless Chrome will not
  open a window narrower than 500px, so `--width 390` had been measuring 500 and
  every query below that breakpoint stayed shut — a mobile layout that could not
  be reproduced in a real browser.
- `references/site-rules.md` records the house phone layout, measured off
  `/solutions/geospatial/`: breakpoints 768 and 1024, 28px gutter, 50px
  sections, three-step type scale, 16px floor for body copy, and the fixed 81px
  site header a first section has to clear.

### Images

- `verify` warns when an `<img>`'s box still depends on the file's own
  dimensions. SiteGround lazy-loads: until an image scrolls in its `src` is a
  1x1 GIF, and `width`/`height` attributes do not help because they apply only
  while an image is *unloaded* — so `height:auto` reserves a square. It shipped
  three times on one page. The check walks the markup keeping ancestor classes,
  so a rule written against a wrapper still counts.

### Extraction

- `collect_frames` keeps small filled leaves that sit in real layout — dots,
  bullets, rules. Five 16px and 10px ellipses were being dropped by `min_side`
  and a whole switcher had to be measured out of the API by hand. Icon interiors
  are still skipped.

### Housekeeping

- The browser-side measurement moved out of a Python string into `measure.js`.
  183 lines of JavaScript in triple quotes gets no syntax check: a call to a
  helper that did not exist parsed fine and surfaced as "no `fw-measure` node in
  the DOM". `doctor` runs `node --check` on it.
- `push` reads `modified_gmt` after the post-meta write and the permalink
  regeneration, both of which bump it. Every push had been tripping its own
  "the page moved on" guard.

## [0.4.0]

- `pull` brings a page that already exists in WordPress down to a build folder,
  so a page nobody has a `build/` for — someone else's, one edited in the admin,
  one from before this tool — can be edited at all. Takes a post id, a URL, a
  slug or a title; titles match post titles only, because the REST `search` also
  matches post content and does not rank by title. Round-trip verified
  byte-identical on a 37KB page.
- `push` refuses two things it used to do silently. It stops when WordPress has
  moved ahead of the build, before uploading anything, because site revisions
  are off and an overwrite is unrecoverable. And the build folder name no longer
  decides the URL of an existing page: pushing `build/pulltest` back to a live
  page renamed it to `/pulltest/` and 404'd the real URL.
- `push` reads the page from `wp.json`, so `--post-id` is no longer needed after
  a `pull`.
- The Figma node cache is keyed to the file's version. It had no expiry of any
  kind while the render was always fetched live, so the numbers could describe
  one version of a design and `design.png` show another — with every check
  downstream agreeing with itself and being wrong.
- `doctor` checks python, Pillow, Chrome, cwebp and poppler as well as the two
  APIs. Pillow and Chrome read as optional and are not: `extract` crops the
  canvas render with Pillow, and `diff` cannot render a page without Chrome.

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
