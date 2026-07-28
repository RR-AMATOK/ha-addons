# Changelog

## 0.3.8

- **Linked accounts now truly share one view.** Fixed the reported bug where
  two linked logins saw the same transactions but *different* budget
  categories (one stuck on the starter template). Three causes, all fixed:
  a device now shows the shared profile immediately after linking (one
  reload, not two); a long-lived app view (like the phone companion app)
  now re-checks who you are whenever it returns to the foreground and pulls
  the shared profile on its own — no reload ritual; and entering What-If
  Mode during startup no longer silently skips the sync (it catches up when
  you exit).
- **Your shared profile can no longer be overwritten by a stale device.**
  If a device with an outdated identity tries to save, the server now
  refuses, the device re-syncs itself, and your real data stays intact —
  previously such a save could silently replace the shared profile with
  old values, with no warning.
- Safety hardening along the way: a failed identity check can never change
  who the app thinks you are mid-session; if a genuine identity change
  displaces unsaved edits, a backup snapshot is taken and a visible notice
  shown; and a background sync will never repaint a field you are actively
  typing in.

## 0.3.7

- **One MAGI, the correct one.** The Tax and Invest tabs now share a single
  MAGI figure computed once by the engine — no more two tabs showing
  different numbers for the same state. The figure now correctly excludes
  §125 insurance premiums and **includes your recurring investment income**
  (interest, dividends, capital gains), so a Roth-IRA "eligible" readout can
  no longer be fooled by wages alone. The ESPP/RSU sale *explorer* is
  deliberately excluded — exploring "what if I sell" moves your AGI estimate
  but never flips your real eligibility.
- **Over the Roth limit? The app warns — it never touches your number.**
  Previously, crossing the MAGI limit silently overwrote your entered Roth
  IRA contribution. Now the field keeps exactly what you typed and shows a
  clear warning ("cap $0 — reduce it or use the backdoor") instead.
- **The Next-Dollar plan follows your actual paycheck.** The waterfall's
  401(k) dollars now split between Traditional and Roth exactly per your
  Tax-tab election (e.g. 80/20) instead of routing everything to the
  strategy's own pick, and the IRA step honors your Roth-IRA/backdoor
  election. The Traditional-vs-Roth recommendation remains — as advice,
  shown next to your current election, never silently overriding it.
- **Your payroll election is visible on the Invest tab** — a line under the
  plan shows exactly what your Tax tab elects, so you can always compare
  the plan against reality.
- **No more silently-frozen projections.** If you ever hand-edited a
  projection row, auto-sync from the Tax tab stopped without telling you.
  Now a hint appears when the Tax tab has moved on, with a one-tap
  "Re-sync from Tax" to catch up.
- Fixed: the What-If banner's "Income base" delta could be wildly inflated
  by a stale cached figure — both banner deltas now derive from the same
  snapshot, so equal contributions mean equal deltas. Also fixed: planner
  contribution rooms stuck at 0 from an old session now self-heal to the
  annual limits when all four are zero.

## 0.3.6

- **Tell the app who you are, once.** The Overview forecast now carries an
  "About you" pair — your age and target retirement age (default 65) — and
  that one setting drives every horizon in the app: the Overview forecast,
  the Invest projections, and the retirement inputs. Charts now read
  "age 65" instead of "+30y", and the horizon adjusts itself.
- **The forecast uses your real numbers.** Current net worth is sourced
  from your Invest accounts and Annual invested from your full plan
  (Budget + Tax contributions) — locked by default with a clear muted
  style, unlockable for manual overrides, with a drift hint if your manual
  value wanders from the source.
- FIRE keeps its own "Target FI age" on purpose — when work becomes
  optional is a different question from when you plan to stop; a note ties
  the two together.
- Formatting audit: fixed a shadow artifact on locked fields, a focus ring
  that stayed blue under the gold theme, and a column misalignment.

## 0.3.5

- **AGI and MAGI in the Tax summary.** A new "Your MAGI" row shows AGI and
  MAGI (Roth IRA) side by side, with the distance to the Roth phase-out and
  a color cue only when you're near the cliff. When they differ, the AGI
  tile shows why ("MAGI + $X investment").
- **FIRE now counts all your saving.** The FIRE tab's annual-savings figure
  was missing your 401k/HSA/Roth payroll contributions — it now includes
  them, so your savings rate and FI window reflect your real plan.
