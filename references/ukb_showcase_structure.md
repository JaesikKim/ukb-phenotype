# UK Biobank Showcase — structure reference (for the coding agent)

Read this **before** mapping a phenotype/trait to UKB fields. Every fact is verified against the local
KG (`data/ukb_kg.sqlite`) and the official UKB schema. Query tools: `scripts/kg_query.py` (JSON out).
Build: `scripts/build_kg.py`.

> One line: **search is the seed, the category/code graph is completeness, you (the agent) do precise
> selection.** Never trust a single search.

---

## 1. What the Showcase is
The UKB Showcase (`biobank.ndph.ox.ac.uk/showcase/`) is the public data dictionary for thousands of
variables. We do not scrape the website — we download UK Biobank's **machine-readable schema**
(`scdown.cgi?fmt=txt&id=N`) once and materialize it as local SQLite, so every lookup is instant and
offline. It is a snapshot (field rows carry a version date); refresh with
`python3 scripts/build_kg.py --refresh`.

Ingested schemas: `1` field, `2` encoding, `3` category, `13` catbrowse (hierarchy), `5/6/7/8/20` simple
encoding values, `11/12` hierarchical encoding values.

---

## 2. The 4-level entity model (this is all of it)
```
Category (410, a tree)  ──contains──▶  Field (11,821)  ──(if categorical)──▶  Encoding (858, shared dict)  ──has──▶  Code values (533k)
```

### Category — topical grouping, tree-structured
- Tables `categories(category_id, title, group_type, descript, notes)` + `catbrowse(parent_id, child_id)`.
- Example path: `Assessment centre > Touchscreen > Lifestyle and environment > Diet`.
- **Each Field belongs to exactly one Category** (`fields.main_category`); the Category tree itself can be
  a DAG. Tools: `category <id>`, `tree <id> --up|--down`, `neighbors category <id>`.

### Field — one measured variable
- Table `fields`. Key columns: `value_type` (§3), `units`, `encoding_id` (0 = no codes = numeric/text),
  `main_category`, `num_participants` (coverage), `notes` (the question text), `instanced`/`arrayed`,
  `instance_min/max`, `array_min/max`, `category_path` (precomputed).
- Tools: `field <id>` (metadata + column template + coding table), `neighbors field <id>`.
- ⚠️ The raw `instance_min/max` / `array_min/max` values can be opaque `[VERIFY]`. If the exact instance
  set matters, confirm on the field's Showcase page; otherwise the convention in §4 is enough.

### Encoding — a code dictionary shared by many fields
- Table `encodings(encoding_id, title, coded_as, structure, num_members, descript)`.
- **One encoding is reused by many fields.** e.g. enc `4` (Treatments) is used by field `20003`; ICD10 enc
  `19` by `41202/41204/40001/40002/40006/41201` etc.
- "Which field carries code X" is solved by reverse lookup: `fields-using <encoding_id>`.

### Code values — value → meaning (disease/drug/job/food names live here)
- Table `codings(encoding_id, value, meaning, parent_id, code_id, selectable, kind, is_special, special_kind)`.
- **simple** (flat) vs **hierarchical** (ICD/OPCS/SOC/food trees; use `parent_id` to walk a subtree).
- Tools: `coding <encoding_id>`, `neighbors code <encoding_id> <value>`.

---

## 3. value_type — the branch point for encoding
| code | type | encoding treatment |
|---|---|---|
| 11 | Integer | numeric: threshold / unit conversion |
| 31 | Continuous | numeric (real): threshold / unit conversion |
| 21 | Categorical (single) | map each code value → score (use the coding table) |
| 22 | Categorical (multiple) | array of codes → membership (any/all) |
| 41 | Text | string; usually not directly scored |
| 51 / 61 | Date / Time | event / duration derivation |
| 101 | Compound | composite; case by case |

`field <id>` already returns the human-readable `type`.

---

## 4. The address of one data point (what encode.py must know)
A value = **`(participant eid, field, instance, array)` → value**, interpreted by the field's encoding.
- **instance** = assessment timepoint: `0` = baseline (2006–10), `1` = repeat (2012–13), `2` = imaging
  (2014+), `3` = repeat imaging.
- **array** = repeated/multiple answers within one visit (e.g. several medications, several diagnoses).
- **Extract column names**:
  - classic ukbconv CSV: **`{field}-{instance}.{array}`** (e.g. `1309-0.0`, `41202-0.5`)
  - RAP / Spark dataset: **`p{field}_i{instance}_a{array}`** (e.g. `p1309_i0`, `p41202_i0_a5`)
- `field <id>` returns both patterns and the instance/array ranges in `columns`.
- → Encoding usually has to **aggregate** across instances/arrays (e.g. max over visits, first non-null,
  priority fallback). `encode.py` exposes this as `instance_agg`.

---

## 5. Special / missing codes — `special_kind` (critical for correctness)
UKB special codes are conventionally **negative**. The KG pre-classifies each code:
| special_kind | example | encoding treatment |
|---|---|---|
| `missing` | −1 "Do not know", −3 "Prefer not to answer" | map to **NaN** (never 0 / a real value) |
| `bounded` | −10 "Less than one" | **not missing** — map to a domain value (e.g. 0.5) |
| `sentinel` | other negatives | review, then decide |
| `normal` | 0/1/2/3, "More than half the days" | ordinary value (positive ordinals stay normal) |

`field` / `coding` output carries `special_kind`. In `rules.json`: list `missing` codes in `missing_codes`
(→ NaN) and `bounded`/categorical levels in `recode` (raw → value). **Do not blanket-null every negative.**

---

## 6. The two search axes (the key operating rule)
Whether the information is in a field's metadata or inside its code values picks the tool.

