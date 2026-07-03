"""
flights.py
-----------
Thin wrapper around the fast-flights library.

Why a separate file: main.py (our API layer) should never talk to
fast-flights directly. If fast-flights changes its internals again,
we only fix this one file instead of hunting through every endpoint.

NOTE ON VERSIONS: fast-flights has changed its API before (older docs
export FlightData/get_flights(flight_data=...), but v3.0.2 — what
actually installs today — exports FlightQuery + create_query() instead).
Always check with
    python3 -c "import fast_flights; print(dir(fast_flights))"
before trusting any tutorial, including this one, if pip installs a
different version later.
"""

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound


def search_direct_flight(origin: str, destination: str, travel_date: str):
    """
    Search for a one-way flight between two airports on a given date.

    Args:
        origin: 3-letter IATA airport code, e.g. "BOM" (Mumbai)
        destination: 3-letter IATA airport code, e.g. "YYZ" (Toronto)
        travel_date: date string in "YYYY-MM-DD" format

    Returns:
        A plain dict (not the raw fast-flights object) so it's easy to
        turn into JSON and send to the frontend. Returns None if
        nothing is found.
    """
    query = create_query(
        flights=[
            FlightQuery(date=travel_date, from_airport=origin, to_airport=destination)
        ],
        trip="one-way",
        seat="economy",
        passengers=Passengers(
            adults=1, children=0, infants_in_seat=0, infants_on_lap=0
        ),
        currency="INR",
    )

    try:
        results = get_flights(query)
    except FlightsNotFound:
        return None
    except Exception as e:
        # fast-flights is a scraper, not an official API — Google can serve
        # a different page layout, a CAPTCHA, or a blocked response, and the
        # library will fail to parse it. We don't want that to crash our
        # whole endpoint, so we catch broadly here and log what happened.
        # If you see this a lot, try fetch_mode differences or add delays
        # between requests (see fast-flights README on rate limiting).
        print(f"[fast-flights error] {origin}->{destination} on {travel_date}: {e}")
        return None

    if not results:
        return None

    # results is a list of "Flights" entries (each can itself represent
    # a multi-leg option), already effectively sorted with cheapest/best
    # first by Google's own ranking. We take the first as "the" direct price.
    best = results[0]
    leg = best.flights[0]  # the first physical flight segment

    return {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "airlines": best.airlines,
        "price": best.price,  # already an int, e.g. 45231
        "duration_minutes": leg.duration,
        "plane_type": leg.plane_type,
        "num_legs": len(best.flights),  # >1 means it's a connecting flight
    }
