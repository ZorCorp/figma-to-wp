"""Make a Claude Design export safe to put in a masterconcept.ai post body.

Canonical copy: figma-to-wp/scripts/wpsafe.py. mcp-wp vendors it through
scripts/sync-wpsafe.sh — edit it here, never there.

Standard library only, no network. The contract and the reset are in
docs/superpowers/specs/2026-09-28-claude-design-to-wp-design.md, sections 4.1
and 4.3; every rule below exists because post 78436 broke without it.
"""
import re
from html import unescape
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}
# Things that may sit beside the root and are handled by clean() rather than
# being a second root: font links are moved in, the rest is checked or dropped.
NOT_ROOT = {"link", "script", "style", "meta", "title", "noscript"}


def split_document(html):
    """(head_inner, body_inner). Strips DOCTYPE, <html>, <body> and wp:html."""
    h = re.search(r"<head\b[^>]*>(.*?)</head>", html, re.S | re.I)
    head = h.group(1) if h else ""
    b = re.search(r"<body\b[^>]*>(.*)</body>", html, re.S | re.I)
    if b:
        body = b.group(1)
    else:
        body = html.replace(h.group(0), "") if h else html
    body = re.sub(r"<!DOCTYPE[^>]*>|</?html\b[^>]*>|</?body\b[^>]*>", "", body, flags=re.I)
    body = re.sub(r"<!--\s*/?wp:html\s*-->", "", body)
    return head, body


