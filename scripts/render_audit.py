#!/usr/bin/env python3
"""
render_audit.py — Render the phenotyping audit trail as one self-contained HTML page.

Reads whatever artifacts exist in a directory:
  concept.json          (step 1 — what to look for + why, with sources)
  discovery.jsonl       (--log output — what the KG returned per query)
  rules.json            (final fields + encoding plan, incl. optional `dropped[]` reasons)
The keep/drop ledger is DERIVED from discovery.jsonl ∩ rules.json (no hand-written file); a
legacy discovery_audit.json, if present, overrides the derivation. Writes one offline,
double-click HTML report tying everything together by kg_version.

Usage:  python3 scripts/render_audit.py <artifact_dir> [-o report.html]
No third-party deps. Fail-loud: errors if the directory has none of the artifacts.
"""
import argparse
import html
import json
import os
import sys

CSS = """
:root{--bg:#fafafa;--card:#fff;--bd:#e5e7eb;--ink:#111827;--mut:#6b7280;--ac:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:24px;font-weight:600;margin:0 0 4px}
h2{font-size:17px;font-weight:600;margin:0 0 14px;display:flex;align-items:center;gap:8px}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:20px 22px;margin:0 0 20px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;line-height:1.6}
.keep{background:#dcfce7;color:#166534}.drop{background:#f3f4f6;color:#6b7280}
.tier{background:#e0e7ff;color:#3730a3}.warn{background:#fef3c7;color:#92400e}
.ok{background:#dcfce7;color:#166534}.no{background:#fee2e2;color:#991b1b}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--mut);font-weight:600;border-bottom:2px solid var(--bd);padding:7px 10px;position:sticky;top:0;background:var(--card)}
td{border-bottom:1px solid var(--bd);padding:7px 10px;vertical-align:top}
tr:hover td{background:#f9fafb}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 16px;font-size:14px}
.kv dt{color:var(--mut)}.kv dd{margin:0}
.src{font-size:13.5px;margin:4px 0}.src a{color:var(--ac);text-decoration:none}
.callout{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:10px 14px;margin:12px 0;font-size:13.5px;color:#92400e}
.controls{display:flex;gap:8px;align-items:center;margin:0 0 12px;flex-wrap:wrap}
.fbtn{border:1px solid var(--bd);background:#fff;border-radius:8px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
.fbtn.on{background:var(--ac);color:#fff;border-color:var(--ac)}
input[type=search]{border:1px solid var(--bd);border-radius:8px;padding:6px 11px;font:inherit;font-size:13px;min-width:200px}
.count{color:var(--mut);font-size:13px}
details{margin-top:6px}summary{cursor:pointer;color:var(--ac);font-size:13.5px}
.chain{display:flex;gap:6px;align-items:center;flex-wrap:wrap;color:var(--mut);font-size:13px;margin-top:6px}
.chain code{background:#f3f4f6;border-radius:5px;padding:2px 7px;font-size:12.5px}
.muted{color:var(--mut)}
.formula{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;background:#eef2ff;border:1px solid #c7d2fe;border-radius:8px;padding:8px 12px;margin:6px 0 12px;color:#3730a3;overflow-x:auto;white-space:nowrap}
.vname{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;color:#111827}
"""

JS = """
function flt(d,el){document.querySelectorAll('#ledger tbody tr').forEach(function(r){r.dataset.f=(d==='all'||r.dataset.decision===d)?'1':'0';r.style.display=r.dataset.f==='1'&&r.dataset.t!=='0'?'':'none';});document.querySelectorAll('.fbtn').forEach(function(b){b.classList.remove('on')});el.classList.add('on');}
function ftext(q){q=q.toLowerCase();document.querySelectorAll('#ledger tbody tr').forEach(function(r){r.dataset.t=r.innerText.toLowerCase().indexOf(q)>-1?'1':'0';r.style.display=(r.dataset.f!=='0'&&r.dataset.t==='1')?'':'none';});}
"""


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path):
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                rows.append(json.loads(ln))
    return rows


