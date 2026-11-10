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


# ============================== VOLUME ANALYSIS ==============================


def volume_context(df: pd.DataFrame, k: int, m: int, last: int) -> Dict:
    """Анализ объёма вокруг свипа и MSS."""
    rv = relative_volume(df, VOL_SMA_PERIOD).to_numpy()
    vol = df["volume"].to_numpy()
    cvd = approx_cvd(df).to_numpy()

    def safe(arr, idx, default=1.0):
        if idx < 0 or idx >= len(arr) or np.isnan(arr[idx]):
            return default
        return float(arr[idx])

    sweep_rv = safe(rv, k, 1.0)
    mss_rv = safe(rv, m, 1.0)
    recent_rv = float(np.nanmean(rv[max(0, last - 5):last + 1])) if last >= 0 else 1.0

    # Приток / отток: изменение CVD за последние 8 свечей
    cvd_delta = 0.0
    if last >= 8:
        cvd_delta = float(cvd[last] - cvd[last - 8])

    # Цена растёт / падает без объёма
    price_chg = float(df["close"].iloc[last] - df["close"].iloc[max(0, last - 6)])
    low_vol_move = abs(price_chg) > 0 and recent_rv < LOW_VOL_RATIO

    return {
        "sweep_rv": sweep_rv,
        "mss_rv": mss_rv,
        "recent_rv": recent_rv,
        "cvd_delta": cvd_delta,
        "low_vol_move": low_vol_move,
        "price_chg_6": price_chg,
        "vol_ok_sweep": sweep_rv >= MIN_VOL_RATIO_SWEEP,
        "vol_ok_mss": mss_rv >= MIN_VOL_RATIO_MSS,
    }


def quality_score(sig: Dict, vol: Dict) -> float:
    """0–10. Чем выше — тем качественнее сетап."""
    score = 5.0
    # RR
    if sig["rr"] >= 3.0:
        score += 1.5
    elif sig["rr"] >= 2.5:
        score += 1.0
    elif sig["rr"] >= 2.0:
        score += 0.5
    # Volume
    if vol["vol_ok_mss"]:
        score += 1.0
    if vol["vol_ok_sweep"]:
        score += 0.7
    if vol["mss_rv"] >= 2.0:
        score += 0.5
    # CVD подтверждает направление
    if sig["direction"] == "LONG" and vol["cvd_delta"] > 0:
        score += 0.8
    elif sig["direction"] == "SHORT" and vol["cvd_delta"] < 0:
        score += 0.8
    # Штраф за низкий объём при движении цены
    if vol["low_vol_move"]:
        score -= 1.2
    # Kill zone уже учтена, FVG свежий
    age = sig.get("fvg_age", 0)
    if age <= 3:
        score += 0.5
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
                continue  # оба объёма слабые — пропускаем
            if not ALLOW_LOW_VOL_PRICE_UP and vol["low_vol_move"] and vol["price_chg_6"] > 0:
                # цена растёт без объёма — часто ловушка / разгрузка
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
    # volume остаётся как есть
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
        # Equal highs — сильная ликвидность
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

    # Минимальный quality score (можно поднять для ещё большей точности)
    if q < 5.5:
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

    return ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": MARKET_TYPE}})


def drop_unclosed(df: pd.DataFrame, tf: str, now: Optional[datetime] = None) -> pd.DataFrame:
    if df.empty:
        return df
    now = now or utcnow()
    if df.index[-1] + pd.Timedelta(seconds=tf_seconds(tf)) > now:
        return df.iloc[:-1]
    return df


def fetch_frame(ex, symbol: str, tf: str, limit: int) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return drop_unclosed(df, tf)


