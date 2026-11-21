#!/usr/bin/env bash
# Сохранение состояния ботов между запусками GitHub Actions.
#   persist.sh restore  — вытащить data/ из ветки bot-data ПЕРЕД запуском бота
#   persist.sh save     — закоммитить data/ в ветку bot-data ПОСЛЕ запуска
#
# Гарантии:
#  * restore падает (а не «начинает с нуля»), если ветка есть, но достать её не удалось —
#    иначе следующий save затёр бы историю пустыми данными;
#  * save коммитит поверх ветки (история сохраняется), коммит создаётся только при изменениях;
#  * save громко падает, если пуш не прошёл за 4 попытки.
set -euo pipefail

BRANCH="${DATA_BRANCH:-bot-data}"
DIR="${DATA_DIR:-data}"
REMOTE_URL="${PERSIST_REMOTE:-https://x-access-token:${GH_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"

branch_state() {  # 0 = ветка есть, 2 = ветки нет, 1 = не удалось проверить
  local rc=1
  for i in 1 2 3; do
    set +e
    git ls-remote --exit-code --heads "$REMOTE_URL" "$BRANCH" >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then echo "$rc"; return; fi
    sleep $((i * 3))
  done
  echo 1
}

restore() {
  mkdir -p "$DIR"
  case "$(branch_state)" in
    0)
      tmp="$(mktemp -d)"
      git clone -q --depth=1 --branch "$BRANCH" "$REMOTE_URL" "$tmp"
      rm -rf "$tmp/.git"
      cp -a "$tmp"/. "$DIR"/
      rm -rf "$tmp"
      echo "restored from $BRANCH: $(ls -A "$DIR" | tr '\n' ' ')"
      ;;
    2) echo "ветки $BRANCH ещё нет — начинаем с чистого листа" ;;
    *) echo "ERROR: не удалось проверить ветку $BRANCH (нет сети/доступа). Прерываю, чтобы не затереть данные." >&2
       exit 1 ;;
  esac
}

save() {
  if [ ! -d "$DIR" ] || [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; then
    echo "save: папка $DIR пуста, сохранять нечего"
    return 0
  fi
  local state tmp
  state="$(branch_state)"
  tmp="$(mktemp -d)"
  if [ "$state" = "0" ]; then
    git clone -q --depth=1 --branch "$BRANCH" "$REMOTE_URL" "$tmp"
  elif [ "$state" = "2" ]; then
    git init -q -b "$BRANCH" "$tmp"
    git -C "$tmp" remote add origin "$REMOTE_URL"
  else
    echo "ERROR: save: нет доступа к репозиторию" >&2
    exit 1
  fi
  find "$tmp" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp -a "$DIR"/. "$tmp"/
  cd "$tmp"
  git config user.name "signal-bot"
  git config user.email "signal-bot@users.noreply.github.com"
  git add -A
  if git diff --cached --quiet; then
    echo "save: изменений нет"
    return 0
  fi
  git commit -q -m "bot data $(date -u +%FT%TZ)"
  for i in 1 2 3 4; do
    if git push -q origin "$BRANCH"; then
      echo "save: данные записаны в $BRANCH"
      return 0
    fi
    git pull -q --rebase origin "$BRANCH" || { git rebase --abort 2>/dev/null || true; }
    sleep $((i * 3))
  done
  echo "ERROR: не удалось записать данные в $BRANCH за 4 попытки" >&2
  exit 1
}

case "${1:-}" in
  restore) restore ;;
  save) save ;;
  *) echo "usage: $0 restore|save" >&2; exit 2 ;;
esac
