#!/usr/bin/env python3
"""Журнал сигналов: хранение, проверка исходов (TP/SL), статистика.

Один и тот же файл лежит во всех репозиториях с сигнальными ботами.
Сетевых зависимостей нет: свечи и отправку в Telegram передаёт вызывающий код.

Файл журнала: data/<имя>.json  ({"meta": {...}, "trades": [...]}).
Папку data/ GitHub Actions восстанавливает из ветки bot-data перед запуском
и сохраняет обратно после (см. .github/scripts/persist.sh).

Статусы сделки:
  pending  — сигнал выдан, вход ещё не сработал
  open     — вход исполнен, сделка идёт
  tp / sl  — закрыта по цели / по стопу
  time     — закрыта по времени (Plus One: 8 баров)
  timeout  — закрыта по таймауту (свечной трекер)
  unfilled — вход так и не сработал (в статистику R не входит)
  stale    — не удалось проверить (нет данных), в статистику не входит
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger("tracker")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
COST = float(os.getenv("FEE_PER_SIDE", "0.0007"))  # комиссия+проскальзывание на сторону

OPEN_STATES = ("pending", "open")
RESULT_STATES = ("tp", "sl", "time", "timeout")
NOFILL_STATES = ("unfilled", "stale")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ============================== ЖУРНАЛ ==============================


class Journal:
    """JSON-журнал сделок. Запись атомарная (tmp + replace)."""

    def __init__(self, name: str, data_dir: Optional[str] = None):
        self.name = name
        self.path = Path(data_dir or DATA_DIR) / f"{name}.json"
        self.trades: List[Dict] = []
        self.meta: Dict = {}

    def load(self) -> "Journal":
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.trades = list(raw.get("trades", []))
                self.meta = dict(raw.get("meta", {}))
            except Exception as e:  # битый файл не затираем молча — откладываем в сторону
                bad = self.path.with_suffix(".corrupt")
                os.replace(self.path, bad)
                log.error("Журнал %s повреждён (%s), перенесён в %s", self.path, e, bad)
                self.trades, self.meta = [], {}
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"meta": self.meta, "trades": self.trades}, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ---------- доступ ----------
    def has_key(self, key: str) -> bool:
        return any(t.get("key") == key for t in self.trades)

    def add(self, trade: Dict) -> None:
        self.trades.append(trade)
        self.save()  # сразу на диск: даже если дальше что-то упадёт, сигнал не потеряется

    def open_trades(self) -> List[Dict]:
        return [t for t in self.trades if t["status"] in OPEN_STATES]

    def open_for_symbol(self, symbol: str) -> bool:
        return any(t["symbol"] == symbol for t in self.open_trades())

    def last_created_ms(self, symbol: str) -> int:
        ms = [t["created_ms"] for t in self.trades if t["symbol"] == symbol]
        return max(ms) if ms else 0


def new_trade(*, key: str, symbol: str, direction: str, entry: float, sl: float, tp: float,
              tf: str = "", extra: Optional[Dict] = None, status: str = "pending") -> Dict:
    t = {
        "id": uuid.uuid4().hex[:8],
        "key": key,
        "symbol": symbol,
        "direction": direction.upper(),
        "tf": tf,
        "entry": float(entry),
        "sl": float(sl),
        "tp": float(tp),
        "status": status,
        "created_ms": now_ms(),
        "checked_ms": now_ms(),
        "closed_ms": None,
        "net_r": None,
    }
    if extra:
        t.update(extra)
    return t


# ============================== ТЕКСТЫ ==============================

_ICON = {"tp": "✅", "sl": "❌", "time": "⏱", "timeout": "⏱"}
_WORD = {
    "tp": "цель достигнута",
    "sl": "стоп",
    "time": "выход по времени",
    "timeout": "выход по таймауту",
}


def result_text(t: Dict) -> str:
    return (f"{_ICON.get(t['status'], '•')} {t['symbol']} {t['direction']} {t.get('tf', '')}: "
            f"{_WORD.get(t['status'], t['status'])}, {t['net_r']:+.2f}R (после комиссий)").replace("  ", " ")


# ============================== СТАТИСТИКА ==============================


def _closed(trades: List[Dict]) -> List[Dict]:
    return [t for t in trades if t["status"] in RESULT_STATES and t.get("net_r") is not None]


def _line(ts: List[Dict]) -> str:
    n = len(ts)
    if not n:
        return "нет закрытых сделок"
    rs = [t["net_r"] for t in ts]
    wins = sum(1 for r in rs if r > 0)
    return f"{n} сделок, winrate {wins / n * 100:.0f}%, {sum(rs):+.2f}R"


def stats_text(trades: List[Dict], title: str, window_days: int = 7) -> str:
    done = _closed(trades)
    cnt = lambda *st: sum(1 for t in trades if t["status"] in st)  # noqa: E731
    rs = [t["net_r"] for t in done]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r <= 0)
    pf = f"{gp / gl:.2f}" if gl > 0 else ("∞" if gp > 0 else "—")
    wr = f"{sum(1 for r in rs if r > 0) / len(rs) * 100:.0f}%" if rs else "—"
    avg = f"{sum(rs) / len(rs):+.2f}R" if rs else "—"

    cutoff = now_ms() - window_days * 86400 * 1000
    week = [t for t in done if (t.get("closed_ms") or 0) >= cutoff]
    week_new = sum(1 for t in trades if t["created_ms"] >= cutoff)

    lines = [
        f"📊 {title}",
        "",
        f"Сигналов всего: {len(trades)}",
        f"Ждут входа: {cnt('pending')} · в сделке: {cnt('open')} · не исполнились: {cnt('unfilled')}",
        "",
        f"Закрыто сделок: {len(done)}  (TP {cnt('tp')} · SL {cnt('sl')} · по времени {cnt('time', 'timeout')})",
        f"Winrate (R>0): {wr} · средний R: {avg} · сумма R: {sum(rs):+.2f}R · PF: {pf}",
        f"LONG: {_line([t for t in done if t['direction'] == 'LONG'])}",
        f"SHORT: {_line([t for t in done if t['direction'] == 'SHORT'])}",
        "",
        f"За {window_days} дн.: новых сигналов {week_new}, закрыто: {_line(week)}",
        "",
        "R считается после комиссий. Пока закрытых сделок меньше 30, цифрам верить рано.",
    ]
    return "\n".join(lines)


def send_daily_summary(journal: Journal, notify: Callable[[str], bool], hour_utc: int, title: str,
                       force: bool = False) -> bool:
    """Раз в сутки (после hour_utc) присылает сводку. force=True — прислать сейчас."""
    if not journal.trades:
        return False
    n = datetime.now(timezone.utc)
    if not force and (journal.meta.get("summary_day") == today_utc() or n.hour < hour_utc):
        return False
    ok = notify(stats_text(journal.trades, title))
    if ok and not force:
        journal.meta["summary_day"] = today_utc()
        journal.save()
    return bool(ok)


# ============================== PLUS ONE: ПРОВЕРКА ИСХОДОВ ==============================


def resolve_plus_one(journal: Journal, fetch_klines: Callable, simulate: Callable,
                     drop_unclosed: Callable, tf_step: Callable, notify: Callable[[str], bool]) -> int:
    """Прогоняет открытые сделки Plus One через ту же симуляцию, что и бэктест
    (simulate_fill_and_exit): вход по стоп-ордеру на следующем баре, стоп, 2R, выход на 8-м баре.
    Возвращает число сделок, у которых изменился статус."""
    import pandas as pd

    changed = 0
    cache: Dict = {}
    for t in journal.open_trades():
        setup = dict(t["setup"])
        tf, sym = t["tf"], t["bingx"]
        age_h = (now_ms() - t["created_ms"]) / 3_600_000
        ck = (sym, tf)
        if ck not in cache:
            try:
                cache[ck] = drop_unclosed(fetch_klines(sym, tf, 200), tf_step(tf))
            except Exception as e:  # noqa: BLE001
                log.warning("resolve %s %s: нет свечей (%s)", sym, tf, e)
                cache[ck] = None
        df = cache[ck]
        if df is None or df.empty:
            if age_h > 24 * 30:
                t["status"], t["closed_ms"] = "stale", now_ms()
                changed += 1
            continue

        st = pd.Timestamp(setup["setup_time"])
        if st not in df.index:
            if age_h > 24 * 20:  # вышла за окно в 200 баров — проверить уже нельзя
                t["status"], t["closed_ms"] = "stale", now_ms()
                changed += 1
            continue
        setup["setup_i"] = int(df.index.get_loc(st))

        out = simulate(df, setup)
        if not out:
            continue
        status = out.get("status")
        if status in ("tp", "sl", "time") and out.get("filled"):
            t.update(status=status, net_r=round(float(out["net_r"]), 3),
                     gross_r=round(float(out.get("gross_r", 0.0)), 3),
                     fill=out.get("fill"), exit=out.get("exit"), closed_ms=now_ms(),
                     sl_final=out.get("sl"), tp_final=out.get("tp"), bars=out.get("bars"))
            changed += 1
            notify(result_text(t))
        elif status == "open" and out.get("filled"):
            if t["status"] != "open":
                t.update(status="open", fill=out.get("fill"))
                changed += 1
        elif status in ("unfilled", "invalid_sl"):
            t.update(status="unfilled", closed_ms=now_ms())
            changed += 1
        # no_next_bar и всё неизвестное — ждём следующего запуска
    if changed:
        journal.save()
    return changed


# ============================== СВЕЧНОЙ ТРЕКЕР (рыночный вход) ==============================


def resolve_candles(t: Dict, candles: List[tuple], tf_ms: int, max_hours: float, cost: float = COST) -> List[str]:
    """Проверка сделки по ЗАКРЫТЫМ свечам (ts_open_ms, o, h, l, c, ...).
    Вход — по цене entry сразу после created_ms. Если в одной свече и стоп, и цель — считаем стоп.
    Возвращает список событий (статус закрытия или пусто)."""
    events: List[str] = []
    s = 1 if t["direction"] == "LONG" else -1
    entry, sl, tp = t["entry"], t["sl"], t["tp"]
    risk = abs(entry - sl)
    if risk <= 0:
        t.update(status="stale", closed_ms=now_ms())
        return ["stale"]
    if t["status"] == "pending":
        t["status"] = "open"
    max_ms = int(max_hours * 3_600_000)

    def close(status: str, exit_px: float, ts_close: int) -> None:
        gross = (exit_px - entry) * s
        net = (gross - cost * (entry + exit_px)) / risk
        t.update(status=status, net_r=round(net, 3), gross_r=round(gross / risk, 3),
                 exit=exit_px, closed_ms=ts_close)
        events.append(status)

    for c in candles:
        ts, hi, lo, cl = int(c[0]), float(c[2]), float(c[3]), float(c[4])
        if ts < t["checked_ms"]:
            continue
        t["checked_ms"] = ts + tf_ms
        hit_sl = lo <= sl if s == 1 else hi >= sl
        hit_tp = hi >= tp if s == 1 else lo <= tp
        if hit_sl:  # при одновременном касании считаем худшее
            close("sl", sl, ts + tf_ms)
            break
        if hit_tp:
            close("tp", tp, ts + tf_ms)
            break
        if ts + tf_ms - t["created_ms"] >= max_ms:
            close("timeout", cl, ts + tf_ms)
            break
    return events


# ============================== ОДИН РАЗ В СУТКИ (для ботов без Journal-меты) ==============================


def due_daily(name: str, hour_utc: int, data_dir: Optional[str] = None) -> bool:
    p = Path(data_dir or DATA_DIR) / f"{name}.day"
    n = datetime.now(timezone.utc)
    if n.hour < hour_utc:
        return False
    return not (p.exists() and p.read_text().strip() == today_utc())


def mark_daily(name: str, data_dir: Optional[str] = None) -> None:
    p = Path(data_dir or DATA_DIR) / f"{name}.day"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(today_utc())
