# Deploy the live monitor on Hetzner Cloud

Total time: ~15 min. End state: bot runs 24/7 on a €4.51/mo VM, auto-restarts on crash, survives reboots. You message the bot from Telegram exactly as you do locally — the deployment is transparent from your phone's perspective.

## 1. Create the VM (5 min)

1. Sign up at <https://console.hetzner.cloud/> (Hetzner Cloud, not Hetzner Robot).
2. **+ New project** → name it `sportbet` (or anything).
3. **+ Add Server**:
   - Location: **Falkenstein** or **Helsinki** (both are fine; low latency to Betfair LDN + SofaScore Vienna).
   - Image: **Ubuntu 24.04**.
   - Type: **CX22** (€4.51/mo, 2 vCPU, 4 GB RAM, 40 GB SSD). The CPX/CCX tiers are overkill.
   - Networking: leave **Public IPv4** + **Public IPv6** on.
   - SSH Keys: add yours (Generate one on your laptop first if you don't have one: `ssh-keygen -t ed25519`; paste `~/.ssh/id_ed25519.pub` into Hetzner). If you skip this Hetzner emails you a root password — using SSH keys is strongly preferred.
   - Name: `sportbet`.
4. Click **Create & Buy**. Wait ~30 s. Note the public IPv4 — call it `<IP>` below.

## 2. Bootstrap the VM (5 min)

From your laptop:

```powershell
ssh root@<IP>
```

(If using a fresh SSH key, you'll be asked to accept the host key — type `yes`.)

Once you're in, run:

```bash
curl -fsSL https://raw.githubusercontent.com/lukasbecker36-dot/Sportbet/HEAD/deploy/bootstrap.sh | sudo bash
```

This:
- Installs Python 3.11, git, tzdata
- Sets timezone to UTC
- Creates a non-root `sportbet` user
- Clones the repo into `/home/sportbet/Sportbet/`
- Creates a venv + installs `requirements.txt`
- Installs the systemd unit (but does not start it yet — your secrets aren't on the box)

By default it tracks your repo's default branch on GitHub. To pin a specific one: `BRANCH=foo curl … | sudo -E bash`.

## 3. Upload your `config.py` (1 min)

From your laptop (NOT inside the SSH session):

```powershell
scp c:\Users\lukas\football\sportbet\config.py root@<IP>:/home/sportbet/Sportbet/config.py
ssh root@<IP> 'chown sportbet:sportbet /home/sportbet/Sportbet/config.py && chmod 600 /home/sportbet/Sportbet/config.py'
```

`config.py` is gitignored, so it never went through GitHub. This `scp` is the only place your credentials touch the VM.

## 4. Start it (30 s)

Back on the VM:

```bash
sudo systemctl start sportbet
sudo systemctl status sportbet
sudo journalctl -u sportbet -f
```

Within a few seconds you'll see your Telegram bot DM:

```
🟢 Live monitor online. Auto-discover: ON
leagues: 5 (stake £10, EV floor +0.10, daily cap £30)
```

Press `Ctrl+C` to stop tailing the log (the service keeps running).

## 5. Verify it survives a reboot

```bash
sudo reboot
```

Wait ~30 s, SSH back in, run `systemctl status sportbet` — it should be `active (running)` again. You'll also get a fresh `🟢 Live monitor online` Telegram message after each boot.

## Day-to-day operations

| Action | Command |
|---|---|
| Tail live logs | `sudo journalctl -u sportbet -f` |
| Show last 200 lines | `sudo journalctl -u sportbet -n 200` |
| Logs from today | `sudo journalctl -u sportbet --since today` |
| Restart the bot | `sudo systemctl restart sportbet` |
| Stop / start | `sudo systemctl stop sportbet` / `start` |
| Pull latest code | `sudo systemctl stop sportbet && sudo -u sportbet bash -c 'cd /home/sportbet/Sportbet && git pull && .venv/bin/pip install -q -r requirements.txt' && sudo systemctl start sportbet` |
| Update config | `scp config.py root@<IP>:/home/sportbet/Sportbet/config.py && ssh root@<IP> systemctl restart sportbet` |
| Emergency stop placement | message `/kill` in Telegram (creates `KILL` file) |
| Re-enable placement | `ssh root@<IP> 'sudo -u sportbet rm /home/sportbet/Sportbet/KILL'` |

## Cost

- VM: €4.51/mo (Hetzner CX22). Includes 20 TB outbound traffic — you'll use <1 GB/mo.
- IPv4 fee: €0.50/mo (Hetzner bundles this). Total ~€5/mo.
- That's it. No bandwidth surprises; no Betfair/SofaScore/Telegram bills.

## Why this design

- **systemd** auto-restarts the bot if it crashes (`Restart=always`, 10s back-off) and starts it on boot (`WantedBy=multi-user.target`).
- **Non-root** user (`sportbet`) — even if the bot were exploited, it can't touch system files.
- **Hardening flags** (`PrivateTmp`, `NoNewPrivileges`, `ProtectSystem=full`) — `/etc`, `/usr`, `/boot` are read-only to the service.
- **No inbound ports** — the bot only does outbound HTTPS to Telegram / SofaScore / Betfair. Hetzner's default firewall already blocks all inbound except SSH; you don't need to open anything.
- **UTC timezone** — match logs to SofaScore + Betfair timestamps without conversion.

## Things to know

- **Hetzner sends a verification email** before first sign-up — they sometimes review new accounts. Usually instant; occasionally a few hours.
- **First boot can take 60–90 s** after creation; if SSH refuses immediately, wait a minute.
- The bootstrap script clones from your `main` branch by default. Override with `BRANCH=foo` before the curl.
- Betfair sessions are now auto-refreshed every 3 h (see `live/betfair_client.py`) — no manual relogin needed for long-running uptime.
