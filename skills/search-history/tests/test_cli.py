"""The command line (search_history.py), run as a Claude session runs it: against the index,
also after Claude Code has deleted the transcripts, and without an index."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest

import util
from util import (PNG, SESSION, assistant, image_block, queued, tool_result, tool_use, uid, user,
                  write_jsonl)

from test_indexer import IndexerCase

OTHER = "99999999-2222-3333-4444-555555555555"
AGENT = "a1b2c3d4e5f6a7b8c"
BODY = "line of the written file äöü\n" * 200  # 5,800 characters
CRLF = "first line\r\nsecond line\r\n"
PATCH = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-old text", "+new text"]}]


class CliCase(IndexerCase):
    def setUp(self):
        super().setUp()
        write_jsonl(self.main, [
            {"type": "ai-title", "aiTitle": "Fix the tests", "sessionId": SESSION},
            user(1, "please fix the flaky test", 0, origin={"kind": "human"}),
            assistant(2, [{"type": "text", "text": "On it."}], 1, parent=1),
            assistant(3, [tool_use("t1", "Write", {"file_path": "/home/user/proj/a.py", "content": BODY})], 1),
            user(4, [tool_result("t1", [{"type": "text", "text": "written"}, image_block()])], 2),
            queued(5, "also check the logs", 2),
            user(6, "report from the agent\r\nsecond line", 3, isMeta=True, origin={"kind": "peer"}),
            assistant(7, [tool_use("t2", "Edit", {"file_path": "/home/user/proj/a.py", "old_string": "old text",
                                                  "new_string": "new text", "replace_all": False})], 4),
            user(8, [tool_result("t2", "The file /home/user/proj/a.py has been updated.")], 4,
                 toolUseResult={"filePath": "/home/user/proj/a.py", "oldString": "old text",
                                "newString": "new text", "originalFile": "old text\n",
                                "structuredPatch": PATCH, "userModified": False, "replaceAll": False}),
            {"type": "file-history-snapshot", "messageId": uid(1), "isSnapshotUpdate": False,
             "snapshot": {"messageId": uid(1), "timestamp": util.ts(1), "trackedFileBackups": {
                 "/home/user/proj/a.py": {"backupFileName": "aaaabbbbccccdddd@v1", "version": 1,
                                          "backupTime": util.ts(1)}}}},
        ])
        write_jsonl(self.project / SESSION / "subagents" / f"agent-{AGENT}.jsonl", [
            user(20, "count the widgets", 5, agent=AGENT),
            assistant(21, [{"type": "text", "text": "There are seven widgets."}], 6, agent=AGENT),
        ])
        write_jsonl(self.project / f"{OTHER}.jsonl", [
            user(30, "an unrelated question about flaky things", 10, session=OTHER, origin={"kind": "human"})])
        history = self.claude / "file-history" / SESSION
        history.mkdir(parents=True)
        (history / "aaaabbbbccccdddd@v1").write_bytes(CRLF.encode())
        self.index()
        self.close()

    def forget_files(self):
        """What Claude Code's cleanup does in the end: the transcripts and backups are gone."""
        shutil.rmtree(self.claude / "projects")
        shutil.rmtree(self.claude / "file-history")

    def cli(self, *args, tz="UTC", session=None, db=None, status=0):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.claude), CLAUDE_HISTORY_DB=str(db or self.db_path), TZ=tz)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        proc = subprocess.run([sys.executable, "-B", str(util.SKILL_DIR / "search_history.py"), *args],
                              capture_output=True, env=env, cwd=self.tmp.name)
        self.assertEqual(proc.returncode, status, proc.stderr.decode())
        return proc.stdout

    def text(self, *args, **kwargs):
        return self.cli(*args, **kwargs).decode()


