#!/usr/bin/env python3
"""figma_to_wp — a Figma frame becomes a WordPress page.

Three things come out of Figma and nothing else:

    design.png    what the page has to look like
    design.json   the copy, verbatim, plus the colour and type tokens
    assets/       the images and icons

You look at the picture and write the HTML. The script never guesses layout.
An earlier version tried — it segmented the frame into sections, grouped
siblings into rows, and emitted a nested spec tree. It produced a confident and
completely wrong page, because outside auto-layout subtrees the only honest
source of layout is the render. That code is gone. Position is something you
read off the picture, not a number a heuristic invents.

    setup                       check and store the Figma + WordPress credentials
    doctor                      check them, change nothing
    extract  <url|export-dir>   Figma -> build/<slug>/, and what changed
    verify   <slug>             page.html against design.json
    preview  <slug>             serve build/<slug>/ for a browser to screenshot
    diff     <slug>             screenshot vs design.png, band scores + overlay
    audit    <slug>             per-element deltas, and per-section drift
    mobile   <slug>             overflow / text size / tap targets / gutter
    push     <slug>             media upload -> draft page -> permalink

The browser-side measurement is `measure.js`, next to this file. It used to be
a 183-line string in here, where nothing checked its syntax; `doctor` runs
`node --check` on it now.

`extract` takes either a Figma frame URL or a folder of Figma UI exports. Prefer
the folder: a UI export costs nothing against the API quota (which is small on a
viewer seat and locks out plain reads for a long while once spent), and a PDF
export carries the copy as real, selectable text. The URL path is still the only
way to get the shared style tokens.

Stdlib only, except: PDF import wants poppler (`pdftotext`, `pdftoppm`, or
`pdfinfo`) or `pypdf`, and WebP conversion wants Pillow or `cwebp`. Each
degrades with a message rather than failing.
"""

import argparse
import base64
import glob
import collections
import hashlib
from html import unescape
import shutil
import subprocess
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from urllib.parse import unquote
import urllib.request

HOME = os.path.expanduser(os.environ.get("FIGMA_WP_HOME", "~/.figma-wp"))
BUILD = os.path.join(os.getcwd(), "build")
# some hosts refuse a bare urllib request
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) figma-to-wp"
FIGMA = "https://api.figma.com"

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".gif": "image/gif",
        ".mp4": "video/mp4", ".webm": "video/webm"}


def die(code, msg):
    print(f"{code}: {msg}", file=sys.stderr)
    sys.exit(1)


def env(key, default=None):
    v = os.environ.get(key)
    if v:
        return v.strip()
    path = os.path.join(HOME, ".env")
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                k, _, val = line.strip().partition("=")
                if k == key:
                    return val.strip()
    return default


# --------------------------------------------------------------------- figma

def figma_get(path, tok, tries=5):
    """Figma's file-read quota is cost-based and shared with image renders, so a
    burst of renders locks out plain reads too. Back off, and cache."""
    req = urllib.request.Request(FIGMA + path, headers={"X-Figma-Token": tok})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = min(120, 10 * 2 ** attempt)
                print(f"  HTTP {e.code} — retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            die(f"FIGMA_HTTP_{e.code}", f"{path} -> {body}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(10 * 2 ** attempt)
                continue
            die("NETWORK_ERROR", f"{path} -> {e.reason}")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "figma-to-wp"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as fh:
        fh.write(r.read())


# Only design files carry a readable document. The REST API answers every
# other Figma product with 400 "File type not supported by this endpoint",
# so name the product instead of reporting a malformed URL.
OTHER_KINDS = {"site": "Figma Sites", "board": "FigJam", "slides": "Figma Slides",
               "make": "Figma Make", "deck": "Figma Slides", "proto": "a prototype"}


def parse_url(url):
    m = re.search(r"/(?:file|design)/([A-Za-z0-9]+)", url)
    if not m:
        kind = re.search(r"figma\.com/([a-z]+)/", url)
        product = OTHER_KINDS.get(kind.group(1)) if kind else None
        if product:
            die("NOT_A_DESIGN_FILE",
                f"that is a {product} file. The REST API refuses it — no text, "
                f"no geometry, no styles. Only /v1/images (a render) and the "
                f"file's image map work. Open the page in a design file, or "
                f"give me the published site URL to read instead.")
        die("BAD_FIGMA_URL", "no file key in that URL")
    node = urllib.parse.parse_qs(
        urllib.parse.urlparse(url).query).get("node-id", [None])[0]
    if node:
        node = node.replace("-", ":")
    return m.group(1), node


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "page"


def rgba(c, opacity=1.0):
    if not c:
        return None
    r, g, b = (int(round(c.get(k, 0) * 255)) for k in "rgb")
    a = c.get("a", 1) * opacity
    return (f"#{r:02X}{g:02X}{b:02X}" if a >= 0.999
            else f"rgba({r},{g},{b},{round(a, 3)})")


def fills(n):
    return [f for f in (n.get("fills") or [])
            if f.get("visible", True) and f.get("opacity", 1) > 0]


def bbox(n):
    b = n.get("absoluteBoundingBox") or {}
    return b.get("x", 0), b.get("y", 0), b.get("width", 0), b.get("height", 0)


def walk(node, fn):
    """Every node, hidden branches included. Use `walk_visible` for content."""
    fn(node)
    for c in node.get("children") or []:
        walk(c, fn)


def walk_visible(node, fn):
    """Only what Figma actually renders.

    Hiding a group in Figma clears the flag on the group, not on its children,
    so a child of a hidden group still reports `visible: true`. Checking each
    node in isolation therefore pulls abandoned drafts into design.json, and
    they get built as if they were the design — which is how three feature
    cards that render nowhere in the file ended up on the live page.
    """
    if not node.get("visible", True):
        return
    fn(node)
    for c in node.get("children") or []:
        walk_visible(c, fn)


def file_version(key, tok):
    """The file's newest version id, or None if Figma will not say."""
    try:
        v = figma_get(f"/v1/files/{key}/versions", tok).get("versions") or []
        return v[0].get("id") if v else None
    except SystemExit:
        return None


def load_frame(url, tok):
    key, node = parse_url(url)
    if not node:
        die("NO_NODE_ID", "that URL has no node-id. Open the page or frame in "
                          "Figma and copy its link.")
    cache_dir = os.path.join(HOME, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"{key}-{node.replace(':', '_')}.json")

    # The cache used to be keyed on nothing but existence, while the render is
    # always fetched live — so the numbers could describe one version of the
    # file and design.png show another, and every check downstream would agree
    # with itself and be wrong. Observed: a cache three hours old still holding
    # 70 strings and 188 boxes belonging to a different page that had since
    # been deleted from the same canvas.
    current = file_version(key, tok)
    data = None
    if os.path.exists(cache):
        with open(cache) as fh:
            data = json.load(fh)
        cached = data.get("_fw_file_version")
        if current is None:
            print(f"cache     reusing the node response — could not reach "
                  f"/versions, so this may be out of date", file=sys.stderr)
        elif cached != current:
            print(f"cache     stale ({cached or 'unversioned'} -> {current}), "
                  f"refetching", file=sys.stderr)
            data = None
        else:
            print("cache     reusing the node response (file unchanged)",
                  file=sys.stderr)
    if data is None:
        data = figma_get(f"/v1/files/{key}/nodes?ids={urllib.parse.quote(node)}", tok)
        data["_fw_file_version"] = current
        with open(cache, "w") as fh:
            json.dump(data, fh)

    entry = (data.get("nodes") or {}).get(node)
    if not entry:
        die("NODE_NOT_FOUND", f"{node} is not in file {key}")
    return key, node, entry["document"], entry.get("styles") or {}


# ----------------------------------------------------------------- extraction

def collect_tokens(root, styles):
    """Resolve each shared style to a value via the first node that uses it.

    Read every value off the *node*, never off a style definition elsewhere:
    the two can disagree. Figma's MCP reports the `button` text style with
    letterSpacing 2.5 while the node that uses it actually renders at 0.4, and
    what renders is what the node carries.
    """
    color, text, effect = {}, {}, {}

    def visit(n):
        s = n.get("styles") or {}
        fid = s.get("fill") or s.get("fills")
        if fid in styles and styles[fid].get("styleType") == "FILL":
            name = styles[fid]["name"]
            f = (fills(n) or [None])[0]
            if name not in color and f and f.get("type") == "SOLID":
                color[name] = rgba(f.get("color"), f.get("opacity", 1))
        tid = s.get("text")
        if tid in styles and styles[tid]["name"] not in text:
            st = n.get("style") or {}
            if st.get("fontSize"):
                text[styles[tid]["name"]] = {
                    "family": st.get("fontFamily"),
                    "size": st["fontSize"],
                    "weight": st.get("fontWeight"),
                    "lh": round(st["lineHeightPx"] / st["fontSize"], 3)
                          if st.get("lineHeightPx") else None,
                    "ls": st.get("letterSpacing") or 0,
                }
        eid = s.get("effect")
        if eid in styles and styles[eid]["name"] not in effect:
            parts = []
            for e in n.get("effects") or []:
                if not e.get("visible", True):
                    continue
                t = e.get("type")
                if t in ("DROP_SHADOW", "INNER_SHADOW"):
                    o = e.get("offset") or {}
                    parts.append(
                        ("inset " if t == "INNER_SHADOW" else "")
                        + f"{round(o.get('x', 0))}px {round(o.get('y', 0))}px "
                        + f"{round(e.get('radius', 0))}px "
                        + (f"{round(e['spread'])}px " if e.get("spread") else "")
                        + str(rgba(e.get("color"))))
                else:
                    # GLASS, LAYER_BLUR, NOISE… no box-shadow equivalent. Record
                    # them rather than drop them silently, so whoever writes the
                    # CSS knows the design asked for something more.
                    parts.append("/* " + str(t)
                                 + (f" radius {e['radius']}" if e.get("radius") else "")
                                 + " */")
            if parts:
                effect[styles[eid]["name"]] = " ".join(parts)

    walk_visible(root, visit)
    return {"color": color, "text": text, "effect": effect}


def text_runs(n):
    """The distinct type styles inside one text node, in order."""
    over = n.get("characterStyleOverrides") or []
    if not over:
        return []
    table = n.get("styleOverrideTable") or {}
    base = n.get("style") or {}
    # A text node's own fill lives on the node, not in its style block, so a
    # run that inherits colour has to fall back to it or it reports None and
    # the run reads as colourless next to one that overrides.
    node_fill = [f for f in (n.get("fills") or [])
                 if f.get("type", "SOLID") == "SOLID" and f.get("visible", True)]

    def desc(key):
        st = table.get(str(key)) or base if key else base
        # Fill belongs here too. Without it a run that only changes colour —
        # an orange half of a heading, which is the house's whole accent
        # pattern — reports as no runs at all, and reads as an invention
        # somebody added rather than what the file asks for.
        paint = [f for f in (st.get("fills") or [])
                 if f.get("type", "SOLID") == "SOLID" and f.get("visible", True)] \
            or node_fill
        return {"size": st.get("fontSize", base.get("fontSize")),
                "weight": st.get("fontWeight", base.get("fontWeight")),
                "lh": st.get("lineHeightPx", base.get("lineHeightPx")),
                "case": st.get("textCase", base.get("textCase")),
                "fill": rgba(paint[0].get("color"), paint[0].get("opacity", 1))
                        if paint else None}

    runs = []
    for key in over:
        d = desc(key)
        if runs and runs[-1]["style"] == d:
            runs[-1]["chars"] += 1
        else:
            runs.append({"chars": 1, "style": d})
    return [{"chars": r["chars"], **r["style"]} for r in runs]


def collect_texts(root, styles, origin=None):
    """Every string in the design, top to bottom then left to right. The only
    place copy may come from — never retype it off the render.

    Coordinates are relative to the frame, the same origin `frames` uses, so
    the two can be read against each other."""
    found = []
    # A CANVAS has no box of its own, so its origin is 0,0 and subtracting it
    # leaves canvas coordinates. Measure against the layout frame instead —
    # the same origin `frames` uses.
    ox, oy, _, _ = bbox(origin or root)

    def visit(n):
        if n.get("type") != "TEXT" or not n.get("visible", True):
            return
        s = n.get("characters") or ""
        if not s.strip():
            return
        x, y, w, h = bbox(n)
        sid = n.get("styles") or {}
        st = n.get("style") or {}
        # Record the resolved values, not only the style's name. A name is
        # not checkable against anything: "heading-2" cannot be compared to a
        # stylesheet, and it cannot tell you the paragraph shipped at 14px
        # where the design says 20.
        # The node's own box, not one line of it. Section spans used to be
        # y + a single lineHeight, so a six-line paragraph counted as 24px tall
        # and the design side of every span came out far too short.
        rec = {"y": round(y - oy), "x": round(x - ox),
               "w": round(w), "h": round(h), "text": s,
               # Which edge of its box the copy is set against. Without it the
               # file cannot say that a title is centred in a card-wide node,
               # and a page that left-aligns the same string reads as correct:
               # box at x126 either way, ink 140px apart.
               "align": {"CENTER": "center", "RIGHT": "right",
                         "JUSTIFIED": "justify"}.get(
                             st.get("textAlignHorizontal"), "left"),
               "style": styles.get(sid.get("text"), {}).get("name"),
               "color": styles.get(sid.get("fill"), {}).get("name"),
               "type": {"family": st.get("fontFamily"), "size": st.get("fontSize"),
                        "weight": st.get("fontWeight"), "lh": st.get("lineHeightPx"),
                        "ls": st.get("letterSpacing")}}
        paint = [p for p in (fills(n) or []) if p.get("type") == "SOLID"]
        if paint:
            rec["type"]["fill"] = rgba(paint[0].get("color"), paint[0].get("opacity", 1))
        # Figma renders the case; the characters keep their original casing. Miss
        # this and every TITLE/UPPER heading ships in the wrong case while the
        # copy check still passes, because the letters are identical.
        case = st.get("textCase")
        if case and case != "ORIGINAL":
            rec["case"] = {"UPPER": "uppercase", "LOWER": "lowercase",
                           "TITLE": "capitalize"}.get(case, case.lower())
        # One Figma text node can carry several type styles: the hero's
        # heading and its paragraph are a single node, 48/72 bold over 20/32
        # medium. Reporting only the node's base style makes the paragraph
        # look like a heading that someone shrank, so record the runs.
        runs = text_runs(n)
        if len(runs) > 1:
            rec["runs"] = runs
        elif runs:
            # Every character can carry the same override, and then the node's
            # own style is not what renders: the Why-MC paragraph reports
            # lineHeightPx 28 while all 649 characters override it to 24.
            # One uniform run is still an override, not an absence of one.
            for k, v in runs[0].items():
                if k != "chars" and v is not None:
                    rec["type"][{"size": "size", "weight": "weight",
                                 "lh": "lh"}.get(k, k)] = v
            if runs[0].get("case") and runs[0]["case"] != "ORIGINAL":
                rec["case"] = {"UPPER": "uppercase", "LOWER": "lowercase",
                               "TITLE": "capitalize"}.get(runs[0]["case"],
                                                          runs[0]["case"].lower())
        found.append(rec)

    walk_visible(root, visit)
    found.sort(key=lambda t: (t["y"], t["x"]))
    for i, t in enumerate(found, 1):
        t["i"] = i
    return found


VECTORS = ("VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "REGULAR_POLYGON")


def seed_page_files(out):
    """Every build has the same three files, so there is never a question of
    where a piece of the page goes."""
    seeds = {
        "page.html": ('<!-- wp:html -->\n<div class="mc-page CHANGE-ME">\n'
                      "{{styles}}\n\n<!-- sections go here; the first one needs "
                      'id="herotop" -->\n\n</div>\n<!-- /wp:html -->\n'),
        "page.css": "/* Scope every rule under .CHANGE-ME */\n",
        "page.js": PAGE_JS_HEADER,
    }
    for name, body in seeds.items():
        p = os.path.join(out, name)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body)


def paint_of(f):
    """One fill, described the way CSS needs it."""
    t = f.get("type", "")
    if t == "SOLID":
        return rgba(f.get("color"), f.get("opacity", 1))
    if t.startswith("GRADIENT"):
        return {"gradient": t.lower(),
                "stops": [{"at": round(g.get("position", 0), 3),
                           "color": rgba(g.get("color"))}
                          for g in (f.get("gradientStops") or [])],
                "handles": [[round(p.get("x", 0), 3), round(p.get("y", 0), 3)]
                            for p in (f.get("gradientHandlePositions") or [])]}
    if t == "IMAGE":
        return {"image": f.get("imageRef"), "fit": f.get("scaleMode")}
    return {"fill": t.lower()}


def collect_frames(root, origin=None, min_side=24):
    """Every box worth measuring, with its position relative to the frame.

    Without this, design.json carries copy and colour but no numbers, so
    "follow the design" degrades to eyeballing a PNG — which is how a 392px
    card pitch gets built as 390 and a hero ends up 200px too tall.

    Icon interiors are noise, so a node's children are skipped once it is
    small enough to be one. This is a record of what Figma says, not an
    attempt to infer layout; the earlier version that inferred was deleted
    on purpose and should not come back.
    """
    # Measured against the layout frame, not the canvas, so a box's x/y can be
    # read straight into CSS. Pieces parked beside the frame keep coordinates
    # in the same system, which is what makes them recognisable as parked.
    ox, oy, _, _ = bbox(origin or root)
    found = []

    def visit(n, depth, parent_min=10 ** 6):
        if not n.get("visible", True):
            return
        x, y, w, h = bbox(n)
        if not n.get("absoluteBoundingBox"):
            # A CANVAS has no box of its own; measuring stops here but the
            # children below it are the whole point.
            for c in n.get("children") or []:
                visit(c, depth)
            return
        # A stroked or rotated node is a drawn thing — a chevron, a rule, a
        # ring — and its numbers are exactly the ones you cannot read off a
        # picture. The carousel chevron is an 18x30 path with a 3px stroke
        # rotated 90 degrees; min_side threw it away, nothing recorded the
        # stroke, and the page ended up built from pixels measured off the
        # render at 22x34 with a 5px stroke. Keep those regardless of size.
        drawn = bool(n.get("strokes")) or bool(n.get("rotation"))
        # A small filled leaf sitting in real layout is a marker, not noise: a
        # dot, a bullet, a rule. The vertical switcher's Ellipse 66-70 are 16
        # and 10px, min_side dropped all five, and the whole list had to be
        # measured out of the API by hand. Interiors of an icon really are
        # noise, so require a parent much larger than an icon frame.
        marker = (not (n.get("children") or [])
                  and parent_min >= 64
                  and any(f.get("type") == "SOLID" for f in fills(n)))
        if (w < min_side or h < min_side) and not drawn and not marker:
            return                      # too small to lay anything out against
        rec = {"id": n["id"], "name": n.get("name"), "type": n.get("type"),
               "x": round(x - ox), "y": round(y - oy),
               "w": round(w), "h": round(h), "depth": depth}
        if n.get("layoutMode") and n["layoutMode"] != "NONE":
            rec["layout"] = n["layoutMode"].lower()
            pad = [round(n.get(k, 0)) for k in
                   ("paddingTop", "paddingRight",
                    "paddingBottom", "paddingLeft")]
            # Check the metadata against where the children actually are. In
            # this file a card records [30,28,50,28] while every one of its own
            # text nodes sits 41 in, and a badge records 10 while its label is
            # centred on 41 — the numbers are left over from an earlier version
            # of the component. A padding that disagrees with the frame's own
            # contents is worse than no padding: it fails every page that is
            # built correctly, and it did.
            kids = [bbox(c) for c in (n.get("children") or [])
                    if c.get("visible", True) and c.get("absoluteBoundingBox")]
            if kids:
                real = [round(min(k[1] for k in kids) - y),
                        round((x + w) - max(k[0] + k[2] for k in kids)),
                        round((y + h) - max(k[1] + k[3] for k in kids)),
                        round(min(k[0] for k in kids) - x)]
                if any(abs(a - b) > 2 for a, b in zip(pad, real)):
                    rec["padSaid"] = pad
                    pad = real
            rec["pad"] = pad
            if n.get("itemSpacing"):
                rec["gap"] = round(n["itemSpacing"])
            for k, short in (("primaryAxisAlignItems", "main"),
                             ("counterAxisAlignItems", "cross")):
                if n.get(k) and n[k] != "MIN":
                    rec[short] = n[k].lower()
        if n.get("cornerRadius"):
            rec["radius"] = round(n["cornerRadius"])
        elif n.get("rectangleCornerRadii"):
            rec["radius"] = [round(v) for v in n["rectangleCornerRadii"]]
        # Every fill, not just the first: masterconcept.ai's panels stack a
        # solid under a gradient, and reading fill[0] alone reports the panel
        # as flat navy when the design shows a gradient over it.
        paint = [paint_of(f) for f in fills(n)]
        if paint:
            rec["bg"] = paint[0] if len(paint) == 1 else paint
        # Stroke and rotation, from the file. Both are invisible in the node's
        # box and both change what has to be written in CSS.
        strokes = [st for st in (n.get("strokes") or [])
                   if st.get("visible", True) and st.get("type") == "SOLID"]
        if strokes:
            rec["stroke"] = {
                "color": rgba(strokes[0].get("color"),
                              strokes[0].get("opacity", 1)),
                "weight": round(n.get("strokeWeight", 1), 2),
                "align": (n.get("strokeAlign") or "").lower() or None}
            if n.get("strokeCap") and n["strokeCap"] != "NONE":
                rec["stroke"]["cap"] = n["strokeCap"].lower()
        # Shadows. The product cards carry no visible stroke at all — the edge
        # in the render is a drop shadow — and with effects unread the page was
        # built with a 1px #E8EFF8 border standing in for one.
        shad = [e for e in (n.get("effects") or [])
                if e.get("visible", True)
                and e.get("type") in ("DROP_SHADOW", "INNER_SHADOW")]
        if shad:
            rec["shadow"] = [{
                "inset": e["type"] == "INNER_SHADOW",
                "x": round(e.get("offset", {}).get("x", 0)),
                "y": round(e.get("offset", {}).get("y", 0)),
                "blur": round(e.get("radius", 0)),
                "spread": round(e.get("spread", 0)),
                "color": rgba(e.get("color"), e.get("color", {}).get("a", 1)),
            } for e in shad]
        if n.get("rotation"):
            # Figma reports radians, anticlockwise. Degrees clockwise is what
            # a CSS transform takes.
            rec["rotation"] = round(-math.degrees(n["rotation"]), 1)
        found.append(rec)
        for c in n.get("children") or []:
            visit(c, depth + 1, min(w, h))

    visit(root, 0)
    return found


def collect_comments(key, node_ids, tok):
    """Unresolved comments anchored inside this page.

    Designers leave instructions here that exist nowhere else in the file — on
    this project: "循環按鈕" (the switcher loops), "hover 字體變藍", "下面的按鈕
    皆連接到下面的詳細內容". None of it is inferable from the geometry, and
    missing it means shipping a page that looks right and behaves wrong.
    """
    try:
        data = figma_get(f"/v1/files/{key}/comments", tok, tries=2)
    except SystemExit:
        return []
    out = []
    for c in data.get("comments") or []:
        if c.get("resolved_at"):
            continue
        meta = c.get("client_meta") or {}
        anchor = meta.get("node_id") or (meta.get("stable_path") or [None])[0]
        if node_ids and anchor not in node_ids:
            continue
        out.append({"node": anchor,
                    "at": meta.get("node_offset"),
                    "by": (c.get("user") or {}).get("handle"),
                    "text": (c.get("message") or "").strip()})
    return out


def all_vector(n):
    """True when nothing under this node is anything but drawing."""
    if n.get("type") not in VECTORS and n.get("type") not in (
            "FRAME", "GROUP", "COMPONENT", "INSTANCE"):
        return False
    return all(all_vector(c) for c in n.get("children") or [])


def collect_assets(root):
    """Images come from the file's image map — original bytes, and it does not
    touch the render quota. Icons are rendered as SVG in one batched call.

    An icon is exported at its container, not at each path inside it. Taking
    the paths one by one gives a file per path, so a magnifying glass ships as
    a bare circle with the handle in a different file — which is what the
    three Workspace cards were showing.
    """
    images, icons = [], []

    def visit(n):
        if not n.get("visible", True):
            return
        _, _, w, h = bbox(n)
        if not n.get("absoluteBoundingBox"):    # a CANVAS has none; go deeper
            for c in n.get("children") or []:
                visit(c)
            return
        if w < 1 or h < 1:                      # dividers; they export empty
            return
        ref = next((f.get("imageRef") for f in fills(n) if f.get("type") == "IMAGE"), None)
        if ref:
            n_ = len(images) + 1
            images.append({"id": f"a{n_}", "node": n["id"], "ref": ref,
                           "name": n.get("name"), "w": round(w), "h": round(h),
                           "file": f"assets/a{n_}.png", "alt": ""})
            return
        if max(w, h) <= 64 and all_vector(n):
            n_ = len(icons) + 1
            icons.append({"id": f"i{n_}", "node": n["id"], "name": n.get("name"),
                          "w": round(w), "h": round(h), "file": f"assets/i{n_}.svg"})
            return                              # whole icon; do not split it
        for c in n.get("children") or []:
            visit(c)

    visit(root)
    return images, icons



# ------------------------------------------------- local Figma export (PDF…)
#
# A Figma UI export costs nothing against the API quota, and a PDF carries the
# copy as real text. That makes it the sturdier input: no 429, exact strings.
# The API path stays, because only it knows the shared style tokens.

RASTER = (".png", ".jpg", ".jpeg", ".webp")


def have(binary):
    return shutil.which(binary) is not None


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def pdf_size(path):
    """(width, height) in points; a Figma PDF exports 1pt per design px."""
    if have("pdfinfo"):
        out = run(["pdfinfo", path]).stdout
        m = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", out)
        if m:
            return float(m.group(1)), float(m.group(2))
    try:
        import pypdf
        box = pypdf.PdfReader(path).pages[0].mediabox
        return float(box.width), float(box.height)
    except Exception:                                        # noqa: BLE001
        return 0.0, 0.0


def pdf_text(path):
    """Lines of copy, in reading order."""
    if have("pdftotext"):
        out = run(["pdftotext", "-layout", "-nopgbrk", path, "-"]).stdout
    else:
        try:
            import pypdf
            out = "\n".join(pg.extract_text() or ""
                            for pg in pypdf.PdfReader(path).pages)
        except Exception:                                    # noqa: BLE001
            die("NO_PDF_TEXT",
                "install poppler (brew install poppler) or pypdf to read PDF exports")
    return [ln.strip() for ln in out.splitlines() if len(ln.strip()) > 1]


def pdf_extract_original(path, dest):
    """Pull the bitmap embedded in a one-image PDF, alpha and all.

    A PDF *render* flattens onto white, which is why an exported artwork comes
    back sitting in a white box. The embedded original keeps its transparency;
    poppler writes the PDF soft mask out as a separate greyscale file, so put
    it back as the alpha channel.
    """
    if not have("pdfimages"):
        return False
    import glob as _glob
    import tempfile
    try:
        from PIL import Image
    except ImportError:
        return False
    with tempfile.TemporaryDirectory() as td:
        if run(["pdfimages", "-all", path, td + "/x"]).returncode != 0:
            return False
        files = sorted(_glob.glob(td + "/x-*"))
        if not files:
            return False
        try:
            base = Image.open(files[0]).convert("RGB")
            out = base
            for f in files[1:]:
                m = Image.open(f)
                if m.mode in ("L", "1") and m.size == base.size:
                    out = base.copy()
                    out.putalpha(m.convert("L"))
                    break
            out.save(dest)
            return True
        except Exception:                                # noqa: BLE001
            return False


def pdf_render(path, dest, scale=1.0):
    """PDF -> PNG at `scale` x the design's own pixel size."""
    if have("pdftoppm"):
        base = dest[:-4] if dest.endswith(".png") else dest
        r = run(["pdftoppm", "-png", "-r", str(round(72 * scale)), "-singlefile",
                 path, base])
        return r.returncode == 0 and os.path.exists(base + ".png")
    if sys.platform == "darwin" and have("sips"):
        return run(["sips", "-s", "format", "png", path, "--out", dest]).returncode == 0
    return False


def extract_from_export(args):
    """Build design.png / design.json / assets from a folder of Figma exports."""
    src = os.path.abspath(args.source)
    files = sorted(f for f in glob.glob(os.path.join(src, "**", "*"), recursive=True)
                   if os.path.isfile(f))
    pdfs = [f for f in files if f.lower().endswith(".pdf")]
    loose = [f for f in files if f.lower().endswith(RASTER + (".svg",))]
    if not pdfs and not loose:
        die("EMPTY_EXPORT", f"no PDF/PNG/SVG files under {src}")

    sized = sorted(((pdf_size(f)[0] * pdf_size(f)[1], f) for f in pdfs), reverse=True)
    frame = None
    if args.frame:
        frame = next((f for f in pdfs
                      if args.frame.lower() in os.path.basename(f).lower()), None)
        if not frame:
            die("FRAME_NOT_FOUND", f"no PDF matching {args.frame!r} in {src}")
    elif sized:
        frame = sized[0][1]                        # the biggest page is the page

    slug = args.slug or slugify(os.path.splitext(os.path.basename(frame or src))[0])
    out = os.path.join(BUILD, slug)
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)
    seed_page_files(out)

    texts, assets = [], []
    if frame:
        w, h = pdf_size(frame)
        if pdf_render(frame, os.path.join(out, "design.png")):
            print(f"design    design.png  ({round(w)}x{round(h)})  <- "
                  f"{os.path.basename(frame)}")
        else:
            print("design    could not render the PDF — install poppler, or export "
                  "the frame as PNG and save it as design.png", file=sys.stderr)
        for ln in pdf_text(frame):
            texts.append({"src": os.path.basename(frame), "text": ln})

    # Every other PDF is a separate artboard: carousel slides, variants, images.
    # Its copy counts, and it becomes an asset.
    loose_names = {os.path.splitext(os.path.basename(f))[0] for f in loose}
    for f in pdfs:
        if f == frame:
            continue
        name = os.path.splitext(os.path.basename(f))[0]
        for ln in pdf_text(f):
            texts.append({"src": name, "text": ln})
        if name in loose_names:
            continue          # a PNG/SVG of the same artboard is already coming,
                              # and it keeps its transparency where a render would not
        aid = f"a{len(assets) + 1}"
        dest = os.path.join(out, "assets", f"{aid}.png")
        w, h = pdf_size(f)
        if pdf_extract_original(f, dest) or pdf_render(f, dest, scale=2.0):
            assets.append({"id": aid, "name": name, "w": round(w), "h": round(h),
                           "file": f"assets/{aid}.png", "alt": "", "from": name})

    for f in loose:
        ext = os.path.splitext(f)[1].lower()
        aid = ("i" if ext == ".svg" else "a") + str(len(assets) + 1)
        dest = os.path.join(out, "assets", aid + ext)
        shutil.copy2(f, dest)
        rec = {"id": aid, "name": os.path.splitext(os.path.basename(f))[0],
               "file": f"assets/{aid}{ext}", "from": os.path.relpath(f, src)}
        if ext != ".svg":
            rec["alt"] = ""
        assets.append(rec)

    seen, uniq = set(), []
    for t in texts:
        k = squash(t["text"])
        if k and k not in seen:
            seen.add(k)
            t["i"] = len(uniq) + 1
            uniq.append(t)

    dpath = os.path.join(out, "design.json")
    tokens = {"color": {}, "text": {}}
    if os.path.exists(dpath):                      # keep tokens from an API run
        with open(dpath) as fh:
            tokens = json.load(fh).get("tokens", tokens)
    with open(dpath, "w") as fh:
        json.dump({"source": {"export_dir": src, "frame": os.path.basename(frame or "")},
                   "tokens": tokens, "texts": uniq, "assets": assets},
                  fh, ensure_ascii=False, indent=1)
    print(f"slug      {slug}")
    print(f"texts     {len(uniq)} unique lines from {len(pdfs)} PDFs")
    print(f"assets    {len(assets)}")
    if not tokens["color"]:
        print("tokens    empty — a PDF carries no style names. Run extract on the "
              "Figma URL too if you want them, or read colours off design.png.")


