# masterconcept.ai — hard rules for AI-authored pages

Constraints of the live site, not style preferences. Most of them fail
*silently*: the markup is accepted, then something eats it on output. Each row
gives the symptom so you can recognise it after the fact.

## The body is raw HTML, never Elementor

Write the page with the `mc/set-post-html` ability. It calls
`kses_remove_filters()` first, which is the only reason `<div>`, `<style>` and
`<!-- wp:html -->` survive. The ordinary REST endpoints (`ewpa/create-post`,
`ewpa/update-post`, `POST /wp/v2/pages`) run KSES and strip every structural
tag.

Wrap the whole body:

```html
<!-- wp:html -->
<div class="mc-page my-page">
  <style> .my-page { … } </style>
  …
</div>
<!-- /wp:html -->
```

> **Symptom:** you publish, and every `<div>` and the whole `<style>` block are
> gone. Something used a plain REST endpoint.

**Never open one of these pages with "Edit with Elementor."** It converts the
page to an Elementor document and hides the HTML body. There is no undo.

## Raw post_content pages are clamped; break out or they render narrow

The theme caps the content column on every page that Elementor did not build:

```css
body:not([class*=elementor-page-]) .site-main{max-width:500px}   /* ≥576px  */
body:not([class*=elementor-page-]) .site-main{max-width:600px}   /* ≥768px  */
body:not([class*=elementor-page-]) .site-main{max-width:800px}   /* ≥992px  */
body:not([class*=elementor-page-]) .site-main{max-width:1140px}  /* ≥1200px */
```

Elementor pages put `elementor-page-<id>` on `<body>` and the `:not()` lets
them out. A page built from raw post_content never gets that class, so the
clamp applies to us and to almost nothing else on the site.

Any page whose design is wider than 1140 needs this on its wrapper:

```css
.<page>-page{margin-left:calc(50% - 50vw);margin-right:calc(50% - 50vw)}
```

`overflow-x:clip` on the same element is safe alongside it, and stops a few
pixels of overhang from raising a horizontal scrollbar.

Nothing about this failure looks like breakage. Containers do not overflow and
nothing clips — every band simply renders as a 1140px stripe with white
gutters, and between 992 and 1199px as an 800px one. It reads as "the width is
wrong" and cannot be seen in the post_content at all, which is how a page
shipped with it.

Two things make the rule hard to find, both worth knowing before you go
looking: it lives at the end of a long shared selector list, so grepping for
`site-main{` finds only the `width:100%` shorthand; and it does not appear in
`document.styleSheets` rule-matching either. Fetch the combined stylesheet and
search its text for blocks containing `site-main`, or just measure the
wrapper's rendered width on the live page — it should equal the viewport.


## CSS lives in the page

`<style>` inside the body survives, so each page carries its own scoped
stylesheet. Scope every rule under your wrapper class — the site's Elementor
header, footer and mega-menu share the document.

The shared Style Kit (`wpbuddy-design.css`, loaded automatically on pages
authored by `wpbuddy` / `mcp`) is small: `.mc-container .mc-section
.mc-section-gray .mc-section-dark .mc-hero .mc-h1/.mc-h2/.mc-h3 .mc-lead
.mc-eyebrow .mc-btn .mc-btn-secondary .mc-btn-light .mc-grid .mc-grid-3 .mc-card
.mc-img .mc-cta`. Use it for anything it covers; write page CSS for the rest.

Brand tokens: `--mc-orange #FF9E1B`, `--mc-navy #1B365D`, `--mc-green #12BF6D`,
`--mc-text #4D4D4D`, Raleway. CJK copy needs a font stack —
`Raleway, "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif`.

## Behaviour belongs in the enqueued script — but inline `<script>` does work

The plugin's own comments say inline `<script>` in `post_content` "is stripped
on output by this site." **Measured on the live front end, it is not.** A page
written through `mc/set-post-html` keeps its `<script>`, and the script runs:
KSES is what strips script tags, and that ability calls `kses_remove_filters()`.
The claim is probably true of the ordinary REST endpoints, which do run KSES,
and got over-generalised.

So this is a matter of where code should live, not what survives:

- **Reusable behaviour → `assets/wpbuddy-page.js`.** One copy, shared across
  pages, minified by SiteGround, fixable in one place.
- **A one-off effect, or trying something new → inline is fine.** It touches
  only that page, and needs no plugin deploy.

The enqueued script is class-driven:

| Behaviour | Markup |
|---|---|
| Single-open accordion | `.acc > .acc-item > .acc-head`; the script toggles `.open` on the item — your CSS does the rest |
| Tab / partner switcher | `.ptab[data-p="x"]` buttons and `.ppanel#p-x` panels; `.active` is toggled, scoped to the nearest `<section>` |
| Marquee | pure CSS — duplicate the items statically, no JS |

