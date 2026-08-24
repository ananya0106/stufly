"""
poller.py
----------
Background price-tracking script. Runs continuously, checking a fixed
list of routes on a timer, independent of any user activity. This is
what actually builds real price history over time -- the data
recommendations.py needs for genuine buy-now-vs-wait signals.

Run this as its own long-running process (a separate Railway service),
NOT as part of the main API server -- keeps the API responsive and the
polling schedule independent of web traffic.
"""
import asyncio
from datetime import datetime

from flights import search_direct_flight
from price_history import save_price

# Routes to track. Start small and expand once this is confirmed working --
# each route adds real scraping load, and fast-flights is not built for
# high request volume.
TRACKED_ROUTES = [
    ("DEL", "YYZ"),  # Delhi -> Toronto
    ("BOM", "YVR"),  # Mumbai -> Vancouver
    ("DEL", "YUL"),  # Delhi -> Montreal
    ("BOM", "YYZ"),  # Mumbai -> Toronto
]

# How often to check each route, in seconds. 3 hours = 10800 seconds.
# Chosen to catch same-day price swings (like a sudden Emirates promo)
# without hammering the scraper -- fast-flights is not an official API
# and aggressive polling risks getting blocked.
POLL_INTERVAL_SECONDS = 3 * 60 * 60

# How many days ahead to check prices for. A fixed offset (e.g. 60 days
# out) keeps the comparison consistent across polls -- checking "today's
# date" would mean the days_left value keeps shifting, making price
# history harder to compare apples-to-apples.
DAYS_AHEAD = 60


def get_target_date() -> str:
    from datetime import timedelta
    target = datetime.now() + timedelta(days=DAYS_AHEAD)
    return target.strftime("%Y-%m-%d")


async def poll_once():
    """One full pass: check every tracked route, save whatever comes back."""
    travel_date = get_target_date()
    print(f"[poller] Starting poll cycle for {travel_date} at {datetime.now().isoformat(timespec='seconds')}")

    for origin, destination in TRACKED_ROUTES:
        try:
            result = await asyncio.to_thread(search_direct_flight, origin, destination, travel_date)

            if result:
                save_price(origin, destination, result["price"])
                print(f"[poller] {origin}->{destination} on {travel_date}: {result['price']} (saved)")
            else:
                print(f"[poller] {origin}->{destination} on {travel_date}: no results")

        except Exception as e:
            print(f"[poller] {origin}->{destination} failed: {e}")

        # small delay between routes so we're not firing requests back-to-back
        await asyncio.sleep(5)

    print(f"[poller] Poll cycle complete.")


async def run_forever():
    """Main loop: poll, sleep, repeat, forever."""
    print(f"[poller] Starting background price poller.")
    print(f"[poller] Tracking {len(TRACKED_ROUTES)} routes, checking every {POLL_INTERVAL_SECONDS // 3600} hours.")

    while True:
        await poll_once()
        print(f"[poller] Sleeping for {POLL_INTERVAL_SECONDS // 3600} hours until next cycle...")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())