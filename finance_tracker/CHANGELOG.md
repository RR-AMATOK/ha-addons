# Changelog

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
