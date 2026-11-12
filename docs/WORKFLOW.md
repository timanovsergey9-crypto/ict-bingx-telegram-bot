# Workflow for the Actions maintainer

The `gh` token used to open this PR does not have the `workflow` scope, so
`.github/workflows/run-bot.yml` could not be updated automatically.

Please replace `.github/workflows/run-bot.yml` with `docs/run-bot.yml.recommended`
(GitHub UI or a token that has the `workflow` scope).

The recommended file:
- keeps cron `5 */4 * * *` (aligned with 4h closes)
- is a short one-shot (`timeout-minutes: 20`) with `cancel-in-progress: false`
- runs `python bot.py --selftest` then `python bot.py --scan-once`
- restores/saves `data/plus_one_state.json` via `bot-data`

Until that is applied, the **existing** workflow still works: it runs
`python bot.py`, and the new `bot.py` is a one-shot Plus One scan that
exits in about a minute. The 4-hour timeout and cancel-in-progress are
harmless for a one-shot. Do not set a Kill Zone. Secrets stay as they are
(`TELEGRAM_TOKEN`, `CHAT_ID`).
