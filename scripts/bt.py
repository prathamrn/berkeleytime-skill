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
  ops             List/show the persisted operations the API accepts (see below).
  raw             Run one persisted operation by name: --op NAME --vars JSON.
  rmp             RateMyProfessors rating + profile link for a professor by name.

NOTE — persisted operations. The API rejects arbitrary GraphQL documents
("Invalid persisted operation request"); it only accepts {id, variables} where
id is the sha256 of a document shipped in berkeleytime.com's own JS bundle.
scripts/persisted.py recovers that id set from the live bundle and caches it in
scripts/persisted-ops.json (auto-refreshed weekly, or on a rejection). Two
consequences: schema introspection is gone (`ops --show NAME` replaces it), and
catalogSearch returns only the fields the web app asks for. Instructor,
location and waitlist are no longer in the bulk result, so `--instructor` and
the instructor/location/waitlist columns trigger a per-class enrichment pass
(one extra request per class, threaded) — see --enrich.

Examples:
  bt.py search --breadths "Philosophy & Values" "Arts & Literature" \
        --enrollment NON_RESERVED_OPEN --units-max 3 --exclude-languages \
        --sort-local grade --format md
  bt.py search --breadths "Social & Behavioral Sciences" --sort AVERAGE_GRADE \
        --min-grade 3.5 --time-from 10:00 --time-to 16:00 --format table
  bt.py search --departments ELENG --instructor "Hug" \
        --fields instructor,code,title,meet,location --format md
  bt.py filter-options
  bt.py ops --show GetCatalogSearch
  bt.py raw --op GetTerms
