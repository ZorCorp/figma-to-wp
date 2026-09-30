import os, re, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import wpsafe  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "cd-78436")

# export.html, pushed-before.html and expected-body.html are a real page (post
# 78436). They live in the Figma-to-WP build repo only; the published plugin
# ships without them, and every test that reads them skips there.
PAGE_FIXTURES = all(os.path.exists(os.path.join(FIX, n)) for n in
                    ("export.html", "pushed-before.html", "expected-body.html"))
needs_page = unittest.skipUnless(PAGE_FIXTURES, "page fixtures are not shipped with the plugin")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class Parsing(unittest.TestCase):
    def test_split_document_drops_shell(self):
        head, body = wpsafe.split_document(
            '<!DOCTYPE html><html><head><title>t</title></head>'
            '<body><div id="x">hi</div></body></html>')
        self.assertIn("<title>t</title>", head)
        self.assertEqual(body.strip(), '<div id="x">hi</div>')

    def test_split_document_without_shell(self):
        head, body = wpsafe.split_document('<!-- wp:html -->\n<div id="x">hi</div>\n<!-- /wp:html -->')
        self.assertEqual(head, "")
        self.assertEqual(body.strip(), '<div id="x">hi</div>')

    def test_find_root_ignores_links_and_scripts(self):
        body = '<link rel="stylesheet" href="a.css"><div id="x"><p>a</p></div><script>1<2</script>'
        root = wpsafe.find_root(body)
        self.assertEqual(root["attrs"]["id"], "x")
        self.assertEqual(body[root["start"]:root["end"]], '<div id="x"><p>a</p></div>')

    def test_two_roots_is_none(self):
        self.assertIsNone(wpsafe.find_root('<div id="a"></div><div id="b"></div>'))

    def test_unclosed_p_inside_root_still_one_root(self):
        body = '<div id="x"><p>one<p>two</div>'
        root = wpsafe.find_root(body)
        self.assertIsNotNone(root)
        self.assertEqual(root["attrs"]["id"], "x")
        self.assertEqual(root["end"], len(body))

    def test_root_selector_prefers_id(self):
        self.assertEqual(wpsafe.root_selector('<div id="aim" class="a b"></div>'), "#aim")
        self.assertEqual(wpsafe.root_selector('<div class="mc-page ai-page"></div>'), ".mc-page")
        self.assertEqual(wpsafe.root_selector("<p>a</p><p>b</p>"), "")

    @needs_page
    def test_root_selector_on_export(self):
        self.assertEqual(wpsafe.root_selector(fixture("export.html")), "#aim")

    def test_split_selectors_respects_parens(self):
        self.assertEqual(wpsafe.split_selectors("#aim :is(h2,h3), #aim p"),
                         ["#aim :is(h2,h3)", "#aim p"])

    def test_css_rules_flattens_media_and_skips_keyframes(self):
        css = ("@keyframes k{from{opacity:0}to{opacity:1}}"
               "@media (max-width:759px){#aim p{margin:0}}"
               "@font-face{font-family:X;src:url(a.woff2)}"
               "#aim h2,#aim h3{color:red}")
        self.assertEqual(wpsafe.css_rules(css), ["#aim p", "#aim h2,#aim h3"])


def rules(findings, level=None):
    return sorted({f["rule"] for f in findings if level is None or f["level"] == level})


class Check(unittest.TestCase):
    @needs_page
    def test_export_passes_with_one_head_warning(self):
        f = wpsafe.check(fixture("export.html"))
        self.assertEqual(rules(f, "error"), [])
        self.assertEqual(rules(f, "warn"), ["head-dropped"])

    @needs_page
    def test_pushed_before_is_caught(self):
        f = wpsafe.check(fixture("pushed-before.html"))
        self.assertIn("preview-harness", rules(f, "error"))
        self.assertIn("head-dropped", rules(f, "warn"))

    def test_each_bad_fixture_names_its_rule(self):
        for name, rule in [("bad-html-body.html", "css-scope"),
                           ("bad-style-outside.html", "css-scope"),
                           ("bad-claude-url.html", "url"),
                           ("bad-two-roots.html", "root"),
                           ("bad-media-unscoped.html", "css-scope")]:
            with self.subTest(name=name):
                self.assertIn(rule, rules(wpsafe.check(fixture(name)), "error"))

    def test_has_selectors_are_scoped(self):
        html = ('<div id="x"><style>#x{font-family:A}html:has(#x){scroll-behavior:smooth}'
                'body:has(#x) .site-main{overflow:visible}</style></div>')
        self.assertEqual(rules(wpsafe.check(html), "error"), [])

    def test_is_selector_with_commas_is_scoped(self):
        html = '<div id="x"><style>#x{font-family:A}#x :is(h2,h3){margin:0}</style></div>'
        self.assertEqual(rules(wpsafe.check(html), "error"), [])

    def test_root_without_font_family_warns(self):
        html = '<div id="x"><style>#x p{color:red}</style><p>a</p></div>'
        self.assertIn("root-font", rules(wpsafe.check(html), "warn"))

    def test_script_outside_root(self):
        html = '<div id="x"><style>#x{font-family:A}</style></div><script>alert(1)</script>'
        self.assertIn("script-outside", rules(wpsafe.check(html), "error"))

    def test_base_tag(self):
        html = '<div id="x"><base href="/"><style>#x{font-family:A}</style></div>'
        self.assertIn("base", rules(wpsafe.check(html), "error"))

    def test_url_outside_root_is_caught(self):
        html = ('<div id="x"><style>#x{font-family:A}</style></div>'
                '<link rel="preload" href="https://img.claudeusercontent.com/x.png">')
        self.assertIn("url", rules(wpsafe.check(html), "error"))

    def test_base_in_head_is_caught(self):
        html = ('<html><head><base href="https://claude.ai/"></head>'
                '<body><div id="x"><style>#x{font-family:A}</style></div></body></html>')
        self.assertIn("base", rules(wpsafe.check(html), "error"))


