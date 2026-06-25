#!/usr/bin/env python3
"""
kg_query.py — CLI over the UK Biobank Showcase knowledge graph (ukb_kg.sqlite).

Designed for a coding agent: each subcommand prints JSON to stdout. No server,
no network — the agent shells out, reads JSON back, and reasons over candidates.

  search   "<text>" [--limit N] [--category CID] [--type T] [--all]
  field    <field_id>
  coding   <encoding_id> [--limit N] [--specials-only]
  category <category_id>
  tree     <category_id> [--up | --down]
  stats

Examples:
  python3 kg_query.py search "fresh fruit intake" --limit 15
  python3 kg_query.py field 1309
  python3 kg_query.py coding 100020       # value->meaning map for an encoding
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "ukb_kg.sqlite")


def db():
    if not os.path.exists(DB_PATH):
        sys.exit(f"FAIL: KG not found at {DB_PATH}. Run: python3 scripts/build_kg.py")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


_LAST = None


def out(obj):
    global _LAST
    _LAST = obj
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _audit(a):
    """Append a replayable JSONL record: query + full result + KG version.
    The deterministic half of the audit trail (the judgment half is the agent's
    decision ledger — see SKILL.md)."""
    ver = db().execute("SELECT value FROM meta WHERE key='build_unixtime'").fetchone()
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "kg_version": ver["value"] if ver else None, "cmd": a.cmd,
           "args": {k: v for k, v in vars(a).items() if k not in ("fn", "log", "cmd")},
           "result": _LAST}
    with open(a.log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _fts_query(text, match_all):
    toks = re.findall(r"[A-Za-z0-9]+", text.lower())
    if not toks:
        sys.exit("FAIL: empty search query")
    joiner = " AND " if match_all else " OR "
    return joiner.join(f'"{t}"' for t in toks)


def cmd_search(a):
    con = db()
    where, params = ["fields_fts MATCH ?"], [_fts_query(a.query, a.all)]
    if a.category is not None:
        where.append("f.main_category = ?"); params.append(a.category)
    if a.type:
        where.append("f.value_type_label LIKE ?"); params.append(f"%{a.type}%")
    sql = f"""
        SELECT f.field_id, f.title, f.value_type_label AS type, f.units,
               f.num_participants AS n, f.encoding_id, f.main_category AS category_id,
               f.category_path, substr(f.notes,1,240) AS notes,
               bm25(fields_fts, 10.0, 1.0, 3.0) AS score   -- weight title >> path > notes
        FROM fields_fts JOIN fields f ON f.field_id = fields_fts.field_id
        WHERE {' AND '.join(where)}
        ORDER BY score LIMIT ?"""
    params.append(a.limit)
    rows = [dict(r) for r in con.execute(sql, params)]
    for r in rows:
        r["score"] = round(r.pop("score"), 2)
    out({"query": a.query, "match": "all" if a.all else "any", "count": len(rows), "results": rows})


def _column_template(f):
    """Classic ukbconv extract column naming: {field}-{instance}.{array}."""
    arr_hi = f["array_max"] if f["arrayed"] else 0
    examples = [f'{f["field_id"]}-0.{a}' for a in range(0, min((arr_hi or 0) + 1, 3))]
    return {
        "pattern": f'{f["field_id"]}-{{instance}}.{{array}}',
        "rap_pattern": f'p{f["field_id"]}_i{{instance}}_a{{array}}',
        "instanced": f["instanced"], "arrayed": f["arrayed"],
        "instance_range": [f["instance_min"], f["instance_max"]],
        "array_range": [f["array_min"], f["array_max"]],
        "example_columns": examples,
    }


def cmd_search_code(a):
    """Vertical search: find a concept among CODE VALUES (ICD/drug/job/food names),
    then report which encoding holds it and which fields use that encoding."""
    con = db()
    rows = con.execute(
        "SELECT encoding_id, value, meaning, bm25(codings_fts) AS score "
        "FROM codings_fts WHERE codings_fts MATCH ? ORDER BY score LIMIT ?",
        (_fts_query(a.query, a.all), a.limit)).fetchall()
    matches = [{"encoding_id": r["encoding_id"], "value": r["value"], "meaning": r["meaning"]}
               for r in rows]
    counts = {}
    for r in rows:
        counts[r["encoding_id"]] = counts.get(r["encoding_id"], 0) + 1
    enc_summary = []
    for eid, n in counts.items():
        e = con.execute("SELECT title FROM encodings WHERE encoding_id=?", (eid,)).fetchone()
        used = [{"field_id": f["field_id"], "title": f["title"]} for f in con.execute(
            "SELECT field_id, title FROM fields WHERE encoding_id=? ORDER BY field_id LIMIT 25", (eid,))]
        enc_summary.append({"encoding_id": eid, "encoding_title": e["title"] if e else None,
                            "n_matched_codes": n, "actionable": bool(used), "used_by_fields": used})
    if a.actionable_only:
        enc_summary = [e for e in enc_summary if e["actionable"]]
    # actionable (field-backed) encodings first, then by number of matched codes
    enc_summary.sort(key=lambda x: (not x["actionable"], -x["n_matched_codes"]))
    out({"query": a.query, "n_matched_codes": len(matches), "matches": matches,
         "encodings": enc_summary,
         "note": "actionable=true => encoding is used by a showcase field you can extract. "
                 "actionable=false => code exists only in record-level/linked data (e.g. GP "
                 "Read/CTV3), not a simple field. Hierarchical codes (ICD/OPCS): use "
                 "`coding <encoding_id>` to expand the subtree via parent_id."})


def cmd_fields_using(a):
    con = db()
    rows = [{"field_id": r["field_id"], "title": r["title"], "type": r["value_type_label"],
             "category_path": r["category_path"], "n": r["num_participants"]}
            for r in con.execute("SELECT field_id, title, value_type_label, category_path, "
                                 "num_participants FROM fields WHERE encoding_id=? ORDER BY field_id",
                                 (a.encoding_id,))]
    e = con.execute("SELECT title FROM encodings WHERE encoding_id=?", (a.encoding_id,)).fetchone()
    out({"encoding_id": a.encoding_id, "encoding_title": e["title"] if e else None,
         "n_fields": len(rows), "fields": rows})


def _node(t, i, label):
    return {"type": t, "id": i, "label": label}


def cmd_neighbors(a):
    """Uniform graph adjacency for any node — parents (up), children (down), and
    siblings where meaningful. The deterministic graph-walk primitive: seed with
    `search`, then navigate by structure instead of fuzzy matching."""
    con = db()
    LIM = 100
    t = a.node_type

    if t == "category":
        cid = int(a.id)
        c = con.execute("SELECT category_id,title FROM categories WHERE category_id=?", (cid,)).fetchone()
        if not c:
            sys.exit(f"FAIL: category {cid} not found")
        parents = [_node("category", r["category_id"], r["title"]) for r in con.execute(
            "SELECT c.category_id,c.title FROM catbrowse b JOIN categories c "
            "ON c.category_id=b.parent_id WHERE b.child_id=?", (cid,))]
        childcats = [_node("category", r["category_id"], r["title"]) for r in con.execute(
            "SELECT c.category_id,c.title FROM catbrowse b JOIN categories c "
            "ON c.category_id=b.child_id WHERE b.parent_id=? ORDER BY b.showcase_order", (cid,))]
        fields = [_node("field", r["field_id"], r["title"]) for r in con.execute(
            "SELECT field_id,title FROM fields WHERE main_category=? ORDER BY field_id", (cid,))]
        children = childcats + fields
        res = {"node": _node("category", c["category_id"], c["title"]),
               "parents": parents, "children": children[:LIM],
               "hint": "children = subcategories (type=category) then fields (type=field)."}
        if len(children) > LIM:
            res["children_total"] = len(children)
    elif t == "field":
        fid = int(a.id)
        f = con.execute("SELECT field_id,title,main_category,encoding_id FROM fields WHERE field_id=?",
                        (fid,)).fetchone()
        if not f:
            sys.exit(f"FAIL: field {fid} not found")
        parents = []
        c = con.execute("SELECT category_id,title FROM categories WHERE category_id=?",
                        (f["main_category"],)).fetchone()
        if c:
            parents.append(_node("category", c["category_id"], c["title"]))
        children = []
        if f["encoding_id"]:
            e = con.execute("SELECT encoding_id,title FROM encodings WHERE encoding_id=?",
                            (f["encoding_id"],)).fetchone()
            if e:
                children.append(_node("encoding", e["encoding_id"], e["title"]))
        sibs = [_node("field", r["field_id"], r["title"]) for r in con.execute(
            "SELECT field_id,title FROM fields WHERE main_category=? AND field_id!=? ORDER BY field_id",
            (f["main_category"], fid))]
        res = {"node": _node("field", f["field_id"], f["title"]),
               "parents": parents, "children": children, "siblings": sibs[:LIM],
               "hint": "siblings = same-category fields (your completeness pool). "
                       "children = the encoding; expand it with `neighbors encoding <id>`."}
        if len(sibs) > LIM:
            res["siblings_total"] = len(sibs)
    elif t == "encoding":
        eid = int(a.id)
        e = con.execute("SELECT encoding_id,title FROM encodings WHERE encoding_id=?", (eid,)).fetchone()
        if not e:
            sys.exit(f"FAIL: encoding {eid} not found")
        usedby = [_node("field", r["field_id"], r["title"]) for r in con.execute(
            "SELECT field_id,title FROM fields WHERE encoding_id=? ORDER BY field_id", (eid,))]
        codes = [_node("code", f"{eid}:{r['value']}", r["meaning"]) for r in con.execute(
            "SELECT value,meaning FROM codings WHERE encoding_id=? ORDER BY CAST(value AS INTEGER),value",
            (eid,))]
        res = {"node": _node("encoding", e["encoding_id"], e["title"]),
               "parents": usedby[:LIM], "children": codes[:LIM],
               "hint": "parents = fields that use this encoding (extractable). "
                       "children = code values; full map via `coding <id>`."}
        if len(usedby) > LIM:
            res["parents_total"] = len(usedby)
        if len(codes) > LIM:
            res["children_total"] = len(codes)
    elif t == "code":
        if a.value is None:
            sys.exit("FAIL: `neighbors code <encoding_id> <value>` needs a code value")
        eid = int(a.id); val = a.value
        row = con.execute("SELECT value,meaning,code_id,parent_id FROM codings "
                          "WHERE encoding_id=? AND value=?", (eid, val)).fetchone()
        if not row:
            sys.exit(f"FAIL: code {val!r} not in encoding {eid}")
        parents = []
        if row["parent_id"]:
            p = con.execute("SELECT value,meaning FROM codings WHERE encoding_id=? AND code_id=?",
                            (eid, row["parent_id"])).fetchone()
            if p:
                parents.append(_node("code", f"{eid}:{p['value']}", p["meaning"]))
        e = con.execute("SELECT encoding_id,title FROM encodings WHERE encoding_id=?", (eid,)).fetchone()
        if e:
            parents.append(_node("encoding", e["encoding_id"], e["title"]))
        children = [_node("code", f"{eid}:{r['value']}", r["meaning"]) for r in con.execute(
            "SELECT value,meaning FROM codings WHERE encoding_id=? AND parent_id=? ORDER BY value",
            (eid, row["code_id"]))] if row["code_id"] else []
        res = {"node": _node("code", f"{eid}:{val}", row["meaning"]),
               "parents": parents, "children": children[:LIM],
               "hint": "hierarchical code walk (ICD/OPCS/SOC): children = sub-codes; "
                       "descend to leaves to expand a disease/procedure subtree."}
        if len(children) > LIM:
            res["children_total"] = len(children)
    out(res)


def cmd_field(a):
    con = db()
    f = con.execute("SELECT * FROM fields WHERE field_id=?", (a.field_id,)).fetchone()
    if not f:
        sys.exit(f"FAIL: field {a.field_id} not found")
    f = dict(f)
    res = {
        "field_id": f["field_id"], "title": f["title"], "type": f["value_type_label"],
        "units": f["units"], "encoding_id": f["encoding_id"],
        "num_participants": f["num_participants"], "category_path": f["category_path"],
        "category_id": f["main_category"], "notes": f["notes"],
        "columns": _column_template(f),
    }
    enc = f["encoding_id"]
    if enc and enc != 0:
        e = con.execute("SELECT * FROM encodings WHERE encoding_id=?", (enc,)).fetchone()
        codes = con.execute(
            "SELECT value, meaning, is_special, special_kind, parent_id FROM codings "
            "WHERE encoding_id=? ORDER BY is_special, CAST(value AS INTEGER), value LIMIT 200",
            (enc,)).fetchall()
        res["coding"] = {
            "encoding_id": enc, "title": e["title"] if e else None,
            "n_members": e["num_members"] if e else None,
            "values": [dict(c) for c in codes],
            "missing_codes": [dict(c) for c in codes if c["special_kind"] == "missing"],
            "bounded_codes": [dict(c) for c in codes if c["special_kind"] == "bounded"],
            "note": "special_kind: 'missing' (Do not know / Prefer not to answer) -> set NaN; "
                    "'bounded' ('Less than one' etc.) -> assign a domain numeric value, do NOT null; "
                    "'sentinel' (other negative) -> review; 'normal' -> ordinary value.",
        }
    out(res)


def cmd_coding(a):
    con = db()
    q = "SELECT value, meaning, is_special, special_kind, parent_id, kind FROM codings WHERE encoding_id=?"
    if a.specials_only:
        q += " AND is_special=1"
    q += " ORDER BY is_special, CAST(value AS INTEGER), value LIMIT ?"
    rows = [dict(r) for r in con.execute(q, (a.encoding_id, a.limit))]
    if not rows:
        sys.exit(f"FAIL: no codes for encoding {a.encoding_id}")
    e = con.execute("SELECT * FROM encodings WHERE encoding_id=?", (a.encoding_id,)).fetchone()
    out({"encoding_id": a.encoding_id, "title": e["title"] if e else None,
         "n_members": e["num_members"] if e else None, "count": len(rows), "values": rows})


def cmd_category(a):
    con = db()
    c = con.execute("SELECT * FROM categories WHERE category_id=?", (a.category_id,)).fetchone()
    if not c:
        sys.exit(f"FAIL: category {a.category_id} not found")
    children = [dict(r) for r in con.execute(
        "SELECT c.category_id, c.title FROM catbrowse b JOIN categories c "
        "ON c.category_id=b.child_id WHERE b.parent_id=? ORDER BY b.showcase_order",
        (a.category_id,))]
    fields = [dict(r) for r in con.execute(
        "SELECT field_id, title, value_type_label AS type, num_participants AS n "
        "FROM fields WHERE main_category=? ORDER BY field_id", (a.category_id,))]
    out({"category_id": c["category_id"], "title": c["title"], "descript": c["descript"],
         "notes": c["notes"], "child_categories": children, "n_fields": len(fields),
         "fields": fields})


def cmd_tree(a):
    con = db()
    if a.up:
        sql = """WITH RECURSIVE up(id) AS (
                   SELECT ? UNION SELECT b.parent_id FROM catbrowse b JOIN up ON b.child_id=up.id)
                 SELECT c.category_id, c.title FROM up JOIN categories c ON c.category_id=up.id
                 WHERE up.id != ?"""
        rows = [dict(r) for r in con.execute(sql, (a.category_id, a.category_id))]
        out({"category_id": a.category_id, "direction": "ancestors", "nodes": rows})
    else:
        sql = """WITH RECURSIVE down(id, depth) AS (
                   SELECT ?, 0 UNION SELECT b.child_id, depth+1 FROM catbrowse b JOIN down ON b.parent_id=down.id)
                 SELECT c.category_id, c.title, down.depth FROM down JOIN categories c ON c.category_id=down.id
                 WHERE down.id != ? ORDER BY depth, c.category_id"""
        rows = [dict(r) for r in con.execute(sql, (a.category_id, a.category_id))]
        out({"category_id": a.category_id, "direction": "descendants", "count": len(rows), "nodes": rows})


def cmd_stats(a):
    con = db()
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    out({"db_path": os.path.abspath(DB_PATH),
         "size_mb": round(os.path.getsize(DB_PATH) / 1e6, 1), "meta": meta})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log", metavar="PATH",
                        help="append a JSONL audit record (query + full result + KG version)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", parents=[common]); s.add_argument("query")
    s.add_argument("--limit", type=int, default=20); s.add_argument("--category", type=int)
    s.add_argument("--type"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("search-code", parents=[common]); s.add_argument("query")
    s.add_argument("--limit", type=int, default=25); s.add_argument("--all", action="store_true")
    s.add_argument("--actionable-only", action="store_true",
                   help="only encodings used by an extractable showcase field")
    s.set_defaults(fn=cmd_search_code)
    s = sub.add_parser("fields-using", parents=[common]); s.add_argument("encoding_id", type=int)
    s.set_defaults(fn=cmd_fields_using)
    s = sub.add_parser("neighbors", parents=[common])
    s.add_argument("node_type", choices=["category", "field", "encoding", "code"])
    s.add_argument("id"); s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_neighbors)
    s = sub.add_parser("field", parents=[common]); s.add_argument("field_id", type=int)
    s.set_defaults(fn=cmd_field)
    s = sub.add_parser("coding", parents=[common]); s.add_argument("encoding_id", type=int)
    s.add_argument("--limit", type=int, default=300); s.add_argument("--specials-only", action="store_true")
    s.set_defaults(fn=cmd_coding)
    s = sub.add_parser("category", parents=[common]); s.add_argument("category_id", type=int)
    s.set_defaults(fn=cmd_category)
    s = sub.add_parser("tree", parents=[common]); s.add_argument("category_id", type=int)
    g = s.add_mutually_exclusive_group(); g.add_argument("--up", action="store_true")
    g.add_argument("--down", action="store_true"); s.set_defaults(fn=cmd_tree)
    s = sub.add_parser("stats", parents=[common]); s.set_defaults(fn=cmd_stats)
    a = ap.parse_args()
    a.fn(a)
    if getattr(a, "log", None):
        _audit(a)


if __name__ == "__main__":
    main()
