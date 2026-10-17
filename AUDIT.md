# Agent War 2026 — Adversarial Engineering Audit

**Project:** ChainMind — Autonomous Supply-Chain Orchestrator
**Repo:** `/Users/micheal/Documents/Hobby_Proj/AgentWar` (no `.git` — not yet version-controlled)
**Audit date:** 2026-10-17
**Method:** full source read + live execution against real Gemini and Google Maps keys present in `.env`.

---

## 1. Executive verdict

**This is a real, working system — not a mock demo — with two headline-claim defects and a
handful of correctness holes that an adversarial reviewer will find.**

I ran it live: free-text order in → dispatched shipment out in **3.1–5.5 s** over real Gemini
calls and real `computeRouteMatrix` responses. The feedback loop is the strongest part of the
submission: a recorded delivery measurably changed the next plan (ETA 78.9→82.5 min, learned
policy cited by the planner). Self-healing works (correct escalation with exact stock numbers).
Atomic reservation holds under concurrency (no oversell in a 3-way race).

The problems: **the deployed `/mcp` endpoint 500s** (session manager never started — the
"MCP-over-HTTP" README claim fails at runtime), **garbage input dispatches a real shipment**,
**there are zero automated tests** (`tests/` is empty), **`railway.json` is claimed but absent**,
and the API key leaks into logs and responses. All are fixable inside the deadline.

---

## 2. What was actually executed (evidence, not opinion)

| Run | Result |
|---|---|
| `scripts/verify_apis.py` | Gemini key live; maps `geocoding:true, routes_v2:true, address_validation:false` |
| `scripts/e2e.py` (live) | 3/4 orders dispatched; healer correctly escalated the impossible order; feedback loop verified; **copilot silently degraded**; script exited 1 |
| `POST /api/orders` (live, uvicorn) | ok, 4328 ms, `route.source = google-routes-matrix`, no degradation |
| 3× concurrent orders (mock mode) | No oversell; `reserved ≤ on_hand` held; reroute worked |
| `"asdkfj blah zzz no products at all"` | **ok=true, shipment SHP-8BE0A6DCD6 dispatched** — defect |
| MCP stdio client (real JSON-RPC) | 15 tools listed; `fulfil_order` executed a live order |
| `POST /mcp` (mounted HTTP) | 307 → `/mcp/mcp` → **500 `Task group is not initialized. Make sure to use run()`** |
| DB inspection | 24 orders, 18 shipments, 150 spans, 1 learned policy persisted |

Measured live latency: 3099 / 4328 / 4629 / 5471 ms sequential (agent ≈ 2.6–4.3 s of it; DB ≈
1–3 ms). README claims "~2.6–3.7 s" — plausible on a good run, but the honest range is 3–5.5 s;
say "median ~3.3 s, observed 3–5.5 s".

---

## 3. Capability scorecard (0–5, evidence-based)

| Category | Score | Evidence |
|---|---|---|
| End-to-end functionality | **4** | Live order → shipment verified. −1: garbage input also "completes"; QA-reject leaves status `triaged` |
| Execution speed | **4** | 2 LLM calls on happy path, speculative geocode, single matrix call. −1: quota-driven failover adds variance |
| Agent architecture | **4** | 5 real tiers, schema-constrained outputs, code critic, bounded loops, per-tier probing. No agent proliferation |
| Google integrations | **4** | Routes v2 matrix + geocoding verified live; address-validation honestly degrades. −1: key in logs/URLs |
| MCP integration | **2.5** | stdio verified working (15 tools, real order through it); **HTTP mount broken at runtime** |
| Database & concurrency | **4** | Atomic `BEGIN IMMEDIATE` reservation verified under race; WAL, indexes. −1: `record_delivery` check-then-act race |
| Feedback loop | **5** | Provable: `eta_bias` 1.0→1.192→1.2368; policy written and cited by planner on re-run |
| Self-healing | **4** | Verified escalate + reroute. −1: `tried` never updated on candidate-level failures → wasted cycles |
| Multimodal | **2.5** | Code path exists (`/api/orders/with-photo`, vision agent); not exercised live here |
| Reliability & security | **3** | Good failure handling overall, but key-in-logs, key-in-response, no auth, phantom-dispatch hole |
| Deployment | **3** | Solid Dockerfile; `railway.json` absent; deploy.sh untested; `/mcp` broken when deployed |
| Docs & reproducibility | **4** | README is excellent and mostly honest; drifts on railway.json, reflection model, "5 specialists" vs 7 rows |
| Competitive differentiation | **4** | Speculative geocode, matrix fan-out, learned policies in the prompt, boot model probing — genuinely differentiated |

