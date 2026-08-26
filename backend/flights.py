from cache import reroute_cache
from discounts import get_best_price_discount
import asyncio
import os
from datetime import datetime

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound
from fast_flights.integrations import SearchApi

# flights.py
#
# This file is the only place in the whole codebase that actually talks to
# the flight-search service. Everything else -- main.py, poller.py,
# notifications.py -- goes through the functions here rather than calling
# SearchApi directly. That's deliberate: if the underlying data source
# ever changes (a different provider, a pricing change, whatever), we only
# have to fix it in one place instead of hunting through the whole app.
#
# We're using SearchApi rather than scraping Google Flights ourselves.
# We tried the raw scraper first and it worked fine locally, but the moment
# it ran on Railway's servers it got silently blocked -- Google treats
# requests from cloud/datacenter IPs very differently from a normal home
# connection. SearchApi is a real, paid API that reads the same Google
# Flights data through a legitimate channel, so it doesn't hit that wall.

_searchapi_client = None


def _get_searchapi():
    # Built once and reused, rather than creating a new client on every
    # single search -- no real reason to pay that setup cost repeatedly.
    global _searchapi_client
    if _searchapi_client is None:
        _searchapi_client = SearchApi(api_key=os.getenv("SEARCHAPI_KEY"))
    return _searchapi_client


def _extract_booking_options(flight):
    """
    Google Flights results don't just give you one price -- for a lot of
    flights they'll show a handful of places you could actually book it
    (the airline's own site, plus a few travel agents), each with its own
    price for the exact same flight. We want that, because it's the whole
    basis for telling a student "book direct" vs "book through this other
    site instead" with a real number behind the recommendation, not a
    guess.

    Different SDK versions expose this slightly differently, so we check
    a couple of possible attribute names rather than assuming one. If none
    of them are there, we just return an empty list -- the rest of the app
    is written to cope with that (falls back to only knowing the one
    headline price).
    """
    raw = getattr(flight, "booking_options", None) or getattr(flight, "booking_token_options", None)
    if not raw:
        return []

    options = []
    for opt in raw:
        options.append({
            "book_with": getattr(opt, "book_with", None) or getattr(opt, "provider", "Unknown"),
            "price": getattr(opt, "price", None),
        })

    # Drop anything where we couldn't actually get a price -- a booking
    # option with no price is useless for comparison and would just
    # confuse the "cheapest source" calculation below.
    return [o for o in options if o["price"] is not None]


