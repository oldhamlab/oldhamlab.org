#!/usr/bin/env python3
"""Regenerate papers.yml from ORCID + Crossref.

Usage:  python3 tools/update-papers.py [--offline]

ORCID is the starting point but it is neither complete nor clean, so this
script does three things beyond fetching:

  * it seeds the DOI list from the existing papers.yml as well as ORCID, so
    papers ORCID has never picked up (the JCI Insight alanine paper, the
    Comprehensive Physiology review) are not silently dropped on a rebuild;
  * it discards "Author Correction" records and any preprint whose published
    version is also in the list;
  * it merges in tools/papers-overrides.yml, so awards and commentary survive
    regeneration without living in the generated file.

papers.yml is fully generated and safe to delete. Everything hand-written --
awards, commentary, co-first authorship, suppressions, and DOIs ORCID does not
list -- lives in tools/papers-overrides.yml, which this script only ever reads.
"""

import argparse
import json
import pathlib
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

ORCID = "0000-0003-3029-4866"
MAILTO = "william_oldham@brown.edu"
CUTOFF = 2010  # the page is the lab's record, not the full CV

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "tools" / ".crossref-cache.json"
OVERRIDES = ROOT / "tools" / "papers-overrides.yml"

# Surnames printed in bold: lab members past and present, plus the PI.
LAB = {
    "Oldham", "Ziehr", "Li", "Copeland", "McGarrity", "Leahy", "Nguyen",
    "Joseph", "Gottehrer-Cohen", "Khalil", "Ferreyra Faustino", "Lam",
    "Singh", "Nipoti",
}

# Preserved verbatim across regeneration, keyed by DOI.
KEEP_FIELDS = ("award", "note", "equal", "drop")

# Publisher metadata errors in Crossref, corrected on the way through.
TITLE_FIXUPS = {
    "l -2-hydroxyglutarate": "L-2-hydroxyglutarate",
}

MAX_AUTHORS = 8   # beyond this, truncate
HEAD_AUTHORS = 3  # ...to this many, plus the PI


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                          "User-Agent": f"oldhamlab.org (mailto:{MAILTO})"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def orcid_dois():
    d = fetch(f"https://pub.orcid.org/v3.0/{ORCID}/works")
    out = []
    for w in d["group"]:
        for eid in w["external-ids"]["external-id"]:
            if eid["external-id-type"] == "doi":
                out.append(eid["external-id-value"].lower().strip())
                break
    return out


def load_yaml_dois(path):
    """Pull `doi:` values and any hand-written fields out of the existing file.

    Deliberately a regex rather than a YAML parse so the script has no
    third-party dependency -- it has to run for anyone with just Python.
    """
    if not path.exists():
        return {}
    blocks = re.split(r"\n(?=- )", path.read_text(encoding="utf8"))
    kept = {}
    for b in blocks:
        # Anchored to the doi:/path: keys -- a `note:` may cite an editorial's
        # own DOI, and that is commentary, not one of his papers.
        m = (re.search(r"^\s*(?:-\s*)?doi:\s*\"?([^\"\s]+)\"?\s*(?:#.*)?$", b, re.M)
             or re.search(r"^\s*(?:-\s*)?path:\s*\"?https?://doi\.org/(10\.[^\"\s]+)\"?\s*(?:#.*)?$",
                          b, re.M))
        if not m:
            continue
        doi = m.group(1).lower().strip()
        extra = {}
        for f in KEEP_FIELDS:
            fm = re.search(rf'^\s*{f}:\s*(.+)$', b, re.M)
            if fm:
                extra[f] = fm.group(1).strip()
        kept.setdefault(doi, {}).update(extra)  # merge repeated stubs
    return kept


def crossref(dois, offline=False):
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [d for d in dois if d not in cache]
    if todo and offline:
        print(f"  offline: {len(todo)} DOIs not cached, skipping", file=sys.stderr)
    elif todo:
        for i, doi in enumerate(todo, 1):
            url = ("https://api.crossref.org/works/"
                   + urllib.parse.quote(doi, safe="") + f"?mailto={MAILTO}")
            try:
                cache[doi] = fetch(url)["message"]
            except Exception as e:
                print(f"  ! {doi}: {e}", file=sys.stderr)
            if i % 20 == 0:
                print(f"  crossref {i}/{len(todo)}", file=sys.stderr)
            time.sleep(0.06)
        CACHE.write_text(json.dumps(cache))
    return {d: cache[d] for d in dois if d in cache}


