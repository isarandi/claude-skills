#!/usr/bin/env python3
"""Plot when the user wrote messages, from the search-history index.

Times are shown in the local time zone of the machine that runs this (the index
stores them in UTC). Needs matplotlib and numpy.

    plot_activity.py [OUTPUT.png]      default: claude_activity.png in the current directory
"""
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from search_history import DB_PATH, INDEX_VERSION, TIME_FORMAT  # noqa: E402


def get_user_timestamps():
    """Local times of the messages the user typed, in order."""
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(DB_PATH))}?mode=ro", uri=True)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != INDEX_VERSION:
        sys.exit(f"Index {DB_PATH} has format {version}, this script reads format {INDEX_VERSION}")
    rows = conn.execute(
        "SELECT timestamp FROM records WHERE record_type = 'main_user_text' AND timestamp != ''").fetchall()
    conn.close()

    timestamps = []
    for (ts,) in rows:
        try:
            utc = datetime.strptime(ts, TIME_FORMAT).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        timestamps.append(utc.astimezone())  # the machine's local zone
    return sorted(timestamps)


def plot_activity(timestamps, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np

    zone = timestamps[-1].tzname() or "local time"
    fig, axes = plt.subplots(3, 1, figsize=(12, 10))

    # 1. Time of day (0-24 hours as float)
    hours = np.array([t.hour + t.minute / 60 + t.second / 3600 for t in timestamps])
    ax = axes[0]
    ax.hist(hours, bins=144, range=(0, 24), color='steelblue', edgecolor='none', alpha=0.8)
    ax.set_xlabel(f"Hour of day ({zone})")
    ax.set_ylabel("Messages")
    ax.set_title("Activity by time of day")
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))

    # 2. Day of week (0=Monday, continuous with fractional day)
    week_pos = np.array([t.weekday() + t.hour / 24 + t.minute / 1440 for t in timestamps])
    ax = axes[1]
    ax.hist(week_pos, bins=168, range=(0, 7), color='steelblue', edgecolor='none', alpha=0.8)
    ax.set_xlabel(f"Day of week ({zone})")
    ax.set_ylabel("Messages")
    ax.set_title("Activity by day of week")
    ax.set_xlim(0, 7)
    ax.set_xticks(np.arange(0.5, 7, 1))
    ax.set_xticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    # 3. Calendar timeline (local wall-clock times)
    ax = axes[2]
    dates = mdates.date2num([t.replace(tzinfo=None) for t in timestamps])
    ax.hist(dates, bins=200, color='steelblue', edgecolor='none', alpha=0.8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.set_xlabel(f"Date ({zone})")
    ax.set_ylabel("Messages")
    ax.set_title("Activity over time")
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

    plt.tight_layout()
    plt.savefig(output, dpi=150)
    print(f"Saved to {output}")


def main():
    output = sys.argv[1] if len(sys.argv) > 1 else "claude_activity.png"
    timestamps = get_user_timestamps()
    print(f"Found {len(timestamps)} user messages")
    if not timestamps:
        return
    print(f"Range: {timestamps[0]:%Y-%m-%d %H:%M} to {timestamps[-1]:%Y-%m-%d %H:%M} ({timestamps[-1].tzname()})")
    plot_activity(timestamps, output)


if __name__ == "__main__":
    main()
