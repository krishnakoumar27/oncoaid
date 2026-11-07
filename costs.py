"""Conservative, worker-entered treatment cost planning calculations."""
from decimal import Decimal, InvalidOperation


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Amounts must be valid numbers") from None
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
        raise ValueError("Amounts must be non-negative with at most two decimal places")
    return result


def calculate_plan(phases, coverage):
    """Coverage entries are manually allocated non-overlapping amounts per phase.

    Each entry has a status of confirmed or potential. Potential coverage is
    excluded from the conservative cash amount until the funder confirms it.
    """
    phase_totals = {}
    for phase in phases:
        name = str(phase.get("name", "")).strip()
        if not name or name in phase_totals:
            raise ValueError("Each treatment phase needs a unique name")
        phase_totals[name] = amount(phase["amount"])
    totals = {name: {"confirmed": Decimal(0), "potential": Decimal(0)} for name in phase_totals}
    for entry in coverage:
        phase = entry.get("phase")
        status = entry.get("status")
        if phase not in totals or status not in ("confirmed", "potential"):
            raise ValueError("Coverage needs a valid phase and status")
        if not str(entry.get("source", "")).strip():
            raise ValueError("Each coverage allocation needs a source")
        totals[phase][status] += amount(entry["amount"])
    rows = []
    for name, cost in phase_totals.items():
        confirmed, potential = totals[name]["confirmed"], totals[name]["potential"]
        if confirmed + potential > cost:
            raise ValueError(f"Coverage allocations exceed the cost for {name}")
        rows.append({"phase": name, "cost": cost, "confirmed": confirmed,
                     "potential": potential, "cash_conservative": cost - confirmed,
                     "cash_if_potential_approved": cost - confirmed - potential})
    return {"phases": rows,
            "total_cost": sum((r["cost"] for r in rows), Decimal(0)),
            "confirmed_coverage": sum((r["confirmed"] for r in rows), Decimal(0)),
            "potential_coverage": sum((r["potential"] for r in rows), Decimal(0)),
            "cash_conservative": sum((r["cash_conservative"] for r in rows), Decimal(0)),
            "cash_if_potential_approved": sum((r["cash_if_potential_approved"] for r in rows), Decimal(0))}