def search_direct_flight(origin: str, destination: str, travel_date: str, seat_class: str = "economy"):
    """
    Looks up the cheapest flight for a single origin/destination/date, in
    a given cabin class. This is the workhorse function -- pretty much
    every other search in the app (reroutes, the poller, the tracker) is
    built on top of calling this repeatedly for different legs or dates.

    Returns a plain dict rather than whatever object the SDK hands back,
    because a plain dict is trivial to turn into JSON for the API and easy
    to store in the database -- no risk of some SDK-specific object
    failing to serialize somewhere downstream.

    Returns None if nothing came back at all, so callers can just check
    "if result:" rather than digging through an empty structure.
    """
    query = create_query(
        flights=[
            FlightQuery(date=travel_date, from_airport=origin, to_airport=destination)
        ],
        trip="one-way",
        seat=seat_class,
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
        # This is a scraped/aggregated data source, not a rock-solid
        # official API -- it can hiccup for all sorts of reasons (rate
        # limiting, a route with genuinely no results, a temporary outage
        # on their end). We don't want one flaky call to take down
        # whatever's calling us, so we log what happened and hand back
        # None instead of letting the exception propagate.
        print(f"[flights error] {origin}->{destination} on {travel_date} ({seat_class}): {e}")
        return None

    if not results:
        return None

    flight_list = getattr(results, "flights", results)
    if not flight_list:
        return None

    # Google's own ranking already puts the best option first, so we just
    # take that rather than re-sorting anything ourselves.
    best = flight_list[0]
    leg = best.flights[0] if hasattr(best, "flights") else best

    discounted_price, applied_discount = get_best_price_discount(best.airlines, best.price)
    booking_options = _extract_booking_options(best)

    cheapest_booking_option = None
    if booking_options:
        cheapest_booking_option = min(booking_options, key=lambda o: o["price"])

    return {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "seat_class": seat_class,
        "airlines": best.airlines,
        "price": best.price,
        "discounted_price": discounted_price,
        "applied_discount": applied_discount,
        "duration_minutes": getattr(leg, "duration", None),
        "plane_type": getattr(leg, "plane_type", None),
        "num_legs": len(best.flights) if hasattr(best, "flights") else 1,
        "booking_options": booking_options,
        "cheapest_booking_option": cheapest_booking_option,
    }


async def _search_hub(
    origin: str,
    destination: str,
    travel_date: str,
    hub: str,
    semaphore: asyncio.Semaphore,
    seat_class: str = "economy",
    max_retries: int = 2,
    timeout_seconds: int = 20,
):
    """
    Checks a single hub as a possible connection point: origin -> hub, then
    hub -> destination, as two separate direct searches. We retry a couple
    of times with a short backoff before giving up on a hub, because a
    single failed request usually means a passing glitch, not that the
    route genuinely doesn't exist -- we'd rather retry once than wrongly
    tell someone "no flights via Dubai" because of a one-off timeout.

    The semaphore is there to cap how many of these run at the same time
    across all the hubs being checked -- without it, searching 6 hubs
    would fire off a dozen simultaneous requests, which is a good way to
    get rate-limited by the search provider.
    """
    async with semaphore:
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                leg1 = await asyncio.wait_for(
                    asyncio.to_thread(search_direct_flight, origin, hub, travel_date, seat_class),
                    timeout=timeout_seconds,
                )
                leg2 = await asyncio.wait_for(
                    asyncio.to_thread(search_direct_flight, hub, destination, travel_date, seat_class),
                    timeout=timeout_seconds,
                )

                if leg1 and leg2:
                    return {
                        "hub": hub,
                        "leg1": leg1,
                        "leg2": leg2,
                        "total_price": leg1["price"] + leg2["price"],
                        "is_direct": False,
                    }

                # Both legs came back cleanly but empty -- that means the
                # route genuinely has no flights via this hub, which is
                # different from an error. No point retrying that.
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


async def search_reroute_options(origin: str, destination: str, travel_date: str, hubs: list[str], seat_class: str = "economy"):
    """
    The main comparison function: checks the plain direct flight AND every
    candidate hub, and returns them all together, sorted cheapest first.

    We deliberately include the direct flight in the same list as the hub
    options rather than handling it separately -- early on, direct flights
    were only available through a different endpoint entirely, which meant
    a genuinely good direct flight could get buried and never shown
    alongside the reroute options. Comparing them side by side is the
    whole point of this function.
    """
    cache_key_date = f"{travel_date}-{seat_class}"
    cached = reroute_cache.get(origin, destination, cache_key_date)
    if cached is not None:
        print(f"[cache] hit for {origin}-{destination}-{cache_key_date}")
        return cached["results"], cached["failures"]

    print(f"[cache] miss for {origin}-{destination}-{cache_key_date}, scraping...")

    direct = await asyncio.to_thread(search_direct_flight, origin, destination, travel_date, seat_class)

    semaphore = asyncio.Semaphore(3)
    tasks = [_search_hub(origin, destination, travel_date, hub, semaphore, seat_class) for hub in hubs]
    raw_results = await asyncio.gather(*tasks)

    results = [r for r in raw_results if r and "error" not in r]
    failures = [r for r in raw_results if r and "error" in r]

    if direct:
        results.append({
            "hub": None,
            "leg1": direct,
            "leg2": None,
            "total_price": direct["price"],
            "is_direct": True,
        })

    if failures:
        print(f"[reroute] {len(failures)} hub(s) failed:")
        for f in failures:
            print(f"  - {f['timestamp']} | hub={f['hub']} | error={f['error']}")

    # Only cache when we actually got something useful back -- caching an
    # empty result would mean a temporary glitch locks out real results
    # for the next hour, which is worse than just re-trying next time.
    if results:
        reroute_cache.set(origin, destination, cache_key_date, {
            "results": results,
            "failures": failures,
        })

    return results, failures


def search_prices_by_airline(origin: str, destination: str, travel_date: str, airlines: list[str], seat_class: str = "economy") -> dict:
    """
    Built specifically for the email price tracker: someone asks us to
    watch 2-3 named airlines on a route, so we need the price for each of
    those airlines individually -- not just whichever one happens to come
    back cheapest overall, which is all search_direct_flight() gives you.

    Returns a dict of {airline_name: price}, with None for any airline we
    couldn't find in the results for that day (which does happen -- not
    every airline flies every route every day).
    """
    query = create_query(
        flights=[
            FlightQuery(date=travel_date, from_airport=origin, to_airport=destination)
        ],
        trip="one-way",
        seat=seat_class,
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
        currency="INR",
    )

    prices = {a: None for a in airlines}

    try:
        results = get_flights(query, integration=_get_searchapi())
    except Exception as e:
        print(f"[flights error] airline search {origin}->{destination}: {e}")
        return prices

    if not results:
        return prices

    flight_list = getattr(results, "flights", results)
    if not flight_list:
        return prices

    for flight in flight_list:
        flight_airlines = flight.airlines
        if isinstance(flight_airlines, str):
            flight_airlines = [flight_airlines]

        for wanted in airlines:
            if prices[wanted] is not None:
                # Already found this airline's price -- Google's results
                # are roughly ranked cheapest-first anyway, so the first
                # match we hit is the one worth keeping.
                continue
            for fa in flight_airlines:
                # Loose match on the name rather than an exact string
                # comparison -- Google sometimes returns "IndiGo" and
                # sometimes "IndiGo Airlines" for the same carrier, and we
                # don't want to miss a match over that kind of formatting.
                if wanted.lower() in fa.lower() or fa.lower() in wanted.lower():
                    prices[wanted] = flight.price
                    break

    return prices