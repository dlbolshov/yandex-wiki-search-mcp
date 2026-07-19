#!/usr/bin/env bash
# =============================================================================
# Yandex Wiki API — read-only probe, pass 2 (corrections & precise values)
# Follows up probe_api.sh. READ-ONLY (GET + POST /search).
#   - exact page_size ceiling (binary search)
#   - access_policy / owner optional-field shapes
#   - confirm section-scoping params are ignored (md5 diff)
#   - corrected page_type histogram (page_type is a DEFAULT field, not in `fields`)
#   - extract the type:"file" search-result shape
# Usage: bash probe_api2.sh [path-to-secrets.env]
#   Secrets: WIKI_TOKEN + WIKI_ORG_ID (or YANDEX_*) via env or an env file ($1 / $SECRETS).
#   Pages: PROBE_PAGE_SLUG / PROBE_SECTION_SLUG (defaults: homepage) — see probe_api.sh.
# =============================================================================
set -u

SECRETS="${1:-${SECRETS:-}}"
if [ -n "$SECRETS" ]; then
  # shellcheck disable=SC1090
  source "$SECRETS"
fi
TOKEN="${WIKI_TOKEN:-${YANDEX_WIKI_TOKEN:-}}"
ORG="${WIKI_ORG_ID:-${YANDEX_ORG_ID:-}}"
: "${TOKEN:?export WIKI_TOKEN (or YANDEX_WIKI_TOKEN) directly or via a secrets env file}"
: "${ORG:?export WIKI_ORG_ID (or YANDEX_ORG_ID) directly or via a secrets env file}"
A="Authorization: OAuth $TOKEN"; O="X-Org-Id: $ORG"; CT="Content-Type: application/json"
B="https://api.wiki.yandex.net/v1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; D="$HERE/raw"; mkdir -p "$D"
jqv() { jq -r "$1" "$2" 2>/dev/null; }

PAGE_SLUG="${PROBE_PAGE_SLUG:-homepage}"; SECTION_SLUG="${PROBE_SECTION_SLUG:-homepage}"
resolve_id() { curl -sS -m 20 -H "$A" -H "$O" "$B/pages?slug=$1" | jq -r '.id // empty'; }
PID_PRED="$(resolve_id "$PAGE_SLUG")";    : "${PID_PRED:?cannot resolve PROBE_PAGE_SLUG '$PAGE_SLUG'}"
PID_ROOT="$(resolve_id "$SECTION_SLUG")"; : "${PID_ROOT:?cannot resolve PROBE_SECTION_SLUG '$SECTION_SLUG'}"

echo "############ A. exact page_size ceiling (binary search 50..100) ############"
lo=50; hi=100
while [ $((hi-lo)) -gt 1 ]; do
  mid=$(((lo+hi)/2))
  code=$(curl -sS -m 20 -H "$A" -H "$O" -H "$CT" -X POST "$B/search" \
    -d "$(jq -cn --argjson n "$mid" '{query:"ML",page_size:$n}')" -o /dev/null -w "%{http_code}")
  echo "  page_size=$mid -> HTTP $code"
  if [ "$code" = "200" ]; then lo=$mid; else hi=$mid; fi
done
echo "  => MAX page_size = $lo (first rejected = $hi)"

echo; echo "############ B. optional fields access_policy / owner ############"
for fl in access_policy owner; do
  curl -sS -m 20 -H "$A" -H "$O" -o "$D/field_$fl.json" -w "  $fl -> HTTP %{http_code}\n" "$B/pages/$PID_PRED?fields=$fl"
  echo "     body: $(head -c 400 "$D/field_$fl.json")"
done

echo; echo "############ C. section-scoping params are ignored? (md5 diff) ############"
curl -sS -m 20 -H "$A" -H "$O" -H "$CT" -X POST "$B/search" \
  -d "$(jq -cn '{query:"тест"}')" -o "$D/search_scope_none.json"
echo "  baseline (no scope) first slug: $(jqv '.results[0].slug' "$D/search_scope_none.json")"
echo "  md5 of results[] across baseline + scope variants (identical hash => param ignored):"
for f in scope_none scope_slug scope_cluster scope_path scope_parent scope_folder scope_section scope_filter_obj; do
  h=$(jq -cS '.results' "$D/search_$f.json" 2>/dev/null | md5sum | cut -d' ' -f1)
  printf "    %-18s %s\n" "$f" "$h"
done

echo; echo "############ D. corrected page_type histogram (no fields param) ############"
curl -sS -m 40 -H "$A" -H "$O" "$B/pages/$PID_ROOT/descendants?page_size=95" -o "$D/desc_root95.json"
ids=$(jqv '.results[].id' "$D/desc_root95.json" | head -n 40)
declare -A TYPES; grid_id=""; n=0
for id in $ids; do
  n=$((n+1))
  curl -sS -m 20 -H "$A" -H "$O" "$B/pages/$id" -o "$D/_p.json"
  pt=$(jqv '.page_type' "$D/_p.json")
  TYPES["$pt"]=$(( ${TYPES["$pt"]:-0} + 1 ))
  if [ -z "$grid_id" ] && printf '%s' "$pt" | grep -qi grid; then grid_id="$id"; fi
done
echo "  page_type histogram over $n pages:"
for k in "${!TYPES[@]}"; do printf "    %-16s %s\n" "$k" "${TYPES[$k]}"; done
echo "  first grid page id: ${grid_id:-<none in sample>}"
if [ -n "$grid_id" ]; then
  curl -sS -m 20 -H "$A" -H "$O" "$B/pages/$grid_id/grids" -o "$D/grid_grids.json"
  echo "  grids listing: $(head -c 500 "$D/grid_grids.json")"
fi

echo; echo "############ E. search type:\"file\" result shape ############"
echo "  file-typed result: $(jq -c '[.results[]|select(.type=="file")][0]' "$D/search_types_probe.json" 2>/dev/null | head -c 500)"

echo; echo "############ DONE (pass 2) ############"