| Auto-advancing tabs | `[data-tabs-loop="ms"]` around a `.ptab` group; `[data-tabs-next]` steps it |
| Select a tab from elsewhere | `[data-goto-tab="n"]` anywhere on the page |
| Endless row | `.mc-loop > .mc-loop__track`; the first item moves to the end |
| Scroll-snap carousel | `.mc-car` + `[data-car][data-go]`, either axis |

> **Symptom:** the markup renders but nothing responds to a click. Either the
> script did not load — it is content-gated on `class="ptab` / `acc-item` /
> `acc-head` — or production is running an older copy without the behaviour you
> used. Fetch the deployed file and grep it before assuming anything else.

## The theme paints your buttons

`elementor-kit-7676` — the Elementor global kit, on every page on the site —
styles bare `<button>` elements:

```css
.elementor-kit-7676 button              { background-color:#FF9E1B }   /* the site's orange */
.elementor-kit-7676 button:hover,
.elementor-kit-7676 button:focus        { background-color:#12BF6D; color:#fff }   /* --mc-green */
```

The rest state loses to any two-class rule of yours, so it never shows and you
never think about it. **The hover and focus states carry a pseudo-class, which
outranks a plain two-class rule**, so the first time anyone clicks a control it
turns green and stays green until focus moves. On the Asana page that was the
carousel's next arrow, and it survived a build, a review and a push because no
static render ever enters that state.

So every `<button>` in an AI page must state its own `:hover`, `:focus`,
`:focus-visible` and `:active` — background *and* colour. `verify` warns when
one does not. `<a>` elements are not affected; the kit's selector is `button`,
`input[type=button]`, `input[type=submit]` and `.elementor-button`.

## Links must be relative

`/solutions/xxx/`, never `https://masterconcept.ai/solutions/xxx/`. Absolute
links break WPML language switching and any staging copy.

## Dynamic blocks are shortcodes

Never hardcode post cards or author lists — they go stale and they do not
localise.

| Block | Shortcode |
|---|---|
| Related posts / post list | `[wpb_post_list preset="…"]` (presets live in WP Buddy → Post Lists) |
| Author list | `[wpb_authors]` |
| Zoom webinar | `[wpb_zoom]` / `[zoom_webinar]` |
| Menu | `[wpbuddy_menu]` |

## The "Contact us" CTA reuses the shared popup

The site has one ElementsKit contact popup per language (the `contact-popup`
family). Trigger the existing one; the WP Buddy Header-CTA tool already maps the
right popup id and label per language. **Never build another form.**

## Images

Upload to the media library (`POST /wp-json/wp/v2/media`) and reference the
uploaded URL. Never hotlink `figma.com` or `s3-alpha-sig.figma.com` — those URLs
expire.

- Reuse what is already uploaded. `manifest.json` in the build directory maps
  local asset → media id → URL; `push` skips anything listed there.
- Prefer WebP and size images to their display size. Mobile PageSpeed on this
  site is sensitive to image weight.
- SiteGround's WAF sometimes refuses an upload before it reaches WordPress. It
  comes back as an HTML error page rather than JSON — shrink the file or convert
  it, then retry.
- The media library **normalises what it serves**. Five product shots drawn at
  five different sizes in Figma (563x520, 597x520, 595x425, 549x448, 598x520)
  all came back as 1032x898. Read the shipped size off the page, not the frame.

### Every image needs its box pinned in CSS

SiteGround lazy-loads: until an image scrolls into view its `src` is a **1x1
transparent GIF**. `width`/`height` attributes do not save you — a browser uses
them only while the image is *unloaded*, and the placeholder counts as loaded —
so `height:auto` resolves against a 1:1 ratio and reserves a **square**.

This shipped three times on one page before anyone named it:

| Element | Intended | Rendered |
|---|---|---|
| watermark SVG 318x280 | 318x280 | 318x318 |
| hero wordmark 467x67 at `width:280` | 280x40 | 280x280 |
| product panel image | 337x293 | 390x390, panel 454px tall and empty |

Give every `<img>` an explicit `aspect-ratio` (or both `width` and `height`) in
the page CSS. `verify` warns about any image whose box still depends on the
file's own dimensions; `mobile` catches the ones that slipped through by
comparing the rendered box against `naturalWidth`.

An `<svg>` used as an image is worse: several report `naturalWidth` **1**
whether or not they have loaded, so they are square from the first paint.

