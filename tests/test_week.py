"""
Tests for the "This week" views (metrics.sql), the brief's This week section and the dashboard card.
Standard library only; no API calls.

Run:  python generate_data.py && python -m unittest discover -s tests -v
"""
import os
import re
import sqlite3
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents  # noqa: E402
import build_dashboard  # noqa: E402
import llm  # noqa: E402

EVENT_VIEWS = ["v_week_new_blockers", "v_week_completed_stages", "v_week_new_breaches",
               "v_week_missed_targets", "v_week_went_live"]


class WeekViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(agents.DB_PATH):
            raise RuntimeError("control_tower.db not found: run python generate_data.py first")
        cls.con = sqlite3.connect(agents.DB_PATH)
        cls.con.row_factory = sqlite3.Row
        cls.as_of = cls.con.execute("SELECT as_of_date FROM v_as_of").fetchone()[0]
        cls.summary = dict(cls.con.execute("SELECT * FROM v_week_summary").fetchone())

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def rows(self, view):
        return [dict(r) for r in self.con.execute(f"SELECT * FROM {view}")]

    def test_window_is_the_seven_days_ending_on_the_as_of_date(self):
        start, end = date.fromisoformat(self.summary["week_start"]), date.fromisoformat(self.summary["week_end"])
        self.assertEqual(self.summary["week_end"], self.as_of)
        self.assertEqual(end - start, timedelta(days=6))

    def test_every_event_falls_inside_the_window(self):
        for view in EVENT_VIEWS:
            for r in self.rows(view):
                self.assertTrue(self.summary["week_start"] <= r["event_date"] <= self.summary["week_end"], (view, r))

    def test_new_blockers_match_the_raw_stage_table(self):
        # independent of v_stage_sla: shared client-level tracks count once per client, track and start date
        raw = self.con.execute("""
            SELECT COUNT(DISTINCT CASE WHEN s.track IN ('Contract', 'Legal', 'KYB-KYC', 'Partner bank approval')
                                       THEN cp.client_id || s.track || cp.onboarding_start_date ELSE s.stage_id END)
            FROM onboarding_stages s JOIN client_products cp USING (cp_id)
            WHERE s.stage_status = 'Blocked' AND s.blocker_since BETWEEN date(?, '-6 days') AND ?""",
                               (self.as_of, self.as_of)).fetchone()[0]
        self.assertEqual(self.summary["new_blockers"], raw)
        self.assertGreater(raw, 0)

    def test_missed_targets_are_in_flight_and_forecast_after_target(self):
        for r in self.rows("v_week_missed_targets"):
            self.assertGreater(r["forecast_go_live_date"], r["event_date"])
            self.assertGreater(r["in_flight_delay_days"], 0)

    def test_new_breaches_are_open_and_over_target(self):
        for r in self.rows("v_week_new_breaches"):
            self.assertNotEqual(r["stage_status"], "Done")
            self.assertGreater(r["days_used"], r["target_days"])

    def test_summary_counts_equal_the_view_rows(self):
        s = self.summary
        self.assertEqual(s["new_blockers"], len(self.rows("v_week_new_blockers")))
        self.assertEqual(s["completed_stages"], len(self.rows("v_week_completed_stages")))
        self.assertEqual(s["completed_late"], sum(r["finished_late"] for r in self.rows("v_week_completed_stages")))
        self.assertEqual(s["new_breaches"], len(self.rows("v_week_new_breaches")))
        self.assertEqual(s["missed_targets"], len(self.rows("v_week_missed_targets")))
        self.assertEqual(s["went_live"], len(self.rows("v_week_went_live")))
        # the total shown beside the rows is the sum of the whole-dollar row amounts
        self.assertEqual(s["missed_targets_trapped_usd"],
                         sum(round(r["trapped_in_flight_usd"]) for r in self.rows("v_week_missed_targets")))


class WeekInBriefAndDashboard(unittest.TestCase):
    def test_brief_this_week_counts_equal_the_views(self):
        with mock.patch.object(llm, "client", lambda: None):
            brief, _ = agents.daily_brief(agents.classify_blockers(agents.blocked_items()))
        week = brief.split("\n\nHeadline\n")[0].split("This week", 1)[1]
        top_level = [l for l in week.splitlines() if l.startswith("- ")]
        first_number = lambda line: int(re.search(r":\s*(\d+)", line).group(1))  # noqa: E731
        con = sqlite3.connect(agents.DB_PATH)
        s = con.execute("""SELECT new_blockers, new_breaches, missed_targets, completed_stages, went_live
                           FROM v_week_summary""").fetchone()
        con.close()
        self.assertEqual([first_number(l) for l in top_level], list(s))

    def test_brief_opens_with_this_week(self):
        with mock.patch.object(llm, "client", lambda: None):
            brief, _ = agents.daily_brief(agents.classify_blockers(agents.blocked_items()))
        self.assertTrue(brief.split("\n\n")[1].startswith("This week ("))

    def test_dashboard_week_card_matches_the_views(self):
        con = sqlite3.connect(build_dashboard.DB_PATH)
        data = build_dashboard.build(con)
        con.row_factory = sqlite3.Row
        summary = dict(con.execute("SELECT * FROM v_week_summary").fetchone())
        late = con.execute("SELECT COUNT(*) FROM v_week_completed_stages WHERE finished_late = 1").fetchone()[0]
        con.close()
        self.assertEqual(data["week"]["summary"], summary)
        listed = summary["new_blockers"] + summary["new_breaches"] + summary["missed_targets"] + late + summary["went_live"]
        self.assertEqual(len(data["week"]["events"]), listed)
        dates = [e["date"] for e in data["week"]["events"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


if __name__ == "__main__":
    unittest.main()
