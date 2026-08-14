# Changelog

## 0.6.2

- **Money reads like money everywhere.** The tax-bracket boxes showed bare digits — `640600`
  instead of `640,600` — because they were the one kind of field that cannot display a
  separator. Every money box in the app now carries its commas. Every one was checked, not
  just the ones on screen.
- **Fields line up.** Labels beside a date box sat a few pixels off the ones beside a text box,
  because date fields were the only control still using the browser's own styling. Every field
  is now the same height, so rows read as rows.
- **Room to breathe.** The FIRE assumptions panel had no space at all between its edge and its
  text; the small "Match 50% of first 6%" boxes were tight enough to crowd two digits against
  the arrows. Both have proper room now, and one field that was clipping its own hint text is
  wide enough for it.
- **Easier to tap.** The dismiss crosses and the bucket collapse arrows were around 13-16 pixels
  square on a phone, below the accessibility minimum. The symbols look the same; the area
  around them that responds to a tap is bigger.
- **Sharing an expense inside a what-if.** Creating a shared line is deliberately not allowed
  inside a what-if — it would write real household data from a hypothetical. But the "Share…"
  links stayed on screen anyway, so the only way to find out was to click one and be told no.
  They are now hidden while a what-if is open, and come back when you leave.
- **Leaving a what-if actually leaves.** Choosing **Exit — keep** immediately asked "Resume this
  what-if?" with a Resume and a Discard button, so the only thing that cleared the screen was
  Discard — which throws the sandbox away. You already chose to keep it, so the app no longer
  asks again. Your what-if is still there: it is offered the next time you open the app, and
  the picker's "Parked" entry resumes it from any device.
- **Screen readers can name two more fields.** The current-age and retirement-age boxes on
  Overview sit inside a sentence, which reads fine on screen and said nothing out loud.

## 0.6.1

- **Deleting an account works again.** The ✕ next to an account did nothing at all —
  not even the confirmation prompt. When Accounts moved onto the Setup tab in 0.6.0
  the button came with it but its wiring did not, so the click reached nothing. Every
  other control on that row (edit, save, cancel, type, "Fix") kept working, which is
  why it looked like only delete was broken.
- **Updating an investment balance works again.** Typing a figure into an account's
  "new balance" box and pressing **update** silently did nothing — no saved balance,
  no error, no message. The button and the code meant to handle it were referring to
  each other by two different names. Balances again save a dated snapshot, so the
  net-worth trajectory and FIRE progress stay honest.

- **Balances saved in the evening are dated correctly.** A balance updated after about 7pm
  was stored with tomorrow's date and then displayed as "dated ahead", because the app
  wrote the date in UTC and read it back in your own timezone. The same slip affected a
  venture's start date and the 12-month CSV range. Every date the app calls "today" now
  means today where you are.

Both were silent failures: nothing appeared to go wrong, the thing you asked for
simply did not happen. Both now have tests that drive the real controls in a real
browser and check the server afterwards, so neither can come back unnoticed.

## 0.6.0

- **Every tab has a contents strip now.** It sits under the tab bar, tells you what
  is on the tab, and carries the number that matters — "Accounts 7 · $190,637 ·
  2 stale", "House $5,541/mo", "Car $813/mo" — so you can read the answer without
  opening the screen. On the bigger tabs it is a switcher: click a name and you go
  there instead of scrolling past everything else.
- **The big tools got their own screen.** Tax, Budget, Invest and Decisions were
  each one long scroll of everything at once. Now Invest is Accounts / Next dollar /
  Projection, Decisions is House / Car, and Tax splits its tables and comparisons
  out. One job per screen, the whole width for it.
- **The answer comes first.** House, Car and the next-dollar plan all used to put
  twelve or nineteen input fields above the conclusion you came back for. The
  verdict is now at the top and the inputs sit under it.
- **Settings moved out of the way.** Categories, spelling aliases, budget
  strategies, accounts and import/snapshots now live on their own **Setup** tab
  instead of hiding inside the tabs you use for daily work.
