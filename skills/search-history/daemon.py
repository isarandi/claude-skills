#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["inotify_simple; sys_platform == 'linux'"]
# ///
"""
Claude Code History Indexer Daemon

Watches ~/.claude directories for changes and indexes content into SQLite for fast search.
Uses inotify for real-time updates on Linux, polling elsewhere (macOS), and FTS5 for full-text search.
"""
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "linux":
    try:
        from inotify_simple import INotify, flags
    except ImportError:
        print("Error: inotify_simple not installed. Run: pip install inotify_simple", file=sys.stderr)
        sys.exit(1)
else:
    INotify = None  # No inotify: run() polls instead

# Seconds between rescans when polling
POLL_INTERVAL = 5

# Paths
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PLANS_DIR = CLAUDE_DIR / "plans"
FILE_HISTORY_DIR = CLAUDE_DIR / "file-history"
# Put DB outside ~/.claude to avoid triggering Claude's own file watchers
DB_PATH = Path.home() / ".local/share/claude-history-index/history.db"

# Import record parsing and time conversion from search_history.py
sys.path.insert(0, str(Path(__file__).parent))
from search_history import (INDEX_VERSION, TIME_FORMAT, expand_record, mtime_to_utc_time,
                            utc_index_time, utc_to_local)


def _local_to_utc_text(ts):
    """Convert a local time string (TIME_FORMAT, this process's zone) to UTC; unparseable as is."""
    try:
        dt = datetime.strptime(ts, TIME_FORMAT)
    except (TypeError, ValueError):
        return ts
    return dt.astimezone(timezone.utc).strftime(TIME_FORMAT)


