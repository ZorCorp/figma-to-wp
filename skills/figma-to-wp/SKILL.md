---
name: figma-to-wp
description: "Build or update a masterconcept.ai WordPress page from a Figma design. Pulls the frame's render, its copy verbatim, its colour and type tokens, its box geometry and its images out of Figma; you write the HTML against those numbers, one section at a time. `verify` confirms every string survived and the site's hard rules hold, `audit` names which element is wrong and by how much, `diff` screenshots the result and scores each section against the render, and `mobile` runs the phone checks the frame cannot answer; then it uploads the media and creates the page as a draft via mc/set-post-html with the right slug and Permalink Manager URI. `pull` starts from a page that already exists in WordPress. `extract --from elementor` takes a live Elementor page on the site as the design instead — its render at 1440, 768 and 390, its copy, widgets and responsive rules — to build a new raw-HTML page from it (another language, joined to its WPML group) or, after a reviewed draft, to replace the Elementor page itself with `push --replace` (`elementor-restore` puts it back). Use when someone gives a Figma link and wants it turned into a page on masterconcept.ai, wants an existing AI-built page refreshed from an updated design, or wants an Elementor page rebuilt as raw HTML."
license: MIT
allowed-tools:
  - Bash(*)
  - Read(*)
  - Write(*)
  - Edit(*)
metadata:
  version: "0.7.0"
---

# figma-to-wp

```
extract   figma frame  ->  design.png · design.json · assets/
          re-run on a changed file and it prints what moved
plan      you look at design.png and write down the bands
author    one section at a time, top to bottom — never the whole page at once
verify    every string present, site rules respected (--section while building)
preview   render it, compare against design.png, fix, repeat
diff      overlay match, per band and per section, with a 90% gate
audit     which element is wrong, by how much, and has a section drifted
mobile    the phone checks the frame cannot answer — after desktop is confirmed
push      media -> draft page -> permalink

extract --from elementor   a live Elementor page as the design: 1440 · 768 · 390
push --replace <id>        swap that Elementor page for the reviewed draft
elementor-restore          put a replaced page back on Elementor
```

The script fetches; it does not interpret. **You** decide what the layout is, by
looking at `design.png`. **The script** owns the things with exactly one right
answer: the copy, the hex values, the type scale, where each asset went.

> An earlier version tried to infer layout — segmenting the frame into sections,
> grouping siblings into rows, emitting a nested spec tree. It produced a
> confident and completely wrong page. Outside auto-layout subtrees the only
> honest source of layout is the render. Do not rebuild that machinery.

Same behaviour in Claude Code and Claude Cowork: stdlib Python over HTTPS, no
MCP server needed.

## Where commands run

Both hosts run on the user's own Mac:

| Host | The terminal is | Credentials |
|---|---|---|
| Claude Code | the **Bash** tool | `~/.figma-wp/.env`, or the folder `$FIGMA_WP_HOME` names |
| Claude Cowork | the **Desktop Commander** connector | `~/.figma-wp/.env` |

In Cowork, use Desktop Commander and never Cowork's own sandboxed shell: that
shell reaches a different machine from the one holding `~/.figma-wp/.env`.

## Locating the CLI

The script ships with the skill and is already on disk. **Never download it.**
Resolve it by version so the newest wins when several copies exist:

```sh
FW=$(find -H "$HOME/Library/Application Support/Claude/local-agent-mode-sessions" \
          "$HOME/.claude/plugins/cache/zorskill/figma-to-wp" \
          "$HOME/.claude/skills/figma-to-wp" \
          ".claude/skills/figma-to-wp" \
          "figma-to-wp/skills/figma-to-wp" \
          -maxdepth 8 -path '*/scripts/figma_to_wp.py' 2>/dev/null | while IFS= read -r s; do
  d=${s%scripts/figma_to_wp.py}
  v=
  # In a plugin the script sits at <root>/skills/figma-to-wp/scripts/, so
  # plugin.json is two levels up; a bare personal skill has none and scores 0.0.0.
  for j in "${d}../../.claude-plugin/plugin.json" "${d}.claude-plugin/plugin.json"; do
    [ -f "$j" ] || continue
    v=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$j" | head -1)
    [ -n "$v" ] && break
  done
  printf '%s\t%s\n' "${v:-0.0.0}" "$s"
done | sort -V | tail -1 | cut -f2)
[ -n "$FW" ] || { echo "figma-to-wp CLI not found" >&2; exit 1; }
python3 "$FW" doctor
```

The first path is where an org-managed plugin lands (Claude Desktop and Cowork);
the second is Claude Code's; the third is a personal install; the last two are a
page repo that carries this skill as a submodule (`figma-to-wp/`) and symlinks it
into `.claude/skills/` — `-H` makes `find` follow that link. Cowork nests a
plugin under two session UUIDs, which is why `-maxdepth` has to be this generous.

`find` is used rather than a shell glob on purpose. Under `zsh` — Desktop
Commander's default shell — a glob that matches nothing aborts the whole loop,
so a Mac that has Cowork but not Claude Code would report the CLI missing when
it is in fact installed.

If nothing is found, the skill has not reached this host yet: say so and ask the
user to reopen the session or refresh plugins. In a page repo that carries the
skill as a submodule, `git submodule update --init` brings it in. Do not try to
work around it.

## Where the work lands

The script keeps no state of its own. Each page's working files go in
`build/<slug>/` **relative to the directory you run from**, not next to the
script. Credentials sit in `~/.figma-wp/.env` (mode 600), or in the folder
`$FIGMA_WP_HOME` points at — a page repo keeps its own `.figma-wp/.env`.

On a Mac, pick a working directory with the user and stay in it — `~/figma-to-wp`
is a reasonable default on a Mac that has no repo for this.

When the working directory is a git repo that keeps `build/` — a team's page
repo — commit at each of these five boundaries, `git pull --rebase` first so two
pages being built at once never collide:

| After | Message | What lands |
|---|---|---|
| `extract` | `<slug>: extract` | `design.png`, `design.json`, `assets/` |
| a section passes its own `diff` | `<slug>: section <name>` | `page.html`, `page.css`, `accepted.json` |
| the desktop gate passes | `<slug>: desktop <score>%` | the same three |
| `mobile` passes | `<slug>: mobile` | `accepted.json` |
| `push` | `<slug>: push draft <post id>` | `wp.json`, `manifest.json`, `backups/` |

Picking work back up needs no one to explain where it got to. Three files say so:

- `accepted.json` — which sections are signed off, and what each scored
- `manifest.json` — which assets are already in the media library, with their ids
- `wp.json` — whether a draft exists yet, and its post id

`diff/`, `site/` and `preview.html` are deliberately git-ignored. They are
regenerated on every run, and `diff/` alone is several megabytes of PNG rewritten
dozens of times per page — committing it would bury the repo in noise it can
always rebuild.

`references/site-rules.md`, beside the script, carries the rules that are about
masterconcept.ai rather than about Figma: what KSES strips, the theme's width
clamp, which shortcodes exist, how links have to be written for WPML. Read it
before authoring, not after a push goes wrong. `references/elementor-widgets.md`
is its counterpart for a build whose source is an Elementor page: how each
widget is written as raw HTML.

## Ask for these before you start

None of it is guessable, and guessing wastes far more of the user's time than
asking. Collect whatever is missing up front, in one go:

