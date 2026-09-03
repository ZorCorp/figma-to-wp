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
    extract  <url|export-dir>   Figma -> build/<slug>/
    verify   <slug>             page.html against design.json
    preview  <slug>             serve build/<slug>/ for a browser to screenshot
    push     <slug>             media upload -> draft page -> permalink

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
import hashlib
from html import unescape
import shutil
import subprocess
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
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


def load_frame(url, tok):
    key, node = parse_url(url)
    if not node:
        die("NO_NODE_ID", "that URL has no node-id. Open the page or frame in "
                          "Figma and copy its link.")
    cache_dir = os.path.join(HOME, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"{key}-{node.replace(':', '_')}.json")
    if os.path.exists(cache):
        with open(cache) as fh:
            data = json.load(fh)
        print("cache     reusing the node response", file=sys.stderr)
    else:
        data = figma_get(f"/v1/files/{key}/nodes?ids={urllib.parse.quote(node)}", tok)
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

    def desc(key):
        st = table.get(str(key)) or base if key else base
        return {"size": st.get("fontSize", base.get("fontSize")),
                "weight": st.get("fontWeight", base.get("fontWeight")),
                "lh": st.get("lineHeightPx", base.get("lineHeightPx")),
                "case": st.get("textCase", base.get("textCase"))}

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
        x, y, _, _ = bbox(n)
        sid = n.get("styles") or {}
        st = n.get("style") or {}
        # Record the resolved values, not only the style's name. A name is
        # not checkable against anything: "heading-2" cannot be compared to a
        # stylesheet, and it cannot tell you the paragraph shipped at 14px
        # where the design says 20.
        rec = {"y": round(y - oy), "x": round(x - ox), "text": s,
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

    def visit(n, depth):
        if not n.get("visible", True):
            return
        x, y, w, h = bbox(n)
        if not n.get("absoluteBoundingBox"):
            # A CANVAS has no box of its own; measuring stops here but the
            # children below it are the whole point.
            for c in n.get("children") or []:
                visit(c, depth)
            return
        if w < min_side or h < min_side:
            return                      # too small to lay anything out against
        rec = {"id": n["id"], "name": n.get("name"), "type": n.get("type"),
               "x": round(x - ox), "y": round(y - oy),
               "w": round(w), "h": round(h), "depth": depth}
        if n.get("layoutMode") and n["layoutMode"] != "NONE":
            rec["layout"] = n["layoutMode"].lower()
            rec["pad"] = [round(n.get(k, 0)) for k in
                          ("paddingTop", "paddingRight",
                           "paddingBottom", "paddingLeft")]
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
        found.append(rec)
        for c in n.get("children") or []:
            visit(c, depth + 1)

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
    """
    boxes = [c["absoluteBoundingBox"] for c in (canvas.get("children") or [])
             if c.get("visible", True) and c.get("absoluteBoundingBox")]
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
    with open(os.path.join(out, "design.json"), "w") as fh:
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
    out = os.path.join(BUILD, args.slug)
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
    for t in design["texts"]:
        s = squash(t["text"])
        if not s or s in body:
            continue
        if any(k and k in s for k in dropped):
            skipped += 1
            continue
        errors.append(f'copy missing (#{t["i"]}, y={t["y"]}): {t["text"].strip()[:70]!r}')

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

    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        if 'aria-hidden="true"' in tag:
            continue
        alt = re.search(r'alt="([^"]*)"', tag)
        if not alt:
            errors.append(f"img without alt: {tag[:70]}")
        elif not alt.group(1).strip():
            errors.append(f"img with empty alt: {tag[:70]}")

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
    total = len(design["texts"])
    print(f"\n{total - len([e for e in errors if e.startswith('copy')])}/{total} "
          f"strings matched ({skipped} dropped on purpose)")
    print(f"{len(errors)} errors, {len(warnings)} warnings")
    sys.exit(1 if errors else 0)


# -------------------------------------------------------------------- preview

def cmd_preview(args):
    import functools
    import http.server

    root = os.path.join(BUILD, args.slug)
    if not os.path.isdir(root):
        die("MISSING", root)
    page = assemble(root).replace("<!-- wp:html -->", "").replace("<!-- /wp:html -->", "")
    js = page_js(root)
    if js:
        page += "\n<script>\n" + js + "\n</script>\n"

    # Stamp every asset URL with its file's mtime. no-store on the response is
    # not enough on its own: a browser will keep serving an image it already
    # holds in its in-memory cache for the same URL, so you sit there looking
    # at the previous export wondering why the fix did nothing.
    def bust(m):
        rel = m.group(1)
        f = os.path.join(root, rel)
        v = int(os.path.getmtime(f)) if os.path.exists(f) else 0
        return f'src="{rel}?v={v}"'

    page = re.sub(r'src="(assets/[^"?]+)"', bust, page)

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

    with open(os.path.join(root, "preview.html"), "w", encoding="utf-8") as fh:
        fh.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1">'
                 f"<title>{args.slug} — preview</title>"
                 '<link href="https://fonts.googleapis.com/css2?family=Raleway:'
                 'wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;700'
                 '&display=swap" rel="stylesheet">'
                 "<style>body{margin:0}</style></head><body>" + page +
                 '<script src="wpbuddy-page.js"></script></body></html>')
    class Handler(http.server.SimpleHTTPRequestHandler):
        """Say charset=utf-8 out loud. SimpleHTTPRequestHandler sends bare
        `text/html`, and a viewer that does not read the <meta> tag — an IDE
        preview pane, for one — then guesses Latin-1 and mangles every dash."""

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
    ok = True
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


def cmd_push(args):
    # The build directory name and the page's URL slug are different things:
    # you want to try a design at a throwaway URL before it takes the real one.
    page_slug = args.page_slug or args.slug
    out = os.path.join(BUILD, args.slug)
    creds = wp_creds()
    parent_id = args.parent
    if args.parent_path:
        parent_id = resolve_parent(args.parent_path, creds)
        print(f"parent    {args.parent_path} -> #{parent_id}")
    html = assemble(out)

    man_path = os.path.join(out, "manifest.json")
    manifest = json.load(open(man_path)) if os.path.exists(man_path) else {}
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

    post_id = args.post_id
    if post_id:
        # Revisions are off site-wide. Keep the old body before overwriting it.
        cur = wp("GET", f"/wp-json/wp/v2/pages/{post_id}?context=edit", creds=creds)
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
    v.set_defaults(fn=cmd_verify)

    df = sub.add_parser("diff", help="screenshot a page and compare it to the "
                                     "Figma render")
    df.add_argument("slug")
    df.add_argument("--url", help="the page to shoot: live URL or local preview")
    df.add_argument("--width", type=int, help="viewport width (default: the "
                                              "design's own width)")
    df.add_argument("--bands", type=int, default=20)
    df.add_argument("--offset", type=int, default=0,
                    help="drop this many px off the top of the reference, for a "
                         "frame that draws the site header the page does not")
    df.add_argument("--cols", type=int, default=3,
                    help="split each band across the page too (default 3)")
    df.add_argument("--ref", help="compare against this image instead of "
                                  "the build's design.png")
    df.set_defaults(fn=cmd_diff)

    pv = sub.add_parser("preview")
    pv.add_argument("slug")
    pv.add_argument("--port", type=int, default=8731)
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
    u.add_argument("--no-webp", action="store_true",
                   help="upload PNG/JPEG as-is instead of converting to WebP")
    u.set_defaults(fn=cmd_push)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