def fetch_top_symbols(ex) -> List[str]:
    """Возвращает все ликвидные USDT-пары BingX (или топ TOP_N). TOP_N=0 → все выше MIN_QUOTE_VOLUME."""
    fallback = (
        ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        if MARKET_TYPE != "spot"
        else ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    )
    try:
        # Подгружаем рынки, чтобы ccxt правильно резолвил символы
        try:
            ex.load_markets()
        except Exception:  # noqa: BLE001
            pass

        rows = []
        tickers = ex.fetch_tickers()
        for sym, t in tickers.items():
            if MARKET_TYPE == "spot":
                if not sym.endswith("/USDT") or ":" in sym:
                    continue
            else:
                # фьючерсы BingX в ccxt: BTC/USDT:USDT
                if not (sym.endswith("/USDT:USDT") or (sym.endswith("/USDT") and ":USDT" in sym)):
                    continue
            base = sym.split("/")[0].split(":")[0]
            if base in STABLES or re.search(r"\d+[LS]$", base):
                continue
            vol = float(t.get("quoteVolume") or 0)
            if vol >= MIN_QUOTE_VOLUME:
                rows.append((sym, vol))

        rows.sort(key=lambda r: r[1], reverse=True)
        if TOP_N and TOP_N > 0:
            selected = [s for s, _ in rows[:TOP_N]]
        else:
            selected = [s for s, _ in rows]  # весь рынок выше порога объёма

        logger.info("Найдено %d ликвидных пар (порог объёма %.0f USDT), берём %d", len(rows), MIN_QUOTE_VOLUME, len(selected))
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


def make_trade(sig: Dict, now: datetime) -> Dict:
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
        "quality": round(sig.get("quality", 0), 1),
        "status": "pending",
        "created_ms": ms(now),
        "expires_ms": ms(now) + SETUP_TTL_MIN * 60_000,
        "checked_ms": ms(now),
        "filled_ms": None,
        "closed_ms": None,
        "r": None,
    }


def evaluate_trade(t: Dict, candles: List[Tuple[int, float, float, float, float]]) -> List[str]:
    """Прогоняет закрытые 1m-свечи по сделке. Меняет t, возвращает события."""
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

    # Последние 7 дней
    week_ago = ms(utcnow()) - 7 * 86400 * 1000
    week = [t for t in ts if t["created_ms"] >= week_ago]
    w_wins = sum(1 for t in week if t["status"] == "win")
    w_loss = sum(1 for t in week if t["status"] == "loss")
    w_dec = w_wins + w_loss
    w_wr = f"{w_wins / w_dec * 100:.0f}%" if w_dec else "—"

    return (
        "📊 <b>Статистика сигналов</b>\n\n"
        f"<b>Всего:</b> {len(ts)} сигналов\n"
        f"Ждут входа: {cnt('pending')} | В сделке: {cnt('active')}\n"
        f"Не исполнились: {cnt('expired', 'missed')}\n\n"
        f"✅ TP: {wins} | ❌ SL: {losses} | ⏱ Таймаут: {timeouts}\n"
        f"<b>Winrate (TP/(TP+SL)): {wr}</b>\n"
        f"Итого R: <b>{total_r:+.2f}R</b>\n"
        f"Средний quality: {avg_q:.1f}/10\n\n"
        f"<b>За 7 дней:</b> {len(week)} сигналов, winrate {w_wr}\n\n"
        f"<i>Бот считает только закрытые по TP/SL. Это прозрачная статистика.</i>"
    )


# ============================== MESSAGES / CHART ==============================


def fp(p: float) -> str:
    d = 2 if p >= 1000 else 4 if p >= 1 else 5 if p >= 0.01 else 8
    return f"{p:.{d}f}"


