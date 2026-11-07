#!/usr/bin/env python3
"""
High-Precision ICT Trading Signal Bot for BingX (Top 20)
Based on Michael J. Huddleston (Inner Circle Trader) methodology.

Core logic (strict approximation):
- HTF (4H/1D) bias + Premium/Discount
- LTF (15m) Liquidity Sweep of swing high/low
- Confirmed Market Structure Shift (MSS) with displacement
- Unmitigated Fair Value Gap (FVG) or Order Block in the correct zone
- Entry at Consequent Encroachment (CE) of FVG or refined OB
- Full explanation of every condition

Not financial advice. For educational and research purposes only.
"""

import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import ccxt
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.constants import ParseMode

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Set TELEGRAM_TOKEN and CHAT_ID environment variables")

HTF = os.getenv("HTF", "4h")          # Higher timeframe for bias
LTF = os.getenv("LTF", "15m")         # Entry timeframe
TOP_N = int(os.getenv("TOP_N", "20"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "180"))  # seconds

# ICT parameters
SWING_LOOKBACK = 5
FVG_MIN_ATR_MULT = 0.25
DISPLACEMENT_ATR_MULT = 1.8
MIN_VOLUME_USDT = 50_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ICT-Bot")

exchange = ccxt.bingx({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# ====================== DATA ======================
def get_top_symbols(n: int = TOP_N) -> List[str]:
    try:
        tickers = exchange.fetch_tickers()
        pairs = [
            (sym, t["quoteVolume"])
            for sym, t in tickers.items()
            if sym.endswith("/USDT") and t.get("quoteVolume", 0) > MIN_VOLUME_USDT
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in pairs[:n]]
    except Exception as e:
        logger.error(f"Tickers error: {e}")
        return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df
    except Exception as e:
        logger.warning(f"OHLCV {symbol} {timeframe}: {e}")
        return pd.DataFrame()

# ====================== ICT CORE ======================
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def fractal_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> Tuple[pd.Series, pd.Series]:
    """Proper fractal swing highs and lows."""
    highs = df["high"]
    lows = df["low"]
    swing_high = pd.Series(False, index=df.index)
    swing_low = pd.Series(False, index=df.index)

    for i in range(left, len(df) - right):
        if highs.iloc[i] == highs.iloc[i-left:i+right+1].max():
            swing_high.iloc[i] = True
        if lows.iloc[i] == lows.iloc[i-left:i+right+1].min():
            swing_low.iloc[i] = True
    return swing_high, swing_low

def detect_fvgs(df: pd.DataFrame) -> List[Dict]:
    """Detect unmitigated Fair Value Gaps with size filter."""
    if len(df) < 5:
        return []
    a = atr(df)
    fvgs = []
    for i in range(2, len(df)):
        # Bullish FVG
        if df["low"].iloc[i] > df["high"].iloc[i-2]:
            size = df["low"].iloc[i] - df["high"].iloc[i-2]
            if size > a.iloc[i] * FVG_MIN_ATR_MULT:
                fvgs.append({
                    "type": "bullish",
                    "top": df["low"].iloc[i],
                    "bottom": df["high"].iloc[i-2],
                    "ce": (df["low"].iloc[i] + df["high"].iloc[i-2]) / 2,
                    "index": i,
                    "time": df.index[i],
                    "mitigated": False
                })
        # Bearish FVG
        elif df["high"].iloc[i] < df["low"].iloc[i-2]:
            size = df["low"].iloc[i-2] - df["high"].iloc[i]
            if size > a.iloc[i] * FVG_MIN_ATR_MULT:
                fvgs.append({
                    "type": "bearish",
                    "top": df["low"].iloc[i-2],
                    "bottom": df["high"].iloc[i],
                    "ce": (df["low"].iloc[i-2] + df["high"].iloc[i]) / 2,
                    "index": i,
                    "time": df.index[i],
                    "mitigated": False
                })

    # Mark mitigated
    for fvg in fvgs:
        for j in range(fvg["index"] + 1, len(df)):
            if fvg["type"] == "bullish" and df["low"].iloc[j] <= fvg["bottom"]:
                fvg["mitigated"] = True
                break
            if fvg["type"] == "bearish" and df["high"].iloc[j] >= fvg["top"]:
                fvg["mitigated"] = True
                break
    return [f for f in fvgs if not f["mitigated"]]

def detect_sweep(df: pd.DataFrame, swing_high: pd.Series, swing_low: pd.Series) -> Optional[Dict]:
    """Detect recent liquidity sweep of a swing level with rejection."""
    if len(df) < 10:
        return None
    last = df.iloc[-1]
    prev_swings_high = df.loc[swing_high, "high"].iloc[:-1]
    prev_swings_low = df.loc[swing_low, "low"].iloc[:-1]

    if len(prev_swings_high) == 0 or len(prev_swings_low) == 0:
        return None

    recent_high = prev_swings_high.iloc[-1]
    recent_low = prev_swings_low.iloc[-1]

    # Bullish sweep: took low and closed back above
    if last["low"] < recent_low and last["close"] > recent_low:
        return {"type": "bullish", "level": recent_low, "time": last.name}
    # Bearish sweep
    if last["high"] > recent_high and last["close"] < recent_high:
        return {"type": "bearish", "level": recent_high, "time": last.name}
    return None

def detect_mss(df: pd.DataFrame, swing_high: pd.Series, swing_low: pd.Series) -> Optional[str]:
    """Market Structure Shift with displacement."""
    if len(df) < 20:
        return None
    a = atr(df).iloc[-1]
    last = df.iloc[-1]
    prev_highs = df.loc[swing_high, "high"].iloc[:-1]
    prev_lows = df.loc[swing_low, "low"].iloc[:-1]

    if len(prev_highs) < 1 or len(prev_lows) < 1:
        return None

    # Bullish MSS
    if last["close"] > prev_highs.iloc[-1] and (last["close"] - last["open"]) > a * DISPLACEMENT_ATR_MULT:
        return "bullish"
    # Bearish MSS
    if last["close"] < prev_lows.iloc[-1] and (last["open"] - last["close"]) > a * DISPLACEMENT_ATR_MULT:
        return "bearish"
    return None

def get_premium_discount(df: pd.DataFrame) -> str:
    """Simple dealing range premium/discount."""
    recent = df.iloc[-50:]
    high = recent["high"].max()
    low = recent["low"].min()
    mid = (high + low) / 2
    current = df["close"].iloc[-1]
    return "discount" if current < mid else "premium"

def analyze(symbol: str) -> Optional[Dict]:
    df_htf = fetch_ohlcv(symbol, HTF, 200)
    df_ltf = fetch_ohlcv(symbol, LTF, 250)

    if df_htf.empty or df_ltf.empty or len(df_ltf) < 40:
        return None

    # HTF bias
    ema = df_htf["close"].ewm(span=50).mean().iloc[-1]
    htf_bias = "bullish" if df_htf["close"].iloc[-1] > ema else "bearish"
    zone = get_premium_discount(df_htf)

    # LTF analysis
    sh, sl = fractal_swings(df_ltf)
    sweep = detect_sweep(df_ltf, sh, sl)
    mss = detect_mss(df_ltf, sh, sl)
    fvgs = detect_fvgs(df_ltf)

    if not (sweep and mss and fvgs):
        return None

    direction = None
    if sweep["type"] == "bullish" and mss == "bullish" and htf_bias == "bullish" and zone == "discount":
        direction = "LONG"
    elif sweep["type"] == "bearish" and mss == "bearish" and htf_bias == "bearish" and zone == "premium":
        direction = "SHORT"

    if not direction:
        return None

    # Find best unmitigated FVG
    matching = [f for f in fvgs if f["type"].startswith(direction[0].lower())]
    if not matching:
        return None

    best_fvg = matching[-1]  # most recent
    entry = round(best_fvg["ce"], 6)
    stop = round(best_fvg["bottom"] if direction == "LONG" else best_fvg["top"], 6)
    risk = abs(entry - stop)
    tp1 = round(entry + risk * 2 if direction == "LONG" else entry - risk * 2, 6)

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "htf": HTF,
        "ltf": LTF,
        "htf_bias": htf_bias,
        "zone": zone,
        "sweep_level": round(sweep["level"], 6),
        "fvg_range": f"{best_fvg['bottom']:.6f} - {best_fvg['top']:.6f}",
        "explanation": (
            f"HTF ({HTF}) bias: {htf_bias.upper()} | Zone: {zone.upper()}\n"
            f"LTF ({LTF}): Liquidity Sweep of {sweep['level']:.4f}\n"
            f"Market Structure Shift confirmed with displacement\n"
            f"Unmitigated {best_fvg['type'].upper()} FVG → Entry at CE\n"
            f"Risk:Reward 1:2"
        )
    }

# ====================== TELEGRAM ======================
async def send_signal(bot: Bot, sig: Dict):
    text = (
        f"🎯 <b>ICT HIGH-PRECISION SIGNAL</b>\n\n"
        f"<b>Pair:</b> {sig['symbol']}\n"
        f"<b>Direction:</b> {sig['direction']}\n"
        f"<b>Entry (FVG CE):</b> <code>{sig['entry']}</code>\n"
        f"<b>Stop Loss:</b> <code>{sig['stop']}</code>\n"
        f"<b>TP1 (1:2):</b> <code>{sig['tp1']}</code>\n"
        f"<b>Timeframes:</b> HTF {sig['htf']} | Entry {sig['ltf']}\n\n"
        f"<b>Conditions met:</b>\n{sig['explanation']}\n\n"
        f"<i>ICT Methodology (Huddleston) — Sweep + MSS + FVG in Premium/Discount.\n"
        f"Wait for precise retest of the FVG on {sig['ltf']}. Not financial advice.</i>"
    )
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)

# ====================== MAIN ======================
def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    logger.info("ICT BingX High-Precision Bot started")
    logger.info(f"HTF={HTF} | LTF={LTF} | Top {TOP_N} pairs")

    seen = set()

    while True:
        try:
            symbols = get_top_symbols()
            logger.info(f"Scanning {len(symbols)} pairs...")

            for symbol in symbols:
                sig = analyze(symbol)
                if sig:
                    key = f"{symbol}_{sig['direction']}_{sig['entry']}"
                    if key not in seen:
                        seen.add(key)
                        asyncio.run(send_signal(bot, sig))
                        logger.info(f"Signal → {symbol} {sig['direction']} @ {sig['entry']}")
                        time.sleep(1.5)

            logger.info(f"Cycle done. Sleeping {SCAN_INTERVAL}s")
            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
