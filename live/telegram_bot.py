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


async def _cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    await update.message.reply_text(
        "Sportbet live monitor connected.\n"
        "/watch <event_id> — start watching a SofaScore match (multiple OK)\n"
        "/status — list active monitors\n"
        "/recent — last 10 fired signals (across all matches)\n"
        "/xg <event_id> — current xg_rate_15m + score for a live match\n"
        "/stop [event_id] — stop one monitor (or all if no id)\n"
        "/funds — show Betfair balance (needs Account API perm)\n"
        "/kill — emergency disable of all bet placement",
    )


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
        lines.append(
            f"• {ts}  {a['home']} v {a['away']}\n"
            f"   min {a['minute']}'  score {a['score']}  "
            f"xG15 <b>{a['xg_rate']:.2f}</b>  "
            f"{a['market']}@<b>{a['price']:.2f}</b>  "
            f"EV <b>{a['ev']:+.2f}</b>  ({a['mode']})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("recent", _cmd_recent))
    app.add_handler(CommandHandler("xg", _cmd_xg))
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
            await on_stop(event_id)
            await update.message.reply_text(
                f"Stopped monitor for {event_id}." if event_id is not None
                else "Stopped all monitors.",
            )
        app.add_handler(CommandHandler("stop", _cmd_stop))

    return app, handle


def new_alert_id() -> str:
    return uuid.uuid4().hex[:8]
