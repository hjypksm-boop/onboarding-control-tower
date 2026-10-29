"""
Tests for the agent layer guardrails. Standard library only; no API calls are made.

Run:  python generate_data.py && python -m unittest discover -s tests -v
"""
import os
import re
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents  # noqa: E402
import llm  # noqa: E402

NO_KEY = mock.patch.object(llm, "client", lambda: None)
FAKE_KEY = mock.patch.object(llm, "client", lambda: object())


def fake_chat(reply):
    return mock.patch.object(llm, "_chat", lambda system, user, max_tokens: reply)


def numbers(text):
    """Numbers in text, in order, with record IDs removed: '$662,023' -> '662023'."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*", llm.RECORD_ID.sub("", text))]


class HeadlineMatchesViews(unittest.TestCase):
    """The brief's headline figures must equal the SQL views exactly."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(agents.DB_PATH):
            raise RuntimeError("control_tower.db not found: run python generate_data.py first")
        con = sqlite3.connect(agents.DB_PATH)
        one = lambda sql: con.execute(sql).fetchone()  # noqa: E731
        in_flight, late, trapped = one("""
            SELECT COUNT(*), SUM(in_flight_delay_days > 0), SUM(trapped_in_flight_usd)
            FROM v_trapped_revenue WHERE cp_status = 'Onboarding'""")
        top_cp, top_trapped = one("""
            SELECT cp_id, trapped_in_flight_usd FROM v_trapped_revenue
            WHERE cp_status = 'Onboarding' ORDER BY trapped_in_flight_usd DESC LIMIT 1""")
        open_stages = "FROM v_stage_sla WHERE stage_status <> 'Done'"
        breached, = one(f"SELECT COUNT(DISTINCT work_item_id) {open_stages} AND sla_status = 'breached'")
        breached_cp, = one(f"SELECT COUNT(DISTINCT work_item_id) {open_stages} "
                           "AND sla_status = 'breached' AND is_critical_path = 1")
        blocked, = one("SELECT COUNT(DISTINCT work_item_id) FROM v_stage_sla WHERE stage_status = 'Blocked'")
        con.close()
        cls.expected = [late, in_flight, round(trapped), round(top_trapped), breached, breached_cp, blocked]
        cls.top_cp = top_cp
        with NO_KEY:
            cls.headline = agents.headline(agents.brief_figures())

    def test_headline_figures_equal_views_in_order(self):
        self.assertEqual(numbers(self.headline), [str(n) for n in self.expected])

    def test_headline_names_largest_trapped_journey(self):
        self.assertIn(self.top_cp, self.headline)

    def test_brief_contains_code_written_headline(self):
        with NO_KEY:
            brief, _ = agents.daily_brief(agents.classify_blockers(agents.blocked_items()))
        self.assertIn("Headline\n" + self.headline, brief)


class DraftGuard(unittest.TestCase):
    def test_no_key_uses_unlabelled_fallback(self):
        with NO_KEY:
            self.assertEqual(llm.draft("x", "facts", "fallback"), ("fallback", "template"))

    def test_draft_without_figures_is_labelled(self):
        with FAKE_KEY, fake_chat("- Chase the client for CP-0024 first."):
            text, source = llm.draft("x", "facts", "fallback")
        self.assertEqual(source, "llm")
        self.assertTrue(text.startswith(llm.DRAFT_LABEL))

    def test_draft_with_figures_is_rejected(self):
        for reply in ["- Clear the 10 blocked stages.", "- Recover $662k.", "- Fix within 3 days.",
                      "- About 54% are late."]:
            with self.subTest(reply=reply), FAKE_KEY, fake_chat(reply):
                self.assertEqual(llm.draft("x", "facts", "fallback"), ("fallback", "template"))

    def test_api_error_falls_back(self):
        def boom(*args, **kwargs):
            raise llm.groq.APIConnectionError(request=mock.Mock())
        with FAKE_KEY, mock.patch.object(llm, "_chat", boom):
            self.assertEqual(llm.draft("x", "facts", "fallback"), ("fallback", "template"))


class CodeOwnsFigures(unittest.TestCase):
    """With the LLM on, every line with a figure is still the code-written line."""

    def test_brief_figures_unchanged_by_llm(self):
        with NO_KEY:
            items = agents.classify_blockers(agents.blocked_items())
            template, _ = agents.daily_brief(items)
        with FAKE_KEY, fake_chat("- Start with CP-0019 Skyward Travel Group."):
            drafted, source = agents.daily_brief(items)
        self.assertEqual(source, "llm")
        self.assertEqual(template.split("Recommended actions")[0], drafted.split("Recommended actions")[0])
        self.assertIn(llm.DRAFT_LABEL, drafted.split("Recommended actions")[1])
        self.assertNotIn(llm.DRAFT_LABEL, template)

    def test_nudge_header_bullets_and_signoff_unchanged_by_llm(self):
        with NO_KEY:
            template = [t for t, _ in agents.draft_nudges()]
        with FAKE_KEY, fake_chat("Hi, please start with the blocked stage."):
            drafted = [t for t, _ in agents.draft_nudges()]
        for t, d in zip(template, drafted):
            self.assertIn(llm.DRAFT_LABEL, d)
            figure_lines = lambda text: [l for l in text.splitlines() if numbers(l)]  # noqa: E731
            self.assertEqual(figure_lines(t), figure_lines(d))
            self.assertTrue(d.endswith("Control tower"))


if __name__ == "__main__":
    unittest.main()
