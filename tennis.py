#!/usr/bin/env python3
"""
NYC Parks tennis availability — aggregated.

Shows which tennis court LOCATIONS have at least one court free, so you don't
have to log in and click into each set of courts one by one.

Source: https://www.nycgovparks.org/tennisreservation/  (public, no login to view)

The site renders the full 30-day grid for each location on one page, so a single
request per location gives complete availability. This tool fetches all locations
in parallel, collapses the per-court detail down to "how many courts are open at
this date/time", applies your filters, and prints a scannable summary and/or an
HTML dashboard with one-click booking links.

Examples:
  ./tennis.py                                  # everything, next 30 days
  ./tennis.py --weekends --after 8am --before 8pm
  ./tennis.py --days 7 --locations "central,riverside"
  ./tennis.py --dow sat,sun --after 9am --html board.html --open
  ./tennis.py --json > slots.json              # machine-readable

Standard library only. No dependencies, no install.
"""

import argparse
import concurrent.futures
import datetime as dt
import html as htmllib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import webbrowser

BASE = "https://www.nycgovparks.org"
LIST_URL = BASE + "/tennisreservation/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Used only if the live location list can't be fetched. IDs from the site.
FALLBACK_LOCATIONS = {
    2:  ("Riverside Park (119 St)", "Manhattan"),
    3:  ("Riverside Clay TA at Riverside Park (96 St)", "Manhattan"),
    4:  ("Mill Pond Park", "Bronx"),
    7:  ("Commonpoint Tennis at Alley Pond Park", "Queens"),
    9:  ("Sportime at Randall's Island", "Manhattan"),
    11: ("McCarren Park", "Brooklyn"),
    12: ("Central Park", "Manhattan"),
    13: ("Sutton East at Queensborough Oval", "Manhattan"),
}

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(\d{4})")
TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
TIME_RE = re.compile(r"<td[^>]*>\s*<strong>(.*?)</strong>\s*</td>", re.S)
CELL_RE = re.compile(r'<td class="(status\d)">(.*?)</td>', re.S)
# Booking path varies by location: /tennisreservation/reserve/123 for most,
# /tennisreservation/reservecp/123 for Central Park. Capture the whole path.
RESERVE_RE = re.compile(r'href="(/tennisreservation/reserve[^"]+)"')
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
HAVE_CURL = shutil.which("curl") is not None
_SSL_CTX = ssl.create_default_context()


def _looks_real(body):
    """A real reservation page is tens of KB; the WAF challenge is ~2KB."""
    return len(body) > 8000 and "tennisreservation" in body


def _fetch_curl(url):
    # curl presents a browser-like TLS profile, so it passes the site's WAF
    # where Python's urllib gets served an HTTP 202 challenge stub.
    cmd = ["curl", "-s", "-k", "--compressed", "--max-time", "30", "-A", UA]
    for k, v in HEADERS.items():
        if k != "User-Agent":
            cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, timeout=45)
    return p.stdout.decode("utf-8", "replace")


def _fetch_urllib(url):
    global _SSL_CTX
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        # macOS system Python often lacks a usable cert store. Public,
        # read-only site, so fall back to an unverified context.
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            _SSL_CTX = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
                return r.read().decode("utf-8", "replace")
        raise


def fetch(url, attempts=3):
    """Fetch a page, working around the site's bot-challenge WAF.

    Tries curl first (passes the WAF), then urllib. Retries with backoff when
    the response is a challenge stub rather than the real page. Raises rather
    than returning an empty body, so a blocked fetch is never mistaken for
    'no availability'.
    """
    methods = ([_fetch_curl] if HAVE_CURL else []) + [_fetch_urllib]
    best = ""
    for i in range(attempts):
        for method in methods:
            try:
                body = method(url)
            except Exception:
                body = ""
            if _looks_real(body):
                return body
            if len(body) > len(best):
                best = body
        time.sleep(0.5 * (i + 1))
    if _looks_real(best):
        return best
    raise RuntimeError(
        "site returned a bot-challenge instead of the page (try re-running)")