**Overall: ~3.6/5. Top-quartile architecture, dragged down by two broken/false claims and no tests.**

---

## 4. Claim-to-evidence matrix

| Claimed capability | Code evidence | Runtime evidence | Test evidence | Status | Required action |
|---|---|---|---|---|---|
| Multi-agent orchestration | `crew.py` 5 agents + copilot + reflection; `pipeline.py` orchestrates | e2e run: triage→plan→critic→execute observed in spans | none | **Verified** | — |
| Autonomous logistics execution | `pipeline.py:_run_inner` → `_execute` → `dispatch_shipment` | Live order dispatched in 4.3 s | none | **Verified** | Add regression test |
| Google Maps routing | `maps.py` Routes v2 + matrix | `route.source=google-routes-matrix` in live response | none | **Verified** | — |
| Address validation gate | `maps.validate_address`, boot probe | 403 → degrades to geocoding (honest) | none | **Partially verified** | Enable API or keep honest note |
| MCP tool integration | `mcp_server/server.py`, 15 tools + 2 resources | stdio verified; **HTTP 500** | none | **Partially verified** | Fix session-manager lifespan + path |
| Multimodal input | `/api/orders/with-photo` → `crew.inspect_parcel` → `llm.vision` | untested live | none | **Implemented, untested** | One live photo test before demo |
| Fast DB operations | `db.py` WAL/mmap/serialized writer | bench-style reads ≈ µs; DB spans 1–3 ms live | none | **Verified** | — |
| Feedback loop | `feedback.py` Tier-1 EWMA + Tier-2 reflection | bias moved; ETA changed on re-run | none | **Verified** | — |
| Self-healing | `crew.heal` 6 strategies, `_repair_loop` bounded | escalate + reroute observed | none | **Verified** | Fix `tried` bookkeeping |
| Concurrent processing | `reserve_stock` guarded UPDATE in IMMEDIATE tx | 3-way race, no oversell | none | **Verified** | Add the test permanently |
| Scalable architecture | single-process by design; documented | — | none | **Not applicable** (honestly scoped) | — |
| Production readiness | Dockerfile, health checks, breakers | /mcp broken; key in logs | none | **Partially verified** | Fix findings §5 |
| End-to-end completion | `_result(ok=True)` → shipment row | verified | none | **Verified** | — |
| "railway.json committed" | — | file absent | — | **Missing** | Add file or fix README |
| Learned policies re-enter planner | `plan()` prompt includes `db.get_policies()` | `applied_policies: ['bluedart_heavy_fragile_eta']` | none | **Verified** | — |
| `tracking_url` | `logistics.py:341` emits `/track/{id}` | **no such route exists** | — | **Verified defect (minor)** | Remove or implement |

---

## 5. Ranked findings (verified defects first)

### Verified defects

1. **`/mcp` over HTTP is broken — headline claim fails at runtime.** `api.py:329-340` mounts
   `build_server().streamable_http_app()` but never enters the session manager's task group.
   Observed: `POST /mcp` → 307 → `/mcp/mcp` → `500 RuntimeError: Task group is not initialized.
   Make sure to use run().` The README's "deployed instance is a remote MCP server" is false
   today. Fix: run the session manager inside the FastAPI lifespan and flatten the path so the
   endpoint is literally `/mcp` (or document `/mcp/mcp`).

