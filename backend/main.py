"""
main.py
-------
FastAPI app. This is the layer the frontend actually talks to.
Run locally with:
    uvicorn main:app --reload
Then visit http://127.0.0.1:8000/docs to test it in the browser
(FastAPI auto-generates an interactive test page -- use this constantly
while building, it's faster than curl).
"""
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from flights import search_direct_flight, search_reroute_options
from cache import reroute_cache

app = FastAPI(title="Flight Reroute + Student Fares API")

# CORS: without this, your React frontend (running on a different port,
# e.g. localhost:5173) will be blocked by the browser from calling this API.
# In production you'd restrict allow_origins to your actual frontend domain
# instead of "*", but "*" is fine while developing locally.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Candidate connecting airports to check when looking for a cheaper reroute.
# Starting with major international hubs -- tune this list based on which
# routes you're actually testing (e.g. add DXB, DOH if targeting Gulf routes,
# or LHR, FRA if targeting Europe).
DEFAULT_HUBS = ["DEL", "BOM", "DXB", "DOH", "IST"]

ENV = os.getenv("ENV", "production")


def require_dev():
    """Gate for debug-only routes. Raises 404 so the route looks like it doesn't exist."""
    if ENV != "dev":
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/")
def root():
    return {"status": "ok", "message": "Flight API is running"}


@app.get("/search/direct")
def search_direct(
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    Returns the cheapest direct flight for a given route + date.
    This is the simplest possible endpoint -- no reroute logic,
    just: does fast-flights give us real data back.
    """
    origin = origin.upper()
    destination = destination.upper()
    result = search_direct_flight(origin, destination, travel_date)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No flights found for {origin} -> {destination} on {travel_date}",
        )
    return result


@app.get("/search/reroute")
async def search_reroute(
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    Checks candidate hub airports for a cheaper origin->hub->destination
    routing than flying direct. Results are cached per (origin, destination, date)
    for an hour, hub checks run concurrently (capped at 3 at once), and each
    hub gets retried on transient failure before being marked failed.
    """
    origin = origin.upper()
    destination = destination.upper()

    results, failures = await search_reroute_options(origin, destination, travel_date, DEFAULT_HUBS)

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No reroute options found for {origin} -> {destination} on {travel_date} "
                   f"({len(failures)} of {len(DEFAULT_HUBS)} hubs failed)",
        )

    results.sort(key=lambda r: r["total_price"])

    response = {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "options": results,
        "failed_hubs": [f["hub"] for f in failures],
    }

    # partial success -- some hubs failed but we still have usable results
    if failures:
        response["warning"] = (
            f"{len(failures)} of {len(DEFAULT_HUBS)} hubs could not be checked "
            f"({', '.join(f['hub'] for f in failures)}); results may not be complete"
        )

    return response


@app.get("/debug/cache")
async def debug_cache():
    require_dev()
    return reroute_cache.stats()


@app.delete("/debug/cache")
async def clear_cache():
    require_dev()
    entries_cleared = len(reroute_cache._store)
    reroute_cache._store.clear()
    return {"cleared": entries_cleared}