class FromTheIndexAlone(CliCase):
    """Everything below works after the files are gone."""

    def setUp(self):
        super().setUp()
        self.forget_files()

    def test_search_by_session_and_origin(self):
        out = self.text("-c", "--session", SESSION[:8])
        self.assertIn(f"[ {SESSION[:8]}:{uid(1)[:12]} | 2026-01-15 12:00:00 | main_user_text ", out)
        self.assertIn("main_meta_text:agent      ] report from the agent second line\n", out)  # one line per item
        self.assertNotIn("agent_input_text", out)  # agents only with -t or --agent
        self.assertNotIn("unrelated", out)
        typed = self.text("-c", "--session", SESSION[:8], "-t", "main_user_text")
        self.assertEqual(len(typed.splitlines()), 2)
        self.assertIn("also check the logs", typed)  # delivered mid-turn, found as user text
        self.assertIn("also check the logs", self.text("-c", "--all-projects", "--origin", "human", "logs"))
        self.assertEqual(self.text("--all-projects", "--origin", "coordinator"), "No results found\n")

    def test_scopes(self):
        everything = self.text("-c", "--all-projects", "flaky")
        self.assertEqual(len(everything.splitlines()), 2)
        project = self.text("-c", "--project", "/home/user/proj", "flaky")
        self.assertEqual(project, everything)
        self.assertEqual(len(self.text("-c", "--session", OTHER[:8], "flaky").splitlines()), 1)
        agents = self.text("-c", "--all-projects", "-t", "agent_*", "widgets")
        self.assertIn(f"[ {SESSION[:8]}:{AGENT}:{uid(21)[:12]} ", agents)
        self.assertEqual(len(self.text("-c", "--all-projects", "--agent", AGENT[:5]).splitlines()), 2)

    def test_listing_is_cut_and_full_is_not(self):
        cut = self.text("--session", SESSION[:8], "-t", "main_tool_input", "--tool", "Write")
        self.assertIn(f"[... 2000 of {len('/home/user/proj/a.py') + 1 + len(BODY)} characters shown; "
                      "--full shows all]", cut)
        self.assertNotIn(BODY, cut)
        self.assertIn(BODY, self.text("--session", SESSION[:8], "-t", "main_tool_input", "--tool", "Write",
                                      "--full"))

    def test_expand(self):
        out = self.text("--expand", f"{SESSION[:8]}:{uid(3)[:12]}")
        self.assertIn(f"== [ {SESSION[:8]}:{uid(3)[:12]} | 2026-01-15 12:01:00 | assistant ] ==", out)
        self.assertIn("--- Tool: Write ---\nInput:\n/home/user/proj/a.py\n" + BODY, out)
        digest = hashlib.sha256(PNG).hexdigest()
        self.assertIn(f"Output:\nwritten\n[image sha256:{digest} image/png {len(PNG)} bytes]", out)
        # The uuid alone is enough, and so is the agent's handle
        self.assertEqual(self.text("--expand", uid(3)[:8]), out)
        self.assertIn("There are seven widgets.",
                      self.text("--expand", f"{SESSION[:8]}:{AGENT}:{uid(21)[:12]}"))
        self.assertIn("There are seven widgets.", self.text("--expand", f"{AGENT}:{uid(21)[:12]}"))
        self.assertIn("No record found", self.text("--expand", "ffffffff"))

    def test_json_gives_what_replaying_an_edit_needs(self):
        (call,) = [json.loads(line) for line in self.text(
            "--session", SESSION[:8], "-t", "main_tool_input", "--tool", "Edit", "--json").splitlines()]
        self.assertEqual(call["input"], {"file_path": "/home/user/proj/a.py", "old_string": "old text",
                                         "new_string": "new text", "replace_all": False})
        call, result = [json.loads(line) for line in self.text(
            "--expand", f"{SESSION[:8]}:{uid(8)[:12]}", "--json").splitlines()]
        self.assertEqual((call["type"], result["type"]), ("main_tool_input", "main_tool_output"))
        self.assertEqual(result["result"]["originalFile"], "old text\n")
        self.assertEqual(result["result"]["structuredPatch"], PATCH)
        self.assertIn("Result details kept", self.text("--expand", f"{SESSION[:8]}:{uid(8)[:12]}"))

    def test_context(self):
        out = self.text("-c", "--session", SESSION[:8], "-C", "1", "also check")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith(f"[>{SESSION[:8]}:{uid(5)[:12]} "))
        self.assertIn("main_tool_output:Write", lines[0])
        self.assertIn("main_meta_text:agent", lines[2])

    def test_sessions_and_projects(self):
        sessions = self.text("--list-sessions", "--project", "/home/user/proj")
        self.assertIn(f"[  {SESSION[:8]} | 2026-01-15 12:04 |", sessions)
        self.assertIn("(transcript deleted; in the index)", sessions)
        self.assertIn("title: Fix the tests", sessions)
        self.assertIn("please fix the flaky test", sessions)
        self.assertIn(OTHER[:8], sessions)
        self.assertIn("/home/user/proj", self.text("--list-projects"))
        self.assertIn(" 2 sess ", self.text("--list-projects"))

    def test_snapshots_and_blobs(self):
        listing = self.text("--snapshots", "--session", SESSION[:8], "-c")
        self.assertIn(f"aaaabbbbccccdddd@v1  | {uid(1)[:12]} | 2026-01-15 12:01:00 | /home/user/proj/a.py "
                      f"({len(CRLF)} bytes)", listing)
        self.assertEqual(self.text("--snapshots", "nothing.py", "--session", SESSION[:8]), "No snapshots found\n")
        self.assertIn("second line", self.text("--snapshots", "--all-projects", "SECOND"))
        # The content comes back byte for byte, line ends included
        self.assertEqual(self.cli("--get-snapshot", "aaaabbbb@v1", "--all-projects"), CRLF.encode())
        self.assertEqual(self.cli("--get-snapshot", f"{SESSION[:8]}:aaaabbbbccccdddd@v1"), CRLF.encode())
        self.cli("--get-snapshot", "0000@v1", status=1)
        digest = hashlib.sha256(PNG).hexdigest()
        self.assertEqual(self.cli("--get-blob", digest[:10]), PNG)
        target = os.path.join(self.tmp.name, "picture.png")
        self.cli("--get-blob", f"[image sha256:{digest} image/png {len(PNG)} bytes]", "-o", target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), PNG)

    def test_times_are_local_in_output_and_filters(self):
        tokyo = self.text("-c", "--session", SESSION[:8], "-t", "main_user_text", tz="Asia/Tokyo")
        self.assertIn("| 2026-01-15 21:00:00 |", tokyo)
        self.assertIn("| 2026-01-15 21:02:00 |", tokyo)
        new_york = self.text("-c", "--session", SESSION[:8], "-t", "main_user_text", tz="America/New_York")
        self.assertIn("| 2026-01-15 07:00:00 |", new_york)
        later = self.text("-c", "--session", SESSION[:8], "-t", "main_user_text", "--after",
                          "2026-01-15 21:01", tz="Asia/Tokyo")
        self.assertEqual(len(later.splitlines()), 1)
        self.assertIn("also check the logs", later)
        earlier = self.text("-c", "--session", SESSION[:8], "-t", "main_user_text", "--before",
                            "2026-01-15 07:01", tz="America/New_York")
        self.assertIn("please fix", earlier)
        self.assertNotIn("also check", earlier)
        self.assertIn("| 2026-01-15 21:01:00 |",
                      self.text("--snapshots", "-c", "--session", SESSION[:8], tz="Asia/Tokyo"))