CHROME = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
          "/Applications/Chromium.app/Contents/MacOS/Chromium",
          "/usr/bin/google-chrome", "/usr/bin/chromium")


def shoot(url, png, width, tall=14000):
    """Screenshot a page at the design's width.

    Headless Chrome captures the viewport, not the document, so the viewport
    is made taller than any page and the empty tail trimmed off afterwards.
    That also forces every lazy image into view, which a fold-height capture
    would leave unloaded and silently blank in the comparison.
    """
    exe = next((c for c in CHROME if os.path.exists(c)), None)
    if not exe:
        die("NO_BROWSER", "render diff needs Chrome or Chromium installed.")
    tmp = png + ".raw.png"
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--virtual-time-budget=5000",
                    f"--window-size={width},{tall}", f"--screenshot={tmp}", url],
                   check=True, capture_output=True, timeout=300)
    from PIL import Image
    im = Image.open(tmp).convert("RGB")

    # A viewport this tall pins every position:fixed element (back-to-top,
    # cookie bar) to y=20000, so trimming blank rows from the bottom trims
    # nothing. Cut at the first long blank run instead: the real page ends
    # there and the fixed furniture is stranded below it.
    strip = im.resize((8, im.height), Image.BOX)      # row summary, cheap to scan
    rows = list(strip.getdata())
    rows = [tuple(rows[i * 8:(i + 1) * 8]) for i in range(im.height)]
    tally = {}
    for r in rows[-200:]:
        tally[r] = tally.get(r, 0) + 1
    bg = max(tally, key=tally.get)
    run = 0
    bottom = im.height
    for y, r in enumerate(rows):
        run = run + 1 if r == bg else 0
        if run >= 600:
            bottom = y - run + 1
            break
    im.crop((0, 0, im.width, max(200, bottom + 40))).save(png)
    os.remove(tmp)
    return Image.open(png)


