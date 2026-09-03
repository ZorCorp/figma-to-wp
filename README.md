# figma-to-wp

Turn a Figma design into a page on masterconcept.ai.

The script fetches; it never guesses layout. It pulls the frame's render, its
copy verbatim, every box's geometry and its images out of Figma. You write the
HTML against those numbers. Then three checks that answer different questions:
`verify` reads the strings, `diff` reads the pixels, and the side-by-side it
writes is there for you to read yourself.

    extract → author → verify → diff → push

## Where it runs

On your own Mac, through a local terminal — the **Desktop Commander** connector
in Claude Cowork, the **Bash** tool in Claude Code. Both reach the same machine.
Cowork's sandboxed shell is never used: your Figma token and WordPress
application password live in `~/.figma-wp/.env` on the Mac, and a sandbox has
neither and cannot reach a preview server on `127.0.0.1`.

## What you need

- A Mac that is on. In Cowork, Claude Desktop with the Desktop Commander
  connector connected — the only thing you install by hand.
- A Figma personal access token, and a WordPress application password for an
  account that can edit pages. `setup` writes both to `~/.figma-wp/.env`.
- Python 3.9+ (stdlib only). Chrome and Pillow for `diff`; nothing else needs them.

## Using it

Say what you want in plain language — "turn this Figma link into a page under
partners/work-collaboration" — and the skill asks for what it still needs: the
title, the slug, the parent path, the WordPress username.

Pages are created as **drafts**. Nothing is published without you asking.

## What it will not do

Retype your copy. Every string comes from the design file, so if the design says
something wrong, the page says the same wrong thing and the skill tells you —
rather than quietly improving it and leaving you to find out later.