@needs_page
class Clean(unittest.TestCase):
    def setUp(self):
        self.out = wpsafe.clean(fixture("export.html"))

    def test_wrapped_once(self):
        self.assertTrue(self.out.startswith("<!-- wp:html -->"))
        self.assertTrue(self.out.rstrip().endswith("<!-- /wp:html -->"))
        self.assertEqual(self.out.count("<!-- wp:html -->"), 1)

    def test_idempotent(self):
        self.assertEqual(wpsafe.clean(self.out), self.out)

    def test_no_shell_or_harness(self):
        # Tag-boundary match: the export's own design content has a real
        # <header id="top">, which a plain substring check on "<head" would
        # wrongly flag.
        for s in ("<!DOCTYPE", "<html", "<head", "<body"):
            self.assertNotRegex(self.out, re.escape(s) + r"(?![a-zA-Z-])")
        self.assertNotIn(wpsafe.HARNESS, self.out)

    def test_reset_first_inside_root(self):
        first_style = self.out.index("<style")
        self.assertEqual(self.out.index(wpsafe.RESET_MARK), first_style + len("<style "))
        self.assertIn("#aim label{padding:0;line-height:inherit}", self.out)
        self.assertNotIn("#ROOT", self.out)

    def test_font_family_upgraded(self):
        body = self.out.split("</style>", 1)[1]           # skip the reset block
        self.assertIsNone(re.search(r"font-family\s*:[^;}\"!]+(?=[;}\"])", body))

    def test_site_rules(self):
        self.assertIn('id="herotop"', self.out)
        for tag in re.findall(r"<img\b[^>]*>", self.out):
            self.assertRegex(tag, r'\stitle="[^"]+"')
        for tag in re.findall(r'<a\b[^>]*href="http[^>]*>', self.out):
            self.assertIn('target="_blank"', tag)

    def test_matches_reviewed_expected_body(self):
        self.assertEqual(self.out, fixture("expected-body.html"))

    def test_refuses_on_error(self):
        with self.assertRaises(ValueError) as cm:
            wpsafe.clean(fixture("bad-two-roots.html"))
        self.assertIn("root", str(cm.exception))


class CleanFixRound1(unittest.TestCase):
    """Fix round 1: quoted font-family in a <style> block, <script> bodies
    left alone by the inline-style upgrade, and case-insensitive site rules."""

    def test_double_quoted_family_in_style_block_gets_important(self):
        html = '<div id="x"><style>#x{font-family:"Times New Roman", serif}</style></div>'
        out = wpsafe.clean(html)
        self.assertIn('font-family:"Times New Roman", serif!important', out)

    def test_script_with_style_font_family_left_byte_identical(self):
        script = '<script>var a = \'<span style="font-family:Arial">\';</script>'
        html = f'<div id="x"><style>#x{{font-family:A}}</style>{script}</div>'
        out = wpsafe.clean(html)
        self.assertIn(script, out)

    def test_uppercase_img_tag_gets_title(self):
        html = '<div id="x"><style>#x{font-family:A}</style><IMG src="x.png" alt="cat"></div>'
        out = wpsafe.clean(html)
        self.assertRegex(out, r'<IMG\b[^>]*\stitle="cat"')


