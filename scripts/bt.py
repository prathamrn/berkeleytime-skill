#!/usr/bin/env python3
"""
bt.py — Berkeleytime GraphQL access & consolidation tool.

Endpoint: https://berkeleytime.com/api/graphql  (POST JSON, no auth for reads)

Subcommands:
  search          Mass catalog search: all filters, full pagination, dedup,
                  cross-list collapse, language exclusion, ratings flattening.
  filter-options  Dump valid filter values (breadths, levels, grading, reqs) for a term.
  grades          Full letter grade distribution for one class.
  details         Rich class details (description, reqs, instructors, exam, enrollment).
  introspect      Schema introspection FALLBACK: --root | --type NAME | --enum NAME.
  raw             Run an arbitrary GraphQL query (inline or --file) with --vars JSON.

Examples:
  bt.py search --breadths "Philosophy & Values" "Arts & Literature" \
        --enrollment NON_RESERVED_OPEN --units-max 3 --exclude-languages \
        --sort-local grade --format md
  bt.py search --breadths "Social & Behavioral Sciences" --sort AVERAGE_GRADE \
        --min-grade 3.5 --time-from 10:00 --time-to 16:00 --format table
  bt.py search --departments ELENG --instructor "Hug" \
        --fields instructor,code,title,meet,location --format md
  bt.py filter-options
  bt.py introspect --type CatalogFilters
  bt.py raw --file q.graphql --vars '{"y":2026,"s":"Fall"}'
"""
import argparse, json, sys, ssl, urllib.request, urllib.error

ENDPOINT = "https://berkeleytime.com/api/graphql"

# Some python.org builds ship without system CA certs. Try verified first, then
# fall back to an unverified context (fine for this public, read-only API).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()
_SSL_CTX_UNVERIFIED = ssl._create_unverified_context()

# ---- known-good full result selection for catalogSearch ----------------------
# meetings.instructors / .location are undocumented in the public schema but
# present on CatalogMeeting (verified live via introspect --type CatalogMeeting) —
# this is the only way to get instructor names in bulk (one call for a whole
# term/department), vs. `details`/raw `class(...)` which is per-class.
CATALOG_RESULT_FIELDS = """
  year semester sessionId subject courseNumber number title
  unitsMin unitsMax courseTitle
  allTimeAverageGrade allTimePassCount allTimeNoPassCount
  enrolledCount maxEnroll activeReservedMaxCount
  waitlistedCount maxWaitlist enrollmentStatus primaryOnline
  aggregatedRatings { metrics { metricName count weightedAverage } }
  decal { title }
  meetings { days startTime endTime location instructors { givenName familyName } }
"""

CATALOG_SEARCH_QUERY = """
query BtSearch($year:Int!,$semester:Semester!,$search:String,$filters:CatalogFilters,
               $sortBy:CatalogSortBy,$sortOrder:SortOrder,$page:Int,$pageSize:Int,$semanticSearch:Boolean){
  catalogSearch(year:$year,semester:$semester,search:$search,filters:$filters,
                sortBy:$sortBy,sortOrder:$sortOrder,page:$page,pageSize:$pageSize,
                semanticSearch:$semanticSearch){
    totalCount
    results { %s }
  }
}""" % CATALOG_RESULT_FIELDS

CLASS_KEY_ARGS = """$year:Int!,$semester:Semester!,$sessionId:SessionIdentifier!,
  $subject:String!,$courseNumber:CourseNumber!,$number:ClassNumber!"""
CLASS_KEY_PASS = """year:$year,semester:$semester,sessionId:$sessionId,
  subject:$subject,courseNumber:$courseNumber,number:$number"""

GRADES_QUERY = """
query BtGrades(%s){ class(%s){ course { gradeDistribution {
  average pnpPercentage
  distribution { letter count } } } } }""" % (CLASS_KEY_ARGS, CLASS_KEY_PASS)