"""
import argparse, json, os, sys, ssl, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import persisted

ENDPOINT = "https://berkeleytime.com/api/graphql"

# Some python.org builds ship without system CA certs. Try verified first, then
# fall back to an unverified context (fine for this public, read-only API).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()
_SSL_CTX_UNVERIFIED = ssl._create_unverified_context()

# ---- persisted operation names used below ------------------------------------
# These are the web app's own documents; `ops --show NAME` prints their exact
# selection sets. Fields the app does not select are simply not obtainable in
# bulk any more (see the NOTE in the module docstring).
OP_SEARCH   = "GetCatalogSearch"      # catalogSearch: no instructors/location/waitlist
OP_TERMS    = "GetTerms"
OP_FILTERS  = "GetCatalogFilterOptions"
OP_DETAILS  = "GetClassDetails"       # per class: primarySection meetings + enrollment
OP_REQS     = "GetCourseRequirements" # per course: requirement designations / attributes
OP_COURSE_GRADES = "GetCourseGradeDist"
OP_GRADE_DIST    = "GetGradeDistribution"

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
    d = gql(OP_TERMS)
    terms = d.get("terms") or []
    if not terms:
        raise SystemExit("Could not determine the latest term (terms query returned none).")
    uniq = {(t["year"], t["semester"]) for t in terms}
    ordered = sorted(uniq, key=lambda t: (t[0], _TERM_RANK.get(t[1], 0)), reverse=True)
    for year, semester in ordered:
        r = gql(OP_SEARCH, {"year": year, "semester": semester,
                            "page": 1, "pageSize": 1})
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


DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def http_post_json(url, body, headers, timeout=60):
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):  # cert store missing → retry unverified
            return urllib.request.urlopen(req, timeout=timeout,
                                          context=_SSL_CTX_UNVERIFIED).read().decode("utf-8", "replace")
        raise


def gql(op, variables=None, timeout=60, _refreshed=False):
    """Run one persisted operation by name, posting {id, variables}."""
    man = persisted.load_manifest()
    entry = man["ops"].get(op)
    if entry is None:
        if not _refreshed:
            persisted.load_manifest(refresh=True)
            return gql(op, variables, timeout, _refreshed=True)
        raise SystemExit(f"No persisted operation named {op!r}. Try: bt.py ops")
    payload = {"id": entry["id"]}
    if variables is not None:
        payload["variables"] = variables
    raw = http_post_json(ENDPOINT, json.dumps(payload).encode(), {
        "content-type": "application/json",
        # Cloudflare rejects the default python-urllib UA with an empty body.
        "user-agent": DEFAULT_UA,
        "accept": "application/json"}, timeout)
    d = json.loads(raw, strict=False)  # strict=False: some titles carry raw control chars
    if d.get("error") and not _refreshed:
        # Stale manifest (the site redeployed and every id rotated) — rebuild once.
        persisted.load_manifest(refresh=True)
        return gql(op, variables, timeout, _refreshed=True)
    if d.get("error"):
        raise SystemExit(f"API rejected {op}: {d['error']}")
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
        d = gql(OP_SEARCH, {
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


ENRICH_FIELDS = {"instructor", "location", "waitlist"}
COURSE_FIELDS = {"breadths", "univ_reqs"}


def _class_key_of(r):
    return {"year": r["year"], "semester": r["semester"], "sessionId": r.get("sessionId") or "1",
            "subject": r["subject"], "courseNumber": r["courseNumber"], "number": r["number"]}


def _thread_map(fn, items, workers=12):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def enrich_rows(rows, a):
    """Refill instructor / location / waitlist, one persisted call per class.

    catalogSearch used to carry meetings.instructors, meetings.location and the
    waitlist counts; the app's persisted document drops them, so the only route
    left is the per-class document. That is one request per class, so it is
    opt-in (auto-enabled when those columns or --instructor are asked for) and
    capped by --enrich-max.
    """
    if len(rows) > a.enrich_max:
        sys.stderr.write(
            f"[enrich] {len(rows)} classes exceeds --enrich-max {a.enrich_max}; "
            "narrow the search (e.g. --departments) or raise the cap. "
            "Instructor/location/waitlist will be blank.\n")
        return rows
    sys.stderr.write(f"[enrich] fetching instructors/location for {len(rows)} classes...\n")

    def one(r):
        try:
            d = gql(OP_DETAILS, _class_key_of(r), timeout=30).get("class") or {}
        except Exception:
            return
        ps = d.get("primarySection") or {}
        meets = ps.get("meetings") or []
        if meets:
            r["meetings"] = meets          # same shape, now with instructors + location
        latest = ((ps.get("enrollment") or {}).get("latest") or {})
        r["waitlistedCount"] = latest.get("waitlistedCount")
        r["maxWaitlist"] = latest.get("maxWaitlist")

    _thread_map(one, rows)
    return rows


# GE section-attribute values that are university requirements rather than L&S
# breadths (catalogFilterOptions lists them under both, but only these belong in
# univ_reqs): American Cultures, American History/Institutions, R&C, ELW.
_UNIV_REQ_CODES = {"AC", "AH", "AI", "AHI", "RCA", "RCB", "ELW", "ELWR"}


def enrich_requirements(rows, a):
    """Refill breadths / university requirements, one call per distinct course."""
    courses = sorted({(r["subject"], r["courseNumber"]) for r in rows})
    if len(courses) > a.enrich_max:
        sys.stderr.write(f"[enrich] {len(courses)} courses exceeds --enrich-max "
                         f"{a.enrich_max}; breadths/univ_reqs will be blank.\n")
        return rows
    sys.stderr.write(f"[enrich] fetching requirements for {len(courses)} courses...\n")
    found = {}

    def one(key):
        subject, number = key
        try:
            d = gql(OP_REQS, {"subject": subject, "number": number}, timeout=30)
        except Exception:
            return
        mrc = ((d.get("course") or {}).get("mostRecentClass") or {})
        breadths, univ = set(), set()
        rd = mrc.get("requirementDesignation") or {}
        if rd.get("description"):
            univ.add(rd["description"])
        for sa in ((mrc.get("primarySection") or {}).get("sectionAttributes") or []):
            # only the GE attribute carries breadth / requirement designations;
            # the rest are course level, instruction type, unit rules, notes.
            if ((sa.get("attribute") or {}).get("code") or "") != "GE":
                continue
            val = sa.get("value") or {}
            desc, code = val.get("description"), (val.get("code") or "").upper()
            if not desc:
                continue
            (univ if code in _UNIV_REQ_CODES else breadths).add(desc)
        found[key] = (breadths, univ)

    _thread_map(one, courses)
    for r in rows:
        b, u = found.get((r["subject"], r["courseNumber"]), (set(), set()))
        r["breadthRequirements"] = sorted(b)
        r["universityRequirements"] = sorted(u)
    return rows


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
                 "waitlisted": 0, "maxWaitlist": 0, "status_set": set(), "online_flags": set(),
                 "breadth_set": set(), "univ_req_set": set()}
            by_code[k] = e
        e["open"] += (r.get("maxEnroll") or 0) - (r.get("enrolledCount") or 0)
        e["cap"] += (r.get("maxEnroll") or 0)
        e["sections"] += 1
        e["waitlisted"] += r.get("waitlistedCount") or 0
        e["maxWaitlist"] += r.get("maxWaitlist") or 0
        if r.get("enrollmentStatus"):
            e["status_set"].add(r["enrollmentStatus"])
        e["breadth_set"].update(r.get("breadthRequirements") or [])
        e["univ_req_set"].update(r.get("universityRequirements") or [])
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
            m["breadth_set"] |= e["breadth_set"]
            m["univ_req_set"] |= e["univ_req_set"]
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
        e["breadths"] = ", ".join(sorted(e["breadth_set"])) or "—"
        e["univ_reqs"] = ", ".join(sorted(e["univ_req_set"])) or "—"
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
    wanted = {c.strip() for c in (a.fields or "").split(",") if c.strip()}
    if rows and not a.no_enrich:
        if a.instructor or (wanted & ENRICH_FIELDS):
            enrich_rows(rows, a)
        if wanted & COURSE_FIELDS:
            enrich_requirements(rows, a)
    items = consolidate(rows, a)
    output(items, a)


# --------------------------- filter-options -----------------------------------
def cmd_filter_options(a):
    d = gql(OP_FILTERS, {"year": a.year, "semester": a.semester}).get("catalogFilterOptions") or {}
    for k in ("levels", "breadthRequirements", "universityRequirements", "gradingOptions"):
        v = d.get(k) or []
        print(f"\n{k} ({len(v)}):")
        for x in v:
            print(f"  {x}")
    tr = d.get("timeRange") or {}
    if tr:
        print(f"\ntimeRange: {tr.get('minStartTime')} .. {tr.get('maxEndTime')}")
    # The app's filter-options document no longer selects departments, so the
    # valid --departments values can't be listed. They are still accepted as a
    # server-side filter; use subject codes seen in search results (e.g. COMPSCI).
    print("\ndepartments: not exposed by the persisted filter-options document; "
          "--departments still works, pass subject codes seen in search output.")


# ------------------------------- grades ---------------------------------------
def class_key(a):
    return {"year": a.year, "semester": a.semester, "sessionId": a.session,
            "subject": a.subject, "courseNumber": a.course_number, "number": a.number}


def cmd_grades(a):
    # Course-level distribution (the persisted class-level document carries no
    # pnpPercentage; GetGradeDistribution does, keyed by courseId).
    c = (gql(OP_COURSE_GRADES, {"subject": a.subject,
                                "number": a.course_number}).get("course") or {})
    gd = c.get("gradeDistribution") or {}
    pnp = None
    if c.get("courseId"):
        g = gql(OP_GRADE_DIST, {"subject": a.subject, "courseId": c["courseId"],
                                "year": None, "semester": None, "sessionId": None,
                                "classNumber": None, "familyName": None,
                                "givenName": None}).get("grade") or {}
        pnp = g.get("pnpPercentage")
        gd = g or gd
    print(f"average={gd.get('average')}  pnpPercentage={pnp if pnp is not None else gd.get('pnpPercentage')}")
    for row in (gd.get("distribution") or []):
        print(f"  {row['letter']:>3}: {row['count']}")


def cmd_details(a):
    d = gql(OP_DETAILS, class_key(a)).get("class") or {}
    print(json.dumps(d, indent=2))


# ----------------------------- persisted ops ----------------------------------
def cmd_ops(a):
    man = persisted.load_manifest(refresh=a.refresh)
    if a.show:
        entry = man["ops"].get(a.show)
        if not entry:
            raise SystemExit(f"No persisted operation named {a.show!r}.")
        print(f"# id: {entry['id']}\n")
        print(entry["source"])
        return
    names = sorted(man["ops"])
    if a.grep:
        needle = a.grep.lower()
        names = [n for n in names
                 if needle in n.lower() or needle in man["ops"][n]["source"].lower()]
    print(f"bundle {man['bundle']}  ({len(names)} operations)")
    for n in names:
        print("  " + n)


# ----------------------------- introspect (FALLBACK) --------------------------
# ------------------------- RateMyProfessors lookup -----------------------------
# Minimal port of the professor-search idea from tisuela/ratemyprof-api: given a
# name, find the matching professor and surface their rating + profile link. That
# repo's endpoints (ratemyprofessors.com/filter/professor, /paginate/professors/
# ratings) are dead — RMP moved to a GraphQL API — so this hits RMP's own public
# GraphQL endpoint instead, using the fixed "test:test" Basic auth every browser
# sends (baked into RMP's client JS, not a real credential; no login/paywall
# bypass involved — same public rating data is on ratemyprofessors.com).
RMP_ENDPOINT = "https://www.ratemyprofessors.com/graphql"
RMP_AUTH_HEADER = "Basic dGVzdDp0ZXN0"  # base64("test:test")
RMP_UCB_NUMERIC_ID = "1072"  # ratemyprofessors.com/campusRatings.jsp?sid=1072

RMP_SEARCH_QUERY = """
query BtRmpSearch($text:String!,$schoolID:ID!){
  newSearch{
    teachers(query:{text:$text, schoolID:$schoolID}){
      edges{ node{
        legacyId firstName lastName department
        avgRating avgDifficulty numRatings wouldTakeAgainPercent
      } }
    }
  }
}"""


def rmp_school_gid(numeric_id):
    import base64
    return base64.b64encode(f"School-{numeric_id}".encode()).decode()


def rmp_gql(query, variables, timeout=30):
    body = json.dumps({"query": query, "variables": variables}).encode()
    raw = http_post_json(RMP_ENDPOINT, body, {
        "content-type": "application/json",
        "authorization": RMP_AUTH_HEADER,
        "user-agent": DEFAULT_UA,
        "accept": "application/json"}, timeout)
    d = json.loads(raw, strict=False)
    if d.get("errors"):
        sys.stderr.write("RMP GraphQL errors: " + json.dumps(d["errors"], indent=2) + "\n")
    return d.get("data") or {}


def cmd_rmp(a):
    school_gid = rmp_school_gid(a.school_id)
    d = rmp_gql(RMP_SEARCH_QUERY, {"text": a.name, "schoolID": school_gid})
    edges = (((d.get("newSearch") or {}).get("teachers") or {}).get("edges") or [])
    if not edges:
        print(f"No RateMyProfessors match for {a.name!r}.")
        sys.stderr.write("\n[0 rows]\n")
        return
    rows = []
    for e in edges:
        n = e["node"]
        rating, difficulty, wta = n.get("avgRating"), n.get("avgDifficulty"), n.get("wouldTakeAgainPercent")
        rows.append({
            "name": f'{n.get("firstName","")} {n.get("lastName","")}'.strip(),
            "department": n.get("department") or "—",
            "rating": f'{rating:.1f}/5' if rating is not None else "—",
            "difficulty": f'{difficulty:.1f}/5' if difficulty is not None else "—",
            "num_ratings": n.get("numRatings") or 0,
            "would_take_again": f'{wta:.0f}%' if wta is not None and wta >= 0 else "—",
            "link": f'https://www.ratemyprofessors.com/professor/{n["legacyId"]}',
        })
    cols = ["name", "department", "rating", "difficulty", "num_ratings", "would_take_again", "link"]
    if a.format == "json":
        print(json.dumps(rows, indent=2))
    elif a.format == "md":
        print("| " + " | ".join(cols) + " |")
        print("|" + "|".join("---" for _ in cols) + "|")
        for r in rows:
            print("| " + " | ".join(str(r[c]).replace("|", "\\|") for c in cols) + " |")
    else:
        widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
        print("  ".join(c.ljust(widths[c]) for c in cols))
        print("  ".join("-" * widths[c] for c in cols))
        for r in rows:
            print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    sys.stderr.write(f"\n[{len(rows)} rows]\n")


# -------------------------------- raw -----------------------------------------
def cmd_raw(a):
    variables = json.loads(a.vars) if a.vars else None
    print(json.dumps(gql(a.op, variables), indent=2))


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
    s.add_argument("--enrich-max", type=int, default=400,
                   help="max classes/courses to enrich with per-class lookups (default 400)")
    s.add_argument("--no-enrich", action="store_true",
                   help="skip per-class enrichment (instructor/location/waitlist stay blank)")
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

    op = sub.add_parser("ops", help="list/show the persisted operations the API accepts")
    op.add_argument("--show", help="print one operation's id and full GraphQL source")
    op.add_argument("--grep", help="filter the list by name or source text")
    op.add_argument("--refresh", action="store_true", help="rebuild the manifest from the live site")
    op.set_defaults(func=cmd_ops)

    r = sub.add_parser("raw", help="run one persisted operation by name")
    r.add_argument("--op", required=True, help="operation name (see: bt.py ops)")
    r.add_argument("--vars", help="JSON variables")
    r.set_defaults(func=cmd_raw)

    rmp = sub.add_parser("rmp", help="RateMyProfessors rating + profile link")
    rmp.add_argument("--name", required=True, help='professor name to search, e.g. "Paul Hilfinger"')
    rmp.add_argument("--school-id", default=RMP_UCB_NUMERIC_ID,
                     help="RMP numeric school ID (default: UC Berkeley, 1072)")
    rmp.add_argument("--format", default="table", choices=["table", "md", "json"])
    rmp.set_defaults(func=cmd_rmp)

    a = p.parse_args()
    if hasattr(a, "year"):
        resolve_term(a)
    a.func(a)


if __name__ == "__main__":
    main()