### Horizontal — field search (`search`)
- When the name is the variable: BMI, "fresh fruit intake", a GAD-7 item, a blood marker.
- `search "<text>" [--category <id>] [--type <T>] [--all]` → FTS over title/notes/category_path,
  title-weighted bm25.
- It is **lexical** (token overlap + stemming), not semantic → you must expand synonyms / clinical terms.

### Vertical — code search → field reverse-lookup (`search-code` → `fields-using`)
- When the thing exists **only as a code value**: a specific disease (ICD), drug, operation (OPCS), job
  (SOC), specific food. (e.g. "metformin" is no field's title — it is a code value in enc 4.)
- `search-code "<text>" [--actionable-only]` → matched codes + their encoding + the fields using it.
- `fields-using <encoding_id>` → every field that uses a dictionary (e.g. ICD10 = 19).
- `actionable=true` = a Showcase field uses it (extractable); `false` = §8.

### Tool picker
| looking for | tool |
|---|---|
| the name is the variable | `search` |
| a disease / drug / operation / job / food code | `search-code` → `fields-using` |
| siblings / completeness | `neighbors`, `category`, `tree` |
| detail / missing / aggregation | `field`, `coding` |

Most real phenotypes mix axes (e.g. "T2D on metformin" = drug code + ICD code + a continuous field).

---

## 7. Completeness — why one search is not enough
`search "fruit intake"` recovered only **17 of the paper's 26** fruit fields in the top 30 — "Berries"/
"Citrus" have no "fruit" token and sink in bm25. Lexical search has real recall holes.

**Recipe (recovers 26/26):**
```
1. decompose the concept ("fruit" → apple, berry, citrus, juice, dried, smoothie …)  — your knowledge
2. search / search-code each term → seed fields
3. walk the graph: neighbors field <seed> (siblings) / neighbors category <id> — enumerate the branch
4. read the expanded candidates and keep only the matches → rules.json
```
One keyword hit ("Fruit juice" 26095) is enough — its `category` / `neighbors` siblings include the
buried ones. The branch also contains unrelated siblings (beef, beer) — **selection is yours**.

`neighbors` is the deterministic graph-walk primitive (parents/children/siblings for any node; fixed per
`kg_version`). `neighbors code <enc> <value>` walks ICD/OPCS subtrees for outcome definitions.

---

## 8. "Not a simple field" — pitfalls (must know)
- **`actionable=false`**: the code lives only in **record-level linked data** (GP Read/CTV3, hospital HES,
  death registry), not a Showcase field → no simple column extract; tell the user the linked-data path.
  (e.g. "type 2 diabetes" surfaces CTV3 codes first, but `fields-using` is empty → use `--actionable-only`.)
- **first-occurrence derived fields**: UKB builds per-ICD "Date X first reported" fields (130xxxx), so a
  disease name appears as a field title — simple, but algorithmically defined.
- **lexical limits on the code axis too**: ICD10 calls T2D "Non-insulin-dependent diabetes mellitus" —
  search with the coding system's own vocabulary; expand synonyms yourself.

---

## 9. Encoding checklist (→ rules.json → encode.py)
1. `field <id>`: read `type`, `encoding_id`, `columns` → column naming.
2. `coding`: apply `special_kind` — `missing` → `missing_codes` (NaN); `bounded`/levels → `recode`.
3. Unit / frequency harmonization (weekly↔monthly↔daily, grams↔servings) — record the assumption.
4. Choose `instance_agg` (per field) and `combine` (across fields): `mean|max|min|sum|first_non_null` /
   `sum|max|min|mean|priority|first_non_null`. For derived/conditional logic use a sandboxed
   `expression` (e.g. `weight/(height/100)**2`); `{"variable": name}` reuses an earlier output variable.
5. Optional `score`: threshold/membership → label.
6. **Fail loud**: a referenced field absent from the extract raises (encode.py default) — no silent NaN.
7. Artifacts: structured `rules.json` (plan, reviewed with the user) → `gen_encoder.py` → `encode.py`.

---

## 10. Worked examples
- **Horizontal**: `search "fresh fruit intake"` → field `1309` (Integer, pieces/day, coding `100373`:
  −10 bounded, −1/−3 missing).
- **Vertical (drug)**: `search-code "metformin" --actionable-only` → enc `4` → field `20003`. Rule: array
  of `20003` contains `1140884600`.
- **Vertical (diagnosis / outcome)**: ICD10 = enc `19` → `fields-using 19` → `41202`/`41204`; hierarchical,
  so `neighbors code 19 E11` expands E11.0–E11.9.
- **Completeness**: `search "fruit"` seed → `neighbors field 26095` / `category 100118` recovers
  26089/26090/26091.

---

## 11. Tool quick reference (`python3 scripts/kg_query.py <cmd>`)
| cmd | use |
|---|---|
| `search "<q>" [--category --type --all]` | horizontal field candidates (lexical seed) |
| `search-code "<q>" [--actionable-only]` | vertical: code → encoding → field |
| `fields-using <encoding_id>` | every field using a dictionary |
| `neighbors <category\|field\|encoding\|code> <id> [value]` | **graph walk**: parents/children/siblings (deterministic) |
| `category <id>` | category info + subcategories + member fields |
| `tree <id> [--up\|--down]` | ancestor/descendant categories |
| `field <id>` | field detail + coding + column template |
| `coding <encoding_id> [--specials-only]` | value → meaning map |
| `stats` | KG version / counts |

Add `--log <file>` to any command to append `{query, result, kg_version}` JSONL (audit trail). Seed with
`search`, achieve completeness with `neighbors` (reproducible, unlike fuzzy search). Other scripts:
`build_kg.py` (build the KG), `gen_encoder.py` (rules.json → encode.py), `render_audit.py` (→ HTML report).
