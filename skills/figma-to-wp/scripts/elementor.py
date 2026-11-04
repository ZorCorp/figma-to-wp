"""Read a live Elementor page: what it is made of, how it behaves on a phone,
and a copy of it that a headless Chrome can measure and screenshot.

Standard library only, and no import of figma_to_wp (it imports this, not the
other way round). Everything here is a pure function of its arguments except
serve_live(), which starts a local server. The script only reports facts that
have one right answer — widget types, popup ids, media-query rules — and never
guesses a layout; that stays a human reading the reference screenshot.
"""
import base64
import binascii
import http.server
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# Same string figma_to_wp.UA sends; the site's WAF rejects the default urllib one.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) figma-to-wp"

# Spec §1. TIER2 holds only what tier 2 adds to tier 1; anything in neither is tier 3.
TIER1 = frozenset({
    "heading", "text-editor", "image", "button", "divider", "spacer", "icon",
    "icon-list", "icon-box", "image-box", "html", "video", "menu-anchor",
    "shortcode", "blockquote", "counter", "social-icons"})
TIER2 = frozenset({
    "posts", "nested-accordion", "accordion", "toggle", "nested-tabs", "tabs",
    "flip-box", "image-carousel", "price-table", "animated-headline"})

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}
PAGE_TYPES = ("wp-page", "wp-post")
# A section or container nested inside any of these is not a top-level one
# (an inner section sits in a column; a nested container in a container).
ELEMENT_TYPES = ("section", "container", "column", "widget")
BREAKPOINTS = ("1024", "767")


def _tier(widget_type):
    return 1 if widget_type in TIER1 else 2 if widget_type in TIER2 else 3


# --- document and element scan ------------------------------------------------

def _doc_tags(html):
    """(tag_start, type, id) of every element carrying data-elementor-type, in
    page order. A match inside a <script> (the frontend JS has the selector as
    a string) is not a tag and is skipped."""
    out = []
    for m in re.finditer(r"data-elementor-type\s*=", html):
        ts = html.rfind("<", 0, m.start())
        if ts < 0 or html.find(">", ts, m.start()) != -1 or not re.match(r"<[a-zA-Z]", html[ts:ts + 2]):
            continue
        tag = html[ts:html.find(">", m.end()) + 1]
        t = re.search(r"data-elementor-type\s*=\s*[\"']([^\"']*)[\"']", tag)
        i = re.search(r"data-elementor-id\s*=\s*[\"'](\d+)[\"']", tag)
        out.append((ts, t.group(1) if t else "", int(i.group(1)) if i else None))
    return out


class _Done(Exception):
    pass


