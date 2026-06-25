#!/usr/bin/env python3
"""
build_kg.py — Materialize the UK Biobank Showcase data dictionary as a local
SQLite knowledge graph for offline, agent-driven phenotype field discovery.

Source: UK Biobank Showcase machine-readable schemas (scdown.cgi), the
authoritative bulk dictionaries — NOT scraped HTML. Built once, queried instantly.

No third-party deps (urllib + sqlite3 from stdlib). Fail-loud: any malformed row
or unexpected column count raises immediately rather than being silently skipped.
"""
import argparse
import os
import re
import sqlite3
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(ROOT, "data", "raw")
DB_PATH = os.path.join(ROOT, "data", "ukb_kg.sqlite")

BASE = "https://biobank.ndph.ox.ac.uk/showcase/scdown.cgi?fmt=txt&id="
# schema id -> (local name, expected column count)  [verified against live headers]
SCHEMAS = {
    1:  ("field",       29),
    2:  ("encoding",    7),
    3:  ("category",    6),
    13: ("catbrowse",   3),
    5:  ("esimpint",    4),
    6:  ("esimpstring", 4),
    7:  ("esimpreal",   4),
    8:  ("esimpdate",   4),
    20: ("esimptime",   4),
    11: ("ehierint",    7),
    12: ("ehierstring", 7),
}

# UK Biobank ValueType codes (encoding 100048 "ValueType")
VALUE_TYPES = {
    11: "Integer", 21: "Categorical (single)", 22: "Categorical (multiple)",
    31: "Continuous", 41: "Text", 51: "Date", 61: "Time", 101: "Compound",
}

# Refusal / true-missing meanings -> map to NaN before encoding.
MISSING_RE = re.compile(
    r"do not know|don't know|prefer not to answer|not applicable|not known|"
    r"do not remember|measurement (procedure|abandoned)|measure (was )?not|"
    r"not (recorded|performed|measured|available)|unsure|no data|^missing$",
    re.I,
)
# Bounded sentinels that ARE meaningful ("Less than one") -> need a domain mapping, not NaN.
BOUNDED_RE = re.compile(r"less than|more than|or more|or less|at least|up to|greater than", re.I)


