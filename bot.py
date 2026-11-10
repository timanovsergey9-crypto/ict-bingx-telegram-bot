#!/usr/bin/env python3
"""
ICT / Smart Money бот для BingX (топ ликвидных пар) -> сигналы в Telegram.
Улучшенная версия (pro-trader edition).

Сигнал выдаётся только если ВСЕ условия выполнены (на ЗАКРЫТЫХ свечах):
  1. Kill Zone (London / New York, NY time + DST)
  2. HTF bias по структуре (HH+HL / LH+LL)
  3. Premium / Discount относительно EQ дилинг-рейнджа HTF
  4. Liquidity Sweep (swing / PDH / PDL) + возврат
  5. MSS с displacement
  6. Unmitigated FVG от displacement
  7. Вход по CE, стоп за экстремумом свипа, RR >= MIN_RR
  8. Фильтр BTC
  9. НОВОЕ: подтверждение объёмом (приток / отток / дивергенция)
 10. НОВОЕ: учёт исторических хаёв/лоёв (equal highs/lows, session levels)
 11. Прозрачная статистика + quality score + примерный тех. профит

Не является финансовой рекомендацией.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# ============================== CONFIG ==============================


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HTF = os.getenv("HTF", "4h")
LTF = os.getenv("LTF", "15m")
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap")  # swap = фьючерсы, spot = только long
ALLOW_SHORT = MARKET_TYPE != "spot"
TOP_N = _i("TOP_N", 150)                              # сколько пар сканировать (0 = все выше MIN_QUOTE_VOLUME)
MIN_QUOTE_VOLUME = _f("MIN_QUOTE_VOLUME", 2_000_000)  # мин. 24ч объём USDT
MIN_RR = _f("MIN_RR", 2.0)
MAX_SIGNALS_PER_DAY = _i("MAX_SIGNALS_PER_DAY", 5)
COOLDOWN_H = _f("COOLDOWN_H", 4)
BTC_FILTER = os.getenv("BTC_FILTER", "1") == "1"
ENABLED_KZ = [z.strip() for z in os.getenv("KILL_ZONES", "London,New York").split(",") if z.strip()]
SCAN_CONCURRENCY = _i("SCAN_CONCURRENCY", 6)          # параллельных запросов при скане всего рынка

# ICT параметры
SWING_N = _i("SWING_N", 3)
HTF_SWING = _i("HTF_SWING", 3)
SWEEP_LOOKBACK = _i("SWEEP_LOOKBACK", 12)
MSS_WINDOW = _i("MSS_WINDOW", 6)
MAX_FVG_AGE = _i("MAX_FVG_AGE", 8)
REF_LOOKBACK = _i("REF_LOOKBACK", 60)
LEVEL_LOOKBACK = _i("LEVEL_LOOKBACK", 150)
DISP_ATR = _f("DISP_ATR", 1.5)
DISP_BODY_RATIO = _f("DISP_BODY_RATIO", 0.55)
FVG_MIN_ATR = _f("FVG_MIN_ATR", 0.3)
STOP_BUFFER_ATR = _f("STOP_BUFFER_ATR", 0.15)
MIN_RISK_ATR = _f("MIN_RISK_ATR", 0.5)
MAX_RISK_ATR = _f("MAX_RISK_ATR", 5.0)

# Volume / Smart Money filters
VOL_SMA_PERIOD = _i("VOL_SMA_PERIOD", 20)
MIN_VOL_RATIO_MSS = _f("MIN_VOL_RATIO_MSS", 1.4)
MIN_VOL_RATIO_SWEEP = _f("MIN_VOL_RATIO_SWEEP", 1.2)
ALLOW_LOW_VOL_PRICE_UP = os.getenv("ALLOW_LOW_VOL_PRICE_UP", "0") == "1"
LOW_VOL_RATIO = _f("LOW_VOL_RATIO", 0.7)
REQUIRE_VOLUME_CONFIRM = os.getenv("REQUIRE_VOLUME_CONFIRM", "1") == "1"

# Сопровождение сделок
SETUP_TTL_MIN = _i("SETUP_TTL_MIN", 240)
TRADE_MAX_HOURS = _i("TRADE_MAX_HOURS", 48)
MONITOR_INTERVAL = _i("MONITOR_INTERVAL", 60)
CLOSE_DELAY_S = _i("CLOSE_DELAY_S", 8)
KZ_ALERT_MIN = _i("KZ_ALERT_MIN", 15)
CHART_BARS = _i("CHART_BARS", 90)

DATA_DIR = os.getenv("DATA_DIR", "data")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ICT")

# ============================== TIME / KILL ZONES ==============================

NY = ZoneInfo("America/New_York")
MSK = timezone(timedelta(hours=3))
KZ_HOURS_NY = {"Asia": (20, 24), "London": (2, 5), "New York": (7, 10)}

_TF_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def tf_seconds(tf: str) -> int:
    return int(tf[:-1]) * _TF_UNITS[tf[-1]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fmt_msk(dt) -> str:
    return pd.Timestamp(dt).tz_convert(MSK).strftime("%H:%M МСК")


def kz_at(dt) -> Optional[str]:
    hour = pd.Timestamp(dt).tz_convert(NY).hour
    for name in ENABLED_KZ:
        rng = KZ_HOURS_NY.get(name)
        if rng and rng[0] <= hour < rng[1]:
            return name
    return None


def next_kz_start(now: datetime) -> Optional[Tuple[str, datetime]]:
    now_ny = now.astimezone(NY)
    best = None
    for name in ENABLED_KZ:
        rng = KZ_HOURS_NY.get(name)
        if not rng:
            continue
        start = now_ny.replace(hour=rng[0] % 24, minute=0, second=0, microsecond=0)
        if start <= now_ny:
            start += timedelta(days=1)
        start_utc = start.astimezone(timezone.utc)
        if best is None or start_utc < best[1]:
            best = (name, start_utc)
    return best


# ============================== INDICATORS / STRUCTURE ==============================


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["volume"].rolling(period, min_periods=max(5, period // 2)).mean()


def relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    sma = volume_sma(df, period)
    return df["volume"] / sma.replace(0, np.nan)


def approx_cvd(df: pd.DataFrame) -> pd.Series:
    """Приблизительный CVD: +volume на зелёных, -volume на красных."""
    direction = np.where(df["close"] >= df["open"], 1.0, -1.0)
    return (df["volume"] * direction).cumsum()


def swing_points(df: pd.DataFrame, left: int, right: int):
    """Подтверждённые фрактальные swing'и: [(позиция, цена)]."""
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(left, len(df) - right):
        if h[i] > h[i - left:i].max() and h[i] >= h[i + 1:i + right + 1].max():
            highs.append((i, float(h[i])))
        if l[i] < l[i - left:i].min() and l[i] <= l[i + 1:i + right + 1].min():
            lows.append((i, float(l[i])))
    return highs, lows


