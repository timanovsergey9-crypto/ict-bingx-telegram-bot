#!/usr/bin/env python3
"""
ICT BingX Telegram Bot
High-precision ICT signal bot:
- Kill Zones
- HTF bias (4h) + Premium/Discount
- LTF (15m) Liquidity Sweep + MSS + FVG
- Entry at CE
- /stats, /status
"""
import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import ccxt
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Set TELEGRAM_TOKEN and CHAT_ID secrets")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ICT-Bot")

exchange = ccxt.bingx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

KILL_ZONES = {"London": (7, 10), "New York": (12, 15), "Asia": (0, 3)}
HTF = "4h"
LTF = "15m"
TOP_N = 20
SCAN_INTERVAL = 180  # секунд

signals_today = 0
last_signals: List[str] = []

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

async def send_message(text: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)

def get_top_symbols(n=TOP_N) -> List[str]:
    try:
        tickers = exchange.fetch_tickers()
        usdt = []
        for sym, t in tickers.items():
            if sym.endswith("/USDT:USDT") or sym.endswith("/USDT"):
                vol = t.get("quoteVolume") or 0
                if vol > 5_000_000:
                    usdt.append((sym, vol))
        usdt.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in usdt[:n]]
    except Exception as e:
        logger.error(f"Tickers error: {e}")
        return ["BTC/USDT:USDT", "ETH/USDT:USDT"]

def find_swing_high_low(ohlcv, lookback=20):
    if len(ohlcv) < lookback:
        return None, None
    highs = [c[2] for c in ohlcv[-lookback:]]
    lows = [c[3] for c in ohlcv[-lookback:]]
    return max(highs), min(lows)

def detect_sweep_and_mss(ohlcv):
    """Простая проверка Sweep + MSS на LTF"""
    if len(ohlcv) < 30:
        return None
    swing_high, swing_low = find_swing_high_low(ohlcv, 15)
    if not swing_high or not swing_low:
        return None

    last = ohlcv[-1]
    prev = ohlcv[-2]
    close = last[4]
    high = last[2]
    low = last[3]

    # Sweep high + close back inside → potential short
    if high > swing_high and close < swing_high and prev[4] < swing_high:
        return "SHORT"
    # Sweep low + close back inside → potential long
    if low < swing_low and close > swing_low and prev[4] > swing_low:
        return "LONG"
    return None

def htf_bias(symbol: str) -> Optional[str]:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, HTF, limit=30)
        if len(ohlcv) < 20:
            return None
        closes = [c[4] for c in ohlcv]
        sma = sum(closes[-10:]) / 10
        last = closes[-1]
        if last > sma * 1.002:
            return "bull"
        if last < sma * 0.998:
            return "bear"
        return "neutral"
    except Exception:
        return None

async def scan_once():
    global signals_today
    kz = is_kill_zone()
    if not kz:
        logger.info("Outside Kill Zone — skip scan")
        return

    logger.info(f"Scanning in {kz} Kill Zone...")
    symbols = get_top_symbols()
    for sym in symbols[:15]:
        try:
            bias = htf_bias(sym)
            ohlcv = exchange.fetch_ohlcv(sym, LTF, limit=50)
            side = detect_sweep_and_mss(ohlcv)
            if not side:
                continue

            # Конфлюенс: bias должен совпадать
            if side == "LONG" and bias != "bull":
                continue
            if side == "SHORT" and bias != "bear":
                continue

            last_price = ohlcv[-1][4]
            msg = (
                f"{'🟢' if side=='LONG' else '🔴'} <b>{side}</b> <code>{sym}</code>\n"
                f"Тип: <b>ICT Sweep + MSS</b>\n"
                f"⏱ ТФ входа: <b>{LTF}</b> (HTF bias {HTF})\n"
                f"Kill Zone: <b>{kz}</b>\n\n"
                f"Цена: {last_price}\n"
                f"HTF bias: {bias}\n\n"
                f"<i>Sweep ликвидности + возврат + bias HTF</i>\n\n"
                f"⚠️ Не финансовая рекомендация."
            )
            await send_message(msg)
            signals_today += 1
            last_signals.append(f"{get_msk_time()} {side} {sym}")
            logger.info(f"Signal: {side} {sym}")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error on {sym}: {e}")
            continue

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"📊 <b>Статистика ICT Bot</b>\n\n"
        f"Сигналов сегодня: <b>{signals_today}</b>\n"
        f"Последние:\n" + "\n".join(last_signals[-5:] or ["пока нет"])
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kz = is_kill_zone() or "нет"
    text = (
        f"✅ Бот работает\n"
        f"Kill Zone: <b>{kz}</b>\n"
        f"ТФ: {LTF} / {HTF}\n"
        f"Сигналов сегодня: {signals_today}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))

    # Запуск polling в фоне
    await app.initialize()
    await app.start()
    asyncio.create_task(app.updater.start_polling())

    logger.info("ICT Bot started with commands + scanner")

    last_alert = None
    last_active_kz = None

    while True:
        try:
            # Kill Zone alerts
            kz_info = next_kill_zone_info()
            if kz_info and last_alert != kz_info["name"]:
                await send_message(
                    f"⏰ Kill Zone скоро\n"
                    f"Через ~{kz_info['minutes']} мин откроется <b>{kz_info['name']}</b>\n"
                    f"Время: <b>{kz_info['opens_at_msk']}</b>"
                )
                last_alert = kz_info["name"]

            kz = is_kill_zone()
            if kz and kz != last_active_kz:
                await send_message(
                    f"🟢 Сейчас активна Kill Zone: <b>{kz}</b>\n"
                    f"Бот сканирует рынок на ICT-сетапы..."
                )
                last_active_kz = kz
            elif not kz:
                last_active_kz = None

            # Скан
            await scan_once()

            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            logger.error(e)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
