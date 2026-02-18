#!/usr/bin/env python3
# /// script
# dependencies = ["pyobjc-framework-EventKit"]
# ///
"""
bigcal.py — macOS Calendar → Single-Page HTML Year View
Usage:
  uv run bigcal.py             # current year
  uv run bigcal.py 2025        # specific year
  uv run bigcal.py --list      # print available calendar names
  uv run bigcal.py --calendars "Work,Personal"  # filter (saved to bigcal.json)
"""

import argparse
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import EventKit
from AppKit import NSColorSpace

SCRIPT_DIR = Path(__file__).parent
OUTPUT_PATH = SCRIPT_DIR / "cal.html"

EKEntityTypeEvent = 0
COLS = 22  # days per row


# ---------------------------------------------------------------------------
# EventKit helpers
# ---------------------------------------------------------------------------

def get_event_store() -> EventKit.EKEventStore:
    store = EventKit.EKEventStore.alloc().init()

    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
    # 0 = notDetermined, 1 = restricted, 2 = denied, 3 = fullAccess (macOS 14+), 4 = writeOnly
    if status == 3:
        return store

    if status in (1, 2):
        print(
            f"Calendar access denied (status={status}). "
            "Grant Full Access in System Settings → Privacy & Security → Calendars.",
            file=sys.stderr,
        )
        sys.exit(1)

    # status == 0: not determined — request access
    done = threading.Event()
    result_holder = [False, None]

    def callback(granted, error):
        result_holder[0] = granted
        result_holder[1] = error
        done.set()

    try:
        store.requestFullAccessToEventsWithCompletion_(callback)
    except AttributeError:
        # macOS 13 fallback
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, callback)

    done.wait(timeout=30)

    if not result_holder[0]:
        err = result_holder[1]
        print(f"Calendar access not granted. {err or ''}", file=sys.stderr)
        sys.exit(1)

    return store


def nscolor_to_hex(nscolor) -> str:
    try:
        rgb = nscolor.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if rgb is None:
            raise ValueError("sRGB conversion failed")
    except Exception:
        try:
            from AppKit import NSCalibratedRGBColorSpace
            rgb = nscolor.colorUsingColorSpaceName_(NSCalibratedRGBColorSpace)
        except Exception:
            return "#888888"
    if rgb is None:
        return "#888888"
    r = rgb.redComponent()
    g = rgb.greenComponent()
    b = rgb.blueComponent()
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def nsdate_to_date(nsdate) -> date:
    ts = nsdate.timeIntervalSince1970()
    return datetime.fromtimestamp(ts).date()


def list_calendars(store: EventKit.EKEventStore) -> list[dict]:
    cals = store.calendarsForEntityType_(EKEntityTypeEvent)
    result = []
    for cal in cals:
        color = nscolor_to_hex(cal.color())
        result.append({"title": cal.title(), "color": color})
    return result


def fetch_events(store: EventKit.EKEventStore, year: int, filter_cals: list[str] | None) -> list[dict]:
    from Foundation import NSDate

    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)

    # Buffer to catch multi-day events that start just before the year
    query_start = start_date - timedelta(days=7)
    query_end = end_date + timedelta(days=7)

    def date_to_nsdate(d: date) -> NSDate:
        dt = datetime(d.year, d.month, d.day)
        ts = dt.timestamp()
        return NSDate.dateWithTimeIntervalSince1970_(ts)

    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        date_to_nsdate(query_start), date_to_nsdate(query_end), None
    )
    events = store.eventsMatchingPredicate_(predicate)

    # Build calendar color map
    cals = store.calendarsForEntityType_(EKEntityTypeEvent)
    cal_info = {cal.title(): nscolor_to_hex(cal.color()) for cal in cals}

    result = []
    for ev in events:
        if not ev.isAllDay():
            continue

        cal_title = ev.calendar().title()
        if filter_cals is not None and cal_title not in filter_cals:
            continue

        # EventKit all-day endDate is inclusive (midnight of the last day, same as startDate for single-day events)
        ev_start = nsdate_to_date(ev.startDate())
        ev_end = nsdate_to_date(ev.endDate())

        # Clip to year boundary
        display_start = max(ev_start, start_date)
        display_end = min(ev_end, end_date)

        if display_start > display_end:
            continue

        result.append({
            "title": ev.title() or "(no title)",
            "start": display_start,
            "end": display_end,
            "color": cal_info.get(cal_title, "#888888"),
            "calendar": cal_title,
        })

    return result


