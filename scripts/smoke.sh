#!/usr/bin/env bash
# Smoke-test a running ChainMind instance, local or deployed.
#   ./scripts/smoke.sh                       # localhost:8080
#   ./scripts/smoke.sh https://your-url      # deployed
set -uo pipefail
BASE="${1:-http://localhost:8080}"
pass=0; fail=0
chk() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then printf '  ok   %s\n' "$name"; pass=$((pass+1))
  else printf '  FAIL %s\n' "$name"; fail=$((fail+1)); fi
}
echo "smoke testing ${BASE}"
chk "health"        curl -fsS "${BASE}/health"
chk "readyz"        curl -fsS "${BASE}/readyz"
chk "capabilities"  curl -fsS "${BASE}/api/capabilities"
chk "network"       curl -fsS "${BASE}/api/network"
chk "metrics"       curl -fsS "${BASE}/api/metrics"
chk "dashboard"     curl -fsS "${BASE}/"
chk "openapi"       curl -fsS "${BASE}/openapi.json"

echo
echo "running a real order through the pipeline..."
t0=$(python3 -c 'import time;print(time.time())')
body=$(curl -fsS -X POST "${BASE}/api/orders" -H 'content-type: application/json' \
  -d '{"text":"2 laptops and 5 usb-c cables to Anna Nagar, Chennai, urgent"}') || body=""
t1=$(python3 -c 'import time;print(time.time())')
if [ -n "$body" ]; then
  python3 - "$body" "$t0" "$t1" <<'PY'
import json, sys
d = json.loads(sys.argv[1]); wall = (float(sys.argv[3]) - float(sys.argv[2])) * 1000
s = d.get("shipment") or {}
print(f"  ok={d['ok']}  server={d['total_ms']:.0f}ms  wall={wall:.0f}ms")
print(f"  warehouse={(s.get('warehouse') or {}).get('name')}  carrier={s.get('carrier')}")
print(f"  eta={s.get('eta_minutes')}min  cost=Rs.{s.get('cost_inr')}  "
      f"route={(s.get('route') or {}).get('source')}")
print(f"  models={d.get('models_used')}")
if d.get("degraded"): print(f"  degraded: {d['degraded']}")
PY
  pass=$((pass+1))
else
  echo "  FAIL order pipeline"; fail=$((fail+1))
fi
echo
echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
