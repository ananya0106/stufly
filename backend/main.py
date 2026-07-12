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
from discounts import get_all_discounts, get_discounts_for_airline

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
    Includes original price, discounted_price (after best applicable student
    % discount), and any matching student discount programs for the airline.
    """
    origin = origin.upper()
    destination = destination.upper()
    result = search_direct_flight(origin, destination, travel_date)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No flights found for {origin} -> {destination} on {travel_date}",
        )

    result["student_discounts"] = get_discounts_for_airline(result["airlines"])
    # discounted_price and applied_discount are already set inside search_direct_flight()

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
    hub gets retried on transient failure before being marked failed. Each leg
    of each option is annotated with matching student discount programs and a
    discounted price; discounted_total_price sums both legs' discounted prices.
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

    for option in results:
        option["leg1"]["student_discounts"] = get_discounts_for_airline(option["leg1"]["airlines"])
        option["leg2"]["student_discounts"] = get_discounts_for_airline(option["leg2"]["airlines"])
        # leg1/leg2 already carry discounted_price + applied_discount from search_direct_flight()
        option["discounted_total_price"] = (
            option["leg1"]["discounted_price"] + option["leg2"]["discounted_price"]
        )

    response = {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "options": results,
        "failed_hubs": [f["hub"] for f in failures],
    }

    if failures:
        response["warning"] = (
            f"{len(failures)} of {len(DEFAULT_HUBS)} hubs could not be checked "
            f"({', '.join(f['hub'] for f in failures)}); results may not be complete"
        )

    return response


@app.get("/discounts/student")
def list_student_discounts():
    """
    Returns all known student discount programs. This is the browsable
    catalog the frontend can show on its own page, independent of any search.
    """
    return {"programs": get_all_discounts()}


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