| Ask | Why it cannot be inferred |
|---|---|
| **The Figma link** | Ask for the **page**, not one frame. Carousel slides, variants and card rows are routinely parked outside the main frame, and a frame-only extract loses them silently. A page URL (`?node-id=0-1`) is the right input. |
| **Page title** | |
| **URL slug** | |
| **Parent path**, e.g. `partners/work-collaboration` | Pass it to `push --parent-path`; it resolves to an id by walking the path. |
| **Language** | A WPML language code of the site — `zh-hant`, `zh-hk` and so on. Defaults to `en`. `push --language` checks it against the languages the site actually has before it writes anything; `BAD_LANGUAGE` lists them. |
| **Is the source an Elementor page? Usage A or B?** | **A** builds a new raw-HTML page from it — usually another language, joined to the original's WPML group — and leaves the original alone. **B** turns the Elementor page itself into raw HTML at the same id and URL, and only after the user has signed off a draft. They push differently, and B overwrites a live page. See [From Elementor](#from-elementor). |
| **Usage A: the copy verbatim, or translated?** If translated: from the user (a file, or pasted), or drafted by Claude and confirmed by the user line by line | Decides whether the page has a `copy.json` and who stands behind its words. Usage B is always verbatim. |
| **The WordPress username** | **Ask. Never guess.** A wrong username returns `rest_not_logged_in`, which looks exactly like a server misconfiguration — chasing that instead of asking cost an hour in the session that produced this skill. |
| **Which author should own the page** | The Style Kit CSS is author-gated to `wpbuddy` / `mcp` (see `design-system.php`). Any other author means `wpbuddy-design.css` does not load and the page is on its own for styling, and it will not appear in WP Buddy → AI Builder. Fine if the page carries all its own CSS — but say so rather than let it surprise them. |
| **A throwaway slug first?** | Offer it. Publishing into the real URL later is one `--page-slug` away, and a test slug cannot collide with a live page. |

Also tell them, before you touch the site, which interactive behaviours the
design needs (`.acc`, `.ptab`, `.mc-loop`, `data-tabs-loop`) and whether
`wpbuddy-page.js` on production already has them. If it does not, the page
publishes looking right but sitting dead, and that reads as a broken build.

## Prerequisites

Run `doctor` first. It checks all of this and names whatever is missing:

```
python      ok  (3.14.3)      3.9+; the script is stdlib only
pillow      ok  (12.2.0)      pip3 install Pillow
chrome      ok  (Google Chrome)
cwebp       ok  (yes)         optional — Pillow already covers WebP
poppler     ok  (yes)         optional — only to import from a PDF export
figma       ok  (…)           token in ~/.figma-wp/.env
wordpress   ok  (…)           application password, and the username — ask, never guess
abilities   ok  (50)          WP Buddy reachable
```

Pillow and Chrome read as optional and are not. `extract` crops the canvas
render with Pillow; without it `design.png` is the whole canvas, which is the
wrong picture to build against and does not announce itself. `diff` has no way
to render a page without Chrome, and `diff` is not optional either.

They are installed by hand once on the Mac (`brew install`, `pip3 install
Pillow`, Chrome).

In Cowork the Mac also needs Claude Desktop with the **Desktop Commander**
connector connected — the only thing anyone installs by hand. In Claude Code
the Bash tool already is the terminal.
## 1. Extract

Two inputs work, and **the export folder is the better one**.

### From a Figma UI export (preferred)

Ask for the whole Figma page, not just the frame — carousel slides, variants and
alternate states are usually separate artboards sitting *outside* the main
frame, and a frame-only export silently loses them.

In Figma: select everything on the page and export **twice, into the same
folder** —

- **PDF** — for the page frame. It carries the copy as real selectable text.
- **PNG** — for the artwork. A PDF *render* flattens onto white, so an image
  exported that way arrives sitting in a white box; the PNG keeps its alpha.
  Where both exist for an artboard the PNG wins.

Then:

```bash
python3 "$FW" extract "~/Downloads/<export folder>" \
    --slug <slug> --frame "<name of the page frame>"
```

Why PDF: a UI export costs **nothing** against the API quota, and a PDF carries
the copy as real selectable text, so the strings are exact without an API call.
`--frame` picks which PDF is the page; the default is the largest page.

### From the API

```bash
python3 "$FW" extract "<figma-frame-url>" --slug <slug>
```

The node response is cached under `~/.figma-wp/cache/`, keyed to the file's
version. A cache written before someone edited the file is refetched, not
reused — the render is always fetched live, so a stale cache would describe one
version of the design while `design.png` showed another, and every check
downstream would agree with itself and be wrong. If Figma will not answer
`/versions` (rate limit, no network) the cache is used and says so.

The URL must point at a **frame** — `?node-id=0-1` is the canvas. This is the
only path that recovers the shared **style tokens** (named colours, type scale),
so it is worth running once even when the export folder is the real source; the
token table is preserved when a later export-mode run rewrites `design.json`.

Beware the quota: it is cost-based, shared between reads and renders, and small
on a viewer seat. Spending it on a burst of icon renders locks out plain file
reads for a long while. The node response is cached in `~/.figma-wp/cache/`.

### Either way, you get `build/<slug>/`