def equal_levels(levels: List[Tuple[int, float]], atr_val: float, tol: float = 0.15) -> List[Tuple[float, int]]:
    """Группирует близкие хаи/лои (equal highs/lows). Возвращает [(price, count)]."""
    if not levels:
        return []
    sorted_lvls = sorted(levels, key=lambda x: x[1])
    groups = []
    for p, v in sorted_lvls:
        if groups and abs(groups[-1][0] - v) <= tol * atr_val:
            groups[-1] = ((groups[-1][0] * groups[-1][1] + v) / (groups[-1][1] + 1), groups[-1][1] + 1)
        else:
            groups.append((v, 1))
    return [(price, cnt) for price, cnt in groups if cnt >= 2]


def prev_day_levels(df: pd.DataFrame) -> Optional[Tuple[float, float, int]]:
    """(PDH, PDL, позиция начала текущего дня) по календарю Нью-Йорка."""
    days = df.index.tz_convert(NY).normalize()
    today = days[-1]
    prev_mask = np.asarray(days < today)
    if not prev_mask.any():
        return None
    prev_day = days[prev_mask].max()
    sel = df[np.asarray(days == prev_day)]
    pos_today = int(np.argmax(np.asarray(days == today)))
    return float(sel["high"].max()), float(sel["low"].min()), pos_today


def session_levels(df: pd.DataFrame) -> Dict[str, float]:
    """Азиатская / Лондонская сессии (по NY)."""
    hours = df.index.tz_convert(NY).hour
    asia = df[(hours >= 20) | (hours < 2)]
    london = df[(hours >= 2) & (hours < 7)]
    out = {}
    if len(asia) > 5:
        out["asia_high"] = float(asia["high"].max())
        out["asia_low"] = float(asia["low"].min())
    if len(london) > 5:
        out["london_high"] = float(london["high"].max())
        out["london_low"] = float(london["low"].min())
    return out


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """Зеркалим цены, чтобы SHORT-логика == LONG-логика."""
    return pd.DataFrame(
        {
            "open": -df["open"],
            "high": -df["low"],
            "low": -df["high"],
            "close": -df["close"],
            "volume": df["volume"],
        },
        index=df.index,
    )


def htf_context(df: pd.DataFrame) -> Optional[Dict]:
    """Структурный bias + дилинг-рейндж + HTF-ликвидность + equal levels."""
    if len(df) < 60:
        return None
    highs, lows = swing_points(df, HTF_SWING, HTF_SWING)
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        bias = "bullish"
    elif lh and ll:
        bias = "bearish"
    else:
        bias = "neutral"
    rng_hi, rng_lo = highs[-1][1], lows[-1][1]
    if rng_hi <= rng_lo:
        rng_hi, rng_lo = float(df["high"].iloc[-60:].max()), float(df["low"].iloc[-60:].min())
    a = atr(df).iloc[-1]
    eq_highs = equal_levels(highs[-12:], a if not np.isnan(a) else 0.0)
    eq_lows = equal_levels(lows[-12:], a if not np.isnan(a) else 0.0)
    return {
        "bias": bias,
        "range_high": rng_hi,
        "range_low": rng_lo,
        "eq": (rng_hi + rng_lo) / 2,
        "htf_highs": [v for _, v in highs[-6:]],
        "htf_lows": [v for _, v in lows[-6:]],
        "equal_highs": eq_highs,
        "equal_lows": eq_lows,
    }
