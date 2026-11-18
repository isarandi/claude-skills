"""The indexer (daemon.HistoryIndexer): what it stores, incremental reading, the watch
handling, and the in-place migration of a version 1 index."""
import collections
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import util
from util import (PDF, PNG, PROJECT, SESSION, assistant, image_block, queued, tool_result, tool_use, uid,
                  user, write_jsonl)

import daemon

PNG_SHA = hashlib.sha256(PNG).hexdigest()
PLACEHOLDER = f"[image sha256:{PNG_SHA} image/png {len(PNG)} bytes]"


class IndexerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.claude = root / "claude"
        self.project = self.claude / "projects" / PROJECT
        self.project.mkdir(parents=True)
        self.db_path = root / "index" / "history.db"
        self.main = self.project / f"{SESSION}.jsonl"
        self.indexer = None

    def index(self):
        """Run a full sync with a fresh indexer, as after a daemon restart."""
        if self.indexer:
            self.indexer.cleanup()
        self.indexer = daemon.HistoryIndexer(self.db_path, self.claude, host="testhost")
        self.addCleanup(self.close)
        self.indexer.init_db()
        self.indexer.sync_all()
        return self.indexer.db

    def close(self):
        if self.indexer and self.indexer.db:
            self.indexer.db.close()
            self.indexer.db = None

    def rows(self, where="1", args=()):
        return self.indexer.db.execute(
            f"SELECT * FROM records WHERE {where} ORDER BY byte_offset, id", args).fetchall()

    def one(self, where, args=()):
        rows = self.rows(where, args)
        self.assertEqual(len(rows), 1, f"{where}: {[dict(r) for r in rows]}")
        return rows[0]