class FinalReviewFixes(unittest.TestCase):
    """Final whole-branch review: @font-face left alone, dropped stylesheets
    reported, data-target is not target, and no regex blows up."""

    def test_font_face_block_untouched_and_rule_upgraded(self):
        ff = '@font-face{font-family:"Foo";src:url(data:font/woff2;base64,AA==)}'
        html = f'<div id="x"><style>{ff}#x p{{font-family:"Foo"}}</style><p>a</p></div>'
        out = wpsafe.clean(html)
        self.assertIn(ff, out)
        self.assertIn('#x p{font-family:"Foo"!important}', out)

    def test_keyframes_block_untouched(self):
        kf = "@keyframes k{from{font-family:A}to{font-family:B}}"
        html = f'<div id="x"><style>#x{{font-family:A}}{kf}</style></div>'
        self.assertIn(kf, wpsafe.clean(html))

    def test_non_font_stylesheet_outside_root_warns(self):
        html = ('<html><head><link rel="stylesheet" href="https://cdn.example.com/a.css">'
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
                '</head><body><div id="x"><style>#x{font-family:A}</style></div>'
                '<link rel=stylesheet href=/b.css></body></html>')
        f = [x for x in wpsafe.check(html) if x["rule"] == "stylesheet-dropped"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["level"], "warn")
        self.assertIn("https://cdn.example.com/a.css", f[0]["detail"])
        self.assertIn("/b.css", f[0]["detail"])
        self.assertNotIn("fonts.googleapis", f[0]["detail"])

    @needs_page
    def test_font_links_alone_do_not_warn(self):
        self.assertNotIn("stylesheet-dropped", rules(wpsafe.check(fixture("export.html"))))

    def test_data_target_is_not_a_target(self):
        html = ('<div id="x"><style>#x{font-family:A}</style>'
                '<a data-target="m" href="https://example.com/">a</a>'
                '<a target="_self" href="https://example.com/">b</a></div>')
        out = wpsafe.clean(html)
        self.assertRegex(out, r'<a target="_blank" rel="noopener" data-target="m"')
        self.assertIn('<a target="_self" href="https://example.com/">b</a>', out)

    def assertFast(self, fn, arg, limit=2.0):
        t = time.monotonic()
        try:
            fn(arg)
        except ValueError:
            pass
        self.assertLess(time.monotonic() - t, limit, fn.__name__)

    def test_long_url_run_is_fast(self):
        html = '<div id="x"><style>#x{font-family:A}</style><p>' + "http://a" * 20000 + "</p></div>"
        self.assertFast(wpsafe.check, html)
        self.assertFast(wpsafe.clean, html)

    def test_unclosed_keyframes_is_fast(self):
        self.assertFast(wpsafe.css_rules, "@keyframes k{" + "a" * 40000)
        self.assertFast(wpsafe.css_rules, "@keyframes " * 3600)
        html = '<div id="x"><style>#x{font-family:A}@keyframes k{' + "a" * 40000 + "</style></div>"
        self.assertFast(wpsafe.check, html)
        self.assertFast(wpsafe.clean, html)

    def test_unclosed_anchors_are_fast(self):
        html = '<div id="x"><style>#x{font-family:A}</style>' + "<a " * 20000 + "</div>"
        self.assertFast(wpsafe.check, html)
        self.assertFast(wpsafe.clean, html)

    def test_banned_url_still_found(self):
        html = ('<div id="x"><style>#x{font-family:A}</style>'
                '<img src="https://img.claudeusercontent.com/a.png"></div>')
        self.assertIn("url", rules(wpsafe.check(html), "error"))


class Helpers(unittest.TestCase):
    def test_every_rule_check_can_emit_has_a_fix(self):
        with open(wpsafe.__file__, encoding="utf-8") as fh:
            src = fh.read()
        emitted = set(re.findall(r'_finding\(\s*"(?:error|warn)",\s*"([\w-]+)"', src))
        self.assertIn("stylesheet-dropped", emitted)
        self.assertEqual(emitted - set(wpsafe._FIX), set())

    def test_fix_prompt_unknown_rule_falls_back(self):
        p = wpsafe.fix_prompt([{"level": "error", "rule": "no-such-rule", "where": "#x",
                                "detail": "d"}])
        self.assertIn("[no-such-rule]", p)
        self.assertIn("Fix the page", p)

    def test_fix_prompt_covers_every_rule(self):
        for rule in ("root", "css-scope", "url", "preview-harness", "base",
                     "script-outside", "head-dropped", "root-font",
                     "stylesheet-dropped"):
            with self.subTest(rule=rule):
                p = wpsafe.fix_prompt([{"level": "error", "rule": rule, "where": "#x",
                                        "detail": "d"}])
                self.assertIn(rule, p)
                self.assertNotIn("TODO", p)

    def test_fix_prompt_empty(self):
        self.assertEqual(wpsafe.fix_prompt([]), "")

    def test_visible_texts_skip_script_and_style(self):
        t = wpsafe.visible_texts('<div id="x"><style>p{}</style><p>活動 資訊</p>'
                                 '<script>var a="no"</script><span>AI IN</span></div>')
        self.assertEqual(t, ["活動 資訊", "AI IN"])

    @needs_page
    def test_visible_texts_on_export(self):
        t = wpsafe.visible_texts(fixture("export.html"))
        self.assertIn("立即報名", t)
        self.assertTrue(any("Enterprise Governance Masterclass" in s for s in t))

    def test_autop_delta(self):
        pushed = '<!-- wp:html --><div id="x"><p>a</p><span></span></div><!-- /wp:html -->'
        self.assertEqual(wpsafe.autop_delta(pushed, '<div id="x"><p>a</p><span></span></div>'), 0)
        self.assertEqual(wpsafe.autop_delta(pushed, '<p></p><div id="x"><p>a</p><p><span></span></p></div>'), 2)


if __name__ == "__main__":
    unittest.main()
