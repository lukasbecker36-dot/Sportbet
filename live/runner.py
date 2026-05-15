"""Async orchestrator: Telegram bot + SofaScore monitor + Betfair gate.

One process, one asyncio loop. The Telegram ``Application`` polls Telegram for
commands/callbacks; a separate task per watched event polls SofaScore + Betfair
and pushes alerts back through ``BotHandle``. SDK calls (``tls_requests``,
``betfairlightweight``) are sync — we wrap each in ``asyncio.to_thread`` so
they don't block the loop.

Entry point:

    python -m live.runner

Once running, message the bot from your Telegram chat:

    /start
    /watch 14023928        # e.g. Aston Villa v Liverpool, 2026-05-15
    /status
    /funds
    /kill                  # emergency disable of placement
    /stop                  # stop the active monitor

Only the chat id in ``config.TELEGRAM_CHAT_ID`` is allowed to issue commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from telegram.ext import Application

import config
from live import telegram_bot as tg
from live.betfair_client import (
    BetfairLive, OverGoalsMarket, PriceSnapshot,
    PlacementResult, expected_value,
)
from live.monitor import (
    DEFAULT_MAX_MIN, DEFAULT_MIN_MIN, DEFAULT_THRESHOLD,
    Shot, _candidate_alert, _fmt_score, _match_minute,
    goals_total, parse_shots, xg_rate_window,
)
from live.sofascore import SofaScore

logger = logging.getLogger("sportbet.runner")


class RunnerState:
    """Mutable container shared across the bot + monitor tasks.

    Multi-match aware: ``monitors`` holds one entry per active watch.
    """

    def __init__(self) -> None:
        self.bf: Optional[BetfairLive] = None
        self.bot_handle: Optional[tg.BotHandle] = None
        self.app: Optional[Application] = None
        # event_id -> {task, label, last_tick, strategy}
        self.monitors: dict[int, dict] = {}
        self.daily_staked: float = 0.0
        # Recent fired alerts, newest-first. Reused by /recent.
        self.recent_alerts: deque[dict] = deque(maxlen=25)


STATE = RunnerState()


def _strategy_for(tournament_id: int | None) -> tuple[float, float, int, int, float]:
    """Return (threshold, market_line, min_min, max_min, win_rate) for a league.

    Falls back to the global defaults if the SofaScore tournament id isn't in
    config.LIVE_STRATEGY_BY_LEAGUE.
    """
    by_league = getattr(config, "LIVE_STRATEGY_BY_LEAGUE", {}) or {}
    if tournament_id is not None and tournament_id in by_league:
        return by_league[tournament_id]
    return (
        config.LIVE_XG_THRESHOLD,
        config.LIVE_MARKET_LINE,
        config.LIVE_MIN_MINUTE,
        config.LIVE_MAX_MINUTE,
        config.LIVE_BASE_WIN_RATE,
    )


# --------------------------------------------------------------------------- #
# The per-match monitor task
# --------------------------------------------------------------------------- #
async def _monitor_match(event_id: int) -> None:
    """Poll SofaScore for one match, push gated alerts to Telegram.

    The kickoff card is sent ONCE per task, after the first successful event
    fetch. All transient failures (SofaScore proxy hiccups, Betfair lookup
    flakes) just retry on the next poll — they don't kill the task.
    """
    sofa = SofaScore()
    bf = STATE.bf
    handle = STATE.bot_handle
    assert bf is not None and handle is not None

    market: OverGoalsMarket | None = None
    fired = False
    home = away = "?"
    threshold = market_line = win_rate = 0.0
    min_min = max_min = 0
    kickoff_card_sent = False
    slot = STATE.monitors.setdefault(event_id, {})
    slot["label"] = "?"
    slot["last_tick"] = "connecting…"

    try:
        while True:
            # Fetch event meta — retry forever on transient failures.
            try:
                ev = await asyncio.to_thread(sofa.event, event_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SofaScore event %s fetch failed: %s", event_id, exc)
                await asyncio.sleep(config.LIVE_POLL_SECONDS)
                continue

            home = ev.get("homeTeam", {}).get("name", "?")
            away = ev.get("awayTeam", {}).get("name", "?")
            ko_ts = ev.get("startTimestamp") or 0
            kickoff = (
                datetime.fromtimestamp(ko_ts, tz=timezone.utc) if ko_ts else
                datetime.now(timezone.utc)
            )
            tournament_id = (
                (ev.get("tournament", {}) or {}).get("uniqueTournament", {}) or {}
            ).get("id")
            tournament_name = (ev.get("tournament", {}) or {}).get("name", "?")
            strategy = _strategy_for(tournament_id)
            threshold, market_line, min_min, max_min, win_rate = strategy
            slot["label"] = f"{home} v {away}"
            slot["strategy"] = strategy

            status_type = ev.get("status", {}).get("type")
            status_desc = ev.get("status", {}).get("description") or ""

            # Locate Betfair market once we have meta.
            if market is None:
                try:
                    market = await asyncio.to_thread(
                        bf.find_over_under_market, home, away, kickoff, market_line,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Betfair market lookup failed for %s: %s", event_id, exc)
                    market = None

            # One consolidated kickoff card per task.
            if not kickoff_card_sent:
                market_line_msg = (
                    f"📈 {market.market_name}" if market
                    else f"⚠ Betfair Over {market_line} market not found yet"
                )
                ko_iso = kickoff.isoformat(timespec="minutes") if ko_ts else "?"
                await handle.send_text(
                    f"👀 <b>{home} v {away}</b>  <i>({tournament_name})</i>\n"
                    f"strategy: xG≥<b>{threshold}</b>, over_<b>{market_line}</b>, "
                    f"min <b>{min_min}-{max_min}</b>, win_rate=<b>{win_rate:.0%}</b>\n"
                    f"kickoff {ko_iso}Z  ·  {market_line_msg}",
                )
                kickoff_card_sent = True

            if status_type in {"finished", "postponed", "canceled"}:
                await handle.send_text(
                    f"🏁 <b>{home} v {away}</b> — {status_desc}. "
                    + ("Bet stayed open." if fired else "No signal fired."),
                )
                return

            # Pre-kickoff: don't hit the shotmap endpoint (404s on unstarted
            # matches). Poll loosely until kickoff is within 10 min.
            if status_type != "inprogress":
                seconds_to_ko = max(0, ko_ts - time.time()) if ko_ts else None
                slot["last_tick"] = (
                    f"pre-match ({status_desc})"
                    + (f", ko in {int(seconds_to_ko//60)}m" if seconds_to_ko else "")
                )
                sleep_for = 300 if (seconds_to_ko is None or seconds_to_ko > 600) else 30
                await asyncio.sleep(sleep_for)
                continue

            try:
                raw_shots = await asyncio.to_thread(sofa.shotmap, event_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SofaScore shotmap fetch failed: %s", exc)
                await asyncio.sleep(config.LIVE_POLL_SECONDS)
                continue

            minute = _match_minute(ev)
            shots = parse_shots(raw_shots)
            if minute is None:
                slot["last_tick"] = f"waiting ({status_desc})"
                await asyncio.sleep(config.LIVE_POLL_SECONDS)
                continue

            rate = xg_rate_window(shots, minute)
            gt = goals_total(shots, minute)
            score = _fmt_score(shots, minute)
            slot["last_tick"] = (
                f"{minute}'  {score}  xg15={rate:.2f}  shots={len(shots)}"
            )

            # Re-attempt market lookup silently if needed.
            if market is None and minute >= 1:
                try:
                    market = await asyncio.to_thread(
                        bf.find_over_under_market, home, away, kickoff, market_line,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Betfair market lookup failed: %s", exc)

            if not fired:
                alert_payload = _candidate_alert(
                    shots, minute,
                    threshold, market_line, min_min, max_min,
                )
                if alert_payload and market is not None:
                    price_snap = await asyncio.to_thread(bf.fetch_price, market)
                    price = _best_price(price_snap)
                    if price is None:
                        logger.info(
                            "xG signal at minute %d but no Betfair price (status=%s)",
                            minute, price_snap.status,
                        )
                    else:
                        ev_value = expected_value(price, win_rate)
                        if ev_value >= config.LIVE_MIN_EV:
                            await _push_alert(
                                event_id, market, home, away,
                                minute, rate, score, price, ev_value,
                                market_line=market_line,
                            )
                            fired = True
                        else:
                            logger.info(
                                "xG signal at minute %d but live EV %.3f < %.2f floor "
                                "(LTP %.2f); not alerting.",
                                minute, ev_value, config.LIVE_MIN_EV, price,
                            )

            await asyncio.sleep(config.LIVE_POLL_SECONDS)

    except asyncio.CancelledError:
        await handle.send_text(f"⏹ Stopped watching {home} v {away}.")
        raise
    finally:
        sofa.close()
        STATE.monitors.pop(event_id, None)


def _best_price(snap: PriceSnapshot) -> float | None:
    """Prefer LTP (representative), fall back to best back price."""
    if snap.status != "OPEN":
        return None
    if snap.last_price_traded:
        return float(snap.last_price_traded)
    if snap.best_back:
        return float(snap.best_back)
    return None


async def _push_alert(
    event_id: int,
    market: OverGoalsMarket,
    home: str, away: str,
    minute: int, rate: float, score: str,
    price: float, ev_value: float,
    *,
    market_line: float | None = None,
) -> None:
    handle = STATE.bot_handle
    assert handle is not None
    mode = config.LIVE_AUTO_PLACE_MODE
    if mode not in {"manual", "cancel_window", "full_auto"}:
        logger.warning("Unknown LIVE_AUTO_PLACE_MODE=%r; falling back to manual", mode)
        mode = "manual"

    if mode == "manual":
        expires_at = time.time() + config.LIVE_CONFIRM_TIMEOUT_S
    elif mode == "cancel_window":
        expires_at = time.time() + config.LIVE_AUTO_CANCEL_WINDOW_S
    else:
        expires_at = time.time()  # full_auto — placement happens here

    effective_line = market_line if market_line is not None else config.LIVE_MARKET_LINE
    alert = tg.PendingAlert(
        alert_id=tg.new_alert_id(),
        event_id=event_id,
        market_id=market.market_id,
        home=home, away=away,
        minute=minute,
        market_line=effective_line,
        price=price, xg_rate=rate, score=score, ev=ev_value,
        expires_at=expires_at,
        chat_id=int(config.TELEGRAM_CHAT_ID),
        mode=mode,
        placement_callback=lambda a: _place_bet(a, market),
    )

    STATE.recent_alerts.appendleft({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_id": event_id,
        "home": home, "away": away,
        "minute": minute, "score": score,
        "xg_rate": rate,
        "market": f"over_{effective_line}",
        "price": price, "ev": ev_value,
        "mode": mode,
    })

    if mode == "full_auto":
        # Place first, then send a result-only message.
        try:
            result = await _place_bet(alert, market)
        except Exception as e:  # noqa: BLE001
            await handle.send_text(
                f"⚡ SIGNAL fired but placement crashed: {e}",
                html=False,
            )
            return
        alert.resolved = True
        alert.decision = "auto_placed"
        await handle.send_text(
            handle._format_alert(alert) + tg._result_suffix(result, price),
        )
        return

    await handle.send_alert(alert)


async def _place_bet(
    alert: tg.PendingAlert,
    market: OverGoalsMarket,
) -> dict:
    bf = STATE.bf
    assert bf is not None
    stake = config.LIVE_STAKE_GBP
    # Daily-cap guard
    if STATE.daily_staked + stake > config.LIVE_DAILY_STAKE_CAP_GBP:
        return {"status": "FAILURE", "error":
                f"daily stake cap {config.LIVE_DAILY_STAKE_CAP_GBP} would be exceeded"}
    # Re-pull a fresh price so we don't place at a stale LTP
    snap = await asyncio.to_thread(bf.fetch_price, market)
    price = _best_price(snap) or alert.price
    # Re-check EV with the fresh price; abort if it's collapsed
    if expected_value(price, config.LIVE_BASE_WIN_RATE) < config.LIVE_MIN_EV / 2:
        return {"status": "FAILURE",
                "error": f"price moved (now {price:.2f}); EV no longer ≥ floor"}
    result: PlacementResult = await asyncio.to_thread(
        bf.place_back, market, stake, price,
        customer_ref=f"sb-{alert.alert_id}",
    )
    if result.status == "SUCCESS":
        STATE.daily_staked += stake
    return {
        "status": result.status,
        "bet_id": result.bet_id,
        "matched_size": result.matched_size,
        "avg_price_matched": result.avg_price_matched,
        "error": result.error,
    }


# --------------------------------------------------------------------------- #
# Telegram command callbacks
# --------------------------------------------------------------------------- #
async def _on_watch(event_id: int, *, silent: bool = False) -> None:
    """Spawn a monitor for an event. ``silent=True`` skips the 'already
    watching' chat reply (used by auto-discover to keep noise low)."""
    existing = STATE.monitors.get(event_id, {}).get("task")
    if existing and not existing.done():
        if not silent and STATE.bot_handle:
            await STATE.bot_handle.send_text(
                f"Already watching event {event_id}.",
            )
        return
    task = asyncio.create_task(_monitor_match(event_id))
    STATE.monitors[event_id] = {"task": task, "label": "?", "last_tick": None, "strategy": None}