class _Scan(HTMLParser):
    """Every element of one document, with its absolute offsets and, for
    sections, the top-level index. Stops when the document's root closes.
    Depth is recovered from the matching end tag (an implicit </p> or </li> is
    not modelled), as wpsafe._Top does."""

    def __init__(self, src, base):
        super().__init__(convert_charrefs=False)
        self.src, self.base = src, base
        self.lines = [0] + [m.end() for m in re.finditer("\n", src)]
        self.stack = []          # [tag, is_element_type, top_index|None]
        self.els, self.end = [], None
        self.typed = 0           # open ancestors that are sections/columns/widgets
        self.tops = 0
        self.cur = None          # index of the top-level section we are inside

    def _off(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        off = self._off()
        etype = a.get("data-element_type")
        top = None
        if etype in ("section", "container") and not self.typed:
            self.tops += 1
            top = self.tops
            self.cur = top
        self.els.append({
            "tag": tag, "attrs": a, "off": self.base + off,
            "tag_end": self.base + off + len(self.get_starttag_text() or ""),
            "root": not self.els, "top": top, "sec": self.cur})
        if tag in VOID:
            return
        typed = etype in ELEMENT_TYPES
        if typed:
            self.typed += 1
        self.stack.append([tag, typed, top])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                break
        else:
            return
        while len(self.stack) > i:
            _, typed, top = self.stack.pop()
            if typed:
                self.typed -= 1
            if top is not None:
                self.cur = None
        if not self.stack:
            off = self._off()
            self.end = self.base + self.src.find(">", off) + 1
            raise _Done


def _scan(html, start):
    p = _Scan(html[start:], start)
    try:
        p.feed(p.src)
        p.close()
    except _Done:
        pass
    return p.els, p.end


def _page(html, post_id):
    """(start, end, elements) of the page body doc, or None."""
    docs = _doc_tags(html)
    for n, (ts, typ, pid) in enumerate(docs):
        if typ in PAGE_TYPES and pid == int(post_id):
            els, end = _scan(html, ts)
            nxt = docs[n + 1][0] if n + 1 < len(docs) else len(html)
            return ts, min(end, nxt) if end else nxt, els
    return None


def page_doc(html, post_id):
    """(start, end) of the page body: from the opening tag of the
    wp-page/wp-post doc with data-elementor-id=post_id to where it closes or
    the next doc starts, whichever is first. None when the page renders some
    other doc (a translation that still shows the original's)."""
    p = _page(html, post_id)
    return (p[0], p[1]) if p else None


# --- copy -----------------------------------------------------------------------

# Phrasing elements: their text belongs to the element around them. Every
# other element (headings, p, li, td, button, div, …) is a copy unit of its own.
INLINE = {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em",
          "font", "i", "ins", "kbd", "mark", "q", "s", "samp", "small", "span",
          "strong", "sub", "sup", "time", "u", "var"}
NOT_COPY = {"script", "style", "noscript", "template", "svg", "iframe"}


class _Copy(HTMLParser):
    """Copy strings in document order. A string is the text of one unit, its
    inline descendants included; a nested unit ends the string before it and
    starts a fresh one after it, so text never runs across a block boundary."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []            # [tag, is_unit, widget]
        self.buf = []              # text of the innermost open unit so far
        self.skip = 0
        self.out = []              # (text, widget)

    def _widget(self):
        return self.stack[-1][2] if self.stack else None

    def _flush(self):
        t = " ".join("".join(self.buf).split())
        self.buf = []
        if not t:
            return
        w = self._widget()
        # Elementor prints some labels twice in one widget (a tab title for
        # desktop and again for the phone accordion); that is one string.
        if self.out and self.out[-1] == (t, w):
            return
        self.out.append((t, w))

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.buf.append(" ")
            return
        if tag in VOID:
            return
        a = dict(attrs)
        widget = a.get("data-id") if a.get("data-element_type") == "widget" else self._widget()
        unit = tag not in INLINE
        if unit and not self.skip:
            self._flush()
        if tag in NOT_COPY:
            self.skip += 1
        self.stack.append([tag, unit, widget])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                break
        else:
            return
        while len(self.stack) > i:
            t, unit, _ = self.stack[-1]
            if t in NOT_COPY:
                self.skip -= 1
            elif unit and not self.skip:
                self._flush()
            self.stack.pop()

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def copy_texts(html, post_id):
    """Every string of copy in the page body, whole and in document order —
    hidden tab panels and phone-only blocks included, since the rebuilt page
    has to carry them too. [] when the page renders no doc of its own."""
    rng = page_doc(html, post_id)
    if not rng:
        return []
    p = _Copy()
    p.feed(html[rng[0]:rng[1]])
    p.close()
    p._flush()                 # a doc cut off before its close tag
    return [t for t, _ in p.out]


# --- inventory ----------------------------------------------------------------

def _popup_ids(text):
    ids = []
    pat = (r"#elementor-action(?:%3A|:)action(?:%3D|=)popup(?:%3A|:)open"
           r"(?:%26|&(?:amp;)?)settings(?:%3D|=)([A-Za-z0-9%+/=_-]+)")
    for m in re.finditer(pat, text):
        raw = urllib.parse.unquote(m.group(1)).rstrip("=")
        try:
            data = json.loads(base64.b64decode(
                raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4)))
            pid = int(data["id"])
        except (binascii.Error, ValueError, KeyError, TypeError):
            continue
        if pid not in ids:
            ids.append(pid)
    return ids


def _template(html):
    m = re.search(r"<body\b[^>]*?\sclass\s*=\s*[\"']([^\"']*)[\"']", html, re.I)
    cls = m.group(1).split() if m else []
    for c in cls:
        if c.startswith("page-template-") and not c.endswith("-php"):
            return c[len("page-template-"):]
    if "elementor-template-full-width" in cls:
        return "elementor_header_footer"
    if "elementor-template-canvas" in cls:
        return "elementor_canvas"
    return ""


def _walk_widgets(node, found):
    if isinstance(node, list):
        for n in node:
            _walk_widgets(n, found)
    elif isinstance(node, dict):
        if node.get("widgetType") == "posts" and isinstance(node.get("settings"), dict):
            found.append(node["settings"])
        _walk_widgets(node.get("elements"), found)


def _posts_query(settings):
    """The settings with the skin-prefixed count (classic_posts_per_page,
    cards_posts_per_page) folded into posts_per_page. When several skins have
    a count, the active one (_skin) wins."""
    out = {k: v for k, v in settings.items() if not k.endswith("posts_per_page")}
    counts = {k: v for k, v in settings.items() if k.endswith("posts_per_page")}
    skin = settings.get("_skin")
    for k in ([f"{skin}_posts_per_page"] if skin else []) + ["posts_per_page"] + list(counts):
        if k in counts:
            out["posts_per_page"] = counts[k]
            break
    return out


def posts_queries(elementor_data):
    """Settings of every posts widget in the page's _elementor_data (a list or
    the JSON string of one). The query is not in the front-end HTML."""
    if isinstance(elementor_data, (str, bytes)):
        try:
            elementor_data = json.loads(elementor_data)
        except ValueError:
            return []
    found = []
    _walk_widgets(elementor_data, found)
    return [_posts_query(s) for s in found]


def inventory(html, post_id, elementor_data=None):
    """Facts about the page body: widget list with tiers, popups, posts
    queries, motion/sticky/hidden/HubSpot flags. Header, footer and popup docs
    are not counted."""
    inv = {"post_id": int(post_id), "template": _template(html), "widgets": [],
           "tier3": [], "popups": [], "posts_queries": posts_queries(elementor_data),
           "flags": {"motion_fx": False, "sticky": False,
                     "hidden": {"mobile": False, "tablet": False, "desktop": False},
                     "hubspot": False}}
    page = _page(html, post_id)
    if not page:
        return inv
    start, end, els = page
    text = html[start:end]
    flags = inv["flags"]
    for e in els:
        a = e["attrs"]
        if a.get("data-element_type") == "widget" and a.get("data-widget_type"):
            wt = a["data-widget_type"].split(".")[0]
            inv["widgets"].append({"type": wt, "tier": _tier(wt),
                                   "section": "s%d" % e["sec"] if e["sec"] else None})
            if _tier(wt) == 3 and wt not in inv["tier3"]:
                inv["tier3"].append(wt)
        for k in ("mobile", "tablet", "desktop"):
            if re.search(r"(?:^|\s)elementor-hidden-%s(?:\s|$)" % k, a.get("class", "")):
                flags["hidden"][k] = True
        if a.get("data-settings"):
            try:
                st = json.loads(a["data-settings"])
            except ValueError:
                st = {}
            if isinstance(st, dict):
                if any(k.startswith("motion_fx") and v for k, v in st.items()):
                    flags["motion_fx"] = True
                if st.get("sticky"):
                    flags["sticky"] = True
    inv["popups"] = _popup_ids(text)
    flags["hubspot"] = bool(re.search(r"hbspt\.forms\.create|js\.hsforms\.net", text))
    return inv


# --- responsive rules ---------------------------------------------------------

def _blocks(css, i=0):
    """Split css from i into (prelude, body) pairs at brace depth 0. A rule's
    prelude is its selector, an at-rule's is its query; bodies keep their braces
    out. A trailing fragment with no block is dropped."""
    out, depth, cur, body_start = [], 0, i, None
    for j in range(i, len(css)):
        c = css[j]
        if c == "{":
            if depth == 0:
                prelude, body_start = css[cur:j], j + 1
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                out.append((prelude.strip(), css[body_start:j]))
                cur = j + 1
        elif c == ";" and depth == 0:
            cur = j + 1                      # @import / @charset
    return out


def responsive_rules(css):
    """{"1024": [rule…], "767": [rule…]}: the rules inside @media blocks whose
    only condition is max-width 1024px (Elementor tablet) or 767px (mobile).
    A block with a min-width is a band, not an inheritance, and is left out."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = {bp: [] for bp in BREAKPOINTS}
    for prelude, body in _blocks(css):
        m = re.fullmatch(r"@media\s*(?:screen\s+and\s*)?\(\s*max-width\s*:\s*(\d+)px\s*\)", prelude, re.I)
        if not m or m.group(1) not in out:
            continue
        for sel, decl in _blocks(body):
            out[m.group(1)].append(re.sub(r"\s+", " ", "%s{%s}" % (sel, decl)).strip())
    return out


