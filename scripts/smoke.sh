#!/usr/bin/env bash
# =============================================================================
# Live smoke test for yandex-wiki-search-mcp (run AFTER Devin's PR is merged/built).
#
# Speaks MCP JSON-RPC over stdio to a Docker image and exercises:
#   initialize -> tools/list -> page_search -> page_get
# Verifies: page_search is registered, write tools are ABSENT (read-only mode),
# and search returns results against the live Wiki.
#
# Usage:
#   bash smoke.sh [IMAGE] [QUERY] [SLUG]
#     IMAGE  default: $WIKI_MCP_IMAGE or ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest
#     QUERY  default: "документация" (pick any word common in your wiki)
#     SLUG   default: "homepage"
#
# Secrets: export WIKI_TOKEN + WIKI_ORG_ID (or YANDEX_WIKI_TOKEN + YANDEX_ORG_ID) in the
#          environment, or point $SECRETS at an env file that exports them.
#          Nothing is written anywhere.
# =============================================================================
set -u

IMAGE="${1:-${WIKI_MCP_IMAGE:-ghcr.io/dlbolshov/yandex-wiki-search-mcp:latest}}"
QUERY="${2:-документация}"
SLUG="${3:-homepage}"

if [ -n "${SECRETS:-}" ]; then
  # shellcheck disable=SC1090
  source "$SECRETS"
fi
TOKEN="${WIKI_TOKEN:-${YANDEX_WIKI_TOKEN:-}}"
ORG_ID="${WIKI_ORG_ID:-${YANDEX_ORG_ID:-}}"
: "${TOKEN:?export WIKI_TOKEN (or YANDEX_WIKI_TOKEN) directly or via a \$SECRETS env file}"
: "${ORG_ID:?export WIKI_ORG_ID (or YANDEX_ORG_ID) directly or via a \$SECRETS env file}"

REQ=$(cat <<EOF
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"page_search","arguments":{"query":"$QUERY","page_size":5}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"page_get","arguments":{"slug":"$SLUG"}}}
EOF
)

OUT=$(mktemp)
echo ">> image: $IMAGE"
echo ">> query: '$QUERY'   slug: '$SLUG'   (WIKI_READ_ONLY=true)"
( printf '%s\n' "$REQ"; sleep 12 ) | timeout 90 docker run --rm -i \
  -e WIKI_TOKEN="$TOKEN" \
  -e WIKI_ORG_ID="$ORG_ID" \
  -e WIKI_READ_ONLY=true \
  "$IMAGE" > "$OUT" 2>/tmp/smoke_err.log || true

fail=0
tools=$(jq -rc 'select(.id==2)|[.result.tools[].name]' "$OUT" 2>/dev/null)
echo; echo "== tools/list =="; echo "  $tools"

echo "$tools" | grep -q '"page_search"' \
  && echo "  [OK] page_search is registered" \
  || { echo "  [FAIL] page_search missing"; fail=1; }

if echo "$tools" | grep -qE '"(page_create|page_update|page_delete|grid_create|page_add_comment)"'; then
  echo "  [FAIL] write tools present despite WIKI_READ_ONLY=true"; fail=1
else
  echo "  [OK] no write tools (read-only honored)"
fi

echo; echo "== page_search('$QUERY') =="
n=$(jq -r 'select(.id==3)|.result.content[0].text' "$OUT" 2>/dev/null | jq -r '.results|length' 2>/dev/null)
if [ -n "$n" ] && [ "$n" != "null" ]; then
  echo "  [OK] returned $n results:"
  # url is printed to eyeball that page urls got normalized to absolute clickable links
  jq -r 'select(.id==3)|.result.content[0].text' "$OUT" 2>/dev/null | jq -r '.results[]|"    - [\(.type)] \(.title)  (\(.slug))  \(.url // "")"' 2>/dev/null | head -8
else
  echo "  [FAIL] no results parsed"; fail=1
  jq -c 'select(.id==3)' "$OUT" 2>/dev/null | head -c 500
fi

echo; echo "== page_get('$SLUG') =="
title=$(jq -r 'select(.id==4)|.result.content[0].text' "$OUT" 2>/dev/null | jq -r '.title' 2>/dev/null)
[ -n "$title" ] && [ "$title" != "null" ] \
  && echo "  [OK] title: $title" \
  || { echo "  [WARN] page_get returned no title (slug '$SLUG' may not exist in this org)"; }

echo; echo "== stderr tail =="; tail -c 300 /tmp/smoke_err.log
rm -f "$OUT"
echo; [ "$fail" = 0 ] && echo "SMOKE: PASS" || { echo "SMOKE: FAIL"; exit 1; }
