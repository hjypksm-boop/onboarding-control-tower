# B2B Onboarding-to-Revenue Control Tower — Data set and schema

A fictional data set for a control tower that tracks B2B clients from deal close through parallel onboarding, go-live, revenue ramp and second-product adoption. All companies, people and figures are made up.

**As-of date:** 18 Oct 2026. **Database:** SQLite (`control_tower.db`); the schema is written to port to Postgres.

## Files

| File | Purpose |
|---|---|
| `schema.sql` | Table definitions (11 tables) |
| `metrics.sql` | Metric layer: SQLite views over the tables (see below) |
| `generate_data.py` | Builds the database and CSVs, then loads the metric views. Standard library only, seeded, so every run gives the same data |
| `check_data.py` | Sanity checks, plus a query against each metric view |
| `control_tower.db` | Generated SQLite database |
| `csv/` | One CSV per table |

Regenerate: `python generate_data.py`, then `python check_data.py`.

## What's in it

- **40 clients**: 30 currently onboarding and 10 live.
- **49 client-product journeys**: 35 in flight, including 2 live clients onboarding a second product.
- **8 products**: Pay-ins, Payouts, Wallets, USDC Pay-in, USDC Payout, Verify, Cards, Travel.
- **7 teams, 17 members**: Sales, Legal, Compliance (KYB/KYC), Solutions Engineering, Compliance Ops, Partner Banking, Account Management. Members are anonymised, for example "Legal - Member A".
- **About 2,900 days of transactions** for the live journeys, plus a 12-month revenue forecast for each journey.
- **156 touchpoints**: pre-sale ones (not capped) and post-sale ones (capped at 3).

## Lanes

Lanes are derived from client attributes by `route_lane()`, never hard-coded.

| Lane | Rule | Target |
|---|---|---|
| Fast | Single no-contract product (Verify, Wallets, Pay-ins), low risk, SMB or mid-market, single entity | 10 days |
| Standard | Contracted single product, low or medium risk | 30 days |
| Complex | Cards or Travel, multi-product onboarding, or 3+ legal entities | 60 days |
| Exceptions | USDC products or high-risk KYB (partner bank sign-off), or any journey stalled beyond 1.5× its lane target | 75 days |

`client_products.initial_lane` stores the routed lane. `current_lane` shows escalations, such as a stalled Fast journey that moved to Exceptions.

## Onboarding model

Tracks start together on the onboarding start date and run in parallel: Contract, Legal, KYB-KYC, Tech build, Compliance ops readiness, and Partner bank approval (Exceptions only). AM handoff starts once the slowest track finishes. Go-live is the end of AM handoff.

Per-lane, per-track targets are in `stage_benchmarks`. Fast lane journeys have no Contract track. Client-level tracks (Contract, Legal, KYB-KYC, Partner bank) are shared when a client onboards two products at once. Tech build and Compliance ops readiness are tracked per product.

## Tables

| Table | Grain | Key columns |
|---|---|---|
| `teams` | Team | `team_id`, `owns_track` |
| `team_members` | Person slot | `member_id` (e.g. `SOLENG-B`), `member_label`, `role` |
| `products` | Product | `contract_required`, `partner_bank_required`, `typical_tech_effort` |
| `lanes` | Lane | `target_days`, `routing_rule` |
| `stage_benchmarks` | Lane × track | `target_days` (0 = not required), `is_parallel` |
| `clients` | Client | `segment`, `risk_rating`, `entity_count`, `client_status`, `sales_owner_id`, `am_owner_id` |
| `client_products` | Client × product journey | `product_sequence` (1st or 2nd product), `initial_lane`, `current_lane`, `target_go_live_date`, `forecast_go_live_date`, `actual_go_live_date`, `expected_daily_revenue` |
| `onboarding_stages` | Journey × track | `owner_team_id`, `owner_member_id`, planned, actual and forecast dates, `stage_status`, `blocker_note`, `blocker_since` |
| `revenue_forecast` | Journey × month | `month_number`, `expected_revenue`. Ramps at 40%, 70%, 90%, then 100% from the target go-live |
| `daily_transactions` | Live journey × day | `txn_count`, `volume_usd`, `revenue_usd` |
| `touchpoints` | Client request | `phase`, `requesting_team_id`, `request_type`, `is_bundled`, `teams_covered` |

## Metric definitions

These are implemented as views in `metrics.sql`. `v_as_of` holds the as-of date.