def download(schema_id, name, refresh):
    path = os.path.join(RAW_DIR, name + ".tsv")
    if os.path.exists(path) and not refresh:
        return path
    os.makedirs(RAW_DIR, exist_ok=True)
    url = BASE + str(schema_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ukb-kg-builder)"})
    sys.stderr.write(f"  downloading {name} (id={schema_id}) ...\n")
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    if not data or b"\t" not in data.split(b"\n", 1)[0]:
        raise RuntimeError(f"FAIL: {url} returned no TSV header (got {len(data)} bytes). "
                           f"Network blocked or endpoint changed.")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _decode(raw):
    """UKB schemas are mostly UTF-8 but some are Windows-1252 (e.g. em-dash 0x97)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_tsv(path, name, ncol):
    """Yield rows as lists; fail loud on any column-count mismatch."""
    with open(path, "rb") as f:
        text = _decode(f.read())
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    header = lines[0].split("\t")
    if len(header) != ncol:
        raise RuntimeError(f"FAIL {name}: header has {len(header)} cols, expected {ncol}: {header}")
    for i, ln in enumerate(lines[1:], start=2):
        cells = ln.split("\t")
        if len(cells) != ncol:
            raise RuntimeError(f"FAIL {name}:{i}: {len(cells)} cols, expected {ncol}: {ln[:160]!r}")
        yield cells
    sys.stderr.write(f"  parsed {name}: {len(lines)-1} rows\n")


def _int(x):
    x = (x or "").strip()
    if x == "":
        return None
    try:
        return int(x)
    except ValueError:
        return None


def classify_special(value, meaning):
    """Return (is_special, special_kind): 'missing'|'bounded'|'sentinel'|'normal'.
    'missing' -> NaN; 'bounded' -> needs domain numeric mapping; 'sentinel' -> negative,
    review; 'normal' -> ordinary value. Lets the agent encode correctly instead of
    blanket-nulling every negative code."""
    m = meaning or ""
    if MISSING_RE.search(m):
        return 1, "missing"
    # UKB special codes are conventionally NEGATIVE; positive ordinals like
    # "More than half the days" (a real Likert level) must stay 'normal'.
    v = _int(value)
    if v is not None and v < 0:
        return 1, "bounded" if BOUNDED_RE.search(m) else "sentinel"
    return 0, "normal"


def build(refresh):
    t0 = time.time()
    paths = {name: download(sid, name, refresh) for sid, (name, _) in SCHEMAS.items()}

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE categories(category_id INTEGER PRIMARY KEY, title TEXT,
        availability INTEGER, group_type INTEGER, descript TEXT, notes TEXT);
    CREATE TABLE catbrowse(parent_id INTEGER, child_id INTEGER, showcase_order INTEGER);
    CREATE TABLE fields(field_id INTEGER PRIMARY KEY, title TEXT, value_type INTEGER,
        value_type_label TEXT, base_type INTEGER, item_type INTEGER, strata INTEGER,
        instanced INTEGER, arrayed INTEGER, sexed INTEGER, units TEXT,
        main_category INTEGER, encoding_id INTEGER, instance_min INTEGER,
        instance_max INTEGER, array_min INTEGER, array_max INTEGER, notes TEXT,
        num_participants INTEGER, debut TEXT, category_path TEXT);
    CREATE TABLE encodings(encoding_id INTEGER PRIMARY KEY, title TEXT, coded_as INTEGER,
        structure INTEGER, num_members INTEGER, descript TEXT);
    CREATE TABLE codings(encoding_id INTEGER, value TEXT, meaning TEXT, parent_id INTEGER,
        code_id INTEGER, selectable INTEGER, kind TEXT, is_special INTEGER, special_kind TEXT);
    """)

    # categories
    cat_rows = list(read_tsv(paths["category"], "category", 6))
    db.executemany("INSERT INTO categories VALUES(?,?,?,?,?,?)",
                   [(_int(c[0]), c[1], _int(c[2]), _int(c[3]), c[4], c[5]) for c in cat_rows])

    # catbrowse (category hierarchy edges)
    cb = [( _int(c[0]), _int(c[1]), _int(c[2])) for c in read_tsv(paths["catbrowse"], "catbrowse", 3)]
    db.executemany("INSERT INTO catbrowse VALUES(?,?,?)", cb)
    db.execute("CREATE INDEX idx_cb_parent ON catbrowse(parent_id)")
    db.execute("CREATE INDEX idx_cb_child ON catbrowse(child_id)")

    # build category path (root -> ... -> category) via child->parent map
    parent_of = {}
    for p, c, _o in cb:
        parent_of.setdefault(c, p)  # first parent wins for display
    cat_title = {_int(c[0]): c[1] for c in cat_rows}

    def path_for(cat_id):
        chain, seen = [], set()
        cur = cat_id
        while cur is not None and cur not in seen and cur in cat_title:
            seen.add(cur)
            chain.append(cat_title[cur])
            cur = parent_of.get(cur)
        return " > ".join(reversed(chain))

    # encodings
    db.executemany("INSERT INTO encodings VALUES(?,?,?,?,?,?)",
                   [(_int(c[0]), c[1], _int(c[2]), _int(c[3]), _int(c[4]), c[5])
                    for c in read_tsv(paths["encoding"], "encoding", 7)])

    # fields
    frows = []
    for c in read_tsv(paths["field"], "field", 29):
        vt = _int(c[5])
        frows.append((
            _int(c[0]), c[1], vt, VALUE_TYPES.get(vt, f"type{vt}"), _int(c[6]), _int(c[7]),
            _int(c[8]), _int(c[9]), _int(c[10]), _int(c[11]), c[12], _int(c[13]), _int(c[14]),
            _int(c[16]), _int(c[17]), _int(c[18]), _int(c[19]), c[20], _int(c[23]), c[21],
            path_for(_int(c[13])),
        ))
    db.executemany("INSERT INTO fields VALUES(" + ",".join("?"*21) + ")", frows)
    db.execute("CREATE INDEX idx_fields_cat ON fields(main_category)")
    db.execute("CREATE INDEX idx_fields_enc ON fields(encoding_id)")

    # codings: union of simple + hierarchical value tables
    simple = ["esimpint", "esimpstring", "esimpreal", "esimpdate", "esimptime"]
    hier = ["ehierint", "ehierstring"]
    cod = []
    for name in simple:
        kind = name.replace("esimp", "")
        for c in read_tsv(paths[name], name, 4):
            isp, sk = classify_special(c[1], c[2])
            cod.append((_int(c[0]), c[1], c[2], None, None, None, kind, isp, sk))
    for name in hier:
        kind = name.replace("e", "")  # hierint / hierstring
        for c in read_tsv(paths[name], name, 7):
            isp, sk = classify_special(c[3], c[4])
            cod.append((_int(c[0]), c[3], c[4], _int(c[2]), _int(c[1]), _int(c[5]), kind, isp, sk))
    db.executemany("INSERT INTO codings VALUES(?,?,?,?,?,?,?,?,?)", cod)
    db.execute("CREATE INDEX idx_codings_enc ON codings(encoding_id)")

    # Vertical search axis: FTS over code MEANINGS (disease/drug/job/food names live
    # here, not in field titles). meaning -> encoding -> fields-that-use-it.
    db.execute("""CREATE VIRTUAL TABLE codings_fts USING fts5(
        meaning, encoding_id UNINDEXED, value UNINDEXED, tokenize='porter unicode61')""")
    db.execute("""INSERT INTO codings_fts(meaning, encoding_id, value)
                  SELECT meaning, encoding_id, value FROM codings""")

    # FTS over fields for candidate search
    db.executescript("""
    CREATE VIRTUAL TABLE fields_fts USING fts5(
        title, notes, category_path, field_id UNINDEXED,
        tokenize='porter unicode61');
    """)
    db.execute("""INSERT INTO fields_fts(title, notes, category_path, field_id)
                  SELECT title, notes, category_path, field_id FROM fields""")

    # meta
    showver = ""
    try:
        with open(paths["field"]) as f:
            pass
    except Exception:
        pass
    db.executemany("INSERT INTO meta VALUES(?,?)", [
        ("source", "UK Biobank Showcase scdown.cgi schemas (1,2,3,13,5,6,7,8,20,11,12)"),
        ("base_url", BASE),
        ("build_unixtime", str(int(t0))),
        ("n_fields", str(len(frows))),
        ("n_categories", str(len(cat_rows))),
        ("n_codings", str(len(cod))),
        ("n_encodings", str(db.execute('SELECT COUNT(*) FROM encodings').fetchone()[0])),
    ])
    db.commit()
    db.execute("VACUUM")
    db.close()
    sz = os.path.getsize(DB_PATH) / 1e6
    sys.stderr.write(f"\nOK  {DB_PATH}  ({sz:.1f} MB)  fields={len(frows)} "
                     f"codings={len(cod)} cats={len(cat_rows)}  in {time.time()-t0:.1f}s\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="re-download raw schemas (default: use cached data/raw/*.tsv)")
    build(ap.parse_args().refresh)