- **Fewer things hidden behind a click.** Sections that had no reason to be
  collapsed no longer are — the tax tables, the house loan assumptions, and the
  car's fine print are all just visible. The ones still worth collapsing now say
  what is inside them before you open them ("6.5% · 30 yr · 20% down", "2 values
  edited"), so you can decide whether to bother.
- **Your account balances now show their age.** A balance reads "2 weeks ago" or
  "4 months ago" instead of a raw date, and anything older than about a quarter is
  flagged. If any are stale, a line at the top of Accounts says so plainly, because
  your net worth and FIRE progress quote those figures with no hint of how old they
  are.
- **Ventures moved to Actuals, and tells you when it has gone quiet.** It lives
  next to the transactions that feed it now. A venture with no activity for three
  months is marked, and it stops projecting a payback date off dead data — it used
  to keep saying "a few months to breakeven" forever, slowly stretching, long after
  you had stopped tracking it.
- **Car: pick your own "keep it" years.** 3, 5 and 10 are still there; type any
  number from 1 to 40 and it works the comparison out properly for that year rather
  than guessing between the others. Years you add stick around.
- **Numbers stopped getting cut off.** Tax bracket amounts were rendering as
  "124(" in a box too narrow for them. Fixed, and the tables now line up in one
  column you can scan.
- **Wide screens are used properly.** Cards that stretched to fill a large window
  were leaving labels a thousand pixels from the boxes they belonged to, and long
  paragraphs running the full width of the screen. Text and form fields now hold a
  readable width; tables and charts still get all the room they need.

## 0.5.0

- **Share expenses with someone you live with.** A shared line is a real record you
  both see: you each keep your own budget, and your own share of the rent folds into
  it. Split it by percentage or by a flat amount, or pool contributions toward one
  total. Open **Share expenses with your partner** on the Budget tab, tick the
  categories, and share them in one go — grouped by bucket, with a select-all per
  category, rather than one line at a time.
- **Sharing something no longer moves it.** A subscription shared out of Basic
  Wants › Subscriptions used to reappear at the top of Needs, because placement was
  guessed from the bucket *type* and two buckets can share a type. Shared lines now
  keep the bucket and sub-group they came from, and you can drag them anywhere you
  want them — including into a sub-group.
- **You can un-share something.** There was no way to undo it. "Remove from shared"
  now hands the money back as your own budget line at the amount you had, instead of
  deleting it and quietly leaving a hole in your plan.
- **Split by income, set once.** Enter both incomes in one place and it suggests a
  proportional split you can save as a preset — instead of retyping incomes on every
  expense. Your own income comes from the Tax tab; your partner's is typed by you and
  is never read from their data.
- **The split adds up before you commit.** A split that does not total 100% now says
  so as you type, with how far off you are, instead of accepting the form and
  refusing at the button.
- **Your share of a promoted line is correct.** Turning a 65/35 line into a shared one
  recorded the *whole* bill as your contribution — $2,000 where $1,300 was true. That
  inflated your Needs bucket, your remaining balance and your 50/30/20 verdict, and
  the original figure could not be recovered afterwards.
- **A shared line can no longer be taken over.** Anyone on a shared line could rewrite
  its shares and zero out the other person, archive it, or restate the total — through
  a path the owner-only delete check never saw. The owner now controls the line; a
  participant can adjust what they contribute.
- **What-If no longer restores split percentages onto the wrong line.** Budget items
  were tracked by their position in a list, so adding, deleting or reordering anything
  could restore a saved split onto a different line — or drop it — without a word.
  Items now carry a permanent identity, and anything that cannot be matched is
  reported by name instead of swallowed.
- **First run says the numbers are an example.** The app ships with a worked example
  so you can see it compute, but nothing said so — you could act on a tax bill and a
  30-year forecast built from figures that were never yours. It now says so plainly,
  with one tap to clear them.
- **Budget feedback grades the month, not you.** The verdict now reads "Investing
  $1,420/mo — $340 above the 20% target" rather than judging your rate, and bad news
  carries a next step: how much a day gets you back on plan, or — when fixed bills
  already exceed the plan — that spending less day to day cannot fix it.
- **Nothing runs off the side of the screen.** The Budget tab pushed the page sideways
  on narrow and half-width windows, including inside Home Assistant's own sidebar
  layout. Wide content now scrolls inside its own panel.
- **Easier to see and to operate by keyboard.** Field borders, the "no spend" rows and
  several payment controls were too faint to make out; the tab bar told screen readers
  nothing about which tab was selected; delete and edit icons announced as "cross
  mark" with no idea which row. All fixed. The **$** beside an amount also stopped
  looking like a button — only the **/yr** chip is clickable, and now only it looks it.
- **Linked accounts warns you what it does.** It merges two sign-ins into one profile
  and hands over your whole workspace, which is not partner-sharing — and it was one
  click away from someone looking for exactly that.

## 0.4.0

- **Refunds now subtract instead of adding.** Marking a transaction as a refund
  showed the money coming back on the ledger row, but every category total still
  counted it as spending — so a category you'd been refunded could read wildly
  over budget. Golf, for example, showed $296.93 against a $200 budget (148%)
  when the truth was a net refund of $149.93. Category totals, the recurring
  matcher, and partner shares all handle refunds correctly now, and a category
  that ends the month net-negative renders properly instead of drawing a full bar.
- **Your accounts have real balances.** Each account now carries a running
  balance driven by the money you actually log, anchored to a balance you enter
  once. Credit cards gained a credit-limit field, and the app reconciles what it
  computed against what you tell it, to the cent.
- **Cards show how much of your limit you're using.** The "Pay off your cards"
  section now draws a utilization meter for any card with a limit set — calm
  under 30%, warning above 70% — and it updates the moment you save a limit.
- **A one-tap fix for miscategorised debts.** Cards and loans created before
  balances existed were recorded as assets, which made them count the wrong way
  in your net worth (an Amex reading −$2,588 rather than money owed). Any
  affected account now shows "Recorded as an asset — Fix"; tapping it corrects
  the stored record, not just the display.
- **The Manage panel is no longer a wall of live forms.** Categories and accounts
  share one aligned layout with their actions anchored to the right edge instead
  of landing somewhere different on every row. Accounts are readable rows you
  edit one at a time, rather than six always-on controls each. Deleting an
  account now warns you first — and says plainly that it also deletes that
  account's transactions and snapshots.
- **Accounts without a balance stop guessing.** Instead of showing a meaningless
  lifetime sum of everything ever logged, they invite you to set a starting
  balance.
- **The category, payee and tag suggestion lists are readable again.** The
  browser's built-in dropdown couldn't be styled — its highlighted row was
  effectively invisible in the light theme — so it's been replaced with our own,
  with full keyboard support and proper contrast in both themes.
- Polish: the payment Amount box no longer renders twice the width of the Date
  field next to it.
- Under the hood: refunds can no longer be applied to a split transaction
  through the edit path (creating one was already blocked), and the app's
  account-balance maths gained a full test matrix covering every combination of
  account type, debt flag and balance anchor.

## 0.3.9

- **"Am I on track?" now understands your sinking funds.** Spending drawn
  from a fund you saved up for (car repair, annual insurance) no longer
  reads as overspending — the monthly verdict excuses the funded part,
  attributed to the bucket where the money was actually spent, with a
  breakdown line showing what the funds covered.
- **What-If scenarios grew a details drawer.** Open it from the banner:
  rename the scenario, set its **activation month** explicitly (previously
  it silently assumed next month), and see the rest-of-year catch-up pace
  for your accounts and goals. The banner's delta readouts are now
  tappable chips that jump straight to the Tax, Budget, or FIRE tab.
- **Plan goals and funds inside a What-If.** Add "planned" goals and
  sinking funds while exploring a scenario — they're clearly badged, touch
  nothing real, and survive parking/resuming. Activating the scenario
  creates them for real; reverting it cleanly removes exactly what it
  created (anything you've since touched is archived instead of deleted,
  and pre-existing items with the same name are never affected).
- **What-If exits got tidier:** investment figures you typed while
  exploring no longer linger after a discard, and the two-device sync
  notice now explains itself in plain language.
- Polish: the Overview tile is now labeled "Tax-advantaged invest/yr" (it
  never included regular brokerage investing), the Budget catch-up notice
  and pop-up shadows follow your theme properly, and the partner-share
  hatching re-themes in light mode.
- Under the hood: the developer identity override is now structurally
  disabled in the production image (it was already inert behind Home
  Assistant's ingress — now it's impossible).

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
