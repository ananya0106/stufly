"""
main.py
-------
FastAPI app. This is the layer the frontend actually talks to.
Run locally with:
    uvicorn main:app --reload
Then visit http://127.0.0.1:8000/docs to test it in the browser.

Auth: all routes except / require an X-API-Key header matching API_KEY
in .env. Set API_KEY=dev-local-key (or anything) in .env for local testing;
generate a real random one before any real deployment.
"""
import os
from datetime import datetime, date

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from dotenv import load_dotenv

load_dotenv()

from flights import search_direct_flight, search_reroute_options
from cache import reroute_cache
from discounts import get_all_discounts, get_discounts_for_airline
from ai_summary import summarize_reroute

app = FastAPI(title="Flight Reroute + Student Fares API")

# --- Rate limiting: caps how often any one IP can hit an endpoint, so a
# buggy frontend retry loop or a malicious script can't hammer the scraper
# (and burn our standing with Google) or run up Groq API costs. ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_HUBS = ["DEL", "BOM", "DXB", "DOH", "IST"]

ENV = os.getenv("ENV", "production")
API_KEY = os.getenv("API_KEY")


def require_dev():
    """Gate for debug-only routes. Raises 404 so the route looks like it doesn't exist."""
    if ENV != "dev":
        raise HTTPException(status_code=404, detail="Not found")


def require_api_key(x_api_key: str = Header(None)):
    """
    Simple shared-secret auth. Every protected route depends on this.
    If API_KEY isn't set in .env at all, auth is effectively disabled --
    fine for early local dev, but flagged so it's not forgotten before deploy.
    """
    if API_KEY is None:
        print("[auth warning] API_KEY not set in .env -- all routes are unprotected")
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


def validate_travel_date(travel_date: str) -> str:
    """
    Confirms travel_date is a real, sane date before it ever reaches the
    scraper. Returns the validated string unchanged, or raises a clean 400.
    """
    try:
        parsed = datetime.strptime(travel_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid travel_date '{travel_date}' -- must be in YYYY-MM-DD format",
        )

    if parsed < date.today():
        raise HTTPException(
            status_code=400,
            detail=f"travel_date '{travel_date}' is in the past",
        )

    return travel_date


@app.get("/")
def root():
    return {"status": "ok", "message": "Flight API is running"}


@app.get("/search/direct", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def search_direct(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    Returns the cheapest direct flight for a given route + date, including
    original price, discounted_price (after best student % discount), and
    matching student discount programs for the airline.
    """
    origin = origin.upper()
    destination = destination.upper()
    travel_date = validate_travel_date(travel_date)

    result = search_direct_flight(origin, destination, travel_date)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No flights found for {origin} -> {destination} on {travel_date}",
        )

    result["student_discounts"] = get_discounts_for_airline(result["airlines"])

    return result


@app.get("/search/reroute", dependencies=[Depends(require_api_key)])
@limiter.limit("5/minute")
async def search_reroute(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
    include_summary: bool = Query(False, description="If true, adds an AI-generated plain-English summary"),
):
    """
    Checks candidate hub airports for a cheaper origin->hub->destination
    routing than flying direct. Cached per (origin, destination, date) for
    an hour; hub checks run concurrently with retries and timeouts; each
    leg is annotated with student discounts and a discounted price.
    """
    origin = origin.upper()
    destination = destination.upper()
    travel_date = validate_travel_date(travel_date)

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

    if include_summary:
        response["ai_summary"] = summarize_reroute(origin, destination, travel_date, results)

    return response


@app.get("/discounts/student", dependencies=[Depends(require_api_key)])
def list_student_discounts():
    """Returns all known student discount programs."""
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