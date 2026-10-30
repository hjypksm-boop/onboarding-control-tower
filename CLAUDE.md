# CLAUDE.md

A portfolio project: a B2B onboarding-to-revenue control tower for a fintech. It tracks clients from deal close through parallel onboarding tracks, go-live, revenue ramp and second-product adoption. Built with Python and SQL on SQLite, with a schema written to port to Postgres. All companies, people and figures are fictional.

**As-of date:** 2026-10-18 (`TODAY` in `generate_data.py`). Use this date for "today" in queries, not the real clock.

## Rules

- **All numbers are calculated in SQL or Python, never by an LLM.** Any agent or LLM layer may only explain, summarise or route numbers that code has already computed. It must never estimate, add up or invent figures.
- **Use the standard library only** unless the user approves a dependency. Ask before adding any package. Approved so far: `groq` and `python-dotenv`, for the agent layer only (`requirements.txt`). The data generator, views and checks stay standard-library only.
- **Never commit API keys** or other secrets. `GROQ_API_KEY` lives in `.env`, which is gitignored. `.env.example` is the committed template and must stay empty of real values.
- **The agent layer** (`llm.py` and `agents.py`) uses the Groq SDK with `openai/gpt-oss-120b`, which `GROQ_MODEL` can override. The user's Groq account has no Llama chat model. gpt-oss is a reasoning model, so `llm.py` sets low reasoning effort and adds token headroom. **Code writes every figure and every sentence that states or combines figures,** including the brief headline, nudge bullets and blocker lists. The LLM only classifies edge cases and drafts actions, prioritisation and tone, through `llm.draft()`. Drafts with any digit outside a record ID are rejected. Drafts are labelled `[DRAFT – review before sending]`, and code-written fallback text is used with no key or on an API error. Keep it that way: never let the LLM reword a line that contains a figure.
- Lanes come from `routing.route_lane()` (`routing.py`), the single source of the routing and escalation rules. The generator and the lane router agent both import it. Never hard-code a lane for a client, and never copy the rules elsewhere.
- Don't push to a remote unless the user asks.

## Files and commands

| File | Purpose |
|---|---|
| `schema.sql` | DDL for the 11 tables |
| `metrics.sql` | Metric views: `v_as_of`, `v_stage_sla`, `v_trapped_revenue`, `v_ramp`, `v_second_product`, `v_touchpoints`, `v_owner_workload`. Uses DROP plus CREATE, so it's safe to re-run. Build new metrics here as views. Rank owners by `v_owner_workload.workload_rank` (in progress + blocked), never by total open stages. The `v_week_*` views (the 7 days to the as-of date) derive events only from existing dates: `blocker_since`, `actual_end_date`, planned end + 1, `target_go_live_date` and `actual_go_live_date`. Never fake history; a blocker cleared within the week or a moved forecast can't be shown without snapshots |
| `generate_data.py` | Builds `control_tower.db` and `csv/`, then loads `metrics.sql` and checks that `v_as_of` matches `TODAY`. Seeded (`random.seed(42)`), so every run gives the same data |
| `check_data.py` | Sanity checks, plus a query against each metric view. Exits with status 1 if any integrity check fails. Works from any directory |
| `.github/workflows/ci.yml` | CI on push and PR (Python 3.12 and 3.13): rebuild the data, `git diff --exit-code csv/` (the generator must stay deterministic), integrity checks, tests, agents smoke test without a key. If you change the generator on purpose, commit the regenerated CSVs |
| `control_tower.db` | Generated and gitignored. Rebuild it rather than editing it |
| `csv/` | One CSV per table. Committed |
| `requirements.txt` / `.env.example` | Agent-layer dependencies, and the committed template for `.env` |
| `llm.py` | Groq wrapper: loads `.env`, provides `draft()` (figure-free text with the number guard and the DRAFT label) and `classify()` (returns only an allowed label), and falls back to code-written text |
| `agents.py` | Agents: blocker triage (keyword rules, with the LLM only for edge cases), blocker summary, owner nudges, daily brief, lane router (`lanes`) |
| `routing.py` | `route_lane()`, `should_escalate()` / `escalation_day()` (escalates on the first day beyond 1.5× the lane target), and `boundary_flips()` (one-step attribute changes that move a lane). `LANE_TARGET` must match `LANES` in the generator, which asserts it |
| `tests/test_agents.py` | Guardrail tests (standard library `unittest`, no API calls): headline figures equal the views, the draft guard rejects figures, and the LLM changes no line with a figure |
| `build_dashboard.py`, `dashboard/template.html` | Static dashboard, built to `docs/index.html` and served by GitHub Pages. Standard library only, with inline SVG and vanilla JS (no CDN). Every figure comes from SQL against the views. Text goes in via `textContent` only. The palette follows the dataviz method: categorical slots 1–3 validated for light and dark, status colours always with an icon and label, a table view for every chart. Output is deterministic. After changing the data, views or template, rerun it and commit `docs/index.html`, otherwise CI fails |
| `tests/test_metrics.py` | View-level tests, such as `v_owner_workload` ranking by active work |
| `tests/test_week.py` | This week: window, events inside it, counts equal to an independent raw query, and the brief and dashboard matching the views |
| `tests/test_dashboard.py` | Dashboard figures equal the views, and the embedded JSON is safe and complete |
| `tests/test_routing.py` | Routing rule order, escalation threshold, boundary flips, every stored lane matching the rules, and the lane report staying code-written |

