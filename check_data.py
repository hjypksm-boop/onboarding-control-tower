"""Quick sanity checks on control_tower.db. Run: python check_data.py"""
import sqlite3

con = sqlite3.connect("control_tower.db")


def q(title, sql):
    cur = con.execute(sql)
    print(f"\n## {title}")
    print(" | ".join(c[0] for c in cur.description))
    for r in cur:
        print(" | ".join(str(x) for x in r))


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
q("Integrity: in-flight journeys forecast in the past (should be 0)",
  "SELECT COUNT(*) FROM client_products WHERE cp_status='Onboarding' AND forecast_go_live_date <= '2026-10-18'")
q("Integrity: Fast lane rows with a contract track (should be 0)", """
SELECT COUNT(*) FROM onboarding_stages s JOIN client_products cp USING (cp_id)
WHERE cp.initial_lane='Fast' AND s.track='Contract'""")
