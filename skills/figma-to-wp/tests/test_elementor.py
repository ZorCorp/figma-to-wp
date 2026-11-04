import contextlib, io, json, os, re, shutil, sys, tempfile, unittest
import urllib.parse, urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import figma_to_wp as fw  # noqa: E402
import elementor as el  # noqa: E402


class VerifyCssShortcode(unittest.TestCase):
    def test_comment_with_shortcode_is_flagged(self):
        self.assertTrue(fw.css_shortcode_hits("/* [wpb_post_list] cards */ .a{}"))

    def test_underscore_name_outside_comment_is_flagged(self):
        self.assertTrue(fw.css_shortcode_hits(".a{} [wpb_hs_form] .b{}"))

    def test_attribute_selectors_pass(self):
        self.assertEqual(fw.css_shortcode_hits(
            'a[href^="#"]{} [data-section="x"]{} input[type=submit]{}'), [])

    def test_spaced_hyphen_warns_entity_does_not(self):
        self.assertTrue(fw.spaced_hyphens("<p>Gemini - Google Workspace</p>"))
        self.assertEqual(fw.spaced_hyphens(
            "<p>Gemini &#45; Google Workspace</p><p>e-mail</p>"), [])

    def test_spaced_hyphen_ignores_style_and_comments(self):
        self.assertEqual(fw.spaced_hyphens(
            "<style>a { margin: 1px - 2px }</style><!-- a - b --><p>ok</p>"), [])


class VerifyCheckWiring(unittest.TestCase):
    def build(self, css, body='<p id="herotop">Hello</p>'):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "build", "t")
        os.makedirs(out)
        json.dump({"texts": [], "assets": []}, open(os.path.join(out, "design.json"), "w"))
        open(os.path.join(out, "page.html"), "w").write("{{styles}}\n" + body)
        open(os.path.join(out, "page.css"), "w").write(css)
        return out

    def checks(self, css, body='<p id="herotop">Hello</p>'):
        # run_checks prints its own FAIL lines; keep the test output clean.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fw.run_checks(self.build(css, body))

    def test_shortcode_in_css_is_an_error(self):
        errors, _ = self.checks("/* [wpb_post_list] */ .a{}")
        self.assertTrue(any(e.startswith("shortcode in CSS:") for e in errors), errors)

    def test_clean_css_has_no_shortcode_error(self):
        errors, _ = self.checks('a[href^="#"]{color:red}')
        self.assertFalse(any("shortcode in CSS" in e for e in errors), errors)

    def test_spaced_hyphen_is_a_warning_not_an_error(self):
        errors, warnings = self.checks(".a{}", '<p id="herotop">Gemini - Workspace</p>')
        self.assertTrue(any(w.startswith("spaced hyphen:") for w in warnings), warnings)
        self.assertFalse(any("spaced hyphen" in e for e in errors), errors)