```sh
source .venv/bin/activate
pip install -r requirements.txt
python generate_data.py   # rebuilds DB + CSVs (deletes existing DB first)
python check_data.py      # all integrity checks should return 0
python agents.py          # or: classify | blockers | nudges | brief | lanes
python -m unittest discover -s tests -v
python build_dashboard.py # rebuild docs/index.html, then commit it
```

Expected counts: 40 clients (30 onboarding, 10 live), 49 journeys (35 in flight), 288 stages, 2,885 transaction days, 156 touchpoints.

## Lanes and routing (`routing.py`)

The rules are checked in this order, and the first match wins:

1. Any product that needs partner bank sign-off (USDC Pay-in, USDC Payout) → **Exceptions**
2. High risk → **Exceptions** (high-risk KYB, partner bank enhanced due diligence)
3. Cards or Travel → **Complex**
4. More than one product onboarding at once → **Complex**
5. 3 or more legal entities → **Complex**
6. No-contract product (Verify, Wallets, Pay-ins), low risk, not Enterprise, 1 entity → **Fast**
7. Anything else → **Standard** (contracted single product, low or medium risk)

| Lane | Target (onboarding start to go-live) |
|---|---|
| Fast | 10 days (no Contract track) |
| Standard | 30 days |
| Complex | 60 days |
| Exceptions | 75 days (adds Partner bank approval) |

**Escalation:** a journey outside Exceptions whose elapsed days exceed 1.5× its lane target moves to Exceptions. `client_products.initial_lane` keeps the routed lane, `current_lane` holds the escalated one, and `lane_reason` gets "; escalated: …" appended.

Second products route with `entities = 1`, because the client's entities are already onboarded.

## Onboarding model (parallel tracks)

- All parallel tracks start together on `onboarding_start_date`: **Contract, Legal, KYB-KYC, Tech build, Compliance ops readiness, Partner bank approval** (Partner bank approval runs only in Exceptions).
- **AM handoff** starts when the slowest parallel track finishes (`is_parallel = 0`). Go-live is the end of AM handoff.
- Per-lane, per-track targets are in `stage_benchmarks`, counted in days from onboarding start. A target of 0 means the track isn't required in that lane.
- **Client-level tracks** (Contract, Legal, KYB-KYC, Partner bank approval) are shared across products when a client onboards two products at once. **Product-level tracks** (Tech build, Compliance ops readiness) are tracked per product.
- Track owners: Contract → SALES, Legal → LEGAL, KYB-KYC → COMPLIANCE, Tech build → SOLENG, Compliance ops readiness → COMPOPS, Partner bank approval → PARTNER, AM handoff → AM.
- `stage_status` is one of Not started, In progress, Blocked or Done. A blocked stage carries `blocker_note` and `blocker_since`.