def cmd_diff(args):
    """Put the built page beside the Figma render and measure the gap.

    Every check in `verify` is a string check, so a page can pass all of them
    and still look nothing like the design — which is exactly what happened
    here. Appearance has one source of truth and it is the picture.
    """
    from PIL import Image, ImageChops, ImageDraw
    out = os.path.join(BUILD, args.slug)
    design = json.load(open(os.path.join(out, "design.json"), encoding="utf-8"))
    # A reference handed over by someone else is worth supporting: it is how
    # a designer says "this is what it should look like" without going near
    # the API, and it keeps that claim checkable instead of a conversation.
    ref_path = args.ref or os.path.join(out, "design.png")
    if not os.path.exists(ref_path):
        die("NO_RENDER", f"no reference at {ref_path} — run extract without "
                         f"--no-render, or pass --ref.")
    print(f"reference {ref_path}")
    ref = Image.open(ref_path).convert("RGB")
    w = args.width or design["source"]["width"]

    url = args.url
    if not url:
        die("NO_URL", "pass --url: the live page, or the local preview.")
    if w < 500:
        print(f"width     {w}px requested — macOS Chrome will not size a window "
              f"below ~500px, so the page lays out wider than this and the shot "
              f"is merely cropped. Every band will read as 'cut off on the "
              f"right' when nothing overflows. Use a device emulator instead.",
              file=sys.stderr)

    # Shoot first, ask questions never, is how you end up reporting "live 200px,
    # delta -96.9%" about a preview server that died half an hour ago.
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            head = r.read(4096).decode("utf-8", "replace")
            code = r.status
    except Exception as e:
        die("URL_UNREACHABLE", f"{url} did not answer ({e}). Nothing was "
                               f"screenshotted.")
    if code != 200 or "<" not in head:
        die("URL_NOT_A_PAGE", f"{url} answered {code} with no markup. Nothing "
                              f"was screenshotted.")

    dd = os.path.join(out, "diff")
    os.makedirs(dd, exist_ok=True)
    live = shoot(url, os.path.join(dd, "live.png"), w)

    ref = ref.resize((w, round(ref.height * w / ref.width)), Image.LANCZOS)

    # One section at a time, each cut to its own content. Comparing the whole
    # page at once means everything below the first size difference is offset
    # rather than wrong, and its score says so — loudly, and about nothing.
    if args.section:
        sp = os.path.join(out, "sections.json")
        if not os.path.exists(sp):
            die("NO_SECTION_MAP", "run `audit` first — it records where each "
                                  "section's content sits in the frame.")
        smap = json.load(open(sp, encoding="utf-8"))
        if args.section not in smap:
            die("NO_SUCH_SECTION", f"{args.section!r} is not mapped. Known: "
                                   + ", ".join(smap))
        # The page side of the crop is anchored with `drift`, which `audit`
        # measured against whatever the page looked like when it last ran. Edit
        # the CSS and that anchor is a lie: a section that had just been placed
        # exactly right read 87.6 instead of 91.1, and the number moved the way
        # a wrong fix moves it.
        newest = max((os.path.getmtime(os.path.join(out, f))
                      for f in ("page.css", "page.html")
                      if os.path.exists(os.path.join(out, f))), default=0)
        if os.path.getmtime(sp) < newest - 1:
            die("STALE_SECTION_MAP",
                "page.css/page.html changed after sections.json was written, so "
                "the page-side anchor is out of date. Re-run `audit` first.")
        v = smap[args.section]
        if "drift" not in v:
            die("STALE_SECTION_MAP", "sections.json predates the drift column — "
                                     "re-run `audit` before using --section.")
        # Both crops start at the SAME thing — the section's first string, which
        # is the one landmark that exists on both sides — and run the same
        # distance. They used to start at different things: the design at its
        # first string, the page at its section element's top, and then run for
        # different lengths (the design's text span against the page's box
        # height). On a page that had been built section by section to dx+0 and
        # shipped, that fed 602px of design to 1052px of page and scored the
        # section at 14%. Anything measured that way is noise.
        pad = 40
        span = max(v["design_span"], v["page_span"]) + 2 * pad
        d0 = v["design_y"] - pad
        p0 = v["design_y"] + v["drift"] - pad      # the page's own first string
        def window(im, y0):
            """The same-sized window, white where the image does not reach."""
            out_ = Image.new("RGB", (w, span), (255, 255, 255))
            src = im.crop((0, max(0, y0), w, min(im.height, y0 + span)))
            out_.paste(src, (0, max(0, -y0)))
            return out_
        ref, live = window(ref, d0), window(live, p0)
        print(f"section   {args.section}: {span}px from each side — design "
              f"y{d0}, page y{p0} (drift {v['drift']:+d})")
    if args.offset:
        # A Figma frame usually draws the site header; the page does not, because
        # WordPress supplies it. Left uncorrected, every band below is displaced
        # by that height and the whole sheet reads hot.
        ref = ref.crop((0, args.offset, w, ref.height))
        print(f"offset    reference cropped from y={args.offset}")
    print(f"width     {w}px")
    print(f"height    design {ref.height}   live {live.height}   "
          f"delta {live.height - ref.height:+d}px "
          f"({(live.height / ref.height - 1) * 100:+.1f}%)")

    # Compare at true page coordinates. Scaling one side to the other's height
    # makes every band after the first difference disagree by displacement
    # rather than by content, which buries the one band where the drift starts
    # under nineteen that merely inherited it.
    cmp_live = live
    tall = min(ref.height, live.height)
    bands = args.bands
    step = tall // bands
    # Split across the page as well as down it. A full-width band averages a
    # column-local fault into the calm around it: the hero media sat 66px high
    # for days behind a band score of 15, because the text column beside it
    # was correct and the two were reported as one number.
    cols = args.cols
    cw = w // cols
    print(f"\nband  y-range      " + "".join(f"  col{c+1:<7}" for c in range(cols))
          + f"  (compared over {tall}px, unscaled; each cell is {cw}px wide)")
    worst = []
    for i in range(bands):
        y0, y1 = i * step, (i + 1) * step if i < bands - 1 else tall
        cells = []
        for c in range(cols):
            x0, x1 = c * cw, (c + 1) * cw if c < cols - 1 else w
            a = ref.crop((x0, y0, x1, y1))
            bb = cmp_live.crop((x0, y0, x1, y1))
            d = ImageChops.difference(a, bb)
            mean = sum(sum(v * n for n, v in enumerate(
                d.histogram()[ch * 256:ch * 256 + 256])) for ch in range(3)) \
                / (d.width * d.height * 3)
            cells.append(mean)
            worst.append((mean, i, y0, y1, c + 1))
        print(f"{i:>4}  {y0:>5}-{y1:<8}" + "".join(f"  {m:>7.1f}  " for m in cells))

    worst.sort(reverse=True)
    print("\nlook here first:")
    for mean, i, y0, y1, c in worst[:6]:
        print(f"  band {i:<3} col {c}  y {y0}-{y1}  delta {mean:.1f}")

    # Two pictures a metre apart cannot show a 6px misalignment — you end up
    # confirming "roughly similar" and calling it checked. Superimposed, the
    # design goes in the red channel and the page in green+blue: anything that
    # lines up turns grey, anything that does not leaves a red or cyan ghost
    # whose width is the error.
    oh = max(ref.height, live.height)
    def grey(im):
        g = Image.new("L", (w, oh), 255)
        g.paste(im.convert("L"), (0, 0))
        return g
    gr, gl = grey(ref), grey(live)
    Image.merge("RGB", (gr, gl, gl)).save(os.path.join(dd, "overlay.png"))
    print(f"wrote     {dd}/overlay.png  "
          f"(design red, live cyan — grey where they agree)")

    # How much of the overlay is actually grey? One number for the whole page,
    # and one per band. Read it RELATIVE, never as an absolute: a page that was
    # built section by section to dx+0 dy±2 and shipped scores 84% over the
    # whole sheet and 93% at the median band — the rest is two different
    # rasterisers, a recompressed image and a frame taller than the page. 99%
    # would mean you are comparing something to itself. What carries signal is
    # a band far below this page's OWN median: 40 points down is not
    # anti-aliasing, it is a box the wrong width or a row that lost a card.
    accepted = {}
    apath = os.path.join(out, "accepted.json")
    if os.path.exists(apath):
        accepted = json.load(open(apath, encoding="utf-8"))
    ho = min(ref.height, live.height)
    rb, lb = gr.tobytes(), gl.tobytes()
    bh = step                       # the band height the table above used
    def agree(y0, y1, stride=5):
        a = b = 0
        for i in range(y0 * w, min(y1, ho) * w, stride):
            b += 1
            if abs(rb[i] - lb[i]) <= 16:
                a += 1
        return a / b if b else 1.0
    whole = agree(0, ho)
    rows_m = [(y, agree(y, y + bh)) for y in range(0, ho - bh, bh)]
    vals = sorted(v for _, v in rows_m)
    med = vals[len(vals) // 2] if vals else 0.0
    print(f"match     {whole:.1%} of the overlay agrees; band median {med:.1%}"
          f" over {len(rows_m)} bands")

    # The gate, for building a page one section at a time. Calibrated on a page
    # that was built section by section, reviewed band by band and shipped: its
    # six sections score 84.0 / 89.9 / 91.4 / 93.8 / 94.1 / 94.8, median 93.
    # So 90 is a pass, 85 is "there is still something", and below that there is
    # a fault unless the section can say why in accepted.json. The floor of 84
    # was a full-height gradient band, where two rasterisers dither differently.
    if args.section:
        why = accepted.get(args.section)
        v = smap.get(args.section, {})
        nb = v.get("box_bad", 0) + v.get("box_orphan", 0)
        boxnote = (f" but {nb} box finding(s) — run `audit` and read them; a "
                   f"container the wrong height barely moves this number"
                   if nb else "")
        if whole >= 0.93 and not nb:
            print(f"gate      {args.section} passes at {whole:.1%} — freeze it "
                  f"and start the next section down")
        elif whole >= 0.93 and why:
            print(f"gate      {args.section} {whole:.1%}{boxnote}, accepted: {why}")
        elif whole >= 0.93:
            print(f"gate      {args.section} {whole:.1%}{boxnote}")
        elif why:
            print(f"gate      {args.section} {whole:.1%}{boxnote}, accepted: {why}")
        else:
            print(f"gate      {args.section} {whole:.1%} — not yet. The "
                  f"reference build's sections run 83.6 to 94.8, median 93, so "
                  f"93 is the bar and 84 is the floor a full-height gradient "
                  f"band manages.{boxnote}")
            print(f"          Fix it here, before anything below it exists: a "
                  f"section's position depends on every section above it, and "
                  f"an error found now is one error, not one of six.")
    floor = med - 0.15
    low = [(y, v) for y, v in rows_m if v < floor]
    if low:
        print(f"          bands more than 15 points under this page's own median:")
        for y, v in sorted(low, key=lambda t: t[1]):
            why = next((r for k, r in accepted.items()
                        if k.isdigit() and y <= int(k) < y + bh), None)
            tail = f"   accepted: {why}" if why else ""
            print(f"            y{y}-{y + bh}  {v:.1%}  ({v - med:+.1%}){tail}")
        unexplained = [y for y, v in low
                       if not any(k.isdigit() and y <= int(k) < y + bh
                                  for k in accepted)]
        if unexplained:
            print(f"          {len(unexplained)} of them unexplained — name each "
                  f"one in accepted.json or fix it")
    else:
        print("          every band is within 15 points of the median")

    # Persist it. Two things need this: `mobile`, which has no business running
    # until the desktop has settled — every phone rule restates a desktop value,
    # so a desktop that is still moving invalidates them as fast as they are
    # written — and the loop, which has to be able to tell progress from a
    # stall rather than going round again on feel.
    prev_state = {}
    spath = os.path.join(dd, "state.json")
    if os.path.exists(spath):
        try:
            prev_state = json.load(open(spath, encoding="utf-8"))
        except Exception:
            prev_state = {}
    unexp = [y for y, v in low
             if not any(k.isdigit() and y <= int(k) < y + bh for k in accepted)]
    write_json(spath, {"match": round(whole, 4), "median": round(med, 4),
                       "bands": len(rows_m), "unexplained": unexp,
                       "height_design": ref.height, "height_page": live.height,
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if prev_state.get("match") is not None:
        moved = whole - prev_state["match"]
        was = len(prev_state.get("unexplained") or [])
        print(f"          since the last run: match {moved:+.1%}, "
              f"unexplained {was} -> {len(unexp)}")
        if abs(moved) < 0.002 and len(unexp) >= was:
            print("          nothing moved. Another round will not help — go "
                  "back and re-read the file for the band you are working on.")

    # How far out is each band? Slide the page against the design a few pixels
    # either way and report the shift that agrees best. A number beats deciding
    # from a picture whether two things "look aligned".
    import array
    ra = array.array("d", gr.resize((w // 4, oh // 4)).getdata())
    la = array.array("d", gl.resize((w // 4, oh // 4)).getdata())
    W4, H4 = w // 4, oh // 4
    def cost(band0, band1, shift):
        tot = n = 0
        for row in range(band0, band1):
            src = row + shift
            if src < 0 or src >= H4:
                continue
            base, off = row * W4, src * W4
            for col in range(0, W4, 3):
                tot += abs(ra[base + col] - la[off + col]); n += 1
        return tot / n if n else 1e9
    # Sideways, once, for the whole page. `audit` places the page from text
    # nodes in design.json; this places it from the picture. When the two
    # disagree the reference itself is out of register — a canvas render
    # cropped a few pixels off will move every glyph in design.png together
    # while every number in design.json stays right, and then every overlay
    # anyone reads is lying by that many pixels in the same direction. That
    # cost this project three wrong "fixes" before anyone measured the crop.
    def hcost(shift):
        tot = n = 0
        for row in range(0, oh, 8):
            base = row * w
            lo = max(0, -shift)
            hi = min(w, w - shift)
            for col in range(lo, hi, 2):
                tot += abs(rf[base + col] - lf[base + col + shift]); n += 1
        return tot / n if n else 1e9
    rf = array.array("d", gr.getdata())
    lf = array.array("d", gl.getdata())
    hbest = min(((hcost(sh), sh) for sh in range(-8, 9)), key=lambda t: t[0])[1]
    if hbest:
        print(f"\nREFERENCE_OFF_BY: the whole page fits design.png best {hbest:+d}px "
              f"across — not one band, all of it. Either the page really is "
              f"shifted (audit's origin line will say so too) or design.png is "
              f"cropped wrong and the numbers in design.json are the ones to "
              f"trust. Check audit first; do not move anything on this evidence "
              f"alone.")
    else:
        print("\nregister  design.png and the page agree sideways to the pixel")

    print("\nbest vertical fit per band (page vs frame):")
    step = H4 // args.bands or 1
    for i in range(args.bands):
        b0, b1 = i * step, min((i + 1) * step, H4)
        if b1 - b0 < 2:
            continue
        best = min(((cost(b0, b1, sh), sh) for sh in range(-6, 7)),
                   key=lambda t: t[0])
        if best[1]:
            print(f"  band {i:<3} y {b0*4}-{b1*4}   page sits {best[1]*4:+d}px")

    side = Image.new("RGB", (w * 2 + 24, max(ref.height, live.height)), "white")
    side.paste(ref, (0, 0))
    side.paste(live, (w + 24, 0))
    ImageDraw.Draw(side).line([(w + 12, 0), (w + 12, side.height)], "red", 3)
    scale = 1400 / side.width
    side.resize((1400, round(side.height * scale)), Image.LANCZOS)\
        .save(os.path.join(dd, "side-by-side.png"))
    print(f"\nwrote     {dd}/side-by-side.png  (design left, live right)")
    print("          open it. the numbers say where, the picture says what.")



def canvas_origin(canvas):
    """Top-left of a canvas render, in absolute coordinates.

    A CANVAS has no `absoluteBoundingBox`, so asking bbox() for one gives
    (0, 0) — and Figma renders a canvas from the bounding box of its children,
    which is rarely at the origin. One file's children started at x=0 and the
    crop was right by accident; the next started at x=-1525 and the crop landed
    on empty canvas, producing a design.png that was 98% transparent while
    extract still reported success.

    Figma renders from the children's *rendered* bounds, not their layout
    boxes, and the two differ wherever a child carries a shadow, a blur or an
    outside stroke. On the Asana file one parked card's shadow bled 3px past
    its own left edge, so the canvas render began at -1528 while this
    function said -1525 — and the crop drew the whole page 3px right of where
    the frame puts it. Nothing else notices: design.json is read from the node
    tree, so every number stayed right while the picture that every overlay
    is read against was wrong, and three separate "fixes" were made to chase
    it. Prefer absoluteRenderBounds; fall back only where Figma omits it.
    """
    boxes = [c.get("absoluteRenderBounds") or c.get("absoluteBoundingBox")
             for c in (canvas.get("children") or []) if c.get("visible", True)]
    boxes = [b for b in boxes if b and b.get("x") is not None]
    if not boxes:
        return bbox(canvas)[0], bbox(canvas)[1]
    return min(b["x"] for b in boxes), min(b["y"] for b in boxes)


def warn_if_blank(png, floor=0.25):
    """A render nobody looks at is a spec nobody has.

    Every check in this tool runs against design.json; nothing else would
    notice that the picture beside it is empty, and the whole method depends
    on that picture being right.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(png)
    if im.mode not in ("RGBA", "LA"):
        return
    a = im.getchannel("A")
    px = a.resize((80, 80), Image.BOX).getdata()
    covered = sum(1 for v in px if v > 8) / 6400
    if covered < floor:
        print(f"design    RENDER IS {round((1 - covered) * 100)}% TRANSPARENT — "
              f"design.png is almost certainly wrong. Open it before you build "
              f"anything against it; re-run extract on the FRAME node id if the "
              f"crop missed.", file=sys.stderr)


def report_design_change(prev, new):
    """What moved since the last pull.

    A redesign lands as a new render and a new design.json, and reading the
    difference off a screenshot is guesswork. Diffing the two files gave the
    whole implementation list for the Go switcher in three lines: five labels
    added at x95, the rail's nine nodes gone, the text column moved 141 -> 179.
    Nothing else in this tool tells you a node was *deleted*, and a deletion is
    the one change Figma absorbs silently — it is absolutely positioned, so the
    hole stays open, while the same edit in HTML collapses everything below it.
    """
    def tkey(t):
        return (t.get("text", "")[:80], t.get("x"), t.get("y"))
    def fkey(f):
        return (f.get("name"), f.get("x"), f.get("y"), f.get("w"), f.get("h"))

    ot = {tkey(t): t for t in prev.get("texts", [])}
    nt = {tkey(t): t for t in new.get("texts", [])}
    of = {fkey(f): f for f in prev.get("frames", [])}
    nf = {fkey(f): f for f in new.get("frames", [])}
    add_t = [t for k, t in nt.items() if k not in ot]
    del_t = [t for k, t in ot.items() if k not in nt]
    add_f = [f for k, f in nf.items() if k not in of]
    del_f = [f for k, f in of.items() if k not in nf]

    # Same string, new position: a move, not an add plus a delete.
    by_text = {}
    for t in del_t:
        by_text.setdefault(t.get("text", "")[:80], []).append(t)
    moved = []
    for t in list(add_t):
        cand = by_text.get(t.get("text", "")[:80])
        if cand:
            o = cand.pop(0)
            moved.append((o, t))
            add_t.remove(t)
            del_t.remove(o)

    if not (add_t or del_t or moved or add_f or del_f):
        print("changed   nothing since the last extract")
        return
    print("changed   since the last extract "
          "(previous file kept as design.prev.json):")
    for t in sorted(add_t, key=lambda t: (t.get("y", 0), t.get("x", 0)))[:20]:
        print(f"          + text  x{t.get('x')} y{t.get('y')}  "
              f"{t.get('text','')[:52]!r}")
    for t in sorted(del_t, key=lambda t: (t.get("y", 0), t.get("x", 0)))[:20]:
        print(f"          - text  x{t.get('x')} y{t.get('y')}  "
              f"{t.get('text','')[:52]!r}")
    for o, t in sorted(moved, key=lambda p: (p[1].get("y", 0),))[:20]:
        print(f"          ~ text  ({o.get('x')},{o.get('y')}) -> "
              f"({t.get('x')},{t.get('y')})  {t.get('text','')[:40]!r}")
    for f in sorted(add_f, key=lambda f: (f.get("y", 0), f.get("x", 0)))[:20]:
        print(f"          + box   {str(f.get('name'))[:26]:<26} "
              f"x{f.get('x')} y{f.get('y')} {f.get('w')}x{f.get('h')}")
    for f in sorted(del_f, key=lambda f: (f.get("y", 0), f.get("x", 0)))[:20]:
        print(f"          - box   {str(f.get('name'))[:26]:<26} "
              f"x{f.get('x')} y{f.get('y')} {f.get('w')}x{f.get('h')}")
    if del_f or del_t:
        print("          a deletion does not shorten the Figma frame but it "
              "does shorten yours — check the section tops in `audit`.")


def cmd_extract(args):
    if os.path.isdir(args.source):
        return extract_from_export(args)
    args.url = args.source
    tok = env("FIGMA_TOKEN") or die("NO_FIGMA_TOKEN", f"put FIGMA_TOKEN in {HOME}/.env")
    key, node, root, styles = load_frame(args.url, tok)
    slug = args.slug or slugify(root.get("name"))
    out = os.path.join(BUILD, slug)
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)

    # A page URL gives the whole canvas, which is the safer thing to extract:
    # carousel slides, variants and card rows are routinely parked *outside*
    # the main frame, and a frame-only extract loses them silently. Render the
    # largest frame as the layout reference; everything else on the page still
    # contributes its copy and its assets.
    render_node, page_frames = node, []
    if root.get("type") == "CANVAS":
        page_frames = [c for c in (root.get("children") or [])
                       if c.get("type") == "FRAME" and c.get("visible", True)]
        if not page_frames:
            die("EMPTY_PAGE", f"no frames on page {root.get('name')!r}")
        pick = None
        if args.frame:
            pick = next((f for f in page_frames
                         if args.frame.lower() in (f.get("name") or "").lower()), None)
            if not pick:
                die("FRAME_NOT_FOUND", f"no frame matching {args.frame!r} on this page")
        else:
            pick = max(page_frames, key=lambda f: bbox(f)[2] * bbox(f)[3])
        render_node = pick["id"]
        print(f"page      {root.get('name')}  ({len(page_frames)} frames)")
        for f in sorted(page_frames, key=lambda f: -bbox(f)[2] * bbox(f)[3])[:8]:
            _, _, fw, fh = bbox(f)
            flag = "  <- layout reference" if f["id"] == render_node else ""
            print(f"            {f['id']:<12} {str(f.get('name'))[:30]:<32}"
                  f" {round(fw)}x{round(fh)}{flag}")
        root_for_size = pick
    else:
        root_for_size = root

    ids = set()
    walk(root, lambda n: ids.add(n["id"]))

    _, _, w, h = bbox(root_for_size)
    images, icons = collect_assets(root)
    design = {
        "source": {"file_key": key, "node_id": node, "name": root.get("name"),
                   "kind": root.get("type"), "render_node": render_node,
                   "width": round(w), "height": round(h), "url": args.url},
        "tokens": collect_tokens(root, styles),
        "texts": collect_texts(root, styles, root_for_size),
        "frames": collect_frames(root, root_for_size),
        "comments": collect_comments(key, ids, tok),
        "assets": images + icons,
    }
    dpath = os.path.join(out, "design.json")
    prev = None
    if os.path.exists(dpath):
        try:
            prev = json.load(open(dpath, encoding="utf-8"))
            shutil.copyfile(dpath, os.path.join(out, "design.prev.json"))
        except Exception:
            prev = None
    with open(dpath, "w") as fh:
        json.dump(design, fh, ensure_ascii=False, indent=1)
    print(f"slug      {slug}")
    print(f"frame     {root.get('name')}  {round(w)}x{round(h)}")
    print(f"texts     {len(design['texts'])}")
    print(f"frames    {len(design['frames'])} boxes measured")
    if design["comments"]:
        print(f"comments  {len(design['comments'])} unresolved — READ THESE, they carry "
              f"behaviour the geometry cannot show:")
        for c in design["comments"]:
            print(f"            {c['text'][:72]}")
    if prev:
        report_design_change(prev, design)
    print(f"assets    {len(images)} images, {len(icons)} icons")

    # The layout reference. Render the *canvas*, not the frame, then crop to
    # the frame's bounds: designers park pieces beside a frame and let them
    # overlap it, and those siblings paint on top of the frame on the canvas
    # but are absent from a frame-only render. Rendering the frame alone gave
    # a reference with three feature cards, four product panels and a carousel
    # button missing, so the page was compared against a picture that was
    # itself wrong.
    if not args.no_render:
        scale = 1.0 if h <= 20000 else round(20000 / h, 3)
        target = node if root.get("type") in ("CANVAS", "SECTION") else render_node
        res = figma_get(f"/v1/images/{key}?" + urllib.parse.urlencode(
            {"ids": target, "format": "png", "scale": scale}), tok)
        url = (res.get("images") or {}).get(target)
        if not url:
            print("design    render failed — export the frame from Figma by hand "
                  "(select it, Export, PNG 1x) and save it as design.png",
                  file=sys.stderr)
        else:
            png = os.path.join(out, "design.png")
            download(url, png)
            if target != render_node:
                fx, fy, _, _ = bbox(root_for_size)
                cx, cy = canvas_origin(root)
                try:
                    from PIL import Image
                    im = Image.open(png)
                    im.crop((round((fx - cx) * scale), round((fy - cy) * scale),
                             round((fx - cx + w) * scale),
                             round((fy - cy + h) * scale))).save(png)
                    print("design    canvas rendered, cropped to the frame — "
                          "anything parked over the frame is included")
                except ImportError:
                    print("design    canvas rendered UNCROPPED (needs Pillow): "
                          f"the frame sits at x={round(fx - cx)} in it",
                          file=sys.stderr)
            warn_if_blank(png)
            print(f"design    design.png ({round(w * scale)}x{round(h * scale)})")

    if args.no_assets:
        return
    if images:
        table = (figma_get(f"/v1/files/{key}/images", tok).get("meta") or {}).get("images", {})
        got = 0
        for a in images:
            u = table.get(a["ref"])
            if u:
                download(u, os.path.join(out, a["file"]))
                got += 1
        print(f"images    {got}/{len(images)} (originals)")
    if icons:
        got = 0
        for i in range(0, len(icons), 60):
            chunk = icons[i:i + 60]
            res = figma_get(f"/v1/images/{key}?" + urllib.parse.urlencode(
                {"ids": ",".join(c["node"] for c in chunk), "format": "svg"}), tok)
            for c in chunk:
                u = (res.get("images") or {}).get(c["node"])
                if u:
                    download(u, os.path.join(out, c["file"]))
                    got += 1
        print(f"icons     {got}/{len(icons)}")


# ------------------------------------------------------------------- assemble

def assemble(out):
    """page.html + page.css -> the body that ships.

    The three files are for editing; WordPress only ever sees one blob, so
    everything downstream — verify, preview, push — works on the assembled
    result. Checking page.html directly would let a mistake introduced by the
    assembly pass unnoticed, which is the whole failure mode this guards.

    page.js is NOT inlined: post_content mangles real JavaScript. It travels to
    the page's own footer through post meta instead (see push).
    """
    hp = os.path.join(out, "page.html")
    if not os.path.exists(hp):
        die("MISSING", hp)
    html = open(hp, encoding="utf-8").read()
    css_path = os.path.join(out, "page.css")
    if "{{styles}}" in html:
        if not os.path.exists(css_path):
            die("MISSING", css_path + " (page.html asks for {{styles}})")
        css = open(css_path, encoding="utf-8").read().strip()
        html = html.replace("{{styles}}", "<style>\n" + css + "\n</style>")
    elif os.path.exists(css_path):
        print("warn  page.css exists but page.html has no {{styles}} placeholder",
              file=sys.stderr)
    return html


PAGE_JS_HEADER = """/* Behaviour that only this page needs.
 *
 * Reusable behaviour does NOT belong here — it goes in WP Buddy's
 * assets/wpbuddy-page.js, where one copy serves every page and a fix lands
 * everywhere at once. This file is for the genuinely one-off, and for trying
 * something out before it earns a place in the shared file.
 *
 * It is delivered through post meta and printed from wp_footer, never inlined
 * into the body: post_content mangles real JavaScript (a raw "<" makes every
 * following "&" become "&#038;", so "&&" stops parsing).
 *
 * Leave it at comments only when the page needs nothing.
 */
"""


def page_js(out):
    """The page's own script, or "" when the file holds nothing but comments.

    A comments-only file still documents the slot without shipping an empty
    <script> to every visitor.
    """
    p = os.path.join(out, "page.js")
    if not os.path.exists(p):
        return ""
    src = open(p, encoding="utf-8").read()
    stripped = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
    return src.strip() if stripped.strip() else ""


# --------------------------------------------------------------------- verify

def strip_tags(html):
    # A shortcode's visible label lives in an attribute, so lift those out
    # before dropping markup — otherwise copy that is genuinely on the page
    # (a CTA rendered by [wpb_hs_form text="Contact us"]) reads as missing.
    html = re.sub(r"\[[a-z_][a-z0-9_]*\b[^\]]*?\b(?:text|title|label)=\"([^\"]*)\"[^\]]*\]",
                  r" \1 ", html, flags=re.I)
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    # Every entity, not a hand-picked six. Authoring `&rsquo;` for a curly
    # apostrophe is the obvious thing to do and the copy check used to call it
    # a missing string, which sends you hunting for a bug in your markup.
    return unescape(html)


def squash(t):
    return re.sub(r"\s+", "", t)


def cmd_verify(args):
    errors, warnings = run_checks(os.path.join(BUILD, args.slug), report=True,
                                  section=getattr(args, "section", None),
                                  through=getattr(args, "through", None))
    sys.exit(1 if errors else 0)


def section_range(out, name, through=False):
    """The design y-range a section covers, from sections.json.

    Building a page one section at a time means every string below the one you
    are on is legitimately absent, and a check that fails on all of them stops
    being read. `--section` narrows the copy check to the band you are working
    in; `--through` keeps everything above it in scope too, so an earlier
    section cannot quietly rot while you work further down.
    """
    sp = os.path.join(out, "sections.json")
    if not os.path.exists(sp):
        die("NO_SECTION_MAP", "run `audit` first — it records where each "
                              "section's content sits in the frame.")
    smap = json.load(open(sp, encoding="utf-8"))
    if name not in smap:
        die("NO_SUCH_SECTION", f"{name!r} is not mapped. Known: " + ", ".join(smap))
    v = smap[name]
    lo = min(x["design_y"] for x in smap.values()) if through else v["design_y"]
    return lo - 8, v["design_y"] + v["design_span"] + 8


def run_checks(out, report=False, section=None, through=None):
    """Every check, returned rather than printed, so push can run them too."""
    dj = os.path.join(out, "design.json")
    if not os.path.exists(dj):
        die("MISSING", dj)
    with open(dj) as fh:
        design = json.load(fh)
    html = assemble(out)
    body = squash(strip_tags(html))

    # Copy left out on purpose — site header/footer, blocks left over from
    # whatever design the frame was copied from. Writing the reason down makes
    # the omission reviewable instead of silent.
    dropped = {}
    dpath = os.path.join(out, "dropped.json")
    if os.path.exists(dpath):
        with open(dpath) as fh:
            dropped = {squash(k): v for k, v in json.load(fh).items()}

    errors, warnings, skipped = [], [], 0
    texts = design["texts"]
    band = None
    if section or through:
        band = section_range(out, through or section, through=bool(through))
        texts = [t for t in texts if band[0] <= t["y"] <= band[1]]
        if report:
            print(f"scope     y{band[0]}..{band[1]} — "
                  f"{'everything down through' if through else 'only'} "
                  f"{through or section}; {len(texts)} of "
                  f"{len(design['texts'])} strings in scope. Run without it "
                  f"before pushing.")
    for t in texts:
        s = squash(t["text"])
        if not s or s in body:
            continue
        if any(k and k in s for k in dropped):
            skipped += 1
            continue
        errors.append(f'copy missing (#{t["i"]}, y={t["y"]}): {t["text"].strip()[:70]!r}')

    # A hard break in the file is content, not styling: it decides where the
    # line ends, and the copy check cannot see it because U+2028 is whitespace
    # to Python and squashes away. Writing it into the markup as &#8232; does
    # nothing either — HTML treats U+2028 as an ordinary space, so the browser
    # wraps wherever the column happens to run out. Both pricing notes on the
    # Asana page broke in the wrong place for exactly this reason, one of them
    # with the character sitting right there in the source. It needs a <br>.
    BLOCK = ("br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6",
             "ul", "ol", "td", "tr", "section", "article", "figcaption")
    marked = re.sub(r"</?(?:%s)\b[^>]*>" % "|".join(BLOCK), "\x00", html, flags=re.I)
    marked = re.sub(r"[^\S\x00]+", "", strip_tags(marked))
    marked = re.sub(r"\x00+", "\x00", marked)
    for t in texts:
        parts = [p for p in re.split(r"[\u2028\u2029\n]", t["text"]) if squash(p)]
        if len(parts) < 2:
            continue
        if any(k and k in squash(t["text"]) for k in dropped):
            continue
        if "\x00".join(squash(p) for p in parts) not in marked:
            errors.append(
                f'hard break not reproduced (#{t["i"]}, y={t["y"]}): the file '
                f'breaks after {squash(parts[0])[-28:]!r} — put a <br> there. '
                f'U+2028 in the markup will not do it.')

    # The theme paints bare <button>s itself. elementor-kit-7676 sets a
    # background on `button` and a different one on `button:hover, button:focus`
    # — an orange rest and a green hover, from the site's own palette. A
    # two-class rule of ours beats the rest state, so a page looks right until
    # someone clicks: the pseudo-class puts the kit's selector ahead of any
    # plain two-class rule and the control turns green. It survived a review,
    # a push and a read-through of the rendered page, because nothing static
    # ever enters that state.
    btn_css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    stated = set()
    for sel, decl in re.findall(r"([^{}@]+)\{([^{}]*)\}", btn_css):
        if not re.search(r"(?<![-\w])background(-color)?\s*:", decl):
            continue
        if not re.search(r":(hover|focus|focus-visible|active)\b", sel):
            continue
        stated |= set(re.findall(r"\.([A-Za-z0-9_-]+)", sel))
    unstated = {}
    # Scan the markup, not the stylesheet inlined into it: a CSS comment that
    # says "<button>" is not a button, and the first run of this check found
    # two of them — in the comment explaining this very rule.
    markup = re.sub(r"<style\b.*?</style>", " ", html, flags=re.S | re.I)
    markup = re.sub(r"<!--.*?-->", " ", markup, flags=re.S)
    for m in re.finditer(r"<button\b[^>]*>", markup, re.I):
        cls = re.search(r'class="([^"]*)"', m.group(0))
        names = set((cls.group(1) if cls else "").split())
        if names & stated:
            continue
        unstated[" ".join(sorted(names)) or "<button> with no class"] = True
    for name in sorted(unstated):
        warnings.append(f'button .{name} states no :hover/:focus background — '
                        f'the theme does, and its rule wins on the pseudo-class. '
                        f'Pin the states or the control changes colour when it '
                        f'is clicked')

    # The behaviour classes are a contract with wpbuddy-page.js, and the
    # symptom of breaking it is silence: the page renders, nothing responds to
    # a click, and it reads as a production problem. Check the halves line up.
    doc = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    ids = set(re.findall(r'\bid="([^"]+)"', doc))
    for p_ in sorted(set(re.findall(r'class="[^"]*\bptab\b[^"]*"[^>]*data-p="([^"]+)"', doc))
                     | set(re.findall(r'data-p="([^"]+)"[^>]*class="[^"]*\bptab\b', doc))):
        if "p-" + p_ not in ids:
            errors.append(f'.ptab[data-p="{p_}"] has no #p-{p_} panel — the tab '
                          f'will highlight and switch nothing')
    for car in sorted(set(re.findall(r'data-car="([^"]+)"', doc))):
        if car not in ids:
            errors.append(f'[data-car="{car}"] points at #{car}, which is not '
                          f'in the markup — the control is inert')
    for m in re.finditer(r'class="([^"]*\bmc-loop\b[^"]*)"', doc):
        if "mc-loop__track" in m.group(1):
            continue
        if "mc-loop__track" not in doc:
            errors.append('.mc-loop with no .mc-loop__track inside — the endless '
                          'row needs the track element to move')
            break

    # An icon the file repeats is a UI element, not decoration, and the page
    # should be drawing the file's own path rather than an approximation of it.
    # The Asana checklist marks were rotated CSS borders standing in for a 9x7
    # polyline and two 9.5 diagonals: the tick sat 3px low inside its own disc,
    # the cross was grey where the file draws white, and no geometry check
    # could see any of it because the disc around them was the right size in
    # the right place. Match on the path's numbers, rounded, so an inlined
    # data: URI written in shorthand still counts as used.
    def dsig(d):
        """Shape, not size: the same mark exported at 12px and inlined at 11.5
        has to compare equal, so normalise each subpath into a unit box."""
        out = set()
        for sub in re.split(r"[Mm]", d):
            v = [float(x) for x in re.findall(r"-?\d*\.?\d+", sub)]
            if len(v) < 4:
                continue
            xs, ys = v[0::2], v[1::2]
            k = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
            out.add(" ".join(f"{round((a - b) / k, 2):g}"
                             for a, b in zip(v, [min(xs), min(ys)] * len(xs))))
        return out
    used = set()
    for pat in (r'\bd="([^"]+)"', r"\bd='([^']+)'"):
        for d in re.findall(pat, unquote(html)):
            used |= dsig(d)
    shapes = {}
    for a in design["assets"]:
        if not a["id"].startswith("i"):
            continue
        f = os.path.join(out, a["file"])
        if not os.path.exists(f):
            continue
        try:
            svg = open(f, encoding="utf-8").read()
        except OSError:
            continue
        key = set()
        for d in re.findall(r'\bd="([^"]+)"', svg):
            key |= dsig(d)
        if not key:
            continue
        key = tuple(sorted(key))
        rec = shapes.setdefault(key, {"n": 0, "file": a["file"]})
        rec["n"] += 1
    # dropped.json is where an omission goes to get a reason. A chevron that
    # belongs to the site header, or a wordmark shipped as one raster instead
    # of five letter paths, is a decision — name the file there and the
    # warning stops, the way it already works for copy.
    orphan = [r for k, r in shapes.items()
              if r["n"] >= 3 and not any(d in used for d in k)
              and r["file"] not in html
              and squash(r["file"]) not in dropped]
    for r in sorted(orphan, key=lambda r: -r["n"]):
        warnings.append(f'icon drawn {r["n"]}x in the frame and nowhere in the '
                        f'page: {r["file"]} — the file exported this path, so '
                        f'use it (or inline it) instead of approximating the '
                        f'shape in CSS')

    for a in design["assets"]:
        if a["id"].startswith("a") and a["file"] not in html:
            warnings.append(f'image not used: {a["file"]} ({a.get("name")})')

    # Inline <script> written through mc/set-post-html DOES survive and run on
    # the front end — measured, against the plugin's own comments claiming
    # otherwise. KSES is what strips it, and mc/set-post-html removes KSES. So
    # this is a note, not a failure: a one-off effect is legitimately inline,
    # while anything reusable belongs in wpbuddy-page.js where it is shared,
    # minified and fixable in one place.
    code = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    code = re.sub(r"<style\b.*?</style>", " ", code, flags=re.S | re.I)
    n_inline = len(re.findall(r"<script\b", code, re.I))
    if n_inline:
        warnings.append(f"{n_inline} inline <script> — fine for a one-off, but move "
                        "anything reusable into wpbuddy-page.js")
    for pat, msg in (
        (r"https?://(?:www\.)?figma\.com", "leftover figma.com URL"),
        (r"s3-alpha-sig\.figma\.com|figma-alpha-api", "leftover Figma CDN URL"),
        (r"https?://(?:www\.)?masterconcept\.ai",
         "absolute site link — use a relative path so WPML can localise it"),
    ):
        if re.search(pat, html, re.I):
            errors.append(msg)

    # Which selectors pin an image's box so it never depends on the file's
    # own dimensions — either an explicit aspect-ratio, or both width and
    # height set to something that is not auto.
    page_css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    pinned = []
    for sel, decl in re.findall(r"([^{}@]+)\{([^{}]*)\}", page_css):
        d = decl.replace(" ", "")
        has_ratio = "aspect-ratio:" in d
        both = (re.search(r"(?<![-\w])width:(?!auto)", d)
                and re.search(r"(?<![-\w])height:(?!auto)", d))
        if has_ratio or both:
            for one in sel.split(","):
                pinned.append(set(re.findall(r"\.([A-Za-z0-9_-]+)", one)))

    # Walk the markup keeping the open elements' classes, so a rule written
    # against the wrapper (.sh-card__icon img) still counts for an <img> that
    # carries no class of its own.
    stack, void = [], {"img", "br", "hr", "input", "meta", "link", "source"}
    for m in re.finditer(r"<(/?)([A-Za-z][\w-]*)([^>]*?)(/?)>", html):
        closing, tag, attrs, selfclose = m.groups()
        tag = tag.lower()
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    del stack[i:]
                    break
            continue
        cls = re.search(r'class="([^"]*)"', attrs)
        names = set(cls.group(1).split()) if cls else set()
        if tag == "img":
            here = set(names)
            for _, s_ in stack:
                here |= s_
            alt = re.search(r'alt="([^"]*)"', attrs)
            if 'aria-hidden="true"' not in attrs:
                if not alt:
                    errors.append(f"img without alt: {m.group(0)[:70]}")
                elif not alt.group(1).strip():
                    errors.append(f"img with empty alt: {m.group(0)[:70]}")
            # The site lazy-loads: SiteGround swaps every src for a 1x1 gif
            # until the image scrolls in. width/height attributes do not save
            # you — they apply only while the image is *unloaded*, and the
            # placeholder counts as loaded — so height:auto reserves a square.
            # That shipped three times on one page: a 318x280 watermark drawn
            # 318x318, a 467x67 wordmark drawn 240x240, and a product panel
            # 454px tall with nothing in it.
            if not any(p and p <= here for p in pinned):
                warnings.append(
                    f"img box depends on the file's own size: "
                    f"{m.group(0)[:60]} — lazyload serves a 1x1 gif first, so "
                    f"give it aspect-ratio (or width+height) in the CSS")
            continue
        if selfclose or tag in void:
            continue
        stack.append((tag, names))

    # The site header carries a global back-to-top link pointing at #herotop,
    # and every page supplies that anchor itself — the Elementor pages set it on
    # their hero section. A page without it looks fine and the link silently
    # does nothing, so nobody notices. Convention, not code: it appears in no
    # repo, no brief and no design file.
    if 'id="herotop"' not in html:
        errors.append('no id="herotop" — the site header links to it from every '
                      'page, so the anchor is mandatory; put it on the first '
                      'section, as the Elementor pages do')

    # The <style> block ships inside post_content, so it cannot reach other
    # pages — but it shares the document with the site's Elementor header,
    # footer and mega menu. A bare `.ppanel{display:none}` silently hides
    # anything the chrome happens to call ppanel, and nothing errors: the page
    # looks fine until the day a shared class collides.
    #
    # CSS descendant selectors are anchored on the left, so only the leftmost
    # compound decides what a rule can reach: `.sh-faq .acc-body` is already
    # confined to this page, while a bare `.acc-body` is not.
    style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    wrapper = (re.findall(r'class="mc-page ([\w-]+)"', html) or [""])[0]
    if style and wrapper:
        prefix = wrapper.split("-")[0] + "-"          # sh-page -> sh-
        body = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        body = re.sub(r"@media[^{]*\{", "", body)
        bare = set()
        for group in re.findall(r"([^{}]+)\{[^{}]*\}", body):
            for sel in group.split(","):
                sel = sel.strip()
                if (not sel or sel.startswith(("@", "%", "from", "to", ":root"))
                        or sel[0].isdigit()):
                    continue
                if wrapper in sel or "mc-page" in sel:
                    continue
                head = re.split(r"[\s>+~]", sel.strip(), 1)[0]
                classes = re.findall(r"\.([\w-]+)", head)
                if classes and any(c.startswith(prefix) for c in classes):
                    continue
                bare.add(sel)
        for sel in sorted(bare):
            warnings.append(f"unscoped selector {sel!r} — it can also match the "
                            f"site header and footer on this page; prefix it "
                            f"with .{wrapper}")

    # The theme caps .site-main at 1140px on every page Elementor did not build
    # (see references/site-rules.md). A design wider than that cannot render
    # correctly without the breakout, and the failure is invisible in the
    # markup: nothing overflows, every band just becomes a 1140px stripe.
    dw = (design.get("source") or {}).get("width") or 0
    if dw > 1140 and "50vw" not in html.replace(" ", ""):
        errors.append(f"the design is {dw}px wide but no wrapper takes the width "
                      f"back from the theme's 1140px clamp — add "
                      f"margin-left/right:calc(50% - 50vw) to the page wrapper")

    # Figma applies textCase at render time, so the stored characters look
    # unchanged and the copy check passes either way. If the design asks for a
    # case the stylesheet never mentions, the page ships in the wrong case and
    # nothing complains.
    flat = html.replace(" ", "")
    for case in sorted({t["case"] for t in design.get("texts", []) if t.get("case")}):
        if f"text-transform:{case}" not in flat:
            n = sum(1 for t in design["texts"] if t.get("case") == case)
            warnings.append(f"design sets text-transform {case} on {n} "
                            f"strings, but the CSS never uses it")

    # A named font renders only if something actually loads it. masterconcept.ai
    # names Raleway in every stylesheet and loads it nowhere, so visitors without
    # it installed silently get Lato, then Roboto — and it looks correct on the
    # designer's machine, which is how it goes unnoticed. Check the page can
    # stand on its own.
    # Both the property and any custom property holding a stack — a page that
    # sets --x-font once and refers to it with var() everywhere would otherwise
    # slip through.
    stacks = re.findall(r"font-family\s*:\s*([^;}]+)", html, re.I)
    stacks += re.findall(r"--[\w-]*font[\w-]*\s*:\s*([^;}]+)", html, re.I)
    first = []
    for st in stacks:
        f = st.split(",")[0].strip().strip("\"'")
        if f and not f.startswith("var(") and f.lower() not in (
                "inherit", "initial", "unset", "sans-serif", "serif", "monospace"):
            first.append(f)
    self_loaded = re.findall(r"fonts\.googleapis\.com[^\"']*|@font-face|@import[^;]*", html, re.I)
    for f in sorted(set(first)):
        if not any(f.split()[0].lower() in s_.lower() for s_ in self_loaded):
            warnings.append(f"font {f!r} is named but this page does not load it — "
                            f"confirm the site does, or visitors get the fallback")

    for m in re.finditer(r"(?<!max-)(?<!min-)\bwidth\s*:\s*(\d{3,})px", html):
        if int(m.group(1)) > 480:
            warnings.append(f"fixed width:{m.group(1)}px — prefer max-width")

    for e in errors:
        print(f"FAIL  {e}")
    for w in warnings:
        print(f"warn  {w}")
    total = len(texts)
    print(f"\n{total - len([e for e in errors if e.startswith('copy')])}/{total} "
          f"strings matched ({skipped} dropped on purpose)")
    print(f"{len(errors)} errors, {len(warnings)} warnings")
    return errors, warnings


# -------------------------------------------------------------------- preview


# The browser-side measurement lives in measure.js, not in a Python string.
# 183 lines of JavaScript inside triple quotes gets no syntax check and no
# editor: a call to a helper that did not exist parsed fine, ran fine, and
# surfaced as "no fw-measure node in the DOM" — a silent failure that reads
# like a browser problem. `doctor` syntax-checks the file now.
MEASURE_JS = ("<script>\n"
              + open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "measure.js"), encoding="utf-8").read()
              + "</script>")


def dump_dom(url, width=1440, timeout=180):
    """The page's DOM after its scripts have run."""
    exe = next((c for c in CHROME if os.path.exists(c)), None)
    if not exe:
        die("NO_BROWSER", "audit needs Chrome or Chromium installed.")
    # Without a window size Chrome uses its default width, the page reflows to
    # something no one will ever see, and every measurement describes that
    # instead. The design's own width is the only one worth measuring at.
    # --hide-scrollbars so this measures the same width the screenshot in
    # `diff` does. Without it audit read 1425 and diff drew 1440, and the two
    # tools disagreed about where everything was.
    r = subprocess.run([exe, "--headless=new", "--disable-gpu",
                        "--hide-scrollbars",
                        f"--window-size={width},20000",
                        "--virtual-time-budget=6000", "--dump-dom", url],
                       capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", "replace")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def norm_text(t):
    return " ".join(unescape(t).split())


def css_px(v):
    m = re.match(r"\s*(-?[\d.]+)px", str(v or ""))
    return round(float(m.group(1)), 1) if m else None


def css_hex(v):
    """Normalise a colour for comparison. Alpha has to survive: the design
    records the Why-MC paragraph as rgba(255,255,255,0.9) and dropping the .9
    turned it into #FFFFFF, so the check could never pass however the page was
    written."""
    v = str(v or "").strip()
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", v)
    if m:
        hexed = "#%02X%02X%02X" % tuple(int(g) for g in m.groups()[:3])
        a = m.group(4)
        return hexed if a is None or float(a) >= 0.999 else f"{hexed}@{float(a):g}"
    m = re.match(r"#([0-9a-fA-F]{6})$", v)
    return "#" + m.group(1).upper() if m else None


def cmd_audit(args):
    """Name every element that does not match the design, instead of scoring regions.

    `diff` reports a band as hot and leaves you to find out why, which is a step
    that gets skipped or rationalised — three whole sections shipped wrong that
    way. This reads the rendered geometry back out of the browser and compares
    it string by string, so a difference is a line you have to answer for.
    """
    out = os.path.join(BUILD, args.slug)
    design = json.load(open(os.path.join(out, "design.json"), encoding="utf-8"))
    dom = dump_dom(args.url)
    m = re.search(r'<script type="application/json" id="fw-measure">(.*?)</script>',
                  dom, re.S)
    if not m:
        die("NO_MEASUREMENTS", "the page carries no measurement block. Serve it "
                               "with `preview`, which injects one.")
    payload = json.loads(m.group(1))
    live, sections = payload["runs"], payload.get("sections") or []
    hidden = [e for e in live if e.get("clipped")]
    live = [e for e in live if not e.get("clipped")]
    print(f"measured  {len(live)} text runs in {len(sections)} sections"
          + (f"  ({len(hidden)} skipped: collapsed/clipped on the page)"
             if hidden else ""))
    if args.section:
        keep = [e for e in live if e.get("sec") == args.section]
        if not keep:
            die("NO_SUCH_SECTION",
                f"no section {args.section!r}. The page has: "
                + ", ".join(x["name"] for x in sections))
        live = keep
        print(f"section   {args.section} only ({len(live)} runs)")

    # The same string appears in every pricing card, so matching on text alone
    # pairs a page element in column three with a design node in column one and
    # the offsets come out as nonsense. Establish the offset from the strings
    # that are unique on both sides first, then place the duplicates by
    # position.
    dcount = collections.Counter(norm_text(t["text"])[:40] for t in design["texts"])
    lcount = collections.Counter(norm_text(e["text"])[:40] for e in live)

    # The same string appears in all three pricing cards. Pair the nth
    # occurrence down the page with the nth down the frame; matching on text
    # alone put column three against column one and every offset came out
    # meaningless.
    # One Figma node can hold several type styles and the page splits it across
    # several elements: the Go panel is a single node carrying 20/32 bold bullet
    # headings over 16/28 medium body, which the page writes as <b> inside <li>.
    # Comparing every one of those against runs[0] checked the intro three times
    # and the headings never — the panel shipped at 14px and audit called it
    # clean. Index each run's own characters as well as the whole node.
    def expand_runs(texts):
        out = []
        for t in texts:
            out.append(t)
            runs = t.get("runs") or []
            if len(runs) < 2:
                continue
            s, i = t["text"], 0
            for r in runs:
                seg, i = s[i:i + r["chars"]], i + r["chars"]
                if len(norm_text(seg)) < 6:
                    continue
                ty = dict(t.get("type") or {})
                ty.update({k: v for k, v in r.items()
                           if k not in ("chars", "case") and v is not None})
                out.append({**t, "text": seg, "runs": None, "type": ty,
                            "_seg": True})
        return out

    # A designer parks old versions beside the frame — "Learn about Go" exists
    # twice, on-canvas at 14/18 bold and at x=-405 at 14/24 medium. Sorting by
    # x alone let the parked one win and the page was told to match a copy
    # nobody sees. On-canvas first, then down the page.
    fw = (design.get("source") or {}).get("width") or 0
    def parked(t):
        return 0 if (fw and 0 <= t["x"] < fw) or not fw else 1

    by_key = {}
    for t in sorted(expand_runs(design["texts"]),
                    key=lambda t: (parked(t), t["y"], t["x"])):
        by_key.setdefault(norm_text(t["text"])[:40], []).append(t)

    used = collections.Counter()
    pairs = []
    for el in sorted(live, key=lambda e: (e["y"], e["x"])):
        key = norm_text(el["text"])[:40]
        if len(key) < 6:
            continue
        cands = by_key.get(key) or next(
            (v for k, v in by_key.items() if k.startswith(key) or key in k), None)
        if not cands:
            continue
        t = cands[min(used[id(cands)], len(cands) - 1)]
        used[id(cands)] += 1
        pairs.append((el, t))
    if len(pairs) < 5:
        die("NO_MATCHES", "too little rendered text matched design.json.")

    # A page and a frame of different total heights drift apart as you go down,
    # and that drift is not itself a fault. What matters is where it CHANGES:
    # a jump between one string and the next is space the page added or lost
    # right there. So the baseline is the running offset, not a single number.
    pairs.sort(key=lambda p: p[1]["y"])
    # The origin has to obey the same rule the per-row x check obeys below: a
    # string that appears in all three pricing cards is paired by order and can
    # land against the wrong column, so it cannot vote on where the page sits.
    # Letting them vote put the origin at +157px on a page whose cards were
    # measured at +0, and every x finding was then reported 157px out — 26 of
    # 27 "distinct fixes" were that one number wearing different names.
    # Two kinds of pair cannot vote. A string that appears in all three pricing
    # cards is paired by order and can land against the wrong column. And a
    # string inside a rotating row sits wherever the rotation left it — on this
    # page 35 of 64 otherwise-unique pairs were carousel cards, clustered at
    # +157 and +189, and they alone decided the median. The page's real offset
    # was near zero, so every x finding came out 157px wrong.
    fw_w = (design.get("source") or {}).get("width") or 10 ** 6
    sure = [(e, t) for e, t in pairs
            if not t.get("_seg")
            and len(by_key.get(norm_text(e["text"])[:40], [])) == 1
            and lcount[norm_text(e["text"])[:40]] == 1
            and not e.get("inner")
            and not e.get("loop")
            and 0 <= t["x"] < fw_w]
    basis = sure if len(sure) >= 5 else pairs
    dxs = sorted(e["x"] - t["x"] for e, t in basis)
    dx = dxs[len(dxs) // 2]
    print(f"origin    page sits {dx:+d}px across from the frame; "
          f"{len(pairs)} strings compared"
          + ("" if basis is sure else "  (too few unique strings — every pair "
                                      "voted, so this may be off)")
          + f"\n          {len(sure)} of them unique on both sides and used "
            f"to place it\n")

    def rect(el, t):
        """Where to measure this pair. A page run that carries only part of
        the design string is an inline fragment of a bigger block — the design
        node covers the whole block, so compare block to block. Otherwise the
        run's own rect is the tighter, better answer."""
        if el.get("by") is not None and len(norm_text(el["text"])) + 4 < len(norm_text(t["text"])):
            return el["by"], el["bh"]
        return el["y"], el["h"]

    rows = []
    prev = None
    for el, t in pairs:
        ty = (t.get("runs") or [t.get("type") or {}])[0]
        bad = []
        # Position is only trustworthy where the pairing is certain. A string
        # that appears in all three pricing cards can be paired by order and
        # still land against the wrong column, and a confident wrong number is
        # worse than none. Type properties need no pairing beyond the text.
        # A node parked beside the frame has no position to compare against —
        # the page renders that copy wherever the layout puts it, which is the
        # right answer, and reporting it as "x +2622px" 26 times buries the
        # findings that mean something.
        certain = (not t.get("_seg")
                   and len(by_key.get(norm_text(el["text"])[:40], [])) == 1
                   and lcount[norm_text(el["text"])[:40]] == 1
                   and 0 <= t["x"] < fw_w)
        if certain and not el.get("inner"):
            dy = rect(el, t)[0] - t["y"]
            if prev is not None and abs(dy - prev) > args.tolerance:
                bad.append(("gap above", f"{dy - prev:+d}px"))
            prev = dy
            # A centred block is placed by its centre, not its left edge: give
            # it a wider box and the text does not move at all. Comparing left
            # to left reported a heading 267px out that was sitting exactly
            # where the frame puts it, and would have had someone "fix" it.
            # Compare the content box: the frame's text node starts where the
            # ink starts, and an element's own padding is not displacement.
            pl, pr = el.get("padl", 0), el.get("padr", 0)
            # If the frame centres a string and the page does not (or the
            # other way round), the boxes can agree to the pixel while the ink
            # sits a hundred px apart. Compare centres whenever EITHER side
            # centres, and say so when they disagree about which it is.
            da = t.get("align", "left")
            if da != el.get("align", "left"):
                bad.append(("align", f'{el.get("align","left")} vs {da}'))
            if (el.get("align") == "center" or da == "center") and t.get("w"):
                ox = ((el["x"] + pl + (el.get("w", 0) - pl - pr) / 2)
                      - (t["x"] + t["w"] / 2) - dx)
                ox = int(round(ox))
                label = "centre"
            else:
                ox = int(round(el["x"] + pl - t["x"] - dx))
                label = "x"
            if abs(ox) > args.tolerance:
                bad.append((label, f"{ox:+d}px"))
        if ty.get("size") and el.get("size"):
            if abs(el["size"] - ty["size"]) > 0.6:
                bad.append(("size", f'{el["size"]:.0f} vs {ty["size"]:.0f}'))
        lh = css_px(el["lh"])
        if ty.get("lh") and lh and abs(lh - ty["lh"]) > 1.5:
            bad.append(("line-height", f'{lh:.0f} vs {ty["lh"]:.0f}'))
        # Where the copy breaks. A single-line node in Figma is auto-width, so
        # its w is just the ink and comparing it means nothing; a node that
        # wraps has been given a width, and that width is what decides every
        # break in it. Two faults hid behind this on one page: a card body set
        # 338 wide against the frame's 322, so every line broke a word early,
        # and a checklist row 6px narrow per line because the face was tracked
        # differently. Neither moved the pixel score by a measurable amount.
        # Only where the comparison means something: the page element has to
        # carry exactly this string and nothing more (a node the page splits
        # into a heading and a paragraph has two different line counts by
        # design), the frame's node has to be one style throughout, the page
        # box must not be padded (a button's 48px box is two 24px lines of
        # nothing), and a break forced with <br> is not a wrap.
        same = norm_text(el["text"]) == norm_text(t["text"])
        one_style = len({(r.get("size"), r.get("weight"), r.get("lh"))
                         for r in (t.get("runs") or [])}) <= 1
        unpadded = not (el.get("padt") or el.get("padb"))
        # An inline fragment has no width of its own to wrap in.
        if same and one_style and unpadded and not el.get("br") \
                and not el.get("inner"):
            dlines = round((t.get("h") or 0) / ty["lh"]) if ty.get("lh") else 0
            plines = round((el.get("h") or 0) / lh) if lh else 0
            if dlines >= 2 or plines >= 2:
                if dlines and plines and dlines != plines:
                    bad.append(("lines", f"{plines} vs {dlines}"))
                elif dlines >= 2 and t.get("w") and el.get("w"):
                    pw = el["w"] - el.get("padl", 0) - el.get("padr", 0)
                    if abs(pw - t["w"]) > max(6, args.tolerance):
                        bad.append(("wrap width", f'{pw:.0f} vs {t["w"]}'))
        # getComputedStyle reports untracked text as "normal", which is 0.
        # The file names a family per node. Ours renders one face for the whole
        # page, so a node the file tags differently is worth saying out loud —
        # it is usually a Figma fallback, not a decision.
        df, pf = ty.get("family") or (t.get("type") or {}).get("family"), el.get("family")
        if df and pf and df.split()[0].lower() != pf.split()[0].lower():
            bad.append(("family", f"{pf} vs {df}"))
        ls = 0.0 if str(el.get("ls")) == "normal" else css_px(el.get("ls"))
        if ty.get("ls") is not None and ls is not None and abs(ls - ty["ls"]) > 0.3:
            bad.append(("letter-spacing", f'{ls:.1f} vs {ty["ls"]:.1f}'))
        if ty.get("weight") and str(el["weight"]).isdigit():
            if int(el["weight"]) != int(ty["weight"]):
                bad.append(("weight", f'{el["weight"]} vs {ty["weight"]}'))
        want = css_hex(ty.get("fill") or (t.get("type") or {}).get("fill"))
        got = css_hex(el["color"])
        if want and got and want != got:
            bad.append(("colour", f"{got} vs {want}"))
        if bad:
            jump = next((int(v.rstrip("px")) for k, v in bad if k == "gap above"), 0)
            rows.append((abs(jump), el, t, bad))

    # Each section's design range is wherever its own strings live in the
    # frame. Recorded so `diff --section` can cut both sides to the same
    # content instead of the same y, which is what made every band below the
    # first size difference meaningless.
    # Measure the same thing on both sides: the span from the section's first
    # piece of text to its last. The section's own box and the frame's
    # background rectangle are not comparable — one includes padding the other
    # never had — and a delta between them says nothing.
    smap = {}
    for sec in sections:
        mine = [(e, t) for e, t in pairs if e.get("sec") == sec["name"]]
        # A run segment carries its node's y, so it says nothing about where
        # the page put it — it must stay out of the offset median. It still
        # belongs in the extremes: excluding it made the design side count a
        # 419px multi-run node while the page side stopped at its first line,
        # and sh-go read -293px on a panel that matches.
        segs = [(e, t) for e, t in mine if t.get("_seg")]
        mine = [(e, t) for e, t in mine if not t.get("_seg")]
        if len(mine) < 2:
            continue
        # One bad pair ruins the span. A page heading split across two runs
        # ("… Governance Tiers" + "in Singapore") matched a design string 1100px
        # further down the frame, and the section read -197px when its own
        # content was within 30. Every honest pair in a section shares roughly
        # one offset, so keep the ones near the median and say how many went.
        offs = sorted(rect(e, t)[0] - t["y"] for e, t in mine)
        med = offs[len(offs) // 2]
        dev = sorted(abs(o - med) for o in offs)
        tol = max(150.0, 4 * dev[len(dev) // 2])
        keep = [(e, t) for e, t in mine if abs((rect(e, t)[0] - t["y"]) - med) <= tol]
        outliers = len(mine) - len(keep)
        if len(keep) >= 2:
            mine = keep
        mine = mine + segs
        dy0 = min(t["y"] for _, t in mine)
        dy1 = max(t["y"] + (t.get("h") or (t.get("type") or {}).get("lh") or 0)
                  for _, t in mine)
        py0 = min(rect(e, t)[0] for e, t in mine)
        py1 = max(sum(rect(e, t)) for e, t in mine)
        smap[sec["name"]] = {"page_y": sec["y"], "page_box_h": sec["h"],
                             "page_span": round(py1 - py0),
                             "design_y": dy0, "design_span": round(dy1 - dy0),
                             # Same anchor on both sides — the first string in
                             # the section — so this is the honest running
                             # offset, not a per-band best fit.
                             "drift": round(py0 - dy0),
                             "strings": len(mine), "unpaired": outliers}
    if smap and not args.section:
        write_json(os.path.join(out, "sections.json"), smap)
        print(f"{'section':<24}{'design':>8}{'page':>8}{'delta':>8}"
              f"{'drift':>8}{'step':>7}{'strings':>9}{'dropped':>9}")
        tot = 0
        prev_drift = None
        steps = []
        for n, v in sorted(smap.items(), key=lambda kv: kv[1]["page_y"]):
            d = v["page_span"] - v["design_span"]
            tot += d
            step = "" if prev_drift is None else v["drift"] - prev_drift
            if isinstance(step, int) and abs(step) >= 8:
                steps.append((n, step))
            prev_drift = v["drift"]
            print(f"  {n:<22}{v['design_span']:>8}{v['page_span']:>8}{d:>+8}"
                  f"{v['drift']:>+8}"
                  f"{(f'{step:+d}' if isinstance(step, int) else ''):>7}"
                  f"{v['strings']:>9}{v['unpaired'] or '':>9}")
        print(f"  {'(text span, first to last)':<22}{'':>8}{'':>8}{tot:>+8}")
        # `diff` lets every band find its own best vertical fit, which is what
        # makes it readable — and is exactly why it cannot see this. A section
        # that starts in the wrong place scores fine there. Deleting the Go
        # rail took 74px out of the page and left the Figma frame untouched;
        # nothing said so until these two columns existed.
        if steps:
            print("  drift steps between sections — something above changed "
                  "height, not just moved:")
            for n, step in steps:
                print(f"    {n:<22}{step:>+6}px against the section before it")
        print()

    rows.sort(key=lambda r: -r[0])
    for _, el, t, bad in rows:
        print(f'  y={t["y"]:<5} {el["text"][:48]!r}')
        print(f'      .{el["cls"].split()[0] if el["cls"] else "—"}'
              f'{"".ljust(2)}' + " · ".join(f"{k} {v}" for k, v in bad))
    # Ninety-eight lines is a wall nobody reads, and most of them are one
    # mistake repeated. Group by the thing you would actually edit: a class and
    # a property is one fix, however many strings it touches.
    groups = collections.Counter()
    detail = {}
    for _, el, t, bad in rows:
        cls = (el["cls"].split() or ["(no class)"])[0]
        for k, v in bad:
            groups[(cls, k)] += 1
            detail.setdefault((cls, k), v)
    print(f"\n{len(rows)} of {len(pairs)} strings differ — "
          f"{len(groups)} distinct fixes:\n")
    for (cls, k), n in groups.most_common():
        print(f"  {n:>3} ×  .{cls:<24} {k:<12} {detail[(cls, k)]}")
    # ---- boxes -------------------------------------------------------
    # design.json carries every frame's w/h/pad/gap/radius/stroke/rotation and
    # nothing used to read them, so a drawn element had no source of truth but
    # the render — which is how the carousel chevron got built at 22x34 with a
    # 5px stroke when the file says 18x30 with a 3px one. Boxes have no text to
    # pair on, so they pair on geometry and anything outside the tolerance is
    # reported unpaired rather than guessed.
    boxes = payload.get("boxes") or []
    fw = (design.get("source") or {}).get("width") or 0
    cand = [f for f in design.get("frames", [])
            if f.get("w", 0) >= 6 and f.get("h", 0) >= 6
            and (not fw or 0 <= f["x"] < fw)]
    # One design node, one page box. Without this the five 12px dots each
    # claimed the same 6px chevron vector and reported "width 12 vs 6" five
    # times over, which is a pairing failure dressed up as a finding.
    scored = []
    for b in boxes:
        for f in cand:
            sc = (abs(b["x"] - dx - f["x"]) + abs(b["y"] - f["y"])
                  + abs(b["w"] - f["w"]) * 2 + abs(b["h"] - f["h"]) * 2)
            scored.append((sc, id(b), f["id"], b, f))
    scored.sort(key=lambda r: r[0])
    tol = max(args.tolerance, 10)
    taken_b, taken_f, pairs_box = set(), set(), []
    for sc, bid, fid, b, f in scored:
        if bid in taken_b or fid in taken_f:
            continue
        # Size alone is not identity. A 12px dot and a 6px chevron path 600px
        # apart are within any size tolerance and are not the same thing, so
        # the pair has to be near as well as similar.
        if abs(b["w"] - f["w"]) > tol or abs(b["h"] - f["h"]) > tol:
            continue
        if abs(b["x"] - dx - f["x"]) > 60 or abs(b["y"] - f["y"]) > 90:
            continue
        # Nor is "within 10px" identity at small sizes: a 12px dot and a 6px
        # path are twice apart, which is a different thing, not a 6px error.
        if min(b["w"], f["w"]) < 0.6 * max(b["w"], f["w"]) or \
           min(b["h"], f["h"]) < 0.6 * max(b["h"], f["h"]):
            continue
        taken_b.add(bid); taken_f.add(fid); pairs_box.append((b, f))
    unpaired = [b for b in boxes if id(b) not in taken_b]

    # Where a box sits has to be read off the same baseline the text check
    # uses: the section's own drift. Reported raw, every box in a section that
    # sits 7px low says "7px out", and moving the row to satisfy it doubles the
    # error — which is what happened here, a carousel walked 10px further from
    # the frame while every number said it was closing in.
    def dy_base(design_y):
        for v in smap.values():
            if v["design_y"] - 40 <= design_y <= v["design_y"] + v["design_span"] + 40:
                return v.get("drift", 0)
        return 0

    bad_boxes, matched = [], taken_f
    for b, best in pairs_box:
        d = []
        for k, label in (("w", "width"), ("h", "height")):
            if abs(b[k] - best[k]) > args.tolerance:
                d.append((label, f'{b[k]} vs {best[k]}'))
        if abs((b["x"] - dx) - best["x"]) > args.tolerance:
            d.append(("x", f'{b["x"] - dx - best["x"]:+d}px'))
        # y has to come off the same baseline the text check uses. Reported
        # raw, every box in a section that sits 7px low reads "7px out", and
        # moving the row down to satisfy it doubles the error — which is
        # exactly what happened: a carousel row was walked 10px further from
        # the frame while every number said it was getting closer.
        oy = b["y"] - dy_base(best["y"]) - best["y"]
        if abs(oy) > args.tolerance:
            d.append(("y", f"{oy:+d}px"))
        dr = best.get("radius")
        if isinstance(dr, list):
            # Figma records the radii in the node's own space. The workspace
            # band is a rectangle turned 180 degrees, so its [0,0,40,40] reads
            # as 40 on the top corners on screen; comparing element 0 straight
            # across reported "40 vs 0" on two identical bands.
            turns = int(round((best.get("rotation") or 0) / 90.0)) % 4
            for _ in range(turns):
                dr = dr[-1:] + dr[:-1]
            dr = dr[0]
        if dr is not None and not (best.get("type") == "GROUP"
                                   and not best.get("bg")):
            # Anything at or past half the short side is a circle or a pill.
            # The page writes that as 50% and Figma as 100, and reporting
            # "50 vs 100" on two identical circles is noise.
            half = min(b["w"], b["h"]) / 2.0
            raw = str(b.get("radiusRaw") or "")
            pr = (float(raw.rstrip("%")) / 100.0 * min(b["w"], b["h"])
                  if raw.endswith("%") else b["radius"])
            if min(pr, half) - min(dr, half) > 1.5 or \
               min(dr, half) - min(pr, half) > 1.5:
                d.append(("radius", f'{pr:g} vs {dr:g}'))
        dp = best.get("pad")
        if dp and any(abs(b["pad"][i] - dp[i]) > args.tolerance for i in range(4)):
            d.append(("padding", f'{b["pad"]} vs {dp}'))
        # itemSpacing on a frame with one child is a property of Figma's
        # auto-layout, not of anything the page can render: a button wrapping a
        # single label has no gap to get wrong.
        if best.get("gap") and b.get("childGap") is not None:
            pg = b["childGap"]
            if abs(pg - best["gap"]) > args.tolerance:
                d.append(("gap", f'{pg:g} vs {best["gap"]:g}'))
        dsh, psh = best.get("shadow"), b.get("shadow")
        if dsh and not psh:
            e = dsh[0]
            d.append(("shadow", f'none vs {e["x"]} {e["y"]} {e["blur"]} '
                                f'{e["spread"]} {e["color"]}'))
        elif psh and not dsh:
            d.append(("shadow", f'{psh[:34]} vs none'))
        elif dsh and psh:
            m = re.search(r"(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px", psh)
            if m and abs(float(m.group(3)) - dsh[0]["blur"]) > 4:
                d.append(("shadow blur", f'{float(m.group(3)):g} vs {dsh[0]["blur"]}'))
        ds, ps = best.get("stroke"), b.get("stroke")
        if ds and ps and str(ds.get("color", "")).endswith("0.0)"):
            d.append(("border", f'{ps["w"]:g}px {css_hex(ps.get("color"))} '
                                f'vs none (the file\'s stroke is transparent)'))
        elif ds and ps:
            if abs(ps["w"] - ds["weight"]) > 0.6:
                d.append(("stroke", f'{ps["w"]:g}px vs {ds["weight"]:g}px'))
            pc, dc = css_hex(ps.get("color")), css_hex(ds.get("color"))
            if pc and dc and pc != dc:
                d.append(("stroke colour", f'{pc} vs {dc}'))
        elif ds and not ps and not b.get("hasStrokedChild") \
                and not str(ds.get("color", "")).endswith("0.0)"):
            d.append(("stroke", f'none vs {ds["weight"]:g}px {ds.get("color")}'))
        # A rotated VECTOR is a drawing instruction: Figma turns one chevron
        # path 90 and -90 to make left and right. The page draws each path
        # pointing the right way instead, and the rendered result is the same,
        # so only a rotated *box* is worth reporting.
        # A wrapper with no fill and no stroke of its own is not a box that
        # was rotated — it is a container Figma turned to point the artwork the
        # other way, which the page achieves by drawing the other path.
        # Rotating a circle changes nothing about the box, and Figma turns the
        # carousel's right button 180 degrees to reuse the left one.
        dr_any = best.get("radius")
        dr_any = dr_any[0] if isinstance(dr_any, list) else (dr_any or 0)
        round_box = dr_any >= min(best["w"], best["h"]) / 2.0
        drawn_only = round_box or (best.get("type") in
                      ("VECTOR", "BOOLEAN_OPERATION", "STAR", "POLYGON",
                       "LINE", "GROUP", "RECTANGLE")
                      or not (best.get("bg") or best.get("stroke")))
        if (best.get("rotation") and not drawn_only
                and abs((b.get("rot") or 0) - best["rotation"]) > 2):
            d.append(("rotation", f'{b.get("rot") or 0:g} vs {best["rotation"]:g}'))
        if d:
            bad_boxes.append((best, b, d))

    # A frame that pairs gets checked; a frame that is too far wrong to pair
    # used to just vanish, so the worse a box was, the less likely it was to be
    # reported at all. Say which frames found nothing, and how close the nearest
    # thing on the page came — that is where a missing divider or a container
    # 48px short actually shows up.
    # The site header the frame draws is not ours to build, and neither is
    # anything parked beside the frame, so neither can be a missing component.
    top = min((v["design_y"] for v in smap.values()), default=0) if smap else 0
    fw_w2 = (design.get("source") or {}).get("width") or 10 ** 6
    seen_geo = set()
    orphans = []
    for f in cand:
        if id(f) in matched:
            continue
        if (f.get("w") or 0) < 60 or (f.get("h") or 0) < 20:
            continue
        if f["y"] + f["h"] < top - 8 or not (0 <= f["x"] < fw_w2):
            continue
        # A text node is copy, not a component. The page splits one Figma text
        # node into a heading and a paragraph all the time, and reporting the
        # node as a missing box says nothing anyone can act on.
        if f.get("type") == "TEXT":
            continue
        # An invisible frame cannot be missing: there is nothing to see. Figma
        # groups everything — a content column, a list wrapper — and none of
        # those has a fill, a stroke, a radius or a shadow. The page side only
        # collects boxes that have one of those, so this is the same rule on
        # both sides rather than a threshold guessed for one of them.
        if not any(f.get(k) for k in ("bg", "stroke", "radius", "shadow")):
            continue
        # A frame and the wrapper drawn on top of it are one component.
        key = (f["x"] // 8, f["y"] // 8, f["w"] // 8, f["h"] // 8)
        if key in seen_geo:
            continue
        seen_geo.add(key)
        near, nd = None, 1e9
        for b in boxes:
            dd = abs(b["x"] - dx - f["x"]) + abs(b["y"] - f["y"])
            if dd < nd:
                near, nd = b, dd
        why = []
        if near:
            if abs(near["w"] - f["w"]) > tol: why.append(f'width {near["w"]} vs {f["w"]}')
            if abs(near["h"] - f["h"]) > tol: why.append(f'height {near["h"]} vs {f["h"]}')
            if abs(near["x"] - dx - f["x"]) > 60: why.append(f'x {near["x"] - dx} vs {f["x"]}')
            if abs(near["y"] - f["y"]) > 90: why.append(f'y {near["y"]} vs {f["y"]}')
        orphans.append((f, near, why))

    print(f"\nboxes     {len(boxes)} on the page against "
          f"{len(cand)} frames in the file — {len(matched)} paired, "
          f"{len(unpaired)} page boxes with no frame, "
          f"{len(orphans)} frames with nothing on the page\n")
    for f, b, d in sorted(bad_boxes, key=lambda r: r[0]["y"])[:24]:
        who = ("." + b["cls"].split(" ")[0]) if b["cls"] else b["tag"]
        print(f'  y={f["y"]:<6}{f["name"][:26]!r}  ->  {who}')
        print("      " + " · ".join(f"{k} {v}" for k, v in d))
    if not bad_boxes:
        print("  every paired box matches the file")

    if orphans:
        print(f"\n  {len(orphans)} frame(s) the page has nothing for — the nearest "
              f"box and why it did not pair:")
        for f, near, why in sorted(orphans,
                                   key=lambda r: -(r[0]["w"] * r[0]["h"]))[:14]:
            who = (("." + near["cls"].split(" ")[0]) if near and near.get("cls")
                   else (near["tag"] if near else "—"))
            print(f'  y={f["y"]:<6}{str(f["name"])[:26]!r}  {f["w"]}x{f["h"]}  '
                  f'nearest {who}')
            if why:
                print("      " + " · ".join(why))

    # Hand the box findings to the section gate. `match` is a pixel measure and
    # a container 13px too tall barely moves it, so a section could gate at 91
    # with three cards the wrong height and nothing would say so.
    if smap and not args.section:
        def which(y):
            for n_, v in smap.items():
                if v["design_y"] - 40 <= y <= v["design_y"] + v["design_span"] + 40:
                    return n_
            return None
        for v in smap.values():
            v["box_bad"], v["box_orphan"] = 0, 0
        # Only geometry gates. padding and gap describe how a box is built, and
        # two different structures render identically: the frame nests a card
        # with 28 of padding inside a wrapper that adds 13, the page puts 41 on
        # the card, and the text lands on the same pixel. Where it is and how
        # big it is are the questions with one right answer.
        geom = {"x", "y", "width", "height"}
        for f, _b, dd in bad_boxes:
            k = which(f["y"])
            if k and any(lbl in geom for lbl, _v in dd):
                smap[k]["box_bad"] += 1
        # Figma wraps everything — a Margin round a Background round an icon —
        # and none of those wrappers has, or should have, a CSS box. Only count
        # a frame the page really ought to have built: a card, a band, a panel.
        # Pairing is one to one and greedy, so three identical cards leave two
        # frames "unpaired" even though the page built all three. If a box of
        # this size and shape exists anywhere in the section, the page did build
        # the thing; the matcher just handed it to a sibling.
        for f, _n, why in orphans:
            k = which(f["y"])
            if not (k and why and f["w"] >= 200 and f["h"] >= 60):
                continue
            twin = any(abs(b["w"] - f["w"]) <= 6 and abs(b["h"] - f["h"]) <= 6
                       and b.get("sec") == k for b in boxes)
            if not twin:
                smap[k]["box_orphan"] += 1
        write_json(os.path.join(out, "sections.json"), smap)

    print(f"\n(tolerance {args.tolerance}px; position is only checked where the "
          f"string is unique on both sides; boxes pair on geometry, and one "
          f"outside the tolerance is reported unpaired rather than guessed)")
    sys.exit(1 if rows or bad_boxes else 0)


def site_behaviour_js(out, refresh=False):
    """The site's own wpbuddy-page.js, cached beside the build.

    `preview` has always written `<script src="wpbuddy-page.js">` into the
    page and nothing ever fetched the file, so every preview of an
    interactive page 404'd that tag and ran dead. Tabs did not switch,
    accordions did not open, carousels did not move — the exact symptom
    site-rules tells you to blame on production, while the cause was local.
    Worse, it is silent: the render still looks right, because a carousel at
    rest looks like a carousel.

    The URL is discovered from the site's own home page rather than hardcoded,
    since the plugin folder has been renamed at least once.
    """
    dst = os.path.join(out, "wpbuddy-page.js")
    if not refresh and os.path.exists(dst):
        return dst
    base = env("WP_BASE_URL", "").rstrip("/")
    if not base:
        return None
    def get(url):
        return urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=60).read()

    # The enqueue is content-gated on class="ptab / acc-item / acc-head, so
    # the home page usually does NOT carry the tag. Scrape this build's own
    # live page first when it has one, then the home page, then ask the
    # plugin directory directly — the folder has been renamed once already,
    # hence a list rather than a constant.
    pages = []
    wp = os.path.join(out, "wp.json")
    if os.path.exists(wp):
        link = (json.load(open(wp)) or {}).get("link")
        if link:
            pages.append(link)
    pages.append(base + "/")
    url = None
    for page in pages:
        try:
            html = get(page).decode("utf-8", "replace")
        except Exception:
            continue
        m = re.search(r'src=["\']([^"\']*wpbuddy-page\.js[^"\']*)["\']', html)
        if m:
            url = urllib.parse.urljoin(page, m.group(1))
            break
    if not url:
        for folder in ("wp-buddy", "wpbuddy", "mc-wpbuddy"):
            probe = f"{base}/wp-content/plugins/{folder}/assets/wpbuddy-page.js"
            try:
                if b"wpbuddy-page" in get(probe)[:4000]:
                    url = probe
                    break
            except Exception:
                continue
    if not url:
        sys.stderr.write("site js: wpbuddy-page.js not found on the site; the "
                         "preview cannot run tabs, accordions or carousels\n")
        return None
    try:
        js = get(url)
    except Exception as e:
        sys.stderr.write(f"site js: could not fetch {url}: {e}\n")
        return None
    with open(dst, "wb") as fh:
        fh.write(js)
    print(f"behaviour {len(js)//1024}KB of the site's wpbuddy-page.js cached "
          f"-> {dst}")
    return dst


def site_shell(out, refresh=False):
    """The live page's own CSS, body classes and wrappers, cached locally.

    Without this the preview is a bare .mc-page on an empty body, so every
    rule the site puts on the page is invisible while you build. It shipped a
    heading 10px lower than the preview showed, because the theme pads every
    h1-h6 and nothing in the build ever saw that rule.
    """
    d = os.path.join(out, "site")
    css, meta = os.path.join(d, "site.css"), os.path.join(d, "shell.json")
    if not refresh and os.path.exists(css) and os.path.exists(meta):
        return open(css, encoding="utf-8").read(), json.load(open(meta))
    wp = os.path.join(out, "wp.json")
    if not os.path.exists(wp):
        return "", {}
    link = (json.load(open(wp)) or {}).get("link")
    if not link:
        return "", {}
    try:
        req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    except Exception as e:
        sys.stderr.write(f"site shell: could not fetch {link}: {e}\n")
        return "", {}
    os.makedirs(d, exist_ok=True)
    parts = []
    for tag in re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]*>', html):
        m = re.search(r'href=["\']([^"\']+)["\']', tag)
        if not m:
            continue
        href = urllib.parse.urljoin(link, m.group(1))
        try:
            parts.append(urllib.request.urlopen(
                urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=60).read().decode("utf-8", "replace"))
        except Exception as e:
            sys.stderr.write(f"site shell: {href}: {e}\n")
    parts += re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    sheet = "\n".join(parts)
    # url() is relative to wherever the sheet lived; make it absolute so the
    # preview does not silently drop backgrounds the site actually draws.
    sheet = re.sub(r'url\(\s*["\']?(?!data:|https?:|//)([^"\')]+)["\']?\s*\)',
                   lambda m: f'url("{urllib.parse.urljoin(link, m.group(1))}")', sheet)
    shell = {}
    b = re.search(r'<body[^>]*class="([^"]*)"', html)
    if b:
        shell["body_class"] = b.group(1)
    i = html.find('class="mc-page')
    wraps = []
    if i > 0:
        for m in re.finditer(r'<(main|div|article)[^>]*>', html[:i]):
            t = m.group(0)
            if any(k in t for k in ("site-main", "page-content",
                                    "entry-content", "content-area")):
                wraps.append(t)
    shell["wrappers"] = wraps
    with open(css, "w", encoding="utf-8") as fh:
        fh.write(sheet)
    write_json(meta, shell)
    print(f"site      {len(sheet)//1024}KB of the live page's CSS cached "
          f"({len(wraps)} wrappers) -> {css}")
    return sheet, shell


def local_font_css(out):
    """@font-face for a face shipped in the build, so a measurement never
    depends on a network fetch that headless Chrome does not make."""
    f = os.path.join(out, "assets", "fonts", "raleway.woff2")
    if not os.path.exists(f):
        return ""
    return ("<style>@font-face{font-family:'Raleway';"
            "src:url('assets/fonts/raleway.woff2') format('woff2');"
            "font-weight:100 900;font-style:normal;font-display:block}</style>")


MOBILE_WRAP = """<!doctype html><meta charset="utf-8"><title>mobile</title>
<style>html,body{margin:0;background:#222}iframe{border:0;display:block}</style>
<iframe id="fw" src="preview.html" width="__W__" height="__H__"></iframe>
<script>
// Chrome's headless window will not go below 500px wide, so --window-size
// cannot give a phone viewport: asking for 390 measured 500 and every media
// query below that breakpoint stayed shut. An iframe can be any width, and a
// media query inside it evaluates against the iframe. --dump-dom prints the
// top document only, so republish what the frame sends up.
addEventListener('message', function (e) {
  if (!e.data || !e.data.fwMeasure) { return; }
  var s = document.createElement('script');
  s.type = 'application/json'; s.id = 'fw-measure';
  s.textContent = e.data.fwMeasure;
  document.body.appendChild(s);
});
</script>"""


def cmd_mobile(args):
    """What only goes wrong on a phone.

    None of these pages has a mobile frame in Figma, so there is no reference
    to diff against and `audit` has nothing to say. These four questions are
    what was being retyped into a browser console once per page instead:
    does anything stick out sideways, is any body copy under 16px, is any tap
    target under 40, and do all the blocks share one gutter. The house answers
    come from the rest of the site, not from the design file — see
    references/site-rules.md.
    """
    out = os.path.join(BUILD, args.slug)
    if not os.path.isdir(out):
        die("MISSING", out)
    url = args.url.rstrip("/")
    base = url.rsplit("/", 1)[0] if url.endswith(".html") else url
    wrap = MOBILE_WRAP.replace("__W__", str(args.width)).replace("__H__", str(args.height))
    with open(os.path.join(out, "mobile.html"), "w", encoding="utf-8") as fh:
        fh.write(wrap)
    dom = dump_dom(base + "/mobile.html", width=max(args.width + 120, 800))
    m = re.search(r'<script type="application/json" id="fw-measure">(.*?)</script>',
                  dom, re.S)
    if not m:
        die("NO_MEASURE", "the frame never reported. Is the preview server "
                          "running, and is measure.js loading?")
    mob = json.loads(m.group(1)).get("mobile")
    if not mob:
        die("NO_MOBILE", "measure.js is older than this command.")

    # The phone rules restate desktop values — line-heights above all, since
    # every one of them is an absolute px taken from the frame. Writing them
    # against a desktop that is still moving means writing them twice. Say so
    # rather than let it happen quietly.
    st = os.path.join(out, "diff", "state.json")
    if os.path.exists(st):
        try:
            d0 = json.load(open(st, encoding="utf-8"))
        except Exception:
            d0 = {}
        n_un = len(d0.get("unexplained") or [])
        if n_un or (d0.get("match") or 0) < 0.80:
            print(f"desktop   not settled yet — match {d0.get('match', 0):.1%}"
                  f", {n_un} unexplained band(s).")
            print("          Every rule you write here restates a desktop value,"
                  " so finish the")
            print("          desktop first and have it confirmed. Reading the "
                  "phone now is fine;")
            print("          writing the phone now means writing it twice.\n")
    else:
        print("desktop   no diff has been run — settle the desktop first.\n")

    print(f"width     {mob['vw']}px  (asked for {args.width})")
    if mob["vw"] != args.width:
        print("          the frame did not take the width — check MOBILE_WRAP")
    bad = 0

    over = mob["over"]
    if over:
        bad += 1
        print(f"overflow  {mob['scrollW']} > {mob['vw']} — {len(over)} element(s) "
              f"stick out sideways")
        for o_ in over[:8]:
            print(f"          .{o_['cls'][:40]:<40} x{o_['x']} → {o_['r']}")
    else:
        print(f"overflow  none (scrollWidth {mob['scrollW']})")

    small = mob["small"]
    if small:
        bad += 1
        print(f"under 16  {len(small)} text element(s) — the site holds 16px as "
              f"its phone minimum for body copy")
        for t in small[:8]:
            print(f"          {t['fs']:<6} .{t['cls'][:30]:<30} {t['txt']!r}")
    else:
        print("under 16  none")

    tap = mob["tap"]
    if tap:
        bad += 1
        print(f"tap < 40  {len(tap)} link(s)/button(s)")
        for t in tap[:8]:
            print(f"          {t['box']:<10} .{t['cls'][:28]:<28} {t['txt']!r}")
    else:
        print("tap < 40  none")

    lefts = sorted(((int(k), v) for k, v in mob["lefts"].items()),
                   key=lambda kv: -kv[1])
    # Nested cards indent on purpose, so "one gutter for everything" is not a
    # rule and reporting every inner offset is noise. What is a rule: nothing
    # sits against the edge of the screen.
    if lefts:
        print("gutter    " + "  ".join(f"{x}px×{n_}" for x, n_ in lefts[:4]))
        jammed = [(x, n_) for x, n_ in lefts if x < 12]
        if jammed:
            bad += 1
            print("          content against the edge: "
                  + ", ".join(f"{x}px × {n_}" for x, n_ in jammed[:4]))

    # A square box is only wrong when the file is not square — three 60x60
    # icons drawn 60x60 are fine, a 467x67 wordmark drawn 240x240 is not.
    def _sq(b):
        a, c = b.split("x")
        return a == c
    odd = [i for i in mob["imgs"]
           if i["nat"] == "1x1" or (_sq(i["box"]) and not _sq(i["nat"]))]
    if odd:
        bad += 1
        print(f"images    {len(odd)} rendering square or off a 1x1 placeholder — "
              f"give them aspect-ratio")
        for i in odd[:6]:
            print(f"          .{i['cls'][:30]:<30} nat {i['nat']} box {i['box']}")

    print()
    print("0 findings" if not bad else f"{bad} kind(s) of finding above")
    return bad


def cmd_preview(args):
    import functools
    import http.server

    root = os.path.join(BUILD, args.slug)
    if not os.path.isdir(root):
        die("MISSING", root)

    # Stamp every asset URL with its file's mtime. no-store on the response is
    # not enough on its own: a browser will keep serving an image it already
    # holds in its in-memory cache for the same URL, so you sit there looking
    # at the previous export wondering why the fix did nothing.
    def bust(m):
        rel = m.group(1)
        f = os.path.join(root, rel)
        v = int(os.path.getmtime(f)) if os.path.exists(f) else 0
        return f'src="{rel}?v={v}"'

    def build_page():
        page = (assemble(root).replace("<!-- wp:html -->", "")
                              .replace("<!-- /wp:html -->", ""))
        js = page_js(root)
        if js:
            page += "\n<script>\n" + js + "\n</script>\n"
        # WordPress turns [wpb_hs_form ...] into a button; the preview server
        # cannot, so the shortcode sat in the page as a 51px block of literal
        # text and every measurement below it was taken against a layout no
        # visitor sees. Stand in something the same shape.
        page = re.sub(
            r'\[wpb_hs_form[^\]]*?text="([^"]*)"[^\]]*\]',
            lambda m: ('<div class="wpbhs-wrap"><button type="button" '
                       'class="wpbhs-open" data-preview-stub="1">'
                       + m.group(1) + "</button></div>"),
            page)
        # Freeze the carousels. Both the card row and the product switcher
        # advance on a timer, so a screenshot catches whichever slide happened
        # to be up and every comparison of those two sections was measuring
        # different content. The design draws the first one; hold it there.
        page = re.sub(r'\sdata-(loop-ms|tabs-loop)="[^"]*"', "", page)
        # The frame draws every accordion open — that is how a static design
        # shows its content, not the behaviour the file's own comment asks for
        # ("click a title to open, the previous one closes"). Open them for the
        # measurement only, so the two sides describe the same content; the
        # page keeps shipping one-at-a-time.
        page = re.sub(r'(<div class="acc-item)(")', r'\1 open\2', page)
        return re.sub(r'src="(assets/[^"?]+)"', bust, page)

    # Claim the port first. Writing preview.html before binding means a second
    # run that dies on "address already in use" has still overwritten the file
    # the *first* server is serving — the page appears to update, and a diff
    # against it compares new markup with an old server's idea of the assets.
    handler_dir = root
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), None)
    except OSError as e:
        die("PORT_IN_USE", f"127.0.0.1:{args.port} is taken ({e}). preview.html "
                           f"was NOT rewritten, so whatever is already serving "
                           f"there is still serving the old build. Stop it, or "
                           f"pass --port.")

    site_css, shell, site_js = ("", {}, None)
    if not getattr(args, "bare", False):
        refresh = getattr(args, "refresh_site", False)
        site_css, shell = site_shell(root, refresh=refresh)
        site_js = site_behaviour_js(root, refresh=refresh)
    body_cls = shell.get("body_class", "")
    wraps = shell.get("wrappers") or []
    open_wraps = "".join(wraps)
    close_wraps = "".join("</" + re.match(r"<(\w+)", w).group(1) + ">"
                          for w in reversed(wraps))

    def write_preview():
        with open(os.path.join(root, "preview.html"), "w", encoding="utf-8") as fh:
            fh.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width,initial-scale=1">'
                     f"<title>{args.slug} — preview</title>"
                     '<link href="https://fonts.googleapis.com/css2?family=Raleway:'
                     'wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;700'
                     '&display=swap" rel="stylesheet">'
                     # Headless Chrome never receives the Google Fonts file —
                     # document.fonts.check reported every weight of Raleway
                     # unloaded — so it measured a wider fallback, and boxes
                     # were being widened to compensate for a font that was
                     # never missing on the site itself. Serve the face out of
                     # the build when it is there.
                     + (f'<link rel="stylesheet" href="site/site.css">'
                        if site_css else "")
                     + local_font_css(root) +
                     "<style>body{margin:0}</style></head>"
                     f'<body class="{body_cls}">' + open_wraps + build_page() +
                     close_wraps + MEASURE_JS +
                     # Only when the file is actually there. A tag pointing at
                     # nothing is worse than no tag: it looks like the page
                     # has its behaviour.
                     ('<script src="wpbuddy-page.js"></script>'
                      if site_js else
                      '<!-- no wpbuddy-page.js cached: tabs, accordions and '
                      'carousels will not respond in this preview -->') +
                     '</body></html>')

    write_preview()

    class Handler(http.server.SimpleHTTPRequestHandler):
        """Say charset=utf-8 out loud. SimpleHTTPRequestHandler sends bare
        `text/html`, and a viewer that does not read the <meta> tag — an IDE
        preview pane, for one — then guesses Latin-1 and mangles every dash."""

        def do_GET(self):
            # Reassemble before serving. The server used to template
            # preview.html once at startup, so editing page.css and re-running
            # `audit` measured the markup from whenever the server was started
            # — the deltas came back byte-identical and read as "the fix did
            # nothing". Same failure as a stale design.png, one layer down.
            if self.path.split("?")[0].rstrip("/") in ("/preview.html", ""):
                try:
                    write_preview()
                except Exception as e:      # keep serving the last good build
                    sys.stderr.write(f"preview: rebuild failed: {e}\n")
            return super().do_GET()

        def end_headers(self):
            # No caching. Assets are replaced constantly while a page is being
            # matched to its design, and a cached image silently shows you the
            # previous attempt — which reads as "the fix did not work".
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

        def guess_type(self, path):
            t = super().guess_type(path)
            if t in ("text/html", "text/css", "application/javascript",
                     "text/javascript", "application/json"):
                return t + "; charset=utf-8"
            return t

    # Threading matters: a browser opens several connections at once for the
    # CSS and images, and a single-threaded server stalls on the first one,
    # so the page renders half-styled and looks broken.
    httpd.RequestHandlerClass = functools.partial(Handler, directory=handler_dir)
    with httpd:
        print(f"http://127.0.0.1:{args.port}/preview.html   (ctrl-c to stop)")
        httpd.serve_forever()


# ------------------------------------------------------------------ wordpress

def wp_creds():
    base = env("WP_BASE_URL", "https://masterconcept.ai").rstrip("/")
    user, pw = env("WP_USER"), env("WP_APP_PASSWORD")
    if not user or not pw:
        die("NO_WP_CREDS", f"put WP_USER and WP_APP_PASSWORD (an Application "
                           f"Password, not the login password) in {HOME}/.env")
    return base, user, pw


def wp(method, path, *, body=None, raw=None, headers=None, creds=None):
    base, user, pw = creds or wp_creds()
    url = path if path.startswith("http") else base + path
    hdrs = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
            "User-Agent": "figma-to-wp"}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers=hdrs, method=method),
                timeout=300) as r:
            txt = r.read().decode("utf-8", "replace")
            return json.loads(txt) if txt.strip()[:1] in "{[" else txt
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code in (401, 403):
            die("WP_UNAUTHORIZED", f"{method} {url} -> {detail}")
        if e.code == 406 or "ModSecurity" in detail or detail.lstrip()[:5].lower() == "<html":
            die("WP_WAF_BLOCKED",
                f"{method} {url} was blocked before it reached WordPress "
                "(SiteGround WAF). Shrink the file or convert it to WebP, then retry.")
        die(f"WP_HTTP_{e.code}", f"{method} {url} -> {detail}")
    except urllib.error.URLError as e:
        die("NETWORK_ERROR", f"{method} {url} -> {e.reason}")


def ability(name, payload, creds=None):
    return wp("POST", f"/wp-json/wp-abilities/v1/abilities/{name}/run",
              body={"input": payload}, creds=creds)


def cmd_setup(args):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    tok = env("FIGMA_TOKEN") or die("NO_FIGMA_TOKEN", f"put FIGMA_TOKEN in {HOME}/.env")
    me = figma_get("/v1/me", tok)
    print(f"figma     ok — {me.get('email') or me.get('handle')}")
    creds = wp_creds()
    u = wp("GET", "/wp-json/wp/v2/users/me?context=edit", creds=creds)
    caps = (u or {}).get("capabilities") or {}
    print(f"wordpress ok — {u.get('name')}  edit_pages={bool(caps.get('edit_pages'))}")
    if not caps.get("edit_pages"):
        print("  WARNING: this user cannot edit pages; push will fail.")


def cmd_doctor(args):
    """Credentials AND the local pieces the method depends on.

    Checking only the two APIs is what let a machine with no Pillow report
    three green lines and then hand back an uncropped canvas as the design
    reference, silently. Everything below either works or is named.
    """
    ok = True

    # measure.js is JavaScript, and until it moved out of a Python string
    # nothing checked it: a call to a helper that did not exist parsed fine and
    # surfaced as "no fw-measure node in the DOM" — which reads like a browser
    # problem, not a typo. Check it here if node is around.
    mjs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "measure.js")
    if have("node") and os.path.exists(mjs):
        r = subprocess.run(["node", "--check", mjs], capture_output=True)
        if r.returncode:
            print("measure.js syntax error — audit and mobile will report nothing:")
            print(r.stderr.decode("utf-8", "replace").strip()[:400])
        else:
            print(f"{'measure.js':<11} ok  (node --check)")
    elif not os.path.exists(mjs):
        print(f"{'measure.js':<11} MISSING next to the script — audit cannot measure")

    def line(label, value, fatal=True, hint=""):
        nonlocal ok
        if value:
            print(f"{label:<11} ok  ({value})")
        else:
            ok = ok and not fatal
            print(f"{label:<11} {'MISSING' if fatal else 'absent'}  {hint}",
                  file=sys.stderr)

    # Local first: it costs nothing and a failure here explains the rest.
    v = sys.version_info
    line("python", f"{v.major}.{v.minor}.{v.micro}" if v >= (3, 9) else "",
         hint=f"3.9+ required, this is {v.major}.{v.minor}")
    try:
        import PIL
        pillow = PIL.__version__
    except ImportError:
        pillow = ""
    # Not optional in practice: extract crops the canvas render with it, and
    # without it design.png is the whole canvas — wrong, and it does not say so
    # loudly enough to stop anyone building against it.
    line("pillow", pillow, hint="pip3 install Pillow — extract cannot crop the "
                               "canvas render and diff cannot run without it")
    chrome = next((c for c in CHROME if os.path.exists(c)), None)
    line("chrome", os.path.basename(chrome) if chrome else "",
         hint="install Chrome or Chromium — diff has no other way to render "
              "the page")
    line("cwebp", "yes" if have("cwebp") else "", fatal=False,
         hint="(optional: Pillow already covers WebP)")
    line("poppler", "yes" if have("pdftotext") else "", fatal=False,
         hint="(optional: only needed to import from a PDF export)")

    for label, fn in (
        ("figma", lambda: figma_get("/v1/me", env("FIGMA_TOKEN")).get("handle")),
        ("wordpress", lambda: wp("GET", "/wp-json/wp/v2/users/me?context=edit").get("name")),
        ("abilities", lambda: len(wp("GET", "/wp-json/wp-abilities/v1/abilities") or [])),
    ):
        try:
            print(f"{label:<11} ok  ({fn()})")
        except SystemExit:
            ok = False
    sys.exit(0 if ok else 1)



def to_webp(path, quality=85):
    """PNG/JPEG -> WebP, if it actually comes out smaller. Returns the path to
    upload. Mobile PageSpeed on this site is sensitive to image weight."""
    if not path.lower().endswith((".png", ".jpg", ".jpeg")):
        return path
    dest = os.path.splitext(path)[0] + ".webp"
    if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(path):
        return dest if os.path.getsize(dest) < os.path.getsize(path) else path
    ok = False
    try:
        from PIL import Image
        im = Image.open(path)
        im.save(dest, "WEBP", quality=quality, method=6)
        ok = True
    except ImportError:
        if have("cwebp"):
            ok = run(["cwebp", "-quiet", "-q", str(quality), path, "-o", dest]).returncode == 0
    except Exception as e:                                   # noqa: BLE001
        print(f"  webp failed for {os.path.basename(path)}: {e}", file=sys.stderr)
    if not ok or not os.path.exists(dest):
        return path
    if os.path.getsize(dest) >= os.path.getsize(path):
        os.remove(dest)
        return path
    return dest


def resolve_parent(path, creds):
    """'partners/work-collaboration' -> the id of the deepest page on that path.

    Walking it segment by segment matters: slugs are only unique among
    siblings, so looking up the last segment alone can land on the wrong page.
    """
    parent = 0
    for seg in [s for s in path.strip("/").split("/") if s]:
        hits = wp("GET", f"/wp-json/wp/v2/pages?slug={urllib.parse.quote(seg)}"
                         f"&status=any&per_page=20&_fields=id,slug,parent", creds=creds)
        match = next((p for p in hits if p["parent"] == parent), None)
        if not match:
            die("PARENT_NOT_FOUND",
                f"no page {seg!r} under parent {parent} (path {path!r})")
        parent = match["id"]
    return parent


def wp_meta_path(out):
    return os.path.join(out, "wp.json")


def read_wp_meta(out):
    p = wp_meta_path(out)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def write_wp_meta(out, meta):
    with open(wp_meta_path(out), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)


def find_page(target, creds):
    """Resolve what a person actually typed to one page.

    Nobody remembers a post id; they read a title off the Pages list. But the
    REST `search` matches post CONTENT too and does not rank by title, so
    searching "Asana" returns the Superhuman page first — quietly editing the
    wrong page is the worst outcome this tool has. Titles are therefore matched
    against post_title only, and anything ambiguous is handed back to be
    chosen, never guessed.
    """
    t = str(target).strip()
    q = "/wp-json/wp/v2/pages?status=any&per_page=30&_fields=id,slug,title,status,link,parent,modified_gmt"

    if t.isdigit():
        one = wp("GET", f"/wp-json/wp/v2/pages/{t}?context=edit&_fields="
                         f"id,slug,title,status,link,parent,modified_gmt", creds=creds)
        return [one] if one.get("id") else []

    slug = t
    if t.startswith("http"):
        slug = [p for p in urllib.parse.urlparse(t).path.split("/") if p][-1:]
        slug = slug[0] if slug else ""
    hits = wp("GET", q + f"&slug={urllib.parse.quote(slug)}", creds=creds) or []
    if hits:
        return hits

    # a title: search post_title only, then keep exact matches if there are any
    hits = wp("GET", q + f"&search={urllib.parse.quote(t)}"
                         f"&search_columns[]=post_title", creds=creds) or []
    exact = [h for h in hits
             if h["title"]["rendered"].strip().lower() == t.lower()]
    return exact or hits


def cmd_pull(args):
    """Bring a page that already exists in WordPress down to a build folder.

    Everything else here starts at Figma, which means a page nobody has a
    build/ for — someone else's, one edited in the admin, one from before this
    tool — cannot be touched at all. Reconstructing one by hand from the live
    body and a manifest is possible; it is also an hour nobody should spend
    twice.
    """
    creds = wp_creds()
    hits = find_page(args.target, creds)
    if not hits:
        die("NO_SUCH_PAGE", f"nothing matches {args.target!r}. Pass the post id, "
                            f"the URL, the slug, or the exact title.")
    if len(hits) > 1:
        print(f"{args.target!r} matches {len(hits)} pages — say which:",
              file=sys.stderr)
        for i, h in enumerate(hits, 1):
            print(f"  {i}  #{h['id']:<7} {h['status']:<8} "
                  f"{h['title']['rendered'][:44]:<46} /{h['slug']}  "
                  f"edited {h['modified_gmt'][:10]}", file=sys.stderr)
        die("AMBIGUOUS", "re-run with the post id.")

    page = wp("GET", f"/wp-json/wp/v2/pages/{hits[0]['id']}?context=edit", creds=creds)
    body = page["content"]["raw"]
    slug = args.slug or page["slug"]
    out = os.path.join(BUILD, slug)
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)

    print(f"page      #{page['id']} {page['title']['raw'][:44]!r} "
          f"({page['status']})")
    print(f"body      {len(body)} bytes from WordPress")

    # Elementor keeps its layout in post meta, not in the body, so what comes
    # back is a handful of shortcodes that cannot be edited here. Say so rather
    # than writing a build folder that looks usable and is not.
    if re.search(r"\[/?elementor", body) or (len(body) < 400 and "[" in body):
        print("page      this looks like an Elementor page — its layout is not "
              "in the body, so there is nothing here to edit. Stopping.",
              file=sys.stderr)
        die("ELEMENTOR_PAGE", "pull only works on pages whose HTML is the body.")

    styles = re.findall(r"<style[^>]*>.*?</style>", body, re.S)
    css = "\n\n".join(re.sub(r"</?style[^>]*>", "", x).strip() for x in styles)
    for x in styles:
        body = body.replace(x, "{{styles}}" if x is styles[0] else "", 1)
    if styles:
        with open(os.path.join(out, "page.css"), "w", encoding="utf-8") as fh:
            fh.write(css + "\n")
        print(f"page.css  {len(css)} bytes from {len(styles)} <style> block(s)")

    # Point the markup back at local files and record the mapping, so the next
    # push reuses the media already in the library instead of uploading it all
    # again under -1, -2, -3 names.
    manifest = read_manifest(out)
    urls = sorted(set(re.findall(r'(?:src|poster)="(' + re.escape(creds[0])
                                 + r'/wp-content/uploads/[^"]+)"', body)))
    for u in urls:
        name = u.rsplit("/", 1)[-1]
        rel = "assets/" + re.sub(rf"^{re.escape(slug)}-", "", name)
        local = os.path.join(out, rel)
        if not os.path.exists(local):
            download(u, local)
        manifest[rel] = {"id": manifest.get(rel, {}).get("id"), "url": u,
                         "sha": hashlib.sha256(open(local, "rb").read()).hexdigest()}
        body = body.replace(f'"{u}"', f'"{rel}"')
    if urls:
        with open(os.path.join(out, "manifest.json"), "w") as fh:
            json.dump(manifest, fh, indent=1)
        print(f"assets    {len(urls)} downloaded and rewritten to relative paths")

    with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as fh:
        fh.write(body if body.endswith("\n") else body + "\n")
    print(f"page.html {len(body)} bytes")

    write_wp_meta(out, {"post_id": page["id"], "slug": page["slug"],
                        "title": page["title"]["raw"], "parent": page.get("parent"),
                        "link": page["link"], "status": page["status"],
                        "modified_gmt": page["modified_gmt"],
                        "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime())})
    print(f"wp.json   #{page['id']}, edited {page['modified_gmt']} — push will "
          f"use this, and refuse if WordPress moves ahead of it")
    print(f"\nbuild/{slug}/ is ready. This is the page as WordPress serves it, "
          f"not the original source: shortcodes are raw, asset names come from "
          f"the media library.")


def read_manifest(out):
    p = os.path.join(out, "manifest.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def cmd_push(args):
    # The build directory name and the page's URL slug are different things:
    # you want to try a design at a throwaway URL before it takes the real one.
    # For a page that already exists the build name must NOT decide the slug:
    # pulling a page into build/anything and pushing it back renamed a live
    # URL to "anything" and 404'd the real one. Only --page-slug may rename.
    out = os.path.join(BUILD, args.slug)
    creds = wp_creds()
    parent_id = args.parent
    if args.parent_path:
        parent_id = resolve_parent(args.parent_path, creds)
        print(f"parent    {args.parent_path} -> #{parent_id}")
    html = assemble(out)

    # The design file is the authority on what the page says. A verify error
    # means the page and the file disagree, and pushing anyway is how copy
    # nobody wrote and nobody approved reaches the site — it happened, and the
    # FAIL line was right there in the output at the time. Anything genuinely
    # meant to be left out belongs in dropped.json with the reason, which is
    # reviewable; --force is not.
    if os.path.exists(os.path.join(out, "design.json")):
        errs, _ = run_checks(out)
        if errs and not args.force:
            print(file=sys.stderr)
            for e in errs:
                print(f"  FAIL  {e}", file=sys.stderr)
            die("VERIFY_FAILED",
                f"{len(errs)} check(s) failed, so nothing was pushed. Fix the "
                f"page, or record a deliberate omission in "
                f"build/{args.slug}/dropped.json with the reason. --force "
                f"pushes anyway and leaves no record that it did.")

    # Check the page has not moved on BEFORE uploading anything. Finding out
    # afterwards leaves new attachments in the media library that this run
    # will never reference and nobody will ever clean up.
    meta = read_wp_meta(out)
    post_id = args.post_id or meta.get("post_id")
    if post_id:
        if not args.post_id:
            print(f"page      #{post_id} from wp.json")
        cur = wp("GET", f"/wp-json/wp/v2/pages/{post_id}?context=edit", creds=creds)

        # Renaming a published page breaks every link to it and leaves no
        # redirect behind. Never do it as a side effect of a folder name.
        if args.page_slug and args.page_slug != cur.get("slug") \
                and cur.get("status") == "publish" and not args.force:
            die("WOULD_RENAME_LIVE_PAGE",
                f"#{post_id} is published at /{cur.get('slug')}/ and "
                f"--page-slug would move it to /{args.page_slug}/. Every "
                f"existing link would 404 and no redirect is created. Pass "
                f"--force if that is genuinely what you want.")

        seen, now = meta.get("modified_gmt"), cur.get("modified_gmt")
        if seen and now and now != seen and not args.force:
            die("PAGE_MOVED_ON",
                f"#{post_id} was edited in WordPress after this build last "
                f"touched it:\n"
                f"    this build knows   {seen}\n"
                f"    WordPress now says {now}\n"
                f"  Pull it again to see what changed, or pass --force to "
                f"overwrite it. The old body is only recoverable from "
                f"build/<slug>/backups/ — site revisions are off.")

    # Now that the page is known, the slug follows it — not the folder.
    page_slug = args.page_slug or (cur.get("slug") if post_id else None) \
        or args.slug

    man_path = os.path.join(out, "manifest.json")
    manifest = read_manifest(out)
    new = 0
    for rel in sorted(set(re.findall(r'(?:src|poster)="(assets/[^"]+)"', html))):
        local = os.path.join(out, rel)
        if not os.path.exists(local):
            die("ASSET_MISSING", local)
        # Key on the bytes, not the path. Keying on the path alone means a
        # corrected asset under the same filename is never uploaded and the
        # page silently keeps serving the old one — which is how a logo with
        # its background keyed out stayed opaque on the live page.
        sha = hashlib.sha256(open(local, "rb").read()).hexdigest()
        if manifest.get(rel, {}).get("sha") == sha:
            continue
        send = local if args.no_webp else to_webp(local)
        ext = os.path.splitext(send)[1].lower()
        res = wp("POST", "/wp-json/wp/v2/media", raw=open(send, "rb").read(),
                 headers={"Content-Type": MIME.get(ext, "application/octet-stream"),
                          "Content-Disposition":
                              f'attachment; filename="{args.slug}-'
                              f'{os.path.basename(send)}"'},
                 creds=creds)
        manifest[rel] = {"id": res["id"], "url": res["source_url"], "sha": sha}
        new += 1
        saved = ""
        if send != local:
            saved = (f"  ({os.path.getsize(local) // 1024}K -> "
                     f"{os.path.getsize(send) // 1024}K webp)")
        print(f"  uploaded {rel}{saved}")
    with open(man_path, "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"media     {new} new, {len(manifest) - new} reused")
    for rel, rec in manifest.items():
        html = html.replace(f'"{rel}"', f'"{rec["url"]}"')

    if post_id:
        # Revisions are off site-wide. Keep the old body before overwriting it.


        bdir = os.path.join(out, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = re.sub(r"\D", "", str(cur.get("modified_gmt") or "prev"))
        bpath = os.path.join(bdir, f"{post_id}-{stamp}.html")
        with open(bpath, "w", encoding="utf-8") as fh:
            fh.write((cur.get("content") or {}).get("raw", ""))
        print(f"backup    {bpath}")
    else:
        if not args.title:
            die("NO_TITLE", "--title is required when creating a page")
        res = ability("mc/create-localized-page", {
            "title": args.title, "language": args.language, "status": "draft",
            "slug": page_slug,
            **({"parent_id": parent_id} if parent_id else {}),
        }, creds=creds)
        post_id = (res.get("output") or res).get("id") or res.get("id")
        if not post_id:
            die("CREATE_FAILED", json.dumps(res)[:300])
        print(f"page      created #{post_id} (draft, {args.language})")

    ability("mc/set-post-html", {"post_id": int(post_id), "content": html}, creds=creds)
    print(f"body      set via mc/set-post-html ({len(html)} bytes)")

    # page.js rides in post meta and is printed from wp_footer by the plugin —
    # post_content would corrupt it. Always write the key, so removing the file
    # removes the script from the page too.
    js = page_js(out)
    wp("POST", f"/wp-json/wp/v2/pages/{post_id}",
       body={"meta": {"_wpbuddy_page_js": js}}, creds=creds)
    print(f"page js   {len(js)} bytes -> post meta" if js else "page js   none")
    perm = ability("mc/regenerate-permalink",
                   {"post_id": int(post_id), "slug": page_slug}, creds=creds)
    print(f"permalink {(perm.get('output') or perm).get('permalink', '')}")

    # Read modified_gmt LAST. It used to be read straight after set-post-html,
    # but the post-meta write and the permalink call each bump it again, so the
    # stored stamp was always a few seconds behind the site and the very next
    # push tripped its own PAGE_MOVED_ON guard. A guard that cries wolf every
    # time gets waved through with --force, which is the one thing it exists to
    # prevent.
    after = wp("GET", f"/wp-json/wp/v2/pages/{post_id}"
                      f"?context=edit&_fields=id,slug,title,status,link,parent,modified_gmt",
               creds=creds)
    write_wp_meta(out, {**meta, "post_id": after["id"], "slug": after["slug"],
                        "title": (after["title"] or {}).get("raw", ""),
                        "parent": after.get("parent"), "link": after["link"],
                        "status": after["status"],
                        "modified_gmt": after["modified_gmt"],
                        "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime())})
    print("\nDraft is up. After publishing, purge the cache:")
    print("  PUT /wp-json/siteground-optimizer/v1/purge-cache")


def main():
    p = argparse.ArgumentParser(prog="figma_to_wp")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract")
    e.add_argument("source", help="a Figma frame URL, or a folder of Figma exports")
    e.add_argument("--slug")
    e.add_argument("--frame", help="which frame is the page: a PDF name in "
                                   "export mode, a frame name for a page URL "
                                   "(default: the largest)")
    e.add_argument("--no-assets", action="store_true")
    e.add_argument("--no-render", action="store_true")
    e.set_defaults(fn=cmd_extract)

    v = sub.add_parser("verify")
    v.add_argument("slug")
    v.add_argument("--section", help="check only the copy inside this section's "
                                     "band, for a page still being built")
    v.add_argument("--through", help="check everything from the top of the page "
                                     "down through this section")
    v.set_defaults(fn=cmd_verify)

    df = sub.add_parser("diff", help="screenshot a page and compare it to the "
                                     "Figma render")
    df.add_argument("slug")
    df.add_argument("--url", help="the page to shoot: live URL or local preview")
    df.add_argument("--width", type=int, help="viewport width (default: the "
                                              "design's own width)")
    df.add_argument("--bands", type=int, default=20)
    df.add_argument("--section", help="compare only this section (needs a "
                                      "sections.json from `audit`)")
    df.add_argument("--offset", type=int, default=0,
                    help="drop this many px off the top of the reference, for a "
                         "frame that draws the site header the page does not")
    df.add_argument("--cols", type=int, default=3,
                    help="split each band across the page too (default 3)")
    df.add_argument("--ref", help="compare against this image instead of "
                                  "the build's design.png")
    df.set_defaults(fn=cmd_diff)

    pl = sub.add_parser("pull", help="bring a page that already exists in "
                                     "WordPress down to a build folder")
    pl.add_argument("target", help="post id, URL, slug, or exact title")
    pl.add_argument("--slug", help="build folder name (default: the page slug)")
    pl.set_defaults(fn=cmd_pull)

    au = sub.add_parser("audit", help="name every element that does not match "
                                      "the design, instead of scoring regions")
    au.add_argument("slug")
    au.add_argument("--url", required=True, help="a page served by `preview`")
    au.add_argument("--section", help="only this section (its class or "
                                      "data-section name)")
    au.add_argument("--tolerance", type=int, default=4,
                    help="px of drift to ignore (default 4)")
    au.set_defaults(fn=cmd_audit)

    mo = sub.add_parser("mobile", help="the phone checks the design file cannot "
                                       "answer: overflow, text size, tap targets, "
                                       "gutter")
    mo.add_argument("slug")
    mo.add_argument("--url", required=True, help="a page served by `preview`")
    mo.add_argument("--width", type=int, default=390,
                    help="viewport width (default 390; rendered in an iframe "
                         "because headless Chrome will not go below 500)")
    mo.add_argument("--height", type=int, default=844)
    mo.set_defaults(fn=cmd_mobile)

    pv = sub.add_parser("preview")
    pv.add_argument("slug")
    pv.add_argument("--port", type=int, default=8731)
    pv.add_argument("--bare", action="store_true",
                    help="skip the live page's CSS and wrappers — measures the "
                         "build in isolation, which is not what ships")
    pv.add_argument("--refresh-site", action="store_true",
                    help="re-fetch the live page's CSS and wrappers")
    pv.set_defaults(fn=cmd_preview)

    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    u = sub.add_parser("push")
    u.add_argument("slug")
    u.add_argument("--title")
    u.add_argument("--parent", type=int, help="parent page id")
    u.add_argument("--parent-path",
                   help="parent by URL path, e.g. partners/work-collaboration")
    u.add_argument("--page-slug", help="the page's URL slug (default: the build slug)")
    u.add_argument("--post-id", type=int, help="update this page instead of creating one")
    u.add_argument("--language", default="en", choices=["en", "zh-hant", "zh-hans"])
    u.add_argument("--force", action="store_true",
                   help="push even though WordPress has moved on since this "
                        "build last touched the page")
    u.add_argument("--no-webp", action="store_true",
                   help="upload PNG/JPEG as-is instead of converting to WebP")
    u.set_defaults(fn=cmd_push)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
