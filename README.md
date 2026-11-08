# ICT BingX Telegram Bot

High-precision **ICT / Smart Money** signal bot for the top liquid crypto pairs on **BingX**.

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
9. Фильтр по структуре BTC + лимит сигналов в день + кулдаун

Бот также отслеживает сделки (pending → active → win/loss/timeout) и рисует график с разметкой.

### Быстрый старт (сервер / VPS)

```bash
git clone https://github.com/timanovsergey9-crypto/ict-bingx-telegram-bot.git
cd ict-bingx-telegram-bot

python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
nano .env                  # вставь TELEGRAM_TOKEN и CHAT_ID

export $(grep -v '^#' .env | xargs)
python bot.py
```

### Запуск в фоне

```bash
# screen
screen -S ictbot
export $(grep -v '^#' .env | xargs)
python bot.py
# Ctrl+A → D

# или nohup
nohup python bot.py > bot.log 2>&1 &
```

### Systemd (рекомендуется для VPS)

Создай `/etc/systemd/system/ict-bot.service`:

```ini
[Unit]
Description=ICT BingX Telegram Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/ict-bingx-telegram-bot
EnvironmentFile=/path/to/ict-bingx-telegram-bot/.env
ExecStart=/path/to/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ict-bot
sudo journalctl -u ict-bot -f   # логи
```

### Команды бота
- `/start` или `/help` — справка
- `/stats` — статистика сигналов и R
- `/open` — текущие pending/active сетапы

### Как получить TELEGRAM_TOKEN и CHAT_ID
1. [@BotFather](https://t.me/BotFather) → `/newbot` → скопируй токен
2. Chat ID: напиши боту [@userinfobot](https://t.me/userinfobot) или добавь бота в канал/группу и возьми ID канала (начинается с `-100...`)

### GitHub Actions (опционально)
В репозитории есть workflow, который запускает бота по расписанию. Для 24/7 лучше использовать VPS + systemd.

### Disclaimer
Это образовательная реализация концепций ICT. **Не является финансовой рекомендацией.** Торговля связана с риском потери средств. Управляйте риском самостоятельно.