class Transcripts(IndexerCase):
    def conversation(self):
        body = "ä" * 1200
        return [
            {"type": "ai-title", "aiTitle": "Fix the tests", "sessionId": SESSION},
            user(1, "please fix the test", 0, origin={"kind": "human"}),
            assistant(2, [{"type": "text", "text": "On it."}], 1, parent=1),
            assistant(3, [tool_use("t1", "Write", {"file_path": "/home/user/proj/a.py", "content": body})], 1),
            user(4, [tool_result("t1", [{"type": "text", "text": "written"}, image_block()])], 2),
            queued(5, "also check the logs", 2),
            user(6, "agent says hi", 3, isMeta=True, origin={"kind": "peer"}),
            {"type": "api-request", "sessionId": SESSION, "params": {"x": "y" * 100}},
        ], body

    def test_rows_columns_and_session(self):
        records, body = self.conversation()
        write_jsonl(self.main, records)
        db = self.index()
        types = collections.Counter(r["record_type"] for r in self.rows())
        self.assertEqual(types, {"main_user_text": 2, "main_assistant_text": 1, "main_tool_input": 1,
                                 "main_tool_output": 1, "main_meta_text": 1})
        call = self.one("record_type = 'main_tool_input'")
        self.assertEqual(call["content"], "/home/user/proj/a.py\n" + body)
        self.assertEqual(json.loads(call["input_json"])["content"], body)
        out = self.one("record_type = 'main_tool_output'")
        self.assertEqual((out["content"], out["tool_name"]), ("written\n" + PLACEHOLDER, "Write"))
        blob = db.execute("SELECT media_type, size, data FROM blobs WHERE hash = ?", (PNG_SHA,)).fetchone()
        self.assertEqual(tuple(blob), ("image/png", len(PNG), PNG))
        mid_turn = self.one("content = 'also check the logs'")
        self.assertEqual((mid_turn["record_type"], mid_turn["origin"]), ("main_user_text", "human"))
        typed = self.one("uuid = ?", (uid(1)[:12],))
        self.assertEqual((typed["timestamp"], typed["host"], typed["is_meta"], typed["session_id"]),
                         ("2026-01-15 12:00:00", "testhost", 0, SESSION))
        self.assertEqual(self.one("record_type = 'main_assistant_text'")["model"], "claude-test-1")
        self.assertEqual(self.one("uuid = ?", (uid(2)[:12],))["parent_uuid"], uid(1)[:12])
        session = db.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual((session["session_id"], session["project_name"], session["title"], session["cwd"],
                          session["first_ts"], session["last_ts"]),
                         (SESSION, PROJECT, "Fix the tests", "/home/user/proj",
                          "2026-01-15 12:00:00", "2026-01-15 12:03:00"))

    def test_messages_without_uuid_are_kept_apart(self):
        first, second = user(1, "first without uuid", 0), user(2, "second without uuid", 1)
        del first["uuid"], second["uuid"]
        write_jsonl(self.main, [first, second])
        self.index()
        rows = self.rows()
        self.assertEqual([r["content"] for r in rows], ["first without uuid", "second without uuid"])
        self.assertTrue(all(r["uuid"].startswith("~") and len(r["uuid"]) == 12 for r in rows))
        self.indexer.index_jsonl(self.main, force=True)
        self.assertEqual(len(self.rows()), 2)

    def test_only_new_lines_are_read(self):
        records, _ = self.conversation()
        write_jsonl(self.main, records)
        self.index()
        size = self.main.stat().st_size
        info = self.indexer.get_indexed_info(self.main)
        self.assertEqual((info["byte_offset"], info["index_version"]), (size, daemon.INDEX_VERSION))

        # A line still being written is left for the next pass
        line = json.dumps(user(7, "one more thing", 5, origin={"kind": "human"})) + "\n"
        with open(self.main, "a") as f:
            f.write(line[:40])
        self.assertEqual(self.indexer.index_jsonl(self.main), 0)
        self.assertEqual(self.indexer.get_indexed_info(self.main)["byte_offset"], size)
        with open(self.main, "a") as f:
            f.write(line[40:])
        parsed = []
        original = self.indexer.index_record
        self.indexer.index_record = lambda rec, *a: parsed.append(rec.get("uuid")) or original(rec, *a)
        self.assertEqual(self.indexer.index_jsonl(self.main), 1)
        self.assertEqual(parsed, [uid(7)])  # nothing before the stored offset was parsed again
        self.assertEqual(self.one("content = 'one more thing'")["byte_offset"], size)
        # The tool result of a call from an earlier pass still gets the tool's name
        write_jsonl(self.main, [assistant(8, [tool_use("t9", "Bash", {"command": "ls"})], 6)], mode="a")
        self.indexer.index_jsonl(self.main)
        write_jsonl(self.main, [user(9, [tool_result("t9", "a.py")], 6)], mode="a")
        self.indexer.index_jsonl(self.main)
        self.assertEqual(self.one("record_type = 'main_tool_output' AND tool_use_id = 't9'")["tool_name"], "Bash")

    def test_rewritten_file_is_read_again_without_duplicates(self):
        records, _ = self.conversation()
        write_jsonl(self.main, records)
        self.index()
        before = len(self.rows())
        # Claude Code rewrote the transcript: other bytes before the stored offset
        write_jsonl(self.main, [{"type": "mode", "mode": "x" * 50, "sessionId": SESSION}] + records
                    + [user(7, "after the rewrite", 5, origin={"kind": "human"})])
        self.assertEqual(self.indexer.index_jsonl(self.main), 1)
        self.assertEqual(len(self.rows()), before + 1)
        offsets = [r["byte_offset"] for r in self.rows("uuid = ?", (uid(1)[:12],))]
        self.assertGreater(offsets[0], 0)  # the row's place in the file was brought up to date

    def test_subagent_transcript_and_meta(self):
        write_jsonl(self.main, [user(1, "start", 0, origin={"kind": "human"})])
        agent_file = self.project / SESSION / "subagents" / "agent-a1b2c3.jsonl"
        write_jsonl(agent_file, [
            user(20, "do the task", 1, agent="a1b2c3"),
            user(21, "<system-reminder>careful</system-reminder>", 1, agent="a1b2c3", isMeta=True),
            queued(22, "keep going", 2, origin="coordinator", agent="a1b2c3", isMeta=True),
            assistant(23, [tool_use("t5", "Bash", {"command": "ls"})], 2, agent="a1b2c3"),
            user(24, "Summary of the agent's work so far", 3, agent="a1b2c3", isCompactSummary=True),
        ])
        meta = agent_file.with_name("agent-a1b2c3.meta.json")
        meta.write_text(json.dumps({"agentType": "general-purpose", "description": "Count the widgets"}))
        self.index()
        types = collections.Counter(r["record_type"] for r in self.rows("agent_id = 'a1b2c3'"))
        self.assertEqual(types, {"agent_input_text": 2, "agent_meta_text": 1, "agent_tool_input": 1,
                                 "agent_summary": 1, "subagent_meta": 1})
        for row in self.rows("agent_id = 'a1b2c3'"):
            self.assertEqual((row["session_id"], row["project_name"]), (SESSION, PROJECT))
        self.assertEqual(self.one("content = 'keep going'")["origin"], "coordinator")
        meta_row = self.one("record_type = 'subagent_meta'")
        self.assertEqual(meta_row["tool_name"], "general-purpose")
        self.assertIn("Count the widgets", meta_row["content"])
        # The meta file changes when the agent finishes: both versions stay
        meta.write_text(json.dumps({"agentType": "general-purpose", "description": "Count the widgets",
                                    "finished": True}))
        self.index()
        self.assertEqual(len(self.rows("record_type = 'subagent_meta'")), 2)


