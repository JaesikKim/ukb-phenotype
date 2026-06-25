# ukb-phenotype — UK Biobank Showcase KG + phenotyping toolkit

A toolkit that lets a coding agent (Claude Code / Codex) take a natural-language phenotype/trait,
find the relevant UK Biobank Showcase fields, agree an encoding plan with the user, and emit an
executable `encode.py`. It is an offline, agent-native distillation of Phase 1–3 of the paper
*"An Agentic System for Automated Data Curation and Analysis in Large-Scale Biobanks"* (UKB Agent,
ML4H 2025).

**Core design:** instead of visiting the Showcase website on every query (which can get blocked), we
download UK Biobank's official **machine-readable schema once** and materialize it as a **local SQLite
knowledge graph**. Every lookup is then network-free and sub-millisecond. The agent shells out to a CLI,
gets JSON back, and reasons over it.

> **A coding agent should read [`references/ukb_showcase_structure.md`](references/ukb_showcase_structure.md)
> before starting.** It documents the Showcase ontology (the 4-level entity model, the address model,
> `special_kind`, the two search axes, the pitfalls) and the tools. The full workflow lives in
> [`SKILL.md`](SKILL.md).

## Install

**Requirements:** Python 3.8+ (stdlib only for the KG/query/audit tools; `pandas` + `numpy` for the
generated `encode.py`) and one-time network access to download the UK Biobank Showcase schema. The KG is
built from **public Showcase metadata only — no participant data** ever leaves your machine; the
generated `encode.py` runs against *your own* UKB extract locally.

### As a Claude Code skill
This repository **is** the skill — a `SKILL.md` plus bundled `scripts/`. Drop it under `.claude/skills/`
and Claude Code auto-discovers it.

```bash
# user-level (available in every project):
git clone https://github.com/<you>/ukb-phenotype ~/.claude/skills/ukb-phenotype
cd ~/.claude/skills/ukb-phenotype
python3 scripts/build_kg.py        # download UKB schema + build the local KG (~3 s, ~54 MB, gitignored)
```
Or per-project: clone into `<your-project>/.claude/skills/ukb-phenotype` instead. Then just ask Claude
something like *"find UK Biobank fields for fruit intake and propose an encoding"* — it invokes the skill.
(The scripts resolve their own paths, so they work from any working directory.)

### With Codex / other agents
The skill is a plain CLI over a local SQLite KG — no server, no daemon. Point any coding agent at
`SKILL.md` + `scripts/` (e.g. reference them from `AGENTS.md` or the agent's system instructions).

### Update the KG
`python3 scripts/build_kg.py --refresh` re-downloads the schema (run when UKB updates the Showcase).

## Build the KG
```bash
python3 scripts/build_kg.py            # uses cached data/raw/*.tsv (offline)
python3 scripts/build_kg.py --refresh  # re-download schemas from the Showcase
```
Output: `data/ukb_kg.sqlite` (~54 MB: 11,821 fields / 533k codings / 410 categories, ~3s). Stdlib only
(`urllib` + `sqlite3`). Source: `scdown.cgi` schema ids 1,2,3,13,5,6,7,8,20,11,12.

## Query the KG (agent interface — all JSON out)
Two search axes: **horizontal** (field metadata) + **vertical** (a code value inside a field → the
encoding → the fields that use it).
```bash
# horizontal: the name is the variable (BMI, fruit intake, a GAD-7 item)
python3 scripts/kg_query.py search "fresh fruit intake" --limit 15
python3 scripts/kg_query.py search "worrying" --category 140
# vertical: lives only as a code value (a disease / drug / operation / job / food)
python3 scripts/kg_query.py search-code "metformin" --actionable-only   # code -> encoding -> field
python3 scripts/kg_query.py fields-using 19                             # every field using ICD10
# deterministic graph walk: parents / children / siblings of any node
python3 scripts/kg_query.py neighbors field 1309                        # up=category, down=encoding, siblings
python3 scripts/kg_query.py neighbors code 19 E11                       # ICD subtree E11.0–E11.9 (outcomes)
# completeness / detail
python3 scripts/kg_query.py category 100052
python3 scripts/kg_query.py field 1309                                  # type, coding, column template
python3 scripts/kg_query.py coding 168                                  # value->meaning (special_kind)
python3 scripts/kg_query.py search "x" --log discovery.jsonl            # add --log to every call (audit trail)
```
`search-code`'s `actionable=true` means an extractable Showcase field uses that encoding; `false` means
the code lives only in record-level data (GP Read/CTV3, HES) and is not a simple field extract.

## Encode (plan → script)
```bash
python3 scripts/gen_encoder.py rules.json -o encode.py    # validate the structured plan, emit encode.py
python3 encode.py <extract.csv> -o phenotype.csv          # apply it (fail-loud on absent fields)
```
`rules.json` is the structured encoding plan (per variable: source fields with `missing_codes`→NaN,
`recode`, `instance_agg`; plus `combine` and optional `score`). `encode.py` handles UKB column naming
(`{field}-{instance}.{array}` / RAP `p{field}_i_a`), special-code handling, instance/array aggregation,
and **fails loud** if a referenced field is missing. See `examples/fruit_intake/` for a full set.

## Audit (HTML report)
```bash
python3 scripts/render_audit.py <dir>   # -> audit_report.html (offline, double-click)
```
Self-contained report tying the run together by `kg_version`: the concept, a filterable keep/drop ledger
(**derived** from `discovery.jsonl ∩ rules.json` — keep reason = the variable it feeds, drop reason =
`rules.json` `dropped[]`), the final rules, and the query log.

## KG schema (SQLite)
`categories`, `catbrowse(parent_id, child_id)` (hierarchy), `fields` (value_type · units · encoding_id ·
instance/array ranges · category_path), `encodings`, `codings` (value · meaning · parent_id ·
`is_special` · `special_kind` · kind), `fields_fts` + `codings_fts` (FTS5), `meta`.

## Validation (against the paper's ground truth)
- GAD-7 item searches return the paper's selected fields (28735/30484/20506/29058 …).
- Fruit: touchscreen 1309/1319 + 24h-recall food groups recovered (26/26 of the paper's set via
  category expansion); `neighbors` recovers keyword-buried siblings (Berries/Citrus).
- `special_kind` semantics correct (−10 bounded vs −1/−3 missing); `encode.py` math verified on
  synthetic data; fail-loud verified.

## Pipeline
```
NL trait
 → 1. triage (tier A–D; validated instruments → web search) → concept.json
 → 2. search (both axes) + 3. neighbors (graph walk) → discovery.jsonl   [--log on every call]
 → 4. select → 5. INTERACTIVE rule decisions with the user → rules.json (+ dropped[])
 → 6. gen_encoder.py → encode.py → phenotype.csv
 → render_audit.py → audit_report.html
```
Provenance chain tied by `kg_version`: `concept.json` → `discovery.jsonl` → `rules.json`.

## License & citation
MIT — see [LICENSE](LICENSE). This toolkit is an agent-native distillation of the UKB Agent framework;
if it helps your work, please cite:

> Jeong C-U, Kim J, Joo J, Lee B, Kim Y-G, Kim D. *An Agentic System for Automated Data Curation and
> Analysis in Large-Scale Biobanks.* Machine Learning for Health (ML4H), 2025.

UK Biobank data is accessed under your own application; this repository ships only public Showcase
metadata tooling — never participant data.
