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


if __name__ == "__main__":
    unittest.main()