def put(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class WpHelpers(unittest.TestCase):
    CREDS = ("https://x.test", "u", "p")

    def test_destructive_ability_uses_delete_and_bracket_params(self):
        with mock.patch.object(fw, "wp", return_value={"ok": True}) as w:
            res = fw.ability_destructive(
                "ewpa/update-post-meta",
                {"post_id": 5, "meta_key": "_elementor_data", "meta_value": "[]"},
                creds=self.CREDS)
        self.assertEqual(res, {"ok": True})
        args, kw = w.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertTrue(args[1].startswith(
            "/wp-json/wp-abilities/v1/abilities/ewpa/update-post-meta/run?"))
        self.assertIn("input[post_id]=5", args[1])
        self.assertIn("input[meta_key]=_elementor_data", args[1])
        self.assertIn("input[meta_value]=%5B%5D", args[1])
        self.assertIsNone(kw.get("body"))
        self.assertEqual(kw.get("creds"), self.CREDS)

    def test_destructive_ability_quotes_values(self):
        with mock.patch.object(fw, "wp", return_value={}) as w:
            fw.ability_destructive("a/b", {"k": "x y&z=1"})
        self.assertIn("input[k]=x%20y%26z%3D1", w.call_args[0][1])

    def test_get_meta_reads_both_response_shapes(self):
        for res in ({"output": {"value": "builder"}}, {"value": "builder"}):
            with mock.patch.object(fw, "ability", return_value=res) as a:
                self.assertEqual(fw.get_meta(5, "_elementor_edit_mode"), "builder")
            self.assertEqual(a.call_args[0][0], "mc/get-meta")
            self.assertEqual(a.call_args[0][1],
                             {"post_id": 5, "meta_key": "_elementor_edit_mode"})

    def test_get_meta_real_flat_shape(self):
        # What mc/get-meta really answers: flat, field meta_value.
        res = {"id": 5, "type": "page", "status": "draft", "title": "T",
               "meta_key": "_elementor_edit_mode", "meta_value": "builder"}
        with mock.patch.object(fw, "ability", return_value=res):
            self.assertEqual(fw.get_meta(5, "_elementor_edit_mode"), "builder")

    def test_get_meta_structured_value_is_returned_unchanged(self):
        settings = {"hide_title": "yes", "nested": {"a": [1, 2]}}
        res = {"id": 5, "meta_key": "_elementor_page_settings",
               "meta_value": settings}
        with mock.patch.object(fw, "ability", return_value=res):
            got = fw.get_meta(5, "_elementor_page_settings")
        self.assertEqual(got, settings)
        self.assertIsInstance(got, dict)
        with mock.patch.object(fw, "ability", return_value={"output": {"meta_value": [1]}}):
            self.assertEqual(fw.get_meta(5, "k"), [1])

    def test_get_meta_null_or_absent_is_none(self):
        for res in ({"id": 5, "meta_key": "k", "meta_value": None},
                    {"id": 5, "meta_key": "k"}):
            with mock.patch.object(fw, "ability", return_value=res):
                self.assertIsNone(fw.get_meta(5, "k"))

    def test_get_meta_empty_meta_value_stays_empty(self):
        with mock.patch.object(fw, "ability",
                               return_value={"id": 5, "meta_key": "k", "meta_value": ""}):
            self.assertEqual(fw.get_meta(5, "k"), "")

    def test_get_meta_missing_is_none(self):
        with mock.patch.object(fw, "ability", return_value={"output": {"id": 5}}):
            self.assertIsNone(fw.get_meta(5, "_nope"))

    def test_get_meta_empty_string_stays_empty(self):
        with mock.patch.object(fw, "ability", return_value={"value": ""}):
            self.assertEqual(fw.get_meta(5, "_elementor_edit_mode"), "")

    def test_set_meta_goes_through_destructive_ability(self):
        with mock.patch.object(fw, "ability_destructive") as d:
            self.assertIsNone(fw.set_meta(5, "_elementor_edit_mode", "", creds=self.CREDS))
        d.assert_called_once_with(
            "ewpa/update-post-meta",
            {"post_id": 5, "meta_key": "_elementor_edit_mode", "meta_value": ""},
            creds=self.CREDS)

    def _home(self, html):
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return html.encode()
        return mock.patch.object(fw.urllib.request, "urlopen", return_value=R())

    def test_wpml_languages_from_hreflang(self):
        html = ('<link rel="alternate" hreflang="en" href="https://x/" />'
                '<link rel="alternate" hreflang="zh-HK" href="https://x/zh-hk/" />'
                '<link rel="alternate" hreflang="zh-TW" href="https://x/zh-hant/" />'
                '<link rel="alternate" hreflang="x-default" href="https://x/" />')
        with self._home(html) as u:
            self.assertEqual(fw.wpml_languages(creds=self.CREDS),
                             ["en", "zh-hk", "zh-hant"])
        req = u.call_args[0][0]
        self.assertEqual(req.full_url, "https://x.test/")
        self.assertEqual(req.get_header("User-agent"), fw.UA)
        self.assertIsNone(req.get_header("Authorization"))

    def test_wpml_languages_attribute_order_and_dedupe(self):
        html = ('<link href="https://x/fr/" hreflang="fr" rel="alternate">'
                '<link rel="alternate" href="https://x/fr/page/" hreflang="fr">'
                '<link rel="stylesheet" href="https://x/zh/s.css">')
        with self._home(html):
            self.assertEqual(fw.wpml_languages(creds=self.CREDS), ["fr"])


class PushElementor(unittest.TestCase):
    """push usage A: any WPML language, --translation-of, Elementor meta WPML
    copies onto a raw page, and the front-end check that it is really raw."""

    HTML = ('<!-- wp:html -->\n<div class="mc-page gehk-page"><p>a</p></div>\n'
            '<!-- /wp:html -->\n')
    RENDERED = '<div class="mc-page gehk-page"><p>a</p></div>'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)
        self.build = os.path.join(self.tmp, "build")
        self.out = os.path.join(self.build, "p")
        os.makedirs(self.out)
        put(os.path.join(self.out, "page.html"), self.HTML)
        self.calls = []
        self.rendered = self.RENDERED
        self.meta = ["", ""]          # successive get_meta answers
        for name, repl in (
                ("BUILD", self.build),
                ("wp_creds", lambda: ("https://x", "u", "p")),
                ("wpml_languages", lambda creds=None: ["en", "zh-hant", "zh-hk"]),
                ("wp", self.fake_wp), ("ability", self.fake_ability),
                ("get_meta", self.fake_get_meta), ("set_meta", self.fake_set_meta)):
            p = mock.patch.object(fw, name, repl)
            p.start()
            self.addCleanup(p.stop)

    def fake_wp(self, method, path, **kw):
        self.calls.append((method, path))
        if method == "GET":
            return {"id": 5, "slug": "p", "status": "draft", "link": "https://x/p/",
                    "title": {"raw": "T"}, "parent": 0,
                    "modified_gmt": "2026-01-01T00:00:00",
                    "content": {"raw": "old", "rendered": self.rendered}}
        return {"id": 9, "source_url": "https://x/u.png"}

    def fake_ability(self, name, payload, creds=None):
        self.calls.append((name, payload))
        return {"id": 5} if name in ("mc/create-localized-page",
                                     "ewpa/create-post") else {}

    def fake_get_meta(self, post_id, key, creds=None):
        self.calls.append(("get_meta", key))
        return self.meta.pop(0) if self.meta else ""

    def fake_set_meta(self, post_id, key, value, creds=None):
        self.calls.append(("set_meta", key, value))

    def args(self, **kw):
        a = dict(slug="p", title="T", parent=None, parent_path=None, page_slug=None,
                 post_id=None, language="zh-hant", force=False, no_webp=True,
                 post_type="page", category=[], region=[], translation_of=None)
        a.update(kw)
        return type("A", (), a)()

    def names(self):
        return [c[0] for c in self.calls]

    def push(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fw.cmd_push(self.args(**kw))
        return buf.getvalue()

    def wp_json(self, **extra):
        meta = {"post_id": 5, "post_type": "page",
                "modified_gmt": "2026-01-01T00:00:00"}
        meta.update(extra)
        put(os.path.join(self.out, "wp.json"), json.dumps(meta))

    def test_bad_language_dies_before_any_write(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.push(language="fr")
        self.assertIn("BAD_LANGUAGE", err.getvalue())
        self.assertIn("zh-hk", err.getvalue())        # lists what is available
        self.assertEqual(self.calls, [])

    def test_language_beyond_the_old_choices_is_accepted(self):
        self.push(language="zh-hk")
        create = [c for c in self.calls if c[0] == "mc/create-localized-page"][0]
        self.assertEqual(create[1]["language"], "zh-hk")

    def test_argparse_has_no_hardcoded_language_choices(self):
        with mock.patch.object(sys, "argv",
                               ["figma_to_wp", "push", "p", "--language", "zh-hk",
                                "--translation-of", "77"]), \
                mock.patch.object(fw, "cmd_push") as cp:
            fw.main()
        a = cp.call_args[0][0]
        self.assertEqual((a.language, a.translation_of), ("zh-hk", 77))

    def test_translation_of_reaches_create_localized_page(self):
        self.push(translation_of=77)
        create = [c for c in self.calls if c[0] == "mc/create-localized-page"][0]
        self.assertEqual(create[1]["translation_of"], 77)

    def test_no_translation_of_key_when_not_given(self):
        self.push()
        create = [c for c in self.calls if c[0] == "mc/create-localized-page"][0]
        self.assertNotIn("translation_of", create[1])

    def test_translation_of_on_update_dies_before_any_write(self):
        self.wp_json()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.push(title=None, translation_of=77)
        self.assertIn("TRANSLATION_OF_ON_UPDATE", err.getvalue())
        self.assertFalse([n for n in self.names()
                          if n in ("mc/set-post-html", "mc/create-localized-page")])

    def test_builder_meta_is_cleared_after_set_post_html(self):
        self.meta = ["builder", ""]
        out = self.push()
        n = self.names()
        self.assertLess(n.index("mc/set-post-html"), n.index("set_meta"))
        sets = [c for c in self.calls if c[0] == "set_meta"]
        self.assertEqual(sets, [("set_meta", "_elementor_edit_mode", ""),
                                ("set_meta", "_elementor_data", "[]")])
        self.assertIn("elementor  cleared (copied by WPML)", out)

    def test_plain_page_clears_nothing(self):
        out = self.push()
        self.assertNotIn("set_meta", self.names())
        self.assertNotIn("elementor  cleared", out)

    def test_update_of_builder_page_without_replace_is_refused(self):
        self.wp_json()
        self.meta = ["builder"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.push(title=None)
        self.assertIn("ELEMENTOR_PAGE", err.getvalue())
        self.assertIn("--replace", err.getvalue())
        self.assertNotIn("mc/set-post-html", self.names())
        self.assertNotIn("set_meta", self.names())
        self.assertFalse([c for c in self.calls if c[0] == "POST"])  # no upload
        self.assertFalse(os.path.exists(os.path.join(self.out, "backups")))

    def test_update_of_raw_page_still_goes_through(self):
        self.wp_json()
        self.push(title=None)
        self.assertIn("mc/set-post-html", self.names())
        self.assertNotIn("set_meta", self.names())

    def test_post_type_post_skips_elementor_handling(self):
        self.push(post_type="post")
        self.assertNotIn("get_meta", self.names())
        self.assertNotIn("set_meta", self.names())

    def test_still_elementor_when_rendered_lacks_wrapper(self):
        self.rendered = '<div class="elementor elementor-5"><p>a</p></div>'
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.push()
        self.assertIn("STILL_ELEMENTOR", err.getvalue())

    def test_still_elementor_when_edit_mode_not_empty(self):
        self.meta = ["", "builder"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.push()
        self.assertIn("STILL_ELEMENTOR", err.getvalue())

    def test_assert_raw_passes_on_empty_meta_and_wrapper(self):
        with mock.patch.object(fw, "get_meta", return_value=""):
            fw.assert_raw(5, self.RENDERED, "gehk-page")

    def test_assert_raw_dies_on_builder_meta(self):
        with mock.patch.object(fw, "get_meta", return_value="builder"), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            fw.assert_raw(5, self.RENDERED, "gehk-page")

    def test_assert_raw_without_wrapper_checks_meta_only(self):
        buf = io.StringIO()
        with mock.patch.object(fw, "get_meta", return_value=""), \
                contextlib.redirect_stdout(buf):
            fw.assert_raw(5, "<p>anything</p>", "")
        self.assertIn("wrapper", buf.getvalue())

    def test_page_wrapper_is_the_page_specific_class(self):
        self.assertEqual(fw.page_wrapper(self.out), "gehk-page")

    def test_page_wrapper_none_found(self):
        put(os.path.join(self.out, "page.html"), "<div><p>a</p></div>")
        self.assertEqual(fw.page_wrapper(self.out), "")

    def test_clear_elementor_data_flag(self):
        with mock.patch.object(fw, "set_meta") as sm:
            fw.clear_elementor(5, data=False)
            self.assertEqual([c.args[1:] for c in sm.call_args_list],
                             [("_elementor_edit_mode", "")])
            sm.reset_mock()
            fw.clear_elementor(5, data=True)
            self.assertEqual([c.args[1:] for c in sm.call_args_list],
                             [("_elementor_edit_mode", ""), ("_elementor_data", "[]")])

    def test_push_skips_manifest_entry_without_id(self):
        url = "https://x/elementor/thumbs/a.png"
        put(os.path.join(self.out, "page.html"),
            '<!-- wp:html -->\n<div class="mc-page gehk-page"><img src="assets/a.png"></div>\n'
            '<!-- /wp:html -->\n')
        put(os.path.join(self.out, "manifest.json"),
            json.dumps({"assets/a.png": {"id": None, "url": url}}))
        self.push()
        self.assertFalse([c for c in self.calls
                          if c[0] == "POST" and "/media" in c[1]])
        html = [c for c in self.calls if c[0] == "mc/set-post-html"][0][1]["content"]
        self.assertIn(f'src="{url}"', html)
        self.assertNotIn("assets/a.png", html)


def identity_page(title="Example", desc="About it", canon="https://x/example/",
                  alts=(("en", "https://x/example/"), ("zh-hk", "https://x/zh-hk/example/")),
                  body="", head=""):
    links = "".join(f'<link rel="alternate" hreflang="{c}" href="{h}" />' for c, h in alts)
    return (f'<html><head><title>{title}</title>'
            f'<meta name="description" content="{desc}">'
            f'<link rel="canonical" href="{canon}" />{links}{head}</head>'
            f'<body>{body}</body></html>')


class PushReplace(unittest.TestCase):
    """push usage B: --replace swaps a live Elementor page for the reviewed
    draft. Everything that touches the site is mocked."""

    ORIG, DRAFT = 50, 60
    STAMP = "2026-02-02T00:00:00"
    META = {"_elementor_edit_mode": "builder", "_elementor_data": '[{"id":"a"}]',
            "_elementor_page_settings": {"hide_title": "yes"},
            "_wp_page_template": "elementor_header_footer", "_elementor_version": "3.2"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)
        self.build = os.path.join(self.tmp, "build")
        self.out = os.path.join(self.build, "p")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "page.html"), "w") as fh:
            fh.write('<!-- wp:html -->\n<div class="mc-page gehk-page"><p>a</p></div>\n'
                     '<!-- /wp:html -->\n')
        self.write_design()
        self.write_wp_json()
        self.calls = []
        self.orig_mod = self.STAMP
        self.metas = {k: (dict(v) if isinstance(v, dict) else v)
                      for k, v in self.META.items()}
        self.fetched = [identity_page(body='<div class="elementor">x</div>'),
                        identity_page(body='<div class="mc-page gehk-page">a</div>')]
        self.backup_seen = None
        self.replacing_seen = None
        self.final = None             # fetch_live's final URL, when it differs
        self.fetch_error = None       # raised by the Nth fetch_live call (1-based)
        for name, repl in (
                ("BUILD", self.build),
                ("wp_creds", lambda: ("https://x", "u", "p")),
                ("wpml_languages", lambda creds=None: ["en", "zh-hant", "zh-hk"]),
                ("run_checks", lambda out, **kw: ([], [])),
                ("wp", self.fake_wp), ("ability", self.fake_ability),
                ("get_meta", self.fake_get_meta), ("set_meta", self.fake_set_meta),
                ("fetch_live", self.fake_fetch_live)):
            p = mock.patch.object(fw, name, repl)
            p.start()
            self.addCleanup(p.stop)

    def write_design(self, **source):
        src = {"type": "elementor", "post_id": self.ORIG, "url": "https://x/example/",
               "modified_gmt": self.STAMP}
        src.update(source)
        with open(os.path.join(self.out, "design.json"), "w") as fh:
            json.dump({"source": src, "texts": []}, fh)

    def write_wp_json(self, **extra):
        meta = {"post_id": self.DRAFT, "post_type": "page", "slug": "p-draft",
                "status": "draft", "modified_gmt": "2026-03-03T00:00:00"}
        meta.update(extra)
        meta = {k: v for k, v in meta.items() if v is not None}
        with open(os.path.join(self.out, "wp.json"), "w") as fh:
            json.dump(meta, fh)

    def backups(self):
        d = os.path.join(self.out, "backups")
        return sorted(f for f in os.listdir(d) if "elementor" in f) \
            if os.path.isdir(d) else []

    def note_write(self, what):
        # the first write must find the Elementor backup already on disk
        if self.backup_seen is None:
            self.backup_seen = self.backups()
            self.replacing_seen = self.wp_json().get("replacing")
        self.calls.append(what)

    def fake_wp(self, method, path, **kw):
        if method == "GET":
            self.calls.append((method, path))
            return {"id": self.ORIG, "slug": "example", "status": "publish",
                    "link": "https://x/example/", "title": {"raw": "Example"},
                    "parent": 0, "modified_gmt": self.orig_mod,
                    "content": {"raw": "<p>old</p>", "rendered": "<p>old</p>"}}
        self.note_write((method, path))
        return {"id": 9, "source_url": "https://x/u.png"}

    def fake_ability(self, name, payload, creds=None):
        self.note_write((name, payload))
        return {}

    def fake_get_meta(self, post_id, key, creds=None):
        self.calls.append(("get_meta", key))
        return self.metas.get(key)

    def fake_set_meta(self, post_id, key, value, creds=None):
        self.note_write(("set_meta", key, value))
        self.metas[key] = value

    def fake_fetch_live(self, url):
        self.calls.append(("fetch_live", url))
        if self.fetch_error == len([c for c in self.calls if c[0] == "fetch_live"]):
            raise urllib.error.HTTPError(url, 500, "boom", {}, None)
        return self.final or url, self.fetched.pop(0)

    def args(self, **kw):
        a = dict(slug="p", title=None, parent=None, parent_path=None, page_slug=None,
                 post_id=None, language="en", force=False, no_webp=True,
                 post_type="page", category=[], region=[], translation_of=None,
                 replace=self.ORIG)
        a.update(kw)
        return type("A", (), a)()

    def names(self):
        return [c[0] for c in self.calls]

    def replace(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fw.cmd_push(self.args(**kw))
        return buf.getvalue()

    def dies(self, code, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            fw.cmd_push(self.args(**kw))
        self.assertIn(code, err.getvalue())
        return err.getvalue()

    def no_writes(self):
        self.assertFalse([c for c in self.calls
                          if c[0] in ("mc/set-post-html", "set_meta", "POST", "PUT",
                                      "DELETE") or c[0].startswith("ewpa/")])
        self.assertEqual(self.backups(), [])

    def wp_json(self):
        with open(os.path.join(self.out, "wp.json")) as fh:
            return json.load(fh)

    def test_replace_refuses_without_draft(self):
        self.write_wp_json(post_id=None)
        self.dies("REPLACE_NO_DRAFT")
        self.no_writes()

    def test_replace_refuses_when_draft_is_original(self):
        self.write_wp_json(post_id=self.ORIG)
        self.dies("REPLACE_DRAFT_IS_ORIGINAL")
        self.no_writes()

    def test_replace_refuses_when_already_replaced(self):
        self.write_wp_json(replaced={"draft": 1, "backup": "x"})
        self.dies("ALREADY_REPLACED")
        self.no_writes()

    def test_replace_refuses_other_page_than_the_source(self):
        self.write_design(post_id=999)
        self.dies("REPLACE_WRONG_PAGE")
        self.no_writes()

    def test_replace_refuses_when_original_moved_on(self):
        self.orig_mod = "2026-04-04T00:00:00"
        self.dies("PAGE_MOVED_ON")
        self.no_writes()

    def test_replace_moved_on_message_does_not_offer_force(self):
        self.orig_mod = "2026-04-04T00:00:00"
        self.assertNotIn("--force", self.dies("PAGE_MOVED_ON"))

    def test_replace_refuses_args_that_change_identity_or_skip_checks(self):
        for kw in ({"title": "T"}, {"parent": 3}, {"parent_path": "a/b"},
                   {"page_slug": "x"}, {"force": True}):
            self.dies("REPLACE_ARG", **kw)
        self.assertEqual(self.calls, [])

    def test_replace_never_regenerates_the_permalink(self):
        self.replace()
        self.assertNotIn("mc/regenerate-permalink", self.names())

    def test_replace_records_replacing_before_the_first_write(self):
        out = self.replace()
        self.assertEqual(self.replacing_seen["orig"], self.ORIG)
        self.assertRegex(self.replacing_seen["backup"], r"^backups/50-elementor-\d+\.json$")
        self.assertIn("elementor backup", out)
        self.assertIn("elementor-restore", out)
        self.assertEqual(self.wp_json().get("replacing"), None)    # finished: gone
        self.assertEqual(self.wp_json()["post_id"], self.ORIG)

    def test_replace_unfinished_leaves_replacing_and_rerun_refuses(self):
        self.fetched[1] = identity_page(body='<div class="elementor">x</div>')
        self.dies("STILL_ELEMENTOR")
        w = self.wp_json()
        self.assertEqual(w["post_id"], self.DRAFT)                  # untouched
        self.assertEqual(w["replacing"]["orig"], self.ORIG)
        self.calls.clear()
        self.fetched = [identity_page(), identity_page()]
        err = self.dies("REPLACE_UNFINISHED")
        self.assertIn(w["replacing"]["backup"], err)
        self.assertIn("elementor-restore", err)
        self.assertEqual(self.calls, [])                            # nothing read or written

    def test_replace_dies_cleanly_when_the_recheck_fetch_fails(self):
        self.fetch_error = 2
        err = self.dies("IDENTITY_CHANGED")
        self.assertIn("-elementor-", err)
        self.assertNotIn(("DELETE", f"/wp-json/wp/v2/pages/{self.DRAFT}"), self.calls)

    def test_replace_refuses_an_original_that_redirects(self):
        self.final = "https://x/elsewhere/"
        self.dies("ORIGINAL_REDIRECTS")
        self.no_writes()

    def test_trailing_slash_difference_is_not_a_redirect(self):
        self.final = "https://x/example?fwcb=1"
        self.replace()

    def test_replace_refuses_to_back_up_what_it_cannot_restore(self):
        self.metas["_elementor_data"] = "[]"
        self.dies("BACKUP_FAILED")
        self.no_writes()
        self.metas.update(_elementor_data='[{"id":"a"}]', _elementor_edit_mode="")
        self.dies("BACKUP_FAILED")
        self.no_writes()
        self.assertNotIn("replacing", self.wp_json())

    def test_replace_refuses_with_post_id_or_translation_of(self):
        self.dies("REPLACE_CONFLICT", post_id=7)
        self.dies("REPLACE_CONFLICT", translation_of=7)
        self.no_writes()

    def test_replace_backs_up_before_any_write(self):
        self.replace()
        self.assertEqual(len(self.backup_seen), 1)
        path = os.path.join(self.out, "backups", self.backup_seen[0])
        self.assertTrue(self.backup_seen[0].startswith(f"{self.ORIG}-elementor-"))
        with open(path) as fh:
            b = json.load(fh)
        self.assertEqual((b["post_id"], b["modified_gmt"], b["content_raw"]),
                         (self.ORIG, self.STAMP, "<p>old</p>"))
        for k, v in self.META.items():
            self.assertEqual(b["meta"][k], v)              # dict stays a dict
        self.assertEqual(set(b["meta"]), set(fw.ELEMENTOR_META))
        self.assertIsNone(b["meta"]["_elementor_css"])     # absent stays null

    def test_replace_clears_edit_mode_only(self):
        self.replace()
        sets = [c for c in self.calls if c[0] == "set_meta"]
        self.assertEqual(sets, [("set_meta", "_elementor_edit_mode", "")])

    def test_replace_writes_the_normal_push_body_to_the_original(self):
        self.replace()
        body = [c for c in self.calls if c[0] == "mc/set-post-html"][0][1]
        self.assertEqual(body["post_id"], self.ORIG)
        self.assertIn("gehk-page", body["content"])
        self.assertIn(("POST", f"/wp-json/wp/v2/pages/{self.ORIG}"), self.calls)  # page js
        self.assertFalse([c for c in self.calls if c[0] == "mc/create-localized-page"])

    def test_replace_dies_on_identity_change(self):
        self.fetched[1] = identity_page(title="Other", body='<div class="gehk-page">a</div>')
        err = self.dies("IDENTITY_CHANGED")
        self.assertIn("elementor-restore", err)
        self.assertIn("-elementor-", err)           # the Elementor backup, not the .html
        self.assertIn("title", err)
        self.assertNotIn(("DELETE", f"/wp-json/wp/v2/pages/{self.DRAFT}"), self.calls)
        self.assertEqual(self.wp_json()["post_id"], self.DRAFT)

    def test_replace_dies_on_lost_hreflang(self):
        self.fetched[1] = identity_page(alts=(("en", "https://x/example/"),),
                                        body='<div class="gehk-page">a</div>')
        self.assertIn("hreflang", self.dies("IDENTITY_CHANGED"))

    def test_replace_dies_when_front_end_still_elementor(self):
        self.fetched[1] = identity_page(body='<div class="elementor">x</div>')
        self.dies("STILL_ELEMENTOR")
        self.assertNotIn(("DELETE", f"/wp-json/wp/v2/pages/{self.DRAFT}"), self.calls)

    def test_replace_trashes_draft_and_rewrites_wp_json(self):
        self.replace()
        n = self.calls
        purge = ("PUT", "/wp-json/siteground-optimizer/v1/purge-cache")
        trash = ("DELETE", f"/wp-json/wp/v2/pages/{self.DRAFT}")   # no ?force
        self.assertIn(purge, n)
        self.assertIn(trash, n)
        self.assertLess(n.index(("set_meta", "_elementor_edit_mode", "")), n.index(purge))
        self.assertLess(n.index(purge), [i for i, c in enumerate(n)
                                         if c[0] == "fetch_live"][1])
        self.assertLess(n.index(purge), n.index(trash))
        w = self.wp_json()
        self.assertEqual(w["post_id"], self.ORIG)
        self.assertEqual(w["replaced"]["draft"], self.DRAFT)
        self.assertRegex(w["replaced"]["backup"], r"^backups/50-elementor-\d+\.json$")
        self.assertTrue(os.path.isfile(os.path.join(self.out, w["replaced"]["backup"])))
        self.assertNotIn("replacing", w)
        self.assertEqual(w["modified_gmt"], self.orig_mod)
        self.assertEqual(w["slug"], "example")

    def test_replace_front_end_fetch_is_cache_busted_after_purge(self):
        self.replace()
        urls = [c[1] for c in self.calls if c[0] == "fetch_live"]
        for u in urls:        # the before fetch is busted too
            self.assertRegex(u, r"^https://x/example/\?fwcb=\d+$")

    def test_replace_tolerates_a_style_block_before_the_json(self):
        real = self.fake_wp

        def styled(method, path, **kw):
            r = real(method, path, **kw)
            return "<style>.x{}</style>" + json.dumps(r) if method == "GET" else r
        with mock.patch.object(fw, "wp", styled):
            self.replace()
        self.assertEqual(self.wp_json()["post_id"], self.ORIG)

    def test_backup_failure_stops_everything(self):
        with open(os.path.join(self.out, "backups"), "w") as fh:
            fh.write("a file, not a folder")
        self.dies("BACKUP_FAILED")
        self.assertNotIn("mc/set-post-html", self.names())
        self.assertNotIn("set_meta", self.names())

    def test_backup_elementor_dies_when_read_back_differs(self):
        real = json.load

        def tampered(fh):
            d = real(fh)
            d["content_raw"] = "different"
            return d
        with mock.patch.object(fw, "wp", self.fake_wp), \
                mock.patch.object(fw.json, "load", tampered), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            fw.backup_elementor(self.ORIG, self.out, creds=("https://x", "u", "p"))
        self.assertIn("BACKUP_FAILED", err.getvalue())

    def test_public_identity_reads_title_description_hreflang_canonical(self):
        self.fetched = [identity_page()]
        self.assertEqual(fw.public_identity("https://x/example/"), {
            "title": "Example", "description": "About it",
            "hreflang": {"en": "https://x/example/",
                         "zh-hk": "https://x/zh-hk/example/"},
            "canonical": "https://x/example/"})

    def test_plain_update_of_builder_page_still_refused(self):
        self.write_wp_json(post_id=self.ORIG, modified_gmt=self.STAMP)
        self.dies("ELEMENTOR_PAGE", replace=None)
        self.no_writes()

    def test_argparse_replace_is_an_int(self):
        with mock.patch.object(sys, "argv", ["figma_to_wp", "push", "p", "--replace", "50"]), \
                mock.patch.object(fw, "cmd_push") as cp:
            fw.main()
        self.assertEqual(cp.call_args[0][0].replace, 50)


MINI = os.path.join(HERE, "fixtures", "elementor-mini")


def mini(name):
    with open(os.path.join(MINI, name), encoding="utf-8") as f:
        return f.read()


LIVE = mini("live.html")


class ElementorPageDoc(unittest.TestCase):
    def test_page_doc_skips_header_and_footer(self):
        s, e = el.page_doc(LIVE, 1234)
        seg = LIVE[s:e]
        self.assertIn("elementor-widget-heading", seg)
        self.assertNotIn("elementor-widget-social-icons", seg)   # footer
        self.assertNotIn("elementor-widget-image\" data-id=\"h1c3", seg)  # header
        self.assertNotIn("Popup copy", seg)

    def test_page_doc_other_id_is_none(self):
        # A translation that still renders the original's doc has another id.
        self.assertIsNone(el.page_doc(LIVE, 4321))
        self.assertIsNone(el.page_doc("<p>no docs</p>", 1234))

    def test_page_doc_accepts_wp_post_and_last_doc(self):
        html = ('<div data-elementor-type="header" data-elementor-id="1" class="elementor"><p>h</p></div>'
                '<div data-elementor-type="wp-post" data-elementor-id="7" class="elementor"><p>body</p></div>'
                '<script>x</script>')
        s, e = el.page_doc(html, 7)
        self.assertIn("body", html[s:e])
        self.assertNotIn("<script>", html[s:e])   # stops where the doc closes

    def test_selector_string_in_script_is_not_a_doc(self):
        html = ('<script>q(\'[data-elementor-type="wp-page"]\')</script>'
                '<div data-elementor-type="wp-page" data-elementor-id="7"><p>ok</p></div>')
        s, e = el.page_doc(html, 7)
        self.assertTrue(html[s:].startswith("<div data-elementor-type"))


class ElementorInventory(unittest.TestCase):
    def setUp(self):
        self.inv = el.inventory(LIVE, 1234, mini("elementor-data.json"))

    def test_tiers_and_popups(self):
        inv = self.inv
        self.assertEqual(inv["post_id"], 1234)
        self.assertIn("elementskit-funfact", inv["tier3"])
        self.assertEqual(inv["tier3"], ["elementskit-funfact"])
        self.assertEqual(inv["popups"], [900])        # header's 777 and footer's 901 are not the page's
        self.assertEqual(inv["posts_queries"][0]["posts_per_page"], 3)
        self.assertTrue(inv["flags"]["hidden"]["mobile"])
        self.assertFalse(inv["flags"]["hidden"]["tablet"])
        self.assertFalse(inv["flags"]["hidden"]["desktop"])

    def test_widgets_carry_type_tier_section(self):
        w = {(x["type"], x["tier"], x["section"]) for x in self.inv["widgets"]}
        self.assertIn(("heading", 1, "s1"), w)
        self.assertIn(("image", 1, "s1"), w)
        self.assertIn(("nested-tabs", 2, "s2"), w)
        self.assertIn(("button", 1, "s2"), w)
        self.assertIn(("posts", 2, "s3"), w)             # ".classic" skin stripped
        self.assertIn(("elementskit-funfact", 3, "s3"), w)
        self.assertNotIn("social-icons", {x["type"] for x in self.inv["widgets"]})

    def test_flags_and_template(self):
        f = self.inv["flags"]
        self.assertTrue(f["motion_fx"])
        self.assertFalse(f["sticky"])                    # sticky is on the header, not the page
        self.assertTrue(f["hubspot"])
        self.assertEqual(self.inv["template"], "elementor_header_footer")

    def test_sticky_flag_and_section_wrap(self):
        html = ('<body class="page-template-default"><div data-elementor-type="wp-page" data-elementor-id="5">'
                '<div class="elementor-section-wrap">'
                '<section class="elementor-section elementor-top-section" data-element_type="section" '
                'data-settings="{&quot;sticky&quot;:&quot;top&quot;}"><div class="elementor-container">'
                '<div class="elementor-column" data-element_type="column">'
                '<div class="elementor-widget" data-element_type="widget" data-widget_type="icon.default"></div>'
                '</div></div></section>'
                '<section class="elementor-section elementor-top-section" data-element_type="section">'
                '<div class="elementor-container"><div class="elementor-column" data-element_type="column">'
                '<section class="elementor-section elementor-inner-section" data-element_type="section">'
                '<div class="elementor-widget" data-element_type="widget" data-widget_type="divider.default"></div>'
                '</section></div></div></section>'
                '</div></div></body>')
        inv = el.inventory(html, 5)
        self.assertTrue(inv["flags"]["sticky"])
        self.assertFalse(inv["flags"]["motion_fx"])
        self.assertEqual([(w["type"], w["section"]) for w in inv["widgets"]],
                         [("icon", "s1"), ("divider", "s2")])   # inner section is not a new top section
        self.assertEqual(inv["template"], "default")

    def test_popup_ids_dedupe_pad_and_skip_junk(self):
        import base64
        b = lambda s: base64.b64encode(s.encode()).decode().replace("=", "%3D")
        link = lambda s: ('<a href="#elementor-action%3Aaction%3Dpopup%3Aopen%26settings%3D' + s + '">x</a>')
        html = ('<div data-elementor-type="wp-page" data-elementor-id="5">'
                + link(b('{"id":"31"}')) + link(b('{"id":"9","toggle":false}')) + link(b('{"id":"31"}'))
                + link("!!not-base64!!") + link(b('{"nope":1}')) + '</div>')
        self.assertEqual(el.inventory(html, 5)["popups"], [31, 9])

    def test_posts_queries_from_json_string_skin_and_empty(self):
        data = [{"elType": "container", "elements": [
            {"elType": "widget", "widgetType": "posts", "elements": [],
             "settings": {"_skin": "cards", "cards_posts_per_page": 4, "classic_posts_per_page": 6,
                          "posts_post_type": "post", "posts_include_term_ids": ["7"]}},
            {"elType": "widget", "widgetType": "heading", "settings": {"title": "x"}, "elements": []}]}]
        q = el.inventory(LIVE, 1234, json.dumps(data))["posts_queries"]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["posts_per_page"], 4)     # the active skin's count wins
        self.assertEqual(q[0]["posts_include_term_ids"], ["7"])
        self.assertNotIn("cards_posts_per_page", q[0])
        self.assertEqual(el.inventory(LIVE, 1234)["posts_queries"], [])
        self.assertEqual(el.inventory(LIVE, 1234, "not json")["posts_queries"], [])
        self.assertEqual(el.inventory(LIVE, 1234, "[]")["posts_queries"], [])

    def test_no_doc_gives_empty_inventory(self):
        inv = el.inventory(LIVE, 4321)
        self.assertEqual(inv["widgets"], [])
        self.assertEqual(inv["popups"], [])

    def test_tier_tables(self):
        self.assertIn("heading", el.TIER1)
        self.assertIn("social-icons", el.TIER1)
        self.assertIn("nested-tabs", el.TIER2)
        self.assertIn("posts", el.TIER2)
        self.assertFalse(el.TIER1 & el.TIER2)


class ElementorResponsiveRules(unittest.TestCase):
    def test_responsive_rules_split_by_breakpoint(self):
        r = el.responsive_rules(mini("post-1234.css"))
        self.assertEqual(sorted(r), ["1024", "767"])
        self.assertEqual(len(r["1024"]), 2)
        self.assertEqual(len(r["767"]), 2)
        self.assertIn("padding:40px 20px", r["1024"][0])
        self.assertIn("font-size:30px", r["767"][1])
        self.assertFalse([x for k in r for x in r[k] if "background-color" in x])   # unrelated rule left out

    def test_only_plain_max_width_blocks(self):
        css = ("/* c */@media (min-width:768px) and (max-width:1024px){.a{x:1}}"
               "@media screen and (max-width: 1024px){.b{ y:2 }.c{z:3}}"
               "@media(max-width:1024px){.d{w:4}}@media(max-width:480px){.e{v:5}}"
               "@media(max-width:767px){@supports (display:grid){.f{u:6}}}")
        r = el.responsive_rules(css)
        self.assertEqual(r["1024"], [".b{ y:2 }", ".c{z:3}", ".d{w:4}"])
        self.assertEqual(len(r["767"]), 1)
        self.assertEqual(el.responsive_rules(""), {"1024": [], "767": []})


class ElementorSnapshot(unittest.TestCase):
    def snap(self, js="/*measure*/"):
        return el.snapshot(LIVE, "https://example.test", 1234, js)

    def test_snapshot_unlazies_images(self):
        s = self.snap()
        img = [t for t in __import__("re").findall(r"<img\b[^>]*>", s) if "hero" in t][0]
        self.assertIn('src="/wp-content/uploads/hero.png"', img)
        self.assertIn('srcset="/wp-content/uploads/hero-300.png 300w', img)
        self.assertNotIn("loading=", img)
        self.assertNotIn("data:image/gif", img)

    def test_unlazy_data_lazy_src_and_no_src(self):
        html = LIVE.replace('<img width="640"', '<img data-lazy-src="https://example.test/z.png" loading=lazy width="1"', 1)
        s = el.snapshot(html, "https://example.test", 1234, "")
        self.assertIn('src="/z.png"', s)

    def test_snapshot_marks_root_and_sections(self):
        s = self.snap()
        self.assertEqual(len(re.findall(r"<[^>]*\sdata-fw-root[\s>=]", s)), 1)
        self.assertIn('<div data-fw-root data-elementor-type="wp-page" data-elementor-id="1234"', s)
        for n in (1, 2, 3):
            self.assertEqual(s.count('data-section="s%d"' % n), 1, n)
        self.assertNotIn('data-section="s4"', s)
        # s2 is the e-con container, s1 and s3 legacy sections, never the header or footer
        self.assertIn('<div data-section="s2" class="e-con e-parent', s)
        self.assertNotRegex(s, r'data-section="[^"]*"[^>]*elementor-element-(?:h1a1|f1a1|p9a1)')

    def test_snapshot_relativizes_origin_urls(self):
        s = self.snap()
        self.assertNotIn('href="https://example.test', s)
        self.assertIn('href="/wp-content/uploads/elementor/css/post-1234.css"', s)
        self.assertIn('src="/wp-content/uploads/logo.png"', s)
        self.assertNotIn("example.test.evil", s)
        t = el.snapshot(LIVE.replace("Example heading", '<a href="https://example.test">h</a> https://example.testing/x'),
                        "https://example.test/", 1234, "")
        self.assertIn('<a href="/">h</a>', t)
        self.assertIn("https://example.testing/x", t)   # another host is left alone

    def test_snapshot_injects_style_and_script(self):
        s = self.snap("window.__m=1;")
        self.assertEqual(s.count("<style data-fw>"), 1)
        style = s.split("<style data-fw>")[1].split("</style>")[0]
        self.assertIn("#hs-eu-cookie-confirmation", style)
        self.assertIn('data-elementor-id="1234"', style)
        self.assertIn("display:none", style.replace(" ", ""))
        self.assertIn(".elementor-invisible", style)
        self.assertIn("visibility:visible", style.replace(" ", ""))
        self.assertTrue(s.index("<style data-fw>") < s.index("</head>"))
        self.assertTrue(s.index("window.__m=1;") < s.rindex("</body>"))
        self.assertRegex(s, r"<script>window\.__m=1;</script>\s*</body>")

    def test_snapshot_marks_containers_lazyloaded(self):
        # Elementor holds a container's background back until JS adds this class.
        s = self.snap()
        self.assertRegex(s, r'<div data-section="s2" class="e-con e-parent[^"]*e-lazyloaded')

    def test_snapshot_without_page_doc_raises(self):
        with self.assertRaises(ValueError):
            el.snapshot(LIVE, "https://example.test", 4321, "")

    def test_snapshot_handles_missing_head_and_body(self):
        html = '<div data-elementor-type="wp-page" data-elementor-id="5"><section data-element_type="section"></section></div>'
        s = el.snapshot(html, "https://example.test", 5, "J")
        self.assertIn("<style data-fw>", s)
        self.assertIn("<script>J</script>", s)
        self.assertIn('data-section="s1"', s)


class ElementorServeLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "live.html"), "w") as f:
            f.write("<p>local snapshot</p>")
        self.srv = el.serve_live(self.tmp, "https://example.test")
        self.addCleanup(fw.stop_server, self.srv)

    def get(self, path):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()
        finally:
            c.close()

    def test_live_html_is_local_and_never_proxied(self):
        with mock.patch("urllib.request.urlopen") as uo:
            status, ctype, body = self.get("/live.html?x=1")
        self.assertEqual((status, body), (200, b"<p>local snapshot</p>"))
        self.assertIn("text/html", ctype)
        uo.assert_not_called()

    def test_serve_live_proxies_other_paths(self):
        resp = mock.MagicMock()
        resp.read.return_value = b"a{color:red}"
        resp.status = 200
        resp.headers.get.return_value = "text/css"
        with mock.patch("urllib.request.urlopen", return_value=resp) as uo:
            status, ctype, body = self.get("/wp-content/x.css?ver=3")
        self.assertEqual((status, ctype, body), (200, "text/css", b"a{color:red}"))
        req = uo.call_args.args[0]
        self.assertEqual(req.full_url, "https://example.test/wp-content/x.css?ver=3")
        self.assertEqual(req.get_header("User-agent"), el.UA)

    def test_upstream_error_status_is_passed_through(self):
        import urllib.error
        err = urllib.error.HTTPError("https://example.test/x", 404, "nf", {"Content-Type": "text/plain"},
                                     io.BytesIO(b"nope"))
        self.addCleanup(err.close)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            status, ctype, body = self.get("/missing")
        self.assertEqual((status, ctype, body), (404, "text/plain", b"nope"))

    def test_unreachable_origin_is_502(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            status, _, _ = self.get("/x")
        self.assertEqual(status, 502)


@unittest.skipUnless(any(os.path.exists(c) for c in fw.CHROME), "Chrome not installed")
class MeasureJsInSnapshot(unittest.TestCase):
    """Run the real measure.js over a snapshot in headless Chrome."""

    def test_inner_section_runs_are_labelled_with_the_top_section(self):
        html = ('<html><head></head><body>'
                '<div data-elementor-type="wp-page" data-elementor-id="5">'
                '<section class="elementor-section elementor-top-section" data-element_type="section">'
                '<div class="elementor-container"><div class="elementor-column" data-element_type="column">'
                '<section class="elementor-section elementor-inner-section" data-element_type="section">'
                '<div style="height:120px"><p>Inside the inner section</p></div></section>'
                '</div></div></section></div></body></html>')
        with open(os.path.join(HERE, "..", "scripts", "measure.js"), encoding="utf-8") as f:
            js = f.read()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "live.html"), "w", encoding="utf-8") as f:
            f.write(el.snapshot(html, "https://example.test", 5, js))
        srv = fw.serve_dir(tmp)
        self.addCleanup(fw.stop_server, srv)
        dom = fw.dump_dom("http://127.0.0.1:%d/live.html" % srv.server_address[1], width=800)
        m = re.search(r'<script type="application/json" id="fw-measure">(.*?)</script>', dom, re.S)
        self.assertTrue(m, "no fw-measure block in the DOM")
        data = json.loads(m.group(1))
        self.assertEqual([x["name"] for x in data["sections"]], ["s1"])
        run = [r for r in data["runs"] if "inner section" in r["text"]][0]
        self.assertEqual(run["sec"], "s1")