DETAILS_QUERY = """
query BtDetails(%s){ class(%s){
  courseId number unitsMin unitsMax finalExam
  course { title description requirements
    gradeDistribution { average pnpPercentage }
    aggregatedRatings { metrics { metricName count weightedAverage } } }
  primarySection { component
    enrollment { latest { enrolledCount maxEnroll waitlistedCount maxWaitlist } }
    exams { date startTime endTime location type }
    meetings { days location startTime endTime
      instructors { familyName givenName } } }
} }""" % (CLASS_KEY_ARGS, CLASS_KEY_PASS)

# Language subject codes + language-instruction title pattern (for --exclude-languages)
LANG_SUBJECTS = {"CHINESE","JAPAN","KOREAN","FRENCH","GERMAN","SPANISH","ITALIAN","PORTUG",
"SLAVIC","DUTCH","SCANDIN","CELTIC","GREEK","MDGRK","LATIN","ARABIC","HEBREW","PERSIAN",
"TURKISH","SANSKRT","SANSKR","SASIAN","SEASIAN","TAMIL","HINDI","URDU","FILIPN","VIETNMS",
"THAI","SWAHILI","YORUBA","POLISH","CZECH","HUNGARN","RUSSIAN","BOSCRSR","ELANG","MELC",
"EALANG","TIBETAN","MONGOL","MONGOLN","BANGLA","PUNJABI","ARMENI","GEORGIA","INDONES",
"CATALAN","NORWEGN","FINNISH","SEASN","SSEASN","BURMESE","UKRAINI"}
import re
LANG_TITLE_RE = re.compile(
    r"\b(Elementary|Intermediate|Advanced|Continuing|Beginning|Introductory)\b.*\b"
    r"(Swahili|Burmese|Sanskrit|Ukrainian|Yoruba|Wolof|Zulu|Hausa|Amharic|Tagalog|Chichewa)\b",
    re.I)
DAY_LABELS = ["M","Tu","W","Th","F","Sa","Su"]  # meetings.days is a Mon->Sun boolean array


_TERM_RANK = {"Spring": 0, "Summer": 1, "Fall": 2, "Winter": 3}


def latest_term():
    """Most recent (year, semester) with actual searchable catalog data.

    Term.hasCatalogData is unreliable for this: future terms can already have
    real course listings (catalogSearch totalCount > 0) while still flagged
    False. So pull every known term and probe catalogSearch directly, newest
    first, returning the first one that actually has results.
    """
    d = gql("{ terms(withCatalogData: false) { year semester } }")
    terms = d.get("terms") or []
    if not terms:
        raise SystemExit("Could not determine the latest term (terms query returned none).")
    uniq = {(t["year"], t["semester"]) for t in terms}
    ordered = sorted(uniq, key=lambda t: (t[0], _TERM_RANK.get(t[1], 0)), reverse=True)
    probe = """query($y:Int!,$s:Semester!){
      catalogSearch(year:$y,semester:$s,page:1,pageSize:1){ totalCount } }"""
    for year, semester in ordered:
        r = gql(probe, {"y": year, "s": semester})
        if ((r.get("catalogSearch") or {}).get("totalCount") or 0) > 0:
            return year, semester
    raise SystemExit("No term with catalog data found.")


def resolve_term(a):
    """Fill in --year/--semester with the latest catalog term when either was omitted."""
    if getattr(a, "year", None) is None or getattr(a, "semester", None) is None:
        year, semester = latest_term()
        if a.year is None:
            a.year = year
        if a.semester is None:
            a.semester = semester


def gql(query, variables=None, timeout=60):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "content-type": "application/json",
        # Cloudflare rejects the default python-urllib UA with an empty body.
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "accept": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):  # cert store missing → retry unverified
            raw = urllib.request.urlopen(req, timeout=timeout,
                                         context=_SSL_CTX_UNVERIFIED).read().decode("utf-8", "replace")
        else:
            raise
    d = json.loads(raw, strict=False)  # strict=False: some titles carry raw control chars
    if d.get("errors"):
        sys.stderr.write("GraphQL errors: " + json.dumps(d["errors"], indent=2) + "\n")
    return d.get("data") or {}


def decode_days(days):
    if not days:
        return ""
    return "".join(lbl for lbl, on in zip(DAY_LABELS, days) if on)


