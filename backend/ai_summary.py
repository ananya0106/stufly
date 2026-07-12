"""
ai_summary.py
-------------
Takes the ALREADY-COMPUTED reroute results (real prices, real savings --
nothing here is invented by the AI) and asks Groq's Llama model to turn
it into a short, human-readable summary.

Important design rule: the AI never sees raw prices without them already
being real numbers from fast-flights. It only narrates data we've already
validated. This keeps a hallucination from ever turning into a fake price
shown to a user.

Requires: GROQ_API_KEY environment variable set (see .env).
Get a key at https://console.groq.com
"""
import os
from groq import Groq

_client = None


def _get_client():
    """Lazy init so a missing key doesn't crash the whole app at import time."""
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


MODEL = "llama-3.1-8b-instant"  # fast + cheap, right fit for short narration


def summarize_reroute(origin: str, destination: str, travel_date: str, results: list, direct_price: int = None) -> str:
    """
    Args:
        origin, destination, travel_date: the searched route
        results: the exact list returned by search_reroute_options() --
                  each item has hub, leg1, leg2, total_price, discounted_total_price
        direct_price: optional, the direct flight price for comparison if available

    Returns:
        A short (1-2 sentence) plain-English summary string.
        Falls back to a simple template if the API call fails, so a
        flaky/missing key never breaks the whole search endpoint.
    """
    if not results:
        return f"No reroute options found for {origin} -> {destination} on {travel_date}."

    best = results[0]  # already sorted cheapest-first by the caller

    fallback = (
        f"Flying via {best['hub']} costs {best['total_price']} "
        f"(discounted: {best.get('discounted_total_price', best['total_price'])})."
    )

    try:
        prompt = f"""Here is real flight data (already computed, do not alter any numbers):

Route: {origin} -> {destination} on {travel_date}
Best reroute option: via {best['hub']}
  Leg 1: {best['leg1']['origin']} -> {best['leg1']['destination']}, price {best['leg1']['price']}, discounted {best['leg1'].get('discounted_price', best['leg1']['price'])}
  Leg 2: {best['leg2']['origin']} -> {best['leg2']['destination']}, price {best['leg2']['price']}, discounted {best['leg2'].get('discounted_price', best['leg2']['price'])}
  Total: {best['total_price']}, discounted total: {best.get('discounted_total_price', best['total_price'])}
{f"Direct flight price for comparison: {direct_price}" if direct_price else ""}
Total alternatives found: {len(results)}

Write a 1-2 sentence, friendly, plain-English summary a traveler would read on a search results page. Only use the numbers given above -- do not calculate or invent any new figures. Mention the price and briefly note the tradeoff (extra stop, separate tickets), and mention if a student discount is already reflected in the discounted total."""

        response = _get_client().chat.completions.create(
            model=MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        # If the API call fails for any reason (bad key, rate limit, network),
        # fall back to a plain template instead of breaking the endpoint.
        print(f"[ai_summary error] {e}")
        return fallback