def parse_time_to_minutes(label):
    """'6:00 a.m.' -> 360 ; '1:30 p.m.' -> 810. Returns None if unparseable."""
    s = label.lower().replace(".", "").replace(" ", " ").strip()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(a|p)m", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "p" and h != 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return h * 60 + mi


def fmt_time(minutes):
    h, mi = divmod(minutes, 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d} {ap}" if mi else f"{h12} {ap}"


def discover_locations():
    """Return {id: (name, borough)} from the live site, or the fallback."""
    try:
        html = fetch(LIST_URL)
    except Exception:
        return dict(FALLBACK_LOCATIONS)
    locs = {}
    for tr in ROW_RE.findall(html):
        m = re.search(r"availability/(\d+)", tr)
        if not m:
            continue
        cid = int(m.group(1))
        name_m = re.search(r"<strong>(.*?)</strong>\s*,\s*([^<]+?)\s*<br", tr, re.S)
        if name_m:
            locs[cid] = (strip_tags(name_m.group(1)), strip_tags(name_m.group(2)))
    return locs or dict(FALLBACK_LOCATIONS)


def parse_availability(html):
    """Parse one location page -> list of slot dicts (one per date+time row).

    Each entry: {date, dow, minutes, time, courts_total, courts_open, reserve_path}
    courts_open collapses the per-court detail to a single count.
    """
    out = []
    for tm in TABLE_RE.finditer(html):
        table = tm.group(1)
        cells_present = CELL_RE.search(table)
        if not cells_present:
            continue  # not an availability grid (e.g. the month calendar)
        # The date label is the nearest "Month DD, YYYY" before this table.
        preceding = html[:tm.start()]
        dm = None
        for dm in DATE_RE.finditer(preceding):
            pass
        if not dm:
            continue
        date = dt.date(int(dm.group(3)), MONTHS[dm.group(1)], int(dm.group(2)))
        for row in ROW_RE.findall(table):
            tmatch = TIME_RE.search(row)
            if not tmatch:
                continue  # header row
            minutes = parse_time_to_minutes(strip_tags(tmatch.group(1)))
            if minutes is None:
                continue
            cells = CELL_RE.findall(row)
            # Availability is defined by the status2 class itself. The reserve
            # link is best-effort for one-click booking.
            reserve_paths = []
            open_count = 0
            for cls, inner in cells:
                if cls == "status2":
                    open_count += 1
                    m = RESERVE_RE.search(inner)
                    if m:
                        reserve_paths.append(m.group(1))
            out.append({
                "date": date,
                "dow": DOW[date.weekday()],
                "minutes": minutes,
                "time": fmt_time(minutes),
                "courts_total": len(cells),
                "courts_open": open_count,
                "reserve_path": reserve_paths[0] if reserve_paths else None,
            })
    return out