def _candidates_from_log(qlog):
    """Every field-type node any query surfaced -> {field_id: {title, by:set}}."""
    cand = {}

    def add(fid, title, by):
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            return
        e = cand.setdefault(fid, {"title": "", "by": set()})
        if title and not e["title"]:
            e["title"] = title
        e["by"].add(by)

    for r in qlog or []:
        cmd = r.get("cmd"); a = r.get("args") or {}; res = r.get("result") or {}
        if cmd == "search":
            by = f'search "{a.get("query","")}"'
            for x in res.get("results", []):
                add(x.get("field_id"), x.get("title"), by)
        elif cmd == "category":
            by = f'category {a.get("category_id","")}'
            for x in res.get("fields", []):
                add(x.get("field_id"), x.get("title"), by)
        elif cmd == "fields-using":
            by = f'fields-using {a.get("encoding_id","")}'
            for x in res.get("fields", []):
                add(x.get("field_id"), x.get("title"), by)
        elif cmd == "neighbors":
            by = f'neighbors {a.get("node_type","")} {a.get("id","")}'
            node = res.get("node") or {}
            if node.get("type") == "field":
                add(node.get("id"), node.get("label"), by)
            for grp in ("siblings", "children"):
                for x in (res.get(grp) or []):
                    if isinstance(x, dict) and x.get("type") == "field":
                        add(x.get("id"), x.get("label"), f"{by} {grp}")
        elif cmd == "field":
            add(res.get("field_id"), res.get("title"), f'field {res.get("field_id")}')
    return cand


def _derive_ledger(qlog, rules):
    """Build the keep/drop ledger from discovery.jsonl ∩ rules.json — no hand-written file.
    keep reason = which variable it feeds; drop reason = rules.json `dropped[]` or 'not selected'."""
    cand = _candidates_from_log(qlog)
    keep = {}
    for v in (rules or {}).get("variables", []):
        for f in v.get("fields", []):
            try:
                keep[int(f["field_id"])] = (v.get("name", ""), f.get("role", ""))
            except (KeyError, ValueError, TypeError):
                pass
    drop_reasons = {}
    for d in (rules or {}).get("dropped", []):
        try:
            fid = int(d["field_id"])
        except (KeyError, ValueError, TypeError):
            continue
        drop_reasons[fid] = d.get("reason", "")
        cand.setdefault(fid, {"title": d.get("title", ""), "by": set()})
        if d.get("title") and not cand[fid]["title"]:
            cand[fid]["title"] = d["title"]
    rows = []
    for fid in sorted(cand):
        e = cand[fid]
        by = " · ".join(sorted(e["by"])) if e["by"] else "(noted)"
        if fid in keep:
            var, role = keep[fid]
            rows.append({"field_id": fid, "title": e["title"], "surfaced_by": by,
                         "decision": "keep", "reason": f"→ {var}" + (f" ({role})" if role else "")})
        else:
            rows.append({"field_id": fid, "title": e["title"], "surfaced_by": by,
                         "decision": "drop", "reason": drop_reasons.get(fid, "surfaced, not selected")})
    return {"candidates": rows, "derived": True}


def concept_html(c):
    if not c:
        return ""
    rows = "".join(
        f"<tr><td class=mono>{esc(it.get('item'))}</td>"
        f"<td>{esc(it.get('encoding_logic',''))}</td>"
        f"<td class=mono>{esc(', '.join(it.get('search_terms',[])))}</td></tr>"
        for it in c.get("items", []))
    srcs = "".join(
        f"<div class=src>• {esc(s.get('title',''))}"
        + (f' <a href=\"{esc(s.get("url"))}\" target=_blank>link</a>' if s.get("url") else "")
        + (f' <span class=muted>— {esc(s.get("note"))}</span>' if s.get("note") else "")
        + "</div>"
        for s in c.get("sources", []))
    ws = c.get("web_searched")
    ws_badge = '<span class="badge ok">web-searched</span>' if ws else '<span class="badge drop">knowledge only</span>'
    conf = c.get("confidence", "")
    conf_cls = {"high": "ok", "medium": "warn", "low": "no"}.get(conf, "drop")
    vf = c.get("verify_flags") or []
    vf_html = f'<div class=callout><b>[VERIFY]</b> {esc(" · ".join(vf))}</div>' if vf else ""
    return f"""<div class=card>
<h2><i></i>1 · Concept <span class="badge tier">{esc(c.get('tier',''))}</span></h2>
<dl class=kv>
<dt>phenotype</dt><dd><b>{esc(c.get('phenotype',''))}</b></dd>
<dt>query</dt><dd class=mono>{esc(c.get('query',''))}</dd>
<dt>sourcing</dt><dd>{ws_badge} &nbsp; confidence <span class="badge {conf_cls}">{esc(conf or '?')}</span></dd>
<dt>scoring</dt><dd>{esc(c.get('scoring',''))}</dd>
</dl>
{('<div style=margin-top:12px><b>sources</b>'+srcs+'</div>') if srcs else ''}
{vf_html}
<div style=margin-top:14px><b>items</b><table><thead><tr><th>item</th><th>encoding logic</th><th>search terms</th></tr></thead><tbody>{rows}</tbody></table></div>
</div>"""


