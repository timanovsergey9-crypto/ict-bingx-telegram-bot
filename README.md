# ICT BingX Telegram Bot (v3)

**ICT / Smart Money** сигналы для ликвидных пар **BingX** в Telegram.
Сигнал приходит только когда выполнены **все** условия на закрытых свечах.

### Логика сигнала
1. **Kill Zone** (London / New York, время Нью-Йорка + DST)
2. **HTF bias** по структуре (HH+HL / LH+LL) и согласие с **EMA50** на HTF
3. **Premium / Discount** относительно EQ дилинг-рейнджа HTF
4. **Liquidity Sweep** (swing low/high, PDL/PDH) + возврат
5. **MSS** с displacement (тело ≥ 1.5 ATR и ≥ 55% диапазона)
6. **Неотработанный FVG**, созданный displacement-свечой
7. Вход лимиткой по **CE**, стоп за экстремумом свипа, TP1 = ближайшая ликвидность, RR ≥ 2.0
8. Фильтр по структуре BTC
9. Подтверждение объёмом (относительный объём + приближённый CVD)
10. **Quality score ≥ `MIN_QUALITY`** (по умолчанию 6.5 = грейды A и B)

Вне Kill Zone бот не сканирует рынок.

### Быстрый старт

```bash
git clone https://github.com/timanovsergey9-crypto/ict-bingx-telegram-bot.git
cd ict-bingx-telegram-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполните TELEGRAM_TOKEN и CHAT_ID
python bot.py --selftest                   # офлайн-проверка логики
FORCE_KZ=1 python bot.py --scan-once       # один скан рынка, без Telegram
python bot.py                              # рабочий режим
```

Перед рабочим запуском напишите своему боту `/start` в Telegram.

### Команды бота
- `/start`, `/help` — справка
- `/status` — состояние и ближайшая Kill Zone
- `/stats` — статистика сигналов и R (в том числе по грейдам)
- `/open` — текущие pending/active сетапы

### Важные параметры (.env)
```env
MIN_QUALITY=6.5          # порог качества 0-10
MIN_RR=2.0
MAX_SIGNALS_PER_DAY=4    # лимит сигналов в сутки (значение 0 не ставить)
COOLDOWN_H=6             # пауза по одной паре
TOP_N=100
MIN_QUOTE_VOLUME=5000000
HTF_EMA=50
```

### Запуск через GitHub Actions
Workflow `Run ICT Bot` перезапускает бота каждые 4 часа. Секреты `TELEGRAM_TOKEN` и `CHAT_ID`
задаются в Settings → Secrets and variables → Actions. Файл статистики между перезапусками не
сохраняется, для честной статистики лучше постоянный сервер (VPS и т. п.).

### Disclaimer
Образовательная реализация концепций ICT. **Не является финансовой рекомендацией.**
Любая стратегия может приносить убытки; сначала тестируйте с минимальным риском.
