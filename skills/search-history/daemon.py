#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["inotify_simple; sys_platform == 'linux'"]
# ///
"""
Claude Code History Indexer Daemon

Follows ~/.claude and keeps an SQLite index of it for fast search, which is also
an archive: nothing is removed from the index when Claude Code deletes a file.

Indexed: the transcripts of sessions and subagents (messages, thinking, tool calls
with their full input, tool results, messages delivered mid-turn), the files tools
saved under <session>/tool-results/, subagent meta files, memory notes and plans
(each version), the prompt history with its pasted texts, and the file snapshots
of ~/.claude/file-history/. Images and other binary content are stored once each
in the blobs table; the text holds a placeholder with the hash, type and size.

Transcripts and snapshots are followed with inotify on Linux (polling elsewhere,
or when inotify_simple is missing); everything is rescanned every
FULL_SYNC_INTERVAL seconds, which also picks up the other files.

    daemon.py                   run until stopped
    daemon.py --once            bring the index up to date (migrating an older one) and exit
    daemon.py --ingest FILE --as PATH
                                index a transcript kept elsewhere (a backup, another machine)
                                as if it were at PATH under <claude-dir>/projects/
"""
import argparse
import ast
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import signal
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from inotify_simple import INotify
except ImportError:
    INotify = None  # No inotify: run() polls instead

# inotify event bits (linux/inotify.h)
IN_MODIFY, IN_MOVED_TO, IN_CREATE = 0x2, 0x80, 0x100

# Seconds between rescans of the transcripts when polling
POLL_INTERVAL = 5
# Seconds between full rescans (side files; transcripts and snapshots as a safety net)
FULL_SYNC_INTERVAL = 120
# A file under tool-results/ is stored once it has not changed for this long
SETTLE_SECONDS = 30
# Bytes that are text except for at most this share of invalid UTF-8 are indexed as text
INVALID_TEXT_SHARE = 0.01
# A file whose indexing failed this often in a row is next tried after the pause (seconds)
FAILURES_BEFORE_PAUSE = 3
RETRY_PAUSE = 600
# Files larger than this are not read whole (the daemon's memory is limited)
MAX_FILE_BYTES = 128 * 1024 * 1024
TRUNCATED_KEEP_BYTES = 16 * 1024 * 1024

# Import record parsing and time conversion from search_history.py
sys.path.insert(0, str(Path(__file__).parent))
from search_history import (CLAUDE_DIR, DB_PATH, INDEX_VERSION, TIME_FORMAT, _blob_block,  # noqa: E402
                            _path_to_project_name, _text_type, blob_placeholder, expand_record,
                            mtime_to_utc_time, utc_index_time, utc_to_local)

# Transcript lines that never hold anything to index and make up most of the bytes
SKIP_PREFIX = b'{"type":"api-request'

SCHEMA = """
-- Conversation items and side files, one row per searchable item
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    uuid TEXT,              -- first 12 characters of the transcript record's uuid
    timestamp TEXT,         -- UTC, "YYYY-MM-DD HH:MM:SS"
    record_type TEXT,
    tool_name TEXT,
    tool_use_id TEXT,
    content TEXT,           -- the text to search and show, in full
    project_name TEXT,
    session_id TEXT,
    agent_id TEXT,
    source_file TEXT,
    byte_offset INTEGER,    -- where the record's line starts in source_file
    model TEXT,
    is_meta INTEGER,        -- NULL: indexed before version 2 and the source is gone
    origin TEXT,            -- human, agent, coordinator, task-notification, ...
    parent_uuid TEXT,
    host TEXT,              -- machine the item was indexed on
    input_json TEXT,        -- tool calls: the exact input
    result_json TEXT,       -- tool results of edits: originalFile, structuredPatch, ...
    is_error INTEGER
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON records(timestamp);
CREATE INDEX IF NOT EXISTS idx_type ON records(record_type);
CREATE INDEX IF NOT EXISTS idx_tool ON records(tool_name);
CREATE INDEX IF NOT EXISTS idx_project ON records(project_name);
CREATE INDEX IF NOT EXISTS idx_session_type ON records(session_id, record_type);
CREATE INDEX IF NOT EXISTS idx_source_offset ON records(source_file, byte_offset, id);
CREATE INDEX IF NOT EXISTS idx_tool_use ON records(tool_use_id) WHERE tool_use_id != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_record
    ON records(uuid, record_type, COALESCE(tool_use_id, ''));

-- Images, documents and other binary content, each stored once
CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,  -- SHA-256 of data, hex
    media_type TEXT,
    size INTEGER,
    data BLOB
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_name TEXT,
    host TEXT,
    cwd TEXT,
    title TEXT,             -- Claude Code's generated title
    custom_title TEXT,      -- the user's rename
    agent_name TEXT,
    first_ts TEXT,          -- UTC
    last_ts TEXT,
    version TEXT,           -- Claude Code version that wrote last
    source_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_name);

-- Tool outputs saved to <session>/tool-results/ that a transcript refers to
CREATE TABLE IF NOT EXISTS persisted_outputs (
    path TEXT PRIMARY KEY,
    stored INTEGER          -- 1: the tool output row holds the whole file
);

-- Track indexing progress per file
CREATE TABLE IF NOT EXISTS indexed_files (
    file_path TEXT PRIMARY KEY,
    byte_offset INTEGER,
    file_mtime REAL,
    size INTEGER,
    tail_sig TEXT,          -- hash of the bytes before byte_offset, to notice a rewritten file
    index_version INTEGER DEFAULT 0
);
"""

