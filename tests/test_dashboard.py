"""
Tests for build_dashboard.py. Standard library only.

Run:  python generate_data.py && python -m unittest discover -s tests -v
"""
import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_dashboard  # noqa: E402


class DashboardData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(build_dashboard.DB_PATH):
            raise RuntimeError("control_tower.db not found: run python generate_data.py first")
        cls.con = sqlite3.connect(build_dashboard.DB_PATH)
        cls.data = build_dashboard.build(cls.con)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def scalar(self, sql):
        return self.con.execute(sql).fetchone()[0]

    def test_kpis_equal_views(self):
        k = self.data["kpis"]
        self.assertEqual(k["trapped_in_flight"], round(self.scalar(
            "SELECT SUM(trapped_in_flight_usd) FROM v_trapped_revenue WHERE cp_status = 'Onboarding'")))
        self.assertEqual(k["late"], self.scalar(
            "SELECT COUNT(*) FROM v_trapped_revenue WHERE cp_status = 'Onboarding' AND in_flight_delay_days > 0"))
        self.assertEqual(k["blocked"], self.scalar(
            "SELECT COUNT(DISTINCT work_item_id) FROM v_stage_sla WHERE stage_status = 'Blocked'"))
        self.assertEqual(k["tp_breach"], self.scalar("SELECT COUNT(*) FROM v_touchpoints WHERE cap_status = 'breach'"))

    def test_trapped_bars_sum_to_kpi(self):
        self.assertEqual(sum(r["trapped"] for r in self.data["trapped"]), self.data["kpis"]["trapped_in_flight"])

    def test_every_in_flight_journey_has_stages(self):
        with_stages = {s["cp_id"] for s in self.data["stages"]}
        self.assertEqual({j["cp_id"] for j in self.data["journeys"]}, with_stages)

    def test_sla_rows_match_open_stage_count(self):
        total = sum(r["on track"] + r["at risk"] + r["breached"] for r in self.data["sla"])
        self.assertEqual(total, self.scalar("""
            SELECT SUM(n) FROM (SELECT COUNT(DISTINCT work_item_id) n FROM v_stage_sla
                                WHERE stage_status <> 'Done' GROUP BY track)"""))


class DashboardPage(unittest.TestCase):
    def test_built_page_embeds_parseable_data_and_no_placeholders(self):
        build_dashboard.main()
        with open(build_dashboard.OUT, encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("__AS_OF__", html)
        payload = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
        self.assertNotIn("</", payload)  # cannot close the script element early
        self.assertIn("kpis", json.loads(payload))


if __name__ == "__main__":
    unittest.main()