class SideFiles(IndexerCase):
    def setUp(self):
        super().setUp()
        write_jsonl(self.main, [user(1, "start", 0, origin={"kind": "human"})])
        self.tool_results = self.project / SESSION / "tool-results"
        self.tool_results.mkdir(parents=True)

    def test_persisted_output_is_stored_in_the_tool_output_row(self):
        saved = self.tool_results / "b1234abcd.txt"
        saved.write_text("line\n" * 5000 + "needle at the end\n")
        util.old(saved)
        write_jsonl(self.main, [
            assistant(2, [tool_use("t1", "Bash", {"command": "seq 5000"})], 1),
            user(3, [tool_result("t1", "<persisted-output>\nOutput too large (25 KB). Full output saved to: "
                                 f"{saved}\n\nPreview (first 2KB):\nline\nline\n</persisted-output>")], 1,
                 toolUseResult={"stdout": "line", "persistedOutputPath": str(saved), "persistedOutputSize": 25018}),
        ], mode="a")
        self.index()
        out = self.one("record_type = 'main_tool_output'")
        self.assertEqual(out["content"], saved.read_text())
        self.assertEqual(json.loads(out["result_json"])["persistedOutputPath"], str(saved))
        self.assertEqual(self.rows("record_type = 'tool_file'"), [])  # not stored a second time
        full = out["content"]
        saved.unlink()  # Claude Code's cleanup takes the file
        self.index()
        self.assertEqual(self.one("record_type = 'main_tool_output'")["content"], full)
        # The transcript is parsed again from the start (here: it was rewritten). It has
        # only the preview; the stored output must stay.
        records = util.read_jsonl(self.main)
        write_jsonl(self.main, [{"type": "mode", "mode": "x" * 50, "sessionId": SESSION}] + records)
        self.index()
        out = self.one("record_type = 'main_tool_output'")
        self.assertEqual(out["content"], full)
        self.assertGreater(out["byte_offset"], 0)
        # The same when the transcript is brought in again from a backup
        self.indexer.index_jsonl(self.main, force=True)
        self.assertEqual(self.one("record_type = 'main_tool_output'")["content"], full)

    def test_output_with_bytes_that_are_not_utf8_is_still_searchable(self):
        saved = self.tool_results / "b5678efgh.txt"
        raw = b"compiling\n" * 300 + b"caf\xe9 au lait\n" + b"done\n"
        saved.write_bytes(raw)
        util.old(saved)
        write_jsonl(self.main, [
            assistant(2, [tool_use("t1", "Bash", {"command": "make"})], 1),
            user(3, [tool_result("t1", "<persisted-output>\nPreview: compiling")], 1,
                 toolUseResult={"stdout": "compiling", "persistedOutputPath": str(saved),
                                "persistedOutputSize": len(raw)}),
        ], mode="a")
        db = self.index()
        out = self.one("record_type = 'main_tool_output'")
        digest = hashlib.sha256(raw).hexdigest()
        self.assertIn("caf\ufffd au lait\ndone\n", out["content"])
        self.assertTrue(out["content"].endswith(f"[file sha256:{digest} text/plain {len(raw)} bytes]]"))
        self.assertEqual(db.execute("SELECT data FROM blobs WHERE hash = ?", (digest,)).fetchone()[0], raw)
        self.assertEqual(self.rows("record_type = 'tool_file'"), [])

    def test_other_saved_files(self):
        page = self.tool_results / "artifact-1a2b3c4d-1790000000000-ab12.html"
        page.write_text("<html><body>Quarterly report</body></html>")
        pdf = self.tool_results / "webfetch-1790000000001-x1y2z.pdf"
        pdf.write_bytes(PDF)
        fresh = self.tool_results / "still-being-written.txt"
        fresh.write_text("partial")
        for path in (page, pdf):
            util.old(path)
        db = self.index()
        html_row = self.one("tool_name = ?", (page.name,))
        self.assertEqual((html_row["record_type"], html_row["session_id"]), ("tool_file", SESSION))
        self.assertEqual(html_row["content"],
                         f"{page.name} (text/html, {page.stat().st_size} bytes)\n{page.read_text()}")
        pdf_row = self.one("tool_name = ?", (pdf.name,))
        digest = hashlib.sha256(PDF).hexdigest()
        self.assertEqual(pdf_row["content"], f"{pdf.name} (application/pdf, {len(PDF)} bytes)\n"
                                             f"[file sha256:{digest} application/pdf {len(PDF)} bytes]")
        self.assertEqual(db.execute("SELECT data FROM blobs WHERE hash = ?", (digest,)).fetchone()[0], PDF)
        self.assertEqual(self.rows("tool_name = ?", (fresh.name,)), [])  # not settled yet
        # A changed file updates its row instead of adding one
        page.write_text("<html><body>Annual report</body></html>")
        util.old(page, 1800)
        self.index()
        self.assertIn("Annual report", self.one("tool_name = ?", (page.name,))["content"])

    def test_memory_notes_keep_their_versions(self):
        note = self.project / "memory" / "topics" / "style.md"
        note.parent.mkdir(parents=True)
        note.write_text("Write short sentences.\n")
        util.old(note, 7200)
        self.index()
        note.write_text("Write short sentences. Prefer verbs.\n")
        util.old(note, 3600)
        self.index()
        self.index()  # unchanged: nothing new
        versions = self.rows("record_type = 'memory'")
        self.assertEqual([v["content"] for v in sorted(versions, key=lambda v: v["timestamp"])],
                         ["Write short sentences.\n", "Write short sentences. Prefer verbs.\n"])
        self.assertEqual({(v["tool_name"], v["project_name"]) for v in versions}, {("topics/style.md", PROJECT)})

    def test_prompt_history_with_pasted_text(self):
        (self.claude / "paste-cache").mkdir()
        (self.claude / "paste-cache" / "0123456789abcdef.txt").write_text("pasted from the cache")
        entries = [
            {"display": "fix it [Pasted text #1 +3 lines]", "timestamp": 1768478400000, "project": "/home/user/proj",
             "sessionId": SESSION, "pastedContents": {"1": {"id": 1, "type": "text", "content": "pasted inline"}}},
            {"display": "and this [Pasted text #2]", "timestamp": 1768478460000, "project": "/home/user/proj",
             "sessionId": SESSION,
             "pastedContents": {"2": {"id": 2, "type": "text", "contentHash": "0123456789abcdef"}}},
        ]
        write_jsonl(self.claude / "history.jsonl", entries)
        self.index()
        first, second = sorted(self.rows("record_type = 'prompt_history'"), key=lambda r: r["timestamp"])
        self.assertEqual(first["content"], "fix it [Pasted text #1 +3 lines]\n[Pasted text #1]\npasted inline")
        self.assertTrue(second["content"].endswith("pasted from the cache"))
        self.assertEqual((first["timestamp"], first["project_name"], first["session_id"]),
                         ("2026-01-15 12:00:00", PROJECT, SESSION))
        self.assertEqual(self.one("record_type = 'paste'")["content"], "pasted from the cache")
        write_jsonl(self.claude / "history.jsonl", [dict(entries[0], display="third", timestamp=1768478520000)],
                    mode="a")
        self.index()
        self.assertEqual(len(self.rows("record_type = 'prompt_history'")), 3)


