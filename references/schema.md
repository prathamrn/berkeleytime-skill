# Berkeleytime GraphQL — documented surface

Endpoint: `https://berkeleytime.com/api/graphql` · POST `application/json` · no auth
for reads. Cloudflare rejects the default `python-urllib` User-Agent, so send a
browser UA (the script does). Some course titles contain raw control characters —
parse JSON with `strict=False` (Python) or a lenient parser.

Everything below was captured via live introspection and verified against the API.
Anything not here → use `bt.py introspect` (see bottom).

## catalogSearch — the main query

```graphql
catalogSearch(
  year: Int!, semester: Semester!, search: String, filters: CatalogFilters,
  sortBy: CatalogSortBy, sortOrder: SortOrder, page: Int, pageSize: Int,
  semanticSearch: Boolean
) { totalCount, results { … } }
```

`totalCount` is authoritative. **The server hard-caps `pageSize` at 100 no
matter what you request** (verified: `pageSize: 150` and `pageSize: 200` both
silently return exactly 100 rows for page 1) — the Berkeleytime UI just pages
at 25. `bt.py search` handles this correctly: `fetch_all()` loops pages using
`totalCount` as the stopping condition, not "got fewer rows than asked for"
(that comparison would falsely declare page 1 the last page whenever
`--page-size` is set above the real cap). If you hand-roll a `raw` query with
your own pagination, loop the same way.

### result fields (per class section)
`year, semester, sessionId, subject, courseNumber, number, title, unitsMin,
unitsMax, courseTitle, allTimeAverageGrade, allTimePassCount, allTimeNoPassCount,
enrolledCount, maxEnroll, activeReservedMaxCount,
waitlistedCount, maxWaitlist, enrollmentStatus, primaryOnline,
aggregatedRatings { metrics { metricName count weightedAverage } },
decal { title },
meetings { days startTime endTime location instructors { givenName familyName } }`

- `allTimeAverageGrade` — all-time average GPA (best proxy for A-rate; `null` for
  courses with no grade history / new offerings).
- Open **non-reserved** seats ≈ `maxEnroll - enrolledCount` under the
  `NON_RESERVED_OPEN` enrollment filter.
- `meetings.days` is a **7-element boolean array in Mon→Sun order**
  `[Mon,Tue,Wed,Thu,Fri,Sat,Sun]` (verified: MWF classes = `[T,F,T,F,T,F,F]`).
- Ratings come back **inline** here — no per-class call needed for
  Workload/Difficulty/Usefulness/Recommended.
- **`meetings.instructors` and `meetings.location`** — undocumented in the
  public API surface but present on `CatalogMeeting` (found via
  `introspect --type CatalogMeeting`) and wired into `bt.py search` as it now
  requests them by default. This is the only way to get instructors **in
  bulk** (one call, any term/department/search-text combo) — `bt.py details`
  only gets you one class at a time. Use `search --instructor "Name"` (local
  substring filter, case-insensitive — matches first *or* last name, so
  `"Hug"` also matches `"Hughes"`) with `--fields instructor,code,title,meet,location`.
  No server-side instructor filter exists; the script fetches the whole
  term/department and filters client-side.
- `waitlistedCount`/`maxWaitlist` and `enrollmentStatus` (e.g. `"O"` open /
  `"C"` closed / `"W"` waitlist) are also on `CatalogClass` and wired into
  `search --fields waitlist,status`. `primaryOnline` → `--fields online`
  (`Yes`/`No`/`Mixed` when sections disagree).

## CatalogFilters (input)

| field | type | notes |
|---|---|---|
| `breadths` | [String] | breadth requirement names (see list); multiple ⇒ OR |
| `levels` | [String] | `Lower Division`, `Upper Division`, `Graduate` |
| `departments` | [String] | department names |
| `unitsMin` / `unitsMax` | Float | unit range |
| `days` | [Int] | list of day indices (Mon→Sun ⇒ 0–6, matching `meetings.days`; verify by experiment) |
| `timeFrom` / `timeTo` | String | `"HH:MM"` 24h window (earliest start / latest end) |
| `enrollmentFilter` | EnrollmentFilterType | see enum |
| `gradingFilters` | [String] | grading bases, e.g. `PNP`, `OPT`, `GRD` |
| `universityRequirements` | [String] | AC / American History / R&C etc. |
| `online` | Boolean | online/async only |

> Input list fields accept a JSON array (`["International Studies"]`). Introspection
> reports the inner scalar (`String`); arrays are what the client sends and what works.
>
> **`days`** is `[Int]` (a list of day indices), not a bitmask. Indices most
> likely follow `meetings.days` (Mon→Sun ⇒ 0–6). For scheduling, `timeFrom`/
> `timeTo` are the reliable levers; if you use `days`, verify the index basis by
> experiment first, or filter locally on the returned `meetings.days`.
>
> `departments` in `catalogFilterOptions` is `[CatalogDepartment]` with fields
> `{ code, name }` (needs a subfield selection), unlike the plain-string filter lists.

## Enums (verified)