## Tables

| Table | Grain | Key columns |
|---|---|---|
| `teams` | Team | `team_id`, `team_name`, `owns_track` |
| `team_members` | Person slot | `member_id` (e.g. `SOLENG-B`), `team_id`, `member_label`, `role` (Lead/Specialist) |
| `products` | Product | `product_id`, `product_family`, `contract_required`, `partner_bank_required`, `typical_tech_effort` |
| `lanes` | Lane | `lane`, `target_days`, `contract_required`, `routing_rule` |
| `stage_benchmarks` | Lane × track | `target_days` (0 = not required), `is_parallel` |
| `clients` | Client | `client_id`, `segment`, `risk_rating`, `entity_count`, `client_status`, `sales_owner_id`, `am_owner_id`, `deal_closed_date` |
| `client_products` | Client × product journey | `cp_id`, `product_sequence` (1 or 2), `initial_lane`, `current_lane`, `lane_reason`, `onboarding_start_date`, `target_go_live_date`, `forecast_go_live_date` (in flight only), `actual_go_live_date` (live only), `cp_status`, `expected_daily_revenue` |
| `onboarding_stages` | Journey × track | `stage_id`, `cp_id`, `track`, `owner_team_id`, `owner_member_id`, planned, actual and forecast dates, `stage_status`, `blocker_note`, `blocker_since` |
| `revenue_forecast` | Journey × month | `forecast_month`, `month_number`, `expected_revenue`. Ramps at 40%, 70%, 90%, then 100% from the target go-live |
| `daily_transactions` | Live journey × day | `txn_date`, `txn_count`, `volume_usd`, `revenue_usd` |
| `touchpoints` | Client request | `phase` (Pre-sale/Post-sale), `requesting_team_id`, `member_id`, `request_type`, `is_bundled`, `teams_covered` |

Dates are stored as ISO `TEXT`. Use `julianday()` for day arithmetic in SQLite.

## Metric definitions

- **Trapped revenue, in flight:** max(0, forecast go-live − target go-live) × `expected_daily_revenue`.
- **Trapped revenue, historic:** max(0, actual go-live − target go-live) × `expected_daily_revenue`.
- **Ramp shortfall:** the sum of (expected − actual) daily revenue from go-live until the client reaches its run-rate.
- **Time to expected revenue:** days from go-live until the 7-day average revenue reaches 90% of `expected_daily_revenue`.
- **Time to second product:** second product's go-live date − first product's go-live date.
- **Touchpoint breach:** more than 3 post-sale touchpoints for a client. A warning fires at 3. Pre-sale touchpoints don't count.

## Scenarios built into the data

The analysis should surface these:

- SOLENG-B carries a heavier queue (weight 3×) and is a workload bottleneck.
- Stalled journeys escalate to Exceptions: CP-0004 and CP-0005 (from Fast) and CP-0019 (from Complex).
- USDC and high-risk clients wait on Partner bank approval, the longest track.
- Monarch Gig Works (CP-0047) and Cinder Games (CP-0048) plateau at 72–84% of expected revenue and never reach it.
- Six clients breach the touchpoint cap: CL-001, CL-003, CL-012, CL-020, CL-027 and CL-031. About a third of touchpoints are bundled.
- Second products go live 52 to 121 days after the first product.

## Assumptions

- Durations are in calendar days. Revenue is in USD.
- Segment multipliers on daily revenue: SMB 0.4×, Mid-market 1×, Enterprise 2.5×.
- Volume and count come from revenue via per-product take rates and average ticket sizes. Verify is billed per check at $0.80. Weekends run lower.