class Snapshots(IndexerCase):
    OTHER = "99999999-2222-3333-4444-555555555555"

    def setUp(self):
        super().setUp()
        self.history = self.claude / "file-history"
        (self.history / SESSION).mkdir(parents=True)
        (self.history / SESSION / "aaaabbbbccccdddd@v1").write_text("first version\n")
        backup = {"backupFileName": "aaaabbbbccccdddd@v1", "version": 1, "backupTime": util.ts(1)}
        write_jsonl(self.main, [
            user(1, "start", 0, origin={"kind": "human"}),
            {"type": "file-history-snapshot", "messageId": uid(1), "isSnapshotUpdate": False,
             "snapshot": {"messageId": uid(1), "timestamp": util.ts(1), "trackedFileBackups": {
                 "/home/user/proj/a.py": backup, "/home/user/proj/new.py": {"backupFileName": None, "version": 1}}}},
            {"type": "file-history-delta", "messageId": uid(1), "snapshotMessageId": uid(1),
             "trackingPath": "/home/user/proj/b.py", "timestamp": util.ts(2),
             "backup": {"backupFileName": "eeeeffff00001111@v2", "version": 2, "backupTime": util.ts(2)}},
        ])

    def snapshots(self):
        return {(r["session_id"], r["backup_id"]): r for r in
                self.indexer.db.execute("SELECT * FROM snapshots").fetchall()}

    def test_keyed_by_session_with_the_file_path_from_the_transcript(self):
        # Another session edited the same file: same backup name, other content
        (self.history / self.OTHER).mkdir()
        (self.history / self.OTHER / "aaaabbbbccccdddd@v1").write_text("the other session's version\n")
        (self.history / self.OTHER / "2222333344445555@v1").write_bytes(b"\xff\xfe binary \x00 data")
        self.index()
        snaps = self.snapshots()
        mine = snaps[(SESSION, "aaaabbbbccccdddd@v1")]
        self.assertEqual((mine["content"], mine["file_path"], mine["version"], mine["message_id"]),
                         ("first version\n", "/home/user/proj/a.py", 1, uid(1)))
        self.assertEqual(snaps[(self.OTHER, "aaaabbbbccccdddd@v1")]["content"], "the other session's version\n")
        # Named only in a delta record, and its file is already gone: known, without content
        known = snaps[(SESSION, "eeeeffff00001111@v2")]
        self.assertEqual((known["file_path"], known["content"], known["timestamp"]),
                         ("/home/user/proj/b.py", None, "2026-01-15 12:02:00"))
        binary = snaps[(self.OTHER, "2222333344445555@v1")]
        self.assertIsNone(binary["content"])
        data = self.indexer.db.execute("SELECT data FROM blobs WHERE hash = ?", (binary["blob_hash"],)).fetchone()[0]
        self.assertEqual(data, b"\xff\xfe binary \x00 data")
        # The file of the known snapshot appears later: its content is filled in
        (self.history / SESSION / "eeeeffff00001111@v2").write_text("second version\n")
        self.index()
        self.assertEqual(self.snapshots()[(SESSION, "eeeeffff00001111@v2")]["content"], "second version\n")

    def test_new_session_folder_is_watched_and_indexed(self):
        self.index()

        class FakeInotify:
            def __init__(self):
                self.watched = []

            def add_watch(self, path, mask):
                self.watched.append(Path(path))
                return len(self.watched)

            def close(self):
                pass

        indexer = self.indexer
        indexer.inotify = FakeInotify()
        indexer.watch(self.history, daemon.IN_CREATE | daemon.IN_MOVED_TO)
        new_dir = self.history / self.OTHER
        new_dir.mkdir()
        (new_dir / "6666777788889999@v1").write_text("was there before the watch\n")
        Event = collections.namedtuple("Event", "wd mask name")
        indexer.handle_event(Event(1, daemon.IN_CREATE | 0x40000000, self.OTHER))
        self.assertIn(new_dir, indexer.inotify.watched)
        self.assertIn((self.OTHER, "6666777788889999@v1"), self.snapshots())
        (new_dir / "6666777788889999@v2").write_text("written after the watch\n")
        indexer.handle_event(Event(indexer.inotify.watched.index(new_dir) + 1, daemon.IN_CREATE,
                                   "6666777788889999@v2"))
        self.assertEqual(self.snapshots()[(self.OTHER, "6666777788889999@v2")]["content"],
                         "written after the watch\n")
        indexer.inotify = None


