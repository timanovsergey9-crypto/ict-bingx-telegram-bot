#!/usr/bin/env python3
"""
ICT / Smart Money бот для BingX -> сигналы в Telegram. Версия 3 (строгие сигналы).

Сигнал выдаётся только если ВСЕ условия выполнены (на ЗАКРЫТЫХ свечах):
  1. Kill Zone (London / New York, время Нью-Йорка + DST)
  2. HTF bias по структуре (HH+HL / LH+LL) И согласие с EMA на HTF
  3. Premium / Discount относительно EQ дилинг-рейнджа HTF
  4. Liquidity Sweep (swing / PDH / PDL) + возврат
  5. MSS с displacement
  6. Неотработанный FVG от displacement
  7. Вход по CE, стоп за экстремумом свипа, RR >= MIN_RR
  8. Фильтр BTC
  9. Подтверждение объёмом (приток / отток CVD)
 10. Quality score >= MIN_QUALITY (по умолчанию 6.5 = только A/B)

Режимы запуска:
  python bot.py              — рабочий режим (нужны TELEGRAM_TOKEN и CHAT_ID)
  python bot.py --selftest   — офлайн-проверка логики на синтетических данных
  python bot.py --scan-once  — один скан рынка BingX, результат в консоль (без Telegram)

Не является финансовой рекомендацией.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
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
TOP_N = _i("TOP_N", 100)
MIN_QUOTE_VOLUME = _f("MIN_QUOTE_VOLUME", 5_000_000)
MIN_RR = _f("MIN_RR", 2.0)
MIN_QUALITY = _f("MIN_QUALITY", 6.5)                  # порог качества сигнала (0-10)
MAX_SIGNALS_PER_DAY = _i("MAX_SIGNALS_PER_DAY", 4)     # 0 или меньше = без лимита
MAX_OPEN_TRADES = _i("MAX_OPEN_TRADES", 6)            # макс. одновременно открытых сетапов (0 = без лимита)
MAX_PER_DIRECTION = _i("MAX_PER_DIRECTION", 3)        # макс. открытых в одну сторону (альты коррелируют)
REQUEST_TIMEOUT_S = _i("REQUEST_TIMEOUT_S", 90)       # таймаут анализа одной пары
COOLDOWN_H = _f("COOLDOWN_H", 6)
BTC_FILTER = os.getenv("BTC_FILTER", "1") == "1"
ENABLED_KZ = [z.strip() for z in os.getenv("KILL_ZONES", "London,New York").split(",") if z.strip()]
SCAN_CONCURRENCY = _i("SCAN_CONCURRENCY", 6)
FORCE_KZ = os.getenv("FORCE_KZ", "0") == "1"          # только для тестов: игнорировать Kill Zone
HTF_EMA = _i("HTF_EMA", 50)                           # EMA-фильтр тренда на HTF

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
    if FORCE_KZ:
        return "TEST"
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
    groups: List[Tuple[float, int]] = []
    for _, v in sorted(levels, key=lambda x: x[1]):
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
    """Структурный bias + EMA-подтверждение + дилинг-рейндж + HTF-ликвидность."""
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

    # EMA-фильтр: структура должна совпадать с положением цены относительно EMA и её наклоном
    ema = df["close"].ewm(span=HTF_EMA, adjust=False).mean()
    close = float(df["close"].iloc[-1])
    ema_up = float(ema.iloc[-1]) > float(ema.iloc[-6])
    if bias == "bullish" and not (close > float(ema.iloc[-1]) and ema_up):
        bias = "neutral"
    elif bias == "bearish" and not (close < float(ema.iloc[-1]) and not ema_up):
        bias = "neutral"

    rng_hi, rng_lo = highs[-1][1], lows[-1][1]
    if rng_hi <= rng_lo:
        rng_hi, rng_lo = float(df["high"].iloc[-60:].max()), float(df["low"].iloc[-60:].min())
    a = atr(df).iloc[-1]
    a = 0.0 if np.isnan(a) else float(a)
    return {
        "bias": bias,
        "range_high": rng_hi,
        "range_low": rng_lo,
        "eq": (rng_hi + rng_lo) / 2,
        "htf_highs": [v for _, v in highs[-6:]],
        "htf_lows": [v for _, v in lows[-6:]],
        "equal_highs": equal_levels(highs[-12:], a),
        "equal_lows": equal_levels(lows[-12:], a),
    }


# ============================== VOLUME ANALYSIS ==============================


def volume_context(df: pd.DataFrame, k: int, m: int, last: int) -> Dict:
    """Анализ объёма вокруг свипа и MSS (в «лонговой» системе координат)."""
    rv = relative_volume(df, VOL_SMA_PERIOD).to_numpy()
    cvd = approx_cvd(df).to_numpy()

    def safe(arr, idx, default=1.0):
        if idx < 0 or idx >= len(arr) or np.isnan(arr[idx]):
            return default
        return float(arr[idx])

    sweep_rv = safe(rv, k, 1.0)
    mss_rv = safe(rv, m, 1.0)
    recent_rv = float(np.nanmean(rv[max(0, last - 5):last + 1])) if last >= 0 else 1.0
    cvd_delta = float(cvd[last] - cvd[last - 8]) if last >= 8 else 0.0
    price_chg = float(df["close"].iloc[last] - df["close"].iloc[max(0, last - 6)])
    low_vol_move = abs(price_chg) > 0 and recent_rv < LOW_VOL_RATIO

    return {
        "sweep_rv": sweep_rv,
        "mss_rv": mss_rv,
        "recent_rv": recent_rv,
        "cvd_delta": cvd_delta,      # >0 = приток в сторону сделки (после _unmirror для SHORT тоже)
        "low_vol_move": low_vol_move,
        "price_chg_6": price_chg,
        "vol_ok_sweep": sweep_rv >= MIN_VOL_RATIO_SWEEP,
        "vol_ok_mss": mss_rv >= MIN_VOL_RATIO_MSS,
    }


def quality_score(sig: Dict, vol: Dict) -> float:
    """0–10. cvd_delta > 0 означает давление В СТОРОНУ сделки (и для LONG, и для SHORT)."""
    score = 5.0
    if sig["rr"] >= 3.0:
        score += 1.5
    elif sig["rr"] >= 2.5:
        score += 1.0
    elif sig["rr"] >= 2.0:
        score += 0.5
    if vol.get("vol_ok_mss"):
        score += 1.0
    if vol.get("vol_ok_sweep"):
        score += 0.7
    if vol.get("mss_rv", 0) >= 2.0:
        score += 0.5
    if vol.get("cvd_delta", 0) > 0:
        score += 0.8
    if vol.get("low_vol_move"):
        score -= 1.2
    if sig.get("fvg_age", 99) <= 3:
        score += 0.5
    if "EQ" in str(sig.get("tp1_label", "")):   # цель — equal highs/lows (сильная ликвидность)
        score += 0.4
    return max(0.0, min(10.0, score))


# ============================== ICT CORE ==============================


def _core_long(
    df: pd.DataFrame,
    sweep_levels: List[Tuple[int, float, str]],
    target_levels: List[Tuple[float, str]],
    sweep_time_ok: Optional[Callable] = None,
) -> Optional[Dict]:
    """Sweep sell-side -> MSS(displacement) -> bullish FVG -> лимитный вход в CE + volume filter."""
    n = len(df)
    if n < 60:
        return None
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    a = atr(df).to_numpy()
    last = n - 1
    if np.isnan(a[last]):
        return None
    highs, _ = swing_points(df, SWING_N, SWING_N)

    for k in range(last, max(last - SWEEP_LOOKBACK, 30) - 1, -1):
        if np.isnan(a[k]):
            continue
        if sweep_time_ok and not sweep_time_ok(df.index[k]):
            continue

        # 1) Sweep: прокол уровня тенью и возврат закрытием выше него
        swept = []
        for p, lvl, lab in sweep_levels:
            if p >= k or not (l[k] < lvl < c[k]):
                continue
            between = l[p + 1:k]
            if between.size and between.min() < lvl:
                continue
            swept.append((p, lvl, lab))
        if not swept:
            continue
        sp, slvl, slab = min(swept, key=lambda s: s[1])

        # 2) MSS: закрытие выше последнего внутреннего swing high до свипа
        refs = [(hp, hv) for hp, hv in highs if k - REF_LOOKBACK <= hp < k and hv > slvl]
        if not refs:
            continue
        rp, rv = refs[-1]
        m = next((j for j in range(k, min(k + MSS_WINDOW, last) + 1) if c[j] > rv), None)
        if m is None:
            continue
        body, rng = c[m] - o[m], h[m] - l[m]
        if body < DISP_ATR * a[m] or rng <= 0 or body / rng < DISP_BODY_RATIO:
            continue

        extreme = float(l[k:m + 1].min())

        # 3) FVG, созданный displacement-движением
        fvgs = []
        for i in range(max(k + 1, 2), last + 1):
            mid = i - 1
            if mid < k or mid > m or c[mid] <= o[mid]:
                continue
            bottom, top = float(h[i - 2]), float(l[i])
            if top <= bottom or (top - bottom) < FVG_MIN_ATR * a[mid]:
                continue
            ce = (top + bottom) / 2
            after = l[i + 1:]
            if after.size and after.min() <= ce:
                continue
            if last - i > MAX_FVG_AGE:
                continue
            fvgs.append({"i": i, "bottom": bottom, "top": top, "ce": ce, "size": top - bottom})
        if not fvgs:
            continue
        fvg = max(fvgs, key=lambda f: (f["size"], f["i"]))

        # 4) Volume filter
        vol = volume_context(df, k, m, last)
        if REQUIRE_VOLUME_CONFIRM:
            if not vol["vol_ok_mss"] and not vol["vol_ok_sweep"]:
                continue
            if not ALLOW_LOW_VOL_PRICE_UP and vol["low_vol_move"] and vol["price_chg_6"] > 0:
                continue

        # 5) Стоп и цели
        entry = fvg["ce"]
        stop = extreme - STOP_BUFFER_ATR * a[last]
        risk = entry - stop
        if risk < MIN_RISK_ATR * a[last] or risk > MAX_RISK_ATR * a[last]:
            continue

        high_since = float(h[k:].max())
        cands = sorted((t for t in target_levels if t[0] > max(high_since, entry)), key=lambda t: t[0])
        if not cands:
            continue
        tp1, tp1_lab = cands[0]
        rr = (tp1 - entry) / risk
        if rr < MIN_RR:
            continue
        tp2 = next(((p2, l2) for p2, l2 in cands[1:] if p2 > tp1 + 0.5 * a[last]), None)

        return {
            "k": k, "m": m, "ref_pos": rp, "ref": float(rv),
            "level_pos": sp, "level": float(slvl), "level_label": slab,
            "extreme": extreme,
            "fvg_i": fvg["i"], "fvg_bottom": fvg["bottom"], "fvg_top": fvg["top"],
            "fvg_age": last - fvg["i"],
            "entry": float(entry), "stop": float(stop),
            "tp1": float(tp1), "tp1_label": tp1_lab,
            "tp2": float(tp2[0]) if tp2 else None, "tp2_label": tp2[1] if tp2 else None,
            "rr": float(rr), "atr": float(a[last]), "sweep_ts": df.index[k],
            "vol": vol,
        }
    return None


def _unmirror(r: Dict) -> Dict:
    out = dict(r)
    for key in ("ref", "level", "extreme", "entry", "stop", "tp1"):
        out[key] = -r[key]
    out["tp2"] = -r["tp2"] if r["tp2"] is not None else None
    out["fvg_bottom"], out["fvg_top"] = -r["fvg_top"], -r["fvg_bottom"]
    # ИСПРАВЛЕНО: в зеркальном df «зелёные» свечи = исходные красные. cvd_delta в зеркале > 0
    # означает давление ПРОДАВЦОВ, т.е. в сторону SHORT — это и есть «в сторону сделки».
    # Поэтому знак cvd_delta оставляем (он уже «в сторону сделки»), а price_chg_6 разворачиваем
    # обратно в реальные цены, чтобы текст сигнала не врал.
    vol = dict(r.get("vol", {}))
    if "price_chg_6" in vol:
        vol["price_chg_6"] = -vol["price_chg_6"]
    out["vol"] = vol
    return out


def find_setup(df: pd.DataFrame, side: str, ctx: Dict, sweep_time_ok: Optional[Callable] = None) -> Optional[Dict]:
    highs, lows = swing_points(df, SWING_N, SWING_N)
    lb = len(df) - LEVEL_LOOKBACK
    pd_levels = prev_day_levels(df)
    sess = session_levels(df)

    if side == "long":
        sweeps = [(p, v, "swing low") for p, v in lows if p >= lb]
        targets = [(v, "swing high") for p, v in highs if p >= lb]
        targets += [(v, "HTF high") for v in ctx["htf_highs"]]
        for price, cnt in ctx.get("equal_highs", []):
            targets.append((price, f"EQ high x{cnt}"))
        if pd_levels:
            pdh, pdl, day_pos = pd_levels
            sweeps.append((day_pos, pdl, "PDL"))
            targets.append((pdh, "PDH"))
        if "asia_high" in sess:
            targets.append((sess["asia_high"], "Asia high"))
        if "london_high" in sess:
            targets.append((sess["london_high"], "London high"))
        return _core_long(df, sweeps, targets, sweep_time_ok)

    sweeps = [(p, -v, "swing high") for p, v in highs if p >= lb]
    targets = [(-v, "swing low") for p, v in lows if p >= lb]
    targets += [(-v, "HTF low") for v in ctx["htf_lows"]]
    for price, cnt in ctx.get("equal_lows", []):
        targets.append((-price, f"EQ low x{cnt}"))
    if pd_levels:
        pdh, pdl, day_pos = pd_levels
        sweeps.append((day_pos, -pdh, "PDH"))
        targets.append((-pdl, "PDL"))
    if "asia_low" in sess:
        targets.append((-sess["asia_low"], "Asia low"))
    if "london_low" in sess:
        targets.append((-sess["london_low"], "London low"))
    res = _core_long(mirror(df), sweeps, targets, sweep_time_ok)
    return _unmirror(res) if res else None


def analyze_frames(
    symbol: str,
    df_htf: pd.DataFrame,
    df_ltf: pd.DataFrame,
    now: datetime,
    btc_bias: Optional[str] = None,
    is_btc: bool = False,
) -> Optional[Dict]:
    """Чистая функция: все фильтры подряд. Только закрытые свечи."""
    kz = kz_at(now)
    if not kz or len(df_htf) < 60 or len(df_ltf) < 120:
        return None

    ctx = htf_context(df_htf)
    if not ctx or ctx["bias"] == "neutral":
        return None
    side = "long" if ctx["bias"] == "bullish" else "short"
    if side == "short" and not ALLOW_SHORT:
        return None
    if BTC_FILTER and not is_btc and btc_bias in ("bullish", "bearish") and btc_bias != ctx["bias"]:
        return None

    s = find_setup(df_ltf, side, ctx, lambda ts: kz_at(ts) is not None)
    if not s:
        return None

    # Premium / Discount
    if side == "long" and s["entry"] >= ctx["eq"]:
        return None
    if side == "short" and s["entry"] <= ctx["eq"]:
        return None

    view = df_ltf.iloc[-CHART_BARS:]
    direction = side.upper()
    vol = s.get("vol", {})
    q = quality_score({**s, "direction": direction}, vol)
    if q < MIN_QUALITY:
        return None

    return {
        **s,
        "symbol": symbol,
        "direction": direction,
        "kill_zone": kz,
        "htf_bias": ctx["bias"],
        "eq": ctx["eq"],
        "range_high": ctx["range_high"],
        "range_low": ctx["range_low"],
        "zone": "discount" if side == "long" else "premium",
        "key": f"{symbol}|{direction}|{s['sweep_ts'].isoformat()}",
        "df": view,
        "offset": len(df_ltf) - len(view),
        "quality": q,
        "vol_info": vol,
    }


# ============================== EXCHANGE ==============================

STABLES = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "BUSD", "USDP", "PYUSD", "USD1"}


def make_exchange():
    import ccxt

    return ccxt.bingx({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": MARKET_TYPE}})


def drop_unclosed(df: pd.DataFrame, tf: str, now: Optional[datetime] = None) -> pd.DataFrame:
    if df.empty:
        return df
    now = now or utcnow()
    if df.index[-1] + pd.Timedelta(seconds=tf_seconds(tf)) > now:
        return df.iloc[:-1]
    return df


def with_retry(fn: Callable, *args, attempts: int = 3, **kwargs):
    """Повторяет сетевой вызов с паузой 1с, 2с, ... (лимиты биржи, обрывы связи)."""
    import time

    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)


def fetch_frame(ex, symbol: str, tf: str, limit: int) -> pd.DataFrame:
    raw = with_retry(ex.fetch_ohlcv, symbol, tf, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return drop_unclosed(df, tf)


def fetch_top_symbols(ex) -> List[str]:
    """Ликвидные USDT-пары BingX (топ TOP_N по 24ч объёму; TOP_N=0 → все выше порога)."""
    fallback = (
        ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        if MARKET_TYPE != "spot"
        else ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    )
    try:
        try:
            ex.load_markets()
        except Exception:  # noqa: BLE001
            pass
        rows = []
        for sym, t in ex.fetch_tickers().items():
            if MARKET_TYPE == "spot":
                if not sym.endswith("/USDT") or ":" in sym:
                    continue
            elif not sym.endswith("/USDT:USDT"):
                continue
            base = sym.split("/")[0].split(":")[0]
            if base in STABLES or re.search(r"\d+[LS]$", base):
                continue
            vol = float(t.get("quoteVolume") or 0)
            if vol >= MIN_QUOTE_VOLUME:
                rows.append((sym, vol))
        rows.sort(key=lambda r: r[1], reverse=True)
        selected = [s for s, _ in (rows[:TOP_N] if TOP_N > 0 else rows)]
        logger.info("Ликвидных пар: %d (порог %.0f USDT), сканирую %d", len(rows), MIN_QUOTE_VOLUME, len(selected))
        return selected or fallback
    except Exception as e:  # noqa: BLE001
        logger.error("fetch_top_symbols: %s", e)
        return fallback


def btc_symbol() -> str:
    return "BTC/USDT" if MARKET_TYPE == "spot" else "BTC/USDT:USDT"


def get_btc_bias(ex) -> Optional[str]:
    try:
        ctx = htf_context(fetch_frame(ex, btc_symbol(), HTF, 250))
        return ctx["bias"] if ctx else None
    except Exception as e:  # noqa: BLE001
        logger.warning("btc bias: %s", e)
        return None


def analyze_symbol(ex, symbol: str, btc_bias: Optional[str]) -> Optional[Dict]:
    df_htf = fetch_frame(ex, symbol, HTF, 250)
    df_ltf = fetch_frame(ex, symbol, LTF, 300)
    return analyze_frames(symbol, df_htf, df_ltf, utcnow(), btc_bias, is_btc=(symbol == btc_symbol()))


# ============================== TRADES ==============================


class TradeStore:
    def __init__(self, path: str):
        self.path = path
        self.trades: List[Dict] = []

    def load(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.trades = json.load(f)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.trades, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, trade: Dict) -> None:
        self.trades.append(trade)
        self.save()

    def signals_today(self, now: datetime) -> int:
        day = now.date()
        return sum(1 for t in self.trades if datetime.fromtimestamp(t["created_ms"] / 1000, timezone.utc).date() == day)

    def blocked(self, sig: Dict, now: datetime) -> bool:
        for t in self.trades:
            if t["key"] == sig["key"]:
                return True
            if t["symbol"] == sig["symbol"]:
                if t["status"] in ("pending", "active"):
                    return True
                if ms(now) - t["created_ms"] < COOLDOWN_H * 3600 * 1000:
                    return True
        return False

    def open_blocked(self, direction: str) -> bool:
        """Достигнут ли лимит одновременно открытых сетапов (общий или по направлению)."""
        open_ = [t for t in self.trades if t["status"] in ("pending", "active")]
        if MAX_OPEN_TRADES > 0 and len(open_) >= MAX_OPEN_TRADES:
            return True
        same = sum(1 for t in open_ if t["direction"] == direction)
        return MAX_PER_DIRECTION > 0 and same >= MAX_PER_DIRECTION

    def daily_limit_hit(self, now: datetime) -> bool:
        return MAX_SIGNALS_PER_DAY > 0 and self.signals_today(now) >= MAX_SIGNALS_PER_DAY

    def grade_winrate(self, grade: str) -> Optional[Tuple[int, int]]:
        """(wins, decided) по закрытым сделкам данного грейда."""
        ts = [t for t in self.trades if t.get("grade") == grade and t["status"] in ("win", "loss")]
        if not ts:
            return None
        return sum(1 for t in ts if t["status"] == "win"), len(ts)


def grade_of(q: float) -> str:
    return "A" if q >= 8.0 else "B" if q >= 6.5 else "C"


def make_trade(sig: Dict, now: datetime) -> Dict:
    q = sig.get("quality", 0)
    return {
        "id": uuid.uuid4().hex[:8],
        "key": sig["key"],
        "symbol": sig["symbol"],
        "direction": sig["direction"],
        "entry": sig["entry"],
        "stop": sig["stop"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"],
        "rr": round(sig["rr"], 2),
        "quality": round(q, 1),
        "grade": grade_of(q),
        "status": "pending",
        "created_ms": ms(now),
        "expires_ms": ms(now) + SETUP_TTL_MIN * 60_000,
        "checked_ms": ms(now),
        "filled_ms": None,
        "closed_ms": None,
        "r": None,
    }


def evaluate_trade(t: Dict, candles: List[Tuple[int, float, float, float, float]]) -> List[str]:
    """Прогоняет закрытые 1m-свечи по сделке. Меняет t, возвращает события.
    Консервативно: если в одной свече и стоп, и цель — считаем стоп."""
    events: List[str] = []
    s = 1 if t["direction"] == "LONG" else -1
    entry, stop, tp = s * t["entry"], s * t["stop"], s * t["tp1"]
    risk = entry - stop

    def close(status: str, r: float, ts: int) -> None:
        t["status"], t["r"], t["closed_ms"] = status, round(r, 2), ts
        events.append(status)

    for ts, _o, h, l, c in candles:
        if ts <= t["checked_ms"]:
            continue
        t["checked_ms"] = ts
        lo, hi, cl = (l, h, c) if s == 1 else (-h, -l, -c)

        if t["status"] == "pending":
            if ts >= t["expires_ms"]:
                close("expired", 0.0, ts)
                break
            if lo <= entry:
                t["status"], t["filled_ms"] = "active", ts
                events.append("filled")
                if lo <= stop:
                    close("loss", -1.0, ts)
                    break
                if cl >= tp:
                    close("win", (tp - entry) / risk, ts)
                    break
            elif hi >= tp:
                close("missed", 0.0, ts)
                break
        elif t["status"] == "active":
            if lo <= stop:
                close("loss", -1.0, ts)
                break
            if hi >= tp:
                close("win", (tp - entry) / risk, ts)
                break
            if ts - (t["filled_ms"] or ts) > TRADE_MAX_HOURS * 3_600_000:
                close("timeout", (cl - entry) / risk, ts)
                break
    return events


def stats_text(store: TradeStore) -> str:
    ts = store.trades

    def cnt(*st: str) -> int:
        return sum(1 for t in ts if t["status"] in st)

    wins, losses, timeouts = cnt("win"), cnt("loss"), cnt("timeout")
    total_r = sum(t["r"] for t in ts if t.get("r") is not None)
    decided = wins + losses
    wr = f"{wins / decided * 100:.0f}%" if decided else "—"
    avg_q = np.mean([t.get("quality", 0) for t in ts]) if ts else 0

    week_ago = ms(utcnow()) - 7 * 86400 * 1000
    week = [t for t in ts if t["created_ms"] >= week_ago]
    w_wins = sum(1 for t in week if t["status"] == "win")
    w_dec = w_wins + sum(1 for t in week if t["status"] == "loss")
    w_wr = f"{w_wins / w_dec * 100:.0f}%" if w_dec else "—"

    by_grade = []
    for g in ("A", "B", "C"):
        gw = store.grade_winrate(g)
        if gw:
            by_grade.append(f"{g}: {gw[0]}/{gw[1]} ({gw[0] / gw[1] * 100:.0f}%)")
    grade_line = ("\nПо грейдам: " + " | ".join(by_grade) + "\n") if by_grade else ""

    return (
        "📊 <b>Статистика сигналов</b>\n\n"
        f"<b>Всего:</b> {len(ts)} сигналов\n"
        f"Ждут входа: {cnt('pending')} | В сделке: {cnt('active')}\n"
        f"Не исполнились: {cnt('expired', 'missed')}\n\n"
        f"✅ TP: {wins} | ❌ SL: {losses} | ⏱ Таймаут: {timeouts}\n"
        f"<b>Winrate (TP/(TP+SL)): {wr}</b>\n"
        f"Итого R: <b>{total_r:+.2f}R</b>\n"
        f"Средний quality: {avg_q:.1f}/10\n"
        f"{grade_line}\n"
        f"<b>За 7 дней:</b> {len(week)} сигналов, winrate {w_wr}\n\n"
        f"<i>Считаются только реальные закрытия по TP/SL. Пока сделок мало — цифрам верить рано.</i>"
    )


# ============================== MESSAGES / CHART ==============================


def fp(p: float) -> str:
    d = 2 if p >= 1000 else 4 if p >= 1 else 5 if p >= 0.01 else 8
    return f"{p:.{d}f}"


def signal_text(sig: Dict, store: Optional[TradeStore] = None) -> str:
    long_ = sig["direction"] == "LONG"
    expire_at = utcnow() + timedelta(minutes=SETUP_TTL_MIN)
    q = sig.get("quality", 0)
    grade = grade_of(q)
    vol = sig.get("vol_info", {})

    risk_pct = abs(sig["entry"] - sig["stop"]) / sig["entry"] * 100
    profit_pct = risk_pct * sig["rr"]
    tp2 = f"🎯 TP2: <code>{fp(sig['tp2'])}</code> ({sig['tp2_label']})\n" if sig.get("tp2") else ""

    cvd = vol.get("cvd_delta", 0)
    flow = ("покупатели" if long_ else "продавцы") if cvd > 0 else ("продавцы" if long_ else "покупатели")
    flow_ok = "✅" if cvd > 0 else "⚠️"

    hist = ""
    if store:
        gw = store.grade_winrate(grade)
        if gw and gw[1] >= 10:
            hist = f"\nИстория грейда {grade}: {gw[0]}/{gw[1]} TP ({gw[0] / gw[1] * 100:.0f}%)"

    return (
        f"{'🟢' if long_ else '🔴'} <b>{sig['symbol']} — {sig['direction']}</b>  [грейд {grade}, {q:.1f}/10]\n\n"
        f"📍 Вход (лимитка): <code>{fp(sig['entry'])}</code>\n"
        f"🛑 Стоп: <code>{fp(sig['stop'])}</code> (−{risk_pct:.2f}%)\n"
        f"🎯 TP1: <code>{fp(sig['tp1'])}</code> ({sig['tp1_label']}) — RR 1:{sig['rr']:.1f}, +{profit_pct:.1f}%\n"
        f"{tp2}\n"
        f"<b>Почему сигнал:</b>\n"
        f"• {sig['kill_zone']} Kill Zone, {HTF} тренд {'вверх' if long_ else 'вниз'} (EMA{HTF_EMA} согласна)\n"
        f"• Цена в {'Discount' if long_ else 'Premium'} (EQ {fp(sig['eq'])})\n"
        f"• Снят {sig['level_label']} {fp(sig['level'])} → MSS {'выше' if long_ else 'ниже'} {fp(sig['ref'])}\n"
        f"• FVG {LTF}: {fp(sig['fvg_bottom'])}–{fp(sig['fvg_top'])}\n"
        f"• Объём: MSS {vol.get('mss_rv', 0):.1f}x, свип {vol.get('sweep_rv', 0):.1f}x; {flow_ok} давят {flow}\n\n"
        f"❌ <b>Отмена:</b> цена дошла до TP1 без касания входа, или нет входа до {fmt_msk(expire_at)}\n"
        f"{hist}\n"
        f"<i>⚠️ Это не гарантия и не финансовый совет. Вероятность оценивается по статистике бота (/stats). Риск ≤1–2% депозита.</i>"
    )


def render_chart(sig: Dict) -> BytesIO:
    df, off = sig["df"], sig["offset"]
    n = len(df)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    tiny = (h.max() - l.min()) * 0.0015
    x_end = n + 10

    def px(pos: int) -> int:
        return min(max(pos - off, 0), n - 1)

    fig, ax = plt.subplots(figsize=(10, 6), dpi=110)
    for x in range(n):
        color = "#26a69a" if c[x] >= o[x] else "#ef5350"
        ax.plot([x, x], [l[x], h[x]], color=color, linewidth=0.8)
        ax.add_patch(Rectangle((x - 0.3, min(o[x], c[x])), 0.6, abs(c[x] - o[x]) or tiny, facecolor=color, edgecolor=color))

    fvg_x = px(sig.get("fvg_i", n - 5))
    ax.axhspan(sig["fvg_bottom"], sig["fvg_top"], xmin=fvg_x / x_end, xmax=1.0, alpha=0.15, color="#42a5f5")
    ax.axhline(sig["entry"], color="#42a5f5", linestyle="--", linewidth=1.2, label="Entry (CE)")
    ax.axhline(sig["stop"], color="#ef5350", linestyle="--", linewidth=1.0, label="Stop")
    ax.axhline(sig["tp1"], color="#26a69a", linestyle="--", linewidth=1.0, label="TP1")
    if sig.get("tp2"):
        ax.axhline(sig["tp2"], color="#66bb6a", linestyle=":", linewidth=0.9, label="TP2")
    ax.axhline(sig["level"], color="#ffa726", linestyle=":", linewidth=0.9, label=f"Swept {sig['level_label']}")

    ax.set_title(f"{sig['symbol']} {sig['direction']} | Q={sig.get('quality', 0):.1f} | RR 1:{sig['rr']:.1f}")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ============================== TELEGRAM HANDLERS ==============================


async def cmd_help(update, context):
    await update.message.reply_text(
        "ICT Smart Money бот v3\n\n"
        "/stats — статистика сигналов и R\n"
        "/open — текущие pending/active сетапы\n"
        "/status — состояние бота и ближайшая Kill Zone\n\n"
        "Сигнал приходит только при полном совпадении: Kill Zone + тренд HTF (структура+EMA) + "
        f"Sweep + MSS + FVG + объём + Premium/Discount + quality ≥ {MIN_QUALITY}."
    )


async def cmd_stats(update, context):
    store = context.application.bot_data["store"]
    await update.message.reply_text(stats_text(store), parse_mode="HTML")


async def cmd_open(update, context):
    store = context.application.bot_data["store"]
    open_ = [t for t in store.trades if t["status"] in ("pending", "active")]
    if not open_:
        await update.message.reply_text("Нет открытых сетапов.")
        return
    lines = [
        f"{t['symbol']} {t['direction']} | {t['status']} | E {fp(t['entry'])} SL {fp(t['stop'])} TP {fp(t['tp1'])} | Q={t.get('quality', '?')}"
        for t in open_
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update, context):
    store = context.application.bot_data["store"]
    now = utcnow()
    kz = kz_at(now)
    nxt = next_kz_start(now)
    txt = f"✅ Бот работает\nСейчас: {fmt_msk(now)}\n"
    txt += f"Kill Zone: {kz}\n" if kz else "Сейчас вне Kill Zone — скан приостановлен\n"
    if nxt and not kz:
        txt += f"Следующая: {nxt[0]} в {fmt_msk(nxt[1])}\n"
    limit = MAX_SIGNALS_PER_DAY if MAX_SIGNALS_PER_DAY > 0 else "∞"
    opened = sum(1 for t in store.trades if t["status"] in ("pending", "active"))
    txt += f"Сигналов сегодня: {store.signals_today(now)}/{limit}\nОткрытых сетапов: {opened}\nПороги: RR≥{MIN_RR}, quality≥{MIN_QUALITY}"
    await update.message.reply_text(txt)


async def send_signal(bot, sig: Dict, store: Optional[TradeStore] = None) -> None:
    text = signal_text(sig, store)
    try:
        chart = render_chart(sig)
        # лимит подписи к фото в Telegram — 1024 символа
        if len(text) <= 1024:
            await bot.send_photo(chat_id=CHAT_ID, photo=chart, caption=text, parse_mode="HTML")
        else:
            await bot.send_photo(chat_id=CHAT_ID, photo=chart)
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        logger.exception("send_signal: фото не ушло, шлю текстом")
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")


# ============================== LOOPS ==============================


async def scan_market(ex) -> List[Dict]:
    symbols = await asyncio.to_thread(fetch_top_symbols, ex)
    btc_bias = await asyncio.to_thread(get_btc_bias, ex)
    logger.info("Скан: %d пар, BTC bias=%s", len(symbols), btc_bias)
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _one(sym: str) -> Optional[Dict]:
        async with sem:
            try:
                return await asyncio.wait_for(asyncio.to_thread(analyze_symbol, ex, sym, btc_bias), REQUEST_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("%s: таймаут анализа", sym)
                return None
            except Exception as e:  # noqa: BLE001
                logger.warning("%s: %s", sym, e)
                return None

    found = [s for s in await asyncio.gather(*[_one(s) for s in symbols]) if s]
    found.sort(key=lambda s: s.get("quality", 0), reverse=True)
    return found


async def scanner_loop(bot, ex, store: TradeStore) -> None:
    while True:
        try:
            now = utcnow()
            tf_sec = tf_seconds(LTF)
            await asyncio.sleep(max(5, tf_sec - (int(now.timestamp()) % tf_sec) + CLOSE_DELAY_S))

            if not kz_at(utcnow()):
                continue  # вне Kill Zone сигналов быть не может — экономим запросы к бирже
            if store.daily_limit_hit(utcnow()):
                logger.info("Достигнут дневной лимит сигналов")
                continue

            for sig in await scan_market(ex):
                if store.daily_limit_hit(utcnow()):
                    break
                if store.blocked(sig, utcnow()):
                    continue
                if store.open_blocked(sig["direction"]):
                    logger.info("Пропуск %s %s: лимит открытых сетапов", sig["symbol"], sig["direction"])
                    continue
                await send_signal(bot, sig, store)
                store.add(make_trade(sig, utcnow()))
                logger.info("Сигнал: %s %s Q=%.1f RR=%.1f", sig["symbol"], sig["direction"], sig["quality"], sig["rr"])
        except Exception:  # noqa: BLE001
            logger.exception("scanner")


EVENT_TEXT = {
    "filled": "🟢 {s} {d}: цена коснулась входа — лимитка исполнена",
    "win": "✅ {s} {d}: TP1 достигнут ({r:+.2f}R)",
    "loss": "❌ {s} {d}: стоп ({r:+.2f}R)",
    "timeout": "⏱ {s} {d}: закрыто по таймауту ({r:+.2f}R)",
    "expired": "⌛ {s} {d}: вход так и не сработал, сетап снят",
    "missed": "🚫 {s} {d}: цена ушла к цели без входа, сетап отменён",
}


async def monitor_trades(bot, ex, store: TradeStore) -> None:
    for t in [x for x in store.trades if x["status"] in ("pending", "active")]:
        try:
            raw = await asyncio.to_thread(with_retry, ex.fetch_ohlcv, t["symbol"], "1m", t["checked_ms"], 1000)
            now_ms = ms(utcnow())
            candles = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in raw if r[0] + 60_000 <= now_ms]
            events = evaluate_trade(t, candles)
            store.save()
            for ev in events:
                await bot.send_message(chat_id=CHAT_ID, text=EVENT_TEXT[ev].format(s=t["symbol"], d=t["direction"], r=t["r"] or 0.0))
        except Exception as e:  # noqa: BLE001
            logger.warning("monitor %s: %s", t["symbol"], e)


async def monitor_loop(bot, ex, store: TradeStore) -> None:
    last_alert = None
    while True:
        try:
            nxt = next_kz_start(utcnow())
            if nxt:
                name, start = nxt
                minutes = (start - utcnow()).total_seconds() / 60
                alert_key = f"{name}-{start.isoformat()}"
                if 0 < minutes <= KZ_ALERT_MIN and alert_key != last_alert:
                    last_alert = alert_key
                    await bot.send_message(chat_id=CHAT_ID, text=f"⏰ Kill Zone «{name}» откроется через ~{int(minutes)} мин ({fmt_msk(start)})")
            await monitor_trades(bot, ex, store)
        except Exception:  # noqa: BLE001
            logger.exception("monitor")
        await asyncio.sleep(MONITOR_INTERVAL)


async def run_bot() -> None:
    from telegram.ext import Application, CommandHandler

    ex = make_exchange()
    store = TradeStore(TRADES_FILE)
    store.load()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.bot_data["store"] = store
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("status", cmd_status))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("ICT bot v3 запущен: %s, HTF %s / LTF %s, quality>=%s", MARKET_TYPE, HTF, LTF, MIN_QUALITY)
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text="✅ ICT бот v3 запущен. Команды: /status /stats /open", disable_notification=True)
    except Exception:  # noqa: BLE001
        logger.exception("не удалось отправить стартовое сообщение — проверьте CHAT_ID и что вы нажали /start у бота")
    try:
        await asyncio.gather(scanner_loop(app.bot, ex, store), monitor_loop(app.bot, ex, store))
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


# ============================== TEST MODES ==============================


def selftest() -> int:
    """Офлайн-проверка логики (без сети, без Telegram)."""
    global FORCE_KZ
    FORCE_KZ = True
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    # 1) зеркало и cvd: для SHORT приток «в сторону сделки» должен быть > 0 при падении цены
    idx = pd.date_range("2026-01-01", periods=40, freq="15min", tz="UTC")
    px = np.linspace(100, 80, 40)
    down = pd.DataFrame({"open": px + 0.3, "high": px + 0.5, "low": px - 0.5, "close": px, "volume": 10.0}, index=idx)
    vm = volume_context(mirror(down), 20, 25, 39)
    check("SHORT: cvd_delta в зеркале > 0 при падении (давят продавцы)", vm["cvd_delta"] > 0)

    # 2) evaluate_trade: LONG win / loss, SHORT win
    def mk(direction, entry, stop, tp):
        return {"direction": direction, "entry": entry, "stop": stop, "tp1": tp, "status": "pending",
                "created_ms": 0, "expires_ms": 10**12, "checked_ms": 0, "filled_ms": None, "closed_ms": None, "r": None}

    t = mk("LONG", 100, 98, 104)
    evaluate_trade(t, [(60_000, 101, 102, 100.5, 101), (120_000, 100.5, 101, 99.9, 100.2), (180_000, 100, 105, 100, 104.5)])
    check("LONG: вход затем TP = win +2R", t["status"] == "win" and abs(t["r"] - 2.0) < 1e-6)
    t = mk("LONG", 100, 98, 104)
    evaluate_trade(t, [(60_000, 100, 100.5, 99.5, 100), (120_000, 100, 100, 97.9, 98)])
    check("LONG: вход затем SL = loss -1R", t["status"] == "loss" and t["r"] == -1.0)
    t = mk("SHORT", 100, 102, 96)
    evaluate_trade(t, [(60_000, 99, 100.2, 98.5, 99), (120_000, 99, 99, 95.5, 96)])
    check("SHORT: вход затем TP = win +2R", t["status"] == "win" and abs(t["r"] - 2.0) < 1e-6)

    # 2b) лимиты: открытых сетапов и по направлению; 0 = без дневного лимита
    global MAX_SIGNALS_PER_DAY
    st = TradeStore("/tmp/_selftest_trades.json")
    st.trades = [{"direction": "LONG", "status": "pending", "created_ms": ms(utcnow()), "key": "k", "symbol": "A"} for _ in range(MAX_PER_DIRECTION)]
    check("лимит по направлению: LONG заблокирован, SHORT свободен", st.open_blocked("LONG") and not st.open_blocked("SHORT"))
    saved, MAX_SIGNALS_PER_DAY = MAX_SIGNALS_PER_DAY, 0
    check("MAX_SIGNALS_PER_DAY=0 означает без лимита", not st.daily_limit_hit(utcnow()))
    MAX_SIGNALS_PER_DAY = saved

    # 2c) with_retry: повторяет и в итоге возвращает результат
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("temp")
        return "ok"

    check("with_retry переживает временную ошибку", with_retry(flaky) == "ok" and calls["n"] == 2)

    # 3) прогон анализатора на случайных данных: нет исключений, сигнал (если есть) валиден
    n_sig = 0
    for seed in range(40):
        rng = np.random.default_rng(seed)

        def series(n, freq, step):
            r = rng.normal(0, step, n).cumsum() + 100
            ix = pd.date_range("2026-03-01", periods=n, freq=freq, tz="UTC")
            hi = r + np.abs(rng.normal(0, step, n))
            lo = r - np.abs(rng.normal(0, step, n))
            op = np.r_[r[0], r[:-1]]
            return pd.DataFrame({"open": op, "high": np.maximum.reduce([hi, op, r]), "low": np.minimum.reduce([lo, op, r]),
                                 "close": r, "volume": rng.uniform(50, 150, n) * (1 + 3 * (rng.random(n) > 0.9))}, index=ix)

        htf, ltf = series(250, "4h", 1.2), series(300, "15min", 0.35)
        sig = analyze_frames("TEST/USDT", htf, ltf, ltf.index[-1].to_pydatetime(), None, True)
        if sig:
            n_sig += 1
            long_ = sig["direction"] == "LONG"
            good = (sig["stop"] < sig["entry"] < sig["tp1"]) if long_ else (sig["stop"] > sig["entry"] > sig["tp1"])
            check(f"seed {seed}: геометрия сигнала корректна ({sig['direction']})", good)
            check(f"seed {seed}: quality >= MIN_QUALITY и RR >= MIN_RR", sig["quality"] >= MIN_QUALITY and sig["rr"] >= MIN_RR)
            txt = signal_text(sig)
            check(f"seed {seed}: текст и график строятся", len(txt) > 100 and render_chart(sig).getbuffer().nbytes > 1000)
    print(f"Случайных рядов проанализировано: 40, сигналов: {n_sig} (на шуме их и должно быть мало)")
    print("ИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
    return 0 if ok else 1


async def scan_once() -> None:
    """Один скан живого рынка BingX; печатает сигналы в консоль, Telegram не нужен."""
    ex = make_exchange()
    found = await scan_market(ex)
    print(f"\nKill Zone сейчас: {kz_at(utcnow()) or 'нет (для теста задайте FORCE_KZ=1)'}")
    print(f"Найдено сигналов: {len(found)}")
    for s in found[:10]:
        print(re.sub(r"<[^>]+>", "", signal_text(s)))
        print("-" * 50)


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    if "--scan-once" in sys.argv:
        asyncio.run(scan_once())
        return
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise SystemExit("Задайте переменные окружения TELEGRAM_TOKEN и CHAT_ID")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
