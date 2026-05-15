"""SofaScore API client.

SofaScore fronts its public JSON API with a Cloudflare TLS-fingerprint check
(JA3). Plain ``requests`` always gets 403. We use ``curl_cffi`` to impersonate
a real Chrome TLS handshake so the requests go through. ``curl_cffi`` is a
Python wrapper around ``curl-impersonate``; it has manylinux + Windows wheels
for Python 3.8–3.13.

Endpoints used:
  /api/v1/sport/football/scheduled-events/{YYYY-MM-DD}
  /api/v1/sport/football/events/live
  /api/v1/event/{id}
  /api/v1/event/{id}/shotmap

The shotmap is what makes this useful for live alerting: every shot row has
``xg``, ``xgot``, ``isHome``, ``time`` (minute), ``addedTime``, ``timeSeconds``,
``shotType``, and ``situation`` — and it updates while the match is in play.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Iterator

from curl_cffi.requests import Session

PL_UNIQUE_TOURNAMENT_ID = 17
BASE = "https://api.sofascore.com/api/v1"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
}

# curl_cffi impersonation profile. "chrome124" is the most recent at the time
# of writing; SofaScore's JA3 acceptance has been stable for older profiles too.
_IMPERSONATE = "chrome124"


class SofaScore:
    """TLS-spoofed wrapper around SofaScore's public JSON endpoints."""

    def __init__(self, *, impersonate: str = _IMPERSONATE) -> None:
        self._client = Session(impersonate=impersonate)

    def __enter__(self) -> "SofaScore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def _get(self, path: str) -> dict:
        r = self._client.get(f"{BASE}{path}", headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()

    def scheduled_events(self, day: date) -> list[dict]:
        return self._get(f"/sport/football/scheduled-events/{day.isoformat()}").get(
            "events", []
        )

    def live_events(self) -> list[dict]:
        return self._get("/sport/football/events/live").get("events", [])

    def event(self, event_id: int) -> dict:
        return self._get(f"/event/{event_id}").get("event", {})

    def shotmap(self, event_id: int) -> list[dict]:
        return self._get(f"/event/{event_id}/shotmap").get("shotmap", [])


def filter_pl(events: Iterable[dict]) -> list[dict]:
    return [
        e for e in events
        if (e.get("tournament", {}).get("uniqueTournament", {}) or {}).get("id")
        == PL_UNIQUE_TOURNAMENT_ID
    ]
