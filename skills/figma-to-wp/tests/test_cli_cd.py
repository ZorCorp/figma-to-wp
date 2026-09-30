import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "figma_to_wp.py")
FIX = os.path.join(HERE, "fixtures", "cd-78436")

# export.html, pushed-before.html and expected-body.html are a real page (post
# 78436). They live in the Figma-to-WP build repo only; the published plugin
# ships without them, and every test that reads them skips there.
PAGE_FIXTURES = all(os.path.exists(os.path.join(FIX, n)) for n in
                    ("export.html", "pushed-before.html", "expected-body.html"))
needs_page = unittest.skipUnless(PAGE_FIXTURES, "page fixtures are not shipped with the plugin")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))


def run(cwd, *args):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd,
                          capture_output=True, text=True, timeout=600)


@needs_page
class ExtractClaudeDesign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.res = run(cls.tmp, "extract", os.path.join(FIX, "export.html"),
                      "--slug", "cd", "--from", "claude-design",
                      "--cd-project", "00000000-0000-4000-8000-000000000000",
                      "--cd-file", "export/wp-ai-in-motion.html")
        cls.out = os.path.join(cls.tmp, "build", "cd")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_exit_ok(self):
        self.assertEqual(self.res.returncode, 0, self.res.stderr + self.res.stdout)

    def test_files(self):
        for f in ("page.html", "design.png", "design.json", "cd-export.html"):
            self.assertTrue(os.path.exists(os.path.join(self.out, f)), f)

    def test_design_json(self):
        with open(os.path.join(self.out, "design.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["source"]["type"], "claude-design")
        self.assertEqual(d["source"]["width"], 1440)
        self.assertTrue(any(t["text"] == "立即報名" for t in d["texts"]))
        self.assertTrue(all({"i", "text", "y"} <= set(t) for t in d["texts"]))
        self.assertTrue(d["assets"])
        for a in d["assets"]:
            self.assertTrue(os.path.exists(os.path.join(self.out, a["file"])), a["file"])

    def test_page_uses_local_assets_not_data_uris_for_img(self):
        with open(os.path.join(self.out, "page.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertNotRegex(page, r'<img\b[^>]*src="data:')
        self.assertIn('src="assets/cd-', page)

    def test_refuses_to_overwrite_hand_edits(self):
        p = os.path.join(self.out, "page.html")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("<!-- hand edit -->\n")
        r = run(self.tmp, "extract", os.path.join(FIX, "export.html"),
                "--slug", "cd", "--from", "claude-design")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HAND_EDITED", r.stderr + r.stdout)

    def test_bad_export_prints_fix_prompt_and_writes_nothing(self):
        r = run(self.tmp, "extract", os.path.join(FIX, "bad-two-roots.html"),
                "--slug", "bad", "--from", "claude-design")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("請按以下要求修改頁面", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "build", "bad")))

    def test_refuses_to_overwrite_a_non_cd_build(self):
        # build/<slug>/page.html from a Figma build (or any build without a
        # claude-design design.json) must never be silently clobbered by an
        # extract --from claude-design that happens to share the slug.
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "build", "figstuff")
            os.makedirs(out)
            with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as fh:
                fh.write('<div id="figma-built">hello</div>')
            with open(os.path.join(out, "design.json"), "w", encoding="utf-8") as fh:
                json.dump({"source": {"type": "figma"}}, fh)
            with open(os.path.join(out, "page.html"), encoding="utf-8") as fh:
                before = fh.read()

            r = run(tmp, "extract", os.path.join(FIX, "export.html"),
                    "--slug", "figstuff", "--from", "claude-design")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("NOT_A_CD_BUILD", r.stderr + r.stdout)

            with open(os.path.join(out, "page.html"), encoding="utf-8") as fh:
                after = fh.read()
            self.assertEqual(before, after)
        finally:
            shutil.rmtree(tmp)

    def test_refuses_to_overwrite_a_build_with_no_design_json(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "build", "nodj")
            os.makedirs(out)
            with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as fh:
                fh.write("<div id=\"whatever\">hi</div>")

            r = run(tmp, "extract", os.path.join(FIX, "export.html"),
                    "--slug", "nodj", "--from", "claude-design")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("NOT_A_CD_BUILD", r.stderr + r.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_verify_passes_on_clean_extract(self):
        r = run(self.tmp, "verify", "cd")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("0 errors", r.stdout)

    def test_verify_does_not_read_font_names_out_of_the_reset(self):
        # The data-wpsafe reset quotes "Raleway…!important" in a comment and
        # sets "inherit!important"; neither is a font this page names.
        r = run(self.tmp, "verify", "cd")
        for line in r.stdout.splitlines():
            if line.startswith("warn"):
                self.assertNotIn("Raleway", line)
                self.assertNotIn("inherit!important", line)
                self.assertNotIn("!important' is named", line)

    def test_verify_fails_when_page_breaks_contract(self):
        p = os.path.join(self.out, "page.html")
        with open(p, encoding="utf-8") as fh:
            keep = fh.read()
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(keep.replace("<!-- /wp:html -->",
                                      "<style>body{margin:0}</style><!-- /wp:html -->"))
            r = run(self.tmp, "verify", "cd")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("css-scope", r.stdout)
        finally:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(keep)

    def test_verify_fails_on_absolute_masterconcept_ai_href_in_cd_build(self):
        # Regression: "links must be relative so WPML can localise them" is
        # not Figma-only — it holds for every build. Only an <img>/<source>
        # src, srcset or <video> poster pointing at an already-published
        # masterconcept.ai/wp-content/ asset is exempt for a Claude Design
        # build; an absolute masterconcept.ai <a href> must still fail.
        p = os.path.join(self.out, "page.html")
        with open(p, encoding="utf-8") as fh:
            keep = fh.read()
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(keep.replace(
                    "<!-- /wp:html -->",
                    '<a href="https://masterconcept.ai/solutions/x/">x</a>'
                    "<!-- /wp:html -->"))
            r = run(self.tmp, "verify", "cd")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("absolute site link", r.stdout)
        finally:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(keep)


class ServeDir(unittest.TestCase):
    def test_stop_server_closes_the_listening_socket(self):
        # Repro from the review finding: shutdown() alone stops the
        # serve_forever() loop but leaves the socket open, so gc collecting
        # the ThreadingHTTPServer raises ResourceWarning: unclosed socket.
        # stop_server() is the exact sequence extract_claude_design's
        # `finally` runs, so this exercises the real code path, not a copy
        # of it.
        import gc
        import figma_to_wp as fw
        srv = fw.serve_dir(HERE)
        self.assertNotEqual(srv.socket.fileno(), -1)
        fw.stop_server(srv)
        self.assertEqual(srv.socket.fileno(), -1, "stop_server left the socket open")
        del srv
        gc.collect()

    def test_bare_shutdown_leaves_the_socket_open(self):
        # The reason stop_server() exists: shutdown() alone is not enough.
        import figma_to_wp as fw
        srv = fw.serve_dir(HERE)
        try:
            srv.shutdown()
            self.assertNotEqual(srv.socket.fileno(), -1)
        finally:
            srv.server_close()


class Images(unittest.TestCase):
    def test_only_img_data_uris_are_extracted(self):
        import figma_to_wp as fw
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        html = (f'<div id="x"><style>#x{{background:url(data:image/png;base64,{png})}}</style>'
                f'<img src="data:image/png;base64,{png}" alt="dot"></div>')
        with tempfile.TemporaryDirectory() as d:
            new, assets = fw.cd_extract_images(html, d)
            self.assertIn("url(data:image/png", new)
            self.assertRegex(new, r'<img src="assets/cd-[0-9a-f]{8}\.png" alt="dot">')
            self.assertEqual(assets[0]["name"], "dot")
            self.assertTrue(os.path.exists(os.path.join(d, assets[0]["file"])))

    def test_video_poster_data_uri_is_extracted_on_any_tag(self):
        import figma_to_wp as fw
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        html = f'<div id="x"><video poster="data:image/png;base64,{png}"></video></div>'
        with tempfile.TemporaryDirectory() as d:
            new, assets = fw.cd_extract_images(html, d)
            self.assertRegex(new, r'<video poster="assets/cd-[0-9a-f]{8}\.png"></video>')
            self.assertTrue(assets)
            self.assertTrue(os.path.exists(os.path.join(d, assets[0]["file"])))


class SiteSample(unittest.TestCase):
    def test_falls_back_to_sample_for_private_link(self):
        import figma_to_wp as fw
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "wp.json"), "w", encoding="utf-8") as fh:
                json.dump({"link": "https://masterconcept.ai/?p=1"}, fh)
            picked = fw.site_shell_url(d, sample=None)
            self.assertEqual(picked, fw.SITE_SAMPLE)

    def test_explicit_sample_wins(self):
        import figma_to_wp as fw
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(fw.site_shell_url(d, sample="https://masterconcept.ai/x/"),
                             "https://masterconcept.ai/x/")

    @unittest.skipUnless(os.environ.get("FIGMA_WP_NETWORK_TESTS") == "1",
                         "network test: set FIGMA_WP_NETWORK_TESTS=1 to fetch the live sample")
    def test_sample_carries_the_rules_the_reset_answers(self):
        """Network test, skipped unless FIGMA_WP_NETWORK_TESTS=1: fetches
        SITE_SAMPLE from masterconcept.ai. The default sample must still load
        both theme rules, or a diff against it cannot see the damage the reset
        prevents."""
        import figma_to_wp as fw
        with tempfile.TemporaryDirectory() as d:
            css, _ = fw.site_shell(d, sample=fw.SITE_SAMPLE)
            self.assertRegex(css, r"form\s+label\s*\{[^}]*padding:\s*20px 0 10px")
            self.assertRegex(css, r"Raleway[^;}]*!important")

    @staticmethod
    def _fake_response(html):
        class _Resp:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_):
                return html.encode("utf-8")
        return _Resp()

    def test_cache_for_a_different_source_is_refetched(self):
        # A cache written for source A must not shadow a `sample=B` passed
        # today — that would silently ignore --site-sample forever.
        import figma_to_wp as fw
        with tempfile.TemporaryDirectory() as d:
            site = os.path.join(d, "site")
            os.makedirs(site)
            with open(os.path.join(site, "site.css"), "w", encoding="utf-8") as fh:
                fh.write("/* stale A css */")
            fw.write_json(os.path.join(site, "shell.json"),
                          {"source": "https://masterconcept.ai/a/", "wrappers": []})
            html = "<html><body><style>.fresh-b{color:red}</style></body></html>"
            with mock.patch.object(fw.urllib.request, "urlopen",
                                   return_value=self._fake_response(html)) as m:
                css, shell = fw.site_shell(d, sample="https://masterconcept.ai/b/")
            self.assertTrue(m.called)
            self.assertIn(".fresh-b", css)
            self.assertEqual(shell["source"], "https://masterconcept.ai/b/")
            with open(os.path.join(site, "shell.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["source"],
                                 "https://masterconcept.ai/b/")

    def test_cache_for_the_same_source_is_not_refetched(self):
        # sample=B against a cache already written for B: no network call.
        import figma_to_wp as fw
        with tempfile.TemporaryDirectory() as d:
            site = os.path.join(d, "site")
            os.makedirs(site)
            with open(os.path.join(site, "site.css"), "w", encoding="utf-8") as fh:
                fh.write("/* cached B css */")
            fw.write_json(os.path.join(site, "shell.json"),
                          {"source": "https://masterconcept.ai/b/", "wrappers": []})
            with mock.patch.object(fw.urllib.request, "urlopen",
                                   side_effect=AssertionError(
                                       "should not fetch on a matching cache")) as m:
                css, shell = fw.site_shell(d, sample="https://masterconcept.ai/b/")
            self.assertFalse(m.called)
            self.assertEqual(css, "/* cached B css */")
            self.assertEqual(shell["source"], "https://masterconcept.ai/b/")

    def test_legacy_cache_without_source_is_refetched(self):
        # A cache written before this fix has no "source" key at all — treat
        # it the same as a mismatch, not as a free pass.
        import figma_to_wp as fw
        with tempfile.TemporaryDirectory() as d:
            site = os.path.join(d, "site")
            os.makedirs(site)
            with open(os.path.join(site, "site.css"), "w", encoding="utf-8") as fh:
                fh.write("/* legacy css, no source recorded */")
            fw.write_json(os.path.join(site, "shell.json"), {"wrappers": []})
            html = "<html><body><style>.fresh-legacy{color:blue}</style></body></html>"
            with mock.patch.object(fw.urllib.request, "urlopen",
                                   return_value=self._fake_response(html)) as m:
                css, shell = fw.site_shell(d, sample="https://masterconcept.ai/b/")
            self.assertTrue(m.called)
            self.assertIn(".fresh-legacy", css)
            self.assertEqual(shell["source"], "https://masterconcept.ai/b/")


class PushPost(unittest.TestCase):
    def setUp(self):
        import figma_to_wp as fw
        self.fw = fw
        self._orig_wp = fw.wp
        self._orig_ability = fw.ability
        self._orig_wp_creds = fw.wp_creds
        self._orig_build = fw.BUILD
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        fw.BUILD = os.path.join(self.tmp, "build")
        out = os.path.join(fw.BUILD, "p")
        os.makedirs(out)
        with open(os.path.join(out, "page.html"), "w") as fh:
            fh.write('<!-- wp:html -->\n<div id="x"><p>a</p></div>\n<!-- /wp:html -->\n')
        with open(os.path.join(out, "wp.json"), "w") as fh:
            json.dump({"post_id": 5, "post_type": "post", "modified_gmt": "2026-01-01T00:00:00"}, fh)
        self.calls = []

    def tearDown(self):
        self.fw.wp = self._orig_wp
        self.fw.ability = self._orig_ability
        self.fw.wp_creds = self._orig_wp_creds
        self.fw.BUILD = self._orig_build
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def fake(self, modified):
        def wp(method, path, **kw):
            self.calls.append((method, path))
            if method == "GET":
                return {"id": 5, "slug": "p", "status": "private", "link": "https://x/p/",
                        "title": {"raw": "P"}, "parent": 0, "modified_gmt": modified,
                        "content": {"raw": "old", "rendered": '<div id="x"><p>a</p></div>'}}
            return {}
        return wp

    def args(self, **kw):
        a = dict(slug="p", title=None, parent=None, parent_path=None, page_slug=None,
                 post_id=None, language="zh-hant", force=False, no_webp=True,
                 post_type=None, category=[], region=[])
        a.update(kw)
        return type("A", (), a)()

    def test_post_uses_posts_endpoint_and_keeps_slug(self):
        self.fw.wp = self.fake("2026-01-01T00:00:00")
        self.fw.ability = lambda name, payload, creds=None: self.calls.append((name, payload)) or {}
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        self.fw.cmd_push(self.args())
        paths = [c[1] for c in self.calls if isinstance(c[1], str)]
        self.assertTrue(any(p.startswith("/wp-json/wp/v2/posts/5") for p in paths))
        self.assertFalse(any("/pages/" in p for p in paths))
        self.assertNotIn("mc/regenerate-permalink", [c[0] for c in self.calls])

    def test_post_moved_on_is_refused(self):
        self.fw.wp = self.fake("2026-09-28T03:57:01")
        self.fw.ability = lambda *a, **k: self.fail("must not write")
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        with self.assertRaises(SystemExit):
            self.fw.cmd_push(self.args())

    def test_post_type_mismatch_dies_before_any_request(self):
        # wp.json says "post"; --post-type page contradicts it — refuse
        # before making a single request, not after acting on one post_type
        # and finding out the other is what the build actually is.
        def wp(method, path, **kw):
            self.fail("must not call wp()")
        self.fw.wp = wp
        self.fw.ability = lambda *a, **k: self.fail("must not write")
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        with self.assertRaises(SystemExit):
            self.fw.cmd_push(self.args(post_type="page"))


class PushPostCreate(unittest.TestCase):
    """Creating a new post: term resolution must happen before any write,
    and in the right order relative to the other calls a create makes."""

    def setUp(self):
        import figma_to_wp as fw
        self.fw = fw
        self._orig_wp = fw.wp
        self._orig_ability = fw.ability
        self._orig_wp_creds = fw.wp_creds
        self._orig_build = fw.BUILD
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        fw.BUILD = os.path.join(self.tmp, "build")
        self.out = os.path.join(fw.BUILD, "p")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "page.html"), "w") as fh:
            fh.write('<!-- wp:html -->\n<div id="x"><p>a</p></div>\n<!-- /wp:html -->\n')
        self.calls = []

    def tearDown(self):
        self.fw.wp = self._orig_wp
        self.fw.ability = self._orig_ability
        self.fw.wp_creds = self._orig_wp_creds
        self.fw.BUILD = self._orig_build
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def args(self, **kw):
        a = dict(slug="p", title="T", parent=None, parent_path=None, page_slug=None,
                 post_id=None, language="zh-hant", force=False, no_webp=True,
                 post_type="post", category=[], region=[])
        a.update(kw)
        return type("A", (), a)()

    def existing_wp_json(self, **extra):
        meta = {"post_id": 5, "post_type": "post", "modified_gmt": "2026-01-01T00:00:00"}
        meta.update(extra)
        with open(os.path.join(self.out, "wp.json"), "w") as fh:
            json.dump(meta, fh)

    def fake_wp(self, region_hit=True):
        def wp(method, path, **kw):
            self.calls.append((method, path))
            if method == "GET" and "/categories" in path:
                return [{"id": 11}]
            if method == "GET" and "/region" in path:
                return [{"id": 22}] if region_hit else []
            if method == "GET":
                return {"id": 5, "slug": "p", "status": "draft", "link": "https://x/p/",
                        "title": {"raw": "T"}, "parent": 0,
                        "modified_gmt": "2026-01-01T00:00:00",
                        "content": {"raw": "x", "rendered": '<div id="x"><p>a</p></div>'}}
            return {}
        return wp

    def test_create_resolves_terms_before_create_then_html_then_region_then_permalink(self):
        self.fw.wp = self.fake_wp()

        def ability(name, payload, creds=None):
            self.calls.append((name, payload))
            if name == "ewpa/create-post":
                return {"id": 5}
            return {}
        self.fw.ability = ability
        self.fw.wp_creds = lambda: ("https://x", "u", "p")

        self.fw.cmd_push(self.args(category=["blog"], region=["hong-kong"]))

        names = [c[0] for c in self.calls]
        term_get_idx = [i for i, c in enumerate(self.calls)
                        if isinstance(c[1], str)
                        and ("/categories" in c[1] or "/region" in c[1])]
        create_idx = names.index("ewpa/create-post")
        html_idx = names.index("mc/set-post-html")
        setterms_idx = names.index("mc/set-terms")
        perm_idx = names.index("mc/regenerate-permalink")

        self.assertTrue(term_get_idx, "expected at least one term GET")
        self.assertLess(max(term_get_idx), create_idx)
        self.assertLess(create_idx, html_idx)
        self.assertLess(html_idx, setterms_idx)
        self.assertLess(setterms_idx, perm_idx)
        self.assertEqual(self.calls[create_idx][1]["categories"], [11])
        self.assertEqual(self.calls[setterms_idx][1]["terms"], [22])

    def test_bad_region_on_create_writes_nothing(self):
        self.fw.wp = self.fake_wp(region_hit=False)
        self.fw.ability = lambda *a, **k: self.fail("must not write")
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        with self.assertRaises(SystemExit):
            self.fw.cmd_push(self.args(category=["blog"], region=["nope"]))
        self.assertFalse(any(m == "POST" and "/media" in p for m, p in self.calls))

    def test_bad_region_on_update_is_refused_before_set_post_html(self):
        self.existing_wp_json()
        self.fw.wp = self.fake_wp(region_hit=False)
        self.fw.ability = lambda *a, **k: self.fail("must not write")
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        with self.assertRaises(SystemExit):
            self.fw.cmd_push(self.args(title=None, region=["nope"]))

    def test_category_ignored_warning_on_page(self):
        self.fw.wp = self.fake_wp()

        def ability(name, payload, creds=None):
            self.calls.append((name, payload))
            if name == "mc/create-localized-page":
                return {"id": 5}
            return {}
        self.fw.ability = ability
        self.fw.wp_creds = lambda: ("https://x", "u", "p")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fw.cmd_push(self.args(post_type="page", category=["blog"]))
        self.assertIn("warn  --category ignored: post-type is page", buf.getvalue())
        # a page create must never touch categories or region term GETs
        self.assertFalse(any(isinstance(c[1], str) and "/categories" in c[1]
                             for c in self.calls))

    def test_category_ignored_warning_on_update(self):
        self.existing_wp_json()
        self.fw.wp = self.fake_wp()
        self.fw.ability = lambda name, payload, creds=None: \
            self.calls.append((name, payload)) or {}
        self.fw.wp_creds = lambda: ("https://x", "u", "p")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fw.cmd_push(self.args(title=None, category=["blog"]))
        self.assertIn("categories are only set when a post is created",
                      buf.getvalue())

    def test_postcheck_notes_fewer_p_than_expected(self):
        # rendered has 0 <p> against a pushed body with 1 <p> -> delta < 0,
        # which is not the wpautop-added-paragraphs case.
        def wp(method, path, **kw):
            self.calls.append((method, path))
            if method == "GET":
                return {"id": 5, "slug": "p", "status": "draft", "link": "https://x/p/",
                        "title": {"raw": "T"}, "parent": 0,
                        "modified_gmt": "2026-01-01T00:00:00",
                        "content": {"raw": "x", "rendered": "<div id=\"x\"></div>"}}
            return {}
        self.fw.wp = wp

        def ability(name, payload, creds=None):
            self.calls.append((name, payload))
            if name == "ewpa/create-post":
                return {"id": 5}
            return {}
        self.fw.ability = ability
        self.fw.wp_creds = lambda: ("https://x", "u", "p")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fw.cmd_push(self.args())
        out = buf.getvalue()
        self.assertIn("postcheck wpautop -1 <p>", out)
        self.assertIn("could not compare", out)
        self.assertNotIn("WordPress added paragraphs", out)

    # Final review: every shape the create ability answers with is read,
    # and wp.json holds the new post before anything after the create runs.

    def test_create_reads_every_id_shape(self):
        for res in ({"output": {"post_id": 7}}, {"output": {"id": 7}}, {"id": 7},
                    {"post_id": 7}):
            with self.subTest(res=res):
                wpj = os.path.join(self.out, "wp.json")
                if os.path.exists(wpj):
                    os.remove(wpj)
                self.calls = []

                def wp(method, path, **kw):
                    self.calls.append((method, path))
                    if method == "GET":
                        return {"id": 7, "slug": "p", "status": "draft", "link": "https://x/p/",
                                "title": {"raw": "T"}, "parent": 0,
                                "modified_gmt": "2026-01-01T00:00:00",
                                "content": {"raw": "x", "rendered": '<div id="x"><p>a</p></div>'}}
                    return {}
                self.fw.wp = wp

                def ability(name, payload, creds=None, _res=res):
                    self.calls.append((name, payload))
                    return _res if name == "ewpa/create-post" else {}
                self.fw.ability = ability
                self.fw.wp_creds = lambda: ("https://x", "u", "p")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.fw.cmd_push(self.args())
                html_calls = [c for c in self.calls if c[0] == "mc/set-post-html"]
                self.assertEqual(html_calls[0][1]["post_id"], 7)
                with open(wpj) as fh:
                    self.assertEqual(json.load(fh)["post_id"], 7)

    def test_wp_json_holds_the_post_before_set_terms_fails(self):
        self.fw.wp = self.fake_wp()

        def ability(name, payload, creds=None):
            self.calls.append((name, payload))
            if name == "ewpa/create-post":
                return {"output": {"post_id": 9}}
            if name == "mc/set-terms":
                raise RuntimeError("set-terms blew up")
            return {}
        self.fw.ability = ability
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                self.fw.cmd_push(self.args(region=["hong-kong"]))
        with open(os.path.join(self.out, "wp.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["post_id"], 9)
        self.assertEqual(meta["post_type"], "post")

        # The next push updates #9 instead of creating a second post.
        self.calls = []
        self.fw.ability = lambda name, payload, creds=None: \
            self.calls.append((name, payload)) or {}
        with contextlib.redirect_stdout(io.StringIO()):
            self.fw.cmd_push(self.args(title=None, region=["hong-kong"]))
        names = [c[0] for c in self.calls]
        self.assertNotIn("ewpa/create-post", names)
        self.assertEqual([c for c in self.calls if c[0] == "mc/set-post-html"][0][1]["post_id"], 9)
        self.assertIn("mc/regenerate-permalink", names)

    def test_postcheck_names_both_causes_of_added_p(self):
        def wp(method, path, **kw):
            if method == "GET":
                return {"id": 5, "slug": "p", "status": "draft", "link": "https://x/p/",
                        "title": {"raw": "T"}, "parent": 0,
                        "modified_gmt": "2026-01-01T00:00:00",
                        "content": {"raw": "x",
                                    "rendered": '<p></p><div id="x"><p>a</p></div>'}}
            return {}
        self.fw.wp = wp
        self.fw.ability = lambda name, payload, creds=None: \
            {"id": 5} if name == "ewpa/create-post" else {}
        self.fw.wp_creds = lambda: ("https://x", "u", "p")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fw.cmd_push(self.args())
        out = buf.getvalue()
        self.assertIn("postcheck wpautop +1 <p>", out)
        self.assertIn("wp:html", out)
        self.assertIn("the_content", out)


class ResolveParent(unittest.TestCase):
    """--parent-path must resolve in the page's own language.

    WPML keeps one `solutions` page per language (46209 en, 46211 zh-hant).
    Without a lang filter the REST query answers in the default language, and
    a zh-hant page pushed with --parent-path solutions landed under the
    English parent.
    """

    def setUp(self):
        import figma_to_wp as fw
        self.fw = fw
        self.orig_wp = fw.wp
        self.paths = []

        def wp(method, path, **kw):
            self.paths.append(path)
            lang = "zh-hant" if "lang=zh-hant" in path else "en"
            return [{"id": 46211 if lang == "zh-hant" else 46209,
                     "slug": "solutions", "parent": 0}]
        fw.wp = wp

    def tearDown(self):
        self.fw.wp = self.orig_wp

    def test_resolves_in_the_given_language(self):
        self.assertEqual(self.fw.resolve_parent("solutions", None, "zh-hant"), 46211)
        self.assertTrue(all("lang=zh-hant" in p for p in self.paths))

    def test_english_is_the_default(self):
        self.assertEqual(self.fw.resolve_parent("solutions", None), 46209)