def doc_css(css, post_id):
    """Only the rules of css that style document post_id (a selector naming
    .elementor-<id>), each @media block kept around the ones inside it. A site
    that prints Elementor CSS inline puts every document's rules in one
    <style>; this is the page's share of it."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    mine = re.compile(r"\.elementor-%d(?![\w-])" % int(post_id))
    out = []
    for prelude, body in _blocks(css):
        if prelude.lower().startswith("@media"):
            inner = ["%s{%s}" % (sel, decl) for sel, decl in _blocks(body) if mine.search(sel)]
            if inner:
                out.append("%s{%s}" % (prelude, "".join(inner)))
        elif not prelude.startswith("@") and mine.search(prelude):
            out.append("%s{%s}" % (prelude, body))
    return "\n".join(out)


# --- snapshot -----------------------------------------------------------------

_ATTR = r"""\s%s\s*=\s*(?:"([^"]*)"|'([^']*)')"""

# The page doc and everything inside it stay; any other Elementor doc goes. A
# nested doc (a template inside the page) must survive, hence the :not(… *).
# Keyed on the doc id so the marker attribute appears once, on the root.
SNAPSHOT_CSS = (
    "[data-elementor-type]:not([data-elementor-id=\"%(id)s\"]):not([data-elementor-id=\"%(id)s\"] *),"
    ".elementor-popup-modal,#hs-eu-cookie-confirmation{display:none!important}"
    ".elementor-invisible{visibility:visible!important;opacity:1!important}")


