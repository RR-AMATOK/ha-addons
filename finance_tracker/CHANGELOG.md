# Changelog

## 0.3.0

- **Your plan now follows you across devices.** Tax inputs, budget setup, FIRE
  assumptions, and categories are stored in your personal server-side profile
  (per household member) instead of living only in one browser. The first time
  you open the app after this update, a one-time migration runs: it saves a
  backup file to your device and asks you to confirm before anything syncs.
  Keep that file until you're satisfied everything looks right.
- **Restore is now guarded.** Restoring a backup that would replace newer data
  warns you first, with a real Cancel; if a sync ever does replace newer data
  from another device, a visible notice appears and the previous version is
  kept one level back.
- Hardening under the hood: backup restores can no longer rewind profile
  version history (the cause of "old values coming back" during testing), and
  malformed backup files are rejected cleanly.
- Opt-out lever: add `?profiles=0` to the URL to run a session the old way
  (browser-local only).

## 0.2.2

- **Each household member now has their own data.** Transactions, accounts,
  budgets/plans, goals, ventures, and scenarios are scoped per user. All
  existing data belongs to the owner; other members start with a clean,
  empty dataset the first time they open the app after this update. (The
  one-time migration runs automatically at update; a safety backup of the
  database is taken first.)
- **The app is now usable on phones.** Cards, forms, and the budget builder
  fit narrow screens (no more content cut off at the right edge); wide
  tables scroll within their own cards; tap targets are finger-sized. Fixes
  apply across all widths below 900px, in both themes.
- The full-database backup (owner-only) is relabeled "household-full" — it
  contains every member's data.

## 0.2.1

- **The panel now opens to every household user, not just admins**
  (`panel_admin: false`). Anyone with a Home Assistant login can open
  **Finance** in the sidebar.
- To keep that safe, a minimal identity guard was added ahead of full
  per-user profiles: **Restore**, both backup downloads (full and Actuals
  only), and the transactions **CSV export** now require the household
  owner. Other members see those buttons hidden and get a friendly
  "owner only" message if they hit the API directly. Everything else —
  the planning tools, What-If Mode, Print, the theme picker — stays open
  to everyone.
- **Important:** the app does not yet have per-user data. Until per-user
  profiles ship, all household members share one dataset — a member's
  transactions land in the same actuals ledger as everyone else's, and
  plan/tax inputs remain per-device (stored in each browser, not per-user
  on the server). Treat this release as shared-household access, not
  multi-user separation. One known limitation of the same guard: because
  **Back up** bundles the owner-only full/Actuals backups with the
  client-only "Settings only" backup, members can't currently download
  their own settings-only backup file — their plan/tax inputs are still
  safe in their browser, they just can't export them until per-user
  profiles land.

## 0.2.0

- **New pill-style tab navigation.** The active tab now shows as an accent
  pill, making the current section clearer at a glance.
- **Consolidated header.** Brand, tabs, and actions now share a single row.
  A new **Settings** (gear) menu holds Print/PDF, Restore, Reset, and a
  Show/Hide-disclaimer toggle; **Back up** and **What-If Mode** stay
  top-level for quick access.
- Fixed the header split-button's hover/focus outline: the caret half was
  missing its left border, so the accent ring never closed around it. Both
  halves now keep full borders (the caret still overlaps seamlessly) and the
  hovered/focused half draws its ring on top.
- **Dismissible disclaimer banner** that remembers you've dismissed it,
  plus a permanent one-line disclosure in the footer so the notice is
  never fully gone.
- **Typography standardization** — a consistent type scale across the app
  for a cleaner, more readable layout.
- **Home Assistant theme support.** The add-on now adopts a bundled theme,
  defaulting to the "Shiro" full-light theme. A new theme picker in
  Settings lets you switch between Full light, Shiro accent, and Classic
  dark, and remembers your choice. Falls back to the classic dark palette
  if no theme is present.

## 0.1.3

- Backup controls moved to the header: **Back up** still downloads the full
  backup in one click; the new **▾** menu next to it holds the selective
  backups (Actuals only / Settings only) and the transactions CSV export.
  The copies in Actuals → Manage are gone — one home for everything.

## 0.1.2

- **One-file backup & restore.** The header's new **Back up** button exports
  everything (settings + actuals) in a single file; **Restore** accepts it —
  plus every older backup format (nothing is stranded). Restore applies the
  database first and only touches browser settings after it succeeds.
- Backup now includes previously-missed settings: FIRE assumptions, custom
  categories, projected accounts, and the max-out planner.
- Selective backups (Actuals only / Settings only) from Actuals → Manage.
- Transactions **CSV export** with a date range (analysis/tax-prep; not a
  backup — deliberately not restorable).

## 0.1.1

- Add-on icon and logo.
- Backup app tag renamed to `financial-planning-suite` (matches the repo rename).
  Backups exported with the old `income-tax-calculator` tag still restore — forever.
- Now distributed via the public add-on repository
  [RR-AMATOK/ha-addons](https://github.com/RR-AMATOK/ha-addons) for one-click
  install and updates.

## 0.1.0

- Initial release: full app behind HA ingress (no published ports, no host
  mounts), private `/data` SQLite, `backup: cold`, `/health` watchdog,
  MQTT service discovery prepared for P1 (unused in this version).