def ratings_map(agg):
    out = {}
    for m in ((agg or {}).get("metrics") or []):
        out[m["metricName"]] = (m.get("weightedAverage"), m.get("count"))
    return out


# ------------------------------- search ---------------------------------------
def build_filters(a):
    f = {}
    if a.enrollment:        f["enrollmentFilter"] = a.enrollment
    if a.breadths:          f["breadths"] = a.breadths
    if a.levels:            f["levels"] = a.levels
    if a.departments:       f["departments"] = a.departments
    if a.grading:           f["gradingFilters"] = a.grading
    if a.university_reqs:   f["universityRequirements"] = a.university_reqs
    if a.units_min is not None: f["unitsMin"] = a.units_min
    if a.units_max is not None: f["unitsMax"] = a.units_max
    if a.time_from:         f["timeFrom"] = a.time_from
    if a.time_to:           f["timeTo"] = a.time_to
    if a.days is not None:  f["days"] = a.days if isinstance(a.days, list) else [a.days]
    if a.online:            f["online"] = True
    return f


def fetch_all(a, filters):
    # The server silently caps pageSize at 100 regardless of what's requested
    # (verified: pageSize=150/200 both return exactly 100 rows/page). Stop on
    # totalCount, not on "got fewer than we asked for" — that comparison lies
    # whenever --page-size is set above the real cap and would drop the tail.
    page, out, total = 1, [], None
    while True:
        d = gql(CATALOG_SEARCH_QUERY, {
            "year": a.year, "semester": a.semester, "search": a.search,
            "filters": filters, "sortBy": a.sort, "sortOrder": a.order,
            "page": page, "pageSize": a.page_size,
            "semanticSearch": a.semantic})
        cs = (d.get("catalogSearch") or {})
        res = cs.get("results") or []
        if cs.get("totalCount") is not None:
            total = cs["totalCount"]
        out.extend(res)
        if not res or (total is not None and len(out) >= total):
            break
        page += 1
    return out


