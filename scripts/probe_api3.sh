#!/usr/bin/env bash
# =============================================================================
# Yandex Wiki API — read-only probe, pass 3 (extras not covered before)
#   - /pages/{id}/resources combined endpoint (+ q, + types) — used by APonkratov
#   - /search body filters for result type (type / types / only_pages)
# READ-ONLY. Usage: bash probe_api3.sh [path-to-secrets.env]
#   Secrets: WIKI_TOKEN + WIKI_ORG_ID (or YANDEX_*) via env or an env file ($1 / $SECRETS).
#   Pages: PROBE_PAGE_SLUG (default: homepage) — a page with attachments for section A.
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

PAGE_SLUG="${PROBE_PAGE_SLUG:-homepage}"
resolve_id() { curl -sS -m 20 -H "$A" -H "$O" "$B/pages?slug=$1" | jq -r '.id // empty'; }
PID_PRED="$(resolve_id "$PAGE_SLUG")"; : "${PID_PRED:?cannot resolve PROBE_PAGE_SLUG '$PAGE_SLUG'}"

echo "############ A. /pages/{id}/resources (combined attachments+grids) ############"
curl -sS -m20 -H "$A" -H "$O" -o "$D/ep_resources.json" -w "  resources          HTTP %{http_code}\n" "$B/pages/$PID_PRED/resources?page_size=10"
echo "     top keys: $(jqv 'keys|join(",")' "$D/ep_resources.json")"
echo "     result[0]: $(jq -c '.results[0]' "$D/ep_resources.json" 2>/dev/null | head -c 300)"
echo "     distinct types: $(jqv '[.results[].type]|unique|join(",")' "$D/ep_resources.json")"
curl -sS -m20 -H "$A" -H "$O" -o "$D/ep_resources_q.json" -w "  resources?q=image  HTTP %{http_code}\n" "$B/pages/$PID_PRED/resources?types=attachment&q=image&page_size=10"
echo "     q-filtered count: $(jqv '.results|length' "$D/ep_resources_q.json")"

echo; echo "############ B. /search — result-type filter probes ############"
sp() { # name, body
  local f="$D/search_$1.json"
  local code; code=$(curl -sS -m20 -H "$A" -H "$O" -H "$CT" -X POST "$B/search" -d "$2" -o "$f" -w "%{http_code}")
  printf "  %-16s HTTP %s  n=%-3s types=%s\n" "$1" "$code" "$(jqv '.results|length' "$f")" "$(jqv '[.results[].type]|unique|join("+")' "$f")"
}
sp baseline    "$(jq -cn '{query:"таблица",page_size:30}')"
sp type_page   "$(jq -cn '{query:"таблица",page_size:30,type:"page"}')"
sp types_page  "$(jq -cn '{query:"таблица",page_size:30,types:["page"]}')"
sp only_pages  "$(jq -cn '{query:"таблица",page_size:30,only_pages:true}')"
sp type_file   "$(jq -cn '{query:"таблица",page_size:30,type:"file"}')"

echo; echo "  md5 of results[] (identical to baseline => filter ignored):"
for f in baseline type_page types_page only_pages type_file; do
  printf "    %-12s %s\n" "$f" "$(jq -cS '[.results[].slug]' "$D/search_$f.json" 2>/dev/null | md5sum | cut -d' ' -f1)"
done

echo; echo "############ DONE (pass 3) ############"
