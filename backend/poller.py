"""
poller.py
----------
Background price-tracking script. Runs continuously, checking a fixed
list of routes on a timer, independent of any user activity. Saves to
Postgres (via price_history.py) so history survives restarts.

Run this as its own long-running process (a separate Railway service),
NOT as part of the main API server.
"""
import asyncio
from datetime import datetime

from flights import search_direct_flight
from price_history import save_price, init_db

TRACKED_ROUTES = [
    ("DEL", "YYZ"),
    ("BOM", "YVR"),
    ("DEL", "YUL"),
    ("BOM", "YYZ"),
]

POLL_INTERVAL_SECONDS = 3 * 60 * 60
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

        await asyncio.sleep(5)

    print(f"[poller] Poll cycle complete.")


async def run_forever():
    """Main loop: poll, sleep, repeat, forever."""
    init_db()
    print(f"[poller] Starting background price poller.")
    print(f"[poller] Tracking {len(TRACKED_ROUTES)} routes, checking every {POLL_INTERVAL_SECONDS // 3600} hours.")

    while True:
        await poll_once()
        print(f"[poller] Sleeping for {POLL_INTERVAL_SECONDS // 3600} hours until next cycle...")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())