async def _auto_discover_loop() -> None:
    """Background task: every N seconds, find live + soon-starting matches in
    LIVE_STRATEGY_BY_LEAGUE and watch any not already in STATE.monitors."""
    lookahead_s = config.LIVE_AUTO_DISCOVER_LOOKAHEAD_H * 3600
    target_leagues = set(getattr(config, "LIVE_STRATEGY_BY_LEAGUE", {}).keys())
    if not target_leagues:
        logger.warning("auto-discover: no leagues in LIVE_STRATEGY_BY_LEAGUE; loop will idle")
    handle = STATE.bot_handle
    seen_announce: set[int] = set()
    while True:
        try:
            sofa = SofaScore()
            try:
                today_evs = await asyncio.to_thread(sofa.scheduled_events, date.today())
                tomorrow_evs = await asyncio.to_thread(
                    sofa.scheduled_events, date.today() + timedelta(days=1),
                )
                live_evs = await asyncio.to_thread(sofa.live_events)
            finally:
                sofa.close()

            now = time.time()
            relevant: dict[int, dict] = {}
            for ev in (today_evs + tomorrow_evs + live_evs):
                tid = (
                    (ev.get("tournament", {}) or {}).get("uniqueTournament", {}) or {}
                ).get("id")
                if tid not in target_leagues:
                    continue
                status = (ev.get("status", {}) or {}).get("type")
                if status in {"finished", "postponed", "canceled"}:
                    continue
                eid = ev["id"]
                if status == "inprogress":
                    relevant[eid] = ev
                    continue
                ko = ev.get("startTimestamp") or 0
                if ko and 0 < ko - now <= lookahead_s:
                    relevant[eid] = ev

            for eid, ev in relevant.items():
                if eid in STATE.monitors:
                    continue
                home = ev.get("homeTeam", {}).get("name", "?")
                away = ev.get("awayTeam", {}).get("name", "?")
                ko_iso = ""
                if ev.get("startTimestamp"):
                    ko_iso = datetime.fromtimestamp(
                        ev["startTimestamp"], tz=timezone.utc,
                    ).isoformat(timespec="minutes")
                logger.info(
                    "auto-discover: starting monitor for %s v %s (event %s, ko=%s)",
                    home, away, eid, ko_iso,
                )
                if handle and eid not in seen_announce:
                    await handle.send_text(
                        f"🤖 Auto-discovered <b>{home} v {away}</b> "
                        f"(event {eid}, ko {ko_iso}Z) — starting monitor.",
                    )
                    seen_announce.add(eid)
                await _on_watch(eid, silent=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-discover loop failed: %s", exc)
        await asyncio.sleep(config.LIVE_AUTO_DISCOVER_INTERVAL_S)


async def _on_stop(event_id: int | None = None) -> None:
    """Cancel one monitor (if event_id given) or all of them."""
    targets = (
        [(event_id, STATE.monitors[event_id])]
        if event_id is not None and event_id in STATE.monitors
        else list(STATE.monitors.items())
    )
    for _eid, slot in targets:
        task = slot.get("task")
        if task and not task.done():
            task.cancel()


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 1. Betfair login
    bf = BetfairLive()
    await asyncio.to_thread(bf.login)
    STATE.bf = bf

    # 2. Telegram app
    app, handle = await tg.build_application(
        on_watch=_on_watch,
        on_stop=_on_stop,
    )
    STATE.app = app
    STATE.bot_handle = handle
    app.bot_data["betfair"] = bf
    app.bot_data["monitor_state"] = {"active": None}
    app.bot_data["recent_alerts"] = STATE.recent_alerts

    # 3. Wire status command to STATE
    async def _refresh_status_loop():
        while True:
            app.bot_data["monitor_state"]["active"] = [
                {
                    "event_id": eid,
                    "label": slot.get("label", "?"),
                    "last_tick": slot.get("last_tick"),
                }
                for eid, slot in STATE.monitors.items()
            ]
            await asyncio.sleep(5)

    asyncio.create_task(_refresh_status_loop())

    if getattr(config, "LIVE_AUTO_DISCOVER", False):
        asyncio.create_task(_auto_discover_loop())
        logger.info(
            "Auto-discover ON: scanning %s leagues every %ds (lookahead %dh)",
            len(getattr(config, "LIVE_STRATEGY_BY_LEAGUE", {})),
            config.LIVE_AUTO_DISCOVER_INTERVAL_S,
            config.LIVE_AUTO_DISCOVER_LOOKAHEAD_H,
        )

    # 4. Start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Telegram polling started; awaiting commands…")
    auto = "ON" if getattr(config, "LIVE_AUTO_DISCOVER", False) else "OFF"
    await handle.send_text(
        f"🟢 Live monitor online. Auto-discover: <b>{auto}</b>\n"
        f"leagues: {len(getattr(config, 'LIVE_STRATEGY_BY_LEAGUE', {}))} "
        f"(stake £{config.LIVE_STAKE_GBP:.0f}, EV floor {config.LIVE_MIN_EV:+.2f}, "
        f"daily cap £{config.LIVE_DAILY_STAKE_CAP_GBP:.0f})",
    )

    # 5. Block until cancelled (Ctrl-C)
    stop = asyncio.Event()

    def _on_signal(*_):
        stop.set()

    if hasattr(signal, "SIGTERM"):
        try:
            asyncio.get_event_loop().add_signal_handler(signal.SIGTERM, _on_signal)
        except NotImplementedError:
            pass  # Windows
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for slot in list(STATE.monitors.values()):
            task = slot.get("task")
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await asyncio.to_thread(bf.logout)
        logger.info("Shutdown complete.")


def main() -> int:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
