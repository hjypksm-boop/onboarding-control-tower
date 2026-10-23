-- =====================================================================
-- B2B Onboarding-to-Revenue Control Tower : schema (SQLite / Postgres-friendly)
-- =====================================================================

-- ---------- Reference tables ----------

CREATE TABLE teams (
    team_id        TEXT PRIMARY KEY,          -- e.g. LEGAL
    team_name      TEXT NOT NULL,             -- e.g. Legal
    owns_track     TEXT                       -- onboarding track this team owns
);

CREATE TABLE team_members (
    member_id      TEXT PRIMARY KEY,          -- e.g. LEGAL-A
    team_id        TEXT NOT NULL REFERENCES teams(team_id),
    member_label   TEXT NOT NULL,             -- e.g. Legal - Member A
    email          TEXT NOT NULL,
    role           TEXT NOT NULL              -- Lead / Specialist
);

CREATE TABLE products (
    product_id              TEXT PRIMARY KEY, -- e.g. PAYOUT
    product_name            TEXT NOT NULL,
    product_family          TEXT NOT NULL,    -- Fiat / Crypto / Identity / Cards
    contract_required       INTEGER NOT NULL, -- 1 = needs signed contract
    partner_bank_required   INTEGER NOT NULL, -- 1 = needs partner bank sign-off
    typical_tech_effort     TEXT NOT NULL     -- Low / Medium / High
);

CREATE TABLE lanes (
    lane                TEXT PRIMARY KEY,     -- Fast / Standard / Complex / Exceptions
    target_days         INTEGER NOT NULL,     -- onboarding start -> go-live
    contract_required   INTEGER NOT NULL,
    routing_rule        TEXT NOT NULL
);

-- Target duration per parallel track per lane (days from onboarding start)
CREATE TABLE stage_benchmarks (
    lane            TEXT NOT NULL REFERENCES lanes(lane),
    track           TEXT NOT NULL,
    target_days     INTEGER NOT NULL,         -- 0 = track not required in this lane
    is_parallel     INTEGER NOT NULL,         -- 0 = runs after parallel tracks (AM handoff)
    PRIMARY KEY (lane, track)
);

-- ---------- Core tables ----------

CREATE TABLE clients (
    client_id          TEXT PRIMARY KEY,      -- e.g. CL-001
    client_name        TEXT NOT NULL,
    industry           TEXT NOT NULL,
    segment            TEXT NOT NULL,         -- SMB / Mid-market / Enterprise
    hq_country         TEXT NOT NULL,
    risk_rating        TEXT NOT NULL,         -- Low / Medium / High
    entity_count       INTEGER NOT NULL,      -- legal entities to onboard
    client_status      TEXT NOT NULL,         -- Onboarding / Live
    sales_owner_id     TEXT REFERENCES team_members(member_id),
    am_owner_id        TEXT REFERENCES team_members(member_id),
    deal_closed_date   DATE NOT NULL          -- commercial agreement in principle
);

-- One row per client x product onboarding journey
CREATE TABLE client_products (
    cp_id                   TEXT PRIMARY KEY, -- e.g. CP-0001
    client_id               TEXT NOT NULL REFERENCES clients(client_id),
    product_id              TEXT NOT NULL REFERENCES products(product_id),
    product_sequence        INTEGER NOT NULL, -- 1 = first product, 2 = second product
    initial_lane            TEXT NOT NULL REFERENCES lanes(lane),
    current_lane            TEXT NOT NULL REFERENCES lanes(lane),
    lane_reason             TEXT NOT NULL,
    contract_signed_date    DATE,             -- NULL for Fast lane (no contract)
    onboarding_start_date   DATE NOT NULL,
    target_go_live_date     DATE NOT NULL,
    forecast_go_live_date   DATE,             -- latest projection (in-flight only)
    actual_go_live_date     DATE,             -- NULL until live
    cp_status               TEXT NOT NULL,    -- Onboarding / Live
    expected_daily_revenue  REAL NOT NULL     -- run-rate revenue (USD) once ramped
);

-- Parallel tracks per onboarding journey
CREATE TABLE onboarding_stages (
    stage_id            TEXT PRIMARY KEY,     -- e.g. ST-00001
    cp_id               TEXT NOT NULL REFERENCES client_products(cp_id),
    track               TEXT NOT NULL,        -- Contract / Legal / KYB-KYC / Tech build /
                                              -- Compliance ops readiness / Partner bank approval / AM handoff
    owner_team_id       TEXT NOT NULL REFERENCES teams(team_id),
    owner_member_id     TEXT NOT NULL REFERENCES team_members(member_id),
    planned_start_date  DATE NOT NULL,
    planned_end_date    DATE NOT NULL,
    actual_start_date   DATE,
    actual_end_date     DATE,
    forecast_end_date   DATE,                 -- projection for unfinished tracks
    stage_status        TEXT NOT NULL,        -- Not started / In progress / Blocked / Done
    blocker_note        TEXT,
    blocker_since       DATE
);

-- Monthly revenue plan per journey (ramp curve from target go-live)
CREATE TABLE revenue_forecast (
    cp_id               TEXT NOT NULL REFERENCES client_products(cp_id),
    forecast_month      DATE NOT NULL,        -- first day of month
    month_number        INTEGER NOT NULL,     -- 1 = first month after target go-live
    expected_revenue    REAL NOT NULL,
    PRIMARY KEY (cp_id, forecast_month)
);

-- Daily actuals for live journeys
CREATE TABLE daily_transactions (
    cp_id               TEXT NOT NULL REFERENCES client_products(cp_id),
    txn_date            DATE NOT NULL,
    txn_count           INTEGER NOT NULL,
    volume_usd          REAL NOT NULL,
    revenue_usd         REAL NOT NULL,
    PRIMARY KEY (cp_id, txn_date)
);

-- Documented client-facing requests (post-sale cap = 3)
CREATE TABLE touchpoints (
    touchpoint_id       TEXT PRIMARY KEY,     -- e.g. TP-0001
    client_id           TEXT NOT NULL REFERENCES clients(client_id),
    cp_id               TEXT REFERENCES client_products(cp_id),
    touchpoint_date     DATE NOT NULL,
    phase               TEXT NOT NULL,        -- Pre-sale / Post-sale
    requesting_team_id  TEXT NOT NULL REFERENCES teams(team_id),
    member_id           TEXT NOT NULL REFERENCES team_members(member_id),
    channel             TEXT NOT NULL,        -- Email / Call / Portal
    request_type        TEXT NOT NULL,        -- Document request / Information request / Sign-off
    description         TEXT NOT NULL,
    is_bundled          INTEGER NOT NULL,     -- 1 = consolidated multi-team ask
    teams_covered       TEXT NOT NULL         -- comma-separated teams included in this ask
);

CREATE INDEX idx_cp_client      ON client_products(client_id);
CREATE INDEX idx_stage_cp       ON onboarding_stages(cp_id);
CREATE INDEX idx_txn_cp_date    ON daily_transactions(cp_id, txn_date);
CREATE INDEX idx_tp_client      ON touchpoints(client_id);
