-- =====================================================================
-- B2B Onboarding-to-Revenue Control Tower : metric layer (SQLite views)
-- Loaded by generate_data.py after the data is inserted. Safe to re-run.
-- Metric definitions follow README.md. All durations are calendar days.
-- =====================================================================

DROP VIEW IF EXISTS v_owner_workload;
DROP VIEW IF EXISTS v_touchpoints;
DROP VIEW IF EXISTS v_second_product;
DROP VIEW IF EXISTS v_ramp;
DROP VIEW IF EXISTS v_trapped_revenue;
DROP VIEW IF EXISTS v_stage_sla;
DROP VIEW IF EXISTS v_as_of;

-- "Today" for every calculation. Must match TODAY in generate_data.py,
-- which checks it after loading this file.
CREATE VIEW v_as_of AS SELECT '2026-10-18' AS as_of_date;

-- ---------- Stage SLA and critical path ----------
-- target_days  = planned end - planned start (the stage_benchmarks target for the routed lane)
-- days_used    = days from start (actual, else planned) to actual end (Done) or the as-of date (open)
-- sla_status   = breached (days_used > target), at risk (open and >= 75% of target used), else on track
-- is_critical_path = the parallel track(s) finishing last for the journey, plus AM handoff,
--                    which always runs after them
-- work_item_id = client-level tracks (Contract, Legal, KYB-KYC, Partner bank approval) are shared by
--                products onboarded together, so they get one id per client, track and start date
CREATE VIEW v_stage_sla AS
WITH base AS (
    SELECT s.*, cp.client_id, cp.current_lane, cp.cp_status,
           CAST(julianday(s.planned_end_date) - julianday(s.planned_start_date) AS INTEGER) AS target_days,
           COALESCE(s.actual_end_date, s.forecast_end_date) AS expected_end_date,
           MAX(0, CAST(julianday(CASE WHEN s.stage_status = 'Done' THEN s.actual_end_date ELSE a.as_of_date END)
                       - julianday(COALESCE(s.actual_start_date, s.planned_start_date)) AS INTEGER)) AS days_used,
           CASE WHEN s.track IN ('Contract', 'Legal', 'KYB-KYC', 'Partner bank approval')
                THEN cp.client_id || ':' || s.track || ':' || cp.onboarding_start_date
                ELSE s.stage_id END AS work_item_id
    FROM onboarding_stages s
    JOIN client_products cp USING (cp_id)
    CROSS JOIN v_as_of a
)
SELECT stage_id, cp_id, client_id, current_lane, cp_status, track, work_item_id,
       owner_team_id, owner_member_id, stage_status,
       planned_start_date, planned_end_date, actual_start_date, actual_end_date, forecast_end_date,
       expected_end_date, target_days, days_used,
       days_used - target_days AS days_over_target,
       ROUND(1.0 * days_used / NULLIF(target_days, 0), 2) AS pct_target_used,
       CASE WHEN days_used > target_days THEN 'breached'
            WHEN stage_status <> 'Done' AND days_used >= 0.75 * target_days THEN 'at risk'
            ELSE 'on track' END AS sla_status,
       CASE WHEN track = 'AM handoff' THEN 1
            WHEN expected_end_date = MAX(CASE WHEN track <> 'AM handoff' THEN expected_end_date END)
                                     OVER (PARTITION BY cp_id) THEN 1
            ELSE 0 END AS is_critical_path,
       blocker_note, blocker_since
FROM base;

-- ---------- Trapped revenue ----------
-- In flight: max(0, forecast go-live - target go-live) x expected daily revenue
-- Historic:  max(0, actual go-live - target go-live) x expected daily revenue
-- Columns that don't apply to the journey's status are NULL.
CREATE VIEW v_trapped_revenue AS
WITH delay AS (
    SELECT cp_id,
           CASE WHEN cp_status = 'Onboarding'
                THEN MAX(0, CAST(julianday(forecast_go_live_date) - julianday(target_go_live_date) AS INTEGER))
           END AS in_flight_delay_days,
           CASE WHEN cp_status = 'Live'
                THEN MAX(0, CAST(julianday(actual_go_live_date) - julianday(target_go_live_date) AS INTEGER))
           END AS historic_delay_days
    FROM client_products
)
SELECT cp.cp_id, cp.client_id, c.client_name, cp.product_id, cp.product_sequence, cp.cp_status,
       cp.initial_lane, cp.current_lane, cp.target_go_live_date, cp.forecast_go_live_date,
       cp.actual_go_live_date, cp.expected_daily_revenue,
       d.in_flight_delay_days,
       ROUND(d.in_flight_delay_days * cp.expected_daily_revenue, 2) AS trapped_in_flight_usd,
       d.historic_delay_days,
       ROUND(d.historic_delay_days * cp.expected_daily_revenue, 2) AS trapped_historic_usd
FROM client_products cp
JOIN delay d USING (cp_id)
JOIN clients c USING (client_id);

