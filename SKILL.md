---
name: ukb-phenotype
description: >
  Translate a natural-language phenotype/trait into UK Biobank Showcase fields and an executable
  encoding script, using a local SQLite knowledge graph (no web scraping). Use when the user wants
  to find UKB category/fields for a trait, build a computable phenotype, decide how to encode it,
  or generate an encoding script. Handles exposures, lifestyle screeners (MED, GAD-7, PHQ-9…), and
  ICD/medication/operation-coded conditions.
---

# ukb-phenotype — NL trait → UKB fields → encode.py

You turn a high-level phenotype into (a) the right UK Biobank **fields**, (b) a reviewable,
**structured** `rules.json` encoding plan agreed with the user, and (c) an executable `encode.py`.
A local KG does the lookup; **you do the reasoning and selection**. The KG is a stateless query
tool — the search/observe/refine loop is yours.

## 0. Setup
- Project dir: the directory containing this file. Run all tools from there.
- **Read `references/ukb_showcase_structure.md` first** — the entity model, the address model
  (`{field}-{instance}.{array}`), `special_kind`, the two search axes, and the pitfalls. Source of truth.
- Ensure the KG exists: `python3 scripts/kg_query.py stats`. If it errors, build once:
  `python3 scripts/build_kg.py` (downloads UKB schemas; ~3s).
- **Audit everything**: pass `--log discovery.jsonl` to *every* `kg_query.py` call so the exact
  query + candidate pool + kg_version are recorded (see "Auditability").

## 1. Triage & conceptualize (decide BEFORE querying)
Classify the request first — this decides whether your own knowledge is enough or you must consult
the literature. Write a `concept.json` spec and **query the KG from that spec's items, not the raw
user phrase**. `concept.json` is the head of the audit chain.

**Tiers — pick one:**
- **A · direct measure** (age, sex, BMI, a single named variable): no decomposition, no search.
- **B · known composite** you can decompose reliably (fruit intake, alcohol, sedentary behaviour):
  decompose into sub-terms from your own knowledge. Web-search only to resolve a specific doubt.
- **C · validated instrument / clinical score** (PHQ-9, GAD-7, MEDAS-14 / Mediterranean diet, AUDIT,
  EPDS, MMSE, Framingham…): **web search is MANDATORY** — get the exact item list, response options,
  and scoring/cutoffs from the authoritative source. Never rebuild a scored instrument from memory.
- **D · contested / heterogeneous definition** (CKD, frailty, metabolic syndrome, "diabetes"):
  definitions vary across studies. **Web search** how prior UK Biobank / peer-reviewed work defined
  it (fields / ICD codes / thresholds), pick one, and **cite it**; surface the choice at step 5.

**When in doubt, search.** Never fabricate a citation — cite a real source or mark `[VERIFY]`. If web
search is unavailable, proceed on knowledge but set `confidence: low`, mark every instrument item
`[VERIFY]`, and tell the user you could not verify the definition.

**Instrument cache** (reproducibility): for Tier C/D, first check `data/instruments/<slug>.json`; if
present, reuse it. If you searched, save the biobank-agnostic definition there. Template:
`data/instruments/gad-7.json`.

## 2. Search — both axes (see references §6)
For each item in `concept.json` (use its `search_terms`), decide which axis the info lives in:
- **Horizontal** (name is the variable): `python3 scripts/kg_query.py search "<text>" [--category N] [--all]`
- **Vertical** (lives only as a code value — a specific disease/drug/operation/job/food):
  `python3 scripts/kg_query.py search-code "<text>" --actionable-only`, then `fields-using <encoding_id>`.
- Coding systems use their own vocabulary (ICD10 = "Non-insulin-dependent…" for T2D). Expand synonyms
  yourself; one query rarely suffices.

## 3. Expand for completeness — walk the graph, don't re-search
Single-keyword recall is ~65% — siblings without the keyword sink (Berries/Citrus for "fruit"). Treat
each hit as a **seed** and navigate deterministically with `neighbors` (uniform up/down/siblings for
any node; fixed per `kg_version`, so reproducible):
- `neighbors field <id>` → its category (up), encoding (down), and **all same-category siblings** = your
  completeness pool. Read them all, keep the matches.
- `neighbors category <id>` → parent/child categories + fields.
- `neighbors encoding <id>` → every field that uses a code dictionary (up) + its codes (down).
- `neighbors code <encoding_id> <value>` → parent/child codes; expand an ICD/OPCS subtree, e.g.
  `neighbors code 19 E11` → E11.0…E11.9. Use for outcome (ICD) definitions.

## 4. Select + inspect
- From the expanded set, **you select** the matching fields (drop unrelated siblings). Prefer higher
  `num_participants` coverage; keep alternates as fallbacks.
- For each kept field: `python3 scripts/kg_query.py field <id>` to read `type`, `units`, `columns`,
  and the `coding` table with `special_kind` (`missing`→NaN, `bounded`→domain value, `normal`→as-is).

## 5. Draft the rules — INTERACTIVE (the human-in-the-loop checkpoint)
**Do not write `rules.json` silently.** The encoding choices are consequential and often the
researcher's call. Work through the decisions below **one round of questions**, proposing your
recommended default + a one-line rationale for each, and ask the user to confirm or change. Use a
question UI if available. Only finalize `rules.json` after they sign off.

