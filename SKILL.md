---
name: figma-to-wp
description: "Build or update a masterconcept.ai WordPress page from a Figma design. Pulls the frame's render, its copy verbatim, its colour and type tokens, its box geometry and its images out of Figma; you write the HTML against those numbers; `verify` confirms every string survived and the site's hard rules hold, and `diff` screenshots the result and compares it to the render band by band; then it uploads the media and creates the page as a draft via mc/set-post-html with the right slug and Permalink Manager URI. Use when someone gives a Figma link and wants it turned into a page on masterconcept.ai, or wants an existing AI-built page refreshed from an updated design."
license: MIT
allowed-tools:
  - Bash(*)
  - Read(*)
  - Write(*)
  - Edit(*)
metadata:
  version: "0.3.0"
---

# figma-to-wp

```
extract   figma frame  ->  design.png · design.json · assets/
author    you look at design.png and write page.html
verify    every string present, site rules respected
preview   render it, compare against design.png, fix, repeat
push      media -> draft page -> permalink
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

**Every command runs on the user's own Mac, through a local terminal. There is no
other supported way to run it.**

| Host | The local terminal is |
|---|---|
| Claude Cowork | the **Desktop Commander** connector |
| Claude Code | the **Bash** tool |

Both reach the same machine. Cowork also offers a sandboxed shell — **never use
it.** The Figma token and the WordPress application password live in
`~/.figma-wp/.env` on the Mac; a sandbox has neither, is thrown away with the
session, and cannot reach a preview server on `127.0.0.1` either.

## Locating the CLI

The script ships with the skill and is already on disk. **Never download it.**
Resolve it by version so the newest wins when several copies exist:

```sh
FW=$(find "$HOME/Library/Application Support/Claude/local-agent-mode-sessions" \
          "$HOME/.claude/plugins/cache/zorskill/figma-to-wp" \
          "$HOME/.claude/skills/figma-to-wp" \
          ".claude/skills/figma-to-wp" \
          -maxdepth 6 -path '*/scripts/figma_to_wp.py' 2>/dev/null | while IFS= read -r s; do
  d=${s%scripts/figma_to_wp.py}
  v=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "${d}.claude-plugin/plugin.json" 2>/dev/null | head -1)
  printf '%s\t%s\n' "${v:-0.0.0}" "$s"
done | sort -V | tail -1 | cut -f2)
[ -n "$FW" ] || { echo "figma-to-wp CLI not found on this Mac" >&2; exit 1; }
python3 "$FW" doctor
```

The first path is where an org-managed plugin lands (Claude Desktop and Cowork);
the second is Claude Code's; the last two are a personal install and a checkout
you are developing in.

`find` is used rather than a shell glob on purpose. Under `zsh` — Desktop
Commander's default shell — a glob that matches nothing aborts the whole loop,
so a Mac that has Cowork but not Claude Code would report the CLI missing when
it is in fact installed.

If nothing is found, the skill has not reached this Mac yet. Say so and ask the
user to reopen the session or refresh plugins. Do not try to work around it.

## Where the work lands

The script keeps no state of its own. Credentials sit in `~/.figma-wp/.env`
(mode 600); each page's working files go in `build/<slug>/` **relative to the
directory you run from**, not next to the script. Pick a working directory with
the user and stay in it — `~/figma-to-wp` is a reasonable default on a Mac that
has no repo for this.

`references/site-rules.md`, beside the script, carries the rules that are about
masterconcept.ai rather than about Figma: what KSES strips, the theme's width
clamp, which shortcodes exist, how links have to be written for WPML. Read it
before authoring, not after a push goes wrong.

## Ask for these before you start

None of it is guessable, and guessing wastes far more of the user's time than
asking. Collect whatever is missing up front, in one go:

| Ask | Why it cannot be inferred |
|---|---|
| **The Figma link** | Ask for the **page**, not one frame. Carousel slides, variants and card rows are routinely parked outside the main frame, and a frame-only extract loses them silently. A page URL (`?node-id=0-1`) is the right input. |
| **Page title** | |
| **URL slug** | |
| **Parent path**, e.g. `partners/work-collaboration` | Pass it to `push --parent-path`; it resolves to an id by walking the path. |
| **Language** (`en` / `zh-hant` / `zh-hans`) | Defaults to `en`. |
| **The WordPress username** | **Ask. Never guess.** A wrong username returns `rest_not_logged_in`, which looks exactly like a server misconfiguration — chasing that instead of asking cost an hour in the session that produced this skill. |
| **Which author should own the page** | The Style Kit CSS is author-gated to `wpbuddy` / `mcp` (see `design-system.php`). Any other author means `wpbuddy-design.css` does not load and the page is on its own for styling, and it will not appear in WP Buddy → AI Builder. Fine if the page carries all its own CSS — but say so rather than let it surprise them. |
| **A throwaway slug first?** | Offer it. Publishing into the real URL later is one `--page-slug` away, and a test slug cannot collide with a live page. |

Also tell them, before you touch the site, which interactive behaviours the
design needs (`.acc`, `.ptab`, `.mc-loop`, `data-tabs-loop`) and whether
`wpbuddy-page.js` on production already has them. If it does not, the page
publishes looking right but sitting dead, and that reads as a broken build.

## Prerequisites

- **A Mac that is switched on.** In Cowork it also needs Claude Desktop with the
  **Desktop Commander** connector installed and connected; that is the only thing
  the user installs by hand. In Claude Code the Bash tool already is the terminal.
- **A Figma personal access token** with file read scope, and a **WordPress
  application password** for an account that can edit pages. `setup` writes both
  to `~/.figma-wp/.env` at mode 600. Ask for the WordPress username — never guess
  it; a wrong one fails as an auth error that looks like a server problem.
- **Python 3.9+.** The script is stdlib only.
- **Chrome or Chromium, and Pillow**, for `diff` alone. Everything else runs
  without them; `diff` says so if they are missing.
- **poppler** (`pdftotext`, `pdftoppm`) only if you import from a PDF export.
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
| `assets/` | `a*.png` images, `i*.svg` icons |

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

**Then look at `design.png`.** Read it top to bottom and write down the
structure before writing markup: how many bands, where each one starts and ends,
which are full-bleed and which are inset boxes, the container width, the column
split. Measure if you are unsure — the PNG is 1440 wide, so pixel positions in
it are the design's own coordinates.

Then write the HTML, taking every string from `design.json` — never retype copy
off the render, especially Chinese, where a substituted character is invisible
in review and wrong on the live site.

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

## 3. Verify, then diff the picture

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

Check a narrow viewport too (`--width 390`): the frame is desktop-only, so
mobile is your call, not the design's.

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
  page and hides the HTML body.
- **Post revisions are disabled site-wide — there is no undo.** `push --post-id`
  writes the existing body to `build/<slug>/backups/` first. Never skip it.
- **Never retype copy from the render.** It comes from `design.json` or it does
  not go on the page.
- **Do not re-upload assets already in `manifest.json`** — the media library
  fills with duplicates and the WAF starts refusing uploads.
- **Do not print or commit `~/.figma-wp/.env`.**

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
