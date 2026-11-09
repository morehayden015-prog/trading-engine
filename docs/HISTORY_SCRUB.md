# Scrubbing old trade data and secrets out of git history (optional — your call)

Push 1 stops tracking these files, but every old commit on GitHub still
contains them. Nothing here has been run.

## What is in the public history

- `trades.json`, `trade_export.csv`, `stats.json`, `open_positions.json` — trade history
- `main.py` — the old dashboard passcode, the default session-signing key, and the default webhook secret
- `test_discord.py` — a full Discord webhook URL
- `claude_desktop_config.json` (and the `- Copy`) — an Obsidian API key
- `ai brain.txt` — an old env template containing the webhook secret

## Do this first, whether or not you scrub

Scrubbing does not un-leak anything: the repo has been public, so assume it was
copied. Rotation is what actually fixes it.

1. Railway: set a new `DASHBOARD_PASSCODE` (long, not 4 digits) and a random `DASHBOARD_SECRET_KEY`.
2. Railway: set a new `WEBHOOK_SECRET`, then update the `secret` field in every TradingView alert message to match.
3. Discord: delete the webhook used in `test_discord.py` and create a new one.
4. Obsidian: regenerate the Local REST API key.

## Easiest option: make the repo private

GitHub → repo → Settings → General → Danger Zone → Change visibility → Private.
No history rewrite, nothing breaks, Railway keeps deploying. This is the
low-risk choice if nobody else needs to read the repo.

## Full scrub (rewrites history)

```
pip install git-filter-repo

:: 1. fresh mirror clone — do NOT run this in your working folder
git clone --mirror https://github.com/morehayden015-prog/trading-engine.git scrub.git
cd scrub.git

:: 2. remove the files from every commit
git filter-repo --invert-paths ^
  --path trades.json --path trade_export.csv --path stats.json --path open_positions.json ^
  --path test_discord.py --path claude_desktop_config.json ^
  --path "claude_desktop_config - Copy.json" --path "ai brain.txt"

:: 3. blank the secrets that lived inside main.py
::    replacements.txt = one line per secret:   OLDVALUE==>REMOVED
git filter-repo --replace-text ..\replacements.txt

:: 4. overwrite GitHub
git remote add origin https://github.com/morehayden015-prog/trading-engine.git
git push --force --mirror origin
```

Then delete both local folders (`C:\Users\Moreh\trading-engine` and
`C:\Users\Moreh\Downloads\trading_engine`) and clone fresh — after copying your
`.env` and any local `.db` files somewhere safe first.

## Risks

- **Every commit hash changes.** Any old clone can no longer push or pull
  normally; pushing from one would put the old history right back.
- **Force-push is irreversible** on GitHub's side. Keep the untouched mirror
  clone (make a copy of `scrub.git` before step 2) until you are sure.
- **Railway** redeploys from the new `main`. Trade data is on the volume, not in
  git, so it is unaffected — but confirm the deploy is healthy afterwards.
- **It is not a guarantee.** Forks, clones, and GitHub's cached commit views can
  keep the old data. To purge GitHub's cached copies you have to contact GitHub
  Support. Files removed from `test_discord.py` etc. also disappear from the
  current tree, so only scrub paths you no longer need.
- The `backup-local-2026-09-26` branch and `fix.bundle` in your local folder
  contain the old history too.
