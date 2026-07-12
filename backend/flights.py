from cache import reroute_cache
from discounts import get_best_price_discount
import asyncio
from datetime import datetime

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound

"""
flights.py
-----------
Thin wrapper around the fast-flights library.

Why a separate file: main.py (our API layer) should never talk to
fast-flights directly. If fast-flights changes its internals again,
we only fix this one file instead of hunting through every endpoint.

NOTE ON VERSIONS: fast-flights has changed its API before (older docs
export FlightData/get_flights(flight_data=...), but v3.0.2 -- what
actually installs today -- exports FlightQuery + create_query() instead).
Always check with
    python3 -c "import fast_flights; print(dir(fast_flights))"
before trusting any tutorial, including this one, if pip installs a
different version later.
"""


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
        # fast-flights is a scraper, not an official API -- Google can serve
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

    discounted_price, applied_discount = get_best_price_discount(best.airlines, best.price)

    return {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "airlines": best.airlines,
        "price": best.price,  # original price, e.g. 45231
        "discounted_price": discounted_price,  # after best student % discount, same as price if none applies
        "applied_discount": applied_discount,  # the specific program used, or None
        "duration_minutes": leg.duration,
        "plane_type": leg.plane_type,
        "num_legs": len(best.flights),  # >1 means it's a connecting flight
    }


async def _search_hub(
    origin: str,
    destination: str,
    travel_date: str,
    hub: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = 2,
    timeout_seconds: int = 20,
):
    """
    Checks one hub: origin -> hub -> destination.
    Retries transient failures (network blips, rate limits) up to
    max_retries times with a short backoff, and caps each attempt
    with a timeout so one stuck hub can't stall the whole batch.
    """
    async with semaphore:
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                leg1 = await asyncio.wait_for(
                    asyncio.to_thread(search_direct_flight, origin, hub, travel_date),
                    timeout=timeout_seconds,
                )
                leg2 = await asyncio.wait_for(
                    asyncio.to_thread(search_direct_flight, hub, destination, travel_date),
                    timeout=timeout_seconds,
                )

                if leg1 and leg2:
                    return {
                        "hub": hub,
                        "leg1": leg1,
                        "leg2": leg2,
                        "total_price": leg1["price"] + leg2["price"],
                    }

                # got a clean response but no flights exist on this route --
                # not a transient error, retrying won't help, so stop here
                return {
                    "hub": hub,
                    "error": "no_results",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }

            except asyncio.TimeoutError:
                last_error = f"timeout after {timeout_seconds}s"
            except Exception as e:
                last_error = str(e)

            # a real failure happened -- back off before retrying,
            # unless this was the last attempt
            if attempt < max_retries:
                print(f"[reroute] hub={hub} attempt {attempt} failed ({last_error}), retrying...")
                await asyncio.sleep(2 * attempt)  # 2s, then 4s, etc.

        # all retries exhausted
        return {
            "hub": hub,
            "error": last_error,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }


async def search_reroute_options(origin: str, destination: str, travel_date: str, hubs: list[str]):
    """
    Checks a list of candidate hub airports for a cheaper origin->hub->destination
    routing than a direct flight. Runs hub checks concurrently (capped at 3 at once)
    and caches results per (origin, destination, date) for an hour.
    """
    cached = reroute_cache.get(origin, destination, travel_date)
    if cached is not None:
        print(f"[cache] hit for {origin}-{destination}-{travel_date}")
        return cached["results"], cached["failures"]

    print(f"[cache] miss for {origin}-{destination}-{travel_date}, scraping...")

    semaphore = asyncio.Semaphore(3)  # cap concurrent hub checks so we don't hammer the scraper
    tasks = [_search_hub(origin, destination, travel_date, hub, semaphore) for hub in hubs]
    raw_results = await asyncio.gather(*tasks)

    results = [r for r in raw_results if r and "error" not in r]
    failures = [r for r in raw_results if r and "error" in r]

    if failures:
        print(f"[reroute] {len(failures)} hub(s) failed:")
        for f in failures:
            print(f"  - {f['timestamp']} | hub={f['hub']} | error={f['error']}")

    if results:
        reroute_cache.set(origin, destination, travel_date, {
            "results": results,
            "failures": failures,
        })

    return results, failures