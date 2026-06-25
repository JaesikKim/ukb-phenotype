#!/usr/bin/env python3
"""
gen_encoder.py — Compile a structured rules.json into a standalone, deterministic encode.py.

The rules.json is the encoding PLAN (one entry per output variable). Each variable lists its
source fields plus, per field: missing_codes (-> NaN), recode (raw value -> value, for bounded
sentinels / categorical levels), and instance_agg (how to reduce its instance/array columns).
Per variable: combine (how to merge the fields) and an optional score (threshold/membership ->
label). gen_encoder validates the plan and emits an encode.py that applies it to a UKB extract.

Fail-loud: an invalid plan aborts here; a referenced field absent from the extract aborts in
encode.py (strict by default). No silent fallbacks.

Usage:  python3 scripts/gen_encoder.py <rules.json> [-o encode.py]
"""
import argparse
import ast
import json
import os
import sys

VALID_AGG = {"mean", "max", "min", "sum", "first_non_null"}
VALID_COMBINE = {"sum", "max", "min", "mean", "priority", "first_non_null"}
VALID_OPS = {">=", "<=", ">", "<", "==", "in"}

HELPERS = r'''
import argparse
import json
import re
import sys

import numpy as np
import pandas as pd


def _cols(df, fid):
    """All extract columns for a field: classic {fid}-{i}.{a}, RAP p{fid}_i{i}_a{a}, or bare {fid}."""
    pats = (re.compile(rf"^{fid}-\d+\.\d+$"), re.compile(rf"^p{fid}_i\d+(_a\d+)?$"))
    return [c for c in df.columns if str(c) == str(fid) or any(p.match(str(c)) for p in pats)]


def _agg(sub, how):
    if how == "mean":
        return sub.mean(axis=1)
    if how == "max":
        return sub.max(axis=1)
    if how == "min":
        return sub.min(axis=1)
    if how == "sum":
        return sub.sum(axis=1, min_count=1)
    if how == "first_non_null":
        return sub.bfill(axis=1).iloc[:, 0]
    raise ValueError(f"unknown instance_agg {how!r}")


def _combine(parts, how):
    m = pd.concat(parts, axis=1)
    if how == "sum":
        return m.sum(axis=1, min_count=1)
    if how == "max":
        return m.max(axis=1)
    if how == "min":
        return m.min(axis=1)
    if how == "mean":
        return m.mean(axis=1)
    if how in ("priority", "first_non_null"):
        out = m.iloc[:, 0].copy()
        for i in range(1, m.shape[1]):
            out = out.fillna(m.iloc[:, i])
        return out
    raise ValueError(f"unknown combine {how!r}")


def _score(v, sc):
    op = sc["op"]
    if op == "in":
        m = v.isin(sc["values"])
    else:
        t = sc["threshold"]
        m = {">=": v >= t, "<=": v <= t, ">": v > t, "<": v < t, "==": v == t}[op]
    out = pd.Series(np.where(m, sc.get("true", 1), sc.get("false", 0)), index=v.index, dtype="float64")
    out[v.isna()] = np.nan  # never invent a label for a missing input
    return out


def encode_variable(df, spec, strict, computed):
    parts = []
    for f in spec["fields"]:
        if "variable" in f:  # reference an already-computed output variable (e.g. a sum of item scores)
            ref = f["variable"]
            if ref not in computed:
                sys.exit(f"FAIL: {spec['name']} references variable {ref!r} before it is computed "
                         f"(define it earlier in 'variables')")
            parts.append(computed[ref])
            continue
        fid = f["field_id"]
        cols = _cols(df, fid)
        if not cols:
            msg = f"field {fid} ({spec['name']}) not in extract — no columns match {fid}-<instance>.<array>"
            if strict:
                sys.exit(f"FAIL: {msg}")
            sys.stderr.write(f"WARN: {msg}; emitting NaN\n")
            parts.append(pd.Series(np.nan, index=df.index))
            continue
        sub = df[cols].apply(pd.to_numeric, errors="coerce")
        mc = f.get("missing_codes") or []
        if mc:
            sub = sub.mask(sub.isin(mc))
        rec = f.get("recode") or {}
        if rec:
            sub = sub.replace({float(k): v for k, v in rec.items()})
        parts.append(_agg(sub, f.get("instance_agg", "mean")))
    v = _combine(parts, spec.get("combine", "first_non_null"))
    if spec.get("score"):
        v = _score(v, spec["score"])
    return v


def main():
    ap = argparse.ArgumentParser(description=f"Encode phenotype: {RULES.get('phenotype','')}")
    ap.add_argument("extract", help="UKB extract CSV (wide: eid + {field}-{instance}.{array} columns)")
    ap.add_argument("-o", "--out", default="phenotype.csv")
    ap.add_argument("--id-col", default="eid")
    ap.add_argument("--lenient", action="store_true",
                    help="emit NaN + warn for absent fields instead of failing")
    a = ap.parse_args()
    df = pd.read_csv(a.extract)
    if a.id_col not in df.columns:
        sys.exit(f"FAIL: id column {a.id_col!r} not in extract; columns start with {list(df.columns)[:5]}")
    out = pd.DataFrame({a.id_col: df[a.id_col]})
    computed = {}
    for spec in RULES["variables"]:
        s = encode_variable(df, spec, strict=not a.lenient, computed=computed)
        out[spec["name"]] = s.values
        computed[spec["name"]] = s
    out.to_csv(a.out, index=False)
    sys.stderr.write(f"OK  wrote {a.out}  rows={len(out)}  "
                     f"variables={[s['name'] for s in RULES['variables']]}\n")
'''


