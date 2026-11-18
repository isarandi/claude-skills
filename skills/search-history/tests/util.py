"""Helpers for the tests: a small Claude directory with transcripts in the current format."""
import base64
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

import daemon  # noqa: E402

daemon.log = lambda message: None  # keep the test output readable

# A 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode()
PDF = b"%PDF-1.4\n\x00\x01\x02 not really a pdf\n%%EOF\n"

SESSION = "11111111-2222-3333-4444-555555555555"
PROJECT = "-home-user-proj"


def uid(n):
    """A transcript uuid whose first 12 characters differ per n."""
    return f"{n:08x}-aaaa-bbbb-cccc-dddddddddddd"


def ts(minute, second=0):
    return f"2026-01-15T12:{minute:02d}:{second:02d}.000Z"


def base(n, kind, minute, session=SESSION, agent=None, parent=None, **extra):
    rec = {"parentUuid": uid(parent) if parent else None, "isSidechain": agent is not None,
           "type": kind, "uuid": uid(n), "timestamp": ts(minute), "sessionId": session,
           "cwd": "/home/user/proj", "version": "2.1.327", "userType": "external"}
    if agent:
        rec["agentId"] = agent
    rec.update(extra)
    return rec


def user(n, content, minute, **extra):
    return base(n, "user", minute, message={"role": "user", "content": content}, **extra)


def assistant(n, blocks, minute, model="claude-test-1", **extra):
    return base(n, "assistant", minute,
                message={"role": "assistant", "model": model, "content": blocks}, **extra)


def tool_use(tool_id, name, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def tool_result(tool_id, content, **extra):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, **extra}


def image_block(data_b64=PNG_B64, media_type="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


def queued(n, prompt, minute, origin="human", **extra):
    attachment = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
                  "source_uuid": uid(9000 + n), "timestamp": ts(minute)}
    if origin:
        attachment["origin"] = {"kind": origin}
    attachment.update(extra)
    return base(n, "attachment", minute, attachment=attachment)


def write_jsonl(path, records, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def old(path, seconds=3600):
    """Give a file a modification time in the past (tool-results files must have settled)."""
    st = path.stat()
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f]
