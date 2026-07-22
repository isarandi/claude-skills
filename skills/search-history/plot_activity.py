#!/usr/bin/env python3
"""Plot user activity times from Claude conversation history."""

import sqlite3
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

DB_PATH = Path.home() / ".claude/history.db"
TZ = ZoneInfo("Europe/Berlin")


def get_user_timestamps():
    """Get all user message timestamps."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT timestamp FROM records WHERE type = 'main_user_text' AND timestamp IS NOT NULL"
    ).fetchall()
    conn.close()

    timestamps = []
    for (ts,) in rows:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
            timestamps.append(dt)
        except (ValueError, AttributeError):
            continue
    return sorted(timestamps)


def plot_activity(timestamps):
    fig, axes = plt.subplots(3, 1, figsize=(12, 10))

    # 1. Time of day (0-24 hours as float)
    hours = np.array([t.hour + t.minute / 60 + t.second / 3600 for t in timestamps])
    ax = axes[0]
    ax.hist(hours, bins=144, range=(0, 24), color='steelblue', edgecolor='none', alpha=0.8)
    ax.set_xlabel("Hour of day (Central Europe)")
    ax.set_ylabel("Messages")
    ax.set_title("Activity by time of day")
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))

    # 2. Day of week (0=Monday, continuous with fractional day)
    week_pos = np.array([t.weekday() + t.hour / 24 + t.minute / 1440 for t in timestamps])
    ax = axes[1]
    ax.hist(week_pos, bins=168, range=(0, 7), color='steelblue', edgecolor='none', alpha=0.8)
    ax.set_xlabel("Day of week (Central Europe)")
    ax.set_ylabel("Messages")
    ax.set_title("Activity by day of week")
    ax.set_xlim(0, 7)
    ax.set_xticks(np.arange(0.5, 7, 1))
    ax.set_xticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    # 3. Calendar timeline
    ax = axes[2]
    dates = mdates.date2num(timestamps)
    ax.hist(dates, bins=200, color='steelblue', edgecolor='none', alpha=0.8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.set_xlabel("Date")
    ax.set_ylabel("Messages")
    ax.set_title("Activity over time")
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

    plt.tight_layout()
    plt.savefig("/tmp/claude_activity.png", dpi=150)
    print(f"Saved to /tmp/claude_activity.png")
    plt.show()


def main():
    timestamps = get_user_timestamps()
    print(f"Found {len(timestamps)} user messages")
    if timestamps:
        print(f"Range: {timestamps[0]} to {timestamps[-1]}")
    plot_activity(timestamps)


if __name__ == "__main__":
    main()