-- ---------- Revenue ramp ----------
-- One row per live journey with transactions.
-- run_rate_date            = first day the full 7-day average revenue reaches 90% of expected_daily_revenue
-- days_to_expected_revenue = run_rate_date - go-live (NULL if not yet reached)
-- ramp_shortfall_usd       = sum of (expected - actual) daily revenue from go-live up to, not including,
--                            run_rate_date; to date for journeys that haven't reached it
CREATE VIEW v_ramp AS
WITH daily AS (
    SELECT t.cp_id, t.txn_date, t.revenue_usd, cp.expected_daily_revenue,
           AVG(t.revenue_usd) OVER w AS avg_7d,
           COUNT(*) OVER w AS days_in_window
    FROM daily_transactions t
    JOIN client_products cp USING (cp_id)
    WINDOW w AS (PARTITION BY t.cp_id ORDER BY t.txn_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
), reached AS (
    SELECT cp_id, MIN(txn_date) AS run_rate_date
    FROM daily
    WHERE days_in_window = 7 AND avg_7d >= 0.9 * expected_daily_revenue
    GROUP BY cp_id
), latest AS (
    SELECT cp_id, avg_7d AS latest_avg_7d
    FROM (SELECT cp_id, avg_7d, ROW_NUMBER() OVER (PARTITION BY cp_id ORDER BY txn_date DESC) rn FROM daily)
    WHERE rn = 1
)
SELECT cp.cp_id, cp.client_id, c.client_name, cp.product_id, cp.product_sequence,
       cp.actual_go_live_date, cp.expected_daily_revenue,
       COUNT(*) AS days_live,
       r.run_rate_date,
       CAST(julianday(r.run_rate_date) - julianday(cp.actual_go_live_date) AS INTEGER) AS days_to_expected_revenue,
       CASE WHEN r.run_rate_date IS NULL THEN 0 ELSE 1 END AS reached_run_rate,
       ROUND(SUM(CASE WHEN r.run_rate_date IS NULL OR d.txn_date < r.run_rate_date
                      THEN d.expected_daily_revenue - d.revenue_usd ELSE 0 END), 2) AS ramp_shortfall_usd,
       ROUND(l.latest_avg_7d, 2) AS latest_avg_7d_usd,
       ROUND(l.latest_avg_7d / cp.expected_daily_revenue, 2) AS latest_pct_of_expected
FROM daily d
JOIN client_products cp USING (cp_id)
JOIN clients c ON c.client_id = cp.client_id
JOIN latest l ON l.cp_id = cp.cp_id
LEFT JOIN reached r ON r.cp_id = cp.cp_id
GROUP BY cp.cp_id;

-- ---------- Time to second product ----------
-- Second product go-live - first product go-live. For a second product still onboarding,
-- days_to_second_product is NULL and forecast_days_to_second_product uses its forecast go-live.
CREATE VIEW v_second_product AS
SELECT a.client_id, c.client_name,
       a.cp_id AS first_cp_id, a.product_id AS first_product, a.actual_go_live_date AS first_go_live_date,
       b.cp_id AS second_cp_id, b.product_id AS second_product, b.cp_status AS second_status,
       b.onboarding_start_date AS second_start_date,
       b.actual_go_live_date AS second_go_live_date, b.forecast_go_live_date AS second_forecast_go_live_date,
       CAST(julianday(b.actual_go_live_date) - julianday(a.actual_go_live_date) AS INTEGER) AS days_to_second_product,
       CAST(julianday(b.forecast_go_live_date) - julianday(a.actual_go_live_date) AS INTEGER) AS forecast_days_to_second_product
FROM client_products a
JOIN client_products b ON b.client_id = a.client_id AND a.product_sequence = 1 AND b.product_sequence = 2
JOIN clients c ON c.client_id = a.client_id;

-- ---------- Touchpoint cap ----------
-- Post-sale touchpoints per client: ok (< 3), warning (= 3), breach (> 3). Pre-sale ones don't count.
CREATE VIEW v_touchpoints AS
SELECT *,
       CASE WHEN post_sale_touchpoints > 3 THEN 'breach'
            WHEN post_sale_touchpoints = 3 THEN 'warning'
            ELSE 'ok' END AS cap_status
FROM (
    SELECT c.client_id, c.client_name, c.client_status,
           COUNT(CASE WHEN t.phase = 'Pre-sale' THEN 1 END) AS pre_sale_touchpoints,
           COUNT(CASE WHEN t.phase = 'Post-sale' THEN 1 END) AS post_sale_touchpoints,
           COUNT(CASE WHEN t.phase = 'Post-sale' AND t.is_bundled = 1 THEN 1 END) AS bundled_post_sale,
           MAX(CASE WHEN t.phase = 'Post-sale' THEN t.touchpoint_date END) AS last_post_sale_date
    FROM clients c
    LEFT JOIN touchpoints t USING (client_id)
    GROUP BY c.client_id
);

-- ---------- Owner workload ----------
-- Open (not Done) stages per team member, counted as distinct work items so a shared
-- client-level track for two products counts once. Every member appears, even with no open work.
CREATE VIEW v_owner_workload AS
SELECT m.member_id, m.member_label, m.team_id, m.role,
       COUNT(DISTINCT s.work_item_id) AS open_stages,
       COUNT(DISTINCT CASE WHEN s.stage_status = 'In progress' THEN s.work_item_id END) AS in_progress,
       COUNT(DISTINCT CASE WHEN s.stage_status = 'Blocked' THEN s.work_item_id END) AS blocked,
       COUNT(DISTINCT CASE WHEN s.stage_status = 'Not started' THEN s.work_item_id END) AS not_started,
       COUNT(DISTINCT CASE WHEN s.sla_status = 'breached' THEN s.work_item_id END) AS breached,
       COUNT(DISTINCT CASE WHEN s.sla_status = 'at risk' THEN s.work_item_id END) AS at_risk,
       COUNT(DISTINCT CASE WHEN s.is_critical_path = 1 THEN s.work_item_id END) AS on_critical_path,
       MAX(CAST(julianday(a.as_of_date) - julianday(s.blocker_since) AS INTEGER)) AS oldest_blocker_days
FROM team_members m
CROSS JOIN v_as_of a
LEFT JOIN v_stage_sla s ON s.owner_member_id = m.member_id AND s.stage_status <> 'Done'
GROUP BY m.member_id;
