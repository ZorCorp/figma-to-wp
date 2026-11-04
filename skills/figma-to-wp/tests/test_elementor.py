import contextlib, io, json, os, shutil, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import figma_to_wp as fw  # noqa: E402


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
        open(os.path.join(self.out, "page.html"), "w").write(self.HTML)
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
        json.dump(meta, open(os.path.join(self.out, "wp.json"), "w"))

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
        open(os.path.join(self.out, "page.html"), "w").write("<div><p>a</p></div>")
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
        open(os.path.join(self.out, "page.html"), "w").write(
            '<!-- wp:html -->\n<div class="mc-page gehk-page"><img src="assets/a.png"></div>\n'
            '<!-- /wp:html -->\n')
        json.dump({"assets/a.png": {"id": None, "url": url}},
                  open(os.path.join(self.out, "manifest.json"), "w"))
        self.push()
        self.assertFalse([c for c in self.calls
                          if c[0] == "POST" and "/media" in c[1]])
        html = [c for c in self.calls if c[0] == "mc/set-post-html"][0][1]["content"]
        self.assertIn(f'src="{url}"', html)
        self.assertNotIn("assets/a.png", html)


if __name__ == "__main__":
    unittest.main()