# ---------------------------------------------------------------------------
# Grid construction — Continuous from Jan 1
# ---------------------------------------------------------------------------

def build_rows(year: int) -> list[list[date | None]]:
    """
    Returns list of rows; each row is a list of COLS dates.
    The last row may be padded with None for days beyond Dec 31.
    """
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    total_days = (end - start).days + 1
    num_rows = (total_days + COLS - 1) // COLS
    rows = []
    for r in range(num_rows):
        row = []
        for c in range(COLS):
            day_offset = r * COLS + c
            d = start + timedelta(days=day_offset)
            row.append(d if d <= end else None)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Event layout per row
# ---------------------------------------------------------------------------

def layout_events_for_row(
    row: list[date | None],
    events: list[dict],
) -> list[dict]:
    """
    Greedy interval scheduling.  Returns event dicts augmented with:
      col_start, col_end, row (vertical slot), continues_left, continues_right
    """
    real_dates = [d for d in row if d is not None]
    if not real_dates:
        return []

    row_start = real_dates[0]
    row_end = real_dates[-1]

    row_events = []
    for ev in events:
        if ev["end"] < row_start or ev["start"] > row_end:
            continue
        col_start = max(0, (ev["start"] - row_start).days)
        col_end = min(COLS - 1, (ev["end"] - row_start).days)
        row_events.append({
            **ev,
            "col_start": col_start,
            "col_end": col_end,
            "continues_left": ev["start"] < row_start,
            "continues_right": ev["end"] > row_end,
        })

    # Sort by start column, then by span length descending (longer events get lower rows)
    row_events.sort(key=lambda e: (e["col_start"], -(e["col_end"] - e["col_start"])))

    slot_end = []  # slot_end[r] = next free column in row r
    for ev in row_events:
        placed = False
        for r, end_col in enumerate(slot_end):
            if end_col <= ev["col_start"]:
                ev["row"] = r
                slot_end[r] = ev["col_end"] + 1
                placed = True
                break
        if not placed:
            ev["row"] = len(slot_end)
            slot_end.append(ev["col_end"] + 1)

    return row_events


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Vertical layout constants (px)
DATE_AREA_H = 20   # height reserved for the date number row
EVENT_H = 15       # height of each event bar
EVENT_GAP = 2      # vertical gap between stacked event bars
EVENT_SLOT = EVENT_H + EVENT_GAP  # 17px per slot
ROW_MIN_H = 52     # minimum row height
ROW_PAD_B = 3      # bottom padding


def _row_height(max_slot: int) -> int:
    needed = DATE_AREA_H + (max_slot + 1) * EVENT_SLOT + ROW_PAD_B
    return max(ROW_MIN_H, needed)