def signal_text(sig: Dict) -> str:
    long_ = sig["direction"] == "LONG"
    expire_at = utcnow() + timedelta(minutes=SETUP_TTL_MIN)
    structure = "бычья (HH/HL)" if long_ else "медвежья (LH/LL)"
    zone = "Discount (ниже EQ)" if long_ else "Premium (выше EQ)"
    swept = "sell-side" if long_ else "buy-side"
    brk = "выше" if long_ else "ниже"
    tp2 = f"TP2: <code>{fp(sig['tp2'])}</code> ({sig['tp2_label']})\n" if sig.get("tp2") else ""

    vol = sig.get("vol_info", {})
    q = sig.get("quality", 0)
    grade = "A" if q >= 8.0 else "B" if q >= 6.5 else "C"

    # Примерный тех. профит
    risk_pct = abs(sig["entry"] - sig["stop"]) / sig["entry"] * 100
    profit_pct = risk_pct * sig["rr"]
    tech_profit = f"+{profit_pct:.1f}% (при RR 1:{sig['rr']:.1f})"

    vol_lines = []
    if vol:
        vol_lines.append(f"• Объём свипа: {vol.get('sweep_rv', 0):.1f}x средней")
        vol_lines.append(f"• Объём MSS: {vol.get('mss_rv', 0):.1f}x средней")
        cvd = vol.get("cvd_delta", 0)
        flow = "приток" if cvd > 0 else "отток" if cvd < 0 else "нейтрально"
        vol_lines.append(f"• CVD (8 свечей): {flow}")
        if vol.get("low_vol_move"):
            vol_lines.append("• ⚠️ Цена двигалась при низком объёме")

    vol_block = "\n".join(vol_lines) if vol_lines else "• объём не рассчитан"

    return (
        f"{'🟢' if long_ else '🔴'} <b>ICT SIGNAL [{grade}] {sig['symbol']} {sig['direction']}</b>\n"
        f"Quality: <b>{q:.1f}/10</b>\n\n"
        f"<b>Контекст</b>\n"
        f"• Kill Zone: {sig['kill_zone']} ({fmt_msk(utcnow())})\n"
        f"• HTF {HTF}: структура {structure}, цена в {zone}\n"
        f"• EQ: {fp(sig['eq'])} | Range: {fp(sig['range_low'])} – {fp(sig['range_high'])}\n"
        f"• Снята {swept} ликвидность: {sig['level_label']} {fp(sig['level'])}\n"
        f"• MSS: закрытие {brk} {fp(sig['ref'])} с displacement\n"
        f"• FVG {LTF}: {fp(sig['fvg_bottom'])} – {fp(sig['fvg_top'])} (возраст {sig.get('fvg_age', '?')} св.)\n\n"
        f"<b>Объём / Smart Money</b>\n{vol_block}\n\n"
        f"<b>План входа (лимитка по CE)</b>\n"
        f"Вход: <code>{fp(sig['entry'])}</code>\n"
        f"Стоп: <code>{fp(sig['stop'])}</code>\n"
        f"TP1: <code>{fp(sig['tp1'])}</code> ({sig['tp1_label']})  RR 1:{sig['rr']:.1f}\n"
        f"{tp2}"
        f"Примерный тех. профит: <b>{tech_profit}</b>\n\n"
        f"<b>Отмена, если:</b>\n"
        f"• цена дойдёт до TP1 без касания CE\n"
        f"• вход не сработает до {fmt_msk(expire_at)}\n\n"
        f"<i>Статистика бота считает только реальные TP/SL. Смотри /stats.</i>\n"
        f"⚠️ Не финансовая рекомендация. Риск — по вашему плану."
    )


def render_chart(sig: Dict) -> BytesIO:
    df, off = sig["df"], sig["offset"]
    n = len(df)
    long_ = sig["direction"] == "LONG"
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

    # FVG
    fvg_x = px(sig.get("fvg_i", n - 5))
    ax.axhspan(sig["fvg_bottom"], sig["fvg_top"], xmin=fvg_x / x_end, xmax=1.0, alpha=0.15, color="#42a5f5")
    ax.axhline(sig["entry"], color="#42a5f5", linestyle="--", linewidth=1.2, label="CE entry")
    ax.axhline(sig["stop"], color="#ef5350", linestyle="--", linewidth=1.0, label="Stop")
    ax.axhline(sig["tp1"], color="#26a69a", linestyle="--", linewidth=1.0, label="TP1")
    if sig.get("tp2"):
        ax.axhline(sig["tp2"], color="#66bb6a", linestyle=":", linewidth=0.9, label="TP2")

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
        "ICT Smart Money бот (pro edition)\n\n"
        "/stats — статистика сигналов и R\n"
        "/open — текущие pending/active сетапы\n\n"
        "Сигналы приходят только при полном совпадении:\n"
        "Kill Zone + структура HTF + Sweep + MSS + FVG + объём + Premium/Discount."
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
    lines = []
    for t in open_:
        lines.append(
            f"{t['symbol']} {t['direction']} | {t['status']} | "
            f"E {fp(t['entry'])} SL {fp(t['stop'])} TP {fp(t['tp1'])} | Q={t.get('quality', '?')}"
        )
    await update.message.reply_text("\n".join(lines))