SNAPSHOTS_SCHEMA = """
-- File snapshots (~/.claude/file-history/<session>/<hash>@v<N>)
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    backup_id TEXT,
    session_id TEXT,
    file_path TEXT,         -- the file this is a version of, from the transcript
    timestamp TEXT,         -- UTC: when the backup was made (the transcript's time, else the file's)
    size INTEGER,           -- bytes
    content TEXT,           -- NULL: known from the transcript only, or binary (blob_hash)
    version INTEGER,
    message_id TEXT,        -- the message before which the file was like this
    blob_hash TEXT,
    host TEXT,
    UNIQUE(session_id, backup_id)
);
"""
SNAPSHOT_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_snap_session ON snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_snap_file ON snapshots(file_path);
CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(timestamp);
"""

NEW_RECORD_COLUMNS = [("model", "TEXT"), ("is_meta", "INTEGER"), ("origin", "TEXT"),
                      ("parent_uuid", "TEXT"), ("host", "TEXT"), ("input_json", "TEXT"),
                      ("result_json", "TEXT"), ("is_error", "INTEGER")]
NEW_INDEXED_FILES_COLUMNS = [("size", "INTEGER"), ("tail_sig", "TEXT"),
                             ("index_version", "INTEGER DEFAULT 0")]

RECORD_COLUMNS = ("uuid", "timestamp", "record_type", "tool_name", "tool_use_id", "content",
                  "project_name", "session_id", "agent_id", "source_file", "byte_offset",
                  "model", "is_meta", "origin", "parent_uuid", "host", "input_json",
                  "result_json", "is_error")
INSERT_RECORD = (f"INSERT OR IGNORE INTO records ({', '.join(RECORD_COLUMNS)}) "
                 f"VALUES ({', '.join('?' * len(RECORD_COLUMNS))})")

# Python text of an image or document block, as index versions 0 and 1 stored it
LEGACY_BLOB_RE = re.compile(r"\{'type': '(?:image|document)', 'source': \{'type': 'base64'.*?\}\}", re.S)


def log(message):
    print(message, file=sys.stderr, flush=True)


def _local_to_utc_text(ts):
    """Convert a local time string (TIME_FORMAT, this process's zone) to UTC; unparseable as is."""
    try:
        dt = datetime.strptime(ts, TIME_FORMAT)
    except (TypeError, ValueError):
        return ts
    return dt.astimezone(timezone.utc).strftime(TIME_FORMAT)


def sniff_media_type(name, data):
    """Media type of a file from its first bytes, else from its name."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class FileContext:
    """What is known about a transcript file while its lines are indexed."""

    def __init__(self, path):
        self.path = path
        self.is_agent = path.name.startswith("agent-")
        if path.parent.name == "subagents":
            # <project>/<session>/subagents/agent-*.jsonl
            self.project_name = path.parents[2].name
            self.parent_session = path.parents[1].name
        else:
            # Older Claude Code versions kept agent files next to the sessions
            self.project_name = path.parent.name
            self.parent_session = None
        self.session_id = path.stem
        self.agent_id = path.stem[len("agent-"):] if self.is_agent else None
        self.tool_names = {}  # tool_use_id -> tool name, to label tool results
        # For the sessions table
        self.first_ts = self.last_ts = self.cwd = self.version = None
        self.title = self.custom_title = self.agent_name = None


class HistoryIndexer:
    def __init__(self, db_path=DB_PATH, claude_dir=CLAUDE_DIR, host=None):
        self.db_path = Path(db_path)
        self.claude_dir = Path(claude_dir)
        self.projects_dir = self.claude_dir / "projects"
        self.plans_dir = self.claude_dir / "plans"
        self.file_history_dir = self.claude_dir / "file-history"
        self.paste_cache_dir = self.claude_dir / "paste-cache"
        self.history_file = self.claude_dir / "history.jsonl"
        self.host = host or socket.gethostname().split(".")[0]
        self.db = None
        self.lock_file = None
        self.inotify = None
        self.watch_to_path = {}  # wd -> path
        self.dir_mtimes = {}  # snapshot dir -> mtime at last listing
        self.file_state = {}  # path -> (mtime, size) as last indexed
        self.seen_backups = set()  # (session, backup id) whose file path is recorded
        self.failures = {}  # path -> (failed attempts in a row, time of the last one)
        self.running = True

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def lock(self):
        """Allow one indexer per database: a second one would repeat its work."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(str(self.db_path) + ".lock", "w")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"Another indexer holds {self.lock_file.name}; not starting a second one")
            sys.exit(1)

    def init_db(self):
        """Open the database; create its schema or bring an older one up to date."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

        # Enable WAL mode for better concurrent access
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")

        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'records'").fetchone():
            self.db.executescript(SCHEMA + SNAPSHOTS_SCHEMA.format(name="snapshots") + SNAPSHOT_INDEXES)
            self.db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
            self.db.commit()
            return
        self.migrate()

    def migrate(self):
        """Bring an index written by an older daemon up to INDEX_VERSION, in place.

        Nothing is rebuilt from scratch: the index holds sessions whose files
        Claude Code has deleted, and those rows must survive.
        """
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= INDEX_VERSION:
            return
        has_data = self.db.execute("SELECT 1 FROM records LIMIT 1").fetchone() or \
            self.db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone()
        if has_data and version < 1:
            log("Migrating index to version 1 (UTC timestamps)...")
            self.migrate_to_v1()
            self.db.execute("PRAGMA user_version = 1")
            self.db.commit()
        if version < 2:
            log("Migrating index to version 2 (full tool inputs, blobs, side files, sessions)...")
            self.migrate_to_v2()

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
                    (self.file_history_dir / row['session_id'] / row['backup_id']).stat().st_mtime)
            except (OSError, TypeError):
                new = _local_to_utc_text(row['timestamp']) if old_was_local else row['timestamp']
            if new != row['timestamp']:
                snapshot_updates.append((new, row['id']))
        db.executemany("UPDATE snapshots SET timestamp = ? WHERE id = ?", snapshot_updates)

        log(f"Migrated: {n_updated} record and {len(snapshot_updates)} snapshot timestamps "
            f"changed; {len(unresolved)} records without readable source, old times taken as "
            f"{'local' if old_was_local else 'UTC'}")

    def migrate_to_v2(self):
        """Version 1 -> 2. Every step can be repeated, so an interrupted run is resumed
        by the next start; the version number is set at the end.

        1. Schema: new columns and tables; snapshots keyed by (session, backup id);
           the unused full-text tables dropped (searches match substrings and regular
           expressions, which those tables cannot serve).
        2. Every transcript still on disk is parsed again and its rows are updated in
           place (full tool inputs, placeholders for images, the new columns and types);
           rows are never deleted. This also indexes what version 1 skipped.
        3. Rows whose source file is gone keep their text; images inside it are moved
           to the blobs table and the text types are relabeled as far as the text shows.
        """
        db = self.db
        started = time.time()

        def columns(table):
            return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

        # 1. Schema
        for trigger in ("records_ai", "records_ad", "records_au", "snapshots_ai"):
            db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for table in ("records_fts", "snapshots_fts"):
            try:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            except sqlite3.Error as e:  # an SQLite without FTS5 cannot drop them
                log(f"Could not drop {table}: {e}")
        have = columns("records")
        for name, decl in NEW_RECORD_COLUMNS:
            if name not in have:
                db.execute(f"ALTER TABLE records ADD COLUMN {name} {decl}")
        have = columns("indexed_files")
        for name, decl in NEW_INDEXED_FILES_COLUMNS:
            if name not in have:
                db.execute(f"ALTER TABLE indexed_files ADD COLUMN {name} {decl}")
        if "version" not in columns("snapshots"):
            db.executescript("BEGIN;" + SNAPSHOTS_SCHEMA.format(name="snapshots_v2") + """
                INSERT INTO snapshots_v2 (id, backup_id, session_id, file_path, timestamp, size, content)
                    SELECT id, backup_id, session_id, file_path, timestamp, size, content FROM snapshots;
                DROP TABLE snapshots;
                ALTER TABLE snapshots_v2 RENAME TO snapshots;
                COMMIT;""")
        db.execute("DROP INDEX IF EXISTS idx_session")
        db.execute("DROP INDEX IF EXISTS idx_source")
        db.executescript(SCHEMA + SNAPSHOT_INDEXES)
        # Compaction summaries of agents were labeled as the main conversation's
        db.execute("UPDATE OR IGNORE records SET record_type = 'agent_summary' "
                   "WHERE record_type = 'main_summary' AND agent_id IS NOT NULL")
        db.commit()
        log(f"  schema ready ({time.time() - started:.0f} s)")

        # 2. Parse again what is on disk (index_version < 2 makes index_jsonl update rows)
        self.sync_all()
        log(f"  files on disk parsed again ({time.time() - started:.0f} s)")

        # 3a. Images that version 1 stored as text, in rows whose source is gone
        n_blobs = 0
        ids = [row[0] for row in db.execute(
            "SELECT id FROM records WHERE is_meta IS NULL "
            "AND content LIKE '%''source'': {''type'': ''base64''%'")]
        for rid in ids:
            content = db.execute("SELECT content FROM records WHERE id = ?", (rid,)).fetchone()[0]
            new = self.legacy_blobs_to_placeholders(content)
            if new != content:
                db.execute("UPDATE records SET content = ? WHERE id = ?", (new, rid))
                n_blobs += 1
        db.commit()

        # 3b. Text of rows that were not parsed again: what the text itself shows
        n_relabeled = 0
        for row in db.execute(
                "SELECT id, record_type, agent_id, substr(content, 1, 200) AS head FROM records "
                "WHERE is_meta IS NULL AND record_type IN ('main_user_text', 'agent_input_text')").fetchall():
            vtype, origin = _text_type(row["agent_id"], False, None, row["head"] or "")
            if vtype != row["record_type"] or origin:
                db.execute("UPDATE OR IGNORE records SET record_type = ?, origin = ? WHERE id = ?",
                           (vtype, origin, row["id"]))
                n_relabeled += 1

        # 3c. Sessions known only from their rows
        db.execute("""
            INSERT OR IGNORE INTO sessions (session_id, project_name, first_ts, last_ts, source_file)
            SELECT session_id, MIN(project_name), MIN(timestamp), MAX(timestamp), MIN(source_file)
            FROM records
            WHERE session_id IS NOT NULL AND agent_id IS NULL AND record_type LIKE 'main%'
              AND session_id NOT IN (SELECT session_id FROM sessions)
            GROUP BY session_id""")
        db.execute("""
            INSERT OR IGNORE INTO sessions (session_id, project_name, first_ts, last_ts)
            SELECT session_id, MIN(project_name), MIN(timestamp), MAX(timestamp) FROM records
            WHERE session_id IS NOT NULL AND agent_id IS NOT NULL
              AND session_id NOT IN (SELECT session_id FROM sessions)
            GROUP BY session_id""")

        # 3d. Snapshots that version 1 stored. It read the backup files as text with the
        # line ends translated (CRLF became LF) and counted characters: take the exact
        # content of the files that are still there, and count bytes.
        n_exact = 0
        for row in db.execute("SELECT id, session_id, backup_id, content FROM snapshots "
                              "WHERE host IS NULL AND content IS NOT NULL").fetchall():
            try:
                data = (self.file_history_dir / row["session_id"] / row["backup_id"]).read_bytes()
                text = data.decode("utf-8")
            except (OSError, TypeError, ValueError):
                continue
            # Only the line ends may differ; anything else is not the file version 1 read
            if text != row["content"] and text.replace("\r\n", "\n").replace("\r", "\n") == row["content"]:
                db.execute("UPDATE snapshots SET content = ? WHERE id = ?", (text, row["id"]))
                n_exact += 1
        db.execute("UPDATE snapshots SET size = LENGTH(CAST(content AS BLOB)) "
                   "WHERE host IS NULL AND content IS NOT NULL")

        # 3e. What version 1 indexed was indexed on this machine
        db.execute("UPDATE records SET host = ? WHERE host IS NULL", (self.host,))
        db.execute("UPDATE snapshots SET host = ? WHERE host IS NULL", (self.host,))

        db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
        db.commit()
        log(f"  {n_blobs} rows with images in their text converted, {n_relabeled} text rows "
            f"relabeled, {n_exact} snapshots given their exact line ends ({time.time() - started:.0f} s)")
        log(f"Migrated to version {INDEX_VERSION} in {time.time() - started:.0f} s")
        # Give back the space of the dropped tables and of the base64 text. The migration
        # is complete without this: if it fails (no disk space for the copy, a reader in
        # the way), the freed pages are reused instead.
        try:
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log(f"  compacted ({time.time() - started:.0f} s)")
        except sqlite3.Error as e:
            log(f"  not compacted, the file keeps its size: {e}")

    def legacy_blobs_to_placeholders(self, content):
        """Replace image and document blocks that versions 0 and 1 stored as Python text."""
        def replace(match):
            try:
                block = ast.literal_eval(match.group(0))
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return match.group(0)
            if not isinstance(block, dict):
                return match.group(0)
            return _blob_block(block, self.store_blob) or match.group(0)
        return LEGACY_BLOB_RE.sub(replace, content)

    def store_blob(self, data, media_type):
        """Keep binary content once, under its SHA-256. Returns the hash."""
        digest = hashlib.sha256(data).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO blobs (hash, media_type, size, data) VALUES (?, ?, ?, ?)",
                        (digest, media_type, len(data), data))
        return digest

    def get_indexed_info(self, file_path):
        """Get indexing progress for a file."""
        row = self.db.execute(
            "SELECT byte_offset, file_mtime, size, tail_sig, index_version FROM indexed_files "
            "WHERE file_path = ?", (str(file_path),)).fetchone()
        return dict(row) if row else None

    def update_indexed_info(self, file_path, byte_offset, st, tail_sig=None):
        """Update indexing progress for a file."""
        self.db.execute(
            "INSERT OR REPLACE INTO indexed_files "
            "(file_path, byte_offset, file_mtime, size, tail_sig, index_version) VALUES (?, ?, ?, ?, ?, ?)",
            (str(file_path), byte_offset, st.st_mtime, st.st_size, tail_sig, INDEX_VERSION))
        self.file_state[str(file_path)] = (st.st_mtime, st.st_size)

    def unchanged(self, key, st):
        """Whether the file is as it was when this index version last indexed it."""
        state = (st.st_mtime, st.st_size)
        if self.file_state.get(key) == state:
            return True
        info = self.get_indexed_info(key)
        if info and info['file_mtime'] == st.st_mtime and info['size'] == st.st_size \
                and (info['index_version'] or 0) >= INDEX_VERSION:
            self.file_state[key] = state
            return True
        return False

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------

    @staticmethod
    def tail_sig(f, offset):
        """Hash of the up to 256 bytes before offset."""
        n = min(offset, 256)
        f.seek(offset - n)
        return hashlib.sha1(f.read(n)).hexdigest()

    def resume_offset(self, path, st, info):
        """Where to go on reading a file that only grew, or None if it must be read again."""
        offset = info['byte_offset'] or 0
        if (info['index_version'] or 0) < INDEX_VERSION or offset > st.st_size:
            return None
        if offset == 0:
            return 0
        try:
            with open(path, 'rb') as f:
                if self.tail_sig(f, offset) != info['tail_sig']:
                    return None  # rewritten (Claude Code compacts transcripts)
        except OSError:
            return None
        return offset

    def index_jsonl(self, file_path, as_path=None, force=False):
        """Index a transcript: the lines added since last time, or all of it.

        A file that only grew is read from the stored offset and its items are
        inserted. A file known from an older index version, or one that was
        rewritten, is read from the start and its rows are updated in place.
        as_path: the path to record instead of file_path (see --ingest).
        Returns the number of rows added.
        """
        path = Path(file_path)
        key = str(as_path or path)
        try:
            st = path.stat()
        except OSError:
            return 0
        if not force and self.unchanged(key, st):
            return 0

        info = self.get_indexed_info(key)
        offset, update = 0, force
        if info and not force:
            offset = self.resume_offset(path, st, info)
            if offset is None:
                offset, update = 0, True

        ctx = FileContext(Path(key))
        added = 0
        try:
            with open(path, 'rb') as f:
                f.seek(offset)
                while True:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    complete = line.endswith(b"\n")
                    rec = None
                    if not line.startswith(SKIP_PREFIX):
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            rec = None
                    if rec is None and not complete:
                        break  # the last line is still being written
                    offset = f.tell()
                    if isinstance(rec, dict):
                        added += self.index_record(rec, ctx, line_start, update)
                sig = self.tail_sig(f, offset)
        except OSError as e:
            log(f"Error reading {path}: {e}")
            self.db.rollback()
            self.file_state.clear()
            self.seen_backups.clear()
            return 0

        self.finish_session(ctx, new_file=info is None)
        self.update_indexed_info(key, offset, st, sig)
        self.db.commit()
        if added:
            log(f"Indexed {added} records from {path.name}")
        return added

    def index_record(self, rec, ctx, line_start, update):
        """Index one transcript line. Returns the number of rows added."""
        rec_type = rec.get("type")
        if rec_type in ("user", "assistant", "attachment"):
            if not rec.get("uuid"):
                # Rows are told apart by the record's uuid. Claude Code gives every message
                # one; should one lack it, its content names it, so that it is not dropped.
                digest = hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()
                rec = dict(rec, uuid="~" + digest[:11])
            added = 0
            for vr in expand_record(rec, agent_id=ctx.agent_id, blob_sink=self.store_blob,
                                    read_persisted=self.read_persisted):
                added += self.put_record(vr, rec, ctx, line_start, update)
            if not ctx.is_agent and rec_type != "attachment":
                ts = utc_index_time(rec.get("timestamp"))
                if ts:
                    ctx.first_ts = min(ctx.first_ts or ts, ts)
                    ctx.last_ts = max(ctx.last_ts or ts, ts)
                ctx.cwd = ctx.cwd or rec.get("cwd")
                ctx.version = rec.get("version") or ctx.version
            return added
        if ctx.is_agent:
            return 0
        if rec_type == "ai-title":
            ctx.title = rec.get("aiTitle") or ctx.title
        elif rec_type == "custom-title":
            ctx.custom_title = rec.get("customTitle") or ctx.custom_title
        elif rec_type == "agent-name":
            ctx.agent_name = rec.get("agentName") or ctx.agent_name
        elif rec_type == "file-history-snapshot":
            snapshot = rec.get("snapshot")
            backups = snapshot.get("trackedFileBackups") if isinstance(snapshot, dict) else None
            if isinstance(backups, dict):
                for tracked_path, backup in backups.items():
                    self.note_backup(ctx.session_id, rec.get("messageId"), tracked_path, backup)
        elif rec_type == "file-history-delta":
            self.note_backup(ctx.session_id, rec.get("messageId"), rec.get("trackingPath"),
                             rec.get("backup"))
        return 0

    def put_record(self, vr, rec, ctx, line_start, update):
        """Insert one item, or with update=True bring its existing row up to date."""
        vtype = vr["type"]
        tool_name = vr["tool_name"]
        tool_use_id = vr["tool_use_id"] or ""
        if tool_use_id and vtype.endswith("tool_input"):
            ctx.tool_names[tool_use_id] = tool_name
        elif tool_use_id and vtype.endswith("tool_output") and tool_name is None:
            tool_name = ctx.tool_names.get(tool_use_id)
            if tool_name is None and not update:
                # The call was indexed in an earlier pass over this file
                row = self.db.execute(
                    "SELECT tool_name FROM records WHERE tool_use_id = ? AND tool_use_id != '' "
                    "AND record_type IN ('main_tool_input', 'agent_tool_input') LIMIT 1",
                    (tool_use_id,)).fetchone()
                tool_name = row[0] if row else None
        if ctx.is_agent:
            # Agent files carry their parent session in each record
            session_id = rec.get("sessionId") or ctx.parent_session
        else:
            session_id = ctx.session_id
        timestamp = utc_index_time(rec.get("timestamp"))
        source_file = str(ctx.path)
        key = (vr["uuid"], vtype, tool_use_id)

        if update:
            if vr["legacy_type"]:
                # Index version 1 gave the item another type. Only a row from the same
                # kind of transcript (main or agent) is this item.
                self.db.execute(
                    "UPDATE OR IGNORE records SET record_type = ? WHERE uuid = ? AND record_type = ? "
                    "AND COALESCE(tool_use_id, '') = ? AND (agent_id IS NOT NULL) = ?",
                    (vtype, vr["uuid"], vr["legacy_type"], tool_use_id, int(ctx.is_agent)))
            # The row of this file's copy of the record is brought up to date. A row that
            # another file's copy owns is left to that file, unless nothing has parsed it
            # again since version 1 (is_meta IS NULL: that file is gone); it keeps its place.
            # A saved tool output whose file is gone meanwhile keeps the text stored earlier:
            # the transcript has only a preview of it.
            cur = self.db.execute(
                "UPDATE records SET timestamp = ?, tool_name = COALESCE(?, tool_name), "
                "content = CASE WHEN ? THEN content ELSE ? END, "
                "byte_offset = CASE WHEN source_file = ? THEN ? ELSE byte_offset END, "
                "model = ?, is_meta = ?, origin = ?, parent_uuid = ?, host = COALESCE(host, ?), "
                "input_json = ?, result_json = ?, is_error = ? "
                "WHERE uuid = ? AND record_type = ? AND COALESCE(tool_use_id, '') = ? "
                "AND (source_file = ? OR is_meta IS NULL)",
                (timestamp, tool_name, int(vr["persisted_missing"]), vr["content"], source_file,
                 line_start, vr["model"], int(vr["is_meta"]), vr["origin"], vr["parent_uuid"], self.host,
                 vr["input_json"], vr["result_json"], int(vr["is_error"]), *key, source_file))
            if cur.rowcount:
                return 0
        cur = self.db.execute(INSERT_RECORD, (
            vr["uuid"], timestamp, vtype, tool_name, tool_use_id, vr["content"],
            ctx.project_name, session_id, ctx.agent_id, source_file, line_start,
            vr["model"], int(vr["is_meta"]), vr["origin"], vr["parent_uuid"], self.host,
            vr["input_json"], vr["result_json"], int(vr["is_error"])))
        return cur.rowcount

    def finish_session(self, ctx, new_file):
        """Record what a main transcript told about its session."""
        if ctx.is_agent:
            return
        found = (ctx.first_ts, ctx.cwd, ctx.version, ctx.title, ctx.custom_title, ctx.agent_name)
        if not new_file and not any(found):
            return
        row = self.db.execute("SELECT first_ts, last_ts FROM sessions WHERE session_id = ?",
                              (ctx.session_id,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO sessions (session_id, project_name, host, cwd, title, custom_title, "
                "agent_name, first_ts, last_ts, version, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ctx.session_id, ctx.project_name, self.host, ctx.cwd, ctx.title, ctx.custom_title,
                 ctx.agent_name, ctx.first_ts, ctx.last_ts, ctx.version, str(ctx.path)))
            return
        first = min(filter(None, (row["first_ts"], ctx.first_ts)), default=None)
        last = max(filter(None, (row["last_ts"], ctx.last_ts)), default=None)
        self.db.execute(
            "UPDATE sessions SET project_name = COALESCE(project_name, ?), host = COALESCE(host, ?), "
            "cwd = COALESCE(cwd, ?), title = COALESCE(?, title), custom_title = COALESCE(?, custom_title), "
            "agent_name = COALESCE(?, agent_name), first_ts = ?, last_ts = ?, version = COALESCE(?, version), "
            "source_file = COALESCE(source_file, ?) WHERE session_id = ?",
            (ctx.project_name, self.host, ctx.cwd, ctx.title, ctx.custom_title, ctx.agent_name,
             first, last, ctx.version, str(ctx.path), ctx.session_id))

    def read_persisted(self, path_str):
        """The whole text of a tool output that Claude Code saved to <session>/tool-results/.

        The transcript holds only a preview of such an output. None if the file
        is gone, binary, or not below this Claude directory.
        """
        path = Path(path_str)
        if path.parent.name != "tool-results":
            return None
        try:
            path.resolve().relative_to(self.projects_dir.resolve())
        except (ValueError, OSError):
            return None
        text = self.read_text(path)
        if text is None:
            self.db.execute("INSERT OR IGNORE INTO persisted_outputs (path, stored) VALUES (?, 0)", (path_str,))
            return None
        self.db.execute("INSERT OR REPLACE INTO persisted_outputs (path, stored) VALUES (?, 1)", (path_str,))
        # The tool output row now holds the file; a row of its own would repeat it
        self.db.execute("DELETE FROM records WHERE record_type = 'tool_file' AND source_file = ?", (path_str,))
        return text

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    @staticmethod
    def read_bytes(path):
        """(data, note): a file's bytes; of a huge file only the start, with a note saying so."""
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size <= MAX_FILE_BYTES:
                return f.read(), ""
            return f.read(TRUNCATED_KEEP_BYTES), (
                f"\n[truncated: the file has {size} bytes, the first {TRUNCATED_KEEP_BYTES} are kept]")

    def text_of(self, data):
        """The text to index for a file's bytes, or None if they are not text.

        Text with a few bytes that are not valid UTF-8 (a character cut in two, a stray
        Latin-1 letter in a command's output) is still text: those bytes become U+FFFD,
        and the exact bytes are kept as a blob that a last line names.
        """
        if b"\x00" in data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        text = data.decode("utf-8", errors="replace")
        if text.count("\ufffd") > len(text) * INVALID_TEXT_SHARE:
            return None
        digest = self.store_blob(data, "text/plain")
        return (f"{text}\n[not valid UTF-8 in places (shown as \ufffd); the exact bytes: "
                f"{blob_placeholder('file', digest, 'text/plain', len(data))}]")

    def read_text(self, path):
        """A file's text, or None if it is missing or not text."""
        try:
            data, note = self.read_bytes(path)
        except OSError:
            return None
        if note:  # only the start was read; the cut may have split a character
            if b"\x00" in data:
                return None
            return data.decode("utf-8", errors="ignore") + note
        return self.text_of(data)

    def text_or_blob(self, name, data):
        """(text, None) for text, else (None, placeholder) with the bytes stored as a blob."""
        text = self.text_of(data)
        if text is not None:
            return text, None
        media_type = sniff_media_type(name, data)
        digest = self.store_blob(data, media_type)
        return None, blob_placeholder("file", digest, media_type, len(data))

    def index_versioned_file(self, path, record_type, name=None, project=None, session=None,
                             agent_id=None, label=None):
        """Index a file that may change (a memory note, a plan, a subagent's meta file):
        one row per distinct content, so earlier versions stay."""
        key = str(path)
        try:
            st = path.stat()
            if self.unchanged(key, st):
                return 0
            data, note = self.read_bytes(path)
        except OSError:
            return 0
        text, placeholder = self.text_or_blob(path.name, data)
        if text is None:
            text = f"{name or path.name}\n{placeholder}"
        if label is not None:
            label = label(text)
        uuid = hashlib.sha256(f"{record_type}\0{key}\0".encode() + data).hexdigest()[:12]
        cur = self.db.execute(INSERT_RECORD, (
            uuid, mtime_to_utc_time(st.st_mtime), record_type, label or name or path.name, "",
            text + note, project, session, agent_id, key, 0, None, 0, None, None, self.host,
            None, None, 0))
        self.update_indexed_info(key, 0, st)
        return cur.rowcount

    def index_tool_file(self, path, project, session, now):
        """Index a file a tool saved under <session>/tool-results/: its text in full, or for
        a binary file (a PDF, an image) a copy in the blobs table and a row with its name,
        type and size. One row per file, updated if the file changes."""
        key = str(path)
        try:
            st = path.stat()
            if now - st.st_mtime < SETTLE_SECONDS or self.unchanged(key, st):
                return 0
            row = self.db.execute("SELECT stored FROM persisted_outputs WHERE path = ?", (key,)).fetchone()
            if row and row[0]:
                self.update_indexed_info(key, 0, st)  # in its tool output row already
                return 0
            data, note = self.read_bytes(path)
        except OSError:
            return 0
        text, placeholder = self.text_or_blob(path.name, data)
        if placeholder is None:
            media_type, body = mimetypes.guess_type(path.name)[0] or "text/plain", text
        else:
            media_type, body = placeholder.split(" ")[2], placeholder
        content = f"{path.name} ({media_type}, {st.st_size} bytes)\n{body}{note}"
        uuid = hashlib.sha256(f"tool_file\0{key}".encode()).hexdigest()[:12]
        timestamp = mtime_to_utc_time(st.st_mtime)
        cur = self.db.execute(
            "UPDATE records SET content = ?, timestamp = ? WHERE uuid = ? AND record_type = 'tool_file' "
            "AND COALESCE(tool_use_id, '') = ''", (content, timestamp, uuid))
        added = 0
        if not cur.rowcount:
            added = self.db.execute(INSERT_RECORD, (
                uuid, timestamp, "tool_file", path.name, "", content, project, session, None, key, 0,
                None, 0, None, None, self.host, None, None, 0)).rowcount
        self.update_indexed_info(key, 0, st)
        return added

    def index_prompt_history(self):
        """Index new lines of history.jsonl: what was typed at the prompt, with pasted texts."""
        path = self.history_file
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            return 0
        if self.unchanged(key, st):
            return 0
        info = self.get_indexed_info(key)
        offset = (self.resume_offset(path, st, info) if info else 0) or 0
        added = 0
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    if not line.endswith(b"\n"):
                        break
                    entry = None
                offset = f.tell()
                if isinstance(entry, dict):
                    added += self.put_prompt(entry, key)
            sig = self.tail_sig(f, offset)
        self.update_indexed_info(key, offset, st, sig)
        return added

    def put_prompt(self, entry, source_file):
        display = entry.get("display")
        if not isinstance(display, str):
            return 0
        parts = [display]
        pasted = entry.get("pastedContents")
        for paste_id, paste in sorted(pasted.items()) if isinstance(pasted, dict) else []:
            if not isinstance(paste, dict):
                continue
            text = paste.get("content")
            if not isinstance(text, str) and isinstance(paste.get("contentHash"), str):
                text = self.read_text(self.paste_cache_dir / f"{paste['contentHash']}.txt")
            if isinstance(text, str):
                parts.append(f"[Pasted {paste.get('type', 'text')} #{paste_id}]\n{text}")
        ts = entry.get("timestamp")
        timestamp = mtime_to_utc_time(ts / 1000) if isinstance(ts, (int, float)) else ""
        session = entry.get("sessionId") if isinstance(entry.get("sessionId"), str) else None
        project = entry.get("project")
        uuid = hashlib.sha256(f"{session}\0{ts}\0{display}".encode()).hexdigest()[:12]
        return self.db.execute(INSERT_RECORD, (
            uuid, timestamp, "prompt_history", None, "", "\n".join(parts),
            _path_to_project_name(project) if isinstance(project, str) else None, session, None,
            source_file, 0, None, 0, "human", None, self.host, None, None, 0)).rowcount

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def note_backup(self, session_id, message_id, tracked_path, backup):
        """Record which file a snapshot is a version of, as a transcript tells it."""
        if not isinstance(backup, dict) or not isinstance(tracked_path, str):
            return
        backup_id = backup.get("backupFileName")
        if not isinstance(backup_id, str) or not backup_id:
            return  # the file did not exist before the edit
        key = (session_id, backup_id)
        if key in self.seen_backups:
            return
        self.seen_backups.add(key)
        version = backup.get("version") if isinstance(backup.get("version"), int) else None
        self.db.execute(
            "INSERT INTO snapshots (backup_id, session_id, file_path, timestamp, version, message_id, host) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, backup_id) DO UPDATE SET "
            "file_path = COALESCE(snapshots.file_path, excluded.file_path), "
            "version = COALESCE(snapshots.version, excluded.version), "
            "message_id = COALESCE(snapshots.message_id, excluded.message_id), "
            "timestamp = COALESCE(excluded.timestamp, snapshots.timestamp)",
            (backup_id, session_id, tracked_path, utc_index_time(backup.get("backupTime")) or None,
             version, message_id if isinstance(message_id, str) else None, self.host))

    def index_snapshot(self, file_path):
        """Index a file snapshot. Returns the number of snapshots stored."""
        backup_id = file_path.name
        session_id = file_path.parent.name
        row = self.db.execute(
            "SELECT content IS NOT NULL OR blob_hash IS NOT NULL FROM snapshots "
            "WHERE session_id = ? AND backup_id = ?", (session_id, backup_id)).fetchone()
        if row and row[0]:
            return 0
        try:
            data = file_path.read_bytes()
            mtime = file_path.stat().st_mtime
        except OSError:
            return 0
        content, blob_hash = None, None
        if b"\x00" not in data:
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        if content is None:
            blob_hash = self.store_blob(data, sniff_media_type(backup_id, data))
        self.db.execute(
            "INSERT INTO snapshots (backup_id, session_id, timestamp, size, content, blob_hash, host) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, backup_id) DO UPDATE SET "
            "timestamp = COALESCE(snapshots.timestamp, excluded.timestamp), size = excluded.size, "
            "content = excluded.content, blob_hash = excluded.blob_hash, "
            "host = COALESCE(snapshots.host, excluded.host)",
            (backup_id, session_id, mtime_to_utc_time(mtime), len(data), content, blob_hash, self.host))
        return 1

    def index_snapshot_dir(self, session_dir):
        """Index the snapshots of one session that are not in the index yet."""
        # Snapshots are only ever added, which changes the directory's mtime.
        # Read it before listing so a file added meanwhile is seen next time.
        try:
            mtime = session_dir.stat().st_mtime
            if self.dir_mtimes.get(session_dir) == mtime:
                return 0
            files = [f for f in session_dir.iterdir() if '@v' in f.name and f.is_file()]
        except OSError:
            return 0
        added = sum(self.index_snapshot(f) for f in files)
        self.db.commit()
        self.dir_mtimes[session_dir] = mtime
        if added:
            log(f"Indexed {added} snapshots of {session_dir.name}")
        return added

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def attempt(self, what, path, *args, **kwargs):
        """Run an indexing step for one file; a failure is logged and does not stop the daemon.

        A file that failed FAILURES_BEFORE_PAUSE times in a row is left alone for
        RETRY_PAUSE seconds and then tried again.
        """
        key = str(path)
        count, last = self.failures.get(key, (0, 0.0))
        if count >= FAILURES_BEFORE_PAUSE and time.monotonic() - last < RETRY_PAUSE:
            return 0
        try:
            result = what(path, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the indexing
            self.failures[key] = (count + 1, time.monotonic())
            log(f"Error indexing {path}: {type(e).__name__}: {e}")
            self.db.rollback()
            # These may name rows that were just rolled back
            self.file_state.clear()
            self.seen_backups.clear()
            return 0
        self.failures.pop(key, None)
        return result or 0

    def initial_sync(self):
        """Index any files that haven't been indexed yet."""
        log("Starting initial sync...")
        self.sync_all()
        log("Initial sync complete")

    def sync_all(self):
        """Index new and changed files of every kind. Unchanged ones are skipped by mtime."""
        self.sync_transcripts()
        self.sync_side_files()

    def sync_transcripts(self):
        """Transcripts and snapshots: what inotify follows, rescanned as a safety net."""
        for project_dir in self.subdirs(self.projects_dir):
            self.sync_project(project_dir)
        for session_dir in self.subdirs(self.file_history_dir):
            self.attempt(self.index_snapshot_dir, session_dir)
        self.db.commit()

    def sync_project(self, project_dir):
        """Index a project's session transcripts and their subagent transcripts."""
        try:
            files = sorted(project_dir.glob("*.jsonl"))
            # Older Claude Code versions kept agent-*.jsonl next to the sessions (matched
            # above); newer ones keep them in <session>/subagents/
            files += sorted(project_dir.glob("*/subagents/*.jsonl"))
        except OSError:
            return
        for jsonl_file in files:
            self.attempt(self.index_jsonl, jsonl_file)

    @staticmethod
    def subdirs(path):
        """Real subdirectories of path (a symlink to another project's folder would repeat it)."""
        try:
            return sorted(d for d in path.iterdir() if d.is_dir() and not d.is_symlink())
        except OSError:
            return []

    def sync_side_files(self):
        """The files next to the transcripts: tool results, subagent meta files, memory
        notes, plans, the prompt history and the paste cache."""
        now = time.time()
        added = 0
        for project_dir in self.subdirs(self.projects_dir):
            project = project_dir.name
            memory_dir = project_dir / "memory"
            if memory_dir.is_dir() and not memory_dir.is_symlink():
                for root, _dirs, names in os.walk(memory_dir):
                    for name in sorted(names):
                        path = Path(root) / name
                        added += self.attempt(self.index_versioned_file, path, "memory",
                                              name=str(path.relative_to(memory_dir)), project=project)
            for session_dir in self.subdirs(project_dir):
                session = session_dir.name
                try:
                    meta_files = sorted((session_dir / "subagents").glob("agent-*.meta.json"))
                    tool_files = sorted(f for f in (session_dir / "tool-results").iterdir() if f.is_file()) \
                        if (session_dir / "tool-results").is_dir() else []
                except OSError:
                    continue
                for path in meta_files:
                    added += self.attempt(
                        self.index_versioned_file, path, "subagent_meta", project=project, session=session,
                        agent_id=path.name[len("agent-"):-len(".meta.json")], label=self.agent_type)
                for path in tool_files:
                    added += self.attempt(self.index_tool_file, path, project, session, now)
            self.db.commit()
        for directory, pattern, record_type in ((self.plans_dir, "*.md", "plan"),
                                                (self.paste_cache_dir, "*", "paste")):
            try:
                files = sorted(f for f in directory.glob(pattern) if f.is_file())
            except OSError:
                files = []
            for path in files:
                added += self.attempt(self.index_versioned_file, path, record_type)
        added += self.attempt(lambda _path: self.index_prompt_history(), self.history_file)
        self.db.commit()
        if added:
            log(f"Indexed {added} side files and prompts")

    @staticmethod
    def agent_type(meta_text):
        """A subagent's type, from its meta file (shown as the row's label)."""
        try:
            meta = json.loads(meta_text)
        except ValueError:
            return None
        if isinstance(meta, dict) and isinstance(meta.get("agentType"), str):
            return meta["agentType"]
        return None

    # ------------------------------------------------------------------
    # Watching
    # ------------------------------------------------------------------

    def watch(self, path, mask):
        """Add an inotify watch, unless the directory vanished meanwhile."""
        try:
            wd = self.inotify.add_watch(path, mask)
        except OSError:
            return
        self.watch_to_path[wd] = path

    def watch_project_dir(self, project_dir):
        """Watch a project directory and the subagents/ folders of its sessions."""
        self.watch(project_dir, IN_MODIFY | IN_CREATE | IN_MOVED_TO)
        for session_dir in self.subdirs(project_dir):
            self.watch_session_dir(session_dir)

    def watch_session_dir(self, session_dir):
        """Watch a session directory for its subagents/ folder, and that folder for agent transcripts."""
        self.watch(session_dir, IN_CREATE | IN_MOVED_TO)
        subagents_dir = session_dir / "subagents"
        if subagents_dir.is_dir():
            self.watch(subagents_dir, IN_MODIFY | IN_CREATE | IN_MOVED_TO)

    def setup_watches(self):
        """Set up inotify watches on all relevant directories."""
        self.inotify = INotify()

        # Projects root, and each project directory
        if self.projects_dir.exists():
            self.watch(self.projects_dir, IN_CREATE | IN_MOVED_TO)
            for project_dir in self.subdirs(self.projects_dir):
                self.watch_project_dir(project_dir)

        # File-history root, and each session directory
        if self.file_history_dir.exists():
            self.watch(self.file_history_dir, IN_CREATE | IN_MOVED_TO)
            for session_dir in self.subdirs(self.file_history_dir):
                self.watch(session_dir, IN_CREATE | IN_MOVED_TO)

        log(f"Set up {len(self.watch_to_path)} watches")

    def handle_event(self, event):
        """Handle an inotify event."""
        path = self.watch_to_path.get(event.wd)
        name = event.name
        if path is None or not name:
            return
        full_path = path / name

        # A new directory. One moved in whole brings files that raise no events
        # of their own, so index what it already holds.
        if event.mask & (IN_CREATE | IN_MOVED_TO) and full_path.is_dir():
            if path == self.projects_dir:
                self.watch_project_dir(full_path)
                log(f"Added watch for new project: {name}")
                self.sync_project(full_path)
            elif path == self.file_history_dir:
                self.watch(full_path, IN_CREATE | IN_MOVED_TO)
                log(f"Added watch for new session history: {name}")
                self.attempt(self.index_snapshot_dir, full_path)
            elif path.parent == self.projects_dir:
                # A session directory in a project
                self.watch_session_dir(full_path)
                for jsonl_file in sorted(full_path.glob("subagents/*.jsonl")):
                    self.attempt(self.index_jsonl, jsonl_file)
            elif path.parent.parent == self.projects_dir and name == "subagents":
                self.watch(full_path, IN_MODIFY | IN_CREATE | IN_MOVED_TO)
                for jsonl_file in sorted(full_path.glob("*.jsonl")):
                    self.attempt(self.index_jsonl, jsonl_file)
            return

        # Transcript written
        if name.endswith('.jsonl'):
            self.attempt(self.index_jsonl, full_path)

        # Snapshot file created
        elif '@v' in name and path.parent == self.file_history_dir:
            if self.attempt(self.index_snapshot, full_path):
                self.db.commit()
                log(f"Indexed snapshot: {name}")

    def run(self):
        """Main event loop."""
        self.lock()
        self.init_db()
        if INotify is None:
            if sys.platform == "linux":
                log("inotify_simple is not installed (run the daemon with uv, or pip install "
                    "inotify_simple): polling instead")
            self.initial_sync()
            self.poll_loop()
            self.cleanup()
            return

        self.setup_watches()
        self.initial_sync()

        log("Daemon running, waiting for events...")
        last_full_sync = time.monotonic()

        while self.running:
            try:
                # Read events with timeout for graceful shutdown
                events = self.inotify.read(timeout=1000)
                for event in events:
                    self.handle_event(event)
                if time.monotonic() - last_full_sync >= FULL_SYNC_INTERVAL:
                    self.sync_all()
                    last_full_sync = time.monotonic()
            except KeyboardInterrupt:
                break
            except Exception as e:  # noqa: BLE001
                log(f"Error in event loop: {type(e).__name__}: {e}")
                time.sleep(1)

        self.cleanup()

    def poll_loop(self):
        """Rescan the transcripts every POLL_INTERVAL seconds, for systems without inotify."""
        log(f"Daemon running, polling every {POLL_INTERVAL}s...")
        last_full_sync = time.monotonic()

        while self.running:
            time.sleep(POLL_INTERVAL)
            try:
                self.sync_transcripts()
                if time.monotonic() - last_full_sync >= FULL_SYNC_INTERVAL:
                    self.sync_side_files()
                    last_full_sync = time.monotonic()
            except Exception as e:  # noqa: BLE001
                log(f"Error in poll loop: {type(e).__name__}: {e}")

    def cleanup(self):
        """Clean up resources."""
        if self.db:
            self.db.close()
        if self.inotify:
            self.inotify.close()
        log("Daemon stopped")

    def stop(self):
        """Signal the daemon to stop."""
        self.running = False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Index Claude Code's history into SQLite. Without options: run until stopped.")
    parser.add_argument("--db", default=str(DB_PATH), help="index database (default: %(default)s)")
    parser.add_argument("--claude-dir", default=str(CLAUDE_DIR),
                        help="Claude Code's directory (default: %(default)s)")
    parser.add_argument("--once", action="store_true",
                        help="bring the index up to date, migrating an older one in place, and exit")
    parser.add_argument("--ingest", metavar="FILE",
                        help="index one transcript kept elsewhere (a backup, another machine) and exit")
    parser.add_argument("--as", dest="as_path", metavar="PATH",
                        help="with --ingest: the path the transcript had, or would have, under "
                             "<claude-dir>/projects/ (it names the project, the session and the agent)")
    parser.add_argument("--host", help="host name to record for what this run indexes (default: this machine's)")
    args = parser.parse_args(argv)

    indexer = HistoryIndexer(db_path=args.db, claude_dir=args.claude_dir, host=args.host)

    if args.ingest or args.once:
        if args.ingest and not args.as_path:
            parser.error("--ingest needs --as PATH")
        indexer.lock()
        indexer.init_db()
        if args.ingest:
            added = indexer.index_jsonl(Path(args.ingest), as_path=Path(args.as_path), force=True)
            log(f"{args.ingest}: {added} records added; existing ones brought up to date")
        else:
            indexer.sync_all()
        indexer.cleanup()
        return

    # Handle signals for graceful shutdown
    def signal_handler(signum, frame):
        log(f"Received signal {signum}, shutting down...")
        indexer.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    indexer.run()


if __name__ == "__main__":
    main()
