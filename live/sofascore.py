"""SofaScore API client.

SofaScore puts a Cloudflare TLS-fingerprint check in front of their public
JSON API. Plain ``requests`` gets 403. ``tls_requests`` (a soccerdata transitive
dep) impersonates a real Chrome handshake so the requests go through.

Endpoints used:
  /api/v1/sport/football/scheduled-events/{YYYY-MM-DD}
  /api/v1/sport/football/events/live
  /api/v1/event/{id}
  /api/v1/event/{id}/shotmap

The shotmap is what makes this useful for live alerting: every shot row has
``xg``, ``xgot``, ``isHome``, ``time`` (minute), ``addedTime``, ``timeSeconds``,
``shotType``, and ``situation`` — and it updates while the match is in play
(verified on 2026-05-14 against an in-progress Brazilian cup tie).
"""

from __future__ import annotations

from datetime import date
from typing import Iterator

import tls_requests

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


class SofaScore:
    """Thin TLS-spoofed wrapper around SofaScore's public JSON endpoints."""

    def __init__(self, *, client_identifier: str = "chrome_124") -> None:
        self._client = tls_requests.Client(client_identifier=client_identifier)

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


# Re-export Iterable for the helper signature above
from typing import Iterable  # noqa: E402,F401  (kept at bottom to avoid forward ref)