2. **Gibberish orders dispatch real shipments.** `crew.triage` (crew.py:109-113) replaces an
   all-unmatched LLM result with `_mock_triage`, which *always* emits `SKU-CABLE-USBC ×1` to
   Chennai (crew.py:70-71). `unmatched_requests` is produced but never consumed anywhere.
   Verified: `"asdkfj blah zzz"` → `SHP-8BE0A6DCD6`, order status `dispatched`. An evaluator
   sending adversarial input gets a false success and stock is consumed. Fix: gate on
   `unmatched_requests` / empty lines before geo; only fall back to mock triage when the LLM was
   *unreachable*, not when it legitimately found no products.

3. **No automated tests.** `tests/` is an empty directory; `make clean` references
   `.pytest_cache` that never exists. "Repository and implementation auditor" will flag this
   instantly; it's also the cheapest credibility win available.

4. **`railway.json` does not exist.** README §4 says "`railway.json` + `Dockerfile` are
   committed." Verified absent (`ls railway.json` → No such file). Either add it (5 min) or
   rewrite the section for `deploy.sh`/`service.yaml` (Cloud Run), which do exist.

5. **Copilot silently degrades and lies about why.** Under free-tier quota (observed 429s in the
   boot log), `tool_loop` exhausts retries → `chat()` returns the canned *"Conversational model
   is offline"* reply with `tools:[]` — while the model is actually rate-limited, not offline,
   and the real error hides in `degraded_reason` which the demo script doesn't print. A judge
   chatting after a few orders sees a seemingly broken copilot. Fix: surface the actual reason
   ("quota-cooled, retry in Ns") in the reply text.

6. **Maps API key leaks.** (a) httpx INFO logging prints full request URLs, including
   `?key=AIza…` for Geocoding/Address Validation — observed verbatim in uvicorn output.
   (b) `static_map_url` embeds the key in the shipment JSON returned to any `/api/orders`
   caller — standard for Static Maps, but it means the key is exposed to every API client.
   Fixes: move address-validation/geocode keys to `X-Goog-Api-Key` headers where supported, or
   downgrade httpx logger / strip query params; document the static-map exposure.

7. **`e2e.py` exits 1 on a fully successful demo.** The engineered escalate case counts against
   `len(oks) == len(results)` (e2e.py:110). `make e2e` looks like a failure to an evaluator.
   Fix: expect the escalation (`r["repairs"]` non-empty or error contains "escalated") and exit 0.

8. **Healer can re-pick the warehouse that already failed.** `tried` is only populated on
   *reservation* failure (pipeline.py:300). When the failure is "no feasible carrier" or a
   critic rejection, `_build_candidates` is called with `exclude=tried` where `tried` is empty —
   observed two wasted `reroute_warehouse` cycles then `repair exhausted` on orders no carrier
   can carry (weight). Bounded by `MAX_REPAIR_CYCLES=2` so it's inefficiency, not a hang —
   but on the exception path it costs ~4-9 s and the *mock* healer loops deterministically.
   Fix: add the healer's chosen warehouse to `tried` when re-planning fails, and let the mock
   healer escalate when `requires_split`/weight-bound.

9. **QA-reject leaves a zombie order.** `pipeline.py:325-329`: on vision "reject" the stock is
   released but `orders.status` is never updated — it stays `triaged` forever. Inconsistent
   final state; a state-machine reviewer catches it.

10. **Traffic-aware results are cached forever.** `_route_cache` and `_geo_cache`
    (maps.py:29-30) have no TTL/eviction; "live traffic" ETAs freeze for the process lifetime.
    Fine for a demo; a performance reviewer probing cached-vs-uncached will note it. Add a TTL
    (e.g. 5 min) — one line each.

### Minor defects / hygiene

- `record_delivery`: check-then-act outside the tx → concurrent double-delivery can
  double-increment `carrier_stats.shipments`. Guard the UPDATE with `WHERE actual_minutes IS NULL`.
- Duplicate `idempotency_key` under concurrency → UNIQUE-constraint 500 instead of replay.
  Pre-check exists but races; catch `IntegrityError` and re-read.
- `POST /api/multidrop` with missing `lat/lng` → 500 (KeyError) instead of 400.
- Dead code: `pipeline._same_place_dbg`, unused `thinking_budget` param on `structured()`,
  unused `get_db` import in `crew.py`.
