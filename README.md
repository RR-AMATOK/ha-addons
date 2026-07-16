# RR-AMATOK Home Assistant Add-ons

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FRR-AMATOK%2Fha-addons)

Or add manually: **Settings → Apps → App store → ⋮ → Repositories** → `https://github.com/RR-AMATOK/ha-addons`

## Add-ons

### Finance Tracker

Personal income-tax, budget, and plan-vs-actual tracker (FastAPI + SQLite behind
HA ingress — no published ports, no host mounts). Your financial data lives only
in the add-on's private `/data` volume on your own device; this repository carries
the built application bundle and **never any data**.

Source is developed in a separate private repository; this repo is the
distribution channel HA's add-on store follows.

> **Not financial, tax, investment, or legal advice.** All figures are estimates
> for planning purposes.