## URLs are governed by Permalink Manager Pro

The native `post_name` is not what the site serves. After setting or changing a
slug, run `mc/regenerate-permalink`. The WPML language prefix (`/zh-hant/`,
`/zh-hans/`) is added automatically, so the slug itself stays the plain English
one with no language marker — and all languages in a translation group share it.

Changing a slug on a live page means adding a 301 from the old URL, including
for child pages: `POST /wp-json/redirection/v1/redirect`.

## WPML

Each language is a separate post. Creating three pages is not enough — they must
share one translation group (`trid`) or the language switcher will not connect
them. Use `mc/create-localized-page` with `translation_of`; do **not** use
`ewpa/set-post-language`, which reports success without persisting.

Terms must be the language-appropriate translation (resolve via
`wpml_object_id`). An EN term on a 繁 page mis-links it and can render in the
wrong language.

## There is no undo, and no staging

Post revisions are **disabled site-wide**. Before overwriting a page body, save
the current one (`GET /wp-json/wp/v2/pages/{id}?context=edit` → `content.raw`).
`push` does this automatically into `build/<slug>/backups/`. The Figma frame and
the generated HTML are the source of truth.

Work draft → review → publish. After publishing, purge the page cache — logged-out
visitors are served a cached copy:

```
PUT /wp-json/siteground-optimizer/v1/purge-cache
```

## The phone layout, since the frame is desktop-only

No Figma frame in this file has a mobile variant. That does not make the phone
layout a free choice — the rest of the site has one, and these numbers were
measured off `/solutions/geospatial/` at a real 390px viewport:

| | phone (<=768) | tablet (1024) | desktop |
|---|---|---|---|
| h1 | 28 / 36.4 | 36.9 / 47.9 | frame |
| h2 | 24 / 31.2 | 28.7 / 37.3 | frame |
| body | 16 / 27.2 | 16 / 27.2 | frame |
| gutter | 28px each side | 24px | frame |
| section padding | 50px vertical | 50px | frame |

- **The breakpoints are 768 and 1024**, not 767. geospatial is already on the
  phone scale at exactly 768.
- **16px is the floor for body copy.** A frame that says 14 for card text or an
  accordion answer is a desktop number; raise it. Label pills stay at 14.
- **The site header is `position: fixed`, 81px tall**, on every page. geospatial
  puts its `h1` at y105, and `.site-main` starts 9px above the document origin,
  so a first section needs ~114px of top padding to land in the same place.
- **A card is inset, a band is not.** geospatial's `.cta-band` sits at x28 w337
  with a 15px radius and 24px of padding, and its button is content-width
  (padding 14/34), not a fixed width. A full-bleed background band keeps its
  edges; a card that bleeds loses its radius and stops reading as a card.
- Every desktop line-height taken from Figma is an **absolute px value** and
  will not scale. A 30px heading left in a 72px line box is the single worst
  thing on a phone. Restate every one of them in the phone query.
- A desktop rule written as `.page .section .h1` (0,3,0) outranks a phone
  override written as `.page .h1` (0,2,0). Match the specificity or the
  override silently does nothing — measure, do not assume.

Run `mobile <slug> --url ...` for the four checks worth automating: sideways
overflow, body copy under 16px, tap targets under 40px, and content jammed
against the screen edge.

## Endpoint reference

| Purpose | Endpoint |
|---|---|
| Run an ability | `POST /wp-json/wp-abilities/v1/abilities/{name}/run` |
| List abilities | `GET /wp-json/wp-abilities/v1/abilities` |
| Media upload | `POST /wp-json/wp/v2/media` |
| Read a page body | `GET /wp-json/wp/v2/pages/{id}?context=edit` |
| 301 redirect | `POST /wp-json/redirection/v1/redirect` |
| Purge cache | `PUT /wp-json/siteground-optimizer/v1/purge-cache` |

Abilities available: `mc/brand-guide`, `mc/create-localized-page`,
`mc/find-posts`, `mc/get-meta`, `mc/regenerate-permalink`, `mc/set-language`,
`mc/set-post-html`, `mc/set-terms`, plus the `ewpa/*` set.

Auth is Basic with a WordPress **Application Password**.

Two things about talking to this host:

- The credentials file is `~/.figma-wp/.env` and the site key is
  **`WP_BASE_URL`**. Do not `source` it from a shell — an application password
  contains spaces and shell metacharacters, and `set -a; . .env` will execute
  part of it. Parse it, or feed curl through `curl -K -` on stdin so the
  password never reaches the process list.
- The site **403s a plain `urllib` request**, including the purge endpoint. Send
  a browser `User-Agent`, or use `curl`.
