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

# The roster is read from people/*.qmd -- see load_roster(). Nothing about
# who is in the lab is hardcoded here.
PEOPLE = ROOT / "people"

# Preserved verbatim across regeneration, keyed by DOI.
KEEP_FIELDS = ("award", "drop")

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


MANUAL_FIELDS = ("title", "author", "journal", "date", "pages", "volume", "path",
                 "editors", "edition", "publisher", "place")


def unquote(v):
    v = v.strip()
    if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def load_overrides(path):
    """Parse tools/papers-overrides.yml into (annotations, manual records).

    Deliberately a hand-rolled parse rather than PyYAML so the script has no
    third-party dependency -- it has to run for anyone with just Python.

    An entry carrying a `title:` is a complete record and is used as-is: book
    chapters have no Crossref DOI, so there is nothing to look up. An entry
    with only a `doi:` is either a seed (pull this paper in) or an annotation
    (attach an award or commentary to it).
    """
    if not path.exists():
        return {}, []
    blocks = re.split(r"\n(?=- )", path.read_text(encoding="utf8"))
    ann, manual = {}, []
    for b in blocks:
        if not b.lstrip().startswith("- "):
            continue
        fields = {}
        for f in KEEP_FIELDS + MANUAL_FIELDS:
            # Anchored to the key -- a `note:` may cite an editorial's own DOI,
            # and that is commentary, not one of his papers.
            fm = re.search(rf"^\s*(?:-\s*)?{f}:\s*(.+?)\s*(?:#[^\"']*)?$", b, re.M)
            if fm:
                fields[f] = unquote(fm.group(1))
        notes = [unquote(x) for x in
                 re.findall(r"^\s*(?:-\s*)?note:\s*(.+?)\s*$", b, re.M)]
        if notes:
            fields["notes"] = notes
        m = (re.search(r"^\s*(?:-\s*)?doi:\s*\"?([^\"\s]+)\"?\s*(?:#.*)?$", b, re.M)
             or re.search(r"^\s*(?:-\s*)?path:\s*\"?https?://doi\.org/(10\.[^\"\s]+)\"?\s*(?:#.*)?$",
                          b, re.M))
        if fields.get("title"):
            fields["doi"] = m.group(1).lower().strip() if m else None
            manual.append(fields)
        elif m:
            doi = m.group(1).lower().strip()
            extra = {k: v for k, v in fields.items()
                     if k in KEEP_FIELDS or k == "notes"}
            prev = ann.setdefault(doi, {})
            if "notes" in prev and "notes" in extra:   # merge across stubs
                extra["notes"] = prev["notes"] + extra["notes"]
            prev.update(extra)
    return ann, manual


def name_key(family, given):
    """(surname, first initial), casefolded.

    Matching on the surname alone bolded every Li, Nguyen and Singh in the
    author lists regardless of who they were. Including the first initial
    separates them, and because it ignores middle initials it also collapses
    "David R." / "David R" / "David" onto one person.
    """
    fam = re.sub(r"\s+", " ", (family or "").strip()).casefold()
    ini = (given or "").strip()[:1].upper()
    return (fam, ini)


def load_roster():
    """Map a Crossref name onto the person's page.

    people/*.qmd is the source of truth. `pub-match:` is the name as Crossref
    records it.

    `pub-cite:` is a CORRECTION, not a house style: names print as the
    bibliographic record has them, so the same person legitimately appears as
    "Ziehr D" on one paper and "Ziehr DR" on another. Set pub-cite only where
    Crossref has split family/given wrongly -- Diana Ferreyra Faustino is
    stored as family "Faustino", given "Diana E. Ferreyra", which would print
    as "Faustino DEF".
    """
    roster = {}
    for f in sorted(PEOPLE.glob("*.qmd")):
        if f.name.startswith("_"):
            continue
        head = f.read_text(encoding="utf8").split("---")[1]
        m = re.search(r'^pub-match:\s*"([^"]+)"', head, re.M)
        if not m:
            continue
        fam, _, ini = m.group(1).rpartition(" ")
        cite = re.search(r'^pub-cite:\s*"([^"]+)"', head, re.M)
        roster[name_key(fam, ini)] = {
            "slug": f.stem,
            "cite": cite.group(1) if cite else None,
        }
    return roster


ROSTER = load_roster()


def mark(key, label):
    """Bold a lab member and link them to their page; pass others through."""
    who = ROSTER.get(key)
    if not who:
        return label
    return (f'<a href="/people/{who["slug"]}.html">'
            f'<strong>{who["cite"] or label}</strong></a>')


