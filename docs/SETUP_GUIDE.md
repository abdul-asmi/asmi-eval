# Asmi Eval — Setup Guide

You need two things running:
1. **The web UI** — already live at [asmi-eval.onrender.com](https://asmi-eval.onrender.com)
2. **The daemon** — a small script you run on your Mac that sends the actual iMessages

The web UI can't send iMessages itself. Your Mac's Messages app does the sending. The daemon bridges the two.

---

## What you need

- A Mac with the **Messages app** signed into an Apple ID
- Python 3.10+ (check with `python3 --version`)
- Terminal with **Full Disk Access** (one-time system setting)

For WhatsApp-only evals, the Mac is not required. The hosted Render service sends
through Twilio and receives replies through a Twilio webhook.

---

## Step 1 — Clone the repo

```bash
cd ~/Desktop
git clone https://github.com/YOUR-ORG/asmi-eval.git
cd asmi-eval
pip install google-generativeai
```

---

## Step 2 — Grant Terminal Full Disk Access

The daemon reads `~/Library/Messages/chat.db` to capture Asmi's replies.
macOS blocks this unless you explicitly allow it.

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click **+** and add **Terminal** (or **iTerm**, whichever you use)
3. Toggle it **on**
4. Quit and reopen Terminal

> If you skip this step the daemon will start but won't see any replies.

---

## Step 3 — Get your User ID from the web UI

1. Go to [asmi-eval.onrender.com](https://asmi-eval.onrender.com) and log in
2. Open your browser's developer tools (⌘⌥I) → **Console** tab
3. Run this in the console:

```js
(await (await fetch('/api/me')).json()).id
```

Copy the UUID that appears — this is your **User ID**.

---

## Step 4 — Create your config file

In the `asmi-eval` folder, create a file called `.env.local`:

```bash
# .env.local — your personal daemon settings
REMOTE_UI_URL=https://asmi-eval.onrender.com
DAEMON_TOKEN=ask-admin-for-this
DAEMON_OWNER_USER_ID=paste-your-user-id-here
```

> **DAEMON_TOKEN** is a shared secret set by the admin. Ask whoever manages the Render deployment for the value.

---

## Step 5 — Start the daemon

```bash
cd ~/Desktop/asmi-eval
source .env.local 2>/dev/null || export $(cat .env.local | grep -v '^#' | xargs)
python daemon.py
```

You should see something like:

```
[daemon] polling https://asmi-eval.onrender.com every 5s
[daemon] connected ✓
```

---

## Step 6 — Verify it's connected

Go back to [asmi-eval.onrender.com](https://asmi-eval.onrender.com). The **Run Monitor** panel should show a green **Mac Online** indicator within ~10 seconds.

If it shows **Waiting for runner**, the daemon isn't reaching the server yet — double-check your `.env.local` values.

---

## Running the daemon in the background

To keep the daemon running after you close Terminal:

```bash
cd ~/Desktop/asmi-eval
source .env.local 2>/dev/null || export $(cat .env.local | grep -v '^#' | xargs)
nohup python daemon.py > daemon.log 2>&1 &

# Check it's alive
tail -f daemon.log

# Stop it later
pkill -f daemon.py
```

Or use the convenience script:

```bash
bash scripts/restart_daemon.sh
```

---

## Keeping it running permanently (optional)

If you want the daemon to start automatically on login and restart if it crashes, use the LaunchAgent:

```bash
bash scripts/setup_daemon.sh
```

This installs a macOS background service. To uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.asmi.eval.daemon.plist
rm ~/Library/LaunchAgents/com.asmi.eval.daemon.plist
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **Mac Online** never goes green | Check `REMOTE_UI_URL` and `DAEMON_TOKEN` in `.env.local` |
| Daemon starts but tests time out | Terminal probably lacks Full Disk Access — redo Step 2 |
| `ModuleNotFoundError` on startup | Run `pip install google-generativeai` |
| Old results show instead of new | The daemon hasn't claimed the job yet — watch `daemon.log` |
| "Waiting for runner" after 30s | Kill and restart with `pkill -f daemon.py && python daemon.py` |

---

## Once it's running

Go to [asmi-eval.onrender.com](https://asmi-eval.onrender.com), pick a test or category, and click **Run**. The daemon on your Mac will pick it up, send the iMessages, and post results back to the UI within a few minutes.

---

## WhatsApp Cloud Evals

WhatsApp evals run fully on Render when every selected test has
`channel: "whatsapp"`. Mixed or iMessage selections still use the Mac daemon.

Set these Render environment variables:

```bash
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=+your_business_whatsapp_number
WHATSAPP_DEV_HANDLE=+asmi_dev_whatsapp_number
WHATSAPP_PROD_HANDLE=+asmi_prod_whatsapp_number
```

In Twilio, set the WhatsApp sender inbound webhook to:

```text
https://asmi-eval.onrender.com/api/whatsapp/webhook
```

If evals may start outside the WhatsApp 24-hour customer service window, add an
approved warmup template:

```bash
WHATSAPP_WARMUP_ENABLED=1
WHATSAPP_WARMUP_TEMPLATE_SID=HX...
WHATSAPP_WARMUP_TIMEOUT=60
```

The warmup template is sent first. Asmi must reply to it; that inbound reply is
what opens the 24-hour freeform window for the real eval messages.