class WithTheFiles(CliCase):
    def test_default_scope_is_the_current_session(self):
        mine = self.text("-c", "flaky", session=SESSION)
        self.assertEqual(len(mine.splitlines()), 1)
        self.assertIn("please fix", mine)
        self.assertEqual(len(self.text("-c", "flaky", "--all-sessions", session=SESSION).splitlines()), 2)
        sessions = self.text("--list-sessions", "-c", session=SESSION)
        self.assertIn(f"[ *{SESSION[:8]} |", sessions)
        self.assertNotIn("transcript deleted", sessions)
        self.assertEqual(self.cli("--get-snapshot", "aaaabbbb@v1", session=SESSION), CRLF.encode())

    def test_without_an_index_the_transcripts_are_read(self):
        missing = os.path.join(self.tmp.name, "no-index.db")
        out = self.text("-c", "--session", SESSION[:8], "-t", "main_user_text", db=missing)
        self.assertIn("please fix the flaky test", out)
        self.assertIn("also check the logs", out)
        self.assertIn(BODY, self.text("--expand", f"{SESSION[:8]}:{uid(3)[:12]}", db=missing))
        self.assertIn("/home/user/proj/a.py", self.text("--snapshots", "-c", session=SESSION, db=missing))
        self.assertEqual(self.cli("--get-snapshot", "aaaabbbb@v1", session=SESSION, db=missing), CRLF.encode())
        self.assertIn(SESSION[:8], self.text("--list-sessions", "-c", session=SESSION, db=missing))

    def test_index_of_an_older_format_is_not_read(self):
        import sqlite3
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA user_version = 1")
        db.commit()
        db.close()
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.claude), CLAUDE_HISTORY_DB=str(self.db_path))
        proc = subprocess.run([sys.executable, "-B", str(util.SKILL_DIR / "search_history.py"), "-c", "--session",
                               SESSION[:8], "flaky"], capture_output=True, env=env, cwd=self.tmp.name)
        self.assertIn(b"has format 1", proc.stderr)
        self.assertIn(b"please fix the flaky test", proc.stdout)


if __name__ == "__main__":
    unittest.main()
