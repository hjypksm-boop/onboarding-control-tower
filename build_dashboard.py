"""
Build the static control tower dashboard: docs/index.html (served by GitHub Pages).

Every figure is queried from the metric views (metrics.sql); the page only draws them.
Standard library only. Deterministic: the same database always gives the same file,
so CI can check the committed page is up to date.

Run:  python generate_data.py && python build_dashboard.py
"""
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "control_tower.db")
TEMPLATE = os.path.join(HERE, "dashboard", "template.html")
OUT = os.path.join(HERE, "docs", "index.html")

TRACKS = ["Contract", "Legal", "KYB-KYC", "Tech build", "Compliance ops readiness",
          "Partner bank approval", "AM handoff"]


def query(con, sql):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql)]


def build(con):
    one = lambda sql: query(con, sql)[0]  # noqa: E731
    kpis = {
        **one("SELECT as_of_date FROM v_as_of"),
        **one("""
            SELECT SUM(cp_status = 'Onboarding') in_flight,
                   SUM(cp_status = 'Onboarding' AND in_flight_delay_days > 0) late,
                   ROUND(SUM(trapped_in_flight_usd)) trapped_in_flight,
                   SUM(cp_status = 'Live') live,
                   SUM(cp_status = 'Live' AND historic_delay_days > 0) late_live,
                   ROUND(SUM(trapped_historic_usd)) trapped_historic
            FROM v_trapped_revenue"""),
        **one("""
            SELECT COUNT(DISTINCT CASE WHEN sla_status = 'breached' THEN work_item_id END) breached,
                   COUNT(DISTINCT CASE WHEN sla_status = 'breached' AND is_critical_path = 1
                                       THEN work_item_id END) breached_cp,
                   COUNT(DISTINCT CASE WHEN sla_status = 'at risk' THEN work_item_id END) at_risk,
                   COUNT(DISTINCT CASE WHEN stage_status = 'Blocked' THEN work_item_id END) blocked
            FROM v_stage_sla WHERE stage_status <> 'Done'"""),
        **one("""
            SELECT SUM(cap_status = 'breach') tp_breach, SUM(cap_status = 'warning') tp_warning,
                   COUNT(*) clients
            FROM v_touchpoints"""),
        **one("SELECT COUNT(*) ramp_live, SUM(reached_run_rate = 0) ramp_missed FROM v_ramp"),
    }
    trapped = query(con, """
        SELECT cp_id, client_name, product_id, current_lane, in_flight_delay_days,
               ROUND(trapped_in_flight_usd) trapped
        FROM v_trapped_revenue WHERE cp_status = 'Onboarding' AND trapped_in_flight_usd > 0
        ORDER BY trapped_in_flight_usd DESC, cp_id""")
    sla_rows = query(con, """
        SELECT track, sla_status, COUNT(DISTINCT work_item_id) n
        FROM v_stage_sla WHERE stage_status <> 'Done' GROUP BY 1, 2""")
    sla = [{"track": t, **{s: next((r["n"] for r in sla_rows if r["track"] == t and r["sla_status"] == s), 0)
                           for s in ("on track", "at risk", "breached")}}
           for t in TRACKS if any(r["track"] == t for r in sla_rows)]
    journeys = query(con, """
        SELECT t.cp_id, t.client_name, t.product_id, t.current_lane, cp.onboarding_start_date,
               t.target_go_live_date, t.forecast_go_live_date, COALESCE(t.in_flight_delay_days, 0) delay_days,
               COALESCE(ROUND(t.trapped_in_flight_usd), 0) trapped
        FROM v_trapped_revenue t JOIN client_products cp USING (cp_id)
        WHERE t.cp_status = 'Onboarding'
        ORDER BY t.trapped_in_flight_usd DESC, t.cp_id""")
    stages = query(con, """
        SELECT s.cp_id, s.track, s.owner_member_id, s.stage_status, s.sla_status, s.is_critical_path,
               CASE WHEN s.track = 'AM handoff' AND s.actual_start_date IS NULL
                    THEN (SELECT MAX(x.expected_end_date) FROM v_stage_sla x
                          WHERE x.cp_id = s.cp_id AND x.track <> 'AM handoff')
                    ELSE COALESCE(s.actual_start_date, s.planned_start_date) END AS start_date,
               s.expected_end_date AS end_date, s.planned_end_date, s.target_days, s.days_used,
               s.blocker_note, s.days_blocked
        FROM v_stage_sla s WHERE s.cp_status = 'Onboarding'
        ORDER BY s.cp_id, s.stage_id""")
    ramp_series = query(con, """
        SELECT r.cp_id, r.client_name, r.product_id, r.reached_run_rate, r.days_to_expected_revenue,
               r.latest_pct_of_expected, r.ramp_shortfall_usd
        FROM v_ramp r ORDER BY r.reached_run_rate, r.ramp_shortfall_usd DESC""")
    ramp_points = query(con, """
        WITH w AS (
            SELECT t.cp_id, CAST(julianday(t.txn_date) - julianday(cp.actual_go_live_date) AS INTEGER) day,
                   AVG(t.revenue_usd) OVER win / cp.expected_daily_revenue pct, COUNT(*) OVER win n
            FROM daily_transactions t JOIN client_products cp USING (cp_id)
            WINDOW win AS (PARTITION BY t.cp_id ORDER BY t.txn_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW))
        SELECT cp_id, day, ROUND(pct * 100, 1) pct FROM w WHERE n = 7 ORDER BY cp_id, day""")
    for s in ramp_series:
        s["points"] = [[p["day"], p["pct"]] for p in ramp_points if p["cp_id"] == s["cp_id"]]
    touch_dist = query(con, """
        SELECT post_sale_touchpoints n, cap_status, COUNT(*) clients
        FROM v_touchpoints GROUP BY 1, 2 ORDER BY 1""")
    touch_flagged = query(con, """
        SELECT client_id, client_name, client_status, post_sale_touchpoints, bundled_post_sale, cap_status
        FROM v_touchpoints WHERE cap_status <> 'ok'
        ORDER BY post_sale_touchpoints DESC, client_id""")
    workload = query(con, """
        SELECT member_id, member_label, in_progress, blocked, not_started, breached, at_risk, oldest_blocker_days
        FROM v_owner_workload WHERE open_stages > 0 ORDER BY workload_rank""")
    second = query(con, """
        SELECT client_name, first_product, second_product, second_status,
               days_to_second_product, forecast_days_to_second_product
        FROM v_second_product ORDER BY client_name""")
    return {"kpis": kpis, "trapped": trapped, "sla": sla, "journeys": journeys, "stages": stages,
            "ramp": ramp_series, "touch_dist": touch_dist, "touch_flagged": touch_flagged,
            "workload": workload, "second": second}


def main():
    con = sqlite3.connect(DB_PATH)
    data = build(con)
    con.close()
    # "</" is escaped so the JSON can't close the <script> element it is embedded in
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read().replace("__DATA__", payload).replace("__AS_OF__", data["kpis"]["as_of_date"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard written to {OUT} ({len(html) // 1024} KB, as of {data['kpis']['as_of_date']})")


if __name__ == "__main__":
    main()