def ledger_html(a):
    if not a:
        return ""
    cands = a.get("candidates", [])
    nkeep = sum(1 for c in cands if c.get("decision") == "keep")
    ndrop = len(cands) - nkeep
    rows = ""
    for c in cands:
        d = c.get("decision", "")
        rows += (f'<tr data-decision="{esc(d)}" data-f=1 data-t=1>'
                 f'<td class=mono>{esc(c.get("field_id",""))}</td>'
                 f'<td>{esc(c.get("title",""))}</td>'
                 f'<td class=mono>{esc(c.get("surfaced_by",""))}</td>'
                 f'<td><span class="badge {esc(d)}">{esc(d)}</span></td>'
                 f'<td>{esc(c.get("reason",""))}</td></tr>')
    note = ('<div class=muted style="font-size:13px;margin:-6px 0 10px">derived from '
            '<code style="background:#f3f4f6;border-radius:5px;padding:1px 6px">discovery.jsonl</code> ∩ '
            '<code style="background:#f3f4f6;border-radius:5px;padding:1px 6px">rules.json</code> — '
            'keep reason = variable it feeds; drop reason = rules.json <code style="background:#f3f4f6;'
            'border-radius:5px;padding:1px 6px">dropped[]</code></div>') if a.get("derived") else ""
    return f"""<div class=card>
<h2>2 · Decision ledger <span class=count>({nkeep} keep · {ndrop} drop · {len(cands)} candidates)</span></h2>
{note}
<div class=controls>
<button class="fbtn on" onclick="flt('all',this)">all</button>
<button class=fbtn onclick="flt('keep',this)">keep</button>
<button class=fbtn onclick="flt('drop',this)">drop</button>
<input type=search placeholder="filter…" oninput="ftext(this.value)">
</div>
<table id=ledger><thead><tr><th>field</th><th>title</th><th>surfaced by</th><th>decision</th><th>reason</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def _score_str(sc):
    if not sc:
        return "continuous (no threshold)"
    if sc.get("op") == "in":
        return f"value in {sc.get('values')} → {sc.get('true', 1)} else {sc.get('false', 0)}"
    return f"value {sc.get('op')} {sc.get('threshold')} → {sc.get('true', 1)} else {sc.get('false', 0)}"


def rules_html(r):
    if not r:
        return ""
    blocks = ""
    for v in r.get("variables", []):
        flds = v.get("fields", [])
        combine = v.get("combine", "first_non_null")
        sc = v.get("score")
        terms = ", ".join(f["variable"] if "variable" in f
                          else f'{f.get("field_id")}[{f.get("instance_agg", "mean")}]' for f in flds)
        formula = f"{combine}( {terms} )"
        if sc:
            formula += f"  →  {_score_str(sc)}"
        frows = ""
        for f in flds:
            if "variable" in f:
                frows += (f'<tr><td class=mono>{esc(f["variable"])}</td>'
                          f'<td class=muted>(computed variable)</td><td>reference</td>'
                          f'<td class=mono>—</td><td class=mono>—</td><td class=mono>—</td></tr>')
                continue
            mc = f.get("missing_codes") or []
            rec = f.get("recode") or {}
            frows += (f'<tr><td class=mono>{esc(f.get("field_id"))}</td>'
                      f'<td>{esc(f.get("title", ""))}</td>'
                      f'<td>{esc(f.get("role", ""))}</td>'
                      f'<td class=mono>{esc(", ".join(map(str, mc)) if mc else "—")}</td>'
                      f'<td class=mono>{esc(", ".join(f"{k}→{val}" for k, val in rec.items()) if rec else "—")}</td>'
                      f'<td class=mono>{esc(f.get("instance_agg", "mean"))}</td></tr>')
        blocks += f"""<div style="border-top:1px solid var(--bd);padding-top:16px;margin-top:16px">
