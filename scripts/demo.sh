#!/usr/bin/env bash
# End-to-end walkthrough against a running stack. Usage: BASE_URL=http://localhost:8000 ./scripts/demo.sh
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8000}"
ADMIN="${GATEWAY_ADMIN_API_KEY:-demo-admin-key-change-me-0000}"

chat() {
  curl -s -X POST "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -H "X-API-Key: ${1:-demo-live-key}" \
    -d "{\"model\":\"${2:-balanced}\",\"messages\":[{\"role\":\"user\",\"content\":\"Explain circuit breakers.\"}]}"
  echo
}
admin() { curl -s -X "$1" "$BASE$2" -H "X-Admin-Key: $ADMIN" "${@:3}"; echo; }

echo "== 1. normal request";                 chat
echo "== 2. primary outage -> failover";      admin POST "/v1/admin/providers/mock-primary/simulate-outage?enabled=true"
for _ in 1 2 3; do chat; done
admin GET /v1/admin/providers
echo "== 3. restore + wait for half-open";    admin POST "/v1/admin/providers/mock-primary/simulate-outage?enabled=false"
sleep 16; chat
echo "== 4. rate limit (search team)"
for _ in $(seq 1 40); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
    -H 'X-API-Key: search-live-key' -d '{"model":"economy","messages":[{"role":"user","content":"hi"}]}'
done | sort | uniq -c
echo "== 5. budget status";                   admin GET /v1/admin/teams/demo/status
