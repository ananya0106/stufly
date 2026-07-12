import time
from typing import Optional, Any


class RerouteCache:
    """
    Simple in-memory cache for reroute search results.
    Keyed by (origin, destination, date). Resets on server restart —
    that's fine for v1, swap for Redis later if needed.
    """

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, dict[str, Any]] = {}

    def _make_key(self, origin: str, destination: str, date: str) -> str:
        return f"{origin.upper()}-{destination.upper()}-{date}"

    def get(self, origin: str, destination: str, date: str) -> Optional[Any]:
        key = self._make_key(origin, destination, date)
        entry = self._store.get(key)
        if entry is None:
            return None
        age = time.time() - entry["timestamp"]
        if age > self.ttl_seconds:
            del self._store[key]
            return None
        return entry["result"]

    def set(self, origin: str, destination: str, date: str, result: Any) -> None:
        key = self._make_key(origin, destination, date)
        self._store[key] = {"result": result, "timestamp": time.time()}

    def stats(self) -> dict:
        now = time.time()
        return {
            "entries": len(self._store),
            "keys": [
                {"key": k, "age_seconds": round(now - v["timestamp"])}
                for k, v in self._store.items()
            ],
        }


reroute_cache = RerouteCache(ttl_seconds=3600)