- Idempotent-replay response shape differs from the normal path (`order` vs `shipment`).
- README drift: "Five specialists" vs 7 table rows; reflection claimed `flash-lite` but code
  uses the `planner` tier (`feedback.py:166`); latency "~2.6–3.7s" vs observed 3.1–5.5s.
- `deploy.sh` hardcodes `PROJECT=mithran-447407` — a cloner deploys to someone else's default.
- Workspace is not a git repo; `BRIEF.md` (contains names/emails) is not gitignored and would
  be published as-is.

### Verified strengths (do not regress)

- Schema-constrained single-shot calls on the hot path — no parse-failure path exists.
- Speculative address validation genuinely overlaps triage.
- One `computeRouteMatrix` for all candidates; verified `google-routes-matrix` live.
- Atomic reservation verified under a real 3-way race; compensating `release_stock` works.
- Feedback loop measurably changes the next decision — the best provable claim in the repo.
- Model pool: per-key probing + quota cooldown + tier failover observed working (429s → lite).
- Breakers + jittered retry + bounded repair + honest degraded mode everywhere.
- Secrets handling: `.env` gitignored *and* gcloudignored; deploy uses Secret Manager.
- README is unusually honest (carrier pricing simulated, single-node trade-off stated).

### Unverified claims to soften or prove

- "~2.6–3.7 s" → re-measure and publish observed p50/p95.
- `/mcp` remote server → currently false; fix or remove.
- Multimodal QA → test once with a real photo, or mark untested.
- `POST /api/multidrop` Google TSP → implemented, untested live.

---

## 6. Ten findings an evaluator is most likely to hit

1. `/mcp` returns 500 on the deployed instance (MCP-HTTP claim).
2. Nonsense input → real dispatched shipment (input validation).
3. `tests/` empty — no executable test suite.
4. `railway.json` promised, absent.
5. Copilot says "model is offline" when it's quota-limited.
6. `?key=` API key visible in server logs and static-map URLs.
7. `make e2e` exits 1 on success.
8. `orders.status` stuck `triaged` after QA reject.
9. Healer loops `reroute_warehouse` on undispatchable weight (wasted ~8 s, then fail).
10. Route cache has no TTL — "live traffic" goes stale; `tracking_url` is a dead link.

## 7. Three highest-impact improvements

1. **Fix `/mcp` HTTP** (~20 min): start `StreamableHTTPSessionManager` in the lifespan and mount
   at the right path. Turns a verified headline defect back into a differentiator.
2. **Intake gate** (~15 min): in `pipeline._run_inner`, if `unmatched_requests` covers all
   requested items or `lines` is empty after LLM triage, mark `status='rejected'` and return
   `ok=False` — and only invoke `_mock_triage` when the LLM *errored*. Closes the
   phantom-shipment hole a hostile reviewer will definitely probe.
3. **A real test file** (~30 min): `tests/test_pipeline.py` in mock mode — garbage-order
   rejection, concurrent oversell, repair-bound, idempotency, delivery-feedback math, MCP
   tool smoke (in-process `build_server()` calls, not stdio). Plus the two one-line fixes
   (e2e exit code, QA-reject status). This converts "claimed" to "provable" for every judge.

---

## 8. Reviewer-by-reviewer simulation

**R1 Repo/impl auditor** — README↔code mostly agrees; catches railway.json, the "5 specialists"
row-count, reflection model tier, empty tests/. README quality is genuinely high.

**R2 E2E executor** — full flow verified live. Fails on garbage input (ships anyway) and on the
QA-reject status hole. Everything else transitions cleanly with persisted state.

**R3 Performance** — two LLM calls, overlap, matrix fan-out: legitimately engineered. Will note
no-TTL caches if probing cached-vs-uncached, and that the "2.6–3.7s" headline is optimistic
(observed 3.1–5.5s). Recommend publishing observed numbers.

**R4 Agent architecture** — real tiered agents, schema-bound contracts, code critic, bounded
loops, tool calls executed in parallel with tracing. Strongest-in-class structure; only the
copilot degradation UX weakens it.

