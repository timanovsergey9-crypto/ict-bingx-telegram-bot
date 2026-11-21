#!/usr/bin/env python3
"""Разовый запуск ICT Kill Zone бота для GitHub Actions (вместо вечного цикла).

За один запуск:
  1) проверяет исходы открытых сделок (TP / SL / таймаут / вход не сработал),
  2) если сейчас Kill Zone и лимиты не исчерпаны — сканирует рынок и шлёт новые сигналы,
  3) раз в сутки присылает сводку статистики.

Состояние лежит в data/trades.json (TradeStore из ict_bot_improved.py) и сохраняется между
запусками через ветку bot-data (.github/scripts/persist.sh).

  python ict_once.py            обычный запуск
  python ict_once.py --stats    прислать статистику прямо сейчас
"""
from __future__ import annotations

import asyncio
import logging
import sys

import ict_bot_improved as ict
import tracker

logger = logging.getLogger("ICT-once")
SUMMARY_HOUR_UTC = ict._i("SUMMARY_HOUR_UTC", 6)


async def _send_stats(bot, store) -> None:
    await bot.send_message(chat_id=ict.CHAT_ID, text=ict.stats_text(store), parse_mode="HTML")


async def run(stats_only: bool = False) -> int:
    if not ict.TELEGRAM_TOKEN or not ict.CHAT_ID:
        print("Задайте TELEGRAM_TOKEN и CHAT_ID")
        return 1
    from telegram import Bot

    store = ict.TradeStore(ict.TRADES_FILE)
    store.load()
    now = ict.utcnow()
    kz = ict.kz_at(now)
    open_ = [t for t in store.trades if t["status"] in ("pending", "active")]
    print(f"ICT: kill zone={kz or 'нет'}, открытых сетапов={len(open_)}, всего сигналов={len(store.trades)}")

    summary_due = bool(store.trades) and tracker.due_daily("ict_summary", SUMMARY_HOUR_UTC)
    if not stats_only and not kz and not open_ and not summary_due:
        print("Вне Kill Zone и нет открытых сетапов — делать нечего.")
        return 0

    ex = ict.make_exchange()
    async with Bot(ict.TELEGRAM_TOKEN) as bot:
        if stats_only:
            await ict.monitor_trades(bot, ex, store)
            await _send_stats(bot, store)
            return 0

        # 1) исходы открытых сделок
        if open_:
            await ict.monitor_trades(bot, ex, store)

        # 2) новые сигналы — только в Kill Zone
        if kz and not store.daily_limit_hit(ict.utcnow()):
            sent = 0
            for sig in await ict.scan_market(ex):
                if store.daily_limit_hit(ict.utcnow()):
                    break
                if store.blocked(sig, ict.utcnow()) or store.open_blocked(sig["direction"]):
                    continue
                await ict.send_signal(bot, sig, store)
                store.add(ict.make_trade(sig, ict.utcnow()))   # add() сразу пишет на диск
                sent += 1
                print(f"Сигнал: {sig['symbol']} {sig['direction']} Q={sig['quality']:.1f} RR={sig['rr']:.1f}")
            print(f"Новых сигналов отправлено: {sent}")
        elif kz:
            print("Дневной лимит сигналов достигнут")

        # 3) суточная сводка
        if store.trades and tracker.due_daily("ict_summary", SUMMARY_HOUR_UTC):
            await _send_stats(bot, store)
            tracker.mark_daily("ict_summary")
    store.save()
    done = [t for t in store.trades if t["status"] in ("win", "loss")]
    print(f"ICT journal: всего={len(store.trades)}, закрыто TP/SL={len(done)}, "
          f"сумма R={sum(t['r'] for t in store.trades if t.get('r') is not None):+.2f}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run(stats_only="--stats" in sys.argv)))


if __name__ == "__main__":
    main()
