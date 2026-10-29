"""
Lane routing rules for onboarding journeys. Standard library only.

The single source of the routing rules: generate_data.py routes the fake data with them,
and the lane router agent (agents.py) re-checks every journey, flags escalations and
finds boundary cases with them. Lanes are always derived here, never hard-coded.
"""

LANE_TARGET = {"Fast": 10, "Standard": 30, "Complex": 60, "Exceptions": 75}  # days, start to go-live
ESCALATION_FACTOR = 1.5   # a journey stalled beyond 1.5x its lane target moves to Exceptions
RISK_LEVELS = ["Low", "Medium", "High"]
SEGMENTS = ["SMB", "Mid-market", "Enterprise"]


def route_lane(segment, risk, entities, products):
    """Return (lane, reason). Rules are checked in order and the first match wins.

    products: the products onboarded together, each a dict with product_id,
    contract_required and partner_bank_required.
    """
    if any(p["partner_bank_required"] for p in products):
        return "Exceptions", "USDC product requires partner bank sign-off"
    if risk == "High":
        return "Exceptions", "High-risk KYB requires partner bank enhanced due diligence"
    if any(p["product_id"] in ("CARDS", "TRAVEL") for p in products):
        return "Complex", "Cards/Travel programme setup"
    if len(products) > 1:
        return "Complex", "Multi-product onboarding"
    if entities >= 3:
        return "Complex", f"{entities} legal entities to onboard"
    if not products[0]["contract_required"] and risk == "Low" and segment != "Enterprise" and entities == 1:
        return "Fast", "No-contract product, low risk, single entity"
    return "Standard", "Contracted single product, low/medium risk"


def escalation_day(lane):
    """First elapsed day on which a journey in this lane escalates, or None for Exceptions."""
    if lane == "Exceptions":
        return None
    return int(ESCALATION_FACTOR * LANE_TARGET[lane]) + 1


def should_escalate(lane, elapsed_days):
    day = escalation_day(lane)
    return day is not None and elapsed_days >= day


def boundary_flips(segment, risk, entities, products):
    """One-step changes to a client's attributes that would change its lane.

    Returns a list of (change, new_lane), for example ("risk Low", "Fast").
    Products are left alone: they are the deal itself, not a client attribute.
    """
    lane = route_lane(segment, risk, entities, products)[0]
    candidates = []
    i = RISK_LEVELS.index(risk)
    candidates += [(f"risk {RISK_LEVELS[j]}", (segment, RISK_LEVELS[j], entities)) for j in (i - 1, i + 1)
                   if 0 <= j < len(RISK_LEVELS)]
    i = SEGMENTS.index(segment)
    candidates += [(f"segment {SEGMENTS[j]}", (SEGMENTS[j], risk, entities)) for j in (i - 1, i + 1)
                   if 0 <= j < len(SEGMENTS)]
    candidates += [(f"{n} legal entit{'y' if n == 1 else 'ies'}", (segment, risk, n)) for n in (entities - 1, entities + 1)
                   if n >= 1]
    flips = []
    for change, (seg, rsk, ents) in candidates:
        new_lane = route_lane(seg, rsk, ents, products)[0]
        if new_lane != lane:
            flips.append((change, new_lane))
    return flips
