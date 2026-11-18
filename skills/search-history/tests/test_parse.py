"""Transcript records -> searchable items (search_history.expand_record)."""
import hashlib
import json
import unittest

import util
from util import PNG, assistant, image_block, queued, tool_result, tool_use, user

import search_history as sh


def items(rec, **kwargs):
    return list(sh.expand_record(rec, **kwargs))


class ToolInputs(unittest.TestCase):
    def test_write_and_edit_are_kept_in_full(self):
        body = "é" * 3000 + "\nlast line"
        rec = assistant(1, [tool_use("t1", "Write", {"file_path": "/a/b.py", "content": body}),
                            tool_use("t2", "Edit", {"file_path": "/a/b.py", "old_string": "x" * 900,
                                                    "new_string": "y" * 900, "replace_all": True})], 0)
        write, edit = items(rec)
        self.assertEqual(write["type"], "main_tool_input")
        self.assertEqual(write["content"], "/a/b.py\n" + body)
        self.assertEqual(json.loads(write["input_json"]), {"file_path": "/a/b.py", "content": body})
        self.assertEqual(edit["content"], "/a/b.py\n-" + "x" * 900 + "\n+" + "y" * 900)
        self.assertIs(json.loads(edit["input_json"])["replace_all"], True)
        self.assertEqual((write["tool_use_id"], edit["tool_use_id"]), ("t1", "t2"))

    def test_other_tools_are_readable_text_with_real_characters(self):
        prompt = 'Zeile eins\nZeile zwei mit "Anführung" und ü' + "z" * 2000
        rec = assistant(1, [tool_use("t1", "Agent", {"description": "d", "prompt": prompt, "n": 3})], 0)
        (call,) = items(rec)
        self.assertEqual(call["content"], f"description: d\nprompt: {prompt}\nn: 3")
        self.assertNotIn("\\u", call["input_json"])
        self.assertEqual(json.loads(call["input_json"])["prompt"], prompt)

    def test_bash_shows_the_command(self):
        rec = assistant(1, [tool_use("t1", "Bash", {"command": "ls -la", "description": "List"})], 0)
        (call,) = items(rec)
        self.assertEqual(call["content"], "ls -la")
        self.assertEqual(json.loads(call["input_json"])["description"], "List")


class ToolResults(unittest.TestCase):
    def test_image_becomes_placeholder_and_blob(self):
        stored = {}

        def sink(data, media_type):
            digest = hashlib.sha256(data).hexdigest()
            stored[digest] = (data, media_type)
            return digest

        rec = user(2, [tool_result("t1", [{"type": "text", "text": "a picture"}, image_block()])], 1)
        (out,) = items(rec, blob_sink=sink)
        digest = hashlib.sha256(PNG).hexdigest()
        self.assertEqual(out["type"], "main_tool_output")
        self.assertEqual(out["content"], f"a picture\n[image sha256:{digest} image/png {len(PNG)} bytes]")
        self.assertEqual(stored, {digest: (PNG, "image/png")})
        self.assertNotIn(util.PNG_B64, out["content"])
        self.assertTrue(sh.PLACEHOLDER_RE.search(out["content"]))

    def test_edit_result_keeps_original_file_and_patch(self):
        patch = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]}]
        rec = user(2, [tool_result("t1", "The file was updated")], 1,
                   toolUseResult={"filePath": "/a", "oldString": "a", "newString": "b", "originalFile": "a\n",
                                  "structuredPatch": patch, "userModified": False, "replaceAll": False})
        (out,) = items(rec)
        kept = json.loads(out["result_json"])
        self.assertEqual(kept["originalFile"], "a\n")
        self.assertEqual(kept["structuredPatch"], patch)

    def test_plain_result_keeps_no_result_json_and_error_flag(self):
        rec = user(2, [tool_result("t1", "boom", is_error=True)], 1,
                   toolUseResult={"stdout": "", "stderr": "boom", "interrupted": False})
        (out,) = items(rec)
        self.assertIsNone(out["result_json"])
        self.assertTrue(out["is_error"])

    def test_persisted_output_is_read_in_full(self):
        rec = user(2, [tool_result("t1", "<persisted-output>\nOutput too large. Preview: abc")], 1,
                   toolUseResult={"stdout": "abc", "persistedOutputPath": "/x/tool-results/t.txt",
                                  "persistedOutputSize": 6})
        asked = []
        (out,) = items(rec, read_persisted=lambda p: asked.append(p) or "abcdef")
        self.assertEqual(asked, ["/x/tool-results/t.txt"])
        self.assertEqual(out["content"], "abcdef")
        self.assertEqual(json.loads(out["result_json"])["persistedOutputPath"], "/x/tool-results/t.txt")
        self.assertFalse(out["persisted_missing"])
        (preview,) = items(rec, read_persisted=lambda p: None)  # the file is gone
        self.assertTrue(preview["content"].startswith("<persisted-output>"))
        self.assertTrue(preview["persisted_missing"])


