#!/usr/bin/env bash
# Сохраняет data/trades.json в отдельную ветку bot-data (одним коммитом, без истории),
# чтобы статистика и открытые сделки переживали перезапуски GitHub Actions.
# Использование: bash .github/scripts/save_data.sh [--loop]
set -u

save() {
  [ -s data/trades.json ] || return 0
  tmp="$(mktemp -d)"
  cp data/trades.json "$tmp/trades.json"
  (
    cd "$tmp" || exit 0
    git init -q -b bot-data
    git config user.name "ict-bot"
    git config user.email "ict-bot@users.noreply.github.com"
    git add trades.json
    git commit -q -m "bot data $(date -u +%FT%TZ)"
    git push -q -f "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" bot-data \
      || echo "WARN: не удалось сохранить данные бота"
  )
  rm -rf "$tmp"
}

if [ "${1:-}" = "--loop" ]; then
  while true; do
    sleep 900   # раз в 15 минут
    save
  done
else
  save
fi
