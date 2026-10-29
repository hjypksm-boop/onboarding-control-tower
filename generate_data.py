"""
Generate the fake data set for the B2B Onboarding-to-Revenue Control Tower.

Creates control_tower.db (SQLite) from schema.sql and exports every table to csv/.
Standard library only. Deterministic (seeded), so every run produces the same data.

Run:  python generate_data.py
"""
import csv
import os
import random
import sqlite3
from datetime import date, timedelta

import routing

random.seed(42)
TODAY = date(2026, 10, 18)          # "as of" date for the control tower
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "control_tower.db")
CSV_DIR = os.path.join(HERE, "csv")

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
TEAMS = [  # team_id, team_name, owns_track, number of members
    ("SALES", "Sales", "Contract", 2),
    ("LEGAL", "Legal", "Legal", 2),
    ("COMPLIANCE", "Compliance (KYB/KYC)", "KYB-KYC", 3),
    ("SOLENG", "Solutions Engineering", "Tech build", 3),
    ("COMPOPS", "Compliance Ops", "Compliance ops readiness", 2),
    ("PARTNER", "Partner Banking", "Partner bank approval", 2),
    ("AM", "Account Management", "AM handoff", 3),
]
TRACK_TEAM = {t[2]: t[0] for t in TEAMS}
TEAM_NAME = {t[0]: t[1] for t in TEAMS}

# Workload skew: Solutions Engineering Member B carries a heavy queue (a bottleneck to spot)
MEMBER_WEIGHTS = {"SOLENG-B": 3}

PRODUCTS = [  # id, name, family, contract_required, partner_bank_required, tech effort
    ("PAYIN", "Pay-ins", "Fiat", 0, 0, "Medium"),
    ("PAYOUT", "Payouts", "Fiat", 1, 0, "Medium"),
    ("WALLET", "Wallets", "Fiat", 0, 0, "Low"),
    ("USDC_IN", "USDC Pay-in", "Crypto", 1, 1, "High"),
    ("USDC_OUT", "USDC Payout", "Crypto", 1, 1, "High"),
    ("VERIFY", "Verify", "Identity", 0, 0, "Low"),
    ("CARDS", "Cards", "Cards", 1, 0, "High"),
    ("TRAVEL", "Travel", "Cards", 1, 0, "High"),
]
PRODUCT = {p[0]: p for p in PRODUCTS}

LANES = [  # lane, target_days, contract_required, routing rule
    ("Fast", 10, 0, "Single no-contract product (Verify, Wallets, Pay-ins), low risk, SMB/Mid-market, single entity"),
    ("Standard", 30, 1, "Single product, low/medium risk, up to 2 entities, no Cards/Travel/USDC"),
    ("Complex", 60, 1, "Cards or Travel, multi-product onboarding, or 3+ legal entities"),
    ("Exceptions", 75, 1, "USDC products or high-risk KYB (partner bank sign-off), or any journey stalled > 1.5x its lane target"),
]
LANE_TARGET = {l[0]: l[1] for l in LANES}
assert LANE_TARGET == routing.LANE_TARGET, "lane targets must match routing.py"

PARALLEL_TRACKS = ["Contract", "Legal", "KYB-KYC", "Tech build",
                   "Compliance ops readiness", "Partner bank approval"]
BENCH = {  # days from onboarding start; 0 = not required. AM handoff runs after parallel tracks.
    "Fast":       {"Contract": 0,  "Legal": 3,  "KYB-KYC": 6,  "Tech build": 8,  "Compliance ops readiness": 5,  "Partner bank approval": 0,  "AM handoff": 2},
    "Standard":   {"Contract": 7,  "Legal": 14, "KYB-KYC": 18, "Tech build": 25, "Compliance ops readiness": 20, "Partner bank approval": 0,  "AM handoff": 5},
    "Complex":    {"Contract": 14, "Legal": 28, "KYB-KYC": 30, "Tech build": 52, "Compliance ops readiness": 40, "Partner bank approval": 0,  "AM handoff": 8},
    "Exceptions": {"Contract": 14, "Legal": 30, "KYB-KYC": 40, "Tech build": 45, "Compliance ops readiness": 45, "Partner bank approval": 65, "AM handoff": 10},
}
SHARED_TRACKS = {"Contract", "Legal", "KYB-KYC", "Partner bank approval"}  # client-level, shared across products