class _Top(HTMLParser):
    """Top-level element spans. HTML's implicit closes (<p>, <li>) are not
    modelled, so depth is recovered from the matching end tag of whatever
    opened at depth 0 instead of from a running count."""

    def __init__(self, src):
        super().__init__(convert_charrefs=False)
        self.src, self.tops, self.open = src, [], None
        self.stack = []
        self.lines = [0] + [m.end() for m in re.finditer("\n", src)]

    def _off(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self._off()
        if not self.stack:
            end = start + len(self.get_starttag_text()) if tag in VOID else None
            self.tops.append({"tag": tag, "attrs": dict(attrs), "start": start, "end": end})
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if not self.stack:
            start = self._off()
            self.tops.append({"tag": tag, "attrs": dict(attrs), "start": start,
                              "end": start + len(self.get_starttag_text())})

    def handle_endtag(self, tag):
        if tag in VOID or tag not in self.stack:
            return
        # Pop to the matching tag: an unclosed <p> inside the root is closed
        # implicitly by the root's own end tag, as a browser would.
        while self.stack and self.stack[-1] != tag:
            self.stack.pop()
        self.stack.pop()
        if not self.stack and self.tops and self.tops[-1]["end"] is None:
            self.tops[-1]["end"] = self.src.index(">", self._off()) + 1


def top_level(body):
    p = _Top(body)
    p.feed(body)
    p.close()
    for t in p.tops:
        if t["end"] is None:
            t["end"] = len(body)
    return p.tops


def find_root(body):
    roots = [t for t in top_level(body) if t["tag"] not in NOT_ROOT]
    return roots[0] if len(roots) == 1 else None


def root_selector(html):
    _, body = split_document(html)
    root = find_root(body)
    if not root:
        return ""
    if root["attrs"].get("id"):
        return "#" + root["attrs"]["id"]
    cls = (root["attrs"].get("class") or "").split()
    return "." + cls[0] if cls else ""


def split_selectors(group):
    out, depth, cur = [], 0, ""
    for ch in group:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _strip_comments(css):
    """Drop /* … */ in one linear pass. A regex with a lazy .*? rescans to
    the end of the input once per unclosed "/*", which is quadratic."""
    out, i = [], 0
    while True:
        j = css.find("/*", i)
        if j < 0:
            out.append(css[i:])
            return "".join(out)
        out.append(css[i:j])
        k = css.find("*/", j + 2)
        if k < 0:
            return "".join(out)
        i = k + 2


def _block_end(css, i):
    """Index just past the "}" that closes the "{" at css[i], or len(css)
    when it is never closed (a browser closes it at the end of the sheet)."""
    depth, n = 0, len(css)
    while i < n:
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


# At-rules whose block holds ordinary style rules; their selectors are checked.
_GROUPING = {"media", "supports", "container", "layer", "document"}
# At-rules whose block holds no selectors at all.
_NO_SELECTORS = {"font-face", "keyframes", "-webkit-keyframes", "-moz-keyframes"}


def _walk_rules(css, out):
    i, start, n = 0, 0, len(css)
    while i < n:
        c = css[i]
        if c in "};":
            start = i + 1
        elif c == "{":
            prelude = css[start:i].strip()
            end = _block_end(css, i)
            body = css[i + 1:end - 1] if end <= n and css[end - 1:end] == "}" else css[i + 1:]
            if prelude.startswith("@"):
                name = re.match(r"@([\w-]*)", prelude).group(1).lower()
                if name in _GROUPING or (name not in _NO_SELECTORS and "{" in body):
                    _walk_rules(body, out)
                elif name not in _NO_SELECTORS:
                    out.append(prelude)
            elif prelude:
                out.append(prelude)
            i = start = end
            continue
        i += 1


def css_rules(css):
    """Selector groups of every style rule, @media flattened. A linear scan,
    not nested regexes: an unclosed @keyframes of 40KB took 12 s that way."""
    out = []
    _walk_rules(_strip_comments(css), out)
    return out


HARNESS = "data-omelette-injected"
RESET_MARK = 'data-wpsafe="theme-reset"'
# URL bodies are bounded: an unbounded run re-scanned from every "http" in a
# 160KB string of them took 18 s. Candidates are found in one linear pass and
# filtered afterwards instead of asking one regex to do both.
URL_TOKEN = re.compile(r"https?://[^\s\"')]{0,2048}", re.I)
BANNED_HOST = re.compile(r"claudeusercontent\.com|claude\.ai", re.I)


def banned_urls(s):
    return [u for u in URL_TOKEN.findall(s) if BANNED_HOST.search(u)]
STYLE = re.compile(r"<style\b([^>]*)>(.*?)</style>", re.S | re.I)
SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def _finding(level, rule, where, detail):
    return {"level": level, "rule": rule, "where": where, "detail": detail}


def _strip_harness(s):
    if HARNESS not in s:
        return s
    s = re.sub(r"<(style|script)\b[^>]*" + HARNESS + r"[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    return re.sub(r"<[^<>]*" + HARNESS + r"[^<>]*>", "", s)


def _scoped(sel, root):
    for p in (root, f"html:has({root})", f"body:has({root})"):
        if sel == p or (sel.startswith(p) and not re.match(r"[\w-]", sel[len(p):len(p) + 1])):
            return True
    return False


def check(html):
    out = []
    if HARNESS in html:
        out.append(_finding("error", "preview-harness", "document",
                            f"{html.count(HARNESS)} element(s) carry {HARNESS} — "
                            "the Claude Design preview frame, not the page"))
    # Rules 3 (url) and 4 (base) carry no scope in spec 4.1 — they must see the
    # whole document, harness excluded (it is already reported above). Rules 2
    # (css-scope) and 5 (script-outside) are scoped to the body outside the
    # root, so they still need `inside`/`outside`, computed below only when a
    # valid root exists.
    doc = _strip_harness(html)
    head, body = split_document(doc)
    if re.search(r"<(style|script)\b", head, re.I):
        out.append(_finding("warn", "head-dropped", "<head>",
                            "<style>/<script> in <head> only serve a standalone "
                            "file and are dropped by clean()"))
    root = find_root(body)
    sel, inside = "", ""
    if not root:
        n = len([t for t in top_level(body) if t["tag"] not in NOT_ROOT])
        out.append(_finding("error", "root", "<body>",
                            f"{n} top-level elements — the page must be one element"))
    else:
        sel = root_selector(body)
        if not sel:
            out.append(_finding("error", "root", f"<{root['tag']}>",
                                "the root element needs an id or a class"))
        else:
            inside = body[root["start"]:root["end"]]
            outside = body[:root["start"]] + body[root["end"]:]
            if STYLE.search(outside):
                out.append(_finding("error", "css-scope", "outside the root",
                                    "a <style> sits outside the root element"))
            if SCRIPT.search(outside):
                out.append(_finding("error", "script-outside", "outside the root",
                                    "a <script> sits outside the root element"))
            bare, root_font = [], False
            for attrs, css in STYLE.findall(inside):
                if RESET_MARK in attrs:
                    continue
                for group in css_rules(css):
                    for s in split_selectors(group):
                        if not _scoped(s, sel):
                            bare.append(s)
                if re.search(re.escape(sel) + r"\s*\{[^{}]*font-family\s*:", css):
                    root_font = True
            if bare:
                out.append(_finding("error", "css-scope", sel,
                                    f"{len(bare)} selector(s) reach outside {sel}: "
                                    + ", ".join(bare[:5])))
            if not root_font and not re.search(r'font-family', root["attrs"].get("style", "")):
                out.append(_finding("warn", "root-font", sel,
                                    f"{sel} sets no font-family, so the theme's Raleway "
                                    "!important wins for the whole page"))
    # clean() moves Google Fonts links into the root and drops every other
    # element outside it. A stylesheet dropped silently is a page that looked
    # right in Claude Design and loses its styles on WordPress.
    around = head + (body[:root["start"]] + body[root["end"]:] if root else "")
    dropped = []
    for tag in LINK_TAG.findall(around):
        if re.search(r"""\srel\s*=\s*["']?[^"'>]*\bstylesheet\b""", tag, re.I) \
                and not FONT_LINK.fullmatch(tag):
            href = re.search(r"""\shref\s*=\s*["']?([^"'\s>]+)""", tag, re.I)
            dropped.append(href.group(1) if href else tag[:60])
    if dropped:
        out.append(_finding("warn", "stylesheet-dropped", "outside the root",
                            f"{len(dropped)} stylesheet link(s) outside the root are "
                            "dropped by clean(): " + ", ".join(dropped[:5])))
    urls = sorted(set(banned_urls(doc)))
    if urls:
        where = sel if sel and all(u in inside for u in urls) else "document"
        out.append(_finding("error", "url", where,
                            f"{len(urls)} Claude Design URL(s), which expire: "
                            + ", ".join(u[:60] for u in urls[:3])))
    if re.search(r"<base\b", doc, re.I):
        where = sel if sel and re.search(r"<base\b", inside, re.I) else "document"
        out.append(_finding("error", "base", where, "<base> rewrites every relative URL on the page"))
    return out


# Each rule answers one rule in the theme, named above it. Placed FIRST inside
# the root: the theme's selectors carry no id, so #ROOT beats them from any
# position, while the page's own #ROOT-prefixed rules come later and win ties.
THEME_RESET_CSS = (
    "/* theme: body,p,h1-h6,span,div,a,li,td,th{font-family:Raleway…!important} */"
    "#ROOT p,#ROOT h1,#ROOT h2,#ROOT h3,#ROOT h4,#ROOT h5,#ROOT h6,#ROOT span,"
    "#ROOT div,#ROOT a,#ROOT li,#ROOT td,#ROOT th,#ROOT label,#ROOT input,"
    "#ROOT select,#ROOT textarea,#ROOT button{font-family:inherit!important}"
    "/* theme: form label{padding:20px 0 10px}; label{line-height:1} */"
    "#ROOT label{padding:0;line-height:inherit}"
    "/* theme: h1-h6 carry padding, margin and min-height */"
    "#ROOT h1,#ROOT h2,#ROOT h3{padding:0;margin-top:0;margin-bottom:0;min-height:0}"
    "/* theme: .site-main is capped at 500/600/800/1140px off Elementor */"
    "#ROOT{position:relative;width:100vw!important;max-width:100vw!important;"
    "left:50%;right:50%;margin-left:-50vw!important;margin-right:-50vw!important}"
    "body:has(#ROOT){overflow-x:hidden}"
)
# [^<>] rather than [^>]: an unclosed tag then stops at the next "<" instead
# of re-scanning to the end of the page, which made 20000 unclosed tags cost
# seconds.
FONT_LINK = re.compile(r"<link\b[^<>]*fonts\.googleapis\.com[^<>]*>", re.I)
LINK_TAG = re.compile(r"<link\b[^<>]*>", re.I)
# Inline style="…" attribute values are handed to us already stripped of their
# surrounding quotes, so a bare double quote can never be part of the value —
# it has to stop the match, the same as ; and }.
FONT_DECL_INLINE = re.compile(r"(font-family\s*:\s*)([^;}\"]+?)(\s*)(?=[;}\"]|$)", re.I)
# <style> block content is the opposite: a quoted family name
# (font-family:"Times New Roman",serif) is legal CSS, so " must NOT stop the
# match there — only ; or } (or end of the block) do.
FONT_DECL_BLOCK = re.compile(r"(font-family\s*:\s*)([^;}]+?)(\s*)(?=[;}]|$)", re.I)
# Either a whole <script>…</script> (left alone — its group name flags "skip
# this match") or a style="…" attribute value (upgraded).
_SCRIPT_OR_STYLE_ATTR = re.compile(
    r"(?P<script><script\b[^>]*>.*?</script>)|style=\"(?P<val>[^\"]*)\"", re.S | re.I)


def _important(m):
    v = m.group(2)
    return m.group(0) if "!important" in v.replace(" ", "") else m.group(1) + v + "!important" + m.group(3)


# Blocks whose font-family is a descriptor, not a declaration: Chrome drops
# `font-family:"Foo"!important` inside @font-face and the font never registers.
_KEEP_AT = re.compile(r"@(?:font-face|(?:-webkit-|-moz-)?keyframes)\b", re.I)


def _upgrade_css(css):
    out, i = [], 0
    for m in _KEEP_AT.finditer(css):
        if m.start() < i:
            continue                        # inside a block already kept
        out.append(FONT_DECL_BLOCK.sub(_important, css[i:m.start()]))
        brace = css.find("{", m.end())
        end = len(css) if brace < 0 else _block_end(css, brace)
        out.append(css[m.start():end])
        i = end
    out.append(FONT_DECL_BLOCK.sub(_important, css[i:]))
    return "".join(out)


def _upgrade_fonts(inside):
    def style_block(m):
        if RESET_MARK in m.group(1):
            return m.group(0)
        return f"<style{m.group(1)}>" + _upgrade_css(m.group(2)) + "</style>"
    inside = STYLE.sub(style_block, inside)

    def inline(m):
        # Never touch <script> bodies: a JS string that happens to contain
        # style="font-family:…" is data, not a declaration to upgrade.
        if m.group("script") is not None:
            return m.group(0)
        return 'style="' + FONT_DECL_INLINE.sub(_important, m.group("val")) + '"'
    return _SCRIPT_OR_STYLE_ATTR.sub(inline, inside)


def _site_rules(inside):
    def img(m):
        tag = m.group(0)
        alt = re.search(r'\salt="([^"]+)"', tag)
        if alt and not re.search(r'\stitle=', tag):
            tag = tag[:4] + f' title="{alt.group(1)}"' + tag[4:]
        return tag
    inside = re.sub(r"<img\b[^<>]*>", img, inside, flags=re.I)

    def link(m):
        tag = m.group(0)
        # A real target attribute, not data-target= or aria-target=.
        if re.search(r"\starget\s*=", tag, re.I) or \
                not re.search(r'\shref="https?://', tag, re.I):
            return tag
        return tag[:2] + ' target="_blank" rel="noopener"' + tag[2:]
    # Match the tag first and test href in Python: `[^>]*\shref=…[^>]*` let
    # 20000 unclosed "<a " backtrack for 6.6 s.
    return re.sub(r"<a\b[^<>]*>", link, inside, flags=re.I)


def clean(html):
    errors = [f for f in check(html) if f["level"] == "error"]
    if errors:
        raise ValueError("not WP-safe: " + "; ".join(f"{f['rule']}: {f['detail']}" for f in errors))
    head, body = split_document(_strip_harness(html))
    root = find_root(body)
    sel = root_selector(body)
    inside = body[root["start"]:root["end"]]
    open_end = inside.index(">") + 1
    open_tag, content = inside[:open_end], inside[open_end:]

    content = re.sub(r"<style[^>]*" + RESET_MARK + r"[^>]*>.*?</style>", "", content, flags=re.S)
    content = content.replace('<span id="herotop"></span>', "")
    have = {re.search(r'href="([^"]+)"', l).group(1) for l in FONT_LINK.findall(content)}
    outside = head + body[:root["start"]] + body[root["end"]:]
    links = []
    for l in FONT_LINK.findall(outside):
        href = re.search(r'href="([^"]+)"', l).group(1)
        if href not in have:
            have.add(href)
            links.append(l)
    herotop = "" if 'id="herotop"' in content else '<span id="herotop"></span>'
    reset = f"<style {RESET_MARK}>" + THEME_RESET_CSS.replace("#ROOT", sel) + "</style>"

    # Lead with what WordPress needs, then the page exactly as it was.
    lead = re.match(r"(\s*(?:<link\b[^<>]*fonts\.googleapis\.com[^<>]*>\s*)*)", content).group(1)
    content = lead + "".join(links) + herotop + reset + content[len(lead):]
    inside = _site_rules(_upgrade_fonts(open_tag + content))
    return "<!-- wp:html -->\n" + inside.strip() + "\n<!-- /wp:html -->\n"


_FIX = {
    "root": ("整頁只可以有一個最外層元素,並且要有 id,例如 <div id=\"page\">…</div>。",
             "Wrap the whole page in exactly one element with an id, e.g. <div id=\"page\">…</div>."),
    "css-scope": ("每條 CSS 規則都要以 root 開頭(例如 #page .card),唔可以有 html、body、:root 規則,<style> 要放喺 root 入面。",
                  "Prefix every CSS rule with the root (e.g. #page .card); no html/body/:root rules; keep <style> inside the root."),
    "url": ("唔好用 claudeusercontent.com 或 claude.ai 嘅圖片網址,佢哋會過期;改用 data URI 或相對路徑。",
            "Do not reference claudeusercontent.com or claude.ai URLs — they expire. Inline images as data URIs or use relative paths."),
    "preview-harness": ("匯出嘅係預覽畫面,唔係頁面:請用 export 功能重新匯出靜態 HTML,唔好複製預覽嘅 DOM。",
                        "This is the preview frame's DOM, not the page. Re-export the static HTML instead of copying the preview."),
    "base": ("刪除 <base>,佢會改寫成個網站嘅相對連結。", "Remove <base>; it rewrites every relative URL on the site."),
    "script-outside": ("<script> 要放喺 root 入面。", "Move every <script> inside the root element."),
    "head-dropped": ("<head> 入面嘅 <style>/<script> 推上 WP 時會被刪除;需要嘅規則請搬入 root 入面。",
                     "<style>/<script> in <head> are dropped on WordPress; move anything the page needs inside the root."),
    "root-font": ("喺 root 規則設定 font-family(例如 #page{font-family:'Noto Sans TC',sans-serif}),否則網站會用 Raleway。",
                  "Set font-family on the root rule (e.g. #page{font-family:'Noto Sans TC',sans-serif}) or the site's Raleway wins."),
    "stylesheet-dropped": ("root 外面嘅 <link rel=\"stylesheet\">(Google Fonts 除外)推上 WP 時會被刪除;請將需要嘅 CSS 用 <style> 放入 root 入面。",
                           "<link rel=\"stylesheet\"> outside the root (other than Google Fonts) is dropped on WordPress; inline the CSS the page needs in a <style> inside the root."),
}
# A rule without its own entry still gets a usable line rather than a KeyError.
_FIX_GENERIC = ("請按問題說明修改頁面。", "Fix the page as the detail describes.")


def fix_prompt(findings):
    if not findings:
        return ""
    zh, en = [], []
    for f in findings:
        z, e = _FIX.get(f["rule"], _FIX_GENERIC)
        zh.append(f"- [{f['rule']}] {z}({f['detail']})")
        en.append(f"- [{f['rule']}] {e} ({f['detail']})")
    return ("請按以下要求修改頁面,然後重新匯出:\n" + "\n".join(zh)
            + "\n\nPlease fix the page and export again:\n" + "\n".join(en))


def visible_texts(html):
    _, body = split_document(_strip_harness(html))
    body = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    out = []
    for chunk in re.split(r"<[^>]+>", body):
        s = " ".join(unescape(chunk).split())
        if s:
            out.append(s)
    return out


def _root_p(html):
    _, body = split_document(html)
    root = find_root(body)
    scope = body[root["start"]:root["end"]] if root else body
    return len(re.findall(r"<p[\s>]", scope, re.I))


def autop_delta(pushed, rendered):
    """How many <p> WordPress added. Counts the whole rendered body: wpautop
    wraps stray markup outside the root too, and that is exactly the 67px it
    cost post 78436."""
    return len(re.findall(r"<p[\s>]", split_document(rendered)[1], re.I)) - _root_p(pushed)