class HistoryIndexer:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.db = None
        self.inotify = None
        self.watch_to_path = {}  # wd -> path
        self.dir_mtimes = {}  # snapshot dir -> mtime at last listing
        self.running = True

    def init_db(self):
        """Initialize SQLite database with schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

        # Enable WAL mode for better concurrent access
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")

        # Create tables
        self.db.executescript("""
            -- Main records table (conversations, agents, plans)
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY,
                uuid TEXT,
                timestamp TEXT,
                record_type TEXT,
                tool_name TEXT,
                tool_use_id TEXT,
                content TEXT,
                project_name TEXT,
                session_id TEXT,
                agent_id TEXT,
                source_file TEXT,
                byte_offset INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_timestamp ON records(timestamp);
            CREATE INDEX IF NOT EXISTS idx_type ON records(record_type);
            CREATE INDEX IF NOT EXISTS idx_tool ON records(tool_name);
            CREATE INDEX IF NOT EXISTS idx_project ON records(project_name);
            CREATE INDEX IF NOT EXISTS idx_session ON records(session_id);
            CREATE INDEX IF NOT EXISTS idx_source ON records(source_file);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_record
                ON records(uuid, record_type, COALESCE(tool_use_id, ''));

            -- File snapshots table
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                backup_id TEXT UNIQUE,
                session_id TEXT,
                file_path TEXT,
                timestamp TEXT,
                size INTEGER,
                content TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snap_session ON snapshots(session_id);
            CREATE INDEX IF NOT EXISTS idx_snap_file ON snapshots(file_path);
            CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(timestamp);

            -- Track indexing progress per file
            CREATE TABLE IF NOT EXISTS indexed_files (
                file_path TEXT PRIMARY KEY,
                byte_offset INTEGER,
                file_mtime REAL
            );
        """)

        # Create FTS5 tables (separate to handle "already exists" gracefully)
        try:
            self.db.execute("""
                CREATE VIRTUAL TABLE records_fts USING fts5(
                    content,
                    content='records',
                    content_rowid='id'
                )
            """)
        except sqlite3.OperationalError:
            pass  # Already exists

        try:
            self.db.execute("""
                CREATE VIRTUAL TABLE snapshots_fts USING fts5(
                    content,
                    content='snapshots',
                    content_rowid='id'
                )
            """)
        except sqlite3.OperationalError:
            pass  # Already exists

        # Create triggers for FTS sync
        try:
            self.db.execute("""
                CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN
                    INSERT INTO records_fts(rowid, content) VALUES (new.id, new.content);
                END
            """)
        except sqlite3.OperationalError:
            pass

        try:
            self.db.execute("""
                CREATE TRIGGER snapshots_ai AFTER INSERT ON snapshots BEGIN
                    INSERT INTO snapshots_fts(rowid, content) VALUES (new.id, new.content);
                END
            """)
        except sqlite3.OperationalError:
            pass

        self.db.commit()
        self.migrate()

    def migrate(self):
        """Bring an index written by an older daemon up to INDEX_VERSION."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= INDEX_VERSION:
            return
        has_data = self.db.execute("SELECT 1 FROM records LIMIT 1").fetchone() or \
            self.db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone()
        if has_data and version < 1:
            print("Migrating index to version 1 (UTC timestamps)...", file=sys.stderr)
            self.migrate_to_v1()
        self.db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
        self.db.commit()
        if has_data:
            # Give back the disk space the migration's write-ahead log took
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def migrate_to_v1(self):
        """Store timestamps in UTC instead of the daemon's local time, and drop
        the copies indexed from "api-request-blob" records.

        A version 0 daemon wrote timestamps in its process's local time zone,
        which could change across restarts. Each timestamp is therefore derived
        anew from its source: the transcript line at the record's byte offset,
        or the plan or snapshot file's modification time. Records whose source
        is gone are converted from whichever zone (UTC or the current local one)
        the re-derived records show the old daemon used.
        """
        db = self.db
        # Records without uuid come from api-request-blob lines (plans use the file name).
        # They can amount to a fifth of the content: free their pages without
        # overwriting them (secure_delete), which would write all of it to the log.
        # Their records_fts entries stay; search does not query that index.
        secure_delete = db.execute("PRAGMA secure_delete").fetchone()[0]
        db.execute("PRAGMA secure_delete = 0")
        db.execute("DELETE FROM records WHERE (uuid IS NULL OR uuid = '') AND record_type != 'plan'")
        db.execute(f"PRAGMA secure_delete = {int(secure_delete)}")

        updates, unresolved, n_updated = [], [], 0
        votes = {"utc": 0, "local": 0}
        for (source_file,) in db.execute(
                "SELECT DISTINCT source_file FROM records WHERE record_type != 'plan'").fetchall():
            rows = db.execute(
                "SELECT id, uuid, timestamp, byte_offset FROM records WHERE source_file = ? "
                "ORDER BY byte_offset", (source_file,)).fetchall()
            try:
                f = open(source_file, 'rb')
            except (OSError, TypeError):
                unresolved.extend((row['id'], row['timestamp']) for row in rows)
                continue
            with f:
                offset, rec = None, None
                for row in rows:
                    if row['byte_offset'] != offset:
                        offset = row['byte_offset']
                        f.seek(offset)
                        try:
                            rec = json.loads(f.readline())
                        except ValueError:
                            rec = None
                    if not isinstance(rec, dict) or (rec.get('uuid') or '')[:12] != row['uuid']:
                        unresolved.append((row['id'], row['timestamp']))
                        continue
                    new = utc_index_time(rec.get('timestamp'))
                    if not new:
                        pass
                    elif row['timestamp'] == new:
                        votes["utc"] += 1
                    elif row['timestamp'] == utc_to_local(rec.get('timestamp')):
                        votes["local"] += 1
                    if new != row['timestamp']:
                        updates.append((new, row['id']))
            db.executemany("UPDATE records SET timestamp = ? WHERE id = ?", updates)
            n_updated += len(updates)
            updates = []

        # Plans: the timestamp was the file's modification time, kept in indexed_files
        for row in db.execute("""
                SELECT r.id, r.timestamp, i.file_mtime FROM records r
                LEFT JOIN indexed_files i ON i.file_path = r.source_file
                WHERE r.record_type = 'plan'""").fetchall():
            if row['file_mtime'] is None:
                unresolved.append((row['id'], row['timestamp']))
            else:
                updates.append((mtime_to_utc_time(row['file_mtime']), row['id']))

        # Records that could not be re-derived
        old_was_local = votes["local"] > votes["utc"]
        if old_was_local:
            updates.extend((_local_to_utc_text(ts), rid) for rid, ts in unresolved)
        db.executemany("UPDATE records SET timestamp = ? WHERE id = ?", updates)
        n_updated += len(updates)

        # Snapshots: the timestamp was the backup file's modification time
        snapshot_updates = []
        for row in db.execute("SELECT id, backup_id, session_id, timestamp FROM snapshots").fetchall():
            try:
                new = mtime_to_utc_time(
                    (FILE_HISTORY_DIR / row['session_id'] / row['backup_id']).stat().st_mtime)
            except (OSError, TypeError):
                new = _local_to_utc_text(row['timestamp']) if old_was_local else row['timestamp']
            if new != row['timestamp']:
                snapshot_updates.append((new, row['id']))
        db.executemany("UPDATE snapshots SET timestamp = ? WHERE id = ?", snapshot_updates)

        print(f"Migrated: {n_updated} record and {len(snapshot_updates)} snapshot timestamps "
              f"changed; {len(unresolved)} records without readable source, old times taken as "
              f"{'local' if old_was_local else 'UTC'}", file=sys.stderr)

    def watch(self, path, mask):
        """Add an inotify watch, unless the directory vanished meanwhile."""
        try:
            wd = self.inotify.add_watch(path, mask)
        except OSError:
            return
        self.watch_to_path[wd] = path

    def watch_project_dir(self, project_dir):
        """Watch a project directory and the subagents/ folders of its sessions."""
        self.watch(project_dir, flags.MODIFY | flags.CREATE | flags.MOVED_TO)
        try:
            session_dirs = [d for d in project_dir.iterdir() if d.is_dir()]
        except OSError:
            return
        for session_dir in session_dirs:
            self.watch_session_dir(session_dir)

    def watch_session_dir(self, session_dir):
        """Watch a session directory for its subagents/ folder, and that folder for agent transcripts."""
        self.watch(session_dir, flags.CREATE | flags.MOVED_TO)
        subagents_dir = session_dir / "subagents"
        if subagents_dir.is_dir():
            self.watch(subagents_dir, flags.MODIFY | flags.CREATE | flags.MOVED_TO)

    def setup_watches(self):
        """Set up inotify watches on all relevant directories."""
        self.inotify = INotify()
        watch_flags = flags.MODIFY | flags.CREATE | flags.MOVED_TO

        # Watch projects root
        if PROJECTS_DIR.exists():
            wd = self.inotify.add_watch(PROJECTS_DIR, flags.CREATE | flags.MOVED_TO)
            self.watch_to_path[wd] = PROJECTS_DIR

            # Watch each project directory
            for project_dir in PROJECTS_DIR.iterdir():
                if project_dir.is_dir():
                    self.watch_project_dir(project_dir)

        # Watch plans directory
        if PLANS_DIR.exists():
            wd = self.inotify.add_watch(PLANS_DIR, watch_flags)
            self.watch_to_path[wd] = PLANS_DIR

        # Watch file-history root
        if FILE_HISTORY_DIR.exists():
            wd = self.inotify.add_watch(FILE_HISTORY_DIR, flags.CREATE | flags.MOVED_TO)
            self.watch_to_path[wd] = FILE_HISTORY_DIR

            # Watch each session directory
            for session_dir in FILE_HISTORY_DIR.iterdir():
                if session_dir.is_dir():
                    wd = self.inotify.add_watch(session_dir, flags.CREATE | flags.MOVED_TO)
                    self.watch_to_path[wd] = session_dir

        print(f"Set up {len(self.watch_to_path)} watches", file=sys.stderr)

    def initial_sync(self):
        """Index any files that haven't been indexed yet."""
        print("Starting initial sync...", file=sys.stderr)
        self.sync_all()
        print("Initial sync complete", file=sys.stderr)

    def sync_all(self):
        """Index new and changed files. Unchanged ones are skipped by mtime."""
        # Index all JSONL files
        if PROJECTS_DIR.exists():
            for project_dir in PROJECTS_DIR.iterdir():
                if project_dir.is_dir():
                    self.sync_project(project_dir)

        # Index all plan files
        if PLANS_DIR.exists():
            for plan_file in PLANS_DIR.glob("*.md"):
                self.index_plan_file(plan_file)

        # Index all snapshots
        if FILE_HISTORY_DIR.exists():
            for session_dir in FILE_HISTORY_DIR.iterdir():
                if not session_dir.is_dir():
                    continue
                # Snapshots are only ever added, which changes the directory's mtime.
                # Read it before listing so a file added meanwhile is seen next time.
                try:
                    mtime = session_dir.stat().st_mtime
                except OSError:
                    continue
                if self.dir_mtimes.get(session_dir) == mtime:
                    continue
                self.dir_mtimes[session_dir] = mtime
                for snapshot_file in session_dir.iterdir():
                    if snapshot_file.is_file() and '@v' in snapshot_file.name:
                        self.index_snapshot(snapshot_file)

        self.db.commit()

    def sync_project(self, project_dir):
        """Index a project's session transcripts and their subagent transcripts."""
        for jsonl_file in project_dir.glob("*.jsonl"):
            self.index_jsonl_incremental(jsonl_file)
        # Older Claude Code versions kept agent-*.jsonl next to the sessions (matched
        # above); newer ones keep them in <session>/subagents/
        for jsonl_file in project_dir.glob("*/subagents/*.jsonl"):
            self.index_jsonl_incremental(jsonl_file)

    def get_indexed_info(self, file_path):
        """Get indexing progress for a file."""
        row = self.db.execute(
            "SELECT byte_offset, file_mtime FROM indexed_files WHERE file_path = ?",
            (str(file_path),)
        ).fetchone()
        return dict(row) if row else None

    def update_indexed_info(self, file_path, byte_offset, mtime):
        """Update indexing progress for a file."""
        self.db.execute(
            "INSERT OR REPLACE INTO indexed_files (file_path, byte_offset, file_mtime) VALUES (?, ?, ?)",
            (str(file_path), byte_offset, mtime)
        )

    def index_jsonl_incremental(self, file_path):
        """Index a JSONL file. Always reparses entire file, INSERT OR IGNORE handles dupes."""
        if not file_path.exists():
            return

        try:
            stat = file_path.stat()
            current_mtime = stat.st_mtime
        except OSError:
            return

        # Skip if file unchanged since last index
        indexed = self.get_indexed_info(file_path)
        if indexed and indexed['file_mtime'] == current_mtime:
            return

        # Determine project and session from path
        if file_path.parent.name == "subagents":
            project_name = file_path.parents[2].name  # <project>/<session>/subagents/agent-*.jsonl
        else:
            project_name = file_path.parent.name
        session_id = file_path.stem
        is_agent = 'agent-' in file_path.name
        agent_id = session_id.replace('agent-', '') if is_agent else None

        # Read and index entire file (INSERT OR IGNORE handles duplicates)
        records_added = 0
        tool_names = {}  # tool_use_id -> tool_name, to label tool_output records
        try:
            with open(file_path, 'rb') as f:
                while True:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break

                    try:
                        rec = json.loads(line)
                        # Agent files carry their parent session in each record
                        rec_session = rec.get('sessionId') if is_agent else session_id
                        for vr in expand_record(rec, agent_id=agent_id):
                            tool_name = vr.get('tool_name')
                            tool_use_id = vr.get('tool_use_id', '')
                            vtype = vr.get('type', '')
                            if tool_use_id and vtype.endswith('tool_input'):
                                tool_names[tool_use_id] = tool_name
                            elif tool_use_id and vtype.endswith('tool_output') and tool_name is None:
                                tool_name = tool_names.get(tool_use_id)
                            self.db.execute("""
                                INSERT OR IGNORE INTO records
                                (uuid, timestamp, record_type, tool_name, tool_use_id, content,
                                 project_name, session_id, agent_id, source_file, byte_offset)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                vr.get('uuid'),
                                utc_index_time(rec.get('timestamp')),
                                vr.get('type'),
                                tool_name,
                                tool_use_id,
                                vr.get('content', ''),
                                project_name,
                                rec_session,
                                agent_id,
                                str(file_path),
                                line_start
                            ))
                            records_added += 1
                    except json.JSONDecodeError:
                        continue

        except IOError as e:
            print(f"Error reading {file_path}: {e}", file=sys.stderr)
            return

        self.update_indexed_info(file_path, 0, current_mtime)

        if records_added > 0:
            self.db.commit()
            print(f"Indexed {records_added} records from {file_path.name}", file=sys.stderr)

    def index_plan_file(self, file_path):
        """Index a plan markdown file."""
        if not file_path.exists():
            return

        try:
            stat = file_path.stat()
            current_mtime = stat.st_mtime
        except OSError:
            return

        indexed = self.get_indexed_info(file_path)

        if indexed and indexed['file_mtime'] == current_mtime:
            return  # Already indexed and unchanged

        # Remove old version if exists
        self.db.execute("DELETE FROM records WHERE source_file = ?", (str(file_path),))

        try:
            content = file_path.read_text()
        except IOError:
            return

        # Use file mtime as timestamp
        timestamp = mtime_to_utc_time(current_mtime)

        self.db.execute("""
            INSERT INTO records
            (uuid, timestamp, record_type, tool_name, content,
             project_name, session_id, agent_id, source_file, byte_offset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            file_path.stem,  # Use filename as uuid
            timestamp,
            'plan',
            None,
            content,
            None,
            None,
            None,
            str(file_path),
            0
        ))

        self.update_indexed_info(file_path, len(content), current_mtime)
        self.db.commit()
        print(f"Indexed plan: {file_path.name}", file=sys.stderr)

    def index_snapshot(self, file_path):
        """Index a file snapshot."""
        if not file_path.exists():
            return

        backup_id = file_path.name

        # Check if already indexed
        existing = self.db.execute(
            "SELECT 1 FROM snapshots WHERE backup_id = ?", (backup_id,)
        ).fetchone()

        if existing:
            return

        session_id = file_path.parent.name

        try:
            content = file_path.read_text()
            size = len(content)
            mtime = file_path.stat().st_mtime
        except (IOError, OSError):
            return

        timestamp = mtime_to_utc_time(mtime)

        # Extract original file path from JSONL if possible (we don't have it here)
        # For now, store backup_id as identifier

        self.db.execute("""
            INSERT INTO snapshots
            (backup_id, session_id, file_path, timestamp, size, content)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            backup_id,
            session_id,
            None,  # We don't know original path without parsing JSONL
            timestamp,
            size,
            content
        ))

        self.db.commit()
        print(f"Indexed snapshot: {backup_id}", file=sys.stderr)

    def handle_event(self, event):
        """Handle an inotify event."""
        if event.wd not in self.watch_to_path:
            return

        path = self.watch_to_path[event.wd]
        name = event.name

        if not name:
            return

        full_path = path / name

        # New project directory. One moved in whole brings files that raise no
        # events of their own, so index what it already holds.
        if path == PROJECTS_DIR and event.mask & (flags.CREATE | flags.MOVED_TO):
            if full_path.is_dir():
                self.watch_project_dir(full_path)
                print(f"Added watch for new project: {name}", file=sys.stderr)
                self.sync_project(full_path)
            return

        # New session directory in a project, or a subagents/ folder in a session directory
        if event.mask & (flags.CREATE | flags.MOVED_TO) and full_path.is_dir():
            if path.parent == PROJECTS_DIR:
                self.watch_session_dir(full_path)
                found = full_path.glob("subagents/*.jsonl")
            elif path.parent.parent == PROJECTS_DIR and name == "subagents":
                self.watch(full_path, flags.MODIFY | flags.CREATE | flags.MOVED_TO)
                found = full_path.glob("*.jsonl")
            else:
                return
            for jsonl_file in found:
                self.index_jsonl_incremental(jsonl_file)
            return

        # New file-history session directory
        if path == FILE_HISTORY_DIR and event.mask & (flags.CREATE | flags.MOVED_TO):
            if full_path.is_dir():
                try:
                    wd = self.inotify.add_watch(full_path, flags.CREATE | flags.MOVED_TO)
                    self.watch_to_path[wd] = full_path
                    print(f"Added watch for new session history: {name}", file=sys.stderr)
                except OSError:
                    pass
            return

        # JSONL file modified
        if name.endswith('.jsonl'):
            self.index_jsonl_incremental(full_path)
            return

        # Plan file modified
        if name.endswith('.md') and path == PLANS_DIR:
            self.index_plan_file(full_path)
            return

        # Snapshot file created
        if '@v' in name and path.parent == FILE_HISTORY_DIR:
            self.index_snapshot(full_path)
            return

    def run(self):
        """Main event loop."""
        self.init_db()
        if INotify is None:
            self.initial_sync()
            self.poll_loop()
            self.cleanup()
            return

        self.setup_watches()
        self.initial_sync()

        print("Daemon running, waiting for events...", file=sys.stderr)

        while self.running:
            try:
                # Read events with timeout for graceful shutdown
                events = self.inotify.read(timeout=1000)
                for event in events:
                    self.handle_event(event)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error in event loop: {e}", file=sys.stderr)
                time.sleep(1)

        self.cleanup()

    def poll_loop(self):
        """Rescan every POLL_INTERVAL seconds, for systems without inotify."""
        print(f"Daemon running, polling every {POLL_INTERVAL}s...", file=sys.stderr)

        while self.running:
            time.sleep(POLL_INTERVAL)
            try:
                self.sync_all()
            except Exception as e:
                print(f"Error in poll loop: {e}", file=sys.stderr)

    def cleanup(self):
        """Clean up resources."""
        if self.db:
            self.db.close()
        if self.inotify:
            self.inotify.close()
        print("Daemon stopped", file=sys.stderr)

    def stop(self):
        """Signal the daemon to stop."""
        self.running = False


def main():
    indexer = HistoryIndexer()

    # Handle signals for graceful shutdown
    def signal_handler(signum, frame):
        print(f"Received signal {signum}, shutting down...", file=sys.stderr)
        indexer.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    indexer.run()


if __name__ == "__main__":
    main()