BLOCKERS = {
    "Contract": ["Redlines on liability cap pending client legal", "Pricing schedule awaiting client CFO sign-off"],
    "Legal": ["Awaiting client's signed MSA addendum", "Data processing agreement under negotiation"],
    "KYB-KYC": ["UBO documents incomplete", "Director proof of address expired", "Source of funds clarification outstanding"],
    "Tech build": ["Webhook signature validation failing in sandbox", "Client dev team capacity - integration paused",
                   "Awaiting client IP allowlist for production"],
    "Compliance ops readiness": ["Monitoring rules not configured for new corridor", "Sanctions screening list mapping pending"],
    "Partner bank approval": ["Partner bank EDD questionnaire outstanding", "Partner bank risk committee sits in 2 weeks"],
    "AM handoff": ["AM capacity - handoff call not yet scheduled"],
}

# Revenue assumptions (USD per calendar day at run-rate, before segment multiplier)
BASE_DAILY_REV = {"PAYIN": 900, "PAYOUT": 1200, "WALLET": 450, "USDC_IN": 1400,
                  "USDC_OUT": 1600, "VERIFY": 150, "CARDS": 1800, "TRAVEL": 2200}
SEGMENT_MULT = {"SMB": 0.4, "Mid-market": 1.0, "Enterprise": 2.5}
TAKE_RATE = {"PAYIN": 0.009, "PAYOUT": 0.005, "WALLET": 0.004, "USDC_IN": 0.0035,
             "USDC_OUT": 0.0035, "VERIFY": None, "CARDS": 0.011, "TRAVEL": 0.008}
AVG_TICKET = {"PAYIN": 85, "PAYOUT": 450, "WALLET": 120, "USDC_IN": 2500,
              "USDC_OUT": 3000, "VERIFY": None, "CARDS": 140, "TRAVEL": 600}
VERIFY_FEE = 0.80  # USD per verification check

