"""Pinnacle front-end odds client (sharp live in-play prices).

Reads from ``guest.api.arcadia.pinnacle.com`` — the same JSON endpoint
their own website uses, with a static API key embedded in their public
JS bundle. Read-only, no Pinnacle account required.

Pinnacle is the sharpest commercial bookmaker (~2-3% margin on totals)
— their live in-play odds are the closest available proxy to Betfair
Exchange. Replaces the synthetic historical-median gate as the primary
odds source in alert-only mode; synthetic lookup remains as fallback.

Pinnacle blocks datacenter IPs (Hetzner returns 403 directly), so all
traffic routes through ``config.SOFASCORE_HTTP_PROXY`` (our Webshare
rotating residential proxy — works for both SofaScore and Pinnacle).
"""

from __future__ import annotations

import logging
import time
import unicodedata

import requests

import config

logger = logging.getLogger("sportbet.pinnacle")

_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
# Public read-only API key (embedded in pinnacle.com's JavaScript bundle).
_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-API-Key": _KEY,
    "Accept": "application/json",
    "Referer": "https://www.pinnacle.com/",
}

# SofaScore uniqueTournament.id  ->  Pinnacle leagueId
LEAGUE_MAP: dict[int, int] = {
    17:  1980,   # ENG-Premier League
    8:   2196,   # ESP-La Liga
    23:  2436,   # ITA-Serie A
    35:  1842,   # GER-Bundesliga
    34:  2036,   # FRA-Ligue 1
    242: 2663,   # USA-Major League Soccer
    325: 1834,   # BRA-Serie A
}


def american_to_decimal(a: int | float | None) -> float | None:
    """Convert American odds (-114, +236, ...) to decimal."""
    if a is None:
        return None
    a = float(a)
    return round(1.0 + (100.0 / abs(a)) if a < 0 else 1.0 + (a / 100.0), 4)


def _normalise(s: str) -> str:
    """Strip accents + common suffix/prefix for fuzzy team-name matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().strip()
    for suffix in (" fc", " cf", " ac", " afc", " sc", " ud", " cd"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    for prefix in ("fc ", "cf ", "sc ", "ac ", "as "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.strip()


class Pinnacle:
    """Read-only Pinnacle odds wrapper. Caches matchup lookups per match."""

    def __init__(self, timeout: float = 8.0) -> None:
        self.s = requests.Session()
        self.s.headers.update(_HEADERS)
        proxy = (getattr(config, "SOFASCORE_HTTP_PROXY", "") or "").strip() or None
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        # (league_id, home_normalised, away_normalised) -> (matchup_id, expires_at)
        self._match_cache: dict[tuple, tuple[int, float]] = {}

    def _matchups(self, league_id: int) -> list[dict]:
        r = self.s.get(
            f"{_BASE}/leagues/{league_id}/matchups?withSpecials=false",
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def find_match(
        self, sofa_tournament_id: int, home_team: str, away_team: str,
        ttl: float = 600,
    ) -> int | None:
        """SofaScore (league, teams) -> Pinnacle matchup id, with caching."""
        league_id = LEAGUE_MAP.get(sofa_tournament_id)
        if league_id is None:
            return None
        h, a = _normalise(home_team), _normalise(away_team)
        key = (league_id, h, a)
        cached = self._match_cache.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
        try:
            matchups = self._matchups(league_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Pinnacle matchups fetch failed for league %s: %s",
                           league_id, e)
            return None
        for m in matchups:
            parts = m.get("participants", [])
            if len(parts) < 2:
                continue
            ph = _normalise(parts[0].get("name", ""))
            pa = _normalise(parts[-1].get("name", ""))
            # Exact normalised match first
            if ph == h and pa == a:
                mid = int(m["id"])
                self._match_cache[key] = (mid, time.time() + ttl)
                return mid
        # Fuzzy fallback: substring containment both ways
        for m in matchups:
            parts = m.get("participants", [])
            if len(parts) < 2:
                continue
            ph = _normalise(parts[0].get("name", ""))
            pa = _normalise(parts[-1].get("name", ""))
            if (h in ph or ph in h) and (a in pa or pa in a):
                mid = int(m["id"])
                self._match_cache[key] = (mid, time.time() + ttl)
                return mid
        return None

    def over_odds(self, matchup_id: int, line: float) -> float | None:
        """Live decimal odds for the Over X.5 selection at the given line.

        Returns None when the market is closed, the line isn't quoted, or
        the fetch fails. Caller should treat None as "no live read; fall
        back to the synthetic-odds lookup".
        """
        try:
            r = self.s.get(
                f"{_BASE}/matchups/{matchup_id}/markets/straight",
                timeout=self.timeout,
            )
            r.raise_for_status()
            markets = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("Pinnacle markets fetch failed for %s: %s", matchup_id, e)
            return None
        if not isinstance(markets, list):
            return None
        # Full-time totals only (period 0). May appear under main markets or
        # alternates — we don't distinguish, both are usable price reads.
        for m in markets:
            if m.get("type") != "total" or m.get("period") != 0:
                continue
            if m.get("status") != "open":
                continue
            for p in m.get("prices", []):
                if (p.get("designation") == "over"
                        and abs(float(p.get("points", -1)) - float(line)) < 0.01):
                    return american_to_decimal(p.get("price"))
        return None

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:  # noqa: BLE001
            pass