def _get_attr(tag, name):
    m = re.search(_ATTR % re.escape(name), tag)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _set_attr(tag, name, value):
    new = ' %s="%s"' % (name, value.replace('"', "&quot;"))
    pat = re.compile(_ATTR % re.escape(name))
    if pat.search(tag):
        return pat.sub(lambda m: new, tag, 1)
    return re.sub(r"^<\w+", lambda m: m.group(0) + new, tag, count=1)


def _unlazy(tag):
    """SiteGround's lazy loader keeps a placeholder in src and the real URL in
    data-*; Chrome renders the placeholder until a scroll the screenshot never
    makes."""
    for real, names in (("src", ("data-src", "data-lazy-src")),
                        ("srcset", ("data-srcset", "data-lazy-srcset"))):
        for n in names:
            v = _get_attr(tag, n)
            if v:
                tag = _set_attr(tag, real, v)
                break
    return re.sub(r"""\sloading\s*=\s*(?:"lazy"|'lazy'|lazy\b)""", "", tag)


def _lazyloaded(tag):
    """Elementor holds back a container's background image until its JS adds
    e-lazyloaded; the snapshot's scripts may not have run by the shot."""
    cls = _get_attr(tag, "class") or ""
    if "e-lazyloaded" in cls.split():
        return tag
    return _set_attr(tag, "class", (cls + " e-lazyloaded").strip())