**R5 Google ecosystem** — Routes v2 (routes + matrix + waypoint-optimisation), Geocoding,
Address Validation (disabled → honest fallback), Static Maps, Gemini, Cloud Run deploy path.
Genuinely multi-product; address-validation just needs enabling.

**R6 MCP** — stdio: verified, 15 tools, real order through `fulfil_order`. HTTP: broken (finding #1).
Half the claim is real.

**R7 Feedback/self-healing** — Tier-1 EWMA provably re-prices the next order; Tier-2 policies
re-enter the planner prompt. Bounded retries, circuit breakers, compensating transactions.
This is a real loop, not a log line.

**R8 Reliability/security** — phantom dispatch, key-in-logs, key-in-response, no auth/rate-limit
on a public endpoint, delivery double-count race. The big-ticket item is #1 of §5's minors —
everything else is small.

**R9 Reproducibility** — `make install/verify/run` all real; `.env.example` complete; mock mode
means zero-key reproduction. Missing: git repo, railway.json, test command, a `make mcp-test`.

**R10 Differentiation** — speculative geocode overlap, matrix fan-out, learned policies fed back
into the prompt, boot-time model probing. These are real engineering, not demo glitter.

---

## 9. Architecture

**Current (verified):**

```
POST /api/orders ──► Orchestrator (pipeline.py, one per order)
  ├─ triage (Gemini, schema-bound)  ║  validate_address (Google)   ← parallel
  ├─ find_fulfillment_candidates (SQLite) → route_matrix (1 call) →
  │     plan_packaging + quote_carriers (pure Python, learned bias)
  ├─ plan (Gemini, schema-bound) → critique (code gate)
  │        └─ rejected/failed → _repair_loop → heal (tool-loop → structured) ×≤2
  ├─ reserve_stock (BEGIN IMMEDIATE) → [QA vision, opt] → dispatch_shipment (tx)
  └─ record_delivery → EWMA eta_bias → reflect → policies → next plan's prompt
MCP stdio: same logistics functions, 15 tools.   MCP HTTP @ /mcp: BROKEN.
```

**Proposed (minimal delta — do not rebuild):**

```
Same flow. Changes only:
  1. lifespan: async with mcp_session_manager.run() → /mcp serves streamable HTTP
  2. intake gate: empty-lines/unmatched → status=rejected, no dispatch
  3. repair: tried ← healer's failed choice; cache TTLs on geo/route
  4. delivery update guarded by `actual_minutes IS NULL`
  5. keys moved to header auth / suppressed from logs
```

Latency impact: zero on the happy path; −4–9 s on the exception path from fix #3; +0 ms for
the intake gate. Reliability impact: kills the false-success and the broken-mount findings.

---

## 10. Prioritized plan (~90 min)

### P0 — must fix (≈60 min)

| # | Task | Benefit | Est. | Files | Verify |
|---|---|---|---|---|---|
| 1 | Fix `/mcp` mount (lifespan session manager + path) | un-breaks a headline claim | 20m | `api.py` | `curl -X POST :8080/mcp -d initialize` → 200 |
| 2 | Intake gate for no-match orders; mock-triage only on LLM error | stops phantom shipments | 15m | `crew.py`, `pipeline.py` | `run_order("zzz")` → ok=False, status=rejected |
| 3 | `tests/test_pipeline.py` (mock mode, ~6 tests) + add `make test` | makes claims executable | 30m | `tests/`, `Makefile` | `.venv/bin/python -m pytest -q` |
| 4 | QA-reject sets `status='qa_rejected'` | consistent final state | 2m | `pipeline.py` | included in test |
| 5 | `e2e.py` exit code: expect the designed escalation | demo exits 0 | 3m | `scripts/e2e.py` | `make e2e && echo $?` |
| 6 | Remove Maps key from logs (header auth / log filter); note static-map exposure | closes a live secret leak | 5m | `maps.py`, `api.py` | boot, grep log for `key=` |
| 7 | Fix `tried` bookkeeping in repair loop | saves ~4-9 s on failures | 5m | `pipeline.py` | undispatchable order → escalate fast |

### P1 — high value (≈20 min)

| # | Task | Est. |
|---|---|---|
| 8 | Create `railway.json` **or** rewrite README §4 around `deploy.sh`/`service.yaml` | 10m |
| 9 | One live photo through `/api/orders/with-photo`; cache TTL on `_route_cache`/`_geo_cache`; remove or implement `/track/{id}`; `record_delivery` NULL-guard | 10m |

### P2 — defer

Multi-parcel split for overweight orders, Postgres swap, auth/rate-limiting, multi-worker,
true linked split orders. Document as roadmap — README's honest-limits style already covers this.

**Reserve:** final 10 min for `make verify`, `make e2e`, `pytest`, README corrections
(reflection tier, latency range, /mcp path), git init + push (drop or gitignore `BRIEF.md`).

---

## 11. Adversarial test suite (spec for `tests/test_pipeline.py`, all `CHAINMIND_MOCK=true`)

| Test | Assert |
|---|---|
| happy path | ok, shipment row exists, status=`dispatched`, reserved=0 after dispatch |
| garbage input | ok=False, status=`rejected`, **no shipment row**, no stock moved |
| insufficient stock (500 laptops) | ok=False, status=`escalated`, reserved=0 everywhere |
| overweight order (15 chairs) | bounded repair, no double-reroute, terminal `failed`/`escalated` |
| concurrent oversell | 3× same order; `reserved ≤ on_hand` for every SKU/warehouse after |
| idempotency | same `idempotency_key` twice → same order_id, no double dispatch |
| delivery feedback | `record_delivery` → `eta_bias` moves toward actual; replay is idempotent |
| reflection | ≥2 deliveries → policies written; planner prompt contains them |
| MCP tools | `network_status`, `check_fulfillment`, `reserve_stock`→`release_stock` round-trip |
| maps fallbacks | haversine leg returned, `source` tagged |

Mock-mode only — deterministic, no keys, seconds. Integration = `make verify` + `make e2e`.

---

## 12. README corrections needed

- §4: `railway.json` is not committed (add it or document `deploy.sh`).
- §5: "Five specialists" → seven agents listed; reflection runs on the `planner` tier, not lite.
- §12: publish *measured* numbers from the machine you demo on (observed here: 3.1–5.5 s).
- §6: the HTTP MCP endpoint path and the trailing-slash/session requirement — after the fix.
- Add `make test` to the entry-points list; add a claim→evidence table pointing at tests.
- `Known limits`: add "no auth on REST endpoints", "static-map URL embeds Maps key",
  "route cache TTL", "carrier pricing is a model, not a carrier API" (already partially there).

---

## 13. Final answers

- **Is the system genuinely functional?** Yes — verified live end-to-end with real Gemini and
  real Google Routes, persisted state, atomic reservation under concurrency.
- **Competitive on speed?** Yes — 3–5.5 s observed, ~75% of it model inference; non-model work
  is sub-second. The engineering (speculation, matrix fan-out, code critic) is real.
- **Does the implementation justify its agentic claims?** Mostly — real tiered agents with
  schema contracts and a code critic. Copilot degradation and the phantom-order hole are the
  weak joints.
- **Are Google integrations and MCP real?** Google: yes (matrix verified live; address-validation
  honestly disabled). MCP: stdio real, **HTTP mount broken — P0**.
- **Is feedback/recovery demonstrable?** Yes — the single most provable feature; `make e2e`
  shows bias→policy→changed-plan.
- **Can an independent evaluator reproduce it?** Nearly — mock mode covers zero-key runs;
  needs a git repo, the railway/README fix, and a test command.
- **Remaining weaknesses:** /mcp HTTP, phantom dispatch, no tests, key-in-logs, QA status hole.
- **Single best remaining improvement:** fix the `/mcp` session-manager mount — it converts a
  verified broken claim back into a genuine differentiator for ~20 minutes of work.

*Audit standard applied: every claim above is backed by file path, runtime output, or is
explicitly marked unverified. Nothing was measured that wasn't run.*
