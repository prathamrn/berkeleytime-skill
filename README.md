# berkeleytime CLI / claude skill

Interact with Berkeleytime (and Rate My Professors) using an AI agent! Ask Claude Code things like:

- "easiest open Arts & Literature classes with seats, ranked by estimated workload"
- "american cultures classes that aren't on Mondays from 4PM to 5PM"
- "when is the best time to take cs 189 according to rate my professors ratings in the past 3 years and average gpa of their sections"
- "music department classes with the highest GPA that fulfill any breadth, with non-reserved open seats"
- "which breadths does espm 50ac fulfill"
- "does the professor for CS70 have a higher rate my professors rating than the one for last semester"
- "make me a schedule for this semester as a sophomore cogsci major who's taken x, y, and z"

Experiment with what you can do! Not everything is documented in the repo, but the agent can use introspection to determine if it's possible.

This skill gives Claude fine-grained, bulk access to the [Berkeleytime](https://berkeleytime.com) course catalog. **This project is not affiliated with Berkeleytime**.

## Install
**Global one-line install** (the skill will work in every directory):

```bash
curl -fsSL https://raw.githubusercontent.com/prathamrn/berkeleytime-skill/main/install.sh | bash
```

Piping from `curl` defaults to the global install; add `| bash -s -- --project`
to put it in the current directory instead. Other targets, either way:

```bash
./install.sh --personal        # ~/.claude/skills/  (global, all projects)
./install.sh --project         # ./.claude/skills/  (this project only)
./install.sh --to /some/dir    # any skills directory you point Claude at
```

Then start a Claude Code session and ask about Berkeley classes. The skill loads
on its own but you can invoke it explicitly with `/berkeleytime`. 

## Usage without Claude Code
`scripts/bt.py` is a normal CLI you can run yourself:

```bash
# Easiest high-GPA breadth classes with open seats, ranked by low workload
python3 scripts/bt.py search \
  --breadths "Philosophy & Values" "Social & Behavioral Sciences" \
  --enrollment NON_RESERVED_OPEN --exclude-languages --collapse-crosslist \
  --sort-local workload --asc \
  --fields grade,code,title,open,workload,difficulty,meet --format md

# Classes that fit a schedule gap (start ≥10:00, end ≤16:00)
python3 scripts/bt.py search --breadths "Historical Studies" \
  --time-from 10:00 --time-to 16:00 --enrollment NON_RESERVED_OPEN --sort-local grade

# Full letter-grade distribution for one class
python3 scripts/bt.py grades --subject COMPSCI --course-number 61C --number 001

# RateMyProfessors rating + profile link (defaults to UC Berkeley)
python3 scripts/bt.py rmp --name "Paul Hilfinger"
```

Subcommands: `search`, `filter-options`, `grades`, `details`, `introspect`, `raw`, `rmp`.
Run `python3 scripts/bt.py -h` or `<subcommand> -h` for every flag.
Term defaults to **Fall 2026**; override with `--year` / `--semester`.

Output formats: `--format table` (default), `md`, `csv`, `json`.
Columns via `--fields`: `grade, code, title, open, cap, units, workload,
difficulty, usefulness, recommended, sections, meet`.

## Disclaimer
Using the skill isn't perfect and should be checked on Berkeley's official course catalog.
The `rmp` subcommand queries RateMyProfessors' own public API directly; this project is
not affiliated with RateMyProfessors either, and ratings reflect self-selected student
reviews, not an official metric.

## License

MIT — see [LICENSE](LICENSE).