def bold_lab(authors):
    """Bold lab surnames in a hand-written author string."""
    out = []
    for n in authors.split(","):
        n = n.strip()
        fam, _, ini = n.rpartition(" ")
        out.append(mark(name_key(fam, ini), n))
    return ", ".join(out)


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
    # Radiolabel notation: "[18F]" -> "[<sup>18</sup>F]". Crossref stores these
    # flat, with no markup to preserve.
    s = re.sub(r"\[(\d+)([A-Z][a-z]?)\]", r"[<sup>\1</sup>\2]", s)
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
    names = []
    for a in (m.get("author") or []):
        fam = clean(a.get("family") or a.get("name") or "")
        if not fam:
            continue
        key = name_key(fam, a.get("given"))
        label = f"{fam} {initials(a.get('given', ''))}".strip()
        names.append((key, mark(key, label), key in ROSTER))

    n = len(names)
    if not n:
        return ""
    if n <= MAX_AUTHORS:
        return ", ".join(x for _, x, _ in names)

    # Keep the head, the last author, and EVERY lab member. Truncating to the
    # PI alone hid the rest of the lab on multi-author papers -- Diana, Aseel
    # and Hilaire all vanished from the JCI Insight alanine paper.
    keep = set(range(min(HEAD_AUTHORS, n))) | {n - 1}
    keep |= {i for i, (_, _, is_lab) in enumerate(names) if is_lab}

    parts, gap = [], False
    for i in range(n):
        if i in keep:
            parts.append(names[i][1])
            gap = False
        elif not gap:
            parts.append("\u2026")
            gap = True

    out = ""
    for i, x in enumerate(parts):
        if i == 0:
            out = x
        elif x == "\u2026":
            out += ", " + x
        elif parts[i - 1] == "\u2026":
            out += " " + x
        else:
            out += ", " + x
    return out


def md_italics(s):
    """award/note are emitted inside a raw HTML block, where Markdown does not
    run, so *journal names* have to become <em> here."""
    return re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)


def build_source(r):
    """The citation line under the title, as HTML.

    Chapters need the full In:/editors/publisher form -- journal-style
    "Book. 291-292" reads as a truncated citation.
    """
    book = f"<em>{r['journal']}</em>" if r.get("journal") else ""
    if r.get("chapter"):
        bits = []
        if r.get("editors"):
            bits.append(f"In: {r['editors']}, editors.")
        bits.append(book + ".")
        if r.get("edition"):
            bits.append(r["edition"])
        imprint = r.get("publisher") or ""
        if r.get("place"):
            imprint = f"{r['place']}: {imprint}" if imprint else r["place"]
        if imprint:
            bits.append(f"{imprint};")
        if r.get("year"):
            bits.append(f"{r['year']}.")
        if r.get("pages"):
            bits.append(f"p. {r['pages']}.")
        return " ".join(x for x in bits if x)

    # Book titles already end in a full stop; journals do not.
    sep = " " if r.get("journal", "").endswith(".") else ". "
    out = book
    if r.get("volume"):
        out += sep + str(r["volume"])
        if r.get("pages"):
            out += ":" + str(r["pages"])
    elif r.get("pages"):
        out += sep + str(r["pages"])
    return out


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
    hand, manual = load_overrides(OVERRIDES)
    for d in hand:
        if d not in dois:
            dois.append(d)
    print(f"  {len(dois)} DOIs ({len(hand)} from papers-overrides.yml), "
          f"{len(manual)} manual entries", file=sys.stderr)

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

    # Book chapters have no Crossref record, so they are authored in full in
    # the overrides file and merged in here.
    seen = {r["doi"] for r in kept if r["doi"]}
    for f in manual:
        if f.get("doi") and f["doi"] in seen:
            continue
        d = f.get("date") or ""
        kept.append({
            "doi": f.get("doi"),
            "title": f["title"],
            "author": bold_lab(f.get("author", "")),
            "journal": f.get("journal", ""),
            "date": d if len(d) == 10 else f"{d[:4]}-01-01",
            "year": int(d[:4]) if d[:4].isdigit() else None,
            "volume": f.get("volume"),
            "pages": f.get("pages"),
            "path": f.get("path"),
            "chapter": True,
            "editors": f.get("editors"),
            "edition": f.get("edition"),
            "publisher": f.get("publisher"),
            "place": f.get("place"),
            "type": "book-chapter",
            **{k: v for k, v in f.items() if k in KEEP_FIELDS},
        })

    # `drop: true` suppresses an entry -- used for preprints whose published
    # version Crossref does not link back to.
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
        # A chapter with neither DOI nor URL has nothing to link to; the
        # template falls back to plain text when `path` is absent.
        path = r.get("path") or (f'https://doi.org/{r["doi"]}' if r.get("doi") else None)
        if path:
            out.append(f"  path: {yaml_str(path)}")
        if r.get("doi"):
            out.append(f'  doi: "{r["doi"]}"')
        out.append(f'  source: {yaml_str(build_source(r))}')
        if r.get("award"):
            out.append(f'  award: {yaml_str(md_italics(unquote(r["award"])))}')
        if r.get("notes"):
            out.append("  notes:")
            for n in r["notes"]:
                out.append(f"    - {yaml_str(md_italics(unquote(n)))}")
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
