# ICT BingX Telegram Bot (Smart Pro)

High-precision **ICT / Smart Money** signal bot for liquid crypto pairs on **BingX**.

Сигналы приходят в Telegram только когда выполнены **все** условия ICT на закрытых свечах.

### Core ICT Logic
1. **Kill Zone** (London / New York, время Нью-Йорка + DST)
2. **HTF bias** по структуре (HH+HL = bullish, LH+LL = bearish)
3. **Premium / Discount** — вход только в правильной зоне относительно EQ дилинг-рейнджа HTF
4. **Liquidity Sweep** (swing low/high или PDL/PDH) + возврат
5. **MSS** с displacement (тело ≥ 1.5 ATR и ≥ 55% от диапазона)
6. **Unmitigated FVG**, созданный displacement-свечой
7. Вход по **CE** (Consequent Encroachment) лимиткой
8. Стоп за экстремумом свипа, TP1 = ближайшая нетронутая ликвидность, RR ≥ 2.0
9. Фильтр по структуре BTC + объём + quality score

**Лимит сигналов в день снят** (`MAX_SIGNALS_PER_DAY=0`) — бот шлёт сигнал сразу, как нашёл валидный сетап.

### Улучшения Smart Pro
- Скан до 150 пар (весь ликвидный рынок)
- Объём / CVD / funding
- Режим рынка (тренд / рейндж)
- Прозрачная статистика + авто-отчёт раз в 7 дней
- В сигнале: вход, стоп-лосс, TP, RR, ликвидность

### Быстрый старт

```bash
git clone https://github.com/timanovsergey9-crypto/ict-bingx-telegram-bot.git
cd ict-bingx-telegram-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # TELEGRAM_TOKEN, CHAT_ID, MAX_SIGNALS_PER_DAY=0
python bot.py
```

### Команды бота
- `/start` или `/help` — справка (покажет актуальный лимит)
- `/stats` — статистика сигналов и R
- `/open` — текущие pending/active сетапы

### .env важные параметры
```env
MAX_SIGNALS_PER_DAY=0    # 0 = без лимита
TOP_N=150
MIN_QUOTE_VOLUME=2000000
MIN_RR=2.0
COOLDOWN_H=2
```

### Disclaimer
Это образовательная реализация концепций ICT. **Не является финансовой рекомендацией.**