def _escape(s: str) -> str:
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def generate_html(
    year: int,
    rows: list[list[date | None]],
    events: list[dict],
    calendars_info: list[dict],
    filter_cals: list[str] | None,
) -> str:
    today = date.today()

    # Build each row's HTML
    rows_html_parts = []
    for row in rows:
        laid_out = layout_events_for_row(row, events)

        max_slot = max((e["row"] for e in laid_out), default=-1)
        rh = _row_height(max_slot)

        # --- Cell backgrounds and date numbers ---
        cells_html = []
        for ci, d in enumerate(row):
            border_right = "border-right:1px solid #e5e7eb;" if ci < COLS - 1 else ""

            if d is None:
                # Padding cell (end of last row)
                cells_html.append(
                    f'<div style="height:100%;{border_right}background:#f9fafb;"></div>'
                )
                continue

            is_today = (d == today)

            # Full date tooltip (native HTML title attribute)
            full_date = d.strftime("%A, %B %-d, %Y")

            # Cell background: tinted amber for today
            cell_bg = "background:#fef3c7;" if is_today else ""

            # Date number: bold amber text for today, normal otherwise
            if is_today:
                date_num_html = (
                    f'<span style="font-size:11px;font-weight:800;color:#92400e;line-height:1;">'
                    f'{d.day}</span>'
                )
            else:
                date_num_html = (
                    f'<span style="font-size:11px;font-weight:600;color:#374151;line-height:1;">'
                    f'{d.day}</span>'
                )

            # Left side of date header: dark badge for day==1, subtle label for all others
            if d.day == 1:
                left_html = (
                    f'<span style="'
                    f'display:inline-flex;align-items:center;'
                    f'background:#111;color:#fff;'
                    f'font-size:9px;font-weight:800;letter-spacing:0.04em;'
                    f'padding:1px 4px;border-radius:3px;'
                    f'line-height:13px;'
                    f'">{MONTH_NAMES[d.month - 1]}</span>'
                )
            else:
                left_html = (
                    f'<span style="font-size:9px;font-weight:600;color:#d1d5db;'
                    f'letter-spacing:0.03em;line-height:1;">'
                    f'{MONTH_NAMES[d.month - 1]}</span>'
                )

            cells_html.append(
                f'<div title="{_escape(full_date)}" style="height:100%;{border_right}{cell_bg}">'
                f'<div style="display:flex;align-items:center;justify-content:flex-end;gap:3px;'
                f'padding:2px 4px 0 3px;min-height:{DATE_AREA_H}px;">'
                f'{left_html}{date_num_html}'
                f'</div>'
                f'</div>'
            )

        cells_joined = "".join(cells_html)

        # --- Event bars (absolutely positioned over the row) ---
        event_bars = []
        for ev in laid_out:
            left_pct = ev["col_start"] / COLS * 100
            width_pct = (ev["col_end"] - ev["col_start"] + 1) / COLS * 100
            top_px = DATE_AREA_H + ev["row"] * EVENT_SLOT

            # Border radius: rounded on sides where event doesn't continue
            r_tl = 0 if ev["continues_left"] else 3
            r_tr = 0 if ev["continues_right"] else 3
            br = f"border-radius:{r_tl}px {r_tr}px {r_tr}px {r_tl}px;"

            # 1px pixel inset on the sides where the event starts/ends (purely cosmetic)
            left_px = 0 if ev["continues_left"] else 1
            right_px = 0 if ev["continues_right"] else 1

            event_bars.append(
                f'<div style="'
                f'position:absolute;'
                f'left:calc({left_pct:.4f}% + {left_px}px);'
                f'width:calc({width_pct:.4f}% - {left_px + right_px}px);'
                f'top:{top_px}px;'
                f'height:{EVENT_H}px;'
                f'background:{ev["color"]};'
                f'color:#fff;'
                f'{br}'
                f'font-size:10px;font-weight:500;'
                f'padding:0 6px;'
                f'display:flex;align-items:center;'
                f'overflow:hidden;white-space:nowrap;text-overflow:ellipsis;'
                f'box-sizing:border-box;'
                f'" title="{_escape(ev["title"])} ({_escape(ev["calendar"])})">'
                f'{_escape(ev["title"])}'
                f'</div>'
            )

        events_joined = "".join(event_bars)

        rows_html_parts.append(
            f'<div style="'
            f'display:grid;grid-template-columns:repeat({COLS},1fr);'
            f'position:relative;'
            f'height:{rh}px;'
            f'border-bottom:1px solid #e5e7eb;'
            f'">'
            f'{cells_joined}'
            f'{events_joined}'
            f'</div>'
        )

    rows_html = "\n".join(rows_html_parts)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>bigcal {year}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::-webkit-scrollbar {{ display: none; }}
    html {{ -ms-overflow-style: none; scrollbar-width: none; }}
    body {{
      background: #fff;
      color: #111;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div>
{rows_html}
  </div>
</body>
</html>
"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a year-view HTML calendar from macOS Calendar."
    )
    parser.add_argument(
        "year", nargs="?", type=int, default=date.today().year,
        help="Year to display (default: current year)",
    )
    parser.add_argument("--list", action="store_true", help="List available calendar names and exit")
    parser.add_argument(
        "--calendars", type=str, default=None,
        help="Comma-separated calendar names to include (saved to bigcal.json)",
    )
    args = parser.parse_args()

    filter_cals = [c.strip() for c in args.calendars.split(",") if c.strip()] if args.calendars else None

    store = get_event_store()

    if args.list:
        cals = list_calendars(store)
        print(f"Available calendars ({len(cals)}):")
        for cal in sorted(cals, key=lambda c: c["title"].lower()):
            print(f"  {cal['color']}  {cal['title']}")
        return

    year = args.year
    print(f"Fetching events for {year}...", end=" ", flush=True)
    events = fetch_events(store, year, filter_cals)
    print(f"{len(events)} all-day events found.")

    calendars_info = list_calendars(store)
    rows = build_rows(year)

    print(f"Building {len(rows)}-row grid ({COLS} days/row)...", end=" ", flush=True)
    html = generate_html(year, rows, events, calendars_info, filter_cals)
    print("done.")

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