- **The Invest tab pulls from your plan.** The Projection table's Taxable
  contribution can seed from your Budget's investment lines ("Use Budget
  plan"), and the Next-Dollar planner gains "Pull from Tax + Budget" —
  income, state, essential expenses, and your full planned savings fill in
  one tap (everything stays editable; contribution rooms are set to the
  full annual limits so the plan decides what fills them).
- **HENRY Playbook v2 strategy.** The Next-Dollar planner gains a waterfall
  strategy selector: the default order, or the community HENRY playbook
  (match → 6-month emergency fund → HSA → 401k/IRA incl. backdoor →
  mega-backdoor → debts ≥5% → taxable), with its allocation guidance and
  RSU/insurance notes — clearly attributed, not financial advice.

## 0.3.4

- **What-if picker with cross-device parking.** The What-If button now opens
  a picker: jump straight into any saved what-if by name (rename/delete
  inline), start a new one, or resume your **parked** what-if — "Exit — keep"
  now saves it to your account, so a what-if parked on one device can be
  resumed on another.
- **Pick your own category colors.** Every category — including the built-in
  Need/Want/Investment/Travel/Other — can be recolored in Manage Categories,
  and the color applies everywhere (bars, chips, filters, fund labels), with
  one-tap reset to the default.
- **"Counts toward" budget math.** Each category can be assigned to Need,
  Want, or Investment for the 50/30/20 comparison (or kept standalone). The
  framework rows fold assigned categories in ("incl. Car"), so the targets
  finally compare against everything — no more invisible spending.
- **Budget buckets: collapse & reorder.** Minimize any bucket and drag whole
  buckets into your preferred order — useful for keeping two same-kind
  buckets separate.
- **Yearly sinking funds.** Mark a fund (car insurance, annual credit-card
  fees) as renewing yearly: the target date rolls forward automatically and
  the fund lens shows the next renewal plus what you've saved/drawn this
  cycle.
- Fixed: the category autocomplete dropdown was near-unreadable in the light
  theme (it now follows the app's theme).

## 0.3.3

- **A cleaner transactions filter.** The wall of tag chips is gone — one
  compact "Filter" control opens a searchable picker, and the active filter
  (tag, card, or category) shows as a single removable chip. A month with
  no matches now explains the active filter instead of showing an empty
  table.
- **Forms look sharper.** Input fields stand out clearly from their cards in
  both themes (the light theme especially), with a theme-matched focus
  glow, and every input column now starts and ends on the same pixel —
  no more ragged edges from differing unit labels.
- Fixed: the link-code entry box was squeezed to a sliver by its button;
  stacked buttons in the settings panels were offset a few pixels from
  each other; a styling leak drew a double border around inputs.

## 0.3.2

- **Link your accounts.** If one person has multiple Home Assistant logins,
  link them into a single profile: Settings gear → Linked accounts →
  generate a code on the profile you're keeping, enter it from the other
  account within 10 minutes. Both logins then share the same data and
  rights; unlink any time. Codes are single-use and rate-limited; treat a
  live code like a house key — whoever enters it joins the profile that
  issued it.
- **Names instead of ids.** The household roster, transfer-ownership picker,
  and linked-accounts list now show each account's Home Assistant display
  name (with a short id suffix); the owner can rename entries in-app where
  no name is available.

## 0.3.1

- **Sinking funds.** Set aside money monthly for irregular expenses (car
  maintenance, travel): create funds in the Budget tab (with an optional
  target), reserve the monthly amount in your plan with one tap, and link
  real transactions as contributions or draws in Actuals — the fund lens
  shows each reserve building up and how much of a big expense was
  pre-funded.
- **Transfer ownership.** The household's owner seat (all shared data +
  admin actions) can now be handed to another member: Settings gear →
  Transfer ownership. Useful when the wrong account ended up as owner —
  the new owner gets everything instantly, nothing is copied or moved.
- **Consistent selected states.** Everything selected — tabs, chips,
  buttons — now shows white text on the deeper accent fill, in both themes
  (this also fixes a text-contrast failure in the light theme's chips).
- **No more automatic backup downloads.** The one-time profile migration
  now asks: download a backup file, continue without, or cancel — nothing
  downloads unless you choose it (the server still takes its own database
  backup regardless). Also fixed a race that could download the file twice.

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
