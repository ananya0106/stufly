from cache import reroute_cache
from discounts import get_best_price_discount
import asyncio
import os
from datetime import datetime

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound
from fast_flights.integrations import SearchApi

"""
flights.py
-----------
Thin wrapper around the fast-flights library.

Uses the SearchApi integration (a real, paid API) instead of raw scraping.
Why: direct scraping gets IP-blocked reliably on cloud hosts (Railway,
AWS, etc.) since Google flags datacenter IPs -- confirmed by our own
deployment. SearchApi routes through a legitimate paid channel, so it
doesn't hit that wall. Requires SEARCHAPI_KEY in .env / Railway variables.
"""

_searchapi_client = None


def _get_searchapi():
    global _searchapi_client
    if _searchapi_client is None:
        _searchapi_client = SearchApi(api_key=os.getenv("SEARCHAPI_KEY"))
    return _searchapi_client


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
        results = get_flights(query, integration=_get_searchapi())
    except FlightsNotFound:
        return None
    except Exception as e:
        print(f"[flights error] {origin}->{destination} on {travel_date}: {e}")
        return None

    if not results:
        return None

    # SearchApi's Result object may expose flights differently than the raw
    # scraper (a .flights attribute vs a plain list) -- handle both shapes
    # rather than assuming one, since we've seen this vary.
    flight_list = getattr(results, "flights", results)

    if not flight_list:
        return None

    best = flight_list[0]
    leg = best.flights[0] if hasattr(best, "flights") else best

    discounted_price, applied_discount = get_best_price_discount(best.airlines, best.price)

    return {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "airlines": best.airlines,
        "price": best.price,
        "discounted_price": discounted_price,
        "applied_discount": applied_discount,
        "duration_minutes": getattr(leg, "duration", None),
        "plane_type": getattr(leg, "plane_type", None),
        "num_legs": len(best.flights) if hasattr(best, "flights") else 1,
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
    Retries transient failures up to max_retries times with backoff,
    and caps each attempt with a timeout.
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

                return {
                    "hub": hub,
                    "error": "no_results",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }

            except asyncio.TimeoutError:
                last_error = f"timeout after {timeout_seconds}s"
            except Exception as e:
                last_error = str(e)

            if attempt < max_retries:
                print(f"[reroute] hub={hub} attempt {attempt} failed ({last_error}), retrying...")
                await asyncio.sleep(2 * attempt)

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

    semaphore = asyncio.Semaphore(3)
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