<div style=margin-bottom:2px><span class=vname>{esc(v.get('name',''))}</span> <span class=muted>{esc(v.get('description',''))}</span></div>
<div class=formula>{esc(formula)}</div>
<table><thead><tr><th>field</th><th>title</th><th>role</th><th>missing → NaN</th><th>recode</th><th>per-instance</th></tr></thead><tbody>{frows}</tbody></table>
<dl class=kv style=margin-top:12px>
<dt>combine fields</dt><dd class=mono>{esc(combine)}</dd>
<dt>score</dt><dd class=mono>{esc(_score_str(sc))}</dd>
<dt>rationale</dt><dd>{esc(v.get('rationale',''))}</dd>
</dl></div>"""
    return f"""<div class=card><h2>3 · Encoding rules <span class=count>({len(r.get('variables',[]))} variable(s))</span></h2>
<div class=muted style=font-size:13.5px>source concept: {esc(r.get('source_concept',''))} &nbsp;·&nbsp; read a formula as <span class=mono>combine( field[per-instance agg], … ) → score</span></div>{blocks}</div>"""


def querylog_html(rows):
    if not rows:
        return ""
    items = ""
    for r in rows:
        res = r.get("result") or {}
        n = (res.get("count") or res.get("n_fields") or res.get("n_matched_codes")
             or (len(res.get("results", [])) if isinstance(res.get("results"), list) else None)
             or (len(res.get("fields", [])) if isinstance(res.get("fields"), list) else None))
        args = " ".join(f"{k}={v}" for k, v in (r.get("args") or {}).items() if v not in (None, False))
        items += (f'<tr><td class=mono>{esc(r.get("cmd",""))}</td><td class=mono>{esc(args)}</td>'
                  f'<td class=mono>{esc(n if n is not None else "")}</td>'
                  f'<td class=mono muted>{esc(r.get("kg_version",""))}</td>'
                  f'<td class="mono muted">{esc(r.get("ts",""))}</td></tr>')
    return f"""<div class=card><h2>4 · Query log <span class=count>({len(rows)} queries — replayable)</span></h2>
<details open><summary>show {len(rows)} kg_query calls</summary>
<table style=margin-top:10px><thead><tr><th>cmd</th><th>args</th><th>results</th><th>kg_version</th><th>ts</th></tr></thead>
<tbody>{items}</tbody></table></details></div>"""


def build(art_dir, out_path):
    concept = load_json(os.path.join(art_dir, "concept.json"))
    rules = load_json(os.path.join(art_dir, "rules.json"))
    qlog = load_jsonl(os.path.join(art_dir, "discovery.jsonl"))
    audit = load_json(os.path.join(art_dir, "discovery_audit.json"))  # optional legacy override
    if not audit and (qlog or rules):
        audit = _derive_ledger(qlog, rules)
    if not any([concept, audit, rules, qlog]):
        sys.exit(f"FAIL: no audit artifacts in {art_dir} "
                 f"(need concept.json / discovery_audit.json / rules.json / discovery.jsonl)")

    title = (concept or rules or {}).get("phenotype") or "UKB phenotype audit"
    kgv = ((concept or {}).get("kg_version") or (rules or {}).get("kg_version")
           or (audit or {}).get("kg_version") or (qlog[0].get("kg_version") if qlog else "") or "?")
    body = (concept_html(concept) + ledger_html(audit) + rules_html(rules) + querylog_html(qlog))

    doc = f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(title)} — audit</title><style>{CSS}</style></head><body><div class=wrap>
<h1>{esc(title)}</h1>
<div class=sub>UKB phenotyping audit · kg_version <span class=mono>{esc(kgv)}</span></div>
<div class=card style="padding:14px 22px">
<div class=chain>provenance:
<code>concept.json</code>→<code>discovery.jsonl</code>→<code>discovery_audit.json</code>→<code>rules.json</code>
<span class=muted>(tied by kg_version; replay the query log to reproduce)</span></div></div>
{body}
</div><script>{JS}</script></body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    sys.stderr.write(f"OK  wrote {out_path}  ({os.path.getsize(out_path)/1024:.0f} KB)\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="directory containing the audit artifacts")
    ap.add_argument("-o", "--out", help="output HTML path (default: <dir>/audit_report.html)")
    a = ap.parse_args()
    build(a.dir, a.out or os.path.join(a.dir, "audit_report.html"))
