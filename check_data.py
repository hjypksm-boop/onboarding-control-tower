"""Quick sanity checks on control_tower.db. Run: python check_data.py

Exits with status 1 if any integrity check (should be 0) fails, so CI can gate on it.
"""
import os
import sqlite3
import sys

con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_tower.db"))
failures = []


def q(title, sql):
    cur = con.execute(sql)
    print(f"\n## {title}")
    print(" | ".join(c[0] for c in cur.description))
    rows = cur.fetchall()
    for r in rows:
        print(" | ".join(str(x) for x in r))
    return rows


def zero(title, sql):
    """Integrity check: the query must return a single 0."""
    rows = q(f"Integrity: {title} (should be 0)", sql)
    if rows[0][0] != 0:
        failures.append(title)


q("Journeys in flight by lane", "SELECT current_lane, COUNT(*) n FROM client_products WHERE cp_status='Onboarding' GROUP BY 1")
q("Lane escalations", "SELECT cp_id, initial_lane, current_lane FROM client_products WHERE initial_lane <> current_lane")
q("Stage status", "SELECT stage_status, COUNT(*) n FROM onboarding_stages GROUP BY 1")
q("Blocked stages by owner", "SELECT owner_member_id, COUNT(*) n FROM onboarding_stages WHERE stage_status='Blocked' GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
q("Late journeys and trapped revenue (in flight)", """
SELECT SUM(forecast_go_live_date > target_go_live_date) late, COUNT(*) in_flight,
       ROUND(SUM(MAX(0, julianday(forecast_go_live_date) - julianday(target_go_live_date)) * expected_daily_revenue)) trapped_usd
FROM client_products WHERE cp_status = 'Onboarding'""")
q("Clients breaching touchpoint cap (>3 post-sale)", """
SELECT client_id, COUNT(*) post_sale FROM touchpoints WHERE phase='Post-sale' GROUP BY 1 HAVING COUNT(*) > 3""")
q("Time to expected revenue (days, 7-day avg >= 90% of run-rate)", """
WITH w AS (SELECT cp_id, txn_date,
                  AVG(revenue_usd) OVER (PARTITION BY cp_id ORDER BY txn_date ROWS 6 PRECEDING) a7
           FROM daily_transactions)
SELECT cp.cp_id, cp.product_id,
       CAST(julianday(MIN(CASE WHEN w.a7 >= 0.9 * cp.expected_daily_revenue THEN w.txn_date END))
            - julianday(cp.actual_go_live_date) AS INT) days_to_target
FROM client_products cp JOIN w USING (cp_id) GROUP BY cp.cp_id""")
q("Time to second product", """
SELECT a.client_id, a.product_id first, b.product_id second, b.cp_status,
       CAST(julianday(b.actual_go_live_date) - julianday(a.actual_go_live_date) AS INT) days
FROM client_products a JOIN client_products b
  ON a.client_id = b.client_id AND a.product_sequence = 1 AND b.product_sequence = 2""")
zero("in-flight journeys forecast in the past",
     "SELECT COUNT(*) FROM client_products, v_as_of WHERE cp_status='Onboarding' AND forecast_go_live_date <= as_of_date")
zero("Fast lane rows with a contract track", """
SELECT COUNT(*) FROM onboarding_stages s JOIN client_products cp USING (cp_id)
WHERE cp.initial_lane='Fast' AND s.track='Contract'""")

# ---------------------------------------------------------------------------
# Metric layer (views from metrics.sql)
# ---------------------------------------------------------------------------
print("\n\n# Metric views")
q("v_stage_sla: stages by state and SLA status", """
SELECT CASE WHEN stage_status = 'Done' THEN 'Done' ELSE 'Open' END state, sla_status, COUNT(*) n
FROM v_stage_sla GROUP BY 1, 2 ORDER BY 1, 2""")
q("v_stage_sla: open critical-path stages breached, worst first (top 10)", """
SELECT cp_id, track, owner_member_id, stage_status, target_days, days_used, days_over_target
FROM v_stage_sla WHERE stage_status <> 'Done' AND is_critical_path = 1 AND sla_status = 'breached'
ORDER BY days_over_target DESC LIMIT 10""")
zero("journeys without a critical parallel track", """
SELECT COUNT(*) FROM (SELECT cp_id FROM v_stage_sla GROUP BY cp_id
                      HAVING SUM(is_critical_path = 1 AND track <> 'AM handoff') = 0)""")
q("v_trapped_revenue: totals", """
SELECT cp_status, COUNT(*) journeys,
       SUM(COALESCE(in_flight_delay_days, historic_delay_days) > 0) late,
       ROUND(SUM(trapped_in_flight_usd)) trapped_in_flight_usd, ROUND(SUM(trapped_historic_usd)) trapped_historic_usd
FROM v_trapped_revenue GROUP BY 1""")
q("v_trapped_revenue: top 5 in-flight journeys", """
SELECT cp_id, client_name, product_id, current_lane, in_flight_delay_days, trapped_in_flight_usd
FROM v_trapped_revenue WHERE cp_status = 'Onboarding' ORDER BY trapped_in_flight_usd DESC LIMIT 5""")
zero("view in-flight trapped revenue minus raw-table calculation", """
SELECT ROUND((SELECT SUM(trapped_in_flight_usd) FROM v_trapped_revenue)
             - SUM(MAX(0, julianday(forecast_go_live_date) - julianday(target_go_live_date)) * expected_daily_revenue))
FROM client_products WHERE cp_status = 'Onboarding'""")
q("v_ramp", """
SELECT cp_id, client_name, product_id, days_live, days_to_expected_revenue, ramp_shortfall_usd, latest_pct_of_expected
FROM v_ramp ORDER BY reached_run_rate, days_to_expected_revenue DESC""")
q("v_second_product", """
SELECT client_name, first_product, second_product, second_status, days_to_second_product, forecast_days_to_second_product
FROM v_second_product ORDER BY client_id""")
q("v_touchpoints: clients by cap status", "SELECT cap_status, COUNT(*) clients FROM v_touchpoints GROUP BY 1 ORDER BY 2 DESC")
q("v_touchpoints: warnings and breaches", """
SELECT client_id, client_name, post_sale_touchpoints, bundled_post_sale, cap_status
FROM v_touchpoints WHERE cap_status <> 'ok' ORDER BY post_sale_touchpoints DESC, client_id""")
q("v_owner_workload: members with open stages, by workload rank (in progress + blocked)", """
SELECT workload_rank, member_id, active, in_progress, blocked, not_started, breached, at_risk, on_critical_path, oldest_blocker_days
FROM v_owner_workload WHERE open_stages > 0 ORDER BY workload_rank""")

q("v_week_summary: this week (7 days to the as-of date)", "SELECT * FROM v_week_summary")
zero("this-week events outside the 7-day window", """
SELECT COUNT(*) FROM (
    SELECT event_date FROM v_week_new_blockers UNION ALL SELECT event_date FROM v_week_completed_stages
    UNION ALL SELECT event_date FROM v_week_new_breaches UNION ALL SELECT event_date FROM v_week_missed_targets
    UNION ALL SELECT event_date FROM v_week_went_live) e, v_week w
WHERE e.event_date NOT BETWEEN w.week_start AND w.week_end""")

print()
if failures:
    print(f"FAILED integrity checks: {', '.join(failures)}")
    sys.exit(1)
print("All integrity checks passed.")
