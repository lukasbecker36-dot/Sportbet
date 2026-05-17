"""Thin wrapper around betfairlightweight for the live monitor.

Scope:
  * Interactive login (email/password + app key — no SSL cert required).
  * Find the in-play over/under-N.5 goals market for a given (home, away, date).
  * Read last traded price + market depth for the "Over" runner.
  * Place a single back bet with safety caps.

Everything stays synchronous and short — async wrapping happens in the monitor
via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import time as _time

import betfairlightweight as bflw
from betfairlightweight import filters
from betfairlightweight.exceptions import (
    APIError, BetfairError, StatusCodeError,
)

import config

logger = logging.getLogger("sportbet.betfair_live")

_OVER_RE = re.compile(r"^Over\s+(\d+\.\d)\s+Goals$", re.IGNORECASE)
_SOCCER_EVENT_TYPE_ID = "1"

_KEY_MARKET_TYPE_BY_LINE = {
    0.5: "OVER_UNDER_05",
    1.5: "OVER_UNDER_15",
    2.5: "OVER_UNDER_25",
    3.5: "OVER_UNDER_35",
}


@dataclass(frozen=True)
class OverGoalsMarket:
    market_id: str
    market_name: str
    over_selection_id: int
    home: str
    away: str
    line: float


@dataclass(frozen=True)
class PriceSnapshot:
    last_price_traded: float | None
    best_back: float | None
    best_back_size: float | None
    inplay: bool
    status: str  # "OPEN" / "SUSPENDED" / "CLOSED"


@dataclass(frozen=True)
class PlacementResult:
    status: str            # "SUCCESS" / "FAILURE"
    bet_id: str | None
    matched_size: float
    avg_price_matched: float | None
    error: str | None


def net_odds(odds: float) -> float:
    """LTP after Betfair commission."""
    return (odds - 1.0) * (1.0 - config.BETFAIR_COMMISSION) + 1.0


def adjusted_odds(odds: float) -> float:
    """net_odds with a conservative in-play haircut on the edge."""
    n = net_odds(odds)
    return 1.0 + (n - 1.0) * config.ODDS_HAIRCUT


def expected_value(odds: float, win_rate: float) -> float:
    """EV per 1u stake at the given decimal odds and win probability."""
    return win_rate * (adjusted_odds(odds) - 1.0) - (1.0 - win_rate)


class BetfairLive:
    """Minimal client. Reuse one instance for a session — sessions expire ~4h."""

    def __init__(self) -> None:
        creds = {
            "BETFAIR_USERNAME": (config.BETFAIR_USERNAME or "").strip(),
            "BETFAIR_PASSWORD": (config.BETFAIR_PASSWORD or "").strip(),
            "BETFAIR_APP_KEY": (config.BETFAIR_APP_KEY or "").strip(),
        }
        missing = [k for k, v in creds.items() if not v]
        if missing:
            raise RuntimeError(
                "Betfair credentials missing in config.py: " + ", ".join(missing)
                + ".\nDid you forget to save the file?"
            )
        # Cert-based login is required when running from a datacenter IP
        # (Betfair 403s the interactive login endpoint from cloud ranges).
        # If BETFAIR_CERTS_PATH is set to a directory containing
        # client-2048.crt + client-2048.key, we use cert login; otherwise we
        # fall back to interactive (fine for residential / dev machines).
        certs_path = (getattr(config, "BETFAIR_CERTS_PATH", "") or "").strip()
        self._certs_path: str | None = certs_path or None
        self._uses_cert = bool(self._certs_path)
        self._client = bflw.APIClient(
            username=creds["BETFAIR_USERNAME"],
            password=creds["BETFAIR_PASSWORD"],
            app_key=creds["BETFAIR_APP_KEY"],
            certs=self._certs_path,
        )
        self._logged_in = False
        self._login_at: float = 0.0
        # Betfair sessions expire after ~4h idle / 8h max. Refresh well before
        # either limit fires.
        self._session_max_age_s = 3 * 3600

    # ------------------------------------------------------------------ session
    def login(self) -> None:
        if self._logged_in and self._login_age_s() < self._session_max_age_s:
            return
        if self._uses_cert:
            self._client.login()
            mode = "cert"
        else:
            self._client.login_interactive()
            mode = "interactive"
        self._logged_in = True
        self._login_at = _time.time()
        logger.info("Betfair: logged in as %s (%s mode)", config.BETFAIR_USERNAME, mode)

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            self._client.logout()
        except BetfairError:
            pass
        self._logged_in = False

    def _login_age_s(self) -> float:
        return _time.time() - self._login_at if self._login_at else float("inf")

    def _ensure_session(self) -> None:
        """Refresh the session if it's stale or never started."""
        if not self._logged_in or self._login_age_s() >= self._session_max_age_s:
            self._logged_in = False  # force re-login
            self.login()

    def _retry_on_session(self, fn, *args, **kwargs):
        """Run ``fn``; on session-related error (APIError text or HTTP 401/403),
        re-login once and retry."""
        self._ensure_session()
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            msg = str(e).upper()
            if any(s in msg for s in (
                "INVALID_SESSION_INFORMATION", "NO_SESSION", "INVALID_SESSION",
                "SESSION_EXPIRED",
            )):
                logger.warning("Betfair session expired (%s); re-logging in.", e)
                self._logged_in = False
                self.login()
                return fn(*args, **kwargs)
            raise
        except StatusCodeError as e:
            # 401/403 from a betting API call almost always means the session
            # token went stale (Betfair returns 401/403 rather than a JSON
            # error body in some cases). Try one re-login and retry.
            code = getattr(e, "status_code", None) or str(e)
            if "401" in str(code) or "403" in str(code):
                logger.warning(
                    "Betfair HTTP %s on betting call; re-logging in and retrying once.",
                    code,
                )
                self._logged_in = False
                self.login()
                return fn(*args, **kwargs)
            raise

    def account_funds(self) -> dict:
        return self._retry_on_session(
            lambda: self._client.account.get_account_funds()._data,
        )

    # ------------------------------------------------------------------ market discovery
    def find_over_under_market(
        self,
        home: str,
        away: str,
        match_date: datetime,
        line: float = 2.5,
        window_hours: int = 12,
    ) -> OverGoalsMarket | None:
        """Locate the Over/Under-N.5 Goals market for a single fixture.

        ``match_date`` is the scheduled kickoff (UTC). We search markets opened
        within ``window_hours`` either side; that's generous enough for any
        SofaScore/Betfair kickoff-time drift.
        """
        line_key = _KEY_MARKET_TYPE_BY_LINE.get(line)
        if line_key is None:
            raise ValueError(f"Unsupported line: {line}")

        from datetime import timedelta
        market_filter = filters.market_filter(
            event_type_ids=[_SOCCER_EVENT_TYPE_ID],
            market_type_codes=[line_key],
            market_start_time={
                "from": (match_date - timedelta(hours=window_hours)).isoformat(),
                "to": (match_date + timedelta(hours=window_hours)).isoformat(),
            },
        )
        catalogue = self._retry_on_session(
            self._client.betting.list_market_catalogue,
            filter=market_filter,
            market_projection=["RUNNER_METADATA", "EVENT", "MARKET_START_TIME"],
            max_results=200,
        )

        wanted_home = _normalise(_sofascore_to_betfair(home))
        wanted_away = _normalise(_sofascore_to_betfair(away))
        # Also try fully-normalised SofaScore names (without translation) as a
        # fallback — covers teams where SofaScore == Betfair after accent strip.
        wanted_home_raw = _normalise(home)
        wanted_away_raw = _normalise(away)
        for m in catalogue:
            ev = m.event
            ev_name = (ev.name or "")
            parts = re.split(r"\s+v\s+", ev_name, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) != 2:
                continue
            h, a = _normalise(parts[0]), _normalise(parts[1])
            if (h, a) not in {
                (wanted_home, wanted_away),
                (wanted_home_raw, wanted_away_raw),
            }:
                continue
            # Find the "Over N.5 Goals" runner
            over_sel_id = None
            for runner in m.runners or []:
                rn = (runner.runner_name or "")
                mr = _OVER_RE.match(rn)
                if mr and float(mr.group(1)) == line:
                    over_sel_id = runner.selection_id
                    break
            if over_sel_id is None:
                continue
            return OverGoalsMarket(
                market_id=m.market_id,
                market_name=m.market_name,
                over_selection_id=over_sel_id,
                home=parts[0].strip(),
                away=parts[1].strip(),
                line=line,
            )
        return None

    # ------------------------------------------------------------------ prices
    def fetch_price(self, market: OverGoalsMarket) -> PriceSnapshot:
        books = self._retry_on_session(
            self._client.betting.list_market_book,
            market_ids=[market.market_id],
            price_projection=filters.price_projection(
                price_data=["EX_BEST_OFFERS", "EX_TRADED"],
            ),
        )
        if not books:
            return PriceSnapshot(None, None, None, False, "UNKNOWN")
        book = books[0]
        # Find the Over runner
        runner = next(
            (r for r in book.runners if r.selection_id == market.over_selection_id),
            None,
        )
        if runner is None:
            return PriceSnapshot(None, None, None, False, book.status or "UNKNOWN")
        best_back = None
        best_back_size = None
        ex = getattr(runner, "ex", None)
        if ex and getattr(ex, "available_to_back", None):
            top = ex.available_to_back[0]
            best_back = top.price
            best_back_size = top.size
        return PriceSnapshot(
            last_price_traded=runner.last_price_traded,
            best_back=best_back,
            best_back_size=best_back_size,
            inplay=bool(book.inplay),
            status=book.status or "UNKNOWN",
        )

    # ------------------------------------------------------------------ placement
    def place_back(
        self,
        market: OverGoalsMarket,
        stake: float,
        price: float,
        *,
        persistence: str = "LAPSE",
        customer_ref: str | None = None,
    ) -> PlacementResult:
        """Place a single back bet on the Over runner at ``price`` for ``stake``."""
        if stake <= 0:
            return PlacementResult("FAILURE", None, 0.0, None, "non-positive stake")
        if stake > config.LIVE_MAX_STAKE_GBP:
            return PlacementResult(
                "FAILURE", None, 0.0, None,
                f"stake {stake} exceeds LIVE_MAX_STAKE_GBP {config.LIVE_MAX_STAKE_GBP}",
            )
        if os.path.exists(config.LIVE_KILL_SWITCH_FILE):
            return PlacementResult(
                "FAILURE", None, 0.0, None,
                f"kill switch present at {config.LIVE_KILL_SWITCH_FILE}",
            )

        instruction = filters.place_instruction(
            order_type="LIMIT",
            selection_id=market.over_selection_id,
            side="BACK",
            limit_order=filters.limit_order(
                size=round(stake, 2),
                price=_round_to_tick(price),
                persistence_type=persistence,
            ),
        )
        try:
            resp = self._retry_on_session(
                self._client.betting.place_orders,
                market_id=market.market_id,
                instructions=[instruction],
                customer_ref=customer_ref,
            )
        except APIError as e:
            return PlacementResult("FAILURE", None, 0.0, None, f"APIError: {e}")

        report = (resp.place_instruction_reports or [None])[0]
        if report is None:
            return PlacementResult("FAILURE", None, 0.0, None, "no report")
        if report.status != "SUCCESS":
            return PlacementResult(
                "FAILURE", None, 0.0, None,
                f"{report.error_code or report.status}",
            )
        return PlacementResult(
            status="SUCCESS",
            bet_id=str(report.bet_id),
            matched_size=float(report.size_matched or 0.0),
            avg_price_matched=float(report.average_price_matched) if report.average_price_matched else None,
            error=None,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# SofaScore returns club names in their official form ("FC Bayern München",
# "Bayer 04 Leverkusen", "1. FC Heidenheim 1846"…). Betfair uses shorter,
# more colloquial labels. The normaliser below strips accents and lowercases
# but doesn't bridge those vocabulary differences, so each find-market call
# would silently 0-match on Bundesliga/La Liga/Serie A/etc.
#
# Map SofaScore canonical → Betfair canonical. Apply BEFORE _normalise.
SOFASCORE_TO_BETFAIR: dict[str, str] = {
    # PL
    "Nottingham Forest": "Nottm Forest",
    # Bundesliga
    "FC Bayern München": "Bayern Munich",
    "Bayer 04 Leverkusen": "Leverkusen",
    "Borussia M'gladbach": "Mönchengladbach",
    "Borussia Mönchengladbach": "Mönchengladbach",
    "1. FC Heidenheim 1846": "Heidenheim",
    "1. FC Heidenheim": "Heidenheim",
    "1. FC Köln": "FC Köln",
    "1. FC Union Berlin": "Union Berlin",
    "1. FSV Mainz 05": "Mainz",
    "FC St. Pauli": "St Pauli",
    "SV Werder Bremen": "Werder Bremen",
    "VfL Wolfsburg": "Wolfsburg",
    "VfB Stuttgart": "Stuttgart",
    "FC Augsburg": "Augsburg",
    "TSG Hoffenheim": "Hoffenheim",
    "Eintracht Frankfurt": "E Frankfurt",
    "SC Freiburg": "Freiburg",
    "RB Leipzig": "RB Leipzig",
    "Hamburger SV": "Hamburg",
    "Holstein Kiel": "Kiel",
    # La Liga
    "Atlético Madrid": "Atletico Madrid",
    "Athletic Club": "Athletic Bilbao",
    "Real Betis": "Betis",
    "Real Sociedad": "Sociedad",
    "Rayo Vallecano": "R Vallecano",
    "Real Madrid": "R Madrid",
    "Cádiz": "Cadiz",
    "Almería": "Almeria",
    "Alavés": "Alaves",
    "Deportivo Alavés": "Alaves",
    "RCD Mallorca": "Mallorca",
    "UD Las Palmas": "Las Palmas",
    "Celta de Vigo": "Celta Vigo",
    # Serie A
    "Internazionale": "Inter Milan",
    "Inter": "Inter Milan",
    "AS Roma": "Roma",
    "Hellas Verona": "Verona",
    "Como 1907": "Como",
    # Ligue 1
    "Paris Saint-Germain": "Paris SG",
    "Olympique Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon",
    "Stade Rennais": "Rennes",
    "OGC Nice": "Nice",
    "Stade Brestois 29": "Brest",
    "RC Strasbourg Alsace": "Strasbourg",
    "RC Lens": "Lens",
    "FC Nantes": "Nantes",
    "LOSC Lille": "Lille",
    "Stade de Reims": "Reims",
    "FC Metz": "Metz",
    "Le Havre AC": "Le Havre",
    "AJ Auxerre": "Auxerre",
    "Angers SCO": "Angers",
    "AS Saint-Étienne": "St Etienne",
    "Saint-Étienne": "St Etienne",
}


def _sofascore_to_betfair(name: str) -> str:
    """Translate a SofaScore club name to its Betfair equivalent before
    normalisation. Pass-through for unmapped names."""
    return SOFASCORE_TO_BETFAIR.get((name or "").strip(), name)


def _normalise(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


# Betfair price ticks. Snap to nearest valid tick downward (more conservative
# for a back bet — never overpay vs the requested price).
_TICKS: list[tuple[float, float, float]] = [
    (1.01, 2.00, 0.01),
    (2.00, 3.00, 0.02),
    (3.00, 4.00, 0.05),
    (4.00, 6.00, 0.10),
    (6.00, 10.00, 0.20),
    (10.00, 20.00, 0.50),
    (20.00, 30.00, 1.00),
    (30.00, 50.00, 2.00),
    (50.00, 100.00, 5.00),
    (100.00, 1000.01, 10.00),
]


def _round_to_tick(price: float) -> float:
    for lo, hi, step in _TICKS:
        if lo <= price < hi:
            n = int((price - lo) / step)
            return round(lo + n * step, 2)
    return round(price, 2)