def gather(location_ids, locations, workers=8):
    """Fetch + parse selected locations in parallel.

    Returns ({id: [slots]}, {id: error_message}). A location that fails to load
    lands in `errors` (not as an empty slot list), so the caller can show
    "couldn't load" rather than a misleading "no availability".
    """
    result, errors = {}, {}

    def work(cid):
        try:
            html = fetch(f"{BASE}/tennisreservation/availability/{cid}")
            return cid, parse_availability(html), None
        except Exception as e:
            return cid, [], str(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for cid, slots, err in ex.map(work, location_ids):
            result[cid] = slots
            if err:
                errors[cid] = err
    return result, errors


# ---------- filtering ----------

def make_filter(args):
    dows = None
    if args.weekends:
        dows = {"sat", "sun"}
    elif args.weekdays:
        dows = {"mon", "tue", "wed", "thu", "fri"}
    elif args.dow:
        dows = {d.strip()[:3].lower() for d in args.dow.split(",") if d.strip()}

    after = parse_clock(args.after) if args.after else None
    before = parse_clock(args.before) if args.before else None
    today = dt.date.today()
    last_day = today + dt.timedelta(days=args.days - 1) if args.days else None

    def keep(slot):
        if slot["courts_open"] < args.min_courts:
            return False
        if dows and slot["dow"] not in dows:
            return False
        if last_day and slot["date"] > last_day:
            return False
        if after is not None and slot["minutes"] < after:
            return False
        if before is not None and slot["minutes"] > before:
            return False
        return True

    return keep


def parse_clock(s):
    s = s.strip().lower()
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        raise SystemExit(f"Could not parse time: {s!r} (try 8am, 18:30, 7:00pm)")
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mi


def select_locations(args, locations):
    if not args.locations:
        return list(locations.keys())
    wanted = [w.strip().lower() for w in args.locations.split(",") if w.strip()]
    chosen = []
    for cid, (name, boro) in locations.items():
        hay = f"{cid} {name} {boro}".lower()
        if any(w == str(cid) or w in hay for w in wanted):
            chosen.append(cid)
    if not chosen:
        raise SystemExit(f"No locations matched {args.locations!r}")
    return chosen


# ---------- output ----------

GREEN, RED, DIM, BOLD, CYAN, RESET = (
    "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[36m", "\033[0m")


def color(s, c, on):
    return f"{c}{s}{RESET}" if on else s


def print_summary(data, locations, keep, args, errors=None):
    errors = errors or {}
    use_color = sys.stdout.isatty() and not args.no_color
    now = dt.datetime.now().strftime("%a %b %-d, %-I:%M %p")
    print(color(f"NYC Tennis Availability — as of {now}", BOLD, use_color))
    bits = describe_filters(args)
    if bits:
        print(color("Filters: " + bits, DIM, use_color))
    print()

    # location order: most open slots first
    order = sorted(locations,
                   key=lambda cid: -sum(s["courts_open"] for s in data.get(cid, []) if keep(s)))
    any_open = False
    for cid in order:
        name, boro = locations[cid]
        if cid in errors:
            print(color(f"  ⚠ {name} ({boro}) — couldn't load ({errors[cid]})", RED, use_color))
            continue
        slots = [s for s in data.get(cid, []) if keep(s)]
        total_open = sum(s["courts_open"] for s in slots)
        url = f"{BASE}/tennisreservation/availability/{cid}"
        if not slots:
            print(color(f"  ✗ {name} ({boro}) — no availability", DIM, use_color))
            continue
        any_open = True
        head = color(f"  ✓ {name} ({boro})", GREEN + BOLD, use_color)
        print(f"{head}  {color(str(total_open) + ' open court-slots', CYAN, use_color)}")
        by_date = {}
        for s in slots:
            by_date.setdefault(s["date"], []).append(s)
        for date in sorted(by_date):
            label = date.strftime("%a %b %-d")
            chunks = []
            for s in sorted(by_date[date], key=lambda x: x["minutes"]):
                t = s["time"].replace(":00", "")
                chunks.append(f"{t}({s['courts_open']})")
            print(f"      {color(label, BOLD, use_color)}  " + " ".join(chunks))
        print(color(f"      → {url}", DIM, use_color))
        print()
    if not any_open:
        print(color("  No courts match your filters right now.", RED, use_color))
    print(color("  (number) = courts open at that time. Booking still requires login.", DIM, use_color))


def describe_filters(args):
    bits = []
    if args.days:
        bits.append(f"next {args.days} day(s)")
    if args.weekends:
        bits.append("weekends")
    elif args.weekdays:
        bits.append("weekdays")
    elif args.dow:
        bits.append("days: " + args.dow)
    if args.after:
        bits.append(f"after {args.after}")
    if args.before:
        bits.append(f"before {args.before}")
    if args.min_courts > 1:
        bits.append(f"≥{args.min_courts} courts")
    if args.locations:
        bits.append(f"locations~{args.locations}")
    return ", ".join(bits)


def to_json(data, locations, keep, errors=None):
    errors = errors or {}
    out = []
    for cid, slots in data.items():
        name, boro = locations[cid]
        kept = [s for s in slots if keep(s)]
        out.append({
            "location_id": cid,
            "name": name,
            "borough": boro,
            "url": f"{BASE}/tennisreservation/availability/{cid}",
            "error": errors.get(cid),
            "open_court_slots": sum(s["courts_open"] for s in kept),
            "slots": [{
                "date": s["date"].isoformat(),
                "dow": s["dow"],
                "time": s["time"],
                "courts_open": s["courts_open"],
                "courts_total": s["courts_total"],
                "reserve_url": (BASE + s["reserve_path"]) if s["reserve_path"] else None,
            } for s in sorted(kept, key=lambda x: (x["date"], x["minutes"]))],
        })
    out.sort(key=lambda x: -x["open_court_slots"])
    return json.dumps({"generated": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "locations": out}, indent=2)


def build_html(data, locations, keep, args, errors=None):
    """Grid dashboard: per location, rows=time, cols=date, green where open."""
    errors = errors or {}
    now = dt.datetime.now().strftime("%A %b %-d, %-I:%M %p")
    order = sorted(locations,
                   key=lambda cid: -sum(s["courts_open"] for s in data.get(cid, []) if keep(s)))
    sections = []
    for cid in order:
        name, boro = locations[cid]
        url = f"{BASE}/tennisreservation/availability/{cid}"
        if cid in errors:
            sections.append(
                f'<section class="loc empty"><h2>{htmllib.escape(name)} '
                f'<span class="boro">{htmllib.escape(boro)}</span>'
                f'<span class="badge none">couldn\'t load — re-run</span></h2></section>')
            continue
        slots = [s for s in data.get(cid, []) if keep(s)]
        total = sum(s["courts_open"] for s in slots)
        if not slots:
            sections.append(
                f'<section class="loc empty"><h2>{htmllib.escape(name)} '
                f'<span class="boro">{htmllib.escape(boro)}</span>'
                f'<span class="badge none">no availability</span></h2></section>')
            continue
        dates = sorted({s["date"] for s in slots})
        times = sorted({s["minutes"] for s in slots})
        grid = {(s["date"], s["minutes"]): s for s in slots}
        head_cells = "".join(
            f'<th>{d.strftime("%a")}<br>{d.strftime("%b %-d")}</th>' for d in dates)
        rows = []
        for mins in times:
            cells = []
            for d in dates:
                s = grid.get((d, mins))
                if s and s["courts_open"]:
                    link = (BASE + s["reserve_path"]) if s["reserve_path"] else url
                    cells.append(
                        f'<td class="open"><a href="{link}" target="_blank" '
                        f'title="{s["courts_open"]} of {s["courts_total"]} courts open — click to book">'
                        f'{s["courts_open"]}</a></td>')
                else:
                    cells.append('<td class="closed"></td>')
            rows.append(f'<tr><th class="t">{fmt_time(mins)}</th>{"".join(cells)}</tr>')
        sections.append(f"""<section class="loc">
  <h2><a href="{url}" target="_blank">{htmllib.escape(name)}</a>
      <span class="boro">{htmllib.escape(boro)}</span>
      <span class="badge">{total} open</span></h2>
  <div class="scroll"><table>
    <thead><tr><th class="t"></th>{head_cells}</tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>""")

    flt = htmllib.escape(describe_filters(args) or "all locations, next 30 days")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NYC Tennis Availability</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
          margin: 0; padding: 24px; background:#f6f7f9; color:#1a1a1a; }}
  @media (prefers-color-scheme: dark) {{ body{{background:#16181d;color:#e8e8ea}}
    section{{background:#1f232b!important;border-color:#2c313b!important}}
    th,td{{border-color:#2c313b!important}} .closed{{background:#23262d!important}}
    a{{color:#7db3ff}} h1 small{{color:#9aa0aa}} }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h1 small {{ font-weight: normal; color:#666; font-size: 14px; }}
  .filters {{ margin: 0 0 20px; color:#888; font-size: 13px; }}
  section {{ background:#fff; border:1px solid #e3e6ea; border-radius:12px;
             padding:16px 18px; margin:0 0 18px; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  section.empty {{ opacity:.5; }}
  h2 {{ font-size:17px; margin:0 0 12px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  h2 a {{ text-decoration:none; color:inherit; }}
  h2 a:hover {{ text-decoration:underline; }}
  .boro {{ font-weight:normal; color:#888; font-size:13px; }}
  .badge {{ margin-left:auto; background:#e7f6ec; color:#1a7f3c; font-size:12px;
            font-weight:600; padding:3px 9px; border-radius:20px; }}
  .badge.none {{ background:#f0f0f0; color:#999; }}
  .scroll {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; font-size:13px; }}
  th, td {{ border:1px solid #eceef1; text-align:center; padding:0; }}
  thead th {{ padding:6px 8px; font-weight:600; color:#555; white-space:nowrap; position:sticky; top:0; }}
  th.t {{ padding:4px 10px; color:#666; white-space:nowrap; font-weight:500; text-align:right; }}
  td {{ width:46px; height:30px; }}
  td.closed {{ background:#fafbfc; }}
  td.open {{ background:#d8f3e0; }}
  td.open a {{ display:block; width:100%; height:100%; line-height:30px;
               color:#0d6b2e; font-weight:700; text-decoration:none; }}
  td.open:hover {{ background:#bfead0; }}
  footer {{ color:#999; font-size:12px; margin-top:24px; }}
</style></head>
<body>
  <h1>🎾 NYC Tennis Availability <small>updated {now}</small></h1>
  <p class="filters">Filters: {flt} &nbsp;•&nbsp; green = courts open (number shown), click to book</p>
  {"".join(sections)}
  <footer>Data from nycgovparks.org. Viewing needs no login; booking does.
          Re-run the script to refresh.</footer>
</body></html>"""


def main():
    p = argparse.ArgumentParser(
        description="Aggregate NYC Parks tennis court availability across all locations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--locations", help="comma list of ids or name substrings, e.g. 'central,riverside,9'")
    p.add_argument("--days", type=int, default=0, help="only the next N days (default: all 30)")
    p.add_argument("--dow", help="days of week, e.g. 'sat,sun' or 'mon,wed,fri'")
    p.add_argument("--weekends", action="store_true", help="Saturdays and Sundays only")
    p.add_argument("--weekdays", action="store_true", help="Monday–Friday only")
    p.add_argument("--after", help="earliest start time, e.g. 8am, 17:00")
    p.add_argument("--before", help="latest start time, e.g. 8pm, 20:00")
    p.add_argument("--min-courts", type=int, default=1, help="require at least this many open courts (default 1)")
    p.add_argument("--html", metavar="FILE", help="write an HTML dashboard to FILE")
    p.add_argument("--open", action="store_true", help="open the HTML dashboard in your browser")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a summary")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = p.parse_args()

    locations = discover_locations()
    chosen = select_locations(args, locations)
    if not args.json:
        print(f"Fetching {len(chosen)} location(s)…", file=sys.stderr)
    data, errors = gather(chosen, locations)
    keep = make_filter(args)
    chosen_locs = {c: locations[c] for c in chosen}

    if args.json:
        print(to_json(data, chosen_locs, keep, errors))
        return

    if args.html or args.open:
        path = args.html or "nyc_tennis.html"
        with open(path, "w") as f:
            f.write(build_html(data, chosen_locs, keep, args, errors))
        print(f"Wrote dashboard to {path}", file=sys.stderr)
        if args.open:
            webbrowser.open("file://" + os.path.abspath(path))
        if not args.html:  # --open only: still show terminal summary too
            print_summary(data, chosen_locs, keep, args, errors)
        return

    print_summary(data, chosen_locs, keep, args, errors)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error reaching nycgovparks.org: {e}")
    except KeyboardInterrupt:
        raise SystemExit(130)
