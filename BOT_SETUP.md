# Live monitor — setup & run guide

## 1. Create the Telegram bot (2 minutes)

1. On Telegram, open a chat with **@BotFather**.
2. Send `/newbot`.
3. Pick a name (e.g. `Sportbet xG Live`) — any name is fine.
4. Pick a username — must end in `bot` (e.g. `sportbet_xg_live_bot`).
5. BotFather replies with a token like `1234567890:AAEhBOQ8U…`. Copy it.

Then get your own chat id:

1. Open a chat with **@userinfobot** on Telegram.
2. Send `/start`. It replies with your numeric `Id`. Copy it.

Open a chat with your new bot and tap `Start` — this is required before the bot can DM you.

## 2. Fill in credentials

Edit [`config.py`](config.py) (gitignored — never commit):

```python
TELEGRAM_BOT_TOKEN = "1234567890:AAEhBOQ8U..."   # from BotFather
TELEGRAM_CHAT_ID   = "123456789"                  # from userinfobot

BETFAIR_USERNAME = "your.betfair.email@example.com"
BETFAIR_PASSWORD = "..."
BETFAIR_APP_KEY  = "..."                          # production key, not delayed
```

Everything else (stake caps, EV floor, kill switch path) is already populated with the OOS-derived defaults.

## 3. Install dependencies

```powershell
cd c:\Users\lukas\football\sportbet
pip install -r requirements.txt
```

## 4. Run

```powershell
python -m live.runner
```

You should see:

```
INFO sportbet.runner: Telegram polling started; awaiting commands…
```

…and your bot DMs you: `🟢 Live monitor online`.

## 5. Watch a match

In the Telegram chat with the bot:

```
/watch 14023928        ← Aston Villa v Liverpool, 15-May 19:00 UTC
```

The bot finds the Betfair Over 2.5 market for that fixture, then on each
30-second poll checks the SofaScore xG-rate. When **both** conditions hit —
`xg_rate_15m ≥ 0.20` in the 30–85 min window **AND** Betfair live EV ≥ 0.10 —
it DMs you a card like:

```
⚡ SIGNAL — Aston Villa v Liverpool
min 42'  score 1-1
xg_rate_15m = 0.45  (≥ 0.20)
market: Over 2.5
LTP: 2.34  EV: +0.121
stake: £10  (expires in 60s)

[✅ Place £10]   [✖ Skip]
```

Tap `Place £10` — the bot re-pulls the price (rejects if it moved meaningfully), submits the back bet via `betfairlightweight.place_orders`, and edits the message with `bet_id` and matched details.

## 6. Safety rails (already enabled)

| Rail | Where | Default |
|---|---|---|
| Max stake per trade | `config.LIVE_MAX_STAKE_GBP` | £10 — bet aborts if exceeded |
| Daily stake cap | `config.LIVE_DAILY_STAKE_CAP_GBP` | £30 — bet aborts if exceeded |
| Confirm timeout | `config.LIVE_CONFIRM_TIMEOUT_S` | 60 s — alert auto-expires |
| Kill switch | presence of file `KILL` | `/kill` from Telegram creates it |
| Chat-id whitelist | `config.TELEGRAM_CHAT_ID` | non-listed chats are silently ignored |
| Re-priced EV check | runs at `Place` time | aborts if EV collapsed pre-fill |

To re-enable placement after `/kill`:

```powershell
del KILL
```

## 7. Other commands

```
/status   — what's being watched + last tick
/funds    — Betfair available balance + exposure
/stop     — cancel the current monitor
```

## 8. Useful event ids (PL 2025/26 remaining as of 2026-05-14)

| Match | Event id | Kickoff (UTC) |
|---|---|---|
| Aston Villa v Liverpool | 14023928 | 2026-05-15 19:00 |
| Manchester United v Nottingham Forest | 14023956 | 2026-05-17 11:30 |
| Brentford v Crystal Palace | 14023953 | 2026-05-17 14:00 |
| Everton v Sunderland | 14023955 | 2026-05-17 14:00 |
| Leeds United v Brighton | 14023954 | 2026-05-17 14:00 |
| Wolves v Fulham | 14023958 | 2026-05-17 14:00 |
| Newcastle United v West Ham | 14023957 | 2026-05-17 16:30 |
| Arsenal v Burnley | 14023950 | 2026-05-18 19:00 |
| Bournemouth v Manchester City | 14023936 | 2026-05-19 18:30 |
| Chelsea v Tottenham | 16087727 | 2026-05-19 19:15 |
| (all 10 fixtures, final matchday) | various | 2026-05-24 15:00 |

Find more via `python -X utf8 -c "from live.sofascore import SofaScore; ..."` if needed.
