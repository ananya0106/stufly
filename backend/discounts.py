"""
discounts.py
------------
Static dataset of known student flight discount programs.

Why static for now: there's no unified API for airline student discounts --
most of these are verified manually (via StudentUniverse, ISIC card, or the
airline's own student portal), so a curated list is the honest v1. This can
later be swapped for a scraped/DB-backed source without changing the shape
other files depend on (get_all_discounts, get_discounts_for_airline).
"""

STUDENT_DISCOUNTS = [
    {
        "id": "su-general",
        "provider": "StudentUniverse",
        "applicable_airlines": ["Air India", "Emirates", "Qatar Airways", "Lufthansa"],
        "discount_type": "percentage",
        "discount_value": 10,
        "eligibility": "Valid student ID, age 18-35 (varies by partner airline)",
        "how_to_apply": "Book directly through studentuniverse.com with verified student status",
        "notes": "Discount varies by route and season; best on international long-haul routes",
    },
    {
        "id": "airindia-student",
        "provider": "Air India",
        "applicable_airlines": ["Air India"],
        "discount_type": "extra_baggage",
        "discount_value": None,
        "eligibility": "Valid student visa + admission letter for students traveling abroad for study",
        "how_to_apply": "Select 'Student Discount' fare type on airindia.com, upload documents at booking",
        "notes": "Discount is mainly extra baggage allowance (up to 10kg extra), not always a lower fare",
    },
    {
        "id": "emirates-student-club",
        "provider": "Emirates Student Club",
        "applicable_airlines": ["Emirates"],
        "discount_type": "percentage",
        "discount_value": 15,
        "eligibility": "Age 18-31, valid student ID, registered on Emirates Student Club",
        "how_to_apply": "Register free at emirates.com/student-club before booking",
        "notes": "Also includes extra baggage allowance on top of the fare discount",
    },
    {
        "id": "isic-general",
        "provider": "ISIC (International Student Identity Card)",
        "applicable_airlines": ["Qatar Airways", "Turkish Airlines", "Lufthansa"],
        "discount_type": "percentage",
        "discount_value": 8,
        "eligibility": "Valid ISIC card (any full-time student, any nationality)",
        "how_to_apply": "Present ISIC card at time of booking through partner travel agents",
        "notes": "Discount amount varies significantly by partner airline and route",
    },
]


def get_all_discounts():
    """Returns the full list of known student discount programs."""
    return STUDENT_DISCOUNTS


def get_discounts_for_airline(airline_name: str):
    """
    Returns any student discount programs applicable to a given airline name.
    Matching is case-insensitive and does a partial match, since airline
    names from fast-flights results may include extra text (e.g. flight
    numbers or codeshare info).
    """
    if not airline_name:
        return []

    airline_lower = airline_name.lower()
    matches = []

    for program in STUDENT_DISCOUNTS:
        for airline in program["applicable_airlines"]:
            if airline.lower() in airline_lower or airline_lower in airline.lower():
                matches.append(program)
                break

    return matches


def get_best_price_discount(airline_name: str, price: int):
    """
    Finds the best applicable *percentage* discount for an airline and
    computes the resulting discounted price. Extra_baggage-type discounts
    are ignored here since they don't reduce fare -- they stay purely
    informational in the applicable_discounts list.

    Returns (discounted_price, applied_discount) -- applied_discount is
    None if no percentage discount applies.
    """
    programs = get_discounts_for_airline(airline_name)
    percentage_programs = [p for p in programs if p["discount_type"] == "percentage"]

    if not percentage_programs:
        return price, None

    # pick the single best (highest %) discount rather than stacking multiple
    best = max(percentage_programs, key=lambda p: p["discount_value"])
    discounted_price = round(price * (1 - best["discount_value"] / 100))

    return discounted_price, best