V1_SCHEMA = """
CREATE TABLE records (id INTEGER PRIMARY KEY, uuid TEXT, timestamp TEXT, record_type TEXT, tool_name TEXT,
    tool_use_id TEXT, content TEXT, project_name TEXT, session_id TEXT, agent_id TEXT, source_file TEXT,
    byte_offset INTEGER);
CREATE INDEX idx_timestamp ON records(timestamp);
CREATE INDEX idx_type ON records(record_type);
CREATE INDEX idx_tool ON records(tool_name);
CREATE INDEX idx_project ON records(project_name);
CREATE INDEX idx_session ON records(session_id);
CREATE INDEX idx_source ON records(source_file);
CREATE UNIQUE INDEX idx_unique_record ON records(uuid, record_type, COALESCE(tool_use_id, ''));
CREATE TABLE snapshots (id INTEGER PRIMARY KEY, backup_id TEXT UNIQUE, session_id TEXT, file_path TEXT,
    timestamp TEXT, size INTEGER, content TEXT);
CREATE INDEX idx_snap_session ON snapshots(session_id);
CREATE INDEX idx_snap_file ON snapshots(file_path);
CREATE INDEX idx_snap_time ON snapshots(timestamp);
CREATE TABLE indexed_files (file_path TEXT PRIMARY KEY, byte_offset INTEGER, file_mtime REAL);
"""
V1_FTS = """
CREATE VIRTUAL TABLE records_fts USING fts5(content, content='records', content_rowid='id');
CREATE VIRTUAL TABLE snapshots_fts USING fts5(content, content='snapshots', content_rowid='id');
CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER snapshots_ai AFTER INSERT ON snapshots BEGIN
    INSERT INTO snapshots_fts(rowid, content) VALUES (new.id, new.content); END;
"""


