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
- **Sinking funds (since 0.3.1):** for irregular expenses (car maintenance, travel),
  create a fund in the Budget tab with an optional target and reserve the monthly
  amount in your plan with one tap. In Actuals, link a real transaction as a
  contribution to or draw from the fund — the fund lens shows the reserve building up
  and how much of a big expense was pre-funded, so a fully-funded draw doesn't count
  as going over plan.
- **Ledger filter (since 0.3.3):** the Actuals ledger's filter is a single, compact
  **Filter** control instead of a wall of tag chips — open it to search and pick a tag,
  or click a card or category anywhere else in the app to filter the ledger to it. The
  active filter always shows as one removable chip; a month with no matches explains
  the active filter instead of rendering an empty table.
- **Changing jobs mid-year (since 0.7.7):** the **Job change** card on the Tax tab takes
  what the old job has already paid you and what the new one will, and works out the
  blended year — remaining 401(k) and HSA room, the employer match forfeited during an
  eligibility wait, and the Social Security over-withholding that two employers in one
  year cause. **Apply** writes that blended year into the Tax tab.

  > **What Apply does to your budget, and what it no longer does.** The blended year is
  > the right figure to file taxes on and the wrong one to spend: it averages the salary
  > you have left behind across twelve months. Apply therefore *keeps* the take-home you
  > had before pressing it, shows it on the Job change card as **Take-home you get now**,
  > and budgets on that until the new job starts. Correct that field if it is not what
  > actually lands — everything downstream reads it. On the start date the budget switches
  > to the new job's own monthly take-home, and a banner on the Budget tab announces the
  > change in advance. Nothing leaves the Job change card until you press Apply.

  > **What your new paycheck actually looks like (since 0.7.9).** Benefits rarely open on
  > day one, so the rest of the year comes in up to three shapes: nothing deducted yet,
  > then one bucket running, then both. **Your new paycheck** at the bottom of the card
  > gives each shape its own column — which deductions are on, the dates it covers, how
  > many paydays fall inside it, and every payslip line down to what lands. A bucket you
  > are contributing nothing to gets no column. If the plan asks for more than a cheque
  > can carry, the figure goes negative and says so rather than being rounded away. A
  > bonus and a Roth IRA are not on it: one is a separate cheque, the other comes out of
  > your bank, and neither is withheld from a payday.

  > **Paycheck counts.** Contribution targets are per-paycheck, so they depend on how many
  > paydays are left in the year. Those are counted on your employer's calendar — including
  > paydays moved earlier because they fall on a weekend or federal holiday, and the
  > 1 January cheque that is really paid on 31 December and belongs to the earlier tax
  > year. The card states the pay schedule it assumed; if your employer pays on different
  > days than it guesses, the count can be one out.

- **Multiple household users:** the panel opens to every Home Assistant user in the
  household, not just admins (since 0.2.1). **Since 0.3.0, each household member has
  their own data** — transactions, accounts, budget, goals, ventures, sinking funds,
  and tax/plan inputs are all scoped to the person who's logged in and synced to their
  profile server-side, instead of one shared dataset with browser-local settings. Open
  Finance as yourself and you see (and can only change) your own numbers.

  > **Owner-only actions.** **Restore**, both backup downloads (full and Actuals only),
  > and the transactions CSV export are restricted to the household **owner** — other
  > members won't see those buttons and get a friendly refusal if they try the
  > underlying API directly. The full backup is labeled **household-full**: it contains
  > every member's data, not just the owner's.

  > **Linked accounts (since 0.3.2).** If one person has more than one Home Assistant
  > login (e.g. two admin accounts), link them into a single profile instead of ending
  > up with two separate personas: Settings gear → **Linked accounts** → generate a
  > code on the profile you're keeping → enter it from the other account within 10
  > minutes. Both logins then share the same data and rights; unlink any time from
  > either side. Codes are single-use and rate-limited — treat a live code like a house
  > key, since whoever enters it joins the profile that issued it. The account
  > currently holding the owner seat can't be linked away from itself (transfer
  > ownership first if you need to link it). The household roster and the
  > transfer-ownership picker show each account's Home Assistant display name (with a
  > short id suffix) rather than a raw id; the owner can rename entries in-app where no
  > name is available.

  > **Note — the first person in becomes the owner.** With `panel_admin: false`,
  > whichever household member opens **Finance** first becomes the owner — the
  > only one who can Restore, download backups, or export CSV. Everyone who
  > opens it afterward is a member. If it matters who ends up owner, have that
  > person open Finance first and confirm it in Settings (or have an admin check
  > `/api/whoami`) before giving other household members access.
  >
  > **Changed your mind? Transfer ownership any time.** Settings gear →
  > **Transfer ownership…**, pick another household member from the list (they
  > must have opened Finance at least once so the app knows they exist), and
  > confirm. After transferring, close or refresh any other tabs you had open
  > as the old owner — only the tab you acted in reloads itself; stale tabs are
  > harmless (owner actions fail) but can look confusing until refreshed.
  > Because the owner's data lives in a household-wide slot rather than
  > under any one person's account, the new owner immediately has everything the
  > old owner had — every transaction, account, backup, and admin action — with
  > nothing copied or moved. The old owner becomes an ordinary member with a
  > fresh, empty workspace of their own. **One nuance:** if the person you're
  > promoting had already been using the app as a member (their own logged
  > transactions or settings), that data is *not* merged in — it's left in place,
  > simply inaccessible while they hold the owner seat, and reappears exactly as
  > it was only if the seat is ever transferred back to a member role for them.
  > There's no merge tool for this; it's a rare edge case (most people promoted
  > to owner haven't used the app as a member first) but worth knowing about.
  > This cannot be undone from within the app except by transferring the seat
  > back the same way.

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