| View | Grain | What it gives |
|---|---|---|
| `v_stage_sla` | Stage | `sla_status`: on track, at risk (open with 75% or more of its target used) or breached. Also `is_critical_path`: the last-finishing parallel track plus AM handoff |
| `v_trapped_revenue` | Journey | In-flight and historic delay days and trapped revenue |
| `v_ramp` | Live journey | Ramp shortfall, time to expected revenue, latest 7-day average as a share of expected revenue |
| `v_second_product` | Client with a 2nd product | Days from first to second go-live, or forecast days if still onboarding |
| `v_touchpoints` | Client | Post-sale touchpoint count and `cap_status`: ok, warning or breach |
| `v_owner_workload` | Team member | Open, in-progress, blocked, not-started, breached, at-risk and critical-path stages. A shared client-level track counts once |


- **Trapped revenue, in flight:** max(0, forecast go-live − target go-live) × expected daily revenue.
- **Trapped revenue, historic:** max(0, actual go-live − target go-live) × expected daily revenue.
- **Ramp shortfall:** the sum of (expected − actual) daily revenue from go-live until the client reaches its run-rate.
- **Time to expected revenue:** days from go-live until the 7-day average revenue reaches 90% of `expected_daily_revenue`.
- **Time to second product:** the second product's go-live date minus the first product's go-live date.
- **Touchpoint breach:** more than 3 post-sale touchpoints for a client. A warning fires at 3.

## Scenarios built into the data

These give the agents and dashboard something real to find:

- **Solutions Engineering Member B** carries a heavier queue, which shows up as a workload bottleneck.
- **Stalled journeys** escalate from Fast or Complex into Exceptions.
- **USDC and high-risk clients** wait on Partner bank approval, which is the longest track.
- **Two live clients** (Monarch Gig Works and Cinder Games) plateau below their expected revenue and never reach it.
- **Six clients** breach the 3-touchpoint cap. About a third of touchpoints are bundled, multi-team asks.
- **Second-product journeys** take from 52 to 121 days after the first product goes live.

## Agent layer

`agents.py` runs four agents on top of the views. The LLM is `openai/gpt-oss-120b` on Groq (override with `GROQ_MODEL`), called through `llm.py`. **Every figure comes from the SQL views, and every sentence that states or combines figures is written by code.**

| Agent | What it does | Code writes | LLM drafts |
|---|---|---|---|
| `classify` | Tags each blocked stage as waiting on Client, Internal or Partner bank | Keyword rules decide the clear cases | A label for edge cases (no keyword match, or conflicting matches), which must be one of the allowed labels |
| `blockers` | Blocked stages, grouped by who we're waiting on | The grouped list with days blocked, SLA and critical path | One next action per group |
| `nudges` | One message per owner with breached, at-risk or blocked stages | To, Subject, stage bullets, the ask, sign-off | The opening line: greeting and which stage to tackle first |
| `brief` | Daily brief | Title, headline, and every section: revenue at risk, onboarding health, live revenue, touchpoints, team load | The recommended actions |

### LLM guardrails

- **Number guard:** LLM drafts may not contain any digits apart from record IDs such as `CP-0024`. A draft that includes a count, amount, percentage, duration or date is rejected, and the code-written fallback is used. The LLM therefore can't restate figures, or combine them wrongly.
- **Code-owned facts and headline:** the brief's headline and every line with a figure come from SQL queries against the views. `tests/test_agents.py` checks that the headline's figures equal the views exactly, and that turning the LLM on changes no line containing a figure.
- **Human review label:** every piece of LLM-written text starts with `[DRAFT – review before sending]`. Nothing is sent automatically.
- **Template fallback:** with no `GROQ_API_KEY`, or if the API call fails, code-written text replaces each draft (no label), so the app runs offline.

Setup:

```sh
pip install -r requirements.txt
cp .env.example .env      # then set GROQ_API_KEY; .env is gitignored
python agents.py          # all agents, or: classify | blockers | nudges | brief
python -m unittest discover -s tests -v   # guardrail tests (no API calls)
```

## Assumptions

- All durations are calendar days.
- Revenue is in USD. Daily revenue depends on product and segment, with segment multipliers of SMB 0.4×, mid-market 1×, enterprise 2.5×.
- Transaction volume and count are derived from revenue using per-product take rates and average ticket sizes. Weekends run lower.
