# Elementor widgets — how each one is written as raw HTML

An Elementor build (`extract --from elementor`) lists every widget on the page
in `build/<slug>/elementor.json` → `widgets`, each with its type, the top-level
section it sits in (`s1`, `s2`, …) and a tier. This file is the recipe for
tiers 1 and 2. **A tier-3 widget has no recipe yet**: `extract` prints
`tier 3: <type> — no recipe in references/elementor-widgets.md yet`. Stop
there, write the recipe here first — generic, no page ids, URLs or copy, this
repo is public — and then build the page.

The recipes say what to write, not what the page looks like. The look comes
from the references (`design.png`, `design-768.png`, `design-390.png`) and the
numbers in `design.json`; the tablet and phone layouts come from the original's
own rules in `elementor.json` → `responsive`. Reuse the behaviours
`wpbuddy-page.js` already has (`.acc-item`, `.ptab`/`.ppanel`, `.mc-car`,
`.mc-loop`) before writing `page.js` for one page.

## Measures every Elementor page shares

These are not in any widget's settings, and each one, missed, moves everything
below it a few pixels — they add up down the page.

- **Every column has 10px of padding** on all four sides. Elementor's default
  column gap puts it on the column's inner wrapper, so a two-column row of
  1140px has 20px between its texts and 10px inside each outer edge.
- **The theme gives `h1`–`h5` `padding-top:10px`** and its own letter-spacing.
  A heading written without them sits 10px high and sets a little tight.
  Take the spacing from `texts[].type.ls`, not by eye.
- **The breakpoints are Elementor's: tablet ≤1024, phone ≤767.** Write
  `@media (max-width:1024px)` and `@media (max-width:767px)`. Not the house
  768 from `site-rules.md` — the references were drawn by Elementor's queries,
  and at exactly 768 the original is on its tablet layout.
- **`elementor.json` → `responsive`** holds the page's own rules inside those
  two queries, one string per rule, keyed by Elementor's element selectors.
  Read them beside `design-768.png` and `design-390.png`; they are the numbers
  behind what the pictures show.
- **`flags.hidden`** says the page hides something per device
  (`elementor-hidden-mobile` / `-tablet` / `-desktop`). Find it in the
  reference where it disappears and hide it with `display:none` in the same
  query.
- **The converted page loses `elementor-page-<id>` on `<body>`**, so the
  theme's width clamp now applies to it. The wrapper needs the 50vw breakout
  (`site-rules.md`, *Raw post_content pages are clamped*), or every band
  renders as a 1140px stripe.
- **No `vh`.** The references are shot with every `vh` pinned to a 1000px
  viewport. A build in `vh` renders a different height in `diff` and in the
  framed `mobile` / `diff --width` checks than the reference does, and every
  position below it drifts. Write the px the reference measured.
- **Motion, entrance animations, sticky** (`flags.motion_fx`, `flags.sticky`)
  show in the references in their final state. Build that state; whether the
  motion comes along is the user's call, not the recipe's.

## Tier 1

### heading

The same tag as the original (`texts[].tag`) — an `h2` stays an `h2`, the page
outline and the theme's heading rules both depend on it. Size, weight, line
height and colour from `texts[].type`.

### text-editor

The widget is a rich-text block: copy its paragraphs and lists as they are,
`<p>`, `<ul>`, `<strong>`, `<a>`. The theme's paragraph margins apply to raw
`<p>` too; measure rather than assume they match the widget's.

### image

`<img>` with the manifest's asset, a non-empty `alt` and `title`, and its box
pinned in CSS. `manifest.json` already points at the media library's copy, so
push uploads nothing. An Elementor thumbnail (`/uploads/elementor/thumbs/`) has
`id: null` and stays pointed at its URL.

### button

`<a>` with the original's `href`. Relative links for the site's own pages; a
popup trigger keeps its `#elementor-action…` href (see *Popup buttons* below).
Off-site links take `target="_blank" rel="noopener"` as everywhere else.

### divider

A border on a block, or `<hr>` with its margins reset. Its weight, colour and
width come from the reference; Elementor puts padding above and below it that
is part of the gap.

### spacer

Not an element: a margin or padding on the block above or below it, in px from
the reference.

### icon

See *Font Awesome icons* below. Keep the icon's box size, not only its glyph
size — the box is what the gap is measured from.

### icon-list

`<ul>` with one `<li>` per item, the icon in the item before the text. Its
divider, gap and icon colour come from the reference.

### icon-box

Icon, heading, text in one block. Same recipe as `icon` plus `heading` plus
`text-editor`; the box's alignment (icon on top or beside) changes per
breakpoint more often than not — read `responsive`.

### image-box

Image, heading, text in one block. Same recipe as `image` plus `heading` plus
`text-editor`.

### html

Copy the markup. Anything with a `<script>` goes to `page.js` instead — not
into the body, where wptexturize breaks it (`SKILL.md`, *page.js*). A HubSpot
form has its own recipe below.

### video

