# Finance Tracker — Home Assistant add-on

Personal income-tax, budget, and plan-vs-actual tracker. The full web app runs as a
self-contained add-on: FastAPI + SQLite inside this container, UI in the HA sidebar
via ingress. **Your financial data lives only in this add-on's private `/data`
volume** — it is never sent anywhere and is captured by HA's normal backups.

## Install — Option A: add-on repository (recommended)

1. In HA: **Settings → Apps → App store → ⋮ (top right) → Repositories.**
2. Add `https://github.com/RR-AMATOK/ha-addons` and close the dialog.
3. "Finance Tracker" appears in the store — install it (the image builds on the
   device from the bundled Dockerfile; a few minutes on a Yellow, all-wheel install).
4. Start it, enable the **Watchdog** toggle, and open **Finance** in the sidebar.

Updates then arrive through the normal **Check for updates** flow whenever a new
version is pushed to the repository (`sh addon/publish_repo.sh` from the dev machine).

> If a copy was previously installed as a *local* add-on (Option B), uninstall it
> first and delete `addons/finance_tracker/` from the HA host — HA treats the local
> and repository copies as two different add-ons with separate `/data`.

## Install — Option B: local add-on copy (no GitHub involved)

1. On your dev machine: `sh addon/build_bundle.sh` → produces `addon/dist/finance_tracker/`.
2. Install the **Samba share** add-on in HA (or use SSH), open the `addons` share.
3. Copy the whole `finance_tracker` folder into `addons/` on the HA host.
4. In HA: **Settings → Apps → App store → ⋮ (top right) → Check for updates.**
   "Finance Tracker" appears under *Local apps* — install it.
5. Start it, enable the **Watchdog** toggle, and open **Finance** in the sidebar.

## Security posture (do not change these)

- **No network ports are published.** The only way in is HA's ingress proxy, behind
  your HA login (incl. 2FA). The `/api/*` surface is deliberately unauthenticated
  *inside* the container because ingress is the sole route to it.
- **No host directories are mapped.** The add-on cannot see `/config` or HA's own
  database; its SQLite lives in the private `/data` volume
  (`/mnt/data/supervisor/addons/data/local_finance_tracker/` on the host).
- `backup: cold` — HA stops the add-on for a few seconds during backups so the
  SQLite copy inside the backup is always consistent.

## Using the app

- **Back up** and **What-If Mode** stay as top-level buttons in the header for quick
  access. Everything else lives behind the **Settings** gear (top-right of the header):
  **Print / PDF**, **Restore** (from a backup file), a **Show/Hide disclaimer** toggle,
  and **Reset**.
- **Theme:** the add-on ships with a bundled "Shiro" theme and displays in **full light**
  by default. Open Settings to switch between **Full light**, **Shiro accent** (dark app,
  gold accent), and **Classic dark** — your choice is remembered in the browser. If no
  theme is bundled, the app falls back to the classic dark palette automatically.
- **Multiple household users:** since 0.2.1 the panel opens to every Home Assistant
  user in the household, not just admins. **Restore**, both backup downloads (full and
  Actuals only), and the transactions CSV export are restricted to the household
  **owner** — other members won't see those buttons and get a friendly refusal if they
  try the underlying API directly.

  > **Note — one shared dataset.** This add-on does not yet have per-user profiles.
  > Every household member who opens the panel is reading and writing the **same**
  > actuals ledger — if a member logs a transaction, it lands in the same shared data
  > as everyone else's, with no per-user separation. Plan and tax inputs are stored
  > per-device (in each browser), not per-user on the server. Treat this as one
  > household account shared by everyone with access, not a multi-user tool with
  > individual data — that's planned for a later release. One side effect: the
  > **Back up** button is owner-only in 0.2.1 (it bundles the full/Actuals
  > backups with the per-device "Settings only" backup), so members can't yet
  > download their own settings-only backup file — their plan/tax inputs are
  > still safely stored in their browser, they just can't export them until
  > per-user profiles land.

  > **Note — the first person in becomes the owner.** With `panel_admin: false`,
  > whichever household member opens **Finance** first after updating to 0.2.1
  > becomes the permanent owner — the only one who can Restore, download
  > backups, or export CSV. Everyone who opens it afterward is a member, and
  > there's no in-app way to reassign owner in 0.2.1. If it matters who ends up
  > owner, have that person open Finance first and confirm it in Settings (or
  > have an admin check `/api/whoami`) before giving other household members
  > access.

## Data & backups

- HA's native backups (Settings → System → Backups) include this add-on's `/data`
  automatically. **Those backups contain your financial data** — protect/encrypt them
  like the data itself.
- The app's own **Download backup** button (Actuals → Manage) still works and is the
  portable JSON escape hatch independent of HA.
- Migrating from local dev: use Download backup on the dev machine, then Restore from
  backup inside the add-on UI. (The SQLite file itself can also be copied into the
  add-on's /data with the add-on stopped.)

## Updating

- **Repository install (Option A):** bump `version:` in `addon/config.yaml`, run
  `sh addon/publish_repo.sh`, then in HA: App store → Check for updates → Update.
- **Local install (Option B):** re-run `build_bundle.sh`, re-copy the folder over
  `addons/finance_tracker/` (bump `version:` first), then Check for updates → Update.
- **Updating the bundled theme (maintainer):** replace `homeassistant/theme/masai.yaml`
  in the repo, then run the normal publish flow (`sh addon/publish_repo.sh`). It rebuilds
  the bundle and regenerates `theme.json` from that YAML automatically — no manual JSON
  editing, and the add-on image itself never needs a YAML parser since the conversion
  happens at build time.

## Recommended on the Yellow

Settings → System → Storage → **Move data disk** to the NVMe SSD (moves HA's recorder
AND all add-on data off the eMMC — best single reliability upgrade).

## MQTT sensors (coming in the next phase)

The add-on already discovers your Mosquitto broker (`mqtt:want`), but sensor
publishing ships in P1 and will be **off by default** (`enable_mqtt` option).
