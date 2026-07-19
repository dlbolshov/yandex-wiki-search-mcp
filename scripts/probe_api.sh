#!/usr/bin/env bash
# =============================================================================
# Yandex Wiki API — read-only exploration probe
# Step 1 of the yandex-wiki-search-mcp handoff plan.
#
# ALL requests are strictly READ-ONLY: GET + POST /search only.
# No create / update / delete. Safe to run against a production org.
#
# Usage:  bash probe_api.sh [path-to-secrets.env]
#   Secrets: export WIKI_TOKEN + WIKI_ORG_ID (or YANDEX_WIKI_TOKEN + YANDEX_ORG_ID) in the
#   environment, or pass an env file as $1 (or via $SECRETS).
#   Pages to probe (set to content-rich pages of YOUR wiki for meaningful output):
#     PROBE_PAGE_SLUG    — a page with attachments/comments   (default: homepage)
#     PROBE_SECTION_SLUG — a section with many descendants    (default: homepage)
#
# Output: raw JSON responses saved to ./raw/ (contains YOUR org data — do not commit),
#         human summary to stdout.
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
A="Authorization: OAuth $TOKEN"
O="X-Org-Id: $ORG"
CT="Content-Type: application/json"
B="https://api.wiki.yandex.net/v1"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
D="$HERE/raw"
mkdir -p "$D"

jqv() { jq -r "$1" "$2" 2>/dev/null; }

# Resolve probe page ids from slugs (no hardcoded org-specific ids)
PAGE_SLUG="${PROBE_PAGE_SLUG:-homepage}"
SECTION_SLUG="${PROBE_SECTION_SLUG:-homepage}"
resolve_id() { curl -sS -m 20 -H "$A" -H "$O" "$B/pages?slug=$1" | jq -r '.id // empty'; }
PID_PRED="$(resolve_id "$PAGE_SLUG")"
: "${PID_PRED:?cannot resolve PROBE_PAGE_SLUG '$PAGE_SLUG' to a page id}"
PID_SECTION="$(resolve_id "$SECTION_SLUG")"
: "${PID_SECTION:?cannot resolve PROBE_SECTION_SLUG '$SECTION_SLUG' to a page id}"
PID_HOME="$(resolve_id homepage)"; [ -n "$PID_HOME" ] || PID_HOME="$PID_PRED"
echo "probing: page='$PAGE_SLUG' (id=$PID_PRED), section='$SECTION_SLUG' (id=$PID_SECTION)"

# GET probe: name, path, [extra curl args...]
get_probe() {
  local name="$1"; local path="$2"; shift 2
  local f="$D/$name.json"; local code
  code=$(curl -sS -m 30 -H "$A" -H "$O" "$@" -o "$f" -w "%{http_code}" "$B$path")
  printf "%-26s GET  %-46s HTTP %s\n" "$name" "$path" "$code"
}

# SEARCH probe: name, json-body
search_probe() {
  local name="$1"; local body="$2"
  local f="$D/search_$name.json"; local code
  code=$(curl -sS -m 30 -H "$A" -H "$O" -H "$CT" -X POST "$B/search" -d "$body" -o "$f" -w "%{http_code}")
  printf "%-26s POST /search HTTP %s  n=%-4s total_documents=%-5s total_pages=%s\n" \
    "$name" "$code" "$(jqv '.results|length' "$f")" "$(jqv '.total_documents' "$f")" "$(jqv '.total_pages' "$f")"
}

mkq() { jq -cn --arg q "$1" '{query:$q}'; }

echo "############ 1. /search — page_size ceiling ############"
search_probe ps10    "$(jq -cn '{query:"ML",page_size:10}')"
search_probe ps50    "$(jq -cn '{query:"ML",page_size:50}')"
search_probe ps100   "$(jq -cn '{query:"ML",page_size:100}')"
search_probe ps500   "$(jq -cn '{query:"ML",page_size:500}')"
search_probe ps1000  "$(jq -cn '{query:"ML",page_size:1000}')"
search_probe ps_zero "$(jq -cn '{query:"ML",page_size:0}')"
search_probe ps_neg  "$(jq -cn '{query:"ML",page_size:-1}')"

echo; echo "############ 2. /search — edge queries ############"
search_probe empty         "$(jq -cn '{query:""}')"
search_probe missing_query '{}'
search_probe single_char   "$(mkq 'a')"
search_probe punct         "$(mkq ';:.,<>?')"
search_probe very_long     "$(jq -cn '{query:("ML "*200)}')"

echo; echo "############ 3. /search — operators (mailsearch backend) ############"
search_probe exact_phrase "$(mkq '"машинное обучение"')"
search_probe minus_word   "$(mkq 'предиктор -клик')"
search_probe or_op        "$(mkq 'предиктор OR сегмент')"
search_probe and_op       "$(mkq 'предиктор AND сегмент')"
search_probe wildcard     "$(mkq 'предиктор*')"

echo; echo "############ 4. /search — section-scoping param probes ############"
for p in slug cluster path parent folder section; do
  search_probe "scope_$p" "$(jq -cn --arg p "$p" --arg s "$SECTION_SLUG" '{query:"тест"} + {($p): $s}')"