async def send_signal(bot, sig: Dict) -> None:
    text = signal_text(sig)
    try:
        chart = render_chart(sig)
        await bot.send_photo(chat_id=CHAT_ID, photo=chart, caption=text, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")


# ============================== LOOPS ==============================


async def scanner_loop(bot, ex, store: TradeStore) -> None:
    while True:
        try:
            # Ждём закрытия свечи LTF
            now = utcnow()
            tf_sec = tf_seconds(LTF)
            wait = tf_sec - (int(now.timestamp()) % tf_sec) + CLOSE_DELAY_S
            await asyncio.sleep(max(5, wait))

            if store.signals_today(utcnow()) >= MAX_SIGNALS_PER_DAY:
                logger.info("Достигнут дневной лимит сигналов")
                await asyncio.sleep(300)
                continue

            symbols = await asyncio.to_thread(fetch_top_symbols, ex)
            btc_bias = await asyncio.to_thread(get_btc_bias, ex)
            logger.info("Скан всего рынка: %d пар, BTC bias=%s, concurrency=%d", len(symbols), btc_bias, SCAN_CONCURRENCY)

            sem = asyncio.Semaphore(SCAN_CONCURRENCY)
            results: List[Optional[Dict]] = []

            async def _scan_one(sym: str) -> Optional[Dict]:
                async with sem:
                    try:
                        return await asyncio.to_thread(analyze_symbol, ex, sym, btc_bias)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("%s: %s", sym, e)
                        return None

            # Параллельный скан всего рынка
            tasks = [_scan_one(sym) for sym in symbols]
            scanned = await asyncio.gather(*tasks)

            # Сортируем по quality (лучшие сначала) и отправляем
            candidates = [s for s in scanned if s]
            candidates.sort(key=lambda s: s.get("quality", 0), reverse=True)

            for sig in candidates:
                if store.signals_today(utcnow()) >= MAX_SIGNALS_PER_DAY:
                    break
                if store.blocked(sig, utcnow()):
                    continue
                await send_signal(bot, sig)
                store.add(make_trade(sig, utcnow()))
                logger.info("Сигнал: %s %s Q=%.1f RR=%.1f", sig["symbol"], sig["direction"], sig.get("quality", 0), sig.get("rr", 0))
        except Exception:  # noqa: BLE001
            logger.exception("scanner")


EVENT_TEXT = {
    "filled": "🟢 {s} {d}: цена коснулась CE — вход по лимитке исполнен",
    "win": "✅ {s} {d}: TP1 достигнут ({r:+.2f}R)",
    "loss": "❌ {s} {d}: стоп ({r:+.2f}R)",
    "timeout": "⏱ {s} {d}: закрыто по таймауту ({r:+.2f}R)",
    "expired": "⌛ {s} {d}: вход так и не сработал, сетап снят",
    "missed": "🚫 {s} {d}: цена ушла к цели без входа, сетап отменён",
}


async def monitor_trades(bot, ex, store: TradeStore) -> None:
    for t in [x for x in store.trades if x["status"] in ("pending", "active")]:
        try:
            raw = await asyncio.to_thread(ex.fetch_ohlcv, t["symbol"], "1m", t["checked_ms"], 1000)
            now_ms = ms(utcnow())
            candles = [
                (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in raw
                if r[0] + 60_000 <= now_ms
            ]
            events = evaluate_trade(t, candles)
            store.save()
            for ev in events:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=EVENT_TEXT[ev].format(s=t["symbol"], d=t["direction"], r=t["r"] or 0.0),
                )
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
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"⏰ Kill Zone «{name}» откроется через ~{int(minutes)} мин ({fmt_msk(start)})",
                    )
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

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("ICT pro bot запущен: %s, HTF %s / LTF %s", MARKET_TYPE, HTF, LTF)
    try:
        await asyncio.gather(scanner_loop(app.bot, ex, store), monitor_loop(app.bot, ex, store))
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise SystemExit("Задайте переменные окружения TELEGRAM_TOKEN и CHAT_ID")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