# ---------------------------------------------------------------------------
# Clients: (name, industry, segment, country, risk, entities, products)
# Lanes are NOT hard-coded: routing.route_lane() derives them from these attributes.
# ---------------------------------------------------------------------------
ONBOARDING_CLIENTS = [
    ("Lumen Tutors", "EdTech", "SMB", "GB", "Low", 1, ["VERIFY"]),
    ("Copperleaf Studios", "Creator platform", "SMB", "NL", "Low", 1, ["WALLET"]),
    ("Harbourline Logistics", "Logistics", "Mid-market", "DE", "Low", 1, ["PAYIN"]),
    ("Quillpay Freelance", "Gig marketplace", "SMB", "GB", "Low", 1, ["VERIFY"]),
    ("Saltmarsh Retail", "E-commerce", "SMB", "IE", "Low", 1, ["PAYIN"]),
    ("Orbitly Apps", "SaaS", "SMB", "SG", "Low", 1, ["WALLET"]),
    ("Fernway Health", "Healthtech", "Mid-market", "AU", "Low", 1, ["VERIFY"]),
    ("Brightdock Events", "Events", "SMB", "GB", "Low", 1, ["PAYIN"]),
    ("Kestrel Payroll", "Payroll", "Mid-market", "GB", "Medium", 1, ["PAYOUT"]),
    ("Tidewater Insurance", "Insurtech", "Mid-market", "DE", "Low", 2, ["PAYOUT"]),
    ("Marlow Marketplace", "Marketplace", "Mid-market", "FR", "Medium", 1, ["PAYIN"]),
    ("Pinecrest Lending", "Lending", "Mid-market", "US", "Medium", 1, ["PAYOUT"]),
    ("Glasshouse Media", "Media", "Enterprise", "GB", "Low", 1, ["WALLET"]),
    ("Redwood Gaming", "Gaming", "Mid-market", "MT", "Medium", 1, ["PAYIN"]),
    ("Ashgrove Property", "Proptech", "Mid-market", "GB", "Low", 2, ["PAYOUT"]),
    ("Nimbus Remit", "Remittance", "Mid-market", "AE", "Medium", 1, ["PAYOUT"]),
    ("Velvet Commerce", "E-commerce", "Enterprise", "ES", "Low", 1, ["PAYIN"]),
    ("Corvid Analytics", "SaaS", "Mid-market", "IN", "Medium", 1, ["WALLET"]),
    ("Skyward Travel Group", "Travel", "Enterprise", "GB", "Medium", 4, ["TRAVEL"]),
    ("Atlas Fleet Services", "Fleet management", "Enterprise", "DE", "Low", 3, ["CARDS"]),
    ("Meridian Marketplaces", "Marketplace", "Enterprise", "SG", "Medium", 2, ["PAYIN", "PAYOUT"]),
    ("Juniper Expense", "Spend management", "Mid-market", "NL", "Low", 1, ["CARDS"]),
    ("Horizon OTA", "Travel", "Mid-market", "AE", "Medium", 2, ["TRAVEL", "PAYOUT"]),
    ("Granite Procurement", "B2B procurement", "Enterprise", "US", "Low", 5, ["PAYOUT"]),
    ("Polaris Digital Assets", "Crypto exchange", "Mid-market", "SG", "Medium", 1, ["USDC_IN"]),
    ("Stablepath Treasury", "Treasury", "Enterprise", "CH", "Medium", 2, ["USDC_OUT", "PAYOUT"]),
    ("Cobalt Gaming", "Gaming", "Mid-market", "CW", "High", 1, ["PAYIN"]),
    ("Lagos Link Remit", "Remittance", "Mid-market", "NG", "High", 1, ["PAYOUT"]),
    ("Driftwood Web3", "Web3", "SMB", "AE", "Medium", 1, ["USDC_OUT"]),
    ("Equinox Trade Finance", "Trade finance", "Enterprise", "HK", "High", 3, ["PAYOUT"]),
]

