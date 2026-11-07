#!/usr/bin/env python3
"""
ICT BingX Telegram Bot - Test version for GitHub Actions
"""
import os
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta

import ccxt
from telegram import Bot
from telegram.constants import ParseMode

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Set TELEGRAM_TOKEN and CHAT_ID secrets")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ICT-Bot")

exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "spot"}})

KILL_ZONES = {"London": (7, 10), "New York": (12, 15), "Asia": (0, 3)}

def get_msk_time(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return (dt + timedelta(hours=3)).strftime("%H:%M МСК")

def is_kill_zone():
    hour = datetime.now(timezone.utc).hour
    for name, (s, e) in KILL_ZONES.items():
        if s <= hour < e:
            return name
    return None

def next_kill_zone_info():
    now = datetime.now(timezone.utc)
    for name, (start, _) in KILL_ZONES.items():
        start_dt = now.replace(hour=start, minute=0, second=0, microsecond=0)
        if start_dt <= now:
            start_dt += timedelta(days=1)
        minutes = (start_dt - now).total_seconds() / 60
        if 0 < minutes <= 12:
            return {"name": name, "opens_at_msk": get_msk_time(start_dt), "minutes": int(minutes)}
    return None

async def send_message(bot, text):
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)

def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    logger.info("ICT Bot started (GitHub Actions test)")

    async def run():
        last_alert = None
        while True:
            try:
                kz_info = next_kill_zone_info()
                if kz_info and last_alert != kz_info["name"]:
                    await send_message(bot, f"⏰ Kill Zone скоро\nЧерез ~{kz_info['minutes']} мин откроется <b>{kz_info['name']}</b>\nВремя: <b>{kz_info['opens_at_msk']}</b>")
                    last_alert = kz_info["name"]

                kz = is_kill_zone()
                if kz:
                    await send_message(bot, f"🟢 Сейчас активна Kill Zone: <b>{kz}</b>\nБот сканирует рынок...")
                    logger.info(f"In Kill Zone: {kz}")
                else:
                    logger.info("Outside Kill Zone")

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(e)
                await asyncio.sleep(30)

    asyncio.run(run())

if __name__ == "__main__":
    main()
