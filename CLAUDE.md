# CLAUDE.md

A portfolio project: a B2B onboarding-to-revenue control tower for a fintech. It tracks clients from deal close through parallel onboarding tracks, go-live, revenue ramp and second-product adoption. Built with Python and SQL on SQLite, with a schema written to port to Postgres. All companies, people and figures are fictional.

**As-of date:** 2026-10-18 (`TODAY` in `generate_data.py`). Use this date for "today" in queries, not the real clock.

## Rules

- **All numbers are calculated in SQL or Python, never by an LLM.** Any agent or LLM layer may only explain, summarise or route numbers that code has already computed. It must never estimate, add up or invent figures.
- **Use the standard library only** unless the user approves a dependency. Ask before adding any package.
- **Never commit API keys** or other secrets. Keep them in `.env`, which is gitignored.
- Lanes come from `route_lane()`. Never hard-code a lane for a client.
- Don't push to a remote unless the user asks.

## Files and commands

| File | Purpose |
|---|---|
| `schema.sql` | DDL for the 11 tables |
| `metrics.sql` | Metric views: `v_as_of`, `v_stage_sla`, `v_trapped_revenue`, `v_ramp`, `v_second_product`, `v_touchpoints`, `v_owner_workload`. Uses DROP plus CREATE, so it's safe to re-run. Build new metrics here as views |
| `generate_data.py` | Builds `control_tower.db` and `csv/`, then loads `metrics.sql` and checks that `v_as_of` matches `TODAY`. Seeded (`random.seed(42)`), so every run gives the same data |
| `check_data.py` | Sanity checks, plus a query against each metric view. Opens `control_tower.db` by relative path, so run it from the project root |
| `control_tower.db` | Generated and gitignored. Rebuild it rather than editing it |
| `csv/` | One CSV per table. Committed |

```sh
source .venv/bin/activate
python generate_data.py   # rebuilds DB + CSVs (deletes existing DB first)
python check_data.py      # both integrity checks at the end should return 0
```

Expected counts: 40 clients (30 onboarding, 10 live), 49 journeys (35 in flight), 288 stages, 2,885 transaction days, 156 touchpoints.

## Lanes and routing (`route_lane()`)

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
