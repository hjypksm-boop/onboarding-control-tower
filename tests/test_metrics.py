"""
Tests for the metric views in metrics.sql. Standard library only.

Run:  python generate_data.py && python -m unittest discover -s tests -v
"""
import os
import sqlite3
import unittest

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "control_tower.db")


class OwnerWorkload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DB_PATH):
            raise RuntimeError("control_tower.db not found: run python generate_data.py first")
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        cls.rows = [dict(r) for r in con.execute("SELECT * FROM v_owner_workload ORDER BY workload_rank")]
        con.close()

    def test_active_is_in_progress_plus_blocked(self):
        for r in self.rows:
            self.assertEqual(r["active"], r["in_progress"] + r["blocked"], r["member_id"])

    def test_rank_orders_by_active_then_blocked(self):
        keys = [(-r["active"], -r["blocked"], r["member_id"]) for r in self.rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([r["workload_rank"] for r in self.rows], list(range(1, len(self.rows) + 1)))

    def test_not_started_stages_do_not_raise_rank(self):
        # AM-C has the most open stages, but most are not started (waiting on other tracks)
        by_open = max(self.rows, key=lambda r: (r["open_stages"], r["member_id"]))
        self.assertEqual(self.rows[0]["member_id"], "SOLENG-B")
        self.assertNotEqual(by_open["member_id"], self.rows[0]["member_id"])
        self.assertGreater(by_open["not_started"], by_open["active"])


if __name__ == "__main__":
    unittest.main()
