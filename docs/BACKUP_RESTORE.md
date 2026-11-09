# Trade DB: backups and restore

The live database is `DB_PATH` (`/app/data/trades.db` on the Railway volume).

## What gets backed up, and where

| Backup | When | Where | Name |
|---|---|---|---|
| Pre-migration | Automatically, before any pending schema migration runs at startup | Same folder as the DB | `trades.db.bak-YYYYMMDD-HHMM` |
| Daily | Once per UTC day (and once right after each startup) | `BACKUP_DIR` (default `/app/data/backups`) | `trades-YYYYMMDD.db` |
| Pre-dedupe | When `/admin/cleanup-dupes/preview` is called | Same folder as the DB | `trades_backup_*.db` |

Settings (Railway → service → Variables, all optional):

- `BACKUP_DIR` – folder for daily copies. Default `<DB folder>/backups`.
- `BACKUP_KEEP` – how many daily copies to keep. Default `30`. `0` = keep all.

Pre-migration backups are never pruned.

**Important limitation.** Railway gives a service one volume, so by default both
copies live on that same volume. That protects against a bad migration, a bug,
or an accidental delete — it does **not** protect against losing the volume
itself. For that, pull a copy to your own computer every so often (below).

## See what backups exist

```
curl -H "X-Admin-Token: <ADMIN_SECRET>" https://<your-app>.up.railway.app/admin/db-status
```

Look at the `backups` list. The same response shows the DB path, whether its
folder is a mounted volume, which migrations have run, the current baseline
numbers, and `history_intact` (every stored baseline re-checked against
today's data).

## Download a copy to your computer (the real off-site copy)

```
curl -H "X-Admin-Token: <ADMIN_SECRET>" -o trades-20261109.db ^
  https://<your-app>.up.railway.app/admin/backups/trades-20261109.db
```

## Restore a backup

1. Pick the file name from the `backups` list (e.g. `trades-20261108.db` or
   `trades.db.bak-20261109-2230`).
2. In Railway → service → Variables, add `RESTORE_FROM=<that file name>`.
   Railway redeploys.
3. On startup the bot checks the backup's integrity, moves the current DB aside
   to `trades.db.replaced-<timestamp>` (it is kept, not deleted), copies the
   backup into place, then starts normally. The log line is
   `DB RESTORED from ...`.
4. Check `/stats` and `/admin/db-status`.
5. Remove the `RESTORE_FROM` variable. (If you forget, nothing happens: each
   value is only ever applied once.)

If the file name is wrong or the backup is corrupt, startup stops with an error
and the live DB is left untouched.

To undo a restore, do the same thing with the `trades.db.replaced-<timestamp>`
file name — it shows up in the `backups` list too.

## If a migration fails

The bot restores the pre-migration backup by itself, keeps the failed copy as
`trades.db.failed-<timestamp>`, writes `trades.db.migration-failed`, and refuses
to start (so it cannot loop). Fix the code, then set `MIGRATION_RETRY=1` for one
deploy.

## Check history by hand

```
python migrations.py status     # applied migrations + current baseline
python migrations.py verify     # re-check every stored baseline; exit code 1 if any old trade changed
```