def v1_tool_input(name, tool_input):
    """What index version 1 stored for a tool call."""
    if name == "Write":
        return f"{tool_input.get('file_path', '')}\n{tool_input.get('content', '')[:500]}"
    return json.dumps(tool_input)[:500]


class Migration(IndexerCase):
    GONE = "77777777-2222-3333-4444-555555555555"
    AGENT = "a1b2c3"

    def build_v1(self):
        """A version 1 index of one session on disk and one whose transcript is gone."""
        body = "ö" * 1500
        prompt = "Bitte prüfe das. " * 60
        main = [
            user(1, "please fix the test", 0, origin={"kind": "human"}),
            assistant(2, [tool_use("t1", "Write", {"file_path": "/p/a.py", "content": body})], 1),
            user(3, [tool_result("t1", [{"type": "text", "text": "written"}, image_block()])], 1),
            assistant(4, [tool_use("t2", "Agent", {"description": "check", "prompt": prompt})], 2),
            user(5, "agent says hi", 3, isMeta=True, origin={"kind": "peer"}),
            queued(6, "also check the logs", 3),
            {"type": "file-history-snapshot", "messageId": uid(1), "isSnapshotUpdate": False,
             "snapshot": {"trackedFileBackups": {"/p/a.py": {"backupFileName": "aaaabbbbccccdddd@v1",
                                                             "version": 1, "backupTime": util.ts(1)}}}},
        ]
        agent = [user(20, "do the task", 4, agent=self.AGENT),
                 user(21, "Summary of the agent's work", 5, agent=self.AGENT, isCompactSummary=True)]
        agent_file = self.project / SESSION / "subagents" / f"agent-{self.AGENT}.jsonl"
        write_jsonl(self.main, main)
        write_jsonl(agent_file, agent)
        history = self.claude / "file-history"
        for session, text in ((SESSION, "mine\n"), (self.GONE, "from the session that is gone\n"),
                              ("88888888-2222-3333-4444-555555555555", "same name, third session\n")):
            (history / session).mkdir(parents=True)
            (history / session / "aaaabbbbccccdddd@v1").write_text(text)
        (history / self.GONE / "aaaabbbbccccdddd@v1").unlink()  # only the index has it

        self.db_path.parent.mkdir(parents=True)
        db = sqlite3.connect(self.db_path)
        db.executescript(V1_SCHEMA)
        try:
            db.executescript(V1_FTS)
        except sqlite3.OperationalError:
            pass  # this SQLite has no FTS5

        def put(n, rtype, content, source, session=SESSION, tool=None, tool_id="", agent=None, offset=0):
            db.execute("INSERT INTO records (uuid, timestamp, record_type, tool_name, tool_use_id, content, "
                       "project_name, session_id, agent_id, source_file, byte_offset) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (uid(n)[:12], f"2026-01-15 12:{n % 60:02d}:00", rtype, tool, tool_id, content, PROJECT,
                        session, agent, str(source), offset))

        image_text = str(image_block())  # version 1 stored the block as Python text
        put(1, "main_user_text", "please fix the test", self.main)
        put(2, "main_tool_input", v1_tool_input("Write", {"file_path": "/p/a.py", "content": body}), self.main,
            tool="Write", tool_id="t1")
        put(3, "main_tool_output", "written\n" + image_text, self.main, tool="Write", tool_id="t1")
        put(4, "main_tool_input", v1_tool_input("Agent", {"description": "check", "prompt": prompt}), self.main,
            tool="Agent", tool_id="t2")
        put(5, "main_user_text", "agent says hi", self.main)
        put(20, "agent_input_text", "do the task", agent_file, agent=self.AGENT)
        put(21, "main_summary", "Summary of the agent's work", agent_file, agent=self.AGENT)
        gone = self.claude / "projects" / "-home-user-old" / f"{self.GONE}.jsonl"
        put(40, "main_user_text", "what the user asked in the lost session", gone, session=self.GONE)
        put(41, "main_user_text", "<task-notification>\n<task-id>b1</task-id>", gone, session=self.GONE)
        put(42, "main_tool_output", "screenshot taken\n" + image_text + "\nend", gone, session=self.GONE,
            tool="Read", tool_id="t40")
        put(43, "main_tool_input", "/p/lost.py\n" + "x" * 500, gone, session=self.GONE, tool="Write", tool_id="t41")
        db.execute("INSERT INTO snapshots (backup_id, session_id, timestamp, size, content) VALUES (?,?,?,?,?)",
                   ("aaaabbbbccccdddd@v1", self.GONE, "2026-01-10 08:00:00", 30, "from the session that is gone\n"))
        for path in (self.main, agent_file):
            db.execute("INSERT INTO indexed_files VALUES (?, 0, ?)", (str(path), path.stat().st_mtime))
        db.execute("PRAGMA user_version = 1")
        db.commit()
        before = {r[0]: r[1:] for r in db.execute("SELECT id, uuid, record_type, content FROM records")}
        db.close()
        return before, body, prompt

    def test_migration_in_place(self):
        before, body, prompt = self.build_v1()
        db = self.index()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        after = {r["id"]: r for r in self.rows()}
        # No row is lost and none changes its identity
        self.assertLessEqual(set(before), set(after))
        for rid, (uuid, _rtype, _content) in before.items():
            self.assertEqual(after[rid]["uuid"], uuid)

        # Rows of the transcript on disk were brought up to date in place
        self.assertEqual(self.one("uuid = ? AND record_type = 'main_tool_input'", (uid(2)[:12],))["content"],
                         "/p/a.py\n" + body)
        agent_call = self.one("uuid = ?", (uid(4)[:12],))
        self.assertEqual(agent_call["content"], f"description: check\nprompt: {prompt}")
        self.assertEqual(json.loads(agent_call["input_json"])["prompt"], prompt)
        self.assertEqual(self.one("uuid = ?", (uid(3)[:12],))["content"], "written\n" + PLACEHOLDER)
        meta = self.one("uuid = ?", (uid(5)[:12],))
        self.assertEqual((meta["record_type"], meta["origin"], meta["is_meta"]), ("main_meta_text", "agent", 1))
        self.assertEqual(self.one("uuid = ?", (uid(21)[:12],))["record_type"], "agent_summary")
        # What version 1 skipped is there now
        self.assertEqual(self.one("content = 'also check the logs'")["record_type"], "main_user_text")

        # Rows whose transcript is gone are kept; their images moved out of the text
        lost = self.one("uuid = ?", (uid(40)[:12],))
        self.assertEqual((lost["content"], lost["record_type"], lost["is_meta"]),
                         ("what the user asked in the lost session", "main_user_text", None))
        note = self.one("uuid = ?", (uid(41)[:12],))
        self.assertEqual((note["record_type"], note["origin"]), ("main_meta_text", "task-notification"))
        self.assertEqual(self.one("uuid = ?", (uid(42)[:12],))["content"],
                         f"screenshot taken\n{PLACEHOLDER}\nend")
        self.assertEqual(self.one("uuid = ?", (uid(43)[:12],))["content"], "/p/lost.py\n" + "x" * 500)
        self.assertEqual(db.execute("SELECT data FROM blobs WHERE hash = ?", (PNG_SHA,)).fetchone()[0], PNG)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE content LIKE '%base64%'").fetchone()[0], 0)
        sessions = {r["session_id"]: r for r in db.execute("SELECT * FROM sessions")}
        self.assertEqual(sessions[self.GONE]["project_name"], PROJECT)
        self.assertEqual(sessions[self.GONE]["first_ts"], "2026-01-15 12:40:00")

        # Snapshots: the old row is kept, the same name is now stored per session
        snaps = {(r["session_id"], r["backup_id"]): r for r in db.execute("SELECT * FROM snapshots")}
        self.assertEqual(snaps[(self.GONE, "aaaabbbbccccdddd@v1")]["content"], "from the session that is gone\n")
        self.assertEqual((snaps[(SESSION, "aaaabbbbccccdddd@v1")]["content"],
                          snaps[(SESSION, "aaaabbbbccccdddd@v1")]["file_path"]), ("mine\n", "/p/a.py"))
        self.assertEqual(len(snaps), 3)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertFalse(tables & {"records_fts", "snapshots_fts", "records_ai", "idx_session", "idx_source"})

        # Running it again changes nothing
        snapshot = [tuple(r) for r in self.rows()]
        self.index()
        self.assertEqual([tuple(r) for r in self.rows()], snapshot)

    def test_rows_of_a_record_that_several_files_hold(self):
        """A continued session repeats the records of the one it continues. The index keeps
        each once, under the file it saw first."""
        self.build_v1()
        body = "ö" * 1500
        # The row belongs to a transcript that is gone, and another file has the record
        db = sqlite3.connect(self.db_path)
        gone = self.project / "66666666-2222-3333-4444-555555555555.jsonl"
        db.execute("UPDATE records SET source_file = ?, byte_offset = 7 WHERE uuid = ?", (str(gone), uid(2)[:12]))
        # An agent's summary with the uuid of a summary in the main transcript
        db.execute("DELETE FROM records WHERE uuid = ? AND agent_id IS NOT NULL", (uid(21)[:12],))
        db.execute("INSERT INTO records (uuid, timestamp, record_type, content, project_name, session_id, "
                   "source_file, byte_offset) VALUES (?, '2026-01-15 12:05:00', 'main_summary', 'main one', ?, ?, ?, 0)",
                   (uid(21)[:12], PROJECT, SESSION, str(gone)))
        db.commit()
        db.close()
        self.index()
        row = self.one("uuid = ? AND record_type = 'main_tool_input'", (uid(2)[:12],))
        self.assertEqual(row["content"], "/p/a.py\n" + body)  # brought up to date from the other file
        self.assertEqual((row["source_file"], row["byte_offset"]), (str(gone), 7))  # and left in its place
        # The main transcript's summary keeps its type; the agent's gets a row of its own
        self.assertEqual(self.one("uuid = ? AND agent_id IS NULL", (uid(21)[:12],))["record_type"], "main_summary")
        self.assertEqual(self.one("uuid = ? AND agent_id IS NOT NULL", (uid(21)[:12],))["record_type"],
                         "agent_summary")

    def test_snapshots_of_version_1_get_their_exact_content(self):
        self.build_v1()
        history = self.claude / "file-history"
        (history / SESSION / "1111222233334444@v1").write_bytes(b"one\r\ntwo\r\n")
        (history / SESSION / "5555666677778888@v1").write_bytes(b"on disk it is different now\n")
        db = sqlite3.connect(self.db_path)
        for backup_id, content in (("1111222233334444@v1", "one\ntwo\n"),      # as version 1 read it
                                   ("5555666677778888@v1", "what version 1 stored\n"),
                                   ("9999aaaabbbbcccc@v1", "größer\n")):         # its file is gone
            db.execute("INSERT INTO snapshots (backup_id, session_id, timestamp, size, content) "
                       "VALUES (?, ?, '2026-01-10 08:00:00', ?, ?)", (backup_id, SESSION, len(content), content))
        db.commit()
        db.close()
        db = self.index()
        snaps = {r["backup_id"]: r for r in db.execute("SELECT * FROM snapshots WHERE session_id = ?", (SESSION,))}
        self.assertEqual((snaps["1111222233334444@v1"]["content"], snaps["1111222233334444@v1"]["size"]),
                         ("one\r\ntwo\r\n", 10))
        self.assertEqual(snaps["5555666677778888@v1"]["content"], "what version 1 stored\n")  # not overwritten
        self.assertEqual((snaps["9999aaaabbbbcccc@v1"]["content"], snaps["9999aaaabbbbcccc@v1"]["size"]),
                         ("größer\n", 9))  # bytes now, not characters
        self.assertEqual({r["host"] for r in snaps.values()}, {"testhost"})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE host IS NULL").fetchone()[0], 0)

    def test_interrupted_migration_is_resumed(self):
        before, body, _prompt = self.build_v1()
        indexer = daemon.HistoryIndexer(self.db_path, self.claude, host="testhost")
        original = indexer.sync_side_files
        indexer.sync_side_files = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            indexer.init_db()
        self.assertEqual(indexer.db.execute("PRAGMA user_version").fetchone()[0], 1)
        indexer.db.close()
        del original
        db = self.index()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertLessEqual(set(before), {r["id"] for r in self.rows()})
        self.assertEqual(self.one("uuid = ? AND record_type = 'main_tool_input'", (uid(2)[:12],))["content"],
                         "/p/a.py\n" + body)

    def test_ingest_transcript_from_elsewhere(self):
        self.build_v1()
        self.index()
        # The transcript of the lost session turns up in a backup
        backup = Path(self.tmp.name) / "restored.jsonl"
        write_jsonl(backup, [
            user(40, "what the user asked in the lost session", 40, session=self.GONE, origin={"kind": "human"}),
            assistant(43, [tool_use("t41", "Write", {"file_path": "/p/lost.py", "content": "x" * 900})], 43,
                      session=self.GONE)])
        as_path = self.claude / "projects" / "-home-user-old" / f"{self.GONE}.jsonl"
        self.indexer.index_jsonl(backup, as_path=as_path, force=True)
        self.assertEqual(self.one("uuid = ? AND record_type = 'main_tool_input'", (uid(43)[:12],))["content"],
                         "/p/lost.py\n" + "x" * 900)
        self.assertEqual(self.one("uuid = ?", (uid(40)[:12],))["is_meta"], 0)


if __name__ == "__main__":
    unittest.main()
