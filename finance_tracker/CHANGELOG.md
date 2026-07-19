# Changelog

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