def validate(rules):
    if not isinstance(rules.get("variables"), list) or not rules["variables"]:
        sys.exit("FAIL: rules.json has no 'variables'")
    for v in rules["variables"]:
        name = v.get("name")
        if not name:
            sys.exit("FAIL: a variable is missing 'name'")
        if not v.get("fields"):
            sys.exit(f"FAIL: variable {name!r} has no 'fields'")
        for f in v["fields"]:
            if "variable" in f:  # reference to another output variable; checked at runtime by ordering
                continue
            if "field_id" not in f:
                sys.exit(f"FAIL: a field in {name!r} is missing 'field_id' (or 'variable')")
            ia = f.get("instance_agg", "mean")
            if ia not in VALID_AGG:
                sys.exit(f"FAIL: {name}.{f['field_id']} bad instance_agg {ia!r} (use {sorted(VALID_AGG)})")
        cb = v.get("combine", "first_non_null")
        if cb not in VALID_COMBINE:
            sys.exit(f"FAIL: {name} bad combine {cb!r} (use {sorted(VALID_COMBINE)})")
        sc = v.get("score")
        if sc:
            if sc.get("op") not in VALID_OPS:
                sys.exit(f"FAIL: {name} bad score op {sc.get('op')!r} (use {sorted(VALID_OPS)})")
            if sc["op"] == "in" and "values" not in sc:
                sys.exit(f"FAIL: {name} score op 'in' needs 'values'")
            if sc["op"] != "in" and "threshold" not in sc:
                sys.exit(f"FAIL: {name} score op {sc['op']!r} needs 'threshold'")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rules", help="path to rules.json")
    ap.add_argument("-o", "--out", help="output encode.py (default: alongside rules.json)")
    a = ap.parse_args()
    with open(a.rules, encoding="utf-8") as f:
        rules = json.load(f)
    validate(rules)

    header = (f"Generated by gen_encoder.py from {os.path.basename(a.rules)} — "
              f"phenotype: {rules.get('phenotype','?')} (kg_version {rules.get('kg_version','?')}).\n"
              f"Applies the encoding rules to a UK Biobank extract. Fail-loud on absent fields "
              f"(--lenient to emit NaN). Usage: python3 encode.py <extract.csv> [-o out.csv] "
              f"[--id-col eid] [--lenient]")
    code = (f'#!/usr/bin/env python3\n"""{header}"""\n' + HELPERS
            + "\nRULES = json.loads(r'''\n" + json.dumps(rules, ensure_ascii=False, indent=2)
            + "\n''')\n\nif __name__ == \"__main__\":\n    main()\n")
    try:
        ast.parse(code)
    except SyntaxError as e:
        sys.exit(f"FAIL: generated code does not parse: {e}")

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.rules)), "encode.py")
    with open(out, "w", encoding="utf-8") as f:
        f.write(code)
    sys.stderr.write(f"OK  wrote {out}  ({len(rules['variables'])} variables)\n")


if __name__ == "__main__":
    main()