An embed `<iframe>` (YouTube, Vimeo) or a `<video>` element, with the
original's aspect ratio held by `aspect-ratio`, not a fixed height.

### menu-anchor

Its id, on the section it marks. Any `href="#<id>"` on the site points at it.

### shortcode

The same shortcode, in the body. **Never write a shortcode's name in
`page.css`**, not even in a comment: WordPress runs shortcodes over the whole
body, the `<style>` block included, and the stylesheet dies at that point.
`verify` fails it.

### blockquote

`<blockquote>`, with its border or quote mark from the reference.

### counter

The final number, static. The widget counts up from zero on scroll; the
reference shows where it stops.

### social-icons

One link per network, `target="_blank" rel="noopener"`, with the icon as
below and a non-empty `title`.

## Tier 2

### posts

`[wpb_post_list …]`, built from `elementor.json` → `posts_queries` (the
taxonomy, term, count and order the widget used; the front end does not carry
them). For example `[wpb_post_list tax="post_tag" term="<term>" count="3"
columns="3"]`. The card styling is page CSS over the shortcode's markup.

The list is per language. A language with no matching term, or no posts on it,
renders the list empty — and an empty list under a heading reads as broken.
Hide the whole band when that happens:

```css
.<x>-page .<x>-more:has(.wpbuddy-pl-empty){display:none}
```

It shows itself again once there are posts.

### nested-tabs

`.ptab[data-p]` buttons and `.ppanel#p-<data-p>` panels, run by
`wpbuddy-page.js`. Follow the original on the phone: Elementor's nested tabs
can turn into an accordion of stacked titles below a breakpoint —
`design-390.png` shows whether this one does — and the build has to read the
same way. One way that keeps the shared behaviour: a second
`.ptab` per panel, shown only in the phone query, placed directly above its
panel.

### tabs

The older tabs widget. Same recipe as `nested-tabs`.

### nested-accordion

`.acc` around `.acc-item > .acc-head` + body, run by `wpbuddy-page.js`. It
is single-open: opening one closes its siblings, and the open item carries
`.open`. Write the body's hidden and open states in the page CSS. The
reference shows whichever item was open when the page loaded — usually the
first, or none.

### accordion

Same recipe as `nested-accordion`.

### toggle

Same recipe as `nested-accordion`. A toggle lets several items stay open at
once and `.acc` does not; if the original depends on that, say so to the user
rather than writing a page-only script for it.

### flip-box

Pure CSS: front and back stacked in one card, swapped on `:hover` **and**
`:focus`, with `tabindex="0"` on the card so a keyboard and a tap can turn it
too. Use the original's direction and timing (a flip, a slide up). Copy on
the back is copy all the same — `verify` wants it.

### image-carousel

For a logo strip that never stops: a pure CSS marquee — the items written out
twice in one track, `animation: translateX(-50%)` on a loop, and a
`prefers-reduced-motion` rule that stops it. For a carousel the visitor steps
through: `.mc-car` with `[data-car][data-go]` controls (`.mc-car[data-car-loop]`
for one that also advances on its own). Items visible per width come from
`responsive`.

### price-table

An HTML `<table>`, or a grid of cards if that is what the widget drew. On the
phone, do what the original does: if it collapses columns (hides them, or
folds them into a row that opens on tap), do the same, from
`design-390.png`.

### animated-headline

Static text with the highlighted word in a `<span>`, and the mark the widget
draws (circle, underline, strike) as an inline SVG path over it. The stroke
can animate with `stroke-dashoffset` in CSS; stop it under
`prefers-reduced-motion`.

## Things that are not widgets but need a recipe

### HubSpot form in an html widget

`flags.hubspot` says the page embeds one. Load it from `page.js`, never inline:
create the target `<div id>` in the body, then in `page.js` load
`https://js.hsforms.net/forms/embed/v2.js` once and call
`hbspt.forms.create({region, portalId, formId, target})` with the ids the
original's embed uses. Check `window.hbspt` first, so a second form on the page
does not load the script twice.

### Popup buttons

A button that opens an Elementor popup keeps its
`#elementor-action:action=popup:open&settings=…` href (URL-encoded as the
original writes it), and never takes `target="_blank"`. `elementor.json` →
`popups` lists the popup ids the original uses.

**The popup id is per language.** A popup only opens on a page that loads it,
and pages in another language often load a different one. For usage A in
another language: open a published page in **that** language, find which
`data-elementor-type="popup"` document it prints and its id, and use that id in
the button's `settings` (the base64 JSON's `"id"`). The same popup id as the
original is a button that renders and does nothing.

### Font Awesome icons

`<i class="fas fa-…">` works only where the page loads Font Awesome. Elementor
loads it for its own icon widgets; a raw page may not get it from the body.
Check the target page's front end (or any raw page on the site) for a Font
Awesome stylesheet: if it is there, use the `<i>`; if not, inline the icon as
an SVG. Either way look at the draft — a missing icon font renders an empty
square or nothing, and no string check can see it.
