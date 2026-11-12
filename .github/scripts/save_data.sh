#!/usr/bin/env bash
# Persist data/plus_one_state.json (and data/trades.json if present) to the
# bot-data branch so dedupe survives GitHub Actions runners.
set -u

save() {
  if [ ! -s data/plus_one_state.json ] && [ ! -s data/trades.json ]; then
    echo "no state files to save"
    return 0
  fi
  tmp="$(mktemp -d)"
  [ -s data/plus_one_state.json ] && cp data/plus_one_state.json "$tmp/plus_one_state.json"
  [ -s data/trades.json ] && cp data/trades.json "$tmp/trades.json"
  (
    cd "$tmp" || exit 0
    git init -q -b bot-data
    git config user.name "ict-bot"
    git config user.email "ict-bot@users.noreply.github.com"
    git add plus_one_state.json trades.json 2>/dev/null || git add -A
    git commit -q -m "bot data $(date -u +%FT%TZ)"
    git push -q -f "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" bot-data \
      || echo "WARN: failed to push bot-data"
  )
  rm -rf "$tmp"
}

if [ "${1:-}" = "--loop" ]; then
  while true; do
    sleep 900
    save
  done
else
  save
fi
