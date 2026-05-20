"""Async Telegram bot — sends alerts and processes Place/Skip button taps.

Auth model: the bot only listens to a single chat_id (``config.TELEGRAM_CHAT_ID``).
Any message from another chat is silently ignored. There is no admin UI beyond
that — a leaked bot token + chat-id pair is the full attack surface, so don't
share either.

This module exports a small ``BotHandle`` that the monitor uses to push alerts.
The bot itself owns the underlying Telegram ``Application`` and its lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

import config

logger = logging.getLogger("sportbet.telegram")


@dataclass
class PendingAlert:
    alert_id: str
    event_id: int
    market_id: str
    home: str
    away: str
    minute: int
    market_line: float
    price: float
    xg_rate: float
    score: str
    ev: float
    expires_at: float
    chat_id: int
    mode: str = "manual"   # "manual" | "cancel_window" | "full_auto"
    message_id: int | None = None
    resolved: bool = False
    decision: str | None = None  # "place" | "skip" | "expired" | "auto_placed" | "cancelled"
    placement_callback: Callable[["PendingAlert"], Awaitable[dict]] | None = field(
        default=None, repr=False,
    )


class BotHandle:
    """API surface used by the monitor to push alerts and bot-replies."""

    def __init__(self, app: Application) -> None:
        self._app = app
        self._pending: dict[str, PendingAlert] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ public
    async def send_text(self, text: str, *, html: bool = True) -> None:
        await self._app.bot.send_message(
            chat_id=int(config.TELEGRAM_CHAT_ID),
            text=text,
            parse_mode=ParseMode.HTML if html else None,
        )

    async def send_signal(self, text: str, *, html: bool = True) -> None:
        """Dual-send: main chat + signals chat (if configured + distinct).

        Use this for signal-fire messages the user wants iPhone push
        notifications for — they can mute the main chat and only allow
        notifications on the signals chat.
        """
        # Send to main chat first (existing behaviour). A failure here is
        # treated like send_text would — we still try the signals chat below.
        try:
            await self._app.bot.send_message(
                chat_id=int(config.TELEGRAM_CHAT_ID),
                text=text,
                parse_mode=ParseMode.HTML if html else None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("send_signal main-chat send failed: %s", e)

        signals_id = (getattr(config, "TELEGRAM_SIGNALS_CHAT_ID", "") or "").strip()
        if not signals_id or signals_id == str(config.TELEGRAM_CHAT_ID):
            return
        try:
            await self._app.bot.send_message(
                chat_id=int(signals_id),
                text=text,
                parse_mode=ParseMode.HTML if html else None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("send_signal signals-chat send failed: %s", e)

    async def send_alert(
        self,
        alert: PendingAlert,
    ) -> None:
        """Post an alert with mode-appropriate keyboard; track for callbacks."""
        async with self._lock:
            self._pending[alert.alert_id] = alert

        if alert.mode == "manual":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"✅ Place £{config.LIVE_STAKE_GBP:.0f}",
                    callback_data=f"confirm:{alert.alert_id}",
                ),
                InlineKeyboardButton(
                    "✖ Skip", callback_data=f"skip:{alert.alert_id}",
                ),
            ]])
        elif alert.mode == "cancel_window":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✖ CANCEL", callback_data=f"cancel:{alert.alert_id}",
                ),
            ]])
        else:  # full_auto — no buttons, placement happens before send
            keyboard = None

        text = self._format_alert(alert)
        msg = await self._app.bot.send_message(
            chat_id=int(config.TELEGRAM_CHAT_ID),
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        alert.message_id = msg.message_id

        # Mirror a no-button copy to the dedicated signals chat (if any).
        # Keeps the interactive buttons in the main chat, sends a simpler
        # push-friendly notification to the iPhone via the signals chat.
        signals_id = (getattr(config, "TELEGRAM_SIGNALS_CHAT_ID", "") or "").strip()
        if signals_id and signals_id != str(config.TELEGRAM_CHAT_ID):
            try:
                await self._app.bot.send_message(
                    chat_id=int(signals_id),
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("signals-chat mirror failed: %s", e)

        if alert.mode in ("manual", "cancel_window"):
            asyncio.create_task(self._expire_after(alert))

    # ------------------------------------------------------------------ internal
    def _format_alert(self, a: PendingAlert) -> str:
        if a.mode == "manual":
            tail = f"stake: £{config.LIVE_STAKE_GBP:.0f}  (expires in {config.LIVE_CONFIRM_TIMEOUT_S}s)"
        elif a.mode == "cancel_window":
            tail = (
                f"stake: £{config.LIVE_STAKE_GBP:.0f}  "
                f"auto-placing in <b>{config.LIVE_AUTO_CANCEL_WINDOW_S}s</b>"
                f"  (tap CANCEL to abort)"
            )
        else:  # full_auto
            tail = f"stake: £{config.LIVE_STAKE_GBP:.0f}  <i>placing automatically…</i>"
        return (
            f"⚡ <b>SIGNAL — {a.home} v {a.away}</b>\n"
            f"min <b>{a.minute}'</b>  score <b>{a.score}</b>\n"
            f"xg_rate_15m = <b>{a.xg_rate:.2f}</b>  (≥ {config.LIVE_XG_THRESHOLD})\n"
            f"market: <b>Over {a.market_line}</b>\n"
            f"LTP: <b>{a.price:.2f}</b>  EV: <b>{a.ev:+.3f}</b>\n"
            + tail
        )

    async def _expire_after(self, alert: PendingAlert) -> None:
        """Manual mode: mark as expired. Cancel-window mode: auto-place."""
        delay = max(0.0, alert.expires_at - time.time())
        await asyncio.sleep(delay)
        async with self._lock:
            if alert.resolved:
                return
            alert.resolved = True
            alert.decision = "expired" if alert.mode == "manual" else "auto_placed"

        if alert.mode == "manual":
            try:
                await self._app.bot.edit_message_text(
                    chat_id=alert.chat_id,
                    message_id=alert.message_id,
                    text=self._format_alert(alert) + "\n\n⌛ <i>expired (no response)</i>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("expire-edit failed: %s", e)
            return

        # cancel_window: user didn't cancel → place the bet
        if alert.placement_callback is None:
            logger.error("cancel_window expired but no placement_callback set")
            return
        try:
            result = await alert.placement_callback(alert)
        except Exception as e:  # noqa: BLE001
            await self._app.bot.edit_message_text(
                chat_id=alert.chat_id,
                message_id=alert.message_id,
                text=self._format_alert(alert) + f"\n\n❌ <i>auto-place error: {e}</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        suffix = _result_suffix(result, alert.price)
        await self._app.bot.edit_message_text(
            chat_id=alert.chat_id,
            message_id=alert.message_id,
            text=self._format_alert(alert) + suffix,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _is_authorised(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    return str(chat.id) == str(config.TELEGRAM_CHAT_ID)


_HELP_TEXT = (
    "<b>Sportbet live monitor — commands</b>\n"
    "/help — show this list\n"
    "/status — list matches currently being watched\n"
    "/pnl — paper P&L if you'd flat-staked every signal\n"
    "/recent — last 10 fired signals (✅ won / ❌ lost / ⏳ pending / ⚠ blocked)\n"
    "/xg <code>&lt;event_id&gt;</code> — current xG rate + score for a live match\n"
    "/debug <code>&lt;event_id&gt;</code> — show how Betfair names this fixture (for blocked-signal diagnosis)\n"
    "/watch <code>&lt;event_id&gt;</code> — manually add a match (auto-discovery covers Big 5)\n"
    "/stop <code>[event_id]</code> — stop one monitor (or all if no id)\n"
    "/funds — show Betfair balance (needs Account API perm on app key)\n"
    "/kill — emergency: disable all bet placement (alerts still fire)\n"
    "\n"
    "Auto-discovery is on — PL / La Liga / Serie A / Bundesliga / Ligue 1 "
    "matches get watched automatically within 3h of kickoff."
)


async def _cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.HTML)


async def _cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.HTML)


def _outcome_marker(a: dict) -> str:
    if a.get("mode") == "blocked":
        return "⚠"
    if not a.get("settled"):
        return "⏳"  # pending
    return "✅" if a.get("won") else "❌"


async def _cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    alerts = list(ctx.application.bot_data.get("recent_alerts") or [])
    if not alerts:
        await update.message.reply_text("No signals have fired yet.")
        return
    lines = ["<b>Recent signals</b> (newest first):"]
    for a in alerts[:10]:
        ts = a["ts"].replace("T", " ").replace("+00:00", "Z")
        marker = _outcome_marker(a)
        is_blocked = a.get("mode") == "blocked"
        tail = ""
        if a.get("settled"):
            ft = a.get("final_total")
            pp = a.get("paper_profit_gbp")
            tail = f"  FT total={ft}  paper £{pp:+.2f}"
        elif is_blocked:
            tail = f"  blocked: {a.get('blocked_reason', '?')}"
        price_str = (
            f"@<b>{a['price']:.2f}</b>" if a.get("price") is not None else ""
        )
        ev_str = (
            f"  EV <b>{a['ev']:+.2f}</b>" if a.get("ev") is not None else ""
        )
        lines.append(
            f"{marker} {ts}  {a['home']} v {a['away']}\n"
            f"   min {a['minute']}'  score {a['score']}  "
            f"xG15 <b>{a['xg_rate']:.2f}</b>  "
            f"{a['market']}{price_str}{ev_str}{tail}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect Betfair's view of a fixture: what eventName + over markets exist."""
    if not _is_authorised(update):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /debug <event_id>")
        return
    event_id = int(parts[1])

    import asyncio
    from datetime import datetime, timezone, timedelta
    from live.sofascore import SofaScore

    bf = ctx.application.bot_data.get("betfair")
    if bf is None:
        await update.message.reply_text("Betfair client not initialised.")
        return

    # Pull home/away/kickoff from SofaScore so we can search Betfair correctly.
    try:
        sofa = SofaScore()
        try:
            ev = await asyncio.to_thread(sofa.event, event_id)
        finally:
            sofa.close()
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"SofaScore fetch error: {e}")
        return
    home = ev.get("homeTeam", {}).get("name", "?")
    away = ev.get("awayTeam", {}).get("name", "?")
    ko_ts = ev.get("startTimestamp")
    kickoff = (
        datetime.fromtimestamp(ko_ts, tz=timezone.utc) if ko_ts else
        datetime.now(timezone.utc)
    )

    # Query Betfair catalogue for over-goals markets one market_type at a
    # time (some Betfair app key configurations 403 on multi-type queries).
    from betfairlightweight import filters
    catalogue = []
    failures: list[str] = []
    for mtype in (
        "OVER_UNDER_05", "OVER_UNDER_15", "OVER_UNDER_25", "OVER_UNDER_35",
    ):
        try:
            part = await asyncio.to_thread(
                bf._retry_on_session,
                bf._client.betting.list_market_catalogue,
                filter=filters.market_filter(
                    event_type_ids=["1"],
                    market_type_codes=[mtype],
                    market_start_time={
                        "from": (kickoff - timedelta(hours=12)).isoformat(),
                        "to": (kickoff + timedelta(hours=12)).isoformat(),
                    },
                ),
                market_projection=["EVENT", "MARKET_START_TIME"],
                max_results=100,
            )
            catalogue.extend(part)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{mtype}: {e}")

    # Filter to events whose name contains either team's surname-ish token.
    def _bag(s: str) -> set[str]:
        return {t.lower() for t in s.split() if len(t) >= 3}
    target_bag = _bag(home) | _bag(away)
    relevant = []
    for m in catalogue:
        ev_name = (m.event.name or "")
        if any(t in ev_name.lower() for t in target_bag):
            relevant.append((ev_name, m.market_name, m.market_id))

    lines = [
        f"<b>SofaScore</b>: {home} v {away}  (ko {kickoff.isoformat(timespec='minutes')}Z)",
        f"<b>Betfair catalogue</b> ({len(catalogue)} markets scanned, "
        f"{len(relevant)} look related):",
    ]
    if not relevant:
        lines.append("  (no matches — Betfair may use very different team names)")
    for ev_name, market_name, mid in relevant[:20]:
        lines.append(f"  • <b>{ev_name}</b>  ·  {market_name}  ·  id {mid}")
    if failures:
        lines.append("\n<b>Errors</b>:")
        for f_ in failures:
            lines.append(f"  • {f_}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    from live import paper_log
    s = paper_log.stats()
    alerts = list(ctx.application.bot_data.get("recent_alerts") or [])
    pending = [a for a in alerts if not a.get("settled")]
    stake = float(getattr(config, "PAPER_STAKE_GBP", 5.0))
    if s["n"] == 0 and not pending:
        await update.message.reply_text("No signals tracked yet.")
        return
    msg_lines = [
        f"<b>📊 Paper P&L</b>  (£{stake:g}/signal)",
        f"settled: <b>{s['n']}</b>  "
        f"({s['wins']}W / {s['losses']}L  ·  win rate {s['win_rate']*100:.1f}%)",
        f"pending: <b>{len(pending)}</b>",
        f"running P&L: <b>£{s['total_pnl']:+.2f}</b>  "
        f"on £{s['total_staked']:.0f} staked  ({s['roi_pct']:+.1f}% ROI)",
    ]
    if s["leagues"]:
        msg_lines.append("\n<b>By league</b>:")
        for lg, st in sorted(s["leagues"].items(),
                             key=lambda kv: kv[1]["pnl"], reverse=True):
            wr = (st["wins"] / st["n"] * 100) if st["n"] else 0.0
            msg_lines.append(
                f"  {lg or '?'}: {st['n']} bets, "
                f"<b>£{st['pnl']:+.2f}</b>  ({wr:.0f}% WR)"
            )
    if s["first_ts"] and s["last_ts"]:
        msg_lines.append(
            f"\nspan: {s['first_ts'][:10]} → {s['last_ts'][:10]}"
        )
    await update.message.reply_text("\n".join(msg_lines), parse_mode=ParseMode.HTML)


async def _cmd_xg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /xg <event_id>")
        return
    event_id = int(parts[1])

    # Local imports to avoid circular ones (this file is imported by runner.py).
    import asyncio
    from live.sofascore import SofaScore
    from live.monitor import (
        parse_shots, xg_rate_window, goals_total, _fmt_score, _match_minute,
    )

    def _fetch():
        with SofaScore() as s:
            return s.event(event_id), s.shotmap(event_id)

    try:
        ev, raw_shots = await asyncio.to_thread(_fetch)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Fetch error: {e}")
        return

    home = ev.get("homeTeam", {}).get("name", "?")
    away = ev.get("awayTeam", {}).get("name", "?")
    status_desc = (ev.get("status", {}) or {}).get("description", "?")
    shots = parse_shots(raw_shots)
    minute = _match_minute(ev) or 0
    if minute == 0 and shots:
        minute = max((s.minute for s in shots), default=0)
    score = _fmt_score(shots, minute) if shots else "0-0"
    cum_h = sum(s.xg for s in shots if s.is_home and s.minute <= minute)
    cum_a = sum(s.xg for s in shots if not s.is_home and s.minute <= minute)
    rate10 = xg_rate_window(shots, minute, window=10)
    rate15 = xg_rate_window(shots, minute, window=15)
    rate20 = xg_rate_window(shots, minute, window=20)
    gt = goals_total(shots, minute)

    msg = (
        f"<b>{home} v {away}</b>  <i>({status_desc})</i>\n"
        f"min <b>{minute}'</b>  score <b>{score}</b>  goals {gt}  shots {len(shots)}\n"
        f"cumulative xG: H <b>{cum_h:.2f}</b> · A <b>{cum_a:.2f}</b> · total <b>{cum_h+cum_a:.2f}</b>\n"
        f"xg_rate: <b>15m {rate15:.2f}</b>  ·  10m {rate10:.2f}  ·  20m {rate20:.2f}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def _cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    monitor = ctx.application.bot_data.get("monitor_state") or {}
    active = monitor.get("active") or []
    if not active:
        await update.message.reply_text("No active monitors.")
        return
    lines = ["Active monitors:"]
    for s in active:
        lines.append(
            f"  • [{s['event_id']}] {s.get('label', '?')}  —  "
            f"{s.get('last_tick') or 'starting…'}"
        )
    await update.message.reply_text("\n".join(lines))


async def _cmd_funds(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    bf = ctx.application.bot_data.get("betfair")
    if bf is None:
        await update.message.reply_text("Betfair client not initialised.")
        return
    try:
        funds = await asyncio.to_thread(bf.account_funds)
        await update.message.reply_text(
            f"Available: £{funds.get('availableToBetBalance', 0):.2f} "
            f"(exposure £{funds.get('exposure', 0):.2f})",
        )
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Funds error: {e}")


async def _cmd_kill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    import pathlib
    pathlib.Path(config.LIVE_KILL_SWITCH_FILE).touch()
    await update.message.reply_text(
        f"⛔ Kill switch enabled (touched {config.LIVE_KILL_SWITCH_FILE}). "
        "All bet placement is now blocked. Alerts will still fire. "
        "Delete the file to re-enable.",
    )


async def _on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if ":" not in data:
        return
    action, alert_id = data.split(":", 1)
    handle: BotHandle = ctx.application.bot_data["handle"]
    async with handle._lock:
        alert = handle._pending.get(alert_id)
        if alert is None or alert.resolved:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n<i>already resolved.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
            return
        alert.resolved = True
        alert.decision = action

    if action == "skip":
        await q.edit_message_text(
            text=handle._format_alert(alert) + "\n\n✖ <i>skipped</i>",
            parse_mode=ParseMode.HTML, reply_markup=None,
        )
        return

    if action == "cancel":
        await q.edit_message_text(
            text=handle._format_alert(alert) + "\n\n✖ <i>cancelled by user</i>",
            parse_mode=ParseMode.HTML, reply_markup=None,
        )
        return

    # action == "confirm" — manual mode placement
    if alert.placement_callback is None:
        await q.edit_message_text(
            text=handle._format_alert(alert) + "\n\n⚠ <i>no placement handler attached</i>",
            parse_mode=ParseMode.HTML, reply_markup=None,
        )
        return
    try:
        result = await alert.placement_callback(alert)
    except Exception as e:  # noqa: BLE001
        await q.edit_message_text(
            text=handle._format_alert(alert) + f"\n\n❌ <i>placement error: {e}</i>",
            parse_mode=ParseMode.HTML, reply_markup=None,
        )
        return
    await q.edit_message_text(
        text=handle._format_alert(alert) + _result_suffix(result, alert.price),
        parse_mode=ParseMode.HTML, reply_markup=None,
    )


def _result_suffix(result: dict, price: float) -> str:
    if result.get("status") == "SUCCESS":
        return (
            f"\n\n✅ <b>placed</b>  bet_id={result['bet_id']}  "
            f"matched={result['matched_size']:.2f} @ "
            f"{result.get('avg_price_matched') or price:.2f}"
        )
    return f"\n\n❌ <i>failed: {result.get('error')}</i>"


# --------------------------------------------------------------------------- #
# Bot bootstrap
# --------------------------------------------------------------------------- #
async def build_application(
    *,
    on_watch: Callable[[int], Awaitable[None]] | None = None,
    on_stop: Callable[[], Awaitable[None]] | None = None,
) -> tuple[Application, BotHandle]:
    """Build (but do not start) the Application; return it + a BotHandle."""
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID or "").strip()
    missing = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(
            "Telegram credentials missing in config.py: " + ", ".join(missing)
            + ".\nDid you forget to save the file?"
        )
    app = Application.builder().token(token).build()
    handle = BotHandle(app)
    app.bot_data["handle"] = handle

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("pnl", _cmd_pnl))
    app.add_handler(CommandHandler("recent", _cmd_recent))
    app.add_handler(CommandHandler("xg", _cmd_xg))
    app.add_handler(CommandHandler("debug", _cmd_debug))
    app.add_handler(CommandHandler("funds", _cmd_funds))
    app.add_handler(CommandHandler("kill", _cmd_kill))
    app.add_handler(CallbackQueryHandler(_on_callback))

    if on_watch is not None:
        async def _cmd_watch(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_authorised(update):
                return
            parts = (update.message.text or "").split()
            if len(parts) < 2 or not parts[1].isdigit():
                await update.message.reply_text("Usage: /watch <event_id>")
                return
            await on_watch(int(parts[1]))
        app.add_handler(CommandHandler("watch", _cmd_watch))

    if on_stop is not None:
        async def _cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_authorised(update):
                return
            parts = (update.message.text or "").split()
            event_id: int | None = None
            if len(parts) >= 2 and parts[1].isdigit():
                event_id = int(parts[1])
            ok = await on_stop(event_id)
            if event_id is not None:
                msg = (
                    f"Stopped monitor for {event_id}." if ok
                    else f"Not currently watching event {event_id}."
                )
            else:
                msg = "Stopped all monitors." if ok else "No active monitors to stop."
            await update.message.reply_text(msg)
        app.add_handler(CommandHandler("stop", _cmd_stop))

    return app, handle


def new_alert_id() -> str:
    return uuid.uuid4().hex[:8]