def clean(s):
    """Crossref titles carry JATS markup and HTML entities."""
    s = re.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip()
    for bad, good in TITLE_FIXUPS.items():
        s = s.replace(bad, good)
    return s


def year_of(m):
    for k in ("published-print", "published-online", "published", "issued"):
        parts = (m.get(k) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            return parts[0][0]
    return None


def date_of(m):
    for k in ("published-print", "published-online", "published", "issued"):
        parts = (m.get(k) or {}).get("date-parts") or [[None]]
        p = parts[0]
        if p and p[0]:
            return "%04d-%02d-%02d" % (p[0], p[1] if len(p) > 1 else 1,
                                       p[2] if len(p) > 2 else 1)
    return None


def initials(given):
    """'David R.' -> 'DR';  'Wan-Ting' -> 'WT'."""
    return "".join(w[0].upper() for w in re.split(r"[\s.\-]+", given or "") if w)


def fmt_authors(m):
    auths = m.get("author") or []
    names = []
    for a in auths:
        fam = clean(a.get("family") or a.get("name") or "")
        if not fam:
            continue
        ini = initials(a.get("given", ""))
        label = f"{fam} {ini}".strip()
        names.append((fam, f"<strong>{label}</strong>" if fam in LAB else label))

    if not names:
        return ""
    if len(names) <= MAX_AUTHORS:
        return ", ".join(n for _, n in names)

    head = [n for _, n in names[:HEAD_AUTHORS]]
    # Always keep the PI visible, even when the cutoff would hide him.
    pi = next((n for f, n in names[HEAD_AUTHORS:] if f == "Oldham"), None)
    last = names[-1][1]
    tail = []
    if pi and pi != last:
        tail.append(pi)
    out = ", ".join(head) + ", … " + ", ".join(tail + [last]) if tail else \
          ", ".join(head) + ", … " + last
    return out


def md_italics(s):
    """award/note are emitted inside a raw HTML block, where Markdown does not
    run, so *journal names* have to become <em> here."""
    return re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)


def is_correction(title):
    return bool(re.match(r"^(author )?(correction|erratum|corrigendum)\b", title, re.I))


def norm_title(t):
    t = unicodedata.normalize("NFKD", clean(t).lower())
    return re.sub(r"[^a-z0-9]+", "", t)


def canonical(doi):
    """eLife mints a DOI per revision; collapse them onto the article DOI."""
    return re.sub(r"^(10\.7554/elife\.\d+)\.\d+$", r"\1", doi.lower().strip())


def similar(a, b):
    """Jaccard overlap of title words, ignoring stopwords.

    Needed because a title can drift between preprint and publication --
    "signaling" becomes "signalling", hyphens come and go -- so an exact
    match on the normalised string is not enough.
    """
    stop = {"the", "a", "an", "of", "in", "to", "and", "for", "via", "with", "on"}
    ta = {w for w in re.findall(r"[a-z0-9]+", clean(a).lower()) if w not in stop}
    tb = {w for w in re.findall(r"[a-z0-9]+", clean(b).lower()) if w not in stop}
    return len(ta & tb) / max(1, len(ta | tb))


def to_record(doi, m):
    return {
        "doi": doi,
        "title": clean((m.get("title") or [""])[0]),
        "year": year_of(m),
        "date": date_of(m),
        "author": fmt_authors(m),
        "journal": clean((m.get("container-title") or [""])[0]),
        "type": m.get("type"),
        "volume": m.get("volume"),
        "pages": m.get("page") or m.get("article-number"),
        "_relation": m.get("relation") or {},
        "_institution": m.get("institution"),
        "_publisher": m.get("publisher"),
    }