class MeasureJs(unittest.TestCase):
    def test_root_and_fonts(self):
        with open(os.path.join(HERE, "..", "scripts", "measure.js")) as f:
            js = f.read()
        self.assertIn("document.querySelector('[data-fw-root]')||document.querySelector('.mc-page')||document.body", js)
        self.assertIn("fonts:", js)
        self.assertIn("status==='loaded'", js)
        # a second run replaces the first report instead of adding another
        self.assertIn("getElementById('fw-measure')", js)
        self.assertIn("getElementById('fw-measure')", fw.MOBILE_WRAP)



def _measure_dom(runs, root, sections=(), fonts=("Roboto",), vw=1440, small=()):
    data = {"runs": runs, "sections": list(sections), "boxes": [], "fontOK": {},
            "fonts": list(fonts), "root": root,
            "mobile": {"vw": vw, "scrollW": vw, "over": [], "small": list(small), "tap": [],
                       "lefts": {}, "imgs": []}}
    return ('<html><body><script type="application/json" id="fw-measure">%s'
            '</script></body></html>' % json.dumps(data))


def _run(text, x, y, **kw):
    r = {"text": text, "x": x, "y": y, "w": 300, "h": 40, "size": 32.0,
         "lh": "41.6px", "ls": "normal", "family": "Roboto", "weight": "700",
         "color": "rgb(17, 17, 17)", "align": "left", "tag": "h1", "sec": "s1",
         "clipped": False}
    r.update(kw)
    return r


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _size(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


class ElementorPinVh(unittest.TestCase):
    def test_pin_vh_rewrites_numbers_not_names(self):
        css = (".hero{min-height:96vh;height:calc(100vh - 80px);top:-5vh}"
               ".h-100vh{margin:.5vh}.x{height:50dvh}")
        out = el.pin_vh(css, 1000)
        self.assertIn("min-height:960px", out)
        self.assertIn("calc(1000px - 80px)", out)
        self.assertIn("top:-50px", out)
        self.assertIn(".h-100vh{margin:5px}", out)   # the class name is left alone
        self.assertIn("height:500px", out)
        self.assertNotIn("vh;", out)

    def test_pin_vh_html_touches_styles_only(self):
        html = ('<style>.a{height:100vh}</style><div style="min-height: 80vh">'
                '<p>Fill 100vh of the screen</p></div>')
        out = el.pin_vh_html(html, 1000)
        self.assertIn(".a{height:1000px}", out)
        self.assertIn('style="min-height: 800px"', out)
        self.assertIn("Fill 100vh of the screen", out)

    def test_serve_live_pins_vh_in_proxied_css_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        _write(os.path.join(tmp, "live.html"), "<p>x</p>")
        srv = el.serve_live(tmp, "https://example.test", viewport_h=1000)
        self.addCleanup(fw.stop_server, srv)

        def resp(body, ctype):
            r = mock.MagicMock()
            r.read.return_value = body
            r.status = 200
            r.headers.get.return_value = ctype
            return r
        import http.client

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            try:
                c.request("GET", path)
                return c.getresponse().read()
            finally:
                c.close()
        with mock.patch("urllib.request.urlopen",
                        return_value=resp(b".h{height:96vh}", "text/css; charset=utf-8")):
            self.assertEqual(get("/a.css"), b".h{height:960px}")
        with mock.patch("urllib.request.urlopen",
                        return_value=resp(b"var h='96vh'", "application/javascript")):
            self.assertEqual(get("/a.js"), b"var h='96vh'")


class ElementorDocCss(unittest.TestCase):
    def test_doc_css_keeps_one_documents_rules_and_their_media(self):
        css = ("/* .elementor-5 in a comment */.elementor-5 .a{x:1}.elementor-50 .b{x:2}"
               "@font-face{font-family:F}.elementor-9 .c{x:3}"
               "@media(max-width:767px){.elementor-9 .d{x:4}.elementor-5 .e{x:5}}"
               "@media(max-width:1024px){.elementor-9 .f{x:6}}")
        out = el.doc_css(css, 5)
        self.assertEqual(out, ".elementor-5 .a{x:1}\n@media(max-width:767px){.elementor-5 .e{x:5}}")
        self.assertEqual(el.responsive_rules(out)["767"], [".elementor-5 .e{x:5}"])


class ExtractFixture(unittest.TestCase):
    """extract --from elementor: every network and browser call mocked."""

    URL = "https://example.test/example/"
    THUMB = "https://example.test/wp-content/uploads/elementor/thumbs/t-abc.png"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.build = os.path.join(self.tmp, "build")
        self.out = os.path.join(self.build, "ex")
        self.html = LIVE
        self.final = None                 # fetch_live's final URL, when it differs
        self.status = "publish"
        self.post_id = 1234
        self.meta = {"_elementor_edit_mode": "builder",
                     "_elementor_data": mini("elementor-data.json")}
        self.media = {"hero": [{"id": 55, "source_url":
                                "https://example.test/wp-content/uploads/hero.png"}]}
        self.calls, self.dumps, self.shots = [], [], []
        self.grow = {}        # dump_dom window width -> px the page grows by on the shot run
        self.no_ink = set()   # dump_dom window widths whose shot has nothing where the text is
        for name, repl in (
                ("BUILD", self.build),
                ("wp_creds", lambda: ("https://example.test", "u", "p")),
                ("wp", self.fake_wp), ("get_meta", self.fake_get_meta),
                ("fetch_live", self.fake_fetch_live), ("http_get", self.fake_http_get),
                ("shoot", self.fake_shoot), ("dump_dom", self.fake_dump_dom),
                ("_framed", self.fake_framed)):
            p = mock.patch.object(fw, name, repl)
            p.start()
            self.addCleanup(p.stop)
        # serve_live and _framed are covered on their own; real servers here
        # only cost half a second each to shut down.
        srv = mock.MagicMock(server_port=1)
        p = mock.patch.object(el, "serve_live", return_value=srv)
        self.serve_live = p.start()
        self.addCleanup(p.stop)

    @contextlib.contextmanager
    def fake_framed(self, url, width, height):
        yield "http://127.0.0.1:2/frame.html?w=%d" % width

    def fake_wp(self, method, path, **kw):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("extract must not write: %s %s" % (method, path))
        if path.startswith("/wp-json/wp/v2/media"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            return self.media.get(q["search"][0], [])
        return {"id": self.post_id, "slug": "example", "status": self.status,
                "link": self.URL, "modified_gmt": "2026-01-02T03:04:05",
                "type": "page", "title": {"raw": "Example"}}

    def fake_get_meta(self, post_id, key, creds=None):
        self.calls.append(("get_meta", key))
        return self.meta.get(key)

    def fake_fetch_live(self, url):
        self.calls.append(("fetch_live", url))
        return (self.final or url), self.html

    def fake_http_get(self, url, timeout=60):
        self.calls.append(("http_get", url))
        if url.endswith("post-1234.css"):
            return url, mini("post-1234.css").encode()
        return url, b"\x89PNG fake " + url.encode()

    def fake_shoot(self, url, png, width, tall=14000):
        self.shots.append((url, os.path.basename(png), width))
        raise AssertionError("the references come out of the measuring run")

    def fake_dump_dom(self, url, width=1440, timeout=180, tall=20000, budget=6000, png=None):
        from PIL import Image
        self.dumps.append({"url": url, "width": width, "tall": tall, "budget": budget,
                           "png": os.path.basename(png) if png else None})
        grow = self.grow.get(width, 0) if png else 0
        if width == 1440:
            runs = [_run("Example heading", 40, 300),
                    _run("Contact us", 60, 900, tag="span", size=16.0, lh="normal",
                         ls="0.5px", weight="400", sec="s2", align="start")]
            root = {"x": 0, "y": 100, "w": 1440, "h": 2000 + grow}
            secs = [{"name": "s1", "y": 100, "h": 600}, {"name": "s2", "y": 700, "h": 400}]
            vw = 1440
        else:
            vw = {888: 768, 800: 390}[width]
            runs = [_run("Example heading", 16, 140, size=30.0)]
            root = {"x": 0, "y": 20, "w": vw, "h": 2400 + grow}
            secs = [{"name": "s1", "y": 20, "h": 900}]
        if png:              # the same run's screenshot, glyphs where the runs are
            im = Image.new("RGB", (width, tall), "white")
            if width not in self.no_ink:
                from PIL import ImageDraw
                d = ImageDraw.Draw(im)
                for r in runs:
                    d.rectangle((r["x"] + 2, r["y"] + 2, r["x"] + 30, r["y"] + r["h"] - 2), "black")
            im.save(png)
        return _measure_dom(runs, root, secs, vw=vw)

    def args(self, **kw):
        a = dict(source=self.URL, slug="ex", from_="elementor", force=False,
                 frame=None, no_assets=False, no_render=False,
                 cd_project=None, cd_file=None)
        a.update(kw)
        return type("A", (), a)()

    def extract(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fw.cmd_extract(self.args(**kw))
        return out.getvalue()

    def dies(self, code, **kw):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            fw.cmd_extract(self.args(**kw))
        self.assertIn(code + ":", err.getvalue())
        return err.getvalue()

    def design(self):
        return json.loads(_read(os.path.join(self.out, "design.json")))



class ExtractElementor(ExtractFixture):
    def test_extract_writes_three_references_and_design_json(self):
        printed = self.extract()
        for f in ("design.png", "design-768.png", "design-390.png", "design.json",
                  "elementor.json", "manifest.json", os.path.join("elementor", "live.html")):
            self.assertTrue(os.path.exists(os.path.join(self.out, f)), f)
        d = self.design()
        src = d["source"]
        self.assertEqual(src["type"], "elementor")
        self.assertEqual((src["post_id"], src["url"], src["width"], src["viewport_h"]),
                         (1234, self.URL, 1440, 1000))
        self.assertEqual(src["modified_gmt"], "2026-01-02T03:04:05")
        self.assertEqual(src["fonts"], ["Roboto"])
        self.assertEqual(set(d["widths"]), {"768", "390"})
        for w in ("768", "390"):
            self.assertEqual(set(d["widths"][w]), {"texts", "sections"})
        t = d["texts"][0]
        for k in ("i", "text", "x", "y", "w", "h", "align", "type", "tag"):
            self.assertIn(k, t)
        self.assertEqual((t["i"], t["text"], t["x"], t["y"], t["tag"]),
                         (1, "Example heading", 40, 200, "h1"))   # relative to data-fw-root
        self.assertEqual(t["type"], {"family": "Roboto", "size": 32.0, "weight": 700,
                                     "lh": 41.6, "ls": 0, "fill": "#111111"})
        # The copy comes from the page's HTML, all of it; positions from the runs.
        self.assertEqual([x["text"] for x in d["texts"]],
                         ["Example heading", "One", "Two", "Contact us", "First post", "42"])
        self.assertEqual([x["i"] for x in d["texts"]], [1, 2, 3, 4, 5, 6])
        c = d["texts"][3]
        self.assertEqual((c["x"], c["y"], c["type"]["ls"], c["align"]), (60, 800, 0.5, "left"))
        self.assertIsNone(c["type"]["lh"])
        self.assertNotIn("hidden", c)
        for x in d["texts"][1:3]:       # no rendered run: kept, flagged, at the text above it
            self.assertEqual((x["hidden"], x["y"]), (True, 200))
        self.assertEqual((d["texts"][4]["hidden"], d["texts"][4]["y"]), (True, 800))
        # measure.js reports nothing under 3 characters, so "42" has no run
        # without being hidden: it is placed the same way, flagged for what it is
        self.assertEqual((d["texts"][5].get("unmeasured"), d["texts"][5]["y"]), (True, 800))
        self.assertNotIn("hidden", d["texts"][5])
        self.assertEqual(d["widths"]["390"]["texts"][0]["y"], 120)
        self.assertEqual(d["widths"]["390"]["sections"], [{"name": "s1", "y": 0, "h": 900}])
        self.assertEqual((d["frames"], d["tokens"], d["comments"]), ([], {}, []))
        # 768 and 390 come out of an iframe, cropped to its width
        self.assertEqual(_size(os.path.join(self.out, "design-390.png"))[0], 390)
        self.assertEqual(_size(os.path.join(self.out, "design-768.png"))[0], 768)
        self.assertEqual(_size(os.path.join(self.out, "design.png")), (1440, 2000))
        # Per width: a sizing run, then one run that both measures and shoots.
        self.assertEqual([(x["url"], x["png"]) for x in self.dumps],
                         [("http://127.0.0.1:1/live.html", None),
                          ("http://127.0.0.1:1/live.html", "design.png"),
                          ("http://127.0.0.1:2/frame.html?w=768", None),
                          ("http://127.0.0.1:2/frame.html?w=768", "design-768.png"),
                          ("http://127.0.0.1:2/frame.html?w=390", None),
                          ("http://127.0.0.1:2/frame.html?w=390", "design-390.png")])
        self.assertEqual({x["budget"] for x in self.dumps}, {fw.EL_BUDGET})
        self.assertFalse(self.shots)
        self.assertEqual(src["ref_drift"], {"1440": 0, "768": 0, "390": 0})
        self.assertEqual(src["ref_blank"], {"1440": 0, "768": 0, "390": 0})
        self.assertNotIn("drift", printed.lower())
        self.assertEqual(self.serve_live.call_args.kwargs.get("viewport_h"), 1000)
        self.assertEqual(self.serve_live.call_args.args[0], os.path.join(self.out, "elementor"))
        inv = json.loads(_read(os.path.join(self.out, "elementor.json")))
        self.assertEqual(inv["tier3"], ["elementskit-funfact"])
        self.assertEqual(inv["posts_queries"][0]["posts_per_page"], 3)
        self.assertEqual(len(inv["responsive"]["767"]), 2)
        self.assertIn("tier 3: elementskit-funfact — no recipe in "
                      "references/elementor-widgets.md yet", printed)
        self.assertIn("900", printed)

    def test_live_html_is_the_snapshot_with_vh_pinned(self):
        self.html = LIVE.replace("<head>", "<head><style>.full{height:100vh}</style>", 1)
        self.extract()
        live = _read(os.path.join(self.out, "elementor", "live.html"))
        self.assertIn("data-fw-root", live)
        self.assertIn(".full{height:1000px}", live)
        self.assertIn("fwMeasure", live)
        # measured again as the body settles, so the last report is the shot's layout
        self.assertIn("ResizeObserver", live)
        self.assertLess(live.index("function fwMeasure"), live.index("ResizeObserver"))

    def test_extract_by_post_id_uses_the_rest_link(self):
        self.extract(source="1234")
        self.assertIn(("fetch_live", self.URL), self.calls)

    def test_extract_dies_when_doc_id_differs(self):
        self.post_id = 999
        self.dies("NO_ELEMENTOR_DOC", source="999")
        self.assertFalse(os.path.exists(os.path.join(self.out, "design.json")))

    def test_extract_refuses_redirect_and_password_form(self):
        self.final = "https://example.test/somewhere-else/"
        self.dies("NOT_PUBLIC")
        self.final = None
        self.html = LIVE.replace("<body", '<body><form class="post-password-form"></form><i', 1)
        self.dies("NOT_PUBLIC")
        self.html = LIVE.replace("<body", '<body><form name="loginform" '
                                 'action="https://example.test/wp-login.php" method="post"></form><i', 1)
        self.dies("NOT_PUBLIC")
        self.assertFalse(os.path.exists(os.path.join(self.out, "design.json")))

    def test_page_css_falls_back_to_its_rules_in_the_inline_css(self):
        # No post-<id>.css on the server and no elementor-post-<id> <style>:
        # the page's rules ride in elementor-frontend-inline-css with every
        # other document's.
        import urllib.error
        real = self.fake_http_get

        def http_get(url, timeout=60):
            if url.endswith("post-1234.css"):
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return real(url, timeout)
        fw.http_get = http_get
        self.html = LIVE.replace("</head>", (
            '<style id="elementor-frontend-inline-css">'
            ".elementor-11 .elementor-element.elementor-element-h1a1{color:red}"
            "@media(max-width:767px){.elementor-11 .elementor-element.elementor-element-h1a1{padding:1px}"
            ".elementor-1234 .elementor-element.elementor-element-a1b2{padding:2px}}"
            ".elementor-1234 .elementor-element.elementor-element-a1b2{background-image:"
            'url("https://example.test/wp-content/uploads/bg.jpg")}'
            ".elementor-11 .x{background-image:url(https://example.test/wp-content/uploads/hdr.jpg)}"
            "</style></head>"), 1)
        printed = self.extract()
        inv = json.loads(_read(os.path.join(self.out, "elementor.json")))
        self.assertEqual(inv["responsive"]["767"],
                         [".elementor-1234 .elementor-element.elementor-element-a1b2{padding:2px}"])
        self.assertIn("elementor-frontend-inline-css", printed)
        man = json.loads(_read(os.path.join(self.out, "manifest.json")))
        self.assertIn("assets/bg.jpg", man)
        self.assertNotIn("assets/hdr.jpg", man)       # the header's background

    def test_password_form_css_and_login_link_are_not_a_form(self):
        # The site's own stylesheet styles .post-password-form on every page,
        # and a footer may link to the login page; neither is the page itself.
        self.html = LIVE.replace(
            "</head>", "<style>.post-password-form{padding:4px}</style></head>", 1).replace(
            "</body>", '<a href="/wp-login.php">Log in</a></body>', 1)
        self.extract()
        self.assertEqual(self.design()["source"]["type"], "elementor")

    def test_extract_refuses_unpublished_page(self):
        self.status = "draft"
        self.dies("NOT_PUBLIC", source="1234")

    def test_extract_refuses_non_builder_page(self):
        self.meta["_elementor_edit_mode"] = ""
        self.dies("NOT_ELEMENTOR")
        self.meta["_elementor_edit_mode"] = None
        self.dies("NOT_ELEMENTOR")

    def test_manifest_keeps_thumbs_without_id(self):
        self.html = LIVE.replace(
            '<span class="number">42</span>',
            '<span class="number">42</span><img src="%s" alt="t">' % self.THUMB, 1)
        self.extract()
        man = json.loads(_read(os.path.join(self.out, "manifest.json")))
        self.assertEqual(man["assets/t-abc.png"], {"id": None, "url": self.THUMB})
        self.assertEqual(man["assets/hero.png"],
                         {"id": 55, "url": "https://example.test/wp-content/uploads/hero.png"})
        for rec in man.values():
            self.assertNotIn("sha", rec)          # push treats url-without-sha as uploaded
        self.assertNotIn("assets/logo.png", man)  # header image, not the page's
        self.assertTrue(os.path.exists(os.path.join(self.out, "assets", "t-abc.png")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "assets", "hero.png")))
        # thumbs are never looked up in the media library
        self.assertFalse([c for c in self.calls if c[0] == "GET" and "search=t-abc" in c[1]])

    def test_extract_refuses_to_overwrite_figma_build(self):
        os.makedirs(self.out)
        _write(os.path.join(self.out, "page.html"), "<p>figma work</p>")
        _write(os.path.join(self.out, "design.json"), json.dumps({"source": {"file_key": "k"}}))
        self.dies("NOT_AN_ELEMENTOR_BUILD")
        self.assertEqual(_read(os.path.join(self.out, "page.html")), "<p>figma work</p>")
        self.assertFalse(self.dumps)
        self.extract(force=True)
        self.assertEqual(self.design()["source"]["type"], "elementor")
        self.assertEqual(_read(os.path.join(self.out, "page.html")), "<p>figma work</p>")

    def test_reextract_of_an_elementor_build_is_allowed(self):
        self.extract()
        _write(os.path.join(self.out, "page.html"), "<p>mine</p>")
        self.extract()
        self.assertEqual(_read(os.path.join(self.out, "page.html")), "<p>mine</p>")

    def test_fonts_without_roboto_or_raleway_warn(self):
        real = self.fake_dump_dom

        def dump(url, width=1440, timeout=180, **kw):
            return real(url, width, timeout, **kw).replace('"Roboto"]', '"Inter"]')
        fw.dump_dom = dump
        self.assertIn("fonts", self.extract())
        self.assertRegex(self.extract(), r"(?i)warn.*neither Roboto nor Raleway")


class ExtractElementorRender(ExtractFixture):
    """The references and the measurements are one render."""

    def test_window_is_sized_from_the_measured_root(self):
        self.extract()
        # root y + h + margin: 100 + 2000 at 1440, 20 + 2400 in the frames
        tall = [x["tall"] for x in self.dumps if x["png"]]
        self.assertEqual(tall, [2100 + fw.REF_MARGIN, 2420 + fw.REF_MARGIN, 2420 + fw.REF_MARGIN])
        for probe, shot in zip(self.dumps[::2], self.dumps[1::2]):
            self.assertEqual((probe["url"], probe["width"], probe["budget"]),
                             (shot["url"], shot["width"], shot["budget"]))

    def test_ref_drift_is_recorded_and_loud_beyond_6px(self):
        self.grow = {800: 700, 888: 5}      # the page grew on the shot run
        printed = self.extract()
        drift = self.design()["source"]["ref_drift"]
        self.assertEqual(drift["1440"], 0)
        self.assertEqual(drift["768"], 0)    # 5px grew inside the margin; png still whole
        self.assertEqual(drift["390"], fw.REF_MARGIN - 700)
        self.assertRegex(printed, r"(?i)warn.*390.*%d ?px" % (fw.REF_MARGIN - 700))
        self.assertNotRegex(printed, r"(?i)warn.*\b768\b.*px")
        self.assertEqual(self.design()["source"]["render_drift"]["390"], 700)

    def test_text_boxes_with_nothing_drawn_under_them_warn(self):
        self.no_ink = {800}                  # the 390 shot does not show the measured layout
        printed = self.extract()
        src = self.design()["source"]
        self.assertEqual(src["ref_blank"], {"1440": 0, "768": 0, "390": 1})
        self.assertEqual(src["ref_drift"]["390"], 0)
        self.assertRegex(printed, r"(?i)warn.*390.*1 of 1 text box")
        self.assertNotRegex(printed, r"(?i)warn.*(1440|768)")

    def test_no_render_and_no_assets_say_they_are_ignored(self):
        printed = self.extract(no_render=True, no_assets=True)
        self.assertIn("ignored with --from elementor", printed)
        self.assertNotIn("ignored with --from elementor", self.extract())


class ElementorCopyTexts(unittest.TestCase):
    def doc(self, body):
        return ('<div data-elementor-type="header" data-elementor-id="1"><p>Header copy</p></div>'
                '<div data-elementor-type="wp-page" data-elementor-id="7">%s</div>'
                '<div data-elementor-type="footer" data-elementor-id="2"><p>Footer</p></div>' % body)

    def test_long_paragraph_survives_whole(self):
        long = "word " * 40
        self.assertEqual(el.copy_texts(self.doc("<p>%s</p>" % long), 7), [long.strip()])

    def test_short_strings_and_inline_runs(self):
        out = el.copy_texts(self.doc(
            '<div class="ekit"><span class="number">42</span></div>'
            "<p>A <b>B</b> C</p><h2>$9</h2><p>x&amp;y&nbsp;z</p>"), 7)
        self.assertEqual(out, ["42", "A B C", "$9", "x&y z"])

    def test_scripts_styles_and_other_documents_are_not_copy(self):
        out = el.copy_texts(self.doc(
            "<style>.a{}</style><script>var s='no';</script><noscript>no</noscript><p>yes</p>"), 7)
        self.assertEqual(out, ["yes"])

    def test_text_around_a_nested_block_is_split_not_glued(self):
        self.assertEqual(el.copy_texts(self.doc("<div>Intro <p>para</p> tail</div>"), 7),
                         ["Intro", "para", "tail"])

    def test_hidden_panel_and_adjacent_repeats(self):
        w = ('<div data-element_type="widget" data-id="%s"><div class="tabs">'
             '<button>One</button><div class="t-mobile">One</div>'
             '<div class="panel" hidden><p>Hidden panel copy</p></div></div></div>')
        out = el.copy_texts(self.doc(w % "a" + w % "b"), 7)
        # the same title twice in one widget is one string; another widget's is its own
        self.assertEqual(out, ["One", "Hidden panel copy", "One", "Hidden panel copy"])

    def test_no_page_doc_is_empty(self):
        self.assertEqual(el.copy_texts(self.doc("<p>x</p>"), 8), [])


class ElementorFramed(unittest.TestCase):
    def test_measure_at_narrow_width_uses_the_iframe_wrapper(self):
        seen = {}

        def dump(url, width=1440, timeout=180, tall=20000, budget=6000, png=None):
            with urllib.request.urlopen(url, timeout=5) as r:
                seen["wrap"] = r.read().decode()
            seen.update(width=width, tall=tall, budget=budget)
            if png:
                from PIL import Image
                Image.new("RGB", (width, tall), "white").save(png)
            return _measure_dom([], {"x": 0, "y": 0, "w": 390, "h": 10}, vw=390)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        png = os.path.join(tmp, "d.png")
        with mock.patch.object(fw, "dump_dom", dump):
            m = fw.measure_at("http://127.0.0.1:1/live.html", 390, png=png, tall=3000)
        self.assertEqual(m["mobile"]["vw"], 390)
        self.assertIn('src="http://127.0.0.1:1/live.html"', seen["wrap"])
        self.assertIn('width="390" height="3000"', seen["wrap"])
        self.assertIn("fwMeasure", seen["wrap"])
        self.assertGreaterEqual(seen["width"], 500)
        self.assertEqual((seen["tall"], seen["budget"]), (3000, fw.EL_BUDGET))
        self.assertEqual(_size(png), (390, 3000))      # the same run's shot, frame width

    def test_measure_at_1440_measures_the_page_itself(self):
        with mock.patch.object(fw, "dump_dom", return_value=_measure_dom(
                [], {"x": 0, "y": 0, "w": 1440, "h": 10})) as d:
            fw.measure_at("http://h/live.html", 1440)
        self.assertEqual(d.call_args.args[0], "http://h/live.html")

    def test_measure_at_without_report_dies(self):
        with mock.patch.object(fw, "dump_dom", return_value="<html></html>"), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fw.measure_at("http://h/live.html", 1440)

    def test_shoot_framed_crops_to_the_frame_width(self):
        from PIL import Image
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        png = os.path.join(tmp, "d.png")

        def shoot(url, out, width, tall=14000):
            self.assertGreaterEqual(width, 500)
            self.assertEqual(tall, 9000)
            Image.new("RGB", (width, 1200), "white").save(out)
            return Image.open(out)
        with mock.patch.object(fw, "shoot", shoot):
            fw.shoot_framed("http://h/live.html", png, 390, 9000)
        self.assertEqual(_size(png), (390, 1200))


def _rec(text, x, y, size=20, **kw):
    """A design.json text: root-relative box, size under type."""
    return {"text": text, "x": x, "y": y, "w": 300, "h": 40, "type": {"size": size}, **kw}


class ComparePositions(unittest.TestCase):
    def test_compare_positions_reports_only_beyond_tolerance(self):
        ref = [_rec("Plans for every team", 20, 100), _rec("Start for free today", 20, 200),
               _rec("Talk to our sales team", 20, 300)]
        live = [_rec("Plans for every team", 24, 106),        # inside +-6
                _rec("Start for free today", 20, 214),
                _rec("Talk to our sales team", 11, 300)]
        got = fw.compare_positions(ref, live, 6)
        self.assertEqual(got, [{"text": "Start for free today", "dx": 0, "dy": 14},
                               {"text": "Talk to our sales team", "dx": -9, "dy": 0}])

    def test_compare_positions_pairs_repeated_text_in_order(self):
        ref = [_rec("Learn more about it", 20, 100), _rec("Learn more about it", 20, 500)]
        live = [_rec("Learn  more about it", 20, 100), _rec("Learn more about it", 20, 530)]
        got = fw.compare_positions(ref, live, 6)
        self.assertEqual(got, [{"text": "Learn more about it", "dx": 0, "dy": 30}])

    def test_compare_positions_skips_short_and_unpaired_text(self):
        ref = [_rec("Go", 0, 0), _rec("Only on the original", 0, 0),
               _rec("Collapsed answer text", 0, 0, clipped=True)]
        live = [_rec("Go", 90, 90), _rec("Collapsed answer text", 90, 90)]
        self.assertEqual(fw.compare_positions(ref, live, 6), [])

    def test_compare_sections_checks_y_and_h_in_order(self):
        ref = [{"name": "s1", "y": 0, "h": 600}, {"name": "s2", "y": 600, "h": 400},
               {"name": "s3", "y": 1000, "h": 300}]
        live = [{"name": "s1", "y": 0, "h": 604}, {"name": "s2", "y": 640, "h": 400},
                {"name": "s3", "y": 1040, "h": 330}]
        self.assertEqual(fw.compare_sections(ref, live, 6),
                         [{"name": "s2", "dy": 40, "dh": 0},
                          {"name": "s3", "dy": 40, "dh": 30}])


class MobileDiffElementor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.build = os.path.join(self.tmp, "build")
        self.out = os.path.join(self.build, "ex")
        os.makedirs(self.out)
        p = mock.patch.object(fw, "BUILD", self.build)
        p.start()
        self.addCleanup(p.stop)
        self.design = {"source": {"type": "elementor", "width": 1440},
                       "widths": {"390": {"texts": [
                           _rec("Tiny legal line that is small", 20, 900, size=12),
                           _rec("Plans for every team", 20, 100)], "sections": []}}}
        _write(os.path.join(self.out, "design.json"), json.dumps(self.design))

    def mobile(self, dom, **kw):
        args = fw.argparse.Namespace(slug="ex", url="http://127.0.0.1:1/preview.html",
                                     width=390, height=844, tolerance=6, **kw)
        buf = io.StringIO()
        with mock.patch.object(fw, "dump_dom", return_value=dom), \
                contextlib.redirect_stdout(buf):
            bad = fw.cmd_mobile(args)
        return bad, buf.getvalue()

    def dom(self, small=(), runs=(), root=None):
        return _measure_dom(list(runs), root or {"x": 0, "y": 0, "w": 390, "h": 1000},
                            vw=390, small=small)

    def test_small_text_present_in_reference_is_a_note_not_a_finding(self):
        small = [{"cls": "legal", "fs": "12px", "txt": "Tiny legal line that is sma"}]
        bad, text = self.mobile(self.dom(small=small))
        self.assertEqual(bad, 0)
        self.assertIn("note", text)
        self.assertIn("under 16", text)

    def test_small_text_missing_from_reference_is_a_finding(self):
        small = [{"cls": "new", "fs": "12px", "txt": "Added on the rebuild only"}]
        bad, text = self.mobile(self.dom(small=small))
        self.assertEqual(bad, 1)
        self.assertIn("1 kind(s) of finding", text)

    def test_positions_are_root_relative_and_counted(self):
        # the root sits 50px down in the frame: a run at 170 is at 120 in the
        # root, against a reference 100 (read unsubtracted it would be -30)
        runs = [_run("Plans for every team", 20, 170, size=20)]
        bad, text = self.mobile(self.dom(runs=runs, root={"x": 0, "y": 50, "w": 390, "h": 1000}))
        self.assertEqual(bad, 1)
        self.assertIn("positions", text)
        self.assertIn("dy +20", text)

    def test_positions_inside_tolerance_report_none(self):
        runs = [_run("Plans for every team", 22, 148, size=20)]
        bad, text = self.mobile(self.dom(runs=runs, root={"x": 0, "y": 50, "w": 390, "h": 1000}))
        self.assertEqual(bad, 0)
        self.assertRegex(text, r"positions\s+none")

    def test_figma_build_keeps_the_old_behaviour(self):
        self.design["source"] = {"width": 1440}
        _write(os.path.join(self.out, "design.json"), json.dumps(self.design))
        small = [{"cls": "legal", "fs": "12px", "txt": "Tiny legal line that is sma"}]
        bad, text = self.mobile(self.dom(small=small))
        self.assertEqual(bad, 1)
        self.assertNotIn("positions", text)

    def with_sections(self):
        self.design["widths"]["390"]["sections"] = [
            {"name": "s1", "y": 0, "h": 600}, {"name": "s2", "y": 600, "h": 400}]
        _write(os.path.join(self.out, "design.json"), json.dumps(self.design))

    def translated(self):
        _write(os.path.join(self.out, "copy.json"), json.dumps(
            {"mode": "translated", "source": "user", "approved": True, "entries": []}))

    def test_translated_mode_compares_sections_not_positions(self):
        self.translated()
        self.with_sections()
        # the root sits 50px down; s2 starts 40px late in the root, and the
        # text is somewhere the English reference never put it
        secs = [{"name": "s1", "y": 50, "h": 600}, {"name": "s2", "y": 690, "h": 400}]
        runs = [_run("\u6bcf\u500b\u5718\u968a\u7684\u65b9\u6848", 300, 700, size=20)]
        bad, text = self.mobile(_measure_dom(
            runs, {"x": 0, "y": 50, "w": 390, "h": 1000}, sections=secs, vw=390))
        self.assertEqual(bad, 1)
        self.assertIn("sections  1 section(s) more than \u00b16px", text)
        self.assertIn("dy +40 dh +0", text)
        self.assertNotIn("positions", text)

    def test_translated_sections_inside_tolerance_report_none(self):
        self.translated()
        self.with_sections()
        secs = [{"name": "s1", "y": 50, "h": 603}, {"name": "s2", "y": 653, "h": 400}]
        bad, text = self.mobile(_measure_dom(
            [], {"x": 0, "y": 50, "w": 390, "h": 1000}, sections=secs, vw=390))
        self.assertEqual(bad, 0)
        self.assertRegex(text, r"sections\s+none")
        self.assertNotIn("positions", text)

    def test_verbatim_copy_json_keeps_the_positions_check(self):
        _write(os.path.join(self.out, "copy.json"), json.dumps(
            {"mode": "verbatim", "entries": []}))
        runs = [_run("Plans for every team", 20, 170, size=20)]
        bad, text = self.mobile(self.dom(runs=runs, root={"x": 0, "y": 50, "w": 390, "h": 1000}))
        self.assertEqual(bad, 1)
        self.assertIn("positions", text)
        self.assertNotIn("sections ", text)

    def diff(self, width, ref=None):
        from PIL import Image
        for n in ("design.png", "design-390.png"):
            Image.new("RGB", (1440 if n == "design.png" else 390, 600), "white").save(
                os.path.join(self.out, n))
        self.framed = []

        def framed(url, png, w, h):
            self.framed.append((url, png, w, h))
            Image.new("RGB", (w, 600), "white").save(png)
        args = fw.argparse.Namespace(slug="ex", url="http://127.0.0.1:1/preview.html",
                                     width=width, bands=4, section=None, offset=0,
                                     cols=3, ref=ref)
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b"<html>"
        resp.status = 200
        seen = {}
        import warnings
        cw = warnings.catch_warnings()
        cw.__enter__()
        self.addCleanup(cw.__exit__, None, None, None)
        warnings.filterwarnings("ignore", message="Image.Image.getdata")   # cmd_diff's own
        with mock.patch.object(fw, "shoot_framed", framed), \
                mock.patch.object(fw, "shoot", side_effect=AssertionError("plain shoot")), \
                mock.patch.object(fw.urllib.request, "urlopen", return_value=resp), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            fw.cmd_diff(args)
        seen["out"] = buf.getvalue()
        return seen

    def test_diff_picks_width_reference_for_elementor(self):
        seen = self.diff(390)
        self.assertEqual(len(self.framed), 1)
        self.assertEqual(self.framed[0][2], 390)
        self.assertIn("design-390.png", seen["out"])

    def test_diff_without_width_reference_dies_for_elementor(self):
        from PIL import Image
        Image.new("RGB", (1440, 600), "white").save(os.path.join(self.out, "design.png"))
        args = fw.argparse.Namespace(slug="ex", url="http://127.0.0.1:1/p.html", width=768,
                                     bands=4, section=None, offset=0, cols=3, ref=None)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            fw.cmd_diff(args)
        self.assertIn("NO_REFERENCE", err.getvalue())


class CopyJson(unittest.TestCase):
    """copy.json: a translated page is checked against its `to` strings."""
    TEXTS = [{"i": 1, "text": "Plans for every team", "y": 100},
             {"i": 2, "text": "Start for free today", "y": 600},
             {"i": 3, "text": "Line one\nLine two", "y": 700}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "build", "t")
        os.makedirs(self.out)
        self.page('<p id="herotop">%s</p><p>%s</p><p>%s<br>%s</p>' %
                  ("\u6bcf\u500b\u5718\u968a\u7684\u65b9\u6848", "\u7acb\u5373\u514d\u8cbb\u8a66\u7528",
                   "\u7b2c\u4e00\u884c", "\u7b2c\u4e8c\u884c"))
        self.design()

    def page(self, body):
        _write(os.path.join(self.out, "page.html"), "{{styles}}\n" + body)
        _write(os.path.join(self.out, "page.css"), ".a{}")

    def design(self, texts=None):
        _write(os.path.join(self.out, "design.json"),
               json.dumps({"texts": texts or self.TEXTS, "assets": []}))

    def copy(self, entries, **kw):
        cp = {"mode": "translated", "source": "user", "approved": True, "entries": entries}
        cp.update(kw)
        _write(os.path.join(self.out, "copy.json"), json.dumps(cp))

    def good_entries(self):
        return [{"i": 1, "from": "Plans for every team", "to": "\u6bcf\u500b\u5718\u968a\u7684\u65b9\u6848"},
                {"i": 2, "from": "Start for free today", "to": "\u7acb\u5373\u514d\u8cbb\u8a66\u7528"},
                {"i": 3, "from": "Line one\nLine two", "to": "\u7b2c\u4e00\u884c\n\u7b2c\u4e8c\u884c"}]

    def checks(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fw.run_checks(self.out, **kw)

    def test_translated_copy_checks_to_strings(self):
        self.copy(self.good_entries())
        errors, _ = self.checks()
        self.assertEqual(errors, [])
        # a `to` the page does not carry is a missing string, and it is the
        # `to` that is reported, not the English
        ents = self.good_entries()
        ents[1]["to"] = "\u9019\u53e5\u9801\u9762\u6c92\u6709"
        self.copy(ents)
        errors, _ = self.checks()
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("copy missing (#2, y=600)"), errors)
        self.assertIn("\u9019\u53e5", errors[0])

    def test_translated_hard_break_is_checked_on_the_to_string(self):
        self.page('<p id="herotop">%s</p><p>%s</p><p>%s%s</p>' %
                  ("\u6bcf\u500b\u5718\u968a\u7684\u65b9\u6848", "\u7acb\u5373\u514d\u8cbb\u8a66\u7528",
                   "\u7b2c\u4e00\u884c", "\u7b2c\u4e8c\u884c"))
        self.copy(self.good_entries())
        errors, _ = self.checks()
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("hard break not reproduced (#3"), errors)

    def test_missing_translation_is_an_error(self):
        ents = self.good_entries()
        ents[0]["to"] = None
        ents[1]["to"] = "  "
        del ents[2]                      # no entry at all for #3
        self.copy(ents)
        errors, _ = self.checks()
        self.assertEqual(
            errors, ["untranslated (#1): Plans for every team",
                     "untranslated (#2): Start for free today",
                     "untranslated (#3): Line one\nLine two"])

    def test_untranslated_is_scoped_and_dropped_json_exempts(self):
        _write(os.path.join(self.out, "sections.json"), json.dumps(
            {"hero": {"design_y": 100, "design_span": 100},
             "cta": {"design_y": 600, "design_span": 200}}))
        _write(os.path.join(self.out, "dropped.json"), json.dumps({"Start for free": "nav"}))
        ents = self.good_entries()
        ents[0]["to"] = None
        ents[1]["to"] = None
        self.copy(ents)
        errors, _ = self.checks(section="cta")        # #1 is outside the band
        self.assertEqual(errors, [])
        errors, _ = self.checks(section="hero")
        self.assertEqual(errors, ["untranslated (#1): Plans for every team"])

    def test_unapproved_claude_draft_is_an_error(self):
        self.copy(self.good_entries(), source="claude-draft", approved=False)
        errors, _ = self.checks()
        self.assertEqual(errors, ["COPY_NOT_APPROVED: copy.json source is claude-draft "
                                  "and approved is not true"])
        self.copy(self.good_entries(), source="claude-draft", approved=None)
        cp = json.loads(_read(os.path.join(self.out, "copy.json")))
        del cp["approved"]                                          # approved absent
        _write(os.path.join(self.out, "copy.json"), json.dumps(cp))
        self.assertEqual(len(self.checks()[0]), 1)
        self.copy(self.good_entries(), source="claude-draft", approved=True)
        self.assertEqual(self.checks()[0], [])

    def test_from_that_differs_is_a_warning_only(self):
        ents = self.good_entries()
        ents[0]["from"] = "Plans for each team"
        self.copy(ents)
        errors, warnings = self.checks()
        self.assertEqual(errors, [])
        self.assertEqual([w for w in warnings if "'from'" in w],
                         ["copy.json #1 'from' differs from design.json"])

    def test_no_copy_json_is_verbatim(self):
        # the design's English strings are not on the Chinese page: errors
        errors, _ = self.checks()
        self.assertEqual(len([e for e in errors if e.startswith("copy missing")]), 3)
        # and `verbatim` with a file present is the same as no file, even with
        # entries that would otherwise be untranslated
        self.copy([{"i": 1, "from": "x", "to": None}], mode="verbatim",
                  source="claude-draft", approved=False)
        errors2, warnings2 = self.checks()
        self.assertEqual(errors2, errors)
        self.assertFalse([w for w in warnings2 if "copy.json" in w])
        # a page that does carry the English passes with no copy.json
        os.remove(os.path.join(self.out, "copy.json"))
        self.page('<p id="herotop">Plans for every team</p><p>Start for free today</p>'
                  '<p>Line one<br>Line two</p>')
        self.assertEqual(self.checks()[0], [])

    def test_malformed_copy_json_dies(self):
        bad = ["{not json", "[]", json.dumps({"mode": "other", "entries": []}),
               json.dumps({"mode": "translated", "source": "bot", "entries": []}),
               json.dumps({"mode": "translated", "entries": {}}),
               json.dumps({"mode": "translated", "entries": [{"from": "a", "to": "b"}]}),
               json.dumps({"mode": "translated", "entries": [{"i": "1", "to": "b"}]}),
               json.dumps({"mode": "translated", "entries": [{"i": 1, "to": 5}]})]
        for text in bad:
            _write(os.path.join(self.out, "copy.json"), text)
            with self.subTest(text=text), contextlib.redirect_stderr(io.StringIO()) as err:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    fw.run_checks(self.out)
            self.assertIn("BAD_COPY_JSON", err.getvalue())

    def test_dropped_key_matching_only_an_out_of_scope_to_string_is_not_stale(self):
        _write(os.path.join(self.out, "sections.json"), json.dumps(
            {"hero": {"design_y": 100, "design_span": 100},
             "cta": {"design_y": 600, "design_span": 200}}))
        # the key matches the page's wording of #2 only, and #2 is outside the band
        _write(os.path.join(self.out, "dropped.json"),
               json.dumps({"\u7acb\u5373\u514d\u8cbb": "nav"}))
        self.copy(self.good_entries())
        _, warnings = self.checks(section="hero")
        self.assertFalse([w for w in warnings if "dropped.json key" in w], warnings)

    def test_load_copy_without_file_is_none(self):
        self.assertIsNone(fw.load_copy(self.out))


if __name__ == "__main__":
    unittest.main()