done
search_probe scope_filter_obj "$(jq -cn --arg s "$SECTION_SLUG" '{query:"тест",filter:{slug:$s}}')"

echo; echo "############ 5. /search — result shape / type histogram ############"
search_probe types_probe "$(jq -cn '{query:"таблица",page_size:50}')"
echo "  distinct types : $(jqv '[.results[].type]|unique|join(",")' "$D/search_types_probe.json")"
echo "  result keys    : $(jqv '.results[0]|keys|join(",")' "$D/search_types_probe.json")"
echo "  top-level keys : $(jqv 'keys|join(",")' "$D/search_types_probe.json")"

echo; echo "############ 6. GET /pages — fields enum discovery ############"
get_probe fields_invalid "/pages?slug=homepage&fields=__bogus__"
echo "  invalid-field error -> $(head -c 500 "$D/fields_invalid.json")"; echo
for fl in content authors author access access_lists acl created_at created_by modified_at updated_at breadcrumbs redirect attributes tags parent cluster subscribers comments_count is_readonly; do
  get_probe "field_$fl" "/pages/$PID_PRED?fields=$fl"
done

echo; echo "############ 7. Undocumented endpoints (page-scoped, on \$PROBE_PAGE_SLUG) ############"
for ep in revisions history versions backlinks links children descendants comments grids attachments access authors subscribers breadcrumbs tree content export raw; do
  get_probe "ep_${ep}" "/pages/$PID_PRED/$ep"
done

echo; echo "############ 8. Same endpoints on homepage (different content) ############"
for ep in grids attachments comments links; do
  get_probe "home_${ep}" "/pages/$PID_HOME/$ep"
done

echo; echo "############ 9. Top-level endpoint probes ############"
get_probe tl_whoami      "/whoami"
get_probe tl_clusters    "/clusters"
get_probe tl_user        "/user"
get_probe tl_pages_noarg "/pages"
get_probe tl_navigation  "/navigation"
get_probe tl_search_get  "/search"

echo; echo "############ 10. descendants: by-id path vs ?slug= + pagination keys ############"
get_probe desc_by_id     "/pages/$PID_SECTION/descendants"
get_probe desc_by_id_ps  "/pages/$PID_SECTION/descendants?page_size=5"
echo "  by_id top keys : $(jqv 'keys|join(",")' "$D/desc_by_id.json")"
echo "  by_id result[0]: $(jqv '.results[0]|keys|join(",")' "$D/desc_by_id.json")"

echo; echo "############ 11. error semantics ############"
get_probe err_bad_slug  "/pages?slug=this/does/not/exist-zzz-000"
get_probe err_bad_id    "/pages/999999999"
get_probe err_bad_route "/definitely-not-a-real-endpoint"
code=$(curl -sS -m 20 -H "$A" -H "X-Org-Id: 0" -o "$D/err_bad_org.json" -w "%{http_code}" "$B/pages?slug=homepage")
printf "%-26s GET  bad org-id  HTTP %s\n" "err_bad_org" "$code"
code=$(curl -sS -m 20 -H "$O" -o "$D/err_no_auth.json" -w "%{http_code}" "$B/pages?slug=homepage")
printf "%-26s GET  no auth     HTTP %s\n" "err_no_auth" "$code"
for e in err_bad_slug err_bad_id err_bad_route err_bad_org err_no_auth; do
  echo "  $e -> $(head -c 220 "$D/$e.json")"
done

echo; echo "############ 12. response headers (rate-limit / content-type) ############"
curl -sS -m 20 -D "$D/headers_search.txt" -H "$A" -H "$O" -H "$CT" -X POST "$B/search" -d "$(mkq 'ML')" -o /dev/null
echo "-- /search response headers:"; sed -n '1,50p' "$D/headers_search.txt"

echo; echo "############ 13. page_type histogram + locate a real grid (bounded 25) ############"
curl -sS -m 40 -H "$A" -H "$O" "$B/pages/$PID_SECTION/descendants?page_size=25" -o "$D/desc_root.json"
ids=$(jqv '.results[].id' "$D/desc_root.json" | head -n 25)
declare -A TYPES
grid_id=""
n=0
for id in $ids; do
  n=$((n+1))
  curl -sS -m 20 -H "$A" -H "$O" "$B/pages/$id?fields=page_type" -o "$D/_pt.json"
  pt=$(jqv '.page_type' "$D/_pt.json")
  TYPES["$pt"]=$(( ${TYPES["$pt"]:-0} + 1 ))
  if [ -z "$grid_id" ] && printf '%s' "$pt" | grep -qi grid; then grid_id="$id"; fi
done
echo "page_type histogram over $n pages:"
for k in "${!TYPES[@]}"; do printf "  %-16s %s\n" "$k" "${TYPES[$k]}"; done
echo "first grid page id: ${grid_id:-<none in sample>}"
if [ -n "$grid_id" ]; then
  get_probe grid_page  "/pages/$grid_id?fields=content"
  get_probe grid_grids "/pages/$grid_id/grids"
  echo "  grid content head: $(jqv '.content' "$D/grid_page.json" | head -c 300)"
fi

echo; echo "############ DONE — raw responses in $D ############"
ls -1 "$D" | sed 's/^/  /'