def consolidate(rows, a):
    # optional language exclusion
    if a.exclude_languages:
        rows = [r for r in rows if r["subject"] not in LANG_SUBJECTS
                and not LANG_TITLE_RE.search(r.get("courseTitle") or "")]
    # collapse sections within a course code, then (optionally) cross-listings
    by_code = {}
    for r in rows:
        k = (r["subject"], r["courseNumber"])
        e = by_code.get(k)
        if not e:
            e = {"subject": r["subject"], "courseNumber": r["courseNumber"],
                 "title": r.get("courseTitle"), "grade": r.get("allTimeAverageGrade"),
                 "unitsMin": r.get("unitsMin"), "unitsMax": r.get("unitsMax"),
                 "open": 0, "cap": 0, "sections": 0, "ratings": {},
                 "meetings": set(), "instructor_set": set(), "location_set": set(),
                 "waitlisted": 0, "maxWaitlist": 0, "status_set": set(), "online_flags": set()}
            by_code[k] = e
        e["open"] += (r.get("maxEnroll") or 0) - (r.get("enrolledCount") or 0)
        e["cap"] += (r.get("maxEnroll") or 0)
        e["sections"] += 1
        e["waitlisted"] += r.get("waitlistedCount") or 0
        e["maxWaitlist"] += r.get("maxWaitlist") or 0
        if r.get("enrollmentStatus"):
            e["status_set"].add(r["enrollmentStatus"])
        if r.get("primaryOnline") is not None:
            e["online_flags"].add(r["primaryOnline"])
        if r.get("allTimeAverageGrade") is not None:
            e["grade"] = r["allTimeAverageGrade"]
        if not e["ratings"]:
            e["ratings"] = ratings_map(r.get("aggregatedRatings"))
        for m in (r.get("meetings") or []):
            e["meetings"].add((decode_days(m.get("days")),
                               (m.get("startTime") or "")[:5], (m.get("endTime") or "")[:5]))
            for i in (m.get("instructors") or []):
                nm = f"{i.get('givenName') or ''} {i.get('familyName') or ''}".strip()
                if nm:
                    e["instructor_set"].add(nm)
            if m.get("location"):
                e["location_set"].add(m["location"])

    items = list(by_code.values())
    if a.collapse_crosslist:
        merged = {}
        for e in items:
            gk = round(e["grade"], 4) if e["grade"] is not None else None
            k = (e["title"], gk, e["open"], e["cap"])
            m = merged.get(k)
            if not m:
                m = dict(e); m["codes"] = []; merged[k] = m
            m["codes"].append(f'{e["subject"]} {e["courseNumber"]}')
            m["meetings"] |= e["meetings"]
            m["instructor_set"] |= e["instructor_set"]
            m["location_set"] |= e["location_set"]
            m["status_set"] |= e["status_set"]
            m["online_flags"] |= e["online_flags"]
        items = list(merged.values())
        for m in items:
            m["code"] = " / ".join(sorted(m["codes"]))
    else:
        for e in items:
            e["code"] = f'{e["subject"]} {e["courseNumber"]}'

    # flatten a few ratings for output/sort
    for e in items:
        for metric in ("Workload", "Difficulty", "Usefulness", "Recommended"):
            wa, _ = e["ratings"].get(metric, (None, None))
            e[metric.lower()] = wa
        um, uM = e.get("unitsMin"), e.get("unitsMax")
        e["units"] = str(um) if um == uM else f"{um}-{uM}"
        e["meet"] = "; ".join(f"{d} {s}-{en}" for d, s, en in sorted(e["meetings"]) if d) or "TBA"
        e["instructor"] = ", ".join(sorted(e["instructor_set"])) or "—"
        e["location"] = "; ".join(sorted(e["location_set"])) or "TBA"
        e["waitlist"] = f'{e["waitlisted"]}/{e["maxWaitlist"]}' if e["maxWaitlist"] else "—"
        e["status"] = ", ".join(sorted(e["status_set"])) or "—"
        flags = e["online_flags"]
        e["online"] = "Yes" if flags == {True} else "No" if flags == {False} else (
            "Mixed" if flags else "—")

    if a.min_grade is not None:
        items = [e for e in items if (e["grade"] or 0) >= a.min_grade]

    if a.instructor:
        needle = a.instructor.lower()
        items = [e for e in items if needle in e["instructor"].lower()]

    # local sort
    key = a.sort_local
    if key:
        rev = not a.asc
        def sk(e):
            v = e.get(key)
            return (v is None, -(v or 0) if rev else (v or 0))
        # for text keys fall back to plain
        if key in ("code", "title", "meet", "units"):
            items.sort(key=lambda e: (e.get(key) is None, str(e.get(key) or "")), reverse=rev)
        else:
            items.sort(key=lambda e: (e.get(key) is None, (e.get(key) or 0)), reverse=rev)
    return items


COLUMNS = ["grade", "code", "title", "open", "cap", "units", "workload",
           "difficulty", "usefulness", "recommended", "sections", "meet"]


def fmt_val(e, c):
    if c == "cap": return str(e.get("cap"))
    if c == "open": return str(e.get("open"))
    if c == "grade":
        g = e.get("grade"); return f"{g:.2f}" if g is not None else "—"
    if c in ("workload", "difficulty", "usefulness", "recommended"):
        v = e.get(c); return f"{v:.2f}" if isinstance(v, (int, float)) else "—"
    return str(e.get(c) if e.get(c) is not None else "")


def output(items, a):
    cols = [c.strip() for c in a.fields.split(",")] if a.fields else COLUMNS
    if a.format == "json":
        clean = [{c: e.get(c) for c in cols} for e in items]
        print(json.dumps(clean, indent=2))
    elif a.format == "csv":
        import csv
        w = csv.writer(sys.stdout); w.writerow(cols)
        for e in items: w.writerow([fmt_val(e, c) for c in cols])
    elif a.format == "md":
        print("| " + " | ".join(cols) + " |")
        print("|" + "|".join("---" for _ in cols) + "|")
        for e in items:
            print("| " + " | ".join(fmt_val(e, c).replace("|", "\\|") for c in cols) + " |")
    else:  # table
        widths = {c: max(len(c), *(len(fmt_val(e, c)) for e in items)) if items else len(c) for c in cols}
        print("  ".join(c.ljust(widths[c]) for c in cols))
        print("  ".join("-" * widths[c] for c in cols))
        for e in items:
            print("  ".join(fmt_val(e, c).ljust(widths[c]) for c in cols))
    sys.stderr.write(f"\n[{len(items)} rows]\n")


