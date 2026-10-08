"""Persisted-operation support for the Berkeleytime GraphQL API (stdlib only).

berkeleytime.com/api/graphql no longer accepts arbitrary GraphQL documents: it
answers `{"error":"Invalid persisted operation request"}` to anything that
carries a `query` string. The web client instead posts `{id, variables}`, where
`id` is

    sha256( stripIgnoredCharacters( print( stripTypename( parse(document) ) ) ) )

computed at runtime over its own Apollo documents. Only documents shipped in the
site bundle have a server-side entry, so this module recovers the allowed set by
downloading the bundle, extracting every `gql` tagged template, and recomputing
those ids.

For the documents the app ships (no block strings, no `__typename` in source)
the parse/print/strip round-trip is a no-op modulo ignored characters, so
`sha256(strip_ignored_characters(source))` reproduces the id exactly — verified
against graphql-js 16 for all 73 operations in the bundle.
"""

import hashlib
import json
import os
import re
import time
import urllib.request

SITE = "https://berkeleytime.com"
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persisted-ops.json")
MAX_AGE_SECONDS = 7 * 24 * 3600  # refresh weekly; a site deploy also forces one

_IDENT = re.compile(r"[A-Za-z0-9_$]")
_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_ASSIGN_TAIL = re.compile(r"([A-Za-z0-9_$]+)\s*=\s*$")
_OP_DECL = re.compile(r"(?:^|[}\s])(query|mutation|subscription)\s+([_A-Za-z][_0-9A-Za-z]*)")
_PUNCTUATORS = set("!$&():=@[]{|}")


# --------------------------- graphql normalization ----------------------------
def _lex(src):
    """Yield (is_punctuator, text) for every significant GraphQL token."""
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n﻿,":
            i += 1
            continue
        if c == "#":                                   # comment: ignored entirely
            while i < n and src[i] not in "\r\n":
                i += 1
            continue
        if src.startswith('"""', i):
            # printBlockString(minimize=True) would be needed here; the app ships
            # none, so refuse rather than emit a silently wrong hash.
            raise ValueError("block strings are not supported by this normalizer")
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            yield False, src[i:j + 1]
            i = j + 1
            continue
        if src.startswith("...", i):
            yield True, "..."                          # SPREAD: punctuator, but see below
            i += 3
            continue
        if c in _PUNCTUATORS:
            yield True, c
            i += 1
            continue
        m = _NAME.match(src, i) or _NUMBER.match(src, i)
        if not m:
            raise ValueError(f"unexpected character {c!r} at {i}")
        yield False, m.group(0)
        i = m.end()


def strip_ignored_characters(src):
    """Port of graphql-js `stripIgnoredCharacters`."""
    out = []
    last_was_non_punctuator = False
    for is_punct, text in _lex(src):
        if last_was_non_punctuator and (not is_punct or text == "..."):
            out.append(" ")
        out.append(text)
        last_was_non_punctuator = not is_punct
    return "".join(out)


def operation_id(source):
    return hashlib.sha256(strip_ignored_characters(source).encode()).hexdigest()


# ------------------------------ bundle scraping -------------------------------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"user-agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _tagged_templates(src):
    """Every ``ident`...` `` template literal in a JS source, with its assigned name."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src[i] != "`":
            i += 1
            continue
        j = i - 1
        if j < 0 or not _IDENT.match(src[j]):           # untagged template: skip
            i += 1
            continue
        while j >= 0 and _IDENT.match(src[j]):
            j -= 1
        body, depth, k, closed = [], 0, i + 1, False
        while k < n:
            c = src[k]
            if c == "\\":
                body.append(src[k:k + 2])
                k += 2
                continue
            if depth == 0 and c == "`":
                closed = True
                break
            if c == "$" and k + 1 < n and src[k + 1] == "{":
                depth += 1
            elif depth > 0 and c == "}":
                depth -= 1
            body.append(c)
            k += 1
        if closed:
            m = _ASSIGN_TAIL.search(src[max(0, j - 60):j + 1])
            out.append({"name": m.group(1) if m else None, "body": "".join(body)})
        i = k + 1
    return out


def _asset_urls(entry_js):
    """Chunk paths from vite's __vite__mapDeps table in the entry bundle."""
    m = re.search(r"__vite__mapDeps=\(i,m=__vite__mapDeps,d=\(m\.f\|\|\(m\.f=\[(.*?)\]\)\)",
                  entry_js, re.S)
    if not m:
        return []
    return [x for x in re.findall(r'"([^"]+)"', m.group(1)) if x.endswith(".js")]


def build_manifest():
    """Download the live site bundle and recompute every persisted operation id."""
    index_html = _get(SITE + "/")
    m = re.search(r'src="(/assets/index-[^"]+\.js)"', index_html)
    if not m:
        raise SystemExit("Could not find the entry bundle in berkeleytime.com HTML.")
    entry = m.group(1)
    entry_js = _get(SITE + entry)

    sources = [entry_js]
    for path in _asset_urls(entry_js):
        try:
            sources.append(_get(f"{SITE}/{path.lstrip('/')}"))
        except Exception:
            continue                                    # a stale dep name is not fatal

    by_name, docs = {}, []
    for src in sources:
        for t in _tagged_templates(src):
            if not re.search(r"\b(query|mutation|subscription|fragment)\s+[A-Za-z]", t["body"]):
                continue
            docs.append(t)
            if t["name"]:
                by_name[t["name"]] = t

    def compose(t, seen=None):
        """graphql-tag semantics: interpolated fragment documents are appended."""
        seen = seen if seen is not None else set()
        parts = []

        def take(match):
            ident = match.group(1)
            dep = by_name.get(ident)
            if dep is None:
                raise KeyError(ident)
            if ident not in seen:
                seen.add(ident)
                parts.append(compose(dep, seen))
            return ""

        head = re.sub(r"\$\{([A-Za-z0-9_$]+)\}", take, t["body"])
        return "\n".join([head, *parts])

    ops = {}
    for t in docs:
        try:
            text = compose(t)
            oid = operation_id(text)
        except (KeyError, ValueError):
            continue
        for _, name in _OP_DECL.findall(text):
            ops[name] = {"id": oid, "source": text.strip()}
    if "GetCatalogSearch" not in ops:
        raise SystemExit("Bundle scrape produced no GetCatalogSearch operation — site changed?")
    return {"bundle": entry, "generated": int(time.time()), "ops": ops}


def save_manifest(man):
    with open(MANIFEST_PATH, "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)


_CACHE = {}


def load_manifest(refresh=False):
    if not refresh and _CACHE.get("man"):
        return _CACHE["man"]
    man = None
    if not refresh and os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH) as fh:
                man = json.load(fh)
            if time.time() - man.get("generated", 0) > MAX_AGE_SECONDS:
                man = None
        except Exception:
            man = None
    if man is None:
        man = build_manifest()
        save_manifest(man)
    _CACHE["man"] = man
    return man