Decisions to put to the user (skip any that are unambiguous, but state your assumption):
1. **Definition / source** (Tier C/D): which published definition or instrument version.
2. **Variable split**: one variable or several? (e.g. unit-incompatible touchscreen pieces vs 24h
   grams → two variables; juice as a separate variable from whole fruit).
3. **Field selection**: primary vs alternate/fallback fields; include or drop borderline candidates.
4. **Missing-data handling**: which codes → NaN (from `special_kind: missing`); how `bounded`
   sentinels map (e.g. −10 "Less than one" → 0.5).
5. **Unit / frequency harmonization**: assumptions (serving size, weekly→daily) — record them.
6. **Aggregation**: `instance_agg` per field (across instances/arrays) and `combine` across fields
   (sum / max / mean / priority-fallback).
7. **Scoring**: any final threshold/cutoff (e.g. ≥4 tbsp → 1; GAD-7 sum cutoffs).

Then write the **structured** `rules.json` (this is what `gen_encoder.py` compiles — keep it structured,
not free text):
```json
{
  "phenotype": "Fruit intake",
  "kg_version": "<from stats>",
  "source_concept": "<instrument/definition ref, or [VERIFY]>",
  "variables": [
    {
      "name": "fresh_fruit_pieces_per_day",
      "description": "Habitual whole fruit (touchscreen), pieces/day",
      "fields": [
        {"field_id": 1309, "title": "Fresh fruit intake",
         "missing_codes": [-1, -3], "recode": {"-10": 0.5},
         "instance_agg": "mean", "role": "primary"}
      ],
      "combine": "sum",
      "score": null,
      "rationale": "<why these fields; conversions and assumptions agreed with the user>"
    }
  ],
  "dropped": [{"field_id": 104340, "title": "Fresh tomato intake", "reason": "vegetable, not fruit"}]
}
```
Field knobs: `missing_codes` (→ NaN), `recode` (raw value → value, for bounded sentinels or categorical
levels), `instance_agg` ∈ `mean|max|min|sum|first_non_null` (reduce a field's instance/array columns).
Variable knobs: `combine` ∈ `sum|max|min|mean|priority|first_non_null` (merge the fields), and `score`
= `null` or `{"op": ">=|<=|>|<|==|in", "threshold": <n> | "values": [...], "true": 1, "false": 0}`.
For composite scores (e.g. a GAD-7 total = sum of item scores), a field entry may be
`{"variable": "<earlier-variable-name>"}` instead of a `field_id` — it reuses an already-computed
output variable. See `examples/gad-7/` (7 items → `gad7_total` → `gad7_moderate_or_worse`).
`dropped` is optional — only judgment-call rejections need a reason; the render derives the rest.

## 6. Compile + run
Only after the user confirms `rules.json`:
```bash
python3 scripts/gen_encoder.py rules.json -o encode.py     # validates the plan + emits encode.py
python3 encode.py <extract.csv> -o phenotype.csv           # applies it (fail-loud)
```
`gen_encoder.py` bakes in the column naming (`{field}-{instance}.{array}` / RAP `p{field}_i_a`),
`missing_codes`→NaN, `recode`, `instance_agg`, `combine`, and `score`. `encode.py` **fails loud** if a
referenced field is absent from the extract (use `--lenient` only on request — no silent NaN columns).

## Auditability (no separate ledger file)
Reproducible and reviewable **without hand-writing a ledger**:
1. **`discovery.jsonl`** — auto-written by `--log` on every query: each query + full candidate pool +
   `kg_version`. Deterministic record of what the KG surfaced (replay → identical pools).
2. **Reasons in `rules.json`** — keeps are your `variables[].fields[]`; for notable *drops* (judgment
   calls) add a `dropped` array. Obvious drops (a vegetable in a fruit query) need no entry.

`render_audit.py` then **derives** the keep/drop ledger from `discovery.jsonl ∩ rules.json` — always
consistent with the actual queries (you can't omit a candidate to hide a bad drop), and every
surfaced-but-not-selected field shows up for recall review.

Provenance chain (tied by `kg_version`): **`concept.json`** → **`discovery.jsonl`** → **`rules.json`**.

**Render for review:** `python3 scripts/render_audit.py <dir>` → a self-contained `audit_report.html`
(offline, double-click): concept, derived keep/drop ledger (filterable, with reasons), final rules,
query log. Hand this to the user instead of raw JSON. Example: `examples/fruit_intake/`.

## Stop conditions
- Do the interactive decisions in step 5 before writing `rules.json`; do not compile to code (step 6)
  until the user confirms the plan.
- If a concept resolves only to `actionable=false` codes (record-level GP/HES, not a field), say so and
  explain the linked-data path instead of fabricating a field.

## Tools (`python3 scripts/<script>`)
- `kg_query.py <cmd>` — `search` · `search-code` · `fields-using` · `neighbors <type> <id> [value]` ·
  `category` · `tree` · `field` · `coding` · `stats` (JSON out; add `--log discovery.jsonl` to every call)
- `build_kg.py` — (re)build the SQLite KG from UKB schemas
- `gen_encoder.py <rules.json>` — compile the plan into `encode.py`
- `render_audit.py <dir>` — render the audit trail to `audit_report.html`

Full KG reference: `references/ukb_showcase_structure.md`.
