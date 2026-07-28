#!/usr/bin/env bash
# Install the berkeleytime Claude skill.
#
# From a checkout (defaults to the current project):
#   ./install.sh                # -> ./.claude/skills/berkeleytime
#   ./install.sh --personal     # -> ~/.claude/skills/berkeleytime
#   ./install.sh --to DIR       # -> DIR/berkeleytime
#
# Without a checkout, piped from curl (defaults to a global install):
#   curl -fsSL https://raw.githubusercontent.com/prathamrn/berkeleytime-skill/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --project
#
# Re-running overwrites the previous copy. Set BT_REF=<branch|tag> to install a
# ref other than main.
set -euo pipefail

REPO="prathamrn/berkeleytime-skill"
REF="${BT_REF:-main}"

usage() {
  cat <<'EOF'
Install the berkeleytime Claude skill.

From a checkout (defaults to the current project):
  ./install.sh              -> ./.claude/skills/berkeleytime
  ./install.sh --personal   -> ~/.claude/skills/berkeleytime
  ./install.sh --to DIR     -> DIR/berkeleytime

Piped from curl (defaults to a global install):
  curl -fsSL https://raw.githubusercontent.com/prathamrn/berkeleytime-skill/main/install.sh | bash
  curl -fsSL .../install.sh | bash -s -- --project

Re-running overwrites the previous copy. Set BT_REF=<branch|tag> to install a
ref other than main.
EOF
}

# A real checkout has SKILL.md sitting next to this script; when piped from curl
# there is no source directory and we fetch the repo instead.
SRC=""
_self="${BASH_SOURCE[0]:-}"
if [ -n "$_self" ] && [ -f "$(dirname "$_self")/SKILL.md" ]; then
  SRC="$(cd "$(dirname "$_self")" && pwd)"
fi

if [ -n "$SRC" ]; then
  DEST_ROOT="$PWD/.claude/skills"      # checkout -> project-local
else
  DEST_ROOT="$HOME/.claude/skills"     # curl | bash -> global
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --project)          DEST_ROOT="$PWD/.claude/skills"; shift ;;
    --personal|--user|--global)
                        DEST_ROOT="$HOME/.claude/skills"; shift ;;
    --to)               DEST_ROOT="${2:?--to needs a directory}"; shift 2 ;;
    -h|--help)          usage; exit 0 ;;
    *)                  echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null || { echo "error: python3 not found (need Python 3.8+)" >&2; exit 1; }

TMP=""
cleanup() { [ -n "$TMP" ] && rm -rf "$TMP"; return 0; }  # keep the EXIT trap from setting a nonzero status
trap cleanup EXIT

if [ -z "$SRC" ]; then
  command -v curl >/dev/null || { echo "error: curl not found" >&2; exit 1; }
  echo "Fetching $REPO@$REF ..."
  TMP="$(mktemp -d)"
  mkdir -p "$TMP/src"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" \
    | tar -xzf - -C "$TMP/src" --strip-components=1 \
    || { echo "error: could not download $REPO@$REF" >&2; exit 1; }
  SRC="$TMP/src"
  [ -f "$SRC/SKILL.md" ] || { echo "error: downloaded archive has no SKILL.md" >&2; exit 1; }
fi

DEST_ROOT="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$DEST_ROOT")"
DEST="$DEST_ROOT/berkeleytime"

# Installing inside the source checkout would nest a copy of the skill in its own
# repo — almost never what you want. Check before creating any directories.
case "$DEST_ROOT/" in
  "$SRC"/*) echo "error: destination is inside the source checkout ($DEST)." >&2
            echo "       cd to the project you want the skill in, or use --personal / --to DIR." >&2
            exit 1 ;;
esac

rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$SRC/SKILL.md" "$SRC/README.md" "$SRC/scripts" "$SRC/references" "$DEST/"
chmod +x "$DEST/scripts/bt.py"

echo "Installed berkeleytime -> $DEST"
echo
echo "Smoke test:"
if python3 "$DEST/scripts/bt.py" filter-options >/dev/null 2>&1; then
  echo "  API reachable, skill ready. Start a Claude session and ask about Berkeley classes."
else
  echo "  WARNING: could not reach https://berkeleytime.com/api/graphql (check your network)."
fi
