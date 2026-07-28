---
name: berkeleytime
description: >-
  Query and consolidate UC Berkeley course-catalog data from the Berkeleytime
  GraphQL API (berkeleytime.com). Use for any request about Berkeley classes for
  a given term — finding, filtering, ranking, or comparing courses by breadth
  requirement, average grade / A-rate, student-reported workload & difficulty,
  open (non-reserved) seats, units, meeting days/times, level (lower/upper/grad),
  grading basis (P/NP), department, or university requirements (American Cultures,
  R&C); pulling a class's full letter-grade distribution or rich details; or any
  bulk "give me all the classes that …" consolidation task. ALWAYS present results
  as a table. Falls back to live GraphQL introspection for anything not documented
  here.
---

# Berkeleytime course data

Access and consolidate UC Berkeley catalog data via the public GraphQL API at
`https://berkeleytime.com/api/graphql` (POST JSON, **no auth** for reads).

**Golden rule: every answer is a table.** Whatever the user asks for, run the
query and present the results as a table (Markdown by default so it renders in
chat). Add a one-line summary above it and any important caveats below.

## The one tool: `scripts/bt.py`

A self-contained Python 3 CLI (stdlib only). It handles full pagination,
consolidation (dedup sections, collapse cross-listings, drop language courses),
flattens ratings into columns, and prints a table. Run `python3 scripts/bt.py -h`
or `<subcommand> -h` for every option.

### `search` — the workhorse (mass access + consolidation)

```
python3 scripts/bt.py search [term] [server-filters] [sort] [consolidation] [output]
```

**Term:** `--year` `--semester` (Fall/Spring/Summer/Winter). Both default to the
most recent term that actually has searchable catalog data (probed live via
`terms` + `catalogSearch`, not the unreliable `hasCatalogData` flag) — override
either or both explicitly when you need a different term.

**Server-side filters** (map 1:1 to `CatalogFilters`):
`--breadths NAME...` · `--levels "Lower Division" "Upper Division" Graduate` ·
`--departments ...` · `--grading PNP OPT GRD ...` · `--university-reqs ...` ·
`--units-min N` · `--units-max N` · `--time-from HH:MM` · `--time-to HH:MM` ·
`--days INT` (bitmask, see references) · `--online` ·
`--enrollment OPEN|NON_RESERVED_OPEN|WAITLIST_OPEN` · `--search "text"` · `--semantic`.

**Server-side sort:** `--sort RELEVANCE|AVERAGE_GRADE|UNITS|OPEN_SEATS` `--order ASC|DESC`.

**Consolidation / local:** `--exclude-languages` · `--collapse-crosslist` ·
`--min-grade 3.5` · `--sort-local grade|workload|difficulty|open|units|...` `--asc`.

**Output:** `--format md|table|csv|json` (default `table`) ·
`--fields grade,code,title,open,cap,units,workload,difficulty,usefulness,recommended,sections,meet`.

Available columns: `grade` (all-time avg GPA, proxy for A-rate), `code`,
`title`, `open`/`cap` (non-reserved open seats / total), `units`, `workload`,
`difficulty`, `usefulness`, `recommended` (student-rated, weighted avg — pulled
inline, no extra request), `sections`, `meet` (decoded days + times).

### Other subcommands
- `filter-options [term]` — list every valid `--breadths`, `--levels`,
  `--grading`, `--university-reqs`, `--departments` value for the term.
- `grades --subject COMPSCI --course-number 61C --number 001` — full letter
  distribution (A+…F counts) + P/NP %.
- `details --subject … --course-number … --number …` — description, requirements,
  instructors, exam, live enrollment.
- `introspect` — **the fallback** (see below).
- `raw --file q.graphql --vars '{...}'` (or `--query "..."`) — arbitrary GraphQL.

## Workflow

1. If the user names a filter value you're unsure of (an exact breadth string,
   a department code), run `filter-options` first.
2. Build one `search` call with the right server filters + sort. Prefer
   server-side `--sort AVERAGE_GRADE` / `--units-max` / `--time-from/--time-to`
   over fetching everything and filtering by hand.
3. Add `--exclude-languages` when the user wants "real" classes, and
   `--collapse-crosslist` so cross-listed courses appear once.
4. Choose `--fields` to match what was asked (e.g. add `workload,difficulty`
   for "easy" classes; `meet` for scheduling).
5. Print the Markdown table, summarize, and note caveats (e.g. grade is a GPA
   proxy; blank ratings = too few reviews; `—` grade = new course, no history).

## Common recipes

```bash
# Easiest high-GPA breadth classes with open seats, ranked by low workload
bt.py search --breadths "Philosophy & Values" "Social & Behavioral Sciences" \
  --enrollment NON_RESERVED_OPEN --exclude-languages --collapse-crosslist \
  --sort-local workload --asc \
  --fields grade,code,title,open,workload,difficulty,meet --format md

# Highest-grade open courses in a breadth, server-sorted, ≤3 units
bt.py search --breadths "Arts & Literature" --enrollment NON_RESERVED_OPEN \
  --sort AVERAGE_GRADE --units-max 3 --format md

# Classes that fit a schedule gap (start ≥10:00, end ≤16:00)
bt.py search --breadths "Historical Studies" --time-from 10:00 --time-to 16:00 \
  --enrollment NON_RESERVED_OPEN --sort-local grade --format md

# Lower-division only, P/NP allowed
bt.py search --breadths "International Studies" --levels "Lower Division" \
  --grading PNP OPT --format md
```

Multiple `--breadths` are OR'd (a class matching any of them). Because you can
only claim **one** breadth per class, the `code`/`breadths` result is the menu
of options, not additive credit.

## Introspection fallback (REQUIRED for anything undocumented)

If the user asks for a field, filter, enum, sort, or root query **not covered
above or in `references/schema.md`**, do not guess — discover it live:

```bash
python3 scripts/bt.py introspect --root              # all Query + Mutation fields
python3 scripts/bt.py introspect --type CatalogFilters   # input/object type shape
python3 scripts/bt.py introspect --enum EnrollmentFilterType   # enum values
```

Then use `raw` to run the newly-discovered query/fields, and still present the
result as a table. See `references/schema.md` for the full documented surface
(enums, filter inputs, all 12 breadths, result fields, ratings metrics, other
root queries, and the `days` bitmask).
