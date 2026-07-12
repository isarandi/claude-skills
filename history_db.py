#!/usr/bin/env python3
"""Build and query SQLite database from Claude conversation history."""
import argparse
import sqlite3
from pathlib import Path

from search_history import (
    PROJECTS_DIR, expand_record, load_history_file,
    resolve_project_dirs, get_agent_files
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DB_PATH = Path.home() / ".claude/history.db"


def init_db(db_path=DB_PATH):
    """Initialize database with schema."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def get_or_create_project(conn, project_name, project_path=None):
    """Get or create a project, return its ID."""
    row = conn.execute("SELECT id FROM projects WHERE name = ?", (project_name,)).fetchone()
    if row:
        return row[0]

    cursor = conn.execute(
        "INSERT INTO projects (name, path) VALUES (?, ?)",
        (project_name, project_path)
    )
    return cursor.lastrowid


def sync_session(conn, session_path, project_id):
    """Sync a single session JSONL file to database."""
    session_id = session_path.stem
    mtime = session_path.stat().st_mtime

    # Check if already synced
    row = conn.execute(
        "SELECT jsonl_mtime FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()

    if row and row[0] == mtime:
        return 0  # Already up to date

    # Delete old records for this session
    conn.execute("DELETE FROM records WHERE session_id = ? AND agent_id IS NULL", (session_id,))

    # Load and parse
    records = load_history_file(session_path)
    if not records:
        return 0

    # Get timestamps for session bounds
    timestamps = [r.get("timestamp", "") for r in records if r.get("timestamp")]
    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None

    # Upsert session
    conn.execute("""
        INSERT INTO sessions (id, project_id, started_at, ended_at, jsonl_path, jsonl_mtime)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            started_at = excluded.started_at,
            ended_at = excluded.ended_at,
            jsonl_mtime = excluded.jsonl_mtime
    """, (session_id, project_id, started_at, ended_at, str(session_path), mtime))

    # Insert virtual records
    count = 0
    for rec in records:
        for vr in expand_record(rec, records, agent_id=None):
            conn.execute("""
                INSERT INTO records (session_id, agent_id, uuid, type, tool_name, content, timestamp)
                VALUES (?, NULL, ?, ?, ?, ?, ?)
            """, (session_id, vr["uuid"], vr["type"], vr.get("tool_name"), vr["content"], vr["timestamp"]))
            count += 1

    return count


def sync_agent(conn, agent_path, session_id):
    """Sync a single agent JSONL file to database."""
    agent_id = agent_path.stem.replace("agent-", "")
    mtime = agent_path.stat().st_mtime

    # Check if already synced
    row = conn.execute(
        "SELECT jsonl_mtime FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()

    if row and row[0] == mtime:
        return 0  # Already up to date

    # Delete old records for this agent
    conn.execute("DELETE FROM records WHERE agent_id = ?", (agent_id,))

    # Load and parse
    records = load_history_file(agent_path)
    if not records:
        return 0

    # Upsert agent
    conn.execute("""
        INSERT INTO agents (id, session_id, jsonl_path, jsonl_mtime)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            jsonl_mtime = excluded.jsonl_mtime
    """, (agent_id, session_id, str(agent_path), mtime))

    # Insert virtual records
    count = 0
    for rec in records:
        for vr in expand_record(rec, records, agent_id=agent_id):
            conn.execute("""
                INSERT INTO records (session_id, agent_id, uuid, type, tool_name, content, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session_id, agent_id, vr["uuid"], vr["type"], vr.get("tool_name"), vr["content"], vr["timestamp"]))
            count += 1

    return count


def sync_project(conn, project_path):
    """Sync all sessions and agents for a project."""
    project_dirs = resolve_project_dirs(project_path, None)
    if not project_dirs:
        print(f"Project not found: {project_path}")
        return

    total = 0
    for project_dir in project_dirs:
        project_name = project_dir.name
        # Derive filesystem path from project name (e.g., -home-user-proj -> /home/user/proj)
        fs_path = "/" + project_name.lstrip("-").replace("-", "/")
        project_id = get_or_create_project(conn, project_name, fs_path)
        print(f"Syncing {project_name}...")

        # Sync sessions
        session_files = [f for f in project_dir.glob("*.jsonl") if "agent-" not in f.name]
        for sf in session_files:
            count = sync_session(conn, sf, project_id)
            if count:
                print(f"  {sf.stem[:8]}: {count} records")
                total += count

            # Sync agents for this session
            agent_files = get_agent_files(project_dir, session_id=sf.stem)
            for af in agent_files:
                count = sync_agent(conn, af, sf.stem)
                if count:
                    agent_id = af.stem.replace("agent-", "")
                    print(f"    agent {agent_id}: {count} records")
                    total += count

    conn.commit()
    return total


def sync_all_projects(conn):
    """Sync all projects in PROJECTS_DIR."""
    total = 0
    project_dirs = sorted(PROJECTS_DIR.iterdir())
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        fs_path = "/" + project_name.lstrip("-").replace("-", "/")
        project_id = get_or_create_project(conn, project_name, fs_path)

        session_files = [f for f in project_dir.glob("*.jsonl") if "agent-" not in f.name]
        if not session_files:
            continue

        print(f"{project_name}: ", end="", flush=True)
        proj_total = 0
        for sf in session_files:
            count = sync_session(conn, sf, project_id)
            proj_total += count

            # Sync agents for this session
            agent_files = get_agent_files(project_dir, session_id=sf.stem)
            for af in agent_files:
                count = sync_agent(conn, af, sf.stem)
                proj_total += count

        print(f"{proj_total} records")
        total += proj_total

    conn.commit()
    return total


def main():
    parser = argparse.ArgumentParser(description="Build SQLite DB from Claude history")
    parser.add_argument("--project", help="Project path to sync")
    parser.add_argument("--all", action="store_true", help="Sync all projects")
    parser.add_argument("--db", default=str(DB_PATH), help="Database path")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    args = parser.parse_args()

    conn = init_db(args.db)

    if args.stats:
        for table in ["projects", "sessions", "agents", "records"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
        return

    if args.all:
        total = sync_all_projects(conn)
        print(f"Total: {total} records synced")
    elif args.project:
        total = sync_project(conn, args.project)
        print(f"Total: {total} records synced")
    else:
        print("Usage: history_db.py --all")
        print("       history_db.py --project /path/to/project")
        print("       history_db.py --stats")


if __name__ == "__main__":
    main()