| File | What |
|---|---|
| `design.png` | the frame at 1×. **This is the spec.** |
| `design.json` | `texts` (verbatim, with `case` and `runs` — one text node can hold several type styles), `tokens` (colour + type + effects), `frames` (**every box's x/y/w/h, padding, gap, radius, fills**), `comments`, `assets` |
| `design.prev.json` | the previous pull, kept whenever `extract` overwrites one |
| `assets/` | `a*.png` images, `i*.svg` icons |

Re-running `extract` on a file the designer has changed prints what moved:
strings added, strings gone, boxes added, boxes gone, and anything that kept its
text but changed position. Read that list before you touch the page — it is the
implementation brief. A redesign of one section came back as three lines: five
labels added at x95, the rail's nine nodes deleted, the text column moved from
x141 to x179.

**A deletion is the change to watch.** Figma is absolutely positioned, so
removing a node leaves the hole open and nothing below it moves. The same edit
in HTML collapses everything below by the height of what you removed — 74px, in
that case — and `verify` cannot see it because the copy still matches. Check the
`drift` column in `audit` afterwards.

You write three more:

| File | What | Where it ends up |
|---|---|---|
| `page.html` | structure, with a `{{styles}}` placeholder | `post_content` |
| `page.css` | the stylesheet | inlined at `{{styles}}` |
| `page.js` | *optional*, one-off behaviour only | post meta, printed from `wp_footer` |

`verify`, `preview` and `push` all work on the **assembled** result, never on
`page.html` alone — a mistake introduced by assembly has to be caught by the
same check that catches everything else.

`page.js` must not be inlined into the body: `<script>` survives `post_content`,
but wptexturize loses the block as soon as the code contains a raw `<` (a
for-loop is enough) and rewrites every following `&` to `&#038;`, so `&&`
becomes a syntax error. `push` sends it to post meta and WP Buddy prints it in
the footer, which never passes through `the_content`. **Reusable behaviour still
belongs in `wpbuddy-page.js`** — `page.js` is for the genuinely page-specific.

For PDF import: poppler (`pdftotext`, `pdftoppm`) or `pypdf`. Without either,
the command says so instead of guessing.

## The one hard rule

**The design file is the authority on what the page says.** Copy comes from
`design.json` → `texts`, verbatim. Not retyped off the render, not improved,
not written to fill a gap.

When the file is wrong — and it is, often; frames get repurposed and the old
page's words stay behind — that is a finding to report, not a hole to patch.
An Asana page shipped with a hero headline and lede that appear nowhere in its
Figma file, written because the file's own hero still said Superhuman. The
words read well. Nobody could say who approved them, or check them against the
partner's guidelines, because they had no source.

`verify` catches this: a string in the file that is not on the page is a FAIL,
and `push` now refuses to run while any check fails. Copy that is genuinely
meant to be left out — the site header the frame draws, a block parked outside
the frame from another page — goes in `build/<slug>/dropped.json` with the
reason, which is reviewable. `--force` pushes past the checks and leaves no
record that it did.

## 2. Read the comments, then author `build/<slug>/page.html`

**Read `design.json` → `comments` before you write anything.** Designers leave
behaviour there that exists nowhere else in the file, and geometry cannot show
it. On the page this skill was built against, four unresolved comments carried
four requirements that were otherwise invisible:

| Comment | What it meant |
|---|---|
| 循環按鈕 | the product switcher auto-advances and wraps |
| hover 字體變藍 | the card link turns blue on hover, not orange |
| 下面的按鈕皆連接到下面的詳細內容 | each card links to its detail panel |
| 點擊＆打開字體變成橘色 | an opened FAQ title turns orange |

Every one of them was missed on the first pass and had to be corrected after
review. `extract` now prints them; do not skip them.

**The comments are also where every button's destination lives.** A comment
holding a URL is that button's `href`, and the button opens it in a new tab
(`target="_blank" rel="noopener"`). A button with no URL commented on it goes to
the shared contact popup for that language. Never leave one on `href="#"` — it
renders identically to a working button, so `diff` scores it perfect and the
dead link ships. In-page anchors and popup triggers keep their `#…` href and
never take `_blank`. See `references/site-rules.md`.

**And every `<img>` needs a non-empty `alt` *and* a non-empty `title`** —
decorative icons included, since `title` is what shows on hover and `alt` never
does. Take the words from the copy beside the image. Do not put
`pointer-events:none` on any of them: it is the obvious rule for a decorative
watermark and it kills the tooltip along with the click. And do not try to
confirm a tooltip from the DOM or a headless screenshot — neither can see one.
`references/site-rules.md` has the way that works, and the two traps in it.

**Then look at `design.png`.** Read it top to bottom and write down the
structure before writing markup: how many bands, where each one starts and ends,
which are full-bleed and which are inset boxes, the container width, the column
split. Measure if you are unsure — the PNG is 1440 wide, so pixel positions in
it are the design's own coordinates.

Then write the HTML, taking every string from `design.json` — never retype copy
off the render, especially Chinese, where a substituted character is invisible
in review and wrong on the live site.

### One section at a time, top to bottom

**Do not build the whole page and then fix it.** Build the first section, get it
past its gate, freeze it, and only then start the second. The order is forced:
a section's vertical position depends on every section above it, so section 3
cannot be checked until 1 and 2 are settled.

The reason is not tidiness. On a page built all at once, every correct fix to a
section's height moves everything below it, and the whole-page score *falls*:

```
80.8 -> 78.5   after the case cards were given the frame's own 200px body box
73.5 -> 69.4   after the FAQ band was restored to the frame's 602px
87.3 -> 79.9   after the pricing block was given its real 120px top gap
```

Three correct, sourced fixes, all punished by the number. Anyone following the
number would have reverted all three. Section by section, a section is compared
against its own crop and nothing below it can contaminate the reading.

It also means each error is solved once. A gutter that put every block 28px
right was diagnosed at the very end of one build, and had to be separated from
five other faults first; caught in section 1, it is one line and never recurs.

```bash
# while building section k
python3 "$FW" verify <slug> --section <name>      # only this band's copy
python3 "$FW" verify <slug> --through <name>      # ...and everything above it
python3 "$FW" audit  <slug> --url … --section <name>
python3 "$FW" diff   <slug> --url … --section <name>
```

`diff --section` crops **both** sides from the same landmark — the section's
first string — and runs both for the same distance, then prints a gate:

```
gate      np-pricing passes at 91.3% — freeze it and start the next section down
```

**A section built to the frame scores 93 or better.** Calibrated against a page
that was built this way, reviewed band by band and shipped: its six sections
score 83.6 / 89.9 / 91.7 / 93.8 / 94.1 / 94.8, median 93. The 83.6 was a
full-height gradient band, where two rasterisers dither differently — that is
the floor, and it needs a line in `accepted.json` saying so.

**Do not aim for 95.** No section of that shipped page reaches it. The residue
is glyph rasterisation, a 3px left side bearing that Figma and Chrome disagree
on, recompressed images and gradient dither. A 95 gate fails a page that is
right, and a gate that fails everything is a gate nobody reads.

### The number cannot see a container

`match` is a pixel measure. A card 13px too tall moves it by a fraction of a
point, so a section can gate in the low 90s with three cards the wrong height.
That happened here: every text run in the pricing section measured within 4px
while the middle card was short by the height of its own bottom padding, and
nothing said so.

So the gate also counts what `audit` found in the boxes, and `audit` now reports
**frames the page has nothing for**. It used to pair a page box to a frame on
geometry and drop anything too far out — which meant the worse a box was, the
less likely it was to appear in the report at all. The reverse list is where a
missing divider, or a container that shrank to its content, actually shows up:

```
y=3288  'Service Box 25'  372x620  nearest .np-case
    height 633 vs 620
```

### Open the overlay. Every section, every time.

Not when the numbers stall — **always, before you call a section done.** On this
page the score said 90.2 and the overlay showed a trial button at the wrong size
and a missing rule; it said 92.1 and the overlay showed the middle card ending
48px above its neighbours. Both times the number was comfortable and the picture
was not, and both times the picture was right.

```bash
python3 "$FW" diff <slug> --url … --section <name>   # writes diff/overlay.png
```

Before you read anything off it, read the one line above the band table:

```
register  design.png and the page agree sideways to the pixel
```

If instead it says `REFERENCE_OFF_BY: the whole page fits design.png best -3px
across`, stop. A shift that is the same in every band is not a layout mistake,
it is the reference being out of register, and `audit`'s `origin` line settles
which side is wrong: `audit` reads design.json, `diff` reads design.png, and
when the two disagree the picture is the one that moved. This exact fault ran
for a whole page — a canvas render cropped from the children's layout boxes
while Figma renders from their *rendered* bounds, so one parked card's shadow
pushed the crop 3px — and it produced three confident "fixes" that each moved
the page 3px further from the frame while the overlay looked like it agreed.
Every number in design.json was right the whole time.

Read the colour against the background every time, because it flips: red is
whichever side holds the *darker* pixel. Dark text on white — red is the page.
Light text on a dark band — red is the design. Getting this backwards has
happened three times in one build.

Both crops used to start from different landmarks and run different lengths —
the design's text span against the page's box height. That fed 602px of design
to 1052px of page and scored a correct section at 14%. If you are reading old
notes that say section scores are meaningless, that is why.

Take numbers from `design.json` too, not from the eye: `tokens.text[*].ls` is
the real letter-spacing, `tokens.effect` the real shadow including its spread.
Guessing them is how a button ends up 2.6× too loose.

Follow `references/site-rules.md`. The short version:

- Wrap in `<!-- wp:html --> <div class="mc-page …"> … </div> <!-- /wp:html -->`.
- One `<style>` block scoped to your wrapper class. `mc/set-post-html` calls
  `kses_remove_filters()`, so `<style>` survives — that is what makes real
  fidelity possible.
- **Every page must carry `id="herotop"`** on its first section. The site header
  links to it from every page; without the anchor its back-to-top silently does
  nothing. Convention, not code — it is in no repo, brief or design file.
- **Scope every CSS rule under your wrapper class.** The `<style>` block cannot
  reach other pages, but it shares the document with the site's header, footer
  and mega menu: a bare `.ppanel{display:none}` hides whatever the chrome calls
  `ppanel`. `verify` flags any selector whose leftmost part is not page-prefixed.
- **Prefer the enqueued behaviours over inline `<script>`.**
  `.acc`/`.acc-item`/`.acc-head` for accordions, `.ptab`/`.ppanel` for tabs,
  `.mc-loop` for an endless row, `data-tabs-loop` to auto-advance a tab group,
  `data-goto-tab` to select a tab from another section.
  Inline `<script>` *does* work through `mc/set-post-html` — KSES is what strips
  it, and that ability removes KSES, verified on the live front end. Use it for a
  one-off effect or to try something new on a single page; put anything reusable
  in `wpbuddy-page.js`, where it is shared across pages, minified, and fixable in
  one place instead of page by page.
- Relative links only (`/solutions/x/`), so WPML can localise them.
- Dynamic blocks are shortcodes: `[wpb_post_list]`, `[wpb_authors]`.
- The "Contact us" CTA opens the existing shared popup. Never build a form.

**Drop the site chrome.** Designs include the header and footer for context;
WordPress renders its own. Watch for stale blocks from whatever design the frame
was copied from, too. Record every deliberate omission in
`build/<slug>/dropped.json` as `{"some copy": "why it was dropped"}` — otherwise
`verify` fails, which is the point: an omission should be a decision someone can
review, not silent loss.

## Audit before you look

```bash
python3 "$FW" audit <slug> --url http://127.0.0.1:8731/preview.html
```

`verify` reads the strings and `diff` reads the pixels; neither says *what* is
wrong. `audit` reads the rendered page back out of the browser — every run of
text with its box and its computed type — and compares it to `design.json`
string by string. It reports a list you have to answer for rather than a score
you can rationalise:

```
  24 ×  .(no class)          weight   400 vs 500
   4 ×  .asana2-h2           size     60 vs 48
   1 ×  .asana2-plan__price  size     18 vs 20
```

It also maps each `<section>` on the page to the range its own text occupies in
the frame, and reports the two numbers separately:

```
section                   design    page   delta   strings
  asana2-plans              1415    1109    -306        52
  asana2-cases               309     215     -94         7
```

A section that is 306px short is a finding. Everything below it being displaced
by 306px is not — and in a whole-page comparison the second buries the first.
`audit --section` and `diff --section` then work on one at a time, each cut to
its own content.

**Treat its output as candidates, not verdicts.** Two of the fifty-eight it
first reported on the Asana page were wrong: a colour read off an anti-aliased
glyph edge, and a line-height for tab labels that have no node in the file at
all. Check each against `design.json` before changing anything — a confident
wrong number is worse than no number.

What it cannot see: anything without text to match on. Corner radii, shadows,
gradients, icons, image crops. That is what `diff` is for; the two are not
alternatives.

## 3. Verify, then diff the picture

**Before your first `diff`: `preview` never returns.** It writes
`build/<slug>/preview.html` and then serves it forever, so running it in the
foreground hangs the session. Start it in the background and kill it once the
file exists, or skip it entirely and serve `build/` with
`python3 -m http.server 8777`. And `preview.html` is a **snapshot** — it does
not change when you edit `page.html`, so regenerate it after every edit and
check your change is in it before you trust any number `diff` or `mobile`
prints. `references/site-rules.md` has both in full.

```bash
python3 "$FW" verify <slug>
python3 "$FW" diff   <slug> --url http://127.0.0.1:8731/preview.html
```

`verify` asserts every string in `design.json` appears verbatim, every image is
referenced, every `<img>` has alt text, no absolute site link, no leftover Figma
URL, and that any `textCase` the design asks for is actually set in the CSS. Run
it first — it is far cheaper than a render round-trip.

Verbatim means the characters, not the rendering. Whitespace and line breaks
are free — the check squashes both — but an entity is not: write `’ — –`, not
`&rsquo; &mdash; &ndash;`. Case is not free either: a string stored `POPULAR`
has to be `POPULAR` in the markup, `text-transform` notwithstanding, because
the check reads the markup and the browser applies the transform after it.

Whitespace is free, but a **hard break is not whitespace — it is content.** A
`\u2028` or a newline inside a text node is the designer saying where the line
ends, and `verify` now fails if the markup does not break there. Reproduce it
with `<br>` or a block boundary (`<li>`, `<p>`): writing `&#8232;` into the
HTML does nothing at all, because browsers treat U+2028 as an ordinary space
and wrap wherever the column runs out. Both pricing notes on the Asana page
broke in the wrong place for that reason, one with the character sitting right
there in the source and every string check passing.

**The preview runs the site's real CSS, and you must build against it.**
`preview` caches the live page's stylesheet beside the build and serves it, so
the theme's own rules are in play while you work. Without it the page you are
measuring is not the page that ships: the theme sets `white-space:nowrap` on
every bare `<button>`, and an accordion whose questions wrapped perfectly in a
bare preview ran every one of them out on a single line under the chevron the
moment it reached the site. `--refresh-site` re-fetches. A build folder with no
`site/site.css` is measuring a page nobody will see.

**The preview runs the site's real behaviour script.** `preview` caches the
live `wpbuddy-page.js` beside the build and serves it, so tabs, accordions and
carousels respond in the preview exactly as they will on the site — click them
before calling a page done. `--refresh-site` re-fetches it. If it could not be
found the preview says so in a comment where the tag would have been, because
a script tag pointing at nothing looks exactly like a page that has its
behaviour.

The markup is a contract with that script, and `verify` now fails when the two
halves do not line up: a `.ptab[data-p="3"]` with no `#p-3`, a `[data-car]`
naming an id that is not in the page, a `.mc-loop` with no `.mc-loop__track`.
The symptom of breaking it is silence — the page renders, nothing responds —
and it reads as a production problem when it is not. What the check cannot see
is a control that was never declared: the Asana carousel's dots were plain
`<span>`s with no `data-car`, so they lit up once and never moved again.

**Do not draw an icon the file already exported.** `extract` writes every
vector to `assets/i*.svg`, and `verify` warns when a shape the frame repeats
three or more times appears nowhere in the build. Rotated borders and gradient
crosses are approximations, and their errors are invisible to every geometry
check because the box around them is the right size in the right place: the
Asana checklist tick sat 3px low inside its own disc and the cross was grey
where the file draws white, through four rounds of review. Reference the SVG,
or inline its path as a data: URI — the warning matches on the path's shape,
so an inlined copy at a different scale still counts as used.

**`verify` passing means nothing about how the page looks.** Every one of its
checks is a string check. A page can score 85/85 strings and 0 errors while the
hero is 200px too tall, the logo has a background box, the icons are the wrong
icons and the body copy is justified. That is not hypothetical — it is what
shipped, and it shipped because a green `verify` was read as "matches the
design".

So `diff` is not optional. It screenshots the page at the design's own width,
puts it beside `design.png`, prints a per-band colour delta, and writes
`build/<slug>/diff/side-by-side.png`. **Open that file and look at it.** The
numbers tell you which band to look at; only your eyes can tell you what is
wrong in it.

Two sources of truth, and they do not overlap:

| Question | Where the answer is |
|---|---|
| Is it laid out right? Right icon? Right wrap? Extra element? | the render — `design.png`, and `diff` |
| Exactly how many px / which hex / which case / which font? | the numbers — `design.json` → `frames`, `tokens`, `texts` |

Never measure pixels off the render by eye. A card pitch measured that way came
out 390 when the file says 392, and padding read as 0 when the file says 17.
Never trust the numbers alone either: they cannot tell you the icon is a plain
circle where the design has a magnifying glass.

### How the overlay is built

`diff` writes `build/<slug>/diff/overlay.png`: the design in the red channel,
the page in green and blue. Anything that lines up goes grey; anything that does
not leaves a coloured ghost whose width is the error. Open it for **every**
section before calling that section done — see *Open the overlay* above; waiting
until the numbers stall is too late, because a comfortable number is exactly
when a container the wrong size hides.

**Read the colour against the background, not off the legend.** Red means *the
darker pixel is only on one side*. For dark text on white, red is the page and
cyan the design. For a light element on a dark ground it is the other way round.
Getting this backwards and reporting a fix in the wrong direction has happened
twice; decide which case the band is before you say which side is late.

### Loop on `match`, and know what it can never reach

`diff` prints one convergence number and a per-band breakdown:

```
match     90.9% of the overlay agrees; band median 93.9% over 20 bands
          bands more than 15 points under this page's own median:
            y1911-2184  74.4%  (-19.5%)   accepted: Go product switcher …
```

**Do not chase 99%.** A page built section by section to `dx +0, dy ±2`,
reviewed band by band and shipped, scores **90.9% whole-sheet and 93.9% at the
median band**. The missing ten points are two different rasterisers disagreeing
on every glyph edge, a recompressed WebP where the frame has Figma's own render,
gradients dithering differently, anti-aliasing on every corner, and a frame
taller than the page. If you ever see 99%, you are comparing something to
itself — a stale `live.png`, or the design against the design.

Read it **relative**. What carries signal is a band far below *this page's own*
median: 40 points down is not anti-aliasing, it is a box the wrong width or a
row that lost a card. On one page a band at 45.7% turned out to be a heading
whose box was 900px where the frame says 1051 — so it wrapped to three lines
instead of two — and a carousel showing three cards where the design shows four.
`verify` was 140/140, `audit` found nothing there, and a side-by-side at page
scale looked fine. Only the band number pointed at it.

**Targets for a first delivery:** every section gated at 93 with no box
findings, whole-sheet `match` in the low 90s, and **zero unexplained bands**. That last one is the
real gate — a percentage lets you round "I do not know why" up to "close
enough", and that is where this goes wrong.

Bands that genuinely cannot converge go in `build/<slug>/accepted.json`, keyed
by any y inside the band, with the reason:

```json
{
  "2000": "Go product switcher. The frame draws one still of an auto-advancing
           panel; the screenshot catches whichever product was up."
}
```

`diff` labels those and stops counting them as unexplained. It is the geometry
counterpart of `dropped.json` — a deviation someone can review rather than one
that quietly persists.

### The loop

The tool measures; it cannot write CSS. So the loop is you and it, alternating:

Desktop only, and **one section at a time** — see *One section at a time* above
for why the whole-page number cannot guide this. The phone comes after the whole
desktop converges and the user has confirmed it.

```
0  gate       is the measurement trustworthy at all?
              fonts loaded (audit says so), preview freezing what moves,
              design.png fresh, origin plausible. If not, stop — do not iterate
              against a broken instrument.
1  measure    diff --section (match + gate) · audit --section (per element)
2  pick       the lowest unexplained band inside THIS section
3  name       what is in it — which strings, which boxes, from design.json
4  fix        by a sourced number only. Every change must be able to name its
              origin: "texts[7].runs[2].lh = 24", "Frame 85725 w=354, pitch 394".
              A change you cannot source is a guess; make it and the number can
              improve while the page gets further from the design.
5  re-measure and go back to 2
6  freeze     once the section gates at 90+, do not touch it again; start the
              next section down. Its position depends on this one being right.
```

**Stop when** every band is explained, or two consecutive rounds do not move
`match`, or the budget is gone. A stalled loop means step 3 or 4 is wrong, not
that you need another round: go back and read the file again.

**The trap this exists to prevent** is real and has happened here. A round was
run against a node's *base* type (20/32) when every character in it carried an
override (18/24). The page moved further from the design, the fixer added
compensating padding, and `drift` and `step` both improved. Sourcing each change
is what makes that visible instead of invisible.

### `audit` reports drift, and `diff` cannot

`audit`'s section table carries two columns `diff` has no way to produce:

- **`drift`** — how far the section's first string sits from where the frame
  puts it. Same anchor on both sides, so it is the honest running offset.
- **`step`** — the change in drift against the section above. Anything over a
  few pixels means something between them changed *height*, not position.

`diff` lets every band find its own best vertical fit, which is what makes its
numbers readable and is exactly why it cannot see this: a section that starts in
the wrong place still scores well once the band slides to meet it.

### The phone — after the desktop is confirmed, not alongside it

**Settle the desktop, show it to the user, get it confirmed. Then write the
phone.** Not because it is quicker, but because every phone rule restates a
desktop value — the line-heights above all, since each one is an absolute px
lifted from the frame. A desktop that is still moving invalidates them as fast
as they are written. On the page this section was written for, a hero lead went
18/24 → 20/32 → 18/24 across two rounds; every phone override keyed to it was
written twice for nothing.

The desktop also *converges* and the phone does not: the desktop is a search
against a reference, run with `match` and the loop above. The phone has no
reference in Figma at all — it is a checklist against the house spec, done once.
Interleaving a search with a checklist makes you redo the checklist every pass.
An Elementor build is the exception: the original page has its own tablet and
phone layouts, so both have a reference — see [From Elementor](#from-elementor).

`mobile` says so itself. It reads `diff/state.json` and, if the desktop has
unexplained bands or `match` under 80%, tells you that reading the phone now is
fine but writing it now means writing it twice.

The phone layout is **not** your call — the rest of the site has one, and
`references/site-rules.md` records it: breakpoints 768 and 1024, a 28px gutter,
50px sections, 16px floor for body copy, and a fixed 81px site header every
first section has to clear.

```bash
python3 "$FW" mobile <slug> --url http://127.0.0.1:8731/preview.html
```

Overflow, body copy under 16px, tap targets under 40px, content jammed against
the edge. It renders into an **iframe** of the requested width, because headless
Chrome will not open a window narrower than 500px — `diff --width 390` measured
500 and every query below that breakpoint stayed shut, which is why it used to
report a mobile layout that nobody could reproduce.

Three things the reference itself can get wrong, all of which shipped here:

- **Pieces parked beside the frame.** Designers leave cards, panels and
  controls next to a frame and let them overlap it. They paint on top of the
  frame on the canvas but are absent from a frame-only render, so `extract`
  renders the canvas and crops. If you ever hand-export a reference from the
  Figma UI, export the page, not the frame.
- **A stale `design.png`.** Re-run `extract` when the file changes. Swapping
  `design.json` alone leaves you comparing against last week's picture.
- **One text node, several type styles.** The hero heading and its paragraph
  are a single node: 48/72 bold over 20/32 medium. Read `runs`, not just the
  node's base style, or the paragraph ships at heading defaults. A node whose
  characters *all* carry the same override is the same trap wearing a
  disguise: its own style says one thing and every character says another.
  `texts[].type` already resolves this; do not read the raw file yourself.

Bands the diff cannot settle, and should not be chased:

- **Auto-rotating content.** A `data-tabs-loop` panel shows whatever was up
  when the shutter fired. Static mock, moving page.
- **Accordions.** The mock draws every answer open; the page ships closed.
- **Site furniture.** The theme's own CTA band and footer are not in the frame.

`diff` needs Chrome or Chromium installed, and Pillow. It is a development
check, not part of `push`.

## From Claude Design

A Claude Design project is a React template (`.dc.html`) that only runs inside
Claude Design. What reaches WordPress is its **static export** — ask Claude
Design for one if the project has none. Never paste the preview's DOM: that is
how post 78436 shipped 20KB of preview harness and a transparent body.

1. **Read the export.** `mcp__claude_design__list_files` on the project; compare
   the export's etag with the `.dc.html` it came from. If the `.dc.html` is newer,
   stop and ask the user to export again — the export is a copy and does not
   follow edits. Then `mcp__claude_design__read_file`, decode `&amp; &lt; &gt;`,
   and write it to disk.
2. **Extract.**
   `python3 "$FW" extract <export.html> --slug <slug> --from claude-design --cd-project <id> --cd-file <path>`
   If it stops with `NOT_WP_SAFE`, give the printed text to the user to paste
   into Claude Design. Do not hand-fix the export: the next export would undo it.
3. **verify → preview → diff → mobile**, as for Figma, except that the page is
   whole already: diff the full page once, gate every band at 90 or explain it
   in `accepted.json`.
4. **push**, with `--post-type post` for a blog / event / news entry. Read the
   `postcheck` line: anything but `wpautop +0 <p>` means WordPress rewrote the body.

What `wpsafe` does for you, and why each exists:

| Trap on 78436 | Cost | wpsafe |
|---|---|---|
| preview harness + document shell pushed | 20KB of script on the live page | `check` refuses; `clean` strips |
| no `<!-- wp:html -->` | 17 `<p>` added, +67px top, buttons moved | `clean` wraps |
| theme `font-family:Raleway!important` | whole page in Raleway | reset + `!important` on the page's own font-family |
| theme `form label{padding:20px 0 10px}` | form +250px | reset |
| `.site-main` capped at 1140px | page squeezed | reset breakout |
| preview CSS taken from a private page | diff said fine, site broke | `--site-sample` |

**Edit `scripts/wpsafe.py`, never mcp-wp's copy.** After changing it, commit,
then run `scripts/sync-wpsafe.sh ../mcp-wp` and open a PR there — nothing
detects a skill change that was never synced.

## From Elementor

Most pages on the site are Elementor pages: no Figma file, no Claude Design
project, and the layout in post meta rather than the body. For those **the live
page is the design**. `extract --from elementor` shoots it and measures it, and
from there it is the same work as a Figma build — you write raw HTML against
the references, one section at a time — with one difference that matters: the
original has a tablet and a phone layout of its own, so those have references
too, and the build keeps them. The site's house phone spec does not apply.

The script still only fetches. It reads what has one right answer — the copy,
the computed type, every box, the widget list, the page's responsive rules, the
images — and you decide the layout by looking at the pictures.

### Two ways to use it — ask which, before anything

| | **A. A new page from it** | **B. Replace it** |
|---|---|---|
| What happens | a new raw-HTML page is created, usually another language joined to the original's WPML group | the Elementor page itself becomes raw HTML |
| The original | untouched | same id, URL, title, SEO and WPML group; its Elementor data is kept for the way back |
| Copy | verbatim or translated — **ask** | verbatim, always |
| Push | `push --title … --language … --translation-of <orig>` | draft first, the user signs it off, then `push --replace <orig>` |

### 1. Extract

```bash
python3 "$FW" extract <page URL or post id> --from elementor --slug <slug>
```

**Public pages only.** extract reads the page the way a visitor does, and stops
(`NOT_PUBLIC`) on a page that is not published, that redirects somewhere else,
or that answers with a password or login form — any of those shot as
`design.png` would be built faithfully. It also stops when the page is not
built with Elementor (`NOT_ELEMENTOR`), and when the page renders no Elementor
document of its own id (`NO_ELEMENTOR_DOC`) — a translation still drawing its
original's document would otherwise be measured as itself. A build folder made
from another source is not overwritten without `--force`
(`NOT_AN_ELEMENTOR_BUILD`).

For usage A in another language, give the build its own `--slug` (the
original's own slug is the default, and the original may get a build of its
own).

What lands in `build/<slug>/`:

| File | What |
|---|---|
| `design.png`, `design-768.png`, `design-390.png` | the page body at 1440, 768 and 390 — header, footer, popups and cookie banner hidden, lazy images loaded, every `vh` pinned to a 1000px viewport |
| `design.json` | `source` (type `elementor`, post id, URL, `modified_gmt`, the fonts the page loaded); `texts` — **the whole copy, from the page's HTML**, each placed by the 1440 render with its computed type in `texts[].type`; `sections`; `widths."768"` / `widths."390"` with the texts and sections measured at that width, for the position checks. `frames` and `tokens` are empty: read type off `texts[].type` |
| `elementor.json` | `widgets` (type, tier, section), `tier3`, `popups`, `posts_queries`, `responsive` (the page's own rules at ≤1024 and ≤767), `flags` (motion, sticky, hidden per device, HubSpot), `template` |
| `elementor/live.html` | the snapshot every reference was shot from |
| `assets/` + `manifest.json` | the images, for the preview. The manifest already points at the media library's copy, so push uploads none of them; an Elementor thumbnail has `id: null` and stays on its URL |

In a page repo, the `<slug>: extract` commit carries all of these.

**Read the output before you write anything.** It is the brief:

- `copy` — how many strings, and two flags. **`hidden` means no text run
  matched the string at 1440**: a closed tab, a phone-only block, a collapsed
  answer. It is still copy and `verify` still wants it on the page; it is not
  "invisible, leave it out". **`unmeasured`** marks strings under 3 characters,
  which the measurement skips — they have no position, only text.
- `WARN ref drift` / `ref mismatch` / `render drift` — the picture and the
  numbers disagree from some point down. Positions below it are not reliable.
- `fonts` — what the original actually loaded. See *Fonts* below.
- `widgets` and `tier 3:` — a tier-3 widget has no recipe. Stop, add one to
  `references/elementor-widgets.md` (generic — this repo is public), then build.
- `posts` — the term ids the posts widgets query. The target language is only
  known at push, which looks each one up in it and warns (never stops) for
  every term it cannot find.
- `popups` — each popup id is per language. Usage A in another language needs
  the one **that** language loads.
- `responsive` and `assets` — how many rules and images were found.

### 2. Author

Read `references/elementor-widgets.md` first: a recipe for every tier-1 and
tier-2 widget, and the measures every Elementor page shares — each column's
10px padding, the theme's `padding-top:10px` on `h1`–`h5`, Elementor's
breakpoints (tablet ≤1024, phone ≤767, not the house 768). The rest of this
skill applies unchanged: one section at a time, sourced numbers only,
`dropped.json` for anything left out on purpose.

- **One `<section>` per top-level section of the original, in the same order,**
  and none nested inside them. The section checks pair the build's sections
  with the original's in order.
- **No `vh`.** The references pin `vh` to a 1000px viewport; the build's `vh`
  renders differently in `diff` and in the framed `mobile` / `diff --width`
  checks, and every position below it drifts. Write the px the reference
  measured.
- **The tablet and phone are a search, not a checklist.** Desktop first and
  confirmed by the user, as always — then the 768 and 390 layouts against
  `design-768.png` and `design-390.png`, with the original's own rules from
  `elementor.json` → `responsive`.

**Fonts.** Do not assume Raleway. The site names it and loads it nowhere on
these pages: the Elementor original and the raw page both render in
**Roboto**, which the site's Elementor header and kit load on every page. So the
raw page needs to load no font of its own — but it depends on the header
loading Roboto, and if that ever stops, the page falls back with it. For
an Elementor build `extract` downloads the files behind `design.json` →
`source.fonts` (icon faces left out; Roboto if it recorded none) into
`build/<slug>/assets/fonts/<family>-<weight>[-italic].<ext>`, taking the
URLs from the page's own `@font-face` rules — its inline `<style>` and the
stylesheets it links, one `@import` deep — and only the weights and styles
those declare (woff2 preferred; masterconcept.ai declares Roboto as ttf from
fonts.gstatic.com). `preview` then serves exactly those files instead of
Raleway, declared after the site's cached CSS so they win over its own gstatic
URLs; Figma builds still get Raleway. Only http(s) URLs are fetched, and a
family split by script with no latin slice (CJK) is not downloaded. A family
`extract` printed a `warn fonts:` line for has no file and the preview cannot
reproduce its glyph widths: put a `<family>.woff2` in that folder by hand. When a whole block is off by the same few px, check which face the
preview actually rendered before moving anything; the draft's front end has
the last word.

### 3. Check

`verify`, `preview`, `audit` and `diff` as for Figma; gate each section at 90
or explain it in `accepted.json`. Then the two narrow widths:

```bash
python3 "$FW" diff   <slug> --url … --width 768     # against design-768.png
python3 "$FW" diff   <slug> --url … --width 390     # against design-390.png
python3 "$FW" mobile <slug> --url … --width 768
python3 "$FW" mobile <slug> --url …                 # 390
```

On an Elementor build `diff --width 768|390` renders the page in an iframe of
that width and compares it to that width's reference (`NO_REFERENCE` for a
width extract did not shoot). `mobile` runs its usual checks and, in addition,
compares every text's position with where the original put it at that width,
±6px (`--tolerance`). Body copy under 16px that is under 16px on the original
too is printed as a note, not counted: the original is the spec.

### Usage A — copy

**Verbatim**: `design.json` → `texts` is the copy, and `verify` checks it
string by string, as for Figma.

**Translated**: ask where the translation comes from — the user (a file, or
pasted), or a draft Claude writes. Either way it goes into
`build/<slug>/copy.json`, and that file becomes the only source of the page's
words. Never retype a translation off anything else.

```json
{"mode": "translated", "source": "user",
 "entries": [{"i": 3, "from": "Get started", "to": "立即開始"}]}
```

- `i` is the string's `texts[].i` in `design.json`; `from` is the original, kept
  so a reviewer can read the pair (verify warns when it no longer matches).
- `verify` checks the page against every `to`, and lists each string with no
  translation as `untranslated (#i)` — an error, unless `dropped.json` leaves
  it out on purpose.
- A Claude draft is `"source": "claude-draft"`. Give it to the user entry by
  entry; only once they have confirmed it, set `"approved": true`. Until then
  `verify` fails `COPY_NOT_APPROVED`, and push will not run.
- A malformed file stops every check (`BAD_COPY_JSON`) rather than run on a
  guess.
- A translated page wraps differently, so `mobile` compares each section's top
  and height (`dy`, `dh`) with the original's instead of each text's position.

### Usage A — push

```bash
python3 "$FW" push <slug> --title "…" --language zh-hk \
    --translation-of <original post id> --page-slug <original's slug> \
    --parent-path <the original's parent path>
```

`--translation-of` joins the new page to the original's WPML group when it is
created (only then: `TRANSLATION_OF_ON_UPDATE`). Every language in a group
shares the slug, and `--parent-path` resolves in the page's own language.

WPML copies the original's Elementor switch and data onto a page the moment it
is created as its translation, so the new page would render Elementor. push
clears both on a page it created, then checks: `STILL_ELEMENTOR` if the edit
mode is still set, or if the rendered body lacks the page's wrapper class
(`<x>-page`). That second check reads the REST body, which does not run
Elementor — so look at the front end too: open the draft logged in at
`?page_id=N&preview=true`, and again once it is published. It should carry
your wrapper class and no `data-elementor-type="wp-page"`.

Expected and not an error: WPML rewrites a translation's internal links to
absolute URLs on every save. The body WordPress holds will not match the build
there.

### Usage B — draft, sign-off, replace

1. **Draft.** Push the build as a new draft of its own, at a throwaway slug,
   joined to no group:

   ```bash
   python3 "$FW" push <slug> --title "…" --page-slug <slug>-draft --language <the original's>
   ```

   The user checks it logged in at `?page_id=N&preview=true`. Nothing goes
   further until they say it is right.

2. **Replace.** Then, and only then:

   ```bash
   python3 "$FW" push <slug> --replace <original post id>
   ```

   In this order:

   - **Refuses before anything is read**: `--title`, `--parent`,
     `--parent-path`, `--page-slug` or `--force` with `--replace`
     (`REPLACE_ARG`). The original keeps its title, place and URL, and no check
     is skipped.
   - **Refuses before anything is written**: `verify` failing; no draft in
     `wp.json` (`REPLACE_NO_DRAFT`); `wp.json`'s page being the original itself
     (`REPLACE_DRAFT_IS_ORIGINAL`); `design.json` not an extract of that page
     (`REPLACE_WRONG_PAGE`); the original edited since extract
     (`PAGE_MOVED_ON` — extract again); the original's URL landing somewhere
     else (`ORIGINAL_REDIRECTS`).
   - Reads what search engines key the page by — title, meta description,
     canonical, hreflang — from the front end.
   - **Backs up** the Elementor meta, the page JS and the body to
     `backups/<id>-elementor-<stamp>.json` and reads it back. A page with no
     builder mode or no Elementor data has nothing to restore:
     `BACKUP_FAILED`, nothing changed.
   - Writes a `replacing` marker to `wp.json` **before the first write**, so a
     run that stops halfway says where its way back is. A rerun then refuses
     (`REPLACE_UNFINISHED`) and points at `elementor-restore`.
   - Writes the body with `mc/set-post-html`, clears **only**
     `_elementor_edit_mode` (`_elementor_data` stays, for the way back), writes
     the page JS. The permalink is never regenerated.
   - Purges the cache and reads the front end again: `STILL_ELEMENTOR` if it is
     not your HTML, `IDENTITY_CHANGED` if the title, description, canonical or
     hreflang moved. Either one leaves the draft alone and names the backup.
   - Moves the draft to the trash, and rewrites `wp.json` to the original with
     a `replaced` record. From here a plain `push <slug>` updates the page.

3. **Back.** `elementor-restore` puts the Elementor page back:

   ```bash
   python3 "$FW" elementor-restore <slug>            # the backup wp.json names
   python3 "$FW" elementor-restore <slug> --backup <path>
   ```

   It refuses if `_elementor_data` on the site is no longer what the backup
   holds (`ELEMENTOR_DATA_CHANGED` — someone edited it since; compare and
   decide, it will not guess), or if the backup is of another page
   (`BACKUP_MISMATCH`). Otherwise it keeps the body the page holds now in
   `backups/`, sets builder mode and the page JS back **first** — Elementor then
   draws its own data whatever the body says, so a failure after that still
   leaves the page on Elementor — then writes the old body, purges, and checks
   the front end renders the Elementor document again (`RESTORE_FAILED` if
   not). Any step failing stops with `RESTORE_INCOMPLETE`. **Running it again
   is always safe.** It clears `replaced`/`replacing` from `wp.json` and prints
   how to replace again.

### What the site was seen to do

Tested on throwaway pages in a WPML group of their own:

- **Only `_elementor_edit_mode` decides whether Elementor renders.** Blank, the
  front end prints the body; `_elementor_data` can stay where it is. The body
  loses its `elementor-page` class (so the theme's width clamp applies — the
  wrapper needs the breakout) and keeps its page-template class.
- **WPML copies `_elementor_edit_mode` and `_elementor_data`** — not the page
  template — onto a page created as a translation, at the moment it is created.
- **Neither side follows the other.** Clearing a translation's Elementor meta
  leaves the original as it was, and changing the original's leaves the
  translation as it was.
- **Restore works.** Builder mode plus the old body brings the Elementor
  version back exactly, and the Elementor editor opens it normally.
- **REST `content.rendered` does not run Elementor.** It shows the body even
  while the front end draws Elementor. Check the front end.
- **A draft can be checked** in a logged-in browser at
  `?page_id=N&preview=true` — nothing has to be published to be looked at.
- Not yet settled: whether saving the original in Elementor copies its meta
  back onto a translation that push made raw — the acceptance run will answer
  it.

### Traps already met

| Trap | What it does | Now |
|---|---|---|
| WPML copies builder mode and data onto a new translation | the front end still shows the Elementor original; the post-check misreads it as extra `<p>` | push clears both and checks (`STILL_ELEMENTOR`); look at the front end |
| a shortcode's name in `page.css`, even in a comment | everything after it in the stylesheet dies, phone queries included; the preview runs no shortcodes and looks fine | `verify` fails it |
| ` - ` in the copy | wptexturize makes it an en dash, inside `wp:html` too | `verify` warns; write `&#45;` |
| Raleway assumed | the live page renders Roboto from the header and kit; a Raleway preview would measure other glyph widths | `extract` downloads the original's font files into `assets/fonts` and `preview` serves them |
| the theme's `padding-top:10px` on `h1`–`h5`, Elementor's 10px column padding | every section a little lower than the last | `references/elementor-widgets.md` |
| `diff --width 390` as a narrowed window | Chrome will not go below ~500px; every number was off | Elementor builds diff at 768 and 390 in an iframe |
| internal links in a translation | WPML rewrites them absolute on save | expected; not an error |
| posts widget → `[wpb_post_list]` in a language with no matching term or posts | "No posts found." under a heading | hide the band when the list is empty (`elementor-widgets.md`, *posts*); `extract` lists the term ids, `push --language` warns for each with no counterpart in that language |
| a hero in `vh` | reference and build disagree on its height | references pin `vh` to 1000px; build in px |
| the original's popup id kept on another language | the button does nothing | `elementor.json` → `popups`; use the id that language loads |
| `push --post-id` on an Elementor page | the body is written, the page keeps rendering Elementor, nothing is backed up | refused (`ELEMENTOR_PAGE`); use `--replace` |

## Editing a page that already exists

```bash
python3 "$FW" pull "Superhuman"        # or the post id, the URL, or the slug
```

Everything above starts at Figma, which leaves a page nobody has a `build/`
for — someone else's, one edited in the admin, one from before this tool —
untouchable. `pull` brings it down: the `<style>` block becomes `page.css`,
the body becomes `page.html` with `{{styles}}` where the CSS was, the media
comes down to `assets/` with a `manifest.json` so the next push reuses it, and
`wp.json` records the page so `push` needs no `--post-id`.

A title is matched against post titles only. The REST `search` also matches
post *content* and does not rank by title, so searching "Asana" returns the
Superhuman page first; anything ambiguous is listed for you to choose, never
guessed.

What comes back is the page as WordPress serves it, not the original source:
shortcodes are raw, asset names come from the media library. It is enough to
edit and push back — verified byte-identical on a 37KB page — but it is not
the file someone originally wrote.

An Elementor page keeps its layout in post meta, not the body. `pull` says so
and stops (`ELEMENTOR_PAGE`, naming `extract --from elementor`) rather than
writing a build folder that looks usable and is not. To work on one, start from
`extract --from elementor` instead — see [From Elementor](#from-elementor).

Without a `design.json` there is no copy to check and no render to diff
against, so `verify` and `diff` have much less to say. That is the trade for
being able to touch a page at all.

### Two things push now refuses

- **The page moved on.** `wp.json` records `modified_gmt`; if WordPress is
  ahead of it, push stops before uploading anything. Site revisions are off,
  so an overwrite is unrecoverable except from `build/<slug>/backups/`.
- **A rename.** The build folder name no longer decides the URL of a page that
  already exists — only an explicit `--page-slug` does, and on a published page
  even that is refused without `--force`. Pulling a page into
  `build/anything` and pushing it back used to rename the live URL to
  `/anything/` and 404 the real one.

## 4. Push

```bash
python3 "$FW" push <build-slug> \
    --title "…" \
    --page-slug superhuman \
    --parent-path partners/work-collaboration
# or, to refresh an existing page:
python3 "$FW" push <build-slug> --post-id 1234
```

`--page-slug` is the page's URL slug, separate from the build directory name, so
a design can be tried at a throwaway URL before it takes the real one.
`--parent-path` walks the path segment by segment — slugs are unique only among
siblings, so resolving just the last segment can land on the wrong page.

The resulting URL is `<parent-path>/<page-slug>/`. While the page is a draft
WordPress reports the ugly `?page_id=N` form even though Permalink Manager has
already stored the real URI; the pretty URL appears on publish.

**Before pushing, confirm the slug is free** and that you are creating rather
than overwriting: never pass `--post-id` unless the user named that page.

```bash
curl -s -u "$WP_USER:$WP_APP_PASSWORD" \
  "$WP_BASE_URL/wp-json/wp/v2/pages?slug=<page-slug>&status=any"
```

Converts PNG/JPEG to **WebP** first — typically 80–90% smaller, and mobile
PageSpeed on this site is sensitive to image weight — keeping the original if
WebP comes out bigger. `--no-webp` opts out. SVG is uploaded untouched.

Then uploads (skipping anything in `manifest.json`), rewrites the HTML to the
uploaded URLs, creates the page **as a draft**, sets the body with
`mc/set-post-html`, regenerates the Permalink Manager URI. Publishing is a
separate, explicit step.

## Hard don'ts

- **Never open one of these pages with "Edit with Elementor."** It converts the
  page and hides the HTML body. A page `push --replace` swapped still holds its
  old Elementor data, and that is what the editor would open and save back
  over the HTML. The way back is `elementor-restore`, not the editor.
- **Post revisions are disabled site-wide — there is no undo.** `push --post-id`
  writes the existing body to `build/<slug>/backups/` first. Never skip it.
- **Never retype copy from the render.** It comes from `design.json` or it does
  not go on the page.
- **Do not re-upload assets already in `manifest.json`** — the media library
  fills with duplicates and the WAF starts refusing uploads.
- **Never print a credential into the conversation.** What Claude echoes stays
  in the transcript.
- **Never commit the Mac's `~/.figma-wp/.env`.** A page repo's own `.env` is a
  different file and is committed on purpose — which is why **that repo has to
  stay private**. If it is ever made public, both credentials are burnt and have
  to be rotated, not just deleted.
- **In a page repo, never end a reply on uncommitted work.** See
  [Where the work lands](#where-the-work-lands).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FIGMA_HTTP_429` | Cost-based quota, shared between reads and renders, and a viewer-seat token has a small one. Wait; the node response is cached in `~/.figma-wp/cache/` so a retry costs nothing. Or export `design.png` by hand and use `--no-render`. |
| `NO_NODE_ID` | The URL points at the canvas. Open the frame, copy its link. |
| `FIGMA_HTTP_403` | Token lacks file access or was revoked. Viewer role is enough to read and render. |
| `WP_UNAUTHORIZED` / `rest_not_logged_in` | **Check the username first.** WordPress reports a failed Application Password the same way it reports no credentials at all, so a wrong username is indistinguishable from a stripped `Authorization` header. Confirm the username with the user before concluding anything about the server. |
| Genuinely stripped auth header | Only if the *correct* username also fails: Apache with mod_php needs `RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]` in `public_html/.htaccess`. Verify by calling a public ability with GET — `mc/brand-guide` needs no auth, so a 200 there plus 401 elsewhere isolates it to authentication. |
| `wp-admin` returns HTTP 202 | SiteGround's Anti-Bot AI challenging an automated request. Not a 404 and not a hidden login URL; a real browser gets through. |
| `WP_WAF_BLOCKED` | SiteGround's WAF refused the upload before WordPress saw it. Shrink the file or convert to WebP. |
| `<div>`s vanished after publishing | Something used a plain REST post endpoint. Only `mc/set-post-html` bypasses KSES. |
| Accordion dead on the live page | `wpbuddy-page.js` did not load, or production is on an older copy without the behaviour you used. It is content-gated: it only loads when the body contains `class="ptab` / `acc-item` / `acc-head`. Fetch the deployed file and check. |
| Language switcher lands on the home page | Translations were never linked into one WPML group. Use `mc/create-localized-page` with `translation_of`. |
| Slug changed but the URL did not | Permalink Manager stores URIs separately. Run `mc/regenerate-permalink`. |
| Page looks stale to logged-out visitors | `PUT /wp-json/siteground-optimizer/v1/purge-cache`. |
| `NOT_PUBLIC` from `extract --from elementor` | The page is a draft or private, redirects, or shows a password or login form. extract only reads what a visitor sees; it will not shoot a login page as the design. |
| A translation pushed from an Elementor original still shows the original | `STILL_ELEMENTOR` from push, or found by eye on the front end. WPML copied the builder meta onto it. The REST body does not run Elementor, so only the front end tells — open it at `?page_id=N&preview=true` logged in. |