# Live clients: (name, industry, segment, country, risk, entities, first product, second product, second status)
LIVE_CLIENTS = [
    ("Bluepeak Marketplace", "Marketplace", "Enterprise", "GB", "Low", 2, "PAYIN", "PAYOUT", "Live"),
    ("Sterling Payroll Co", "Payroll", "Mid-market", "GB", "Low", 1, "PAYOUT", "WALLET", "Live"),
    ("Aurora Remit", "Remittance", "Mid-market", "AE", "Medium", 1, "PAYOUT", "USDC_OUT", "Onboarding"),
    ("Thistle Commerce", "E-commerce", "SMB", "GB", "Low", 1, "PAYIN", "VERIFY", "Live"),
    ("Zephyr Travel", "Travel", "Enterprise", "DE", "Medium", 3, "TRAVEL", "CARDS", "Onboarding"),
    ("Oakline Insurance", "Insurtech", "Mid-market", "NL", "Low", 1, "PAYOUT", None, None),
    ("Pebble Learning", "EdTech", "SMB", "GB", "Low", 1, "VERIFY", "PAYIN", "Live"),
    ("Monarch Gig Works", "Gig marketplace", "Mid-market", "US", "Medium", 1, "PAYOUT", None, None),
    ("Cinder Games", "Gaming", "Mid-market", "MT", "Medium", 1, "PAYIN", None, None),
    ("Halcyon Wallet", "Fintech", "Mid-market", "SG", "Low", 1, "WALLET", None, None),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def d(x):
    return x.isoformat() if x else None


def products_info(product_ids):
    """Product records in the shape routing.route_lane() expects."""
    return [{"product_id": p, "contract_required": PRODUCT[p][3], "partner_bank_required": PRODUCT[p][4]}
            for p in product_ids]


def route_lane(segment, risk, entities, product_ids):
    return routing.route_lane(segment, risk, entities, products_info(product_ids))


def pick_member(team_id):
    n = next(t[3] for t in TEAMS if t[0] == team_id)
    ids = [f"{team_id}-{chr(65 + i)}" for i in range(n)]
    weights = [MEMBER_WEIGHTS.get(i, 1) for i in ids]
    return random.choices(ids, weights=weights)[0]


def slip_factor(track, lane):
    r = random.random()
    if r < 0.68:
        f = random.uniform(0.6, 0.95)
    elif r < 0.88:
        f = random.uniform(0.95, 1.3)
    else:
        f = random.uniform(1.35, 1.9)
    if track == "Tech build" and lane in ("Complex", "Exceptions"):
        f += 0.1
    if track == "Partner bank approval":
        f += 0.15
    return f


def expected_daily(product, segment):
    return round(BASE_DAILY_REV[product] * SEGMENT_MULT[segment] * random.uniform(0.7, 1.3), 2)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
rows = {k: [] for k in ["teams", "team_members", "products", "lanes", "stage_benchmarks",
                        "clients", "client_products", "onboarding_stages", "revenue_forecast",
                        "daily_transactions", "touchpoints"]}
counters = {"cp": 0, "st": 0, "tp": 0}


def next_id(kind, prefix, width):
    counters[kind] += 1
    return f"{prefix}-{counters[kind]:0{width}d}"


for team_id, name, track, n in TEAMS:
    rows["teams"].append((team_id, name, track))
    for i in range(n):
        letter = chr(65 + i)
        rows["team_members"].append((f"{team_id}-{letter}", team_id, f"{name} - Member {letter}",
                                     f"{team_id.lower()}.member{letter.lower()}@example.com",
                                     "Lead" if i == 0 else "Specialist"))
rows["products"] = list(PRODUCTS)
rows["lanes"] = list(LANES)
for lane, tracks in BENCH.items():
    for track, days in tracks.items():
        rows["stage_benchmarks"].append((lane, track, days, 0 if track == "AM handoff" else 1))


def simulate_tracks(lane, start, historic, shared_cache=None):
    """Return dict track -> outcome for parallel tracks. historic=True forces all Done."""
    out = {}
    for track in PARALLEL_TRACKS:
        target = BENCH[lane][track]
        if target == 0:
            continue
        if shared_cache is not None and track in SHARED_TRACKS and track in shared_cache:
            out[track] = shared_cache[track]
            continue
        f = slip_factor(track, lane)
        dur = max(1, round(target * f))
        res = {"target": target, "dur": dur, "factor": f, "member": pick_member(TRACK_TEAM[track])}
        out[track] = res
        if shared_cache is not None and track in SHARED_TRACKS:
            shared_cache[track] = res
    return out


def build_journey(cp_id, lane, start, outcomes, historic):
    """Create onboarding_stages rows; return (forecast_go_live, actual_go_live, contract_signed)."""
    planned_parallel_end = start + timedelta(days=max(o["target"] for o in outcomes.values()))
    parallel_ends = []
    contract_signed = None
    for track, o in outcomes.items():
        planned_end = start + timedelta(days=o["target"])
        end = start + timedelta(days=o["dur"])
        status, actual_end, forecast_end, note, since = "Done", end, None, None, None
        if not historic and end > TODAY:
            actual_end = None
            forecast_end = end
            status = "In progress"
            overdue = TODAY > planned_end
            if overdue or (o["factor"] > 1.35 and random.random() < 0.6):
                status = "Blocked"
                note = random.choice(BLOCKERS[track])
                since = min(TODAY - timedelta(days=1),
                            max(start + timedelta(days=1), start + timedelta(days=int(o["target"] * 0.6))))
                forecast_end = max(forecast_end, TODAY + timedelta(days=random.randint(3, 15)))
        if track == "Contract" and actual_end:
            contract_signed = actual_end
        parallel_ends.append(actual_end or forecast_end)
        rows["onboarding_stages"].append((
            next_id("st", "ST", 5), cp_id, track, TRACK_TEAM[track], o["member"],
            d(start), d(planned_end), d(start), d(actual_end), d(forecast_end), status, note, d(since)))

    # AM handoff runs after all parallel tracks
    am_target = BENCH[lane]["AM handoff"]
    am_planned_start = planned_parallel_end
    am_planned_end = am_planned_start + timedelta(days=am_target)
    parallel_done = max(parallel_ends)
    am_member = pick_member("AM")
    am_dur = max(1, round(am_target * random.uniform(0.8, 1.5)))
    am_end = parallel_done + timedelta(days=am_dur)
    if historic:
        rows["onboarding_stages"].append((next_id("st", "ST", 5), cp_id, "AM handoff", "AM", am_member,
                                          d(am_planned_start), d(am_planned_end), d(parallel_done), d(am_end),
                                          None, "Done", None, None))
        return None, am_end, contract_signed
    all_done = all(e <= TODAY for e in parallel_ends)
    if all_done:
        if am_end <= TODAY:  # keep journey in flight: handoff still being completed
            am_end = TODAY + timedelta(days=random.randint(1, 4))
        status, a_start = "In progress", parallel_done
        note = random.choice(BLOCKERS["AM handoff"]) if TODAY > am_planned_end and random.random() < 0.3 else None
    else:
        status, a_start, note = "Not started", None, None
    rows["onboarding_stages"].append((next_id("st", "ST", 5), cp_id, "AM handoff", "AM", am_member,
                                      d(am_planned_start), d(am_planned_end), d(a_start), None, d(am_end),
                                      "Blocked" if note else status, note, d(TODAY - timedelta(days=2)) if note else None))
    return am_end, None, contract_signed


def add_forecast(cp_id, target_go_live, exp_daily):
    ramp = [0.4, 0.7, 0.9] + [1.0] * 9
    m = date(target_go_live.year, target_go_live.month, 1)
    for i, pct in enumerate(ramp):
        nxt = date(m.year + (m.month // 12), m.month % 12 + 1, 1)
        days = (nxt - m).days
        rows["revenue_forecast"].append((cp_id, d(m), i + 1, round(exp_daily * pct * days, 2)))
        m = nxt


def add_transactions(cp_id, product, go_live, exp_daily, ramp_days, plateau):
    day, t = go_live, 0
    while day < TODAY:
        level = plateau * min(1.0, (t + 1) / ramp_days) ** 0.8
        wk = {5: 0.65, 6: 0.5}.get(day.weekday(), 1.15)
        rev = round(exp_daily * level * wk * random.uniform(0.88, 1.12), 2)
        if product == "VERIFY":
            count, vol = max(0, round(rev / VERIFY_FEE)), 0.0
        else:
            vol = round(rev / TAKE_RATE[product], 2)
            count = max(0, round(vol / (AVG_TICKET[product] * random.uniform(0.85, 1.15))))
        rows["daily_transactions"].append((cp_id, d(day), count, vol, rev))
        day += timedelta(days=1)
        t += 1


TP_TEXT = {
    "LEGAL": "Signed MSA addendum and DPA",
    "COMPLIANCE": "UBO register, director ID and source of funds evidence",
    "SOLENG": "Webhook endpoint, IP allowlist and sandbox test sign-off",
    "COMPOPS": "Expected corridors, volumes and use-case confirmation",
    "PARTNER": "Partner bank EDD questionnaire",
    "AM": "Go-live sign-off and operational contacts",
}


def add_touchpoints(client_id, cp_id, lane, deal_date, window_start, window_end, n_post):
    # Pre-sale (not counted against the cap)
    for _ in range(random.randint(1, 2)):
        when = deal_date - timedelta(days=random.randint(3, 25))
        rows["touchpoints"].append((next_id("tp", "TP", 4), client_id, None, d(when), "Pre-sale", "SALES",
                                    pick_member("SALES"), random.choice(["Call", "Email"]), "Information request",
                                    "Commercial discovery and pricing discussion", 0, "Sales"))
    teams = ["LEGAL", "COMPLIANCE", "SOLENG", "COMPOPS", "AM"] + (["PARTNER"] if lane == "Exceptions" else [])
    span = max(1, (window_end - window_start).days)
    dates = sorted(window_start + timedelta(days=random.randint(0, span)) for _ in range(n_post))
    for when in dates:
        if random.random() < 0.35:
            covered = random.sample(teams, k=min(len(teams), random.randint(2, 3)))
            lead = covered[0]
            desc = "Consolidated request: " + "; ".join(TP_TEXT[t] for t in covered)
            bundled = 1
        else:
            covered = [random.choice(teams)]
            lead = covered[0]
            desc = TP_TEXT[lead]
            bundled = 0
        rtype = "Sign-off" if lead == "AM" else random.choice(["Document request", "Information request"])
        rows["touchpoints"].append((next_id("tp", "TP", 4), client_id, cp_id, d(when), "Post-sale", lead,
                                    pick_member(lead), random.choice(["Email", "Call", "Portal"]), rtype, desc,
                                    bundled, ", ".join(TEAM_NAME[t] for t in covered)))


START_AGO = {"Fast": (1, 9), "Standard": (4, 30), "Complex": (8, 58), "Exceptions": (10, 75)}
STALL_CHANCE = 0.18   # share of journeys that started long ago (to surface stalls / escalations)
STALL_AGO = {"Fast": (13, 20), "Standard": (35, 55), "Complex": (65, 100), "Exceptions": (80, 120)}

# ---- Clients currently onboarding --------------------------------------------
for i, (name, ind, seg, ctry, risk, ents, prods) in enumerate(ONBOARDING_CLIENTS, start=1):
    client_id = f"CL-{i:03d}"
    lane, reason = route_lane(seg, risk, ents, prods)
    ago = STALL_AGO[lane] if random.random() < STALL_CHANCE else START_AGO[lane]
    start = TODAY - timedelta(days=random.randint(*ago))
    rows["clients"].append((client_id, name, ind, seg, ctry, risk, ents, "Onboarding",
                            pick_member("SALES"), pick_member("AM"), d(start)))
    shared = {}
    first_cp = None
    for p in prods:
        cp_id = next_id("cp", "CP", 4)
        first_cp = first_cp or cp_id
        outcomes = simulate_tracks(lane, start, historic=False, shared_cache=shared)
        fc_go_live, _, signed = build_journey(cp_id, lane, start, outcomes, historic=False)
        target = start + timedelta(days=LANE_TARGET[lane])
        elapsed = (TODAY - start).days
        cur_lane, cur_reason = lane, reason
        if routing.should_escalate(lane, elapsed):
            cur_lane, cur_reason = "Exceptions", f"{reason}; escalated: stalled beyond 1.5x {lane} target"
        exp = expected_daily(p, seg)
        rows["client_products"].append((cp_id, client_id, p, 1, lane, cur_lane, cur_reason, d(signed),
                                        d(start), d(target), d(fc_go_live), None, "Onboarding", exp))
        add_forecast(cp_id, target, exp)
    n_post = random.choices([1, 2, 3, 4, 5], weights=[3, 5, 4, 1.5, 0.7])[0]
    add_touchpoints(client_id, first_cp, lane, start, start, TODAY, n_post)

# ---- Live clients (first product live, some with a second product) ------------
plateau_misses = {"Monarch Gig Works", "Cinder Games"}  # never reach expected revenue
for j, (name, ind, seg, ctry, risk, ents, p1, p2, p2_status) in enumerate(LIVE_CLIENTS, start=31):
    client_id = f"CL-{j:03d}"
    lane1, reason1 = route_lane(seg, risk, ents, [p1])
    start1 = TODAY - timedelta(days=random.randint(150, 420))
    rows["clients"].append((client_id, name, ind, seg, ctry, risk, ents, "Live",
                            pick_member("SALES"), pick_member("AM"), d(start1)))
    cp1 = next_id("cp", "CP", 4)
    outcomes = simulate_tracks(lane1, start1, historic=True)
    _, live1, signed1 = build_journey(cp1, lane1, start1, outcomes, historic=True)
    exp1 = expected_daily(p1, seg)
    rows["client_products"].append((cp1, client_id, p1, 1, lane1, lane1, reason1, d(signed1), d(start1),
                                    d(start1 + timedelta(days=LANE_TARGET[lane1])), None, d(live1), "Live", exp1))
    add_forecast(cp1, start1 + timedelta(days=LANE_TARGET[lane1]), exp1)
    plateau = random.uniform(0.72, 0.84) if name in plateau_misses else random.uniform(1.0, 1.1)
    add_transactions(cp1, p1, live1, exp1, random.randint(25, 140), plateau)
    add_touchpoints(client_id, cp1, lane1, start1, start1, live1 + timedelta(days=20),
                    random.choices([1, 2, 3], weights=[3, 4, 2])[0])

    if p2:
        lane2, reason2 = route_lane(seg, risk, 1, [p2])  # second product: entities already onboarded
        cp2 = next_id("cp", "CP", 4)
        exp2 = expected_daily(p2, seg)
        if p2_status == "Live":
            start2 = live1 + timedelta(days=random.randint(25, 120))
            start2 = min(start2, TODAY - timedelta(days=LANE_TARGET[lane2] * 2 + 20))
            outcomes2 = simulate_tracks(lane2, start2, historic=True)
            _, live2, signed2 = build_journey(cp2, lane2, start2, outcomes2, historic=True)
            rows["client_products"].append((cp2, client_id, p2, 2, lane2, lane2, reason2, d(signed2), d(start2),
                                            d(start2 + timedelta(days=LANE_TARGET[lane2])), None, d(live2),
                                            "Live", exp2))
            add_transactions(cp2, p2, live2, exp2, random.randint(20, 90), random.uniform(0.95, 1.1))
            n2 = random.choices([0, 1, 2], weights=[2, 3, 1])[0]
            add_touchpoints(client_id, cp2, lane2, start2, start2, live2, n2)
        else:
            start2 = TODAY - timedelta(days=random.randint(20, 55))
            outcomes2 = simulate_tracks(lane2, start2, historic=False)
            fc2, _, signed2 = build_journey(cp2, lane2, start2, outcomes2, historic=False)
            rows["client_products"].append((cp2, client_id, p2, 2, lane2, lane2, reason2, d(signed2), d(start2),
                                            d(start2 + timedelta(days=LANE_TARGET[lane2])), d(fc2), None,
                                            "Onboarding", exp2))
            add_touchpoints(client_id, cp2, lane2, start2, start2, TODAY, random.randint(0, 2))
        add_forecast(cp2, start2 + timedelta(days=LANE_TARGET[lane2]), exp2)

# ---------------------------------------------------------------------------
# Write SQLite + CSV
# ---------------------------------------------------------------------------
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
con = sqlite3.connect(DB_PATH)
with open(os.path.join(HERE, "schema.sql")) as f:
    con.executescript(f.read())
os.makedirs(CSV_DIR, exist_ok=True)
for table, data in rows.items():
    cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
    con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(cols))})", data)
    with open(os.path.join(CSV_DIR, f"{table}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(data)
con.commit()
for table in rows:
    print(f"{table:20s} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>6}")

# Metric layer: views over the loaded data
with open(os.path.join(HERE, "metrics.sql")) as f:
    con.executescript(f.read())
as_of = con.execute("SELECT as_of_date FROM v_as_of").fetchone()[0]
assert as_of == d(TODAY), f"metrics.sql as-of date {as_of} does not match TODAY {d(TODAY)}"
views = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name")]
print(f"\nViews loaded: {', '.join(views)}")
con.close()
print(f"\nDatabase written to {DB_PATH} (as of {TODAY})")
