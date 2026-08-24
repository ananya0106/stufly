"""
nlp_parser.py
--------------
Converts natural-language travel input into the structured (origin,
destination, travel_date) the rest of the app expects.

Two separate problems:
1. DATES: "31 Aug - 2 Sep" or "early September" -> a real YYYY-MM-DD
   (or a date range to check across, for search_date_range()).
2. LOCATIONS: "Gujarat" or "Toronto" -> an actual 3-letter airport code,
   since fast-flights needs a specific airport, not a state/city name.
   This is a static lookup, not live geocoding -- it only knows the
   cities/regions we've explicitly mapped. Expand AIRPORT_MAP as needed.
"""
import dateparser

AIRPORT_MAP = {
    "gujarat": "AMD", "ahmedabad": "AMD",
    "delhi": "DEL", "new delhi": "DEL",
    "maharashtra": "BOM", "mumbai": "BOM",
    "karnataka": "BLR", "bangalore": "BLR", "bengaluru": "BLR",
    "tamil nadu": "MAA", "chennai": "MAA",
    "west bengal": "CCU", "kolkata": "CCU",
    "telangana": "HYD", "hyderabad": "HYD",
    "punjab": "ATQ", "amritsar": "ATQ",
    "rajasthan": "JAI", "jaipur": "JAI",
    "kerala": "COK", "kochi": "COK", "cochin": "COK",
    "uttar pradesh": "LKO", "lucknow": "LKO",
    "toronto": "YYZ", "ontario": "YYZ",
    "vancouver": "YVR", "british columbia": "YVR",
    "montreal": "YUL", "quebec": "YUL",
    "calgary": "YYC", "alberta": "YYC",
    "ottawa": "YOW",
    "waterloo": "YYZ",
    "dubai": "DXB", "doha": "DOH", "istanbul": "IST",
    "london": "LHR", "new york": "JFK",
}


def resolve_airport(location_text: str) -> str | None:
    """
    Turns a free-text location into an airport code.
    Returns None if not found -- caller must handle this, never guess silently.
    """
    if not location_text:
        return None

    key = location_text.strip().lower()

    if len(key) == 3 and key.isalpha():
        return key.upper()

    return AIRPORT_MAP.get(key)


def parse_date_range(text: str) -> tuple[str, str] | None:
    """
    Parses natural language date/date-range text into (start_date, end_date)
    as YYYY-MM-DD strings.
    """
    if not text:
        return None

    text = text.strip()
    settings = {"PREFER_DATES_FROM": "future"}

    for separator in [" - ", "-", " to ", " until "]:
        if separator in text:
            parts = text.split(separator, 1)
            if len(parts) == 2:
                start_raw, end_raw = parts[0].strip(), parts[1].strip()

                start_parsed = dateparser.parse(start_raw, settings=settings)
                end_parsed = dateparser.parse(end_raw, settings=settings)

                if start_parsed is None and end_parsed is not None:
                    combined = f"{start_raw} {end_parsed.strftime('%B %Y')}"
                    start_parsed = dateparser.parse(combined, settings=settings)

                if start_parsed and end_parsed:
                    return (start_parsed.date().isoformat(), end_parsed.date().isoformat())

    single = dateparser.parse(text, settings=settings)
    if single:
        d = single.date().isoformat()
        return (d, d)

    return None


def parse_travel_request(origin_text: str, destination_text: str, date_text: str) -> dict:
    """
    Top-level entry point for structured (non-free-text) input.
    """
    origin_code = resolve_airport(origin_text)
    destination_code = resolve_airport(destination_text)
    date_range = parse_date_range(date_text)

    errors = []
    if origin_code is None:
        errors.append(f"Couldn't recognize origin '{origin_text}' -- please provide a city name or 3-letter airport code")
    if destination_code is None:
        errors.append(f"Couldn't recognize destination '{destination_text}' -- please provide a city name or 3-letter airport code")
    if date_range is None:
        errors.append(f"Couldn't parse travel date(s) from '{date_text}' -- try a format like '31 Aug - 2 Sep' or '2026-09-01'")

    if errors:
        return {"success": False, "errors": errors}

    return {
        "success": True,
        "origin": origin_code,
        "destination": destination_code,
        "start_date": date_range[0],
        "end_date": date_range[1],
    }