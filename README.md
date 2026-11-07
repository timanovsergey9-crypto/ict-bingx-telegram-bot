# ICT BingX Telegram Bot

High-precision ICT (Inner Circle Trader / Michael Huddleston) signal bot for the top 20 crypto pairs on **BingX**.

### Core ICT Logic
- Higher Timeframe (default 4H) bias + Premium/Discount
- Lower Timeframe (default 15m) Liquidity Sweep of swing high/low
- Confirmed Market Structure Shift (MSS) with displacement
- Unmitigated Fair Value Gap (FVG)
- Entry at Consequent Encroachment (CE)
- Full explanation of every condition in the Telegram message

### Quick Start on Server

```bash
# 1. Clone
git clone https://github.com/timanovsergey9-crypto/ict-bingx-telegram-bot.git
cd ict-bingx-telegram-bot

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Set secrets
cp .env.example .env
nano .env   # paste your TELEGRAM_TOKEN and CHAT_ID

# 4. Load env and run
export $(cat .env | xargs)
python bot.py
```

### Run in background (recommended)

```bash
# Using screen
screen -S ictbot
export $(cat .env | xargs)
python bot.py
# Ctrl+A then D to detach

# Or with nohup
nohup python bot.py > bot.log 2>&1 &
```

### Systemd service (optional)

Create `/etc/systemd/system/ict-bot.service`:

```ini
[Unit]
Description=ICT BingX Telegram Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/ict-bingx-telegram-bot
EnvironmentFile=/path/to/ict-bingx-telegram-bot/.env
ExecStart=/path/to/venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ict-bot
```

### How to get Telegram credentials
1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy token.
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot) or add the bot to a channel and use the channel ID.

### Disclaimer
This is an educational implementation of ICT concepts. Not financial advice. Trading involves risk of loss. Always manage your own risk.