class UserText(unittest.TestCase):
    def test_typed_message(self):
        (item,) = items(user(1, "please fix the test", 0, origin={"kind": "human"}))
        self.assertEqual((item["type"], item["origin"], item["is_meta"]), ("main_user_text", "human", False))
        self.assertIsNone(item["legacy_type"])

    def test_message_from_an_agent_is_meta_text(self):
        (item,) = items(user(1, "report from the agent", 0, isMeta=True, origin={"kind": "peer", "from": "a1"}))
        self.assertEqual((item["type"], item["origin"]), ("main_meta_text", "agent"))
        self.assertEqual(item["legacy_type"], "main_user_text")

    def test_task_notification_without_origin_is_recognized_by_its_tag(self):
        (item,) = items(user(1, "<task-notification>\n<task-id>x</task-id>", 0))
        self.assertEqual((item["type"], item["origin"]), ("main_meta_text", "task-notification"))

    def test_message_delivered_mid_turn(self):
        (item,) = items(queued(5, "also check the logs", 3))
        self.assertEqual((item["type"], item["origin"], item["content"]),
                         ("main_user_text", "human", "also check the logs"))
        self.assertEqual(item["uuid"], util.uid(5)[:12])
        (note,) = items(queued(6, "<task-notification>done</task-notification>", 3,
                               origin="task-notification", commandMode="task-notification"))
        self.assertEqual((note["type"], note["origin"]), ("main_meta_text", "task-notification"))
        (to_agent,) = items(queued(7, "keep going", 3, origin="coordinator", isMeta=True), agent_id="a1")
        self.assertEqual((to_agent["type"], to_agent["origin"]), ("agent_input_text", "coordinator"))
        self.assertEqual(items(util.base(8, "attachment", 3, attachment={"type": "todo_reminder"})), [])

    def test_what_started_the_turn_only_fills_in_the_origin(self):
        # A scheduled task's prompt: no origin of its own, flagged as meta
        (fired,) = items(user(1, "run the nightly check", 0, isMeta=True, turnOrigin="scheduled"))
        self.assertEqual((fired["type"], fired["origin"]), ("main_meta_text", "scheduled"))
        # Output of a command the human ran in a human turn stays user text
        (output,) = items(user(2, "<bash-stdout>ok</bash-stdout>", 0, turnOrigin="human"))
        self.assertEqual((output["type"], output["origin"]), ("main_user_text", "human"))
        # A prompt given on the command line (claude -p)
        (prompt,) = items(user(3, "list the files", 0, turnOrigin="sdk"))
        self.assertEqual((prompt["type"], prompt["origin"]), ("main_user_text", "sdk"))
        # The message's own origin decides, whatever started the turn
        (peer,) = items(user(4, "report", 0, isMeta=True, origin={"kind": "peer"}, turnOrigin="human"))
        self.assertEqual((peer["type"], peer["origin"]), ("main_meta_text", "agent"))

    def test_queued_notification_without_origin(self):
        (note,) = items(queued(6, "Background task stopped", 3, origin=None, commandMode="task-notification"))
        self.assertEqual((note["type"], note["origin"]), ("main_meta_text", "task-notification"))

    def test_pasted_image_and_several_text_blocks_make_one_item(self):
        rec = user(1, [{"type": "text", "text": "look at this"}, image_block(),
                       {"type": "text", "text": "and this"}], 0, origin={"kind": "human"})
        (item,) = items(rec)
        digest = hashlib.sha256(PNG).hexdigest()
        self.assertEqual(item["content"],
                         f"look at this\n[image sha256:{digest} image/png {len(PNG)} bytes]\nand this")

    def test_agent_transcript_types(self):
        (prompt,) = items(user(1, "do the task", 0), agent_id="a1")
        self.assertEqual(prompt["type"], "agent_input_text")
        (reminder,) = items(user(2, "<system-reminder>be careful</system-reminder>", 0, isMeta=True),
                            agent_id="a1")
        self.assertEqual((reminder["type"], reminder["legacy_type"]), ("agent_meta_text", "agent_input_text"))
        (summary,) = items(user(3, "This session is being continued", 0, isCompactSummary=True), agent_id="a1")
        self.assertEqual((summary["type"], summary["legacy_type"]), ("agent_summary", "main_summary"))
        (main_summary,) = items(user(3, "This session is being continued", 0, isCompactSummary=True))
        self.assertEqual((main_summary["type"], main_summary["legacy_type"]), ("main_summary", None))


class AssistantRecords(unittest.TestCase):
    def test_text_thinking_model_and_parent(self):
        rec = assistant(4, [{"type": "thinking", "thinking": "", "signature": "sig"},
                            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                            {"type": "text", "text": "Done."}], 2, parent=3)
        text, thinking = items(rec)
        self.assertEqual((text["type"], text["content"], text["model"]),
                         ("main_assistant_text", "Done.", "claude-test-1"))
        self.assertEqual((thinking["type"], thinking["content"]), ("main_assistant_thinking", "hmm"))
        self.assertEqual(text["parent_uuid"], util.uid(3)[:12])

    def test_other_record_types_give_nothing(self):
        for kind in ("api-request-blob", "system", "ai-title", "file-history-snapshot"):
            self.assertEqual(items({"type": kind, "message": {"role": "user", "content": "x"}}), [])


if __name__ == "__main__":
    unittest.main()