def snapshot(html, origin, post_id, measure_js):
    """The live page made safe to open from a local server: origin URLs made
    root-relative, lazy images real, the page body marked (data-fw-root, and
    data-section="sN" on its top-level sections and containers), everything
    else Elementor draws hidden, and measure.js appended."""
    page = _page(html, post_id)
    if not page:
        raise ValueError("no wp-page/wp-post Elementor doc with id %s" % post_id)
    # Edits go in by offset, last first, so they must precede any rewrite that
    # changes lengths.
    edits = []
    for e in page[2]:
        mark = (" data-fw-root" if e["root"] else "") + \
               (' data-section="s%d"' % e["top"] if e["top"] else "")
        if mark:
            edits.append((e["off"] + 1 + len(e["tag"]), mark))
    for pos, mark in sorted(edits, reverse=True):
        html = html[:pos] + mark + html[pos:]

    host = re.match(r"https?://(.*)", origin.rstrip("/"))
    o = "https?://" + re.escape(host.group(1)) if host else re.escape(origin.rstrip("/"))
    html = re.sub(o + r"(?![\w.:-])/?", "/", html)
    html = re.sub(r"<(?:img|source)\b[^>]*>", lambda m: _unlazy(m.group(0)), html)
    html = re.sub(r"<[a-zA-Z][^>]*\sdata-element_type\s*=\s*[\"']container[\"'][^>]*>",
                  lambda m: _lazyloaded(m.group(0)), html)

    style = "<style data-fw>%s</style>" % (SNAPSHOT_CSS % {"id": int(post_id)})
    if re.search(r"</head>", html, re.I):
        html = re.sub(r"</head>", lambda m: style + m.group(0), html, count=1, flags=re.I)
    else:
        html = style + html
    script = "<script>%s</script>" % measure_js
    i = html.lower().rfind("</body>")
    return html + script if i < 0 else html[:i] + script + html[i:]


# --- viewport height ----------------------------------------------------------

# A number followed by vh (or dvh/svh/lvh), not one glued to a name: the
# lookbehind keeps ".h-100vh" a class, the leading sign keeps "-5vh" negative.
_VH = re.compile(r"(?<![\w.#-])(-?(?:\d+\.?\d*|\.\d+))[dsl]?vh\b")


def pin_vh(css, viewport_h):
    """css with every vh length written out in px against viewport_h. The
    screenshot's viewport is thousands of px tall so the whole page fits, and
    a 96vh hero would fill it."""
    def px(m):
        v = round(float(m.group(1)) * viewport_h / 100, 2)
        return "%gpx" % v
    return _VH.sub(px, css)


def pin_vh_html(html, viewport_h):
    """pin_vh inside <style> blocks and style="" attributes only, so copy that
    happens to say "100vh" is left as written."""
    html = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)",
                  lambda m: m.group(1) + pin_vh(m.group(2), viewport_h) + m.group(3),
                  html, flags=re.S | re.I)
    return re.sub(r"""(\sstyle\s*=\s*)("[^"]*"|'[^']*')""",
                  lambda m: m.group(1) + pin_vh(m.group(2), viewport_h), html, flags=re.I)


# --- local server -------------------------------------------------------------

def serve_live(dir, origin, ua=UA, viewport_h=None):
    """Serve dir/live.html locally and forward every other path to origin, so
    the snapshot's relative URLs (CSS, fonts, scripts, images) load from the
    live site while the page itself stays the edited local copy. With
    viewport_h, vh lengths in proxied stylesheets are pinned to it (pin_vh)."""
    origin = origin.rstrip("/")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, ctype, body):
            # Chrome drops its connections the moment --dump-dom or the
            # screenshot is done; a reply still in flight is not an error.
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path == "/live.html":
                with open(os.path.join(dir, "live.html"), "rb") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
            req = urllib.request.Request(origin + self.path, headers={"User-Agent": ua})
            try:
                r = urllib.request.urlopen(req, timeout=30)
                try:
                    body = r.read()
                finally:
                    r.close()
                status, headers = r.status, r.headers
            except urllib.error.HTTPError as e:
                status, headers, body = e.code, e.headers, e.read()
            except (urllib.error.URLError, OSError) as e:
                return self._send(502, "text/plain", ("upstream: %s" % e).encode())
            ctype = headers.get("Content-Type") if headers is not None else None
            ctype = ctype if isinstance(ctype, str) else "application/octet-stream"
            if viewport_h and ctype.split(";")[0].strip().lower() == "text/css":
                body = pin_vh(body.decode("utf-8", "replace"), viewport_h).encode("utf-8")
            self._send(status if isinstance(status, int) else 200, ctype, body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
