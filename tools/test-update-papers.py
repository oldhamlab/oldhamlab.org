#!/usr/bin/env python3
"""Self-check for the fiddly parts of update-papers.py.

Run:  python3 tools/test-update-papers.py

Covers author truncation (the ellipsis is joined by hand), entity cleaning,
and year/date derivation. Everything else in that script is a straight
transcription of a Crossref field and fails loudly if it drifts.
"""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "up", pathlib.Path(__file__).with_name("update-papers.py"))
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)


def crossref_authors(n):
    return {"author": [{"given": "A", "family": f"Fam{i}"} for i in range(n)]}


# Under the cap: every author, comma separated.
assert up.fmt_authors(crossref_authors(3)) == "Fam0 A, Fam1 A, Fam2 A"

# Over it: head authors, then the last one. No comma AFTER the ellipsis --
# that is the whole reason this join is not a plain ", ".join.
got = up.fmt_authors(crossref_authors(12))
assert got == "Fam0 A, Fam1 A, Fam2 A, … Fam11 A", got

# JATS markup and HTML entities both come off.
assert up.clean("<i>Foo</i> &amp; bar &#39;baz&#39;") == "Foo & bar 'baz'"

# year is derived from date, and a record with no date survives it.
assert up.to_record("10.1/x", {"issued": {"date-parts": [[2021, 5, 3]]}})["year"] == 2021
assert up.to_record("10.1/x", {})["year"] is None

print("ok")