- **CatalogSortBy:** `RELEVANCE`, `UNITS`, `AVERAGE_GRADE`, `OPEN_SEATS`
- **SortOrder:** `ASC`, `DESC`
- **EnrollmentFilterType:** `OPEN` (any open incl. reserved),
  `NON_RESERVED_OPEN` (open seats not held by reservations), `WAITLIST_OPEN`
- **Semester:** `Fall`, `Spring`, `Summer`, `Winter`
- **MetricName** (ratings): `Usefulness`, `Difficulty`, `Workload`, `Attendance`,
  `Recording`, `Recommended`
- **ClassGradingBasis:** `ESU, SUS, OPT, PNP, BMT, GRD, IOP, CNC, LAW, LW1`
- **Component / InstructionMethod:** `LEC, DIS, LAB, SEM, FLD, IND, STD, …`
  (LEC=lecture, DIS=discussion, LAB=lab, SEM=seminar)
- **Levels / AcademicCareer:** Lower Division, Upper Division, Graduate

## Breadth requirements (12) — exact `--breadths` strings

`American Cultures`, `American Hist & Institutions`, `Arts & Literature`,
`Biological Science`, `Entry Level Writing`, `Historical Studies`,
`International Studies`, `Philosophy & Values`, `Physical Science`,
`Reading and Composition A`, `Reading and Composition B`,
`Social & Behavioral Sciences`.

(L&S 7-Course Breadth is the first five + the two sciences; the R&C / ELW / AC&AH
entries are university requirements surfaced as breadth options.)

## University requirements (8) — exact `--university-reqs` strings

`Am Cultures & Am History`, `American Cultures`, `American History`,
`American Institutions`, `Entry Level Writing & Reading Composition A`,
`Reading and Composition A`, `Reading and Composition A or B`,
`Reading and Composition B`.

## Grading options (7)

`BMT, CNC, GRD, IOP, OPT, PNP, SUS` (GRD=letter, PNP=Pass/No-Pass, OPT=optional).

## Per-class queries

- **grades** — `class(year,semester,sessionId,subject,courseNumber,number){
  course { gradeDistribution { average pnpPercentage distribution { letter count } } } }`
  → full letter-grade counts.
- **details** — same key; `course { title description requirements
  aggregatedRatings{…} gradeDistribution{…} }` and
  `primarySection { component enrollment{ latest{ enrolledCount maxEnroll
  waitlistedCount maxWaitlist } } exams{…} meetings{ days location startTime
  endTime instructors{ familyName givenName } } }` — `instructors` here is the
  `Instructor` type (`{ givenName familyName }`), the single-class sibling of
  `CatalogInstructor` used in bulk `catalogSearch` results (same shape, two
  names because `class(...)` and `catalogSearch(...)` return parallel type
  trees — `Class`/`Section`/`Meeting`/`Instructor` vs.
  `CatalogClass`/`CatalogSection`/`CatalogMeeting`/`CatalogInstructor`).
  `primarySection` is the lecture; `class(...).sections` is discussions/labs
  and their `meetings.instructors` usually comes back empty (GSIs aren't
  populated the same way) — query `primarySection` for the instructor of record.

Key scalar types for `raw`/hand-written queries against `class(...)` or
`section(...)`: `sessionId: SessionIdentifier!` (usually `"1"`),
`courseNumber: CourseNumber!` (e.g. `"61C"`), `number: ClassNumber!` (section,
e.g. `"001"`). **These are distinct custom scalars, not `String`** — declaring
a query variable as `$courseNumber: String!` fails at runtime with "used in
position expecting type CourseNumber!" even though the value you pass is a
plain string. Declare the variable with the exact scalar name the field
expects (check via `introspect --type <ParentType>` if unsure) and the string
value passes through fine.

## Other useful root queries

`catalog(year,semester)` · `catalogFilterOptions(year,semester)` ·
`catalogClassIdentities(year,semester)` · `course(subject,number)` ·
`courseById(courseId)` · `section(...)` · `enrollment(...)` ·
`enrollmentTimeframes(year,semester)` · `grade(...)` ·
`aggregatedRatings(...)` · `multipleClassAggregatedRatings(...)` ·
`classReviews(subject,courseNumber)` (student review text) ·
`semestersWithRatings(subject,courseNumber)` · `terms(withCatalogData)`.

There are also ~60 **mutations** (schedules, collections, plans, banners, staff,
targeted messages) — these generally require authentication and are out of scope
for read/consolidation tasks.

## Introspection (fallback)

```bash
bt.py introspect --root                    # every Query + Mutation field
bt.py introspect --type CatalogFilters     # input/object field list w/ types
bt.py introspect --enum EnrollmentFilterType
```

Raw introspection query if you need it directly:

```graphql
query($n:String!){ __type(name:$n){ name kind
  enumValues{ name } inputFields{ name type{ name kind ofType{ name kind } } }
  fields{ name type{ name kind ofType{ name kind } } } } }
```

Type catalog (partial, from `__schema.types`): `CatalogResult`, `CatalogClass`,
`CatalogSection`, `CatalogMeeting`, `CatalogInstructor`, `CatalogDeCal`,
`CatalogExam`, `CatalogFilterOptions`, `AggregatedRatings`, `CatalogMetric`,
`Category`, plus the enums above. Introspect any of them for exact fields.