def cmd_search(a):
    filters = build_filters(a)
    rows = fetch_all(a, filters)
    items = consolidate(rows, a)
    output(items, a)


# --------------------------- filter-options -----------------------------------
def cmd_filter_options(a):
    # departments is an object list ([CatalogDepartment!]!) — request subfields.
    q = """query($y:Int!,$s:Semester!){ catalogFilterOptions(year:$y,semester:$s){
      levels breadthRequirements universityRequirements gradingOptions
      departments { code name } } }"""
    d = gql(q, {"y": a.year, "s": a.semester}).get("catalogFilterOptions") or {}
    for k in ("levels", "breadthRequirements", "universityRequirements", "gradingOptions"):
        v = d.get(k) or []
        print(f"\n{k} ({len(v)}):")
        for x in v: print(f"  {x}")
    depts = d.get("departments") or []
    print(f"\ndepartments ({len(depts)}):")
    for x in depts:
        print(f"  {x.get('code','')}: {x.get('name','')}" if isinstance(x, dict) else f"  {x}")


# ------------------------------- grades ---------------------------------------
def class_key(a):
    return {"year": a.year, "semester": a.semester, "sessionId": a.session,
            "subject": a.subject, "courseNumber": a.course_number, "number": a.number}


def cmd_grades(a):
    d = gql(GRADES_QUERY, class_key(a)).get("class") or {}
    gd = ((d.get("course") or {}).get("gradeDistribution") or {})
    print(f"average={gd.get('average')}  pnpPercentage={gd.get('pnpPercentage')}")
    for row in (gd.get("distribution") or []):
        print(f"  {row['letter']:>3}: {row['count']}")


def cmd_details(a):
    d = gql(DETAILS_QUERY, class_key(a)).get("class") or {}
    print(json.dumps(d, indent=2))


# ----------------------------- introspect (FALLBACK) --------------------------
def cmd_introspect(a):
    if a.root:
        q = """{ __schema { queryType{ fields{ name args{ name }
              type{ name kind ofType{ name kind } } } }
              mutationType{ fields{ name } } } }"""
        d = gql(q).get("__schema") or {}
        print("=== Query fields ===")
        for f in (d.get("queryType") or {}).get("fields") or []:
            args = ",".join(x["name"] for x in f["args"])
            print(f"  {f['name']}({args})")
        mt = d.get("mutationType")
        if mt:
            print("\n=== Mutation fields ===")
            for f in mt.get("fields") or []:
                print(f"  {f['name']}")
        return
    name = a.type or a.enum
    q = """query($n:String!){ __type(name:$n){ name kind description
      enumValues{ name description }
      inputFields{ name type{ name kind ofType{ name kind ofType{ name kind } } } }
      fields{ name type{ name kind ofType{ name kind ofType{ name kind } } } } } }"""
    t = gql(q, {"n": name}).get("__type")
    if not t:
        print(f"No such type: {name}"); return

    def tn(ty):
        s = ""
        while ty:
            if ty.get("kind") == "LIST": s = "[]" + s
            if ty.get("name"): return ty["name"] + s
            ty = ty.get("ofType")
        return "?" + s
    print(f"{t['kind']} {t['name']}" + (f" — {t['description']}" if t.get("description") else ""))
    for f in t.get("enumValues") or []:
        print(f"  = {f['name']}")
    for f in t.get("inputFields") or []:
        print(f"  input {f['name']}: {tn(f['type'])}")
    for f in t.get("fields") or []:
        print(f"  field {f['name']}: {tn(f['type'])}")


# -------------------------------- raw -----------------------------------------
def cmd_raw(a):
    query = open(a.file).read() if a.file else a.query
    variables = json.loads(a.vars) if a.vars else {}
    print(json.dumps(gql(query, variables), indent=2))


