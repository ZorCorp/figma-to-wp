"""Where the real-page fixtures are, if this checkout has them.

export.html, pushed-before.html and expected-body.html are a real page
(masterconcept.ai post 78436). They are page data, so they never live in this
public repo: they sit in the private page repo (mcailab/figma-page-build) under
build/ai-in-motion-cd/test-fixtures/, which carries this repo as a submodule.

Found, in order: $FIGMA_WP_PAGE_FIXTURES, then that folder in any parent
directory. Without them every test that needs them skips.
"""
import os
import unittest

NAMES = ("export.html", "pushed-before.html", "expected-body.html")
_REL = os.path.join("build", "ai-in-motion-cd", "test-fixtures")


def _find():
    env = os.environ.get("FIGMA_WP_PAGE_FIXTURES")
    if env:
        return env
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        cand = os.path.join(d, _REL)
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


PAGE_FIX = _find()
PAGE_FIXTURES = bool(PAGE_FIX) and all(
    os.path.exists(os.path.join(PAGE_FIX, n)) for n in NAMES)
needs_page = unittest.skipUnless(
    PAGE_FIXTURES, "real-page fixtures not found (set FIGMA_WP_PAGE_FIXTURES, "
                   "or run inside mcailab/figma-page-build)")
