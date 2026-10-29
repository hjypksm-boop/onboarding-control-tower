"""
Tests for routing.py and the lane router agent. Standard library only; no API calls.

Run:  python generate_data.py && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents  # noqa: E402
import llm  # noqa: E402
import routing  # noqa: E402


def product(pid, contract=1, partner=0):
    return {"product_id": pid, "contract_required": contract, "partner_bank_required": partner}


VERIFY, PAYOUT, CARDS, USDC = product("VERIFY", 0), product("PAYOUT"), product("CARDS"), product("USDC_IN", 1, 1)


class RouteLane(unittest.TestCase):
    def lane(self, segment, risk, entities, products):
        return routing.route_lane(segment, risk, entities, products)[0]

    def test_rules_in_order(self):
        self.assertEqual(self.lane("SMB", "Low", 1, [USDC]), "Exceptions")          # partner bank first
        self.assertEqual(self.lane("SMB", "High", 1, [VERIFY]), "Exceptions")       # high risk
        self.assertEqual(self.lane("SMB", "Low", 1, [CARDS]), "Complex")            # cards/travel
        self.assertEqual(self.lane("SMB", "Low", 1, [VERIFY, PAYOUT]), "Complex")   # multi-product
        self.assertEqual(self.lane("SMB", "Low", 3, [PAYOUT]), "Complex")           # 3+ entities
        self.assertEqual(self.lane("SMB", "Low", 1, [VERIFY]), "Fast")
        self.assertEqual(self.lane("Enterprise", "Low", 1, [VERIFY]), "Standard")   # no Fast for Enterprise
        self.assertEqual(self.lane("SMB", "Medium", 1, [VERIFY]), "Standard")       # no Fast above low risk
        self.assertEqual(self.lane("SMB", "Low", 2, [VERIFY]), "Standard")          # no Fast for 2 entities

    def test_escalation_threshold_is_strictly_beyond_one_and_a_half_targets(self):
        self.assertFalse(routing.should_escalate("Fast", 15))
        self.assertTrue(routing.should_escalate("Fast", 16))
        self.assertFalse(routing.should_escalate("Standard", 45))
        self.assertTrue(routing.should_escalate("Complex", 91))
        self.assertFalse(routing.should_escalate("Exceptions", 1000))

    def test_boundary_flips(self):
        flips = dict(routing.boundary_flips("Mid-market", "Medium", 1, [VERIFY]))
        self.assertEqual(flips["risk Low"], "Fast")
        self.assertEqual(flips["risk High"], "Exceptions")
        self.assertEqual(routing.boundary_flips("SMB", "Low", 1, [USDC]), [])   # USDC is Exceptions whatever the client


class LaneRouterAgent(unittest.TestCase):
    def test_every_stored_lane_matches_the_rules(self):
        rows = agents.lane_journeys()
        self.assertTrue(rows)
        self.assertEqual([r["cp_id"] for r in rows if r["routed_lane"] != r["initial_lane"]], [])

    def test_no_in_flight_journey_is_overdue_for_escalation(self):
        _, groups = agents.lane_facts(agents.lane_journeys())
        self.assertEqual(groups["due"], [])

    def test_report_is_code_written_and_note_is_labelled_draft_with_llm(self):
        with mock.patch.object(llm, "client", lambda: None):
            template, _ = agents.lane_router()
        with mock.patch.object(llm, "client", lambda: object()), \
                mock.patch.object(llm, "_chat", lambda *a, **k: "- Re-check the risk rating for CP-0030."):
            drafted, source = agents.lane_router()
        self.assertEqual(source, "llm")
        self.assertEqual(template.split("Review note")[0], drafted.split("Review note")[0])
        self.assertIn(llm.DRAFT_LABEL, drafted)


if __name__ == "__main__":
    unittest.main()