def main():
    p = argparse.ArgumentParser(description="Berkeleytime GraphQL access & consolidation")
    sub = p.add_subparsers(dest="cmd", required=True)

    def term_args(sp):
        sp.add_argument("--year", type=int, default=None,
                        help="defaults to the most recent term with catalog data")
        sp.add_argument("--semester", default=None,
                        choices=["Fall", "Spring", "Summer", "Winter"],
                        help="defaults to the most recent term with catalog data")

    s = sub.add_parser("search", help="mass catalog search + consolidation")
    term_args(s)
    s.add_argument("--search", help="free-text search")
    s.add_argument("--semantic", action="store_true", help="semantic search")
    # server-side filters (CatalogFilters)
    s.add_argument("--breadths", nargs="+", help="one or more breadth names")
    s.add_argument("--levels", nargs="+", help='e.g. "Lower Division" "Upper Division" Graduate')
    s.add_argument("--departments", nargs="+")
    s.add_argument("--grading", nargs="+", help="grading bases e.g. PNP OPT GRD")
    s.add_argument("--university-reqs", nargs="+")
    s.add_argument("--units-min", type=float)
    s.add_argument("--units-max", type=float)
    s.add_argument("--time-from", help="HH:MM (24h), earliest start")
    s.add_argument("--time-to", help="HH:MM (24h), latest end")
    s.add_argument("--days", type=int, help="day bitmask (see references/schema.md)")
    s.add_argument("--online", action="store_true")
    s.add_argument("--enrollment", choices=["OPEN", "NON_RESERVED_OPEN", "WAITLIST_OPEN"])
    # server-side sort
    s.add_argument("--sort", default="RELEVANCE",
                   choices=["RELEVANCE", "AVERAGE_GRADE", "UNITS", "OPEN_SEATS"])
    s.add_argument("--order", default="DESC", choices=["ASC", "DESC"])
    s.add_argument("--page-size", type=int, default=100)
    # consolidation / local
    s.add_argument("--exclude-languages", action="store_true")
    s.add_argument("--collapse-crosslist", action="store_true",
                   help="merge cross-listed courses onto one row")
    s.add_argument("--min-grade", type=float)
    s.add_argument("--instructor", help="filter to classes with an instructor name "
                   "matching this substring, case-insensitive (e.g. 'Hug')")
    s.add_argument("--sort-local", help="re-sort locally by any output column "
                   "(grade, workload, difficulty, open, units, ...)")
    s.add_argument("--asc", action="store_true", help="ascending local sort")
    s.add_argument("--fields", help="comma-separated subset of output columns")
    s.add_argument("--format", default="table", choices=["table", "md", "csv", "json"])
    s.set_defaults(func=cmd_search)

    fo = sub.add_parser("filter-options", help="valid filter values for a term")
    term_args(fo); fo.set_defaults(func=cmd_filter_options)

    def class_args(sp):
        term_args(sp)
        sp.add_argument("--session", default="1", help="sessionId (usually '1')")
        sp.add_argument("--subject", required=True)
        sp.add_argument("--course-number", required=True, help="e.g. 61C")
        sp.add_argument("--number", required=True, help="class/section number, e.g. 001")

    g = sub.add_parser("grades", help="full letter distribution for one class")
    class_args(g); g.set_defaults(func=cmd_grades)
    de = sub.add_parser("details", help="rich details for one class")
    class_args(de); de.set_defaults(func=cmd_details)

    it = sub.add_parser("introspect", help="schema introspection FALLBACK")
    it.add_argument("--root", action="store_true", help="list Query/Mutation fields")
    it.add_argument("--type", help="describe an OBJECT/INPUT type")
    it.add_argument("--enum", help="list an ENUM's values")
    it.set_defaults(func=cmd_introspect)

    r = sub.add_parser("raw", help="run an arbitrary GraphQL query")
    r.add_argument("--query", help="inline query string")
    r.add_argument("--file", help="path to a .graphql file")
    r.add_argument("--vars", help="JSON variables")
    r.set_defaults(func=cmd_raw)

    a = p.parse_args()
    if hasattr(a, "year"):
        resolve_term(a)
    a.func(a)


if __name__ == "__main__":
    main()
