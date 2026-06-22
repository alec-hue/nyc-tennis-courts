# NYC Tennis — live court availability

A live, mobile-friendly view of which NYC Parks tennis **locations** have at least
one court open, across the next 14 days — so you don't have to log in and click
into each set of courts one by one.

**Live page:** https://alec-hue.github.io/nyc-tennis-courts/

- `index.html` — the dashboard (filters by day/time, one-tap "copy shareable message")
- `availability.json` — the raw feed it reads (also handy for asking an AI assistant
  "what courts are open Saturday morning?")

The data is regenerated every ~30 minutes by a small job on the owner's Mac
(the source site blocks cloud scrapers, so a real browser-like client refreshes it).
Each entry collapses per-court detail down to "how many courts are open" at a given
date and time. Viewing needs no login; booking does.

Source: [nycgovparks.org/tennisreservation](https://www.nycgovparks.org/tennisreservation/)
