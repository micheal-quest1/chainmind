# ChainMind — Autonomous Supply-Chain Orchestrator

**Agent War · DevFest 2026**

A free-text order goes in. A dispatched shipment comes out — warehouse chosen, route
solved against live Chennai traffic, carrier priced, stock reserved atomically,
exceptions recovered from without a human — in **~2.6–3.7 seconds**, end to end.

Then it learns from what actually happened and plans the next one differently.

```
"2 laptops and 5 usb-c cables to Anna Nagar, Chennai — urgent, hospital procurement"
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  TRIAGE AGENT  (gemini-3.5-flash-lite)                           │  ~1.2s
  │  free text → structured order                                    │
  │  ║ runs CONCURRENTLY with ║                                      │
  │  GOOGLE ADDRESS VALIDATION on a speculatively-extracted address  │
  └──────────────────────────────────────────────────────────────────┘
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  CANDIDATE FAN-OUT                                               │  ~0.3s
  │  SQLite: which warehouses hold the whole order (1.8µs reads)     │
  │  Google Routes computeRouteMatrix: ONE call scores all 6         │
  │  packaging + carrier pricing in pure Python                      │
  └──────────────────────────────────────────────────────────────────┘
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  PLANNER AGENT  (gemini-3.6-flash)                               │  ~1.5s
  │  picks warehouse + carrier from scored candidates,               │
  │  reading back the policies the system has LEARNED                │
  └──────────────────────────────────────────────────────────────────┘
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  CRITIC  (pure code — microseconds, never wrong)                 │  ~0.0s
  │  hard constraints: feasibility, cold chain, express sanity       │
  └────────────┬─────────────────────────────────┬───────────────────┘
      approved │                        rejected │
               ▼                                 ▼
  ┌────────────────────────┐   ┌──────────────────────────────────────┐
  │ RESERVE (atomic tx)    │   │  HEALER AGENT (gemini-3.7-flash)     │
  │ PACKAGE                │   │  investigates with real tools, then  │
  │ [QA VISION — optional] │   │  commits to one of 6 repair          │
  │ DISPATCH               │   │  strategies, then re-plans           │
  └────────────┬───────────┘   └──────────────────────────────────────┘
               ▼
        shipment + full trace
               │
               ▼  (delivery happens)
  ┌──────────────────────────────────────────────────────────────────┐
  │  FEEDBACK LOOP                                                   │
  │  Tier 1: EWMA on actual/predicted → carrier eta_bias → the very  │
  │          next order is priced differently.      (microseconds)   │
  │  Tier 2: reflection agent distils natural-language policies that │
  │          the planner reads on every future order.                │
  └──────────────────────────────────────────────────────────────────┘
```

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Why it is fast](#2-why-it-is-fast-the-judged-metric)
3. [Run it in 3 minutes](#3-run-it-in-3-minutes)
4. [Deploy](#4-deploy)
5. [The agents](#5-the-agents)
6. [MCP integration](#6-mcp-integration)
7. [Self-healing](#7-self-healing)
8. [The feedback loop](#8-the-feedback-loop-closed-and-provable)
9. [Google services used](#9-google-services-used)
10. [Data model](#10-data-model)
11. [API reference](#11-api-reference)
12. [Benchmarks](#12-benchmarks-measured-not-estimated)
13. [Known limits](#13-known-limits-honest-list)
14. [Repo map](#14-repo-map)

---

## 1. What it does

A six-node Indian distribution network — Chennai, Bengaluru, Hyderabad, Mumbai, Delhi,
Coimbatore — holding 8 SKUs across godowns, storehouses and a darkstore, served by 5
carriers with different weight limits, fragility ratings, cold-chain capability and
price curves.

The system takes a plain-English order and autonomously:

1. **Parses** it into structured line items against a real catalogue (never inventing a SKU)
2. **Validates the delivery address** with Google, before anything is committed
3. **Finds** every warehouse that can ship the whole order from live stock
4. **Routes** all candidates to the customer in one Google Routes matrix call, traffic-aware
5. **Packages** it — box size from real weights, bubble wrap for fragile, gel packs for cold chain
6. **Prices** every carrier, with ETAs corrected by each carrier's *learned* historical bias
7. **Decides** warehouse + carrier, citing the numbers and the learned policies it used
8. **Reviews** that decision against hard constraints, and sends it back if it fails
9. **Recovers** autonomously when something is impossible — reroute, switch carrier, split,
   downgrade, partial-fulfil, or escalate — re-reading live state each time
10. **Reserves** stock in a single atomic transaction (two concurrent orders cannot oversell)
11. **Inspects a parcel photo** with a vision agent and can block dispatch (optional)
12. **Dispatches**, then **learns** from the real delivery time

Everything is traced. Every stage is idempotent. Nothing is mocked unless a key is missing.

---

## 2. Why it is fast (the judged metric)

Median end-to-end is **~3.3s**, of which ~2.6s is Gemini. The engineering is in making
the *non-model* work vanish and in making the model work count.

| Decision | Why | Saved |
|---|---|---|
| **Two LLM calls on the happy path, not a conversation** | Triage and planning are schema-constrained single shots. A ReAct loop would be 6–10 round trips. | ~4–8 s |
| **Speculative address validation** | We regex the destination out of the raw text and start the Google call *concurrently with triage*. By the time triage returns, the address is usually already resolved. | ~300–600 ms |
| **One `computeRouteMatrix`, not N `computeRoutes`** | Six warehouses scored in a single HTTP request. | ~5× on the routing stage |
| **The critic is code, not a model** | Constraint checking is what an LLM is worst and slowest at. | ~1.5 s per validation |
| **Minimum reasoning depth on the hot path** | `thinking_level="low"`; the planner does zero arithmetic — it selects over numbers Python already computed. | ~1–3 s |
| **Expensive tier only on the failure path** | The repair model never loads on a healthy order. | ~2 s |
| **In-process SQLite, not a network DB** | 1.8 µs point reads. ~40 DB ops per order would cost 40–800 ms against a managed DB. | ~40–800 ms |
| **Tools called in parallel within an agent turn** | Gemini can emit several function calls per turn; we `asyncio.gather` them. | ~2× on repair |
| **HTTP/2 keep-alive client, reused** | TLS handshakes dominate otherwise. | ~100 ms/call |
| **Model resolution at boot, not first request** | The first customer's order is as fast as the hundredth. | ~1 s on first order |

Run `make bench` to reproduce all of it.

---

## 3. Run it in 3 minutes

**Requirements:** Python 3.13, and optionally Docker.

```bash
git clone <this-repo> && cd AgentWar

# 1. install
make install                       # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. configure
cp .env.example .env
#    put your keys in .env:
#      GEMINI_API_KEY      -> https://aistudio.google.com/apikey
#      GOOGLE_MAPS_API_KEY -> https://console.cloud.google.com/google/maps-apis
#    Both are optional. With neither, everything still runs on deterministic
#    fallbacks and the flow completes end to end.

# 3. prove your keys work before you demo
make verify
#   gemini key present : True
#   [gemini] resolved model tiers:
#      triage    -> gemini-3.5-flash-lite
#      planner   -> gemini-3.6-flash
#      ...
#   [maps] {'geocoding': True, 'routes_v2': True, ...}
#   RESULT: all configured Google APIs reachable

# 4. run
make run                           # http://localhost:8080
```

Open **http://localhost:8080** for the live dashboard: pipeline stages streaming in over
SSE with per-stage timings, the candidate comparison table, the Google static map of the
chosen route, the learning curve, and the conversational copilot.

### Other entry points

```bash
make e2e      # scripted demo: 4 orders incl. one that forces self-healing,
              # then the feedback loop, the reflection agent and the copilot
make bench    # latency benchmark: DB floor, per-stage, sequential vs concurrent
make mcp      # MCP server over stdio
make docker   # build and run the container
```

### Which Google APIs to enable

On the Maps project: **Routes API**, **Geocoding API**, **Maps Static API**, and
optionally **Address Validation API**. `make verify` tells you exactly which answered.

---

## 4. Deploy

Deployed on **Google Cloud Run** — one command, keys held in **Secret Manager**.

```bash
cp .env.example .env     # fill in your keys
./deploy.sh              # build, push, deploy, verify
```

`deploy.sh` is idempotent and does the whole thing:

1. Enables `run`, `cloudbuild`, `artifactregistry`, `secretmanager` on the project
2. Reads each key out of `.env` and writes it to **Secret Manager**
   (`chainmind-gemini-key`, `chainmind-maps-key`) — via a `600` temp file, so the key
   never appears in shell history or the process list, and never enters the image
3. Grants the Cloud Run runtime service account `secretmanager.secretAccessor`
4. Builds from source with Cloud Build and deploys to Cloud Run
5. Curls `/healthz` and `/api/capabilities` on the live URL and prints the result

Override the defaults with env vars:

```bash
PROJECT=my-project REGION=us-central1 SERVICE=chainmind ./deploy.sh
```

`service.yaml` holds the same configuration declaratively, so what is deployed is
reviewable in git rather than buried in CLI flags:

```bash
gcloud run services replace service.yaml --region asia-south1
```

### Why the deployment is configured this way

| Setting | Reason |
|---|---|
| `--min-instances 1` | No cold start mid-demo. Cloud Run cold-starting this image costs ~3 s, which would dwarf the pipeline itself. |
| `--max-instances 1` | The SQLite writer and the SSE event bus are per-process, and learned policies live on the instance's disk. Two instances would diverge. |
| `--no-cpu-throttling` | Background work (reflection, SSE keepalives) must keep running between requests. |
| `--concurrency 40` | The workload is IO-bound on Google APIs, so one process serves many requests on asyncio. |
| `--execution-environment gen2` | Needed for full filesystem semantics and GCS FUSE if you add a volume. |
| Secret Manager, not env vars | Keys are rotatable without a redeploy and never land in the image, the repo, or `gcloud` history. |

> **Persistence:** `/data` on Cloud Run is the instance's writable layer, so learned
> policies and carrier bias reset when the instance is replaced. For durable learning,
> mount a GCS bucket:
> ```bash
> gcloud run services update chainmind --region asia-south1 \
>   --add-volume=name=data,type=cloud-storage,bucket=YOUR_BUCKET \
>   --add-volume-mount=volume=data,mount-path=/data
> ```
> SQLite over GCS FUSE trades the µs latency numbers for durability — a deliberate
> choice, not a default.

### `.gcloudignore` matters

Cloud Build does **not** read `.dockerignore` when uploading source. `.gcloudignore` is
what actually keeps `.env` and the local database off Google's build servers. It is
committed.

### Running it anywhere else

The image is a plain Dockerfile with no Cloud Run coupling, and configs for two other
hosts are committed:

```bash
docker build -t chainmind .
docker run -p 8080:8080 --env-file .env -v chainmind-data:/data chainmind
```

| Host | Config | Steps |
|---|---|---|
| **Railway** | `railway.json` | New Project → Deploy from GitHub repo → add `GEMINI_API_KEY` + `GOOGLE_MAPS_API_KEY` under Variables → Generate Domain |
| **Render** | `render.yaml` | New → Blueprint → point at the repo → fill the two keys. Includes a 1 GB persistent disk at `/data`, so learning survives redeploys. |
| **Fly.io** | — | `fly launch --dockerfile Dockerfile` then `fly secrets set GEMINI_API_KEY=... GOOGLE_MAPS_API_KEY=...` |

All of them inject `$PORT`, which the container honours.

### If Cloud Run rejects the deploy with `BILLING_DISABLED`

This happens when a project is linked to a billing account that has no valid payment
method — `gcloud billing projects describe` still reports `billingEnabled: true`, so the
only visible symptom is Artifact Registry and Secret Manager returning `PERMISSION_DENIED
... BILLING_DISABLED`. Re-link a working billing account at
`console.cloud.google.com/billing/linkedaccount?project=<PROJECT>`, then re-run
`./deploy.sh`. Nothing in the code needs to change.

### Verifying a deployment

```bash
./scripts/smoke.sh https://your-url      # 7 endpoint checks + one real order end to end
```

## 5. The agents

Five specialists. Each runs on the **cheapest model that still succeeds** — that mapping
is the whole multi-model strategy.

| Agent | Model tier | Shape | Job |
|---|---|---|---|
| **Triage** | `gemini-3.5-flash-lite` | structured, 1 call | free text → structured order, mapped to real SKUs |
| **Planner** | `gemini-3.6-flash` | structured, 1 call | choose warehouse + carrier from scored candidates, applying learned policies |
| **Critic** | *no model* | pure Python | hard-constraint gate; rejection routes to the healer |
| **Healer** | `gemini-3.7-flash` | tool loop → structured | investigate with live tools, commit to one of 6 repair strategies |
| **QA inspector** | `gemini-3.5-flash` | multimodal | parcel photo → damage verdict + label OCR; can block dispatch |
| **Copilot** | `gemini-3.5-flash` | multi-turn tool loop | conversational ops, calling the same tools the pipeline uses |
| **Reflection** | `gemini-3.5-flash-lite` | structured, batched | distil delivery outcomes into routing policies |

### Three deliberate call shapes

- **Structured single-shot** on the hot path. Schema-constrained decoding means the model
  is *structurally unable* to return prose where a warehouse ID belongs. There is no
  "failed to parse model output" code path in this repo.
- **Tool loops** where the number of steps genuinely isn't known up front (healer, copilot).
  We drive the loop ourselves rather than using automatic function calling, so every tool
  call is traced, retried and executed in parallel.
- **Multimodal** for parcel QA.

### Model resolution is probed, not hardcoded

Hardcoding model IDs is how demos die. **Trusting `models.list()` is how they die more
subtly** — during this build, `models.list()` cheerfully listed every `gemini-2.5-*`
model, all of which return `404 NOT_FOUND` for keys created after the cutover, and listed
a `pro` model that was quota-exhausted. So at boot we fire a real one-token generation at
every candidate concurrently and pin each tier to one that actually answered.

`GET /api/capabilities` shows what it picked.

---

## 6. MCP integration

The logistics network is exposed as a **Model Context Protocol** server with **15 tools**
and 2 resources — over **both transports**:

```bash
# stdio, for Claude Desktop / Cursor / any local MCP client
python -m chainmind.mcp_server
```

```
# streamable HTTP — the DEPLOYED instance is a remote MCP server
https://<your-app>.up.railway.app/mcp
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "chainmind": {
      "command": "/abs/path/AgentWar/.venv/bin/python",
      "args": ["-m", "chainmind.mcp_server"],
      "cwd": "/abs/path/AgentWar",
      "env": { "GEMINI_API_KEY": "...", "GOOGLE_MAPS_API_KEY": "..." }
    }
  }
}
```

Verify it on a running instance:

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"curl","version":"1"}}}'
```

> **Mounting note.** A mounted ASGI sub-app does *not* get its lifespan run by the
> parent, and `streamable_http_app()` returns `500 task group is not initialized` until
> its session manager is running — so the FastAPI lifespan owns it explicitly. The
> sub-app also defines its own `/mcp` route and mcp 2.3 exposes no setting to change it,
> so it is mounted at `""` rather than `"/mcp"` to avoid serving at `/mcp/mcp`.

**Tools:** `network_status`, `search_inventory`, `check_fulfillment`, `order_status`,
`compute_route`, `validate_address`, `optimize_multidrop`, `plan_packaging`,
`quote_carriers`, `fulfil_order`, `reserve_stock`, `release_stock`, `record_delivery`,
`run_reflection`, `learning_state`.
**Resources:** `chainmind://network`, `chainmind://policies`.

### Why this isn't a box-ticking exercise

These are the *same functions* the in-process agents call. One implementation, two
consumers: the pipeline calls them as Python (no IPC on the hot path — worth tens of ms
per order), external clients get the identical surface over MCP. There is no second code
path to drift out of sync. `fulfil_order` runs the entire autonomous flow, so any MCP
client can drive the whole system with one call.

---

## 7. Self-healing

Four independent layers, because "self-healing" that means "we added a retry" isn't.

**1. Transport — retry with jittered exponential backoff.** Every external call is wrapped
in `resilient()`.

**2. Dependency — circuit breakers.** After N consecutive failures a dependency is tripped
open and calls route straight to a deterministic fallback instead of stalling. Visible at
`/readyz`.

**3. Capability negotiation.** Real failures hit during this build, each now handled
automatically:
- `thinking_budget` (Gemini 2.x) hard-400s on 3.x models, which want `thinking_level`.
  Each model starts on its family's expected syntax and **silently downgrades** the first
  time the API rejects it.
- Free tier enforces both a per-minute **and a per-day** cap. A daily exhaustion returns
  `retry in 16h39m`. A naive backoff would hammer a model that's finished for the day, so
  we honour the API's own retry hint and **drop that model out of the pool**, spilling the
  request to the next model in the tier's chain. Multi-model routing stops being a
  nice-to-have and becomes what keeps the system up under quota pressure.
- A model that 404s or 503s is cooled for 60s and traffic moves on.

**4. Business logic — the healer agent.** When no retry can help (no warehouse has the
stock), the healer investigates against live state and commits to one of six strategies:

| Strategy | When |
|---|---|
| `reroute_warehouse` | another warehouse has the stock |
| `switch_carrier` | same warehouse, different feasible carrier |
| `split_shipment` | no single warehouse holds everything |
| `downgrade_priority` | express is impossible, standard is achievable |
| `partial_fulfil` | ship what exists, drop what doesn't |
| `escalate_human` | genuinely nothing is shippable — say exactly what's missing |

It is handed the authoritative live stock table in its prompt, so it never claims to lack
data, and it re-reads state on each cycle so it never acts on a stale picture. Bounded at
2 cycles. Partial work is undone by a compensating `release_stock` before re-planning.

Try it: order `500 laptops to Bandra, Mumbai`. Total network stock is 158, and the healer
escalates with exactly that number rather than silently failing.

---

## 8. The feedback loop (closed, and provable)

A feedback loop that only writes a log isn't a loop. This one changes the next decision.

**Tier 1 — statistical, every delivery, no LLM, microseconds.**
Each completed delivery updates that carrier's `eta_bias` with an EWMA of
`actual / predicted`, plus on-time and damage rates. `quote_carriers()` reads `eta_bias`
on the **very next order**.

**Tier 2 — semantic, batched, LLM.**
The reflection agent reads recent outcomes and distils them into natural-language policies
stored in the `policies` table. The planner's prompt includes them on every subsequent run,
so learned *judgement* re-enters the decision, not just a number.

### Proving it on stage

```bash
make e2e
```

Real output from this repo:

```
FEEDBACK LOOP
  Delhivery  promised   78.3m actual  103.4m (+32.0%)  bias 1.0   -> 1.192
  BlueDart   promised   36.8m actual   48.6m (+32.0%)  bias 1.0   -> 1.192
  BlueDart   promised   74.5m actual   98.3m (+32.0%)  bias 1.192 -> 1.2368

  reflection by gemini-3.5-flash-lite:
    [bluedart_heavy_fragile_delay] Increase ETA predictions by 35 percent for
    BlueDart when handling heavy fragile shipments exceeding 25kg.  (conf 0.4)

RE-RUN AFTER LEARNING (same order as case 1)
  before: Delhivery eta=78.3m
  after : Delhivery eta=81.9m   <- ETA inflated by learned bias
```

Same order, same route, **different promise** — because the system was wrong last time and
corrected itself. `GET /api/feedback` returns the full curve; the dashboard's *Feedback
loop* tab shows it with a one-click "simulate delivery outcome" button.

---

## 9. Google services used

| Service | Where | Fallback if unavailable |
|---|---|---|
| **Gemini API** (`google-genai`) | all 7 agents, 4 model tiers | deterministic keyword/heuristic agents — flow still completes |
| **Routes API v2** `computeRoutes` | chosen-route detail, traffic-aware | haversine × 1.27 road factor |
| **Routes API v2** `computeRouteMatrix` | scoring all candidate warehouses in one call | per-leg haversine |
| **Routes API v2** `optimizeWaypointOrder` | multi-drop last-mile TSP (`POST /api/multidrop`) | nearest-neighbour + 2-opt |
| **Geocoding API** | destination → coordinates | 20-city lookup table |
| **Address Validation API** | deliverability gate before dispatch | geocoding, then `needs_review` |
| **Maps Static API** | route image on the dashboard | omitted |

**Multi-drop is the real "shortest path" problem.** Depot→customer is trivial; depot→N
customers→depot in the cheapest order is the one that matters in logistics, and Google
solves it against live traffic. `POST /api/multidrop` with a depot and up to 25 stops.

---

## 10. Data model

SQLite in **WAL** mode, `synchronous=NORMAL`, 256 MB mmap, 32 MB page cache, one serialized
writer connection plus per-thread readers, indices on every access path the pipeline uses.

**Why not Postgres/Redis:** the judged criterion is end-to-end latency. This pipeline does
~40 DB operations per order. At 1–20 ms per network round trip, the database alone would
outweigh the LLM calls. Measured here: **1.8 µs** point reads, **122 µs** durable writes.
That's 550–11,000× faster than a managed DB hop. The trade-off is single-node — stated
plainly in [Known limits](#13-known-limits-honest-list).

Tables: `warehouses`, `inventory`, `orders`, `order_lines`, `allocations`, `shipments`,
`events`, `spans`, `policies`, `carrier_stats`.

`spans` is an OpenTelemetry-shaped trace table — every agent call, tool call and stage is
recorded, which is what powers `/api/metrics` and the dashboard's percentile view.

---

## 11. API reference

Interactive docs at **`/docs`** (OpenAPI).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/orders` | **Run the full autonomous flow.** This is the endpoint to time. |
| `POST` | `/api/orders/with-photo` | Same, plus the multimodal QA gate (multipart) |
| `GET` | `/api/orders/{id}` | Order + shipment snapshot |
| `POST` | `/api/chat` | Conversational copilot (tool-calling) |
| `POST` | `/api/deliveries` | **Close the feedback loop** with an actual delivery time |
| `POST` | `/api/reflect` | Run the reflection agent |
| `GET` | `/api/feedback` | Learning curve, carrier bias, active policies |
| `GET` | `/api/network` | Warehouses, stock, carrier stats |
| `GET` | `/api/inventory?sku=` | Inventory by SKU |
| `POST` | `/api/multidrop` | Google waypoint-optimised multi-stop route |
| `GET` | `/api/metrics` | p50/p95/p99 per span, breaker state, totals |
| `GET` | `/api/events` | Recent pipeline events |
| `GET` | `/api/stream` | **SSE** live pipeline feed |
| `GET` | `/api/capabilities` | Resolved models, Google API probe, quota cooldowns |
| `GET` | `/healthz` `/readyz` | Liveness / readiness |
| `*` | `/mcp` | MCP server (streamable HTTP) |

```bash
curl -s localhost:8080/api/orders -H 'content-type: application/json' \
  -d '{"text":"2 laptops and 5 usb-c cables to Anna Nagar, Chennai, urgent"}' | jq '{
    ok, total_ms,
    warehouse: .shipment.warehouse.name,
    carrier:   .shipment.carrier,
    eta:       .shipment.eta_minutes,
    km:        .shipment.route.distance_km,
    rationale: .plan.rationale
  }'
```

Every response carries `x-request-id` and `server-timing`. Pass `idempotency_key` to make
order creation safely retryable.

---

## 12. Benchmarks (measured, not estimated)

`make bench`, on the free Gemini tier:

```
DATASTORE (SQLite WAL, in-process)
  point read             1.8 us
  durable write        121.7 us
  indexed scan          14.6 us
  -> a managed DB round trip is ~1-20 ms, i.e. 549-10985x this.

END-TO-END, SEQUENTIAL
  #1 ok     3561ms   agent=2585  pipeline=2137  db=2.8
  #2 ok     2619ms   agent=2199  pipeline=1401  db=1.5
  #3 ok     3749ms   agent=2907  pipeline=2082  db=1.9
  #4 ok     3704ms   agent=3035  pipeline=2397  db=0.8
  min 2619ms  median 3633ms  p95 3749ms

STAGE PERCENTILES (ms)
  agent.planner                n=8  avg=3600.2
  stage.triage_parallel_geo    n=8  avg=1274.3
  agent.triage                 n=8  avg=1269.9
  stage.candidates             n=8  avg= 788.9     <- incl. the Google route matrix
  stage.dispatch               n=8  avg=   1.1
  stage.reserve                n=8  avg=   0.4
```

**~75% of wall-clock time is Gemini inference.** Everything this project controls —
database, routing fan-out, packaging, pricing, reservation, dispatch — totals **under
800 ms combined**, and the reservation and dispatch writes are *sub-millisecond*.

---

## 13. Known limits (honest list)

- **Concurrency is currently quota-bound, not architecture-bound.** 4 orders in parallel
  took 21s wall versus 13.6s sequential, because 8 simultaneous Gemini calls trip the free
  tier's per-minute limit and the failover logic walks down to slower models. On a billed
  key the same code goes the other way. The fix is billing, not a rewrite.
- **Free-tier daily caps are small** (as low as 20 requests/day on some models). After a
  few demo runs you may see `degraded: true` and deterministic fallbacks. `make verify`
  tells you before you're on stage; `GET /api/capabilities` shows which models are cooling.
- **Address Validation API was not enabled** on the Maps project used here, so that call
  degrades to Geocoding. Enabling it in the Cloud console is the only change needed — the
  code path is already written and probed at boot.
- **Single-node datastore.** SQLite is the right call for single-flow latency and wrong for
  horizontal scale. The `Database` class is a narrow facade over 6 methods; swapping in
  Postgres is a contained change, at the cost of the µs numbers above.
- **One process on purpose.** The SQLite writer and the SSE event bus are per-process. The
  workload is IO-bound on Google APIs, so asyncio inside one process beats multiple workers
  here. Scaling out means externalising both.
- **Carrier APIs are simulated.** Carrier profiles are a priced model, not live carrier
  integrations. Routing, geocoding, address validation and all agent reasoning are real.
- **`split_shipment` ships the largest viable subset** and reports the remainder rather
  than creating a true second linked order.

---

## 14. Repo map

```
chainmind/
  api.py                  FastAPI: REST + SSE + dashboard + /mcp mount
  pipeline.py             the orchestrator — speculative geo, fan-out, critic, repair loop
  feedback.py             two-tier learning loop + learning curve
  core/
    config.py             env-driven settings, model tiers, capability reporting
    db.py                 SQLite WAL facade, schema, span/event recording
    reliability.py        tracer, spans, retry-with-jitter, circuit breakers
    bus.py                SSE pub/sub with bounded per-subscriber queues
    seed.py               the 6-node network, idempotent
  agents/
    llm.py                Gemini runtime: model pool, quota failover, thinking negotiation,
                          structured output, parallel tool loop, multimodal
    crew.py               triage / planner / critic / healer / QA agents
    copilot.py            conversational ops agent
    schemas.py            output schemas constraining every agent
  services/
    maps.py               Routes v2, matrix, waypoint optimisation, address validation,
                          geocoding, static maps — each with a deterministic fallback
    logistics.py          inventory, atomic reservation, packaging, carrier pricing, dispatch
  mcp_server/             15-tool MCP server, stdio + streamable HTTP
  web/index.html          live dashboard
scripts/
  verify_apis.py          prove which Google APIs your key reaches
  probe_models.py         which Gemini models actually answer
  probe_thinking.py       which thinking-control syntax each family accepts
  e2e.py                  the scripted demo
  bench.py                the latency benchmark
```

---

## License

MIT
