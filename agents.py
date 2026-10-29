"""
Agent layer for the control tower: blocker triage, blocker summary, owner nudges and the daily brief.

Every figure is queried from the metric views (metrics.sql) and every sentence that states or
combines figures is written here in code. The LLM (llm.py) only drafts recommended actions,
prioritisation and tone, with no figures, and that text is labelled as a draft for human review.
Without GROQ_API_KEY the code-written fallback text is used instead.

Run:  python agents.py [classify|blockers|nudges|brief|all]
"""
import argparse
import os
import sqlite3
from collections import Counter

import llm

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "control_tower.db")


def query(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def usd(x):
    return f"${x:,.0f}"


def pct(x):
    return f"{x:.0%}"


def days(n):
    return f"{n} day{'' if n == 1 else 's'}"


def waiting(counts):
    """'waiting on Client: 4, needs review: 3' from a Counter of waiting_on labels."""
    return ", ".join(f"{'needs review' if k == EDGE_FALLBACK else 'waiting on ' + k}: {v}"
                     for k, v in counts.most_common())


# ---------------------------------------------------------------------------
# 1. Blocker triage: who is the stage waiting on?
# Keyword rules handle clear cases. Notes that match nothing, or match conflicting
# labels, are edge cases and go to the LLM, which must answer with an allowed label.
# ---------------------------------------------------------------------------
WAITING_ON = ["Client", "Internal", "Partner bank"]
KEYWORDS = [  # (keyword in lower-case blocker note, label)
    ("partner bank", "Partner bank"),
    ("client", "Client"), ("expired", "Client"), ("incomplete", "Client"), ("outstanding", "Client"),
    ("capacity", "Internal"), ("not configured", "Internal"), ("pending", "Internal"),
    ("not yet scheduled", "Internal"),
]
EDGE_FALLBACK = "Needs review"


def rule_waiting_on(note):
    """Label from keyword rules, or None when the note is an edge case."""
    hits = {label for kw, label in KEYWORDS if kw in note.lower()}
    if "Partner bank" in hits:  # anything waiting on the partner bank is a partner bank blocker
        return "Partner bank"
    return hits.pop() if len(hits) == 1 else None


def blocked_items():
    """Open blocked work items (shared client-level tracks appear once), longest blocked first."""
    return query("""
        SELECT GROUP_CONCAT(s.cp_id, '/') AS cp_ids, c.client_name, s.track, s.owner_member_id,
               s.blocker_note, s.days_blocked, s.sla_status, MAX(s.is_critical_path) AS is_critical_path
        FROM v_stage_sla s JOIN clients c USING (client_id)
        WHERE s.stage_status = 'Blocked'
        GROUP BY s.work_item_id
        ORDER BY s.days_blocked DESC, cp_ids""")


def classify_blockers(items):
    """Add waiting_on and how it was decided (rule / llm / template) to each blocked item."""
    cache = {}
    for item in items:
        note = item["blocker_note"]
        if note not in cache:
            label = rule_waiting_on(note)
            if label:
                cache[note] = (label, "rule")
            else:
                cache[note] = llm.classify(
                    "An onboarding stage for a B2B payments client is blocked. Who is the team waiting on?",
                    f"{item['track']} track: {note}", WAITING_ON, EDGE_FALLBACK)
        item["waiting_on"], item["decided_by"] = cache[note]
    return items


def classify_report(items):
    lines = ["Blocker triage (who each blocked stage is waiting on)", ""]
    for i in items:
        lines.append(f"- {i['cp_ids']} {i['track']}: {i['waiting_on']} [{i['decided_by']}] - {i['blocker_note']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Blocker summary: code lists the blocked stages by who we're waiting on;
# the LLM only drafts the next action per group.
# ---------------------------------------------------------------------------
def group_name(label):
    return "Needs review" if label == EDGE_FALLBACK else f"Waiting on {label}"


def blocker_facts(items):
    """Code-written list of blocked stages, grouped by waiting_on. Returns (text, groups)."""
    groups = {}
    for i in items:
        groups.setdefault(i["waiting_on"], []).append(i)
    lines = [f"Blocked stages: {len(items)} ({waiting(Counter(i['waiting_on'] for i in items))})"]
    for label, group in sorted(groups.items(), key=lambda g: -len(g[1])):
        lines += ["", group_name(label)]
        for i in group:
            lines.append(f"- {i['cp_ids']} {i['client_name']}, {i['track']} (owner {i['owner_member_id']}): "
                         f"blocked {days(i['days_blocked'])}, SLA {i['sla_status']}, "
                         f"critical path: {'yes' if i['is_critical_path'] else 'no'}. Note: {i['blocker_note']}")
    return "\n".join(lines), groups


def blocker_actions_template(groups):
    lines = []
    for label, group in groups.items():
        ids = ", ".join(i["cp_ids"] for i in group)
        verb = "agree an owner for" if label == EDGE_FALLBACK else "chase"
        lines.append(f"- {group_name(label)}: {verb} {ids}, longest blocked first.")
    return "\n".join(lines)


def summarise_blockers(items):
    facts, groups = blocker_facts(items)
    actions, source = llm.draft("""
You support the head of onboarding at a fintech's B2B onboarding control tower. For each group of blocked
stages in the facts, write one "-" bullet with a concrete next action: who should do what, and which stage
to tackle first. No heading.""", facts, blocker_actions_template(groups))
    return f"{facts}\n\nNext actions\n{actions}", source


# ---------------------------------------------------------------------------
# 3. Owner nudges: in-progress or blocked stages that are breached, at risk or blocked.
# Not-started stages are left out: they are waiting on other tracks, not on their owner.
# Code writes the header, stage bullets, ask and sign-off; the LLM only drafts the opening.
# ---------------------------------------------------------------------------
NUDGE_INTRO = "Hi,\n\nThese stages need a look today:"
NUDGE_ASK = "Can you reply with the next step and an expected date for each?"


def nudge_items():
    return query("""
        SELECT s.owner_member_id, m.member_label, m.email, GROUP_CONCAT(s.cp_id, '/') AS cp_ids,
               c.client_name, s.track, s.stage_status, s.sla_status, s.days_used, s.target_days,
               s.days_blocked, s.blocker_note, MAX(s.is_critical_path) AS is_critical_path
        FROM v_stage_sla s
        JOIN clients c USING (client_id)
        JOIN team_members m ON m.member_id = s.owner_member_id
        WHERE s.stage_status IN ('In progress', 'Blocked')
          AND (s.sla_status IN ('breached', 'at risk') OR s.stage_status = 'Blocked')
        GROUP BY s.work_item_id
        ORDER BY s.owner_member_id, s.days_used - s.target_days DESC""")


def nudge_header(member_label, email, items):
    n = len(items)
    return (f"To: {member_label} <{email}>\n"
            f"Subject: {n} onboarding stage{'s' if n > 1 else ''} need{'' if n > 1 else 's'} attention")


def nudge_bullets(items):
    lines = []
    for i in items:
        status = f"blocked {days(i['days_blocked'])} ({i['blocker_note']})" if i["stage_status"] == "Blocked" \
            else i["sla_status"]
        cp = ", on the critical path" if i["is_critical_path"] else ""
        lines.append(f"- {i['cp_ids']} {i['client_name']}, {i['track']}: {i['days_used']} of "
                     f"{i['target_days']} target days used, {status}{cp}")
    return "\n".join(lines)


def draft_nudges():
    """One nudge per owner. Returns a list of (text, source)."""
    by_owner = {}
    for i in nudge_items():
        by_owner.setdefault(i["owner_member_id"], []).append(i)
    out = []
    for items in by_owner.values():
        bullets = nudge_bullets(items)
        intro, source = llm.draft("""
Write a short, friendly opening (one or two sentences) for a nudge from the onboarding control tower to the
colleague who owns the stages in the facts. Greet them, then say which stage to tackle first and why, in words.
The stages are listed most urgent first (ranked by code on days over target), so lead with the first one.
Don't list the stages, ask for dates or sign off: the list, the ask and the sign-off follow your text.""",
                                  bullets, NUDGE_INTRO, max_tokens=150)
        out.append((f"{nudge_header(items[0]['member_label'], items[0]['email'], items)}\n\n"
                    f"{intro}\n\n{bullets}\n\n{NUDGE_ASK}\n\nControl tower", source))
    return out


# ---------------------------------------------------------------------------
# 4. Daily brief: code writes the title, headline and every section with figures;
# the LLM only drafts the recommended actions.
# ---------------------------------------------------------------------------
def brief_figures():
    """The headline figures, straight from the views."""
    return query("""
        SELECT a.as_of_date, t.in_flight, t.late, t.trapped,
               top.cp_id AS top_cp_id, top.client_name AS top_client, top.trapped_in_flight_usd AS top_trapped,
               s.breached, s.breached_cp, s.at_risk, s.blocked
        FROM v_as_of a
        CROSS JOIN (SELECT COUNT(*) in_flight, SUM(in_flight_delay_days > 0) late, SUM(trapped_in_flight_usd) trapped
                    FROM v_trapped_revenue WHERE cp_status = 'Onboarding') t
        CROSS JOIN (SELECT cp_id, client_name, trapped_in_flight_usd FROM v_trapped_revenue
                    WHERE cp_status = 'Onboarding' ORDER BY trapped_in_flight_usd DESC LIMIT 1) top
        CROSS JOIN (SELECT COUNT(DISTINCT CASE WHEN sla_status = 'breached' THEN work_item_id END) breached,
                           COUNT(DISTINCT CASE WHEN sla_status = 'breached' AND is_critical_path = 1
                                               THEN work_item_id END) breached_cp,
                           COUNT(DISTINCT CASE WHEN sla_status = 'at risk' THEN work_item_id END) at_risk,
                           COUNT(DISTINCT CASE WHEN stage_status = 'Blocked' THEN work_item_id END) blocked
                    FROM v_stage_sla WHERE stage_status <> 'Done') s""")[0]


def headline(f):
    return (f"{f['late']} of {f['in_flight']} in-flight journeys are forecast to go live late, trapping "
            f"{usd(f['trapped'])} of revenue; the largest is {f['top_cp_id']} {f['top_client']} at "
            f"{usd(f['top_trapped'])}. {f['breached']} open stages are breached ({f['breached_cp']} on the "
            f"critical path) and {f['blocked']} stages are blocked.")


def brief_sections(items, f):
    top = query("""
        SELECT cp_id, client_name, product_id, current_lane, in_flight_delay_days, trapped_in_flight_usd
        FROM v_trapped_revenue WHERE cp_status = 'Onboarding' AND trapped_in_flight_usd > 0
        ORDER BY trapped_in_flight_usd DESC LIMIT 3""")
    escalated = query("""
        SELECT cp_id, client_name, initial_lane, current_lane FROM v_trapped_revenue
        WHERE cp_status = 'Onboarding' AND initial_lane <> current_lane ORDER BY cp_id""")
    ramp = query("""
        SELECT client_name, product_id, latest_pct_of_expected, ramp_shortfall_usd, days_live
        FROM v_ramp WHERE reached_run_rate = 0 ORDER BY ramp_shortfall_usd DESC""")
    tp = query("""
        SELECT SUM(cap_status = 'breach') breaches, SUM(cap_status = 'warning') warnings,
               GROUP_CONCAT(CASE WHEN cap_status = 'breach' THEN client_name END, ', ') breach_clients
        FROM v_touchpoints""")[0]
    load = query("""
        SELECT member_id, in_progress + blocked AS active, in_progress, blocked, oldest_blocker_days
        FROM v_owner_workload ORDER BY active DESC, blocked DESC LIMIT 3""")

    lines = ["Revenue at risk (largest trapped revenue in flight)",
             *[f"- {r['cp_id']} {r['client_name']} ({r['product_id']}, {r['current_lane']} lane): "
               f"{days(r['in_flight_delay_days'])} late, {usd(r['trapped_in_flight_usd'])} trapped" for r in top],
             "", "Onboarding health",
             f"- At risk: {f['at_risk']} open stages have used 75% or more of their target.",
             f"- Blocked: {waiting(Counter(i['waiting_on'] for i in items))}.",
             *[f"- Escalated to {r['current_lane']}: {r['cp_id']} {r['client_name']} (routed {r['initial_lane']})"
               for r in escalated],
             "", "Live revenue",
             *[f"- {r['client_name']} ({r['product_id']}) is at {pct(r['latest_pct_of_expected'])} of expected "
               f"daily revenue after {days(r['days_live'])} live; ramp shortfall so far {usd(r['ramp_shortfall_usd'])}."
               for r in ramp],
             "", "Client experience",
             f"- {tp['breaches']} clients are over the 3 post-sale touchpoint cap ({tp['breach_clients']}); "
             f"{tp['warnings']} are at the cap.",
             "", "Team load (in progress + blocked)",
             *[f"- {r['member_id']}: {r['active']} active ({r['in_progress']} in progress, {r['blocked']} blocked"
               + (f", oldest blocker {days(r['oldest_blocker_days'])}" if r["oldest_blocker_days"] else "") + ")"
               for r in load]]
    return "\n".join(lines), ramp, load


def brief_actions_template(items, f, ramp, load):
    lines = [f"- Unblock the largest trapped-revenue journey first: {f['top_cp_id']} {f['top_client']}."]
    critical = [i for i in items if i["is_critical_path"]]
    if critical:  # items are sorted longest blocked first
        lines.append(f"- Clear the longest-blocked critical-path stage: {critical[0]['cp_ids']} "
                     f"{critical[0]['client_name']} ({critical[0]['track']}).")
    if ramp:
        lines.append(f"- Review ramp plans with {', '.join(r['client_name'] for r in ramp)}.")
    if load:
        lines.append(f"- Rebalance new work away from {load[0]['member_id']}.")
    return "\n".join(lines)


def daily_brief(items):
    f = brief_figures()
    sections, ramp, load = brief_sections(items, f)
    facts = f"Headline\n{headline(f)}\n\n{sections}"
    # the LLM also sees the blocked-stage detail (owner, track, who we wait on) so it doesn't guess them
    context = f"{facts}\n\n{blocker_facts(items)[0]}"
    actions, source = llm.draft("""
You advise the leadership team of a fintech's B2B onboarding control tower. From the facts, write the
three most important recommended actions for today as "-" bullets, most important first. Each names the
stage, client or owner to act on and what to do. Only name an owner, track or blocker the facts give for
that stage. No heading.""", context, brief_actions_template(items, f, ramp, load), max_tokens=300)
    return (f"Control tower daily brief, as of {f['as_of_date']}\n\n{facts}\n\n"
            f"Recommended actions\n{actions}"), source


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("agent", nargs="?", default="all",
                        choices=["classify", "blockers", "nudges", "brief", "all"])
    agent = parser.parse_args().agent

    print(f"[{llm.source_label()}]")
    items = classify_blockers(blocked_items())
    sections = []
    if agent in ("classify", "all"):
        sections.append(classify_report(items))
    if agent in ("blockers", "all"):
        sections.append("Blocker summary\n\n" + summarise_blockers(items)[0])
    if agent in ("nudges", "all"):
        sections.append("Nudges\n\n" + "\n\n---\n\n".join(text for text, _ in draft_nudges()))
    if agent in ("brief", "all"):
        sections.append(daily_brief(items)[0])
    print(("\n\n" + "=" * 72 + "\n\n").join(sections))


if __name__ == "__main__":
    main()