def build():
    print("fetching ORCID…", file=sys.stderr)
    dois = orcid_dois()
    hand = load_yaml_dois(OVERRIDES)
    for d in hand:
        if d not in dois:
            dois.append(d)
    print(f"  {len(dois)} DOIs ({len(hand)} from papers-overrides.yml)", file=sys.stderr)

    meta = crossref(dois)

    # A preprint's `is-preprint-of` often points at a published article that
    # ORCID never recorded. Follow those edges so the published version is
    # listed rather than the preprint.
    extra = []
    for doi, m in meta.items():
        if m.get("type") != "posted-content":
            continue
        for x in (m.get("relation") or {}).get("is-preprint-of", []):
            c = canonical(x.get("id", ""))
            if c and c not in meta and c not in extra:
                extra.append(c)
    if extra:
        print(f"  following {len(extra)} preprint→published link(s)", file=sys.stderr)
        meta.update(crossref(extra))

    records = []
    for doi, m in meta.items():
        r = to_record(doi, m)
        if not r["title"] or not r["year"] or r["year"] < CUTOFF or is_correction(r["title"]):
            continue
        records.append(r)

    pubs = [r for r in records if r["type"] != "posted-content"]
    pub_dois = {canonical(r["doi"]) for r in pubs}

    kept = []
    for r in records:
        if r["type"] == "posted-content":
            rel = {canonical(x.get("id", ""))
                   for x in r["_relation"].get("is-preprint-of", [])}
            if rel & pub_dois:
                continue
            if any(similar(r["title"], p["title"]) >= 0.7 for p in pubs):
                continue
            r["preprint"] = True
            if not r["journal"]:
                # Preprints carry no container-title; the server is in
                # `institution`, falling back to the publisher.
                inst = (r.pop("_institution", None) or [{}])[0].get("name")
                r["journal"] = inst or r.pop("_publisher", None) or "Preprint"
        r.pop("_relation")
        r.pop("_institution", None)
        r.pop("_publisher", None)
        kept.append(r)

    for r in kept:
        r.update(hand.get(r["doi"], {}))
    # `drop: true` in papers.yml suppresses an entry -- used for preprints
    # whose published version Crossref does not link back to.
    dropped = [r for r in kept if str(r.get("drop", "")).lower() == "true"]
    kept = [r for r in kept if r not in dropped]
    if dropped:
        print(f"  {len(dropped)} entr(y/ies) suppressed by drop:", file=sys.stderr)
    kept.sort(key=lambda r: (r["date"] or "", r["title"]), reverse=True)
    return kept


def yaml_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def emit(records):
    out = [
        "# GENERATED by tools/update-papers.py -- every line of this file is",
        "# overwritten on each run. Do not edit it.",
        "#",
        "# Regenerate with:  python3 tools/update-papers.py",
        "#",
        "# Awards, commentary, co-first authorship, suppressions, and papers",
        "# ORCID does not list are maintained in tools/papers-overrides.yml.",
        "",
    ]
    for r in records:
        out.append(f'- title: {yaml_str(r["title"])}')
        out.append(f'  author: {yaml_str(r["author"])}')
        out.append(f'  journal: {yaml_str(r["journal"])}')
        out.append(f'  date: {r["date"]}')
        out.append(f'  path: "https://doi.org/{r["doi"]}"')
        out.append(f'  doi: "{r["doi"]}"')
        if r.get("volume"):
            out.append(f'  volume: {yaml_str(r["volume"])}')
        if r.get("pages"):
            out.append(f'  pages: {yaml_str(r["pages"])}')
        if r.get("preprint"):
            out.append("  preprint: true")
        for f in ("award", "note", "equal"):
            if r.get(f):
                v = r[f].strip()
                if v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                out.append(f"  {f}: {yaml_str(md_italics(v))}")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="use only the Crossref cache; do not hit the network")
    a = ap.parse_args()
    recs = build()
    (ROOT / "papers.yml").write_text(emit(recs), encoding="utf8")
    npre = sum(1 for r in recs if r.get("preprint"))
    print(f"wrote papers.yml: {len(recs)} entries "
          f"({npre} preprints), {recs[-1]['year']}–{recs[0]['year']}", file=sys.stderr)
