#!/usr/bin/env python3
"""Search conversation history with filtering by message type, tool, and scope."""
import argparse
import base64
import binascii
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# Claude Code's directory (CLAUDE_CONFIG_DIR is Claude Code's own override)
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

# SQLite database path (outside ~/.claude to avoid triggering Claude's watchers).
# CLAUDE_HISTORY_DB points the search at another index, such as a copy or a backup.
DB_PATH = Path(os.environ.get("CLAUDE_HISTORY_DB")
               or Path.home() / ".local/share/claude-history-index/history.db")

# Index format version, kept in the database's PRAGMA user_version.
# 0: timestamps in the indexer process's local time zone.
# 1: timestamps in UTC; transcript records other than user/assistant messages not indexed.
# 2: tool inputs in full; images and other binary data in the blobs table, a placeholder
#    in the text; messages delivered mid-turn, side files (tool results, memory, subagent
#    meta, prompt history) and sessions indexed; snapshots keyed by session.
INDEX_VERSION = 2

# Timestamp format of the index (UTC from version 1 on) and of the output (local time)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# All valid record types
MAIN_TYPES = [
    "main_user_text",           # what the human typed (also local commands and their output)
    "main_meta_text",           # text put into the conversation by something else: messages
                                # from agents, task notifications, reminders, skill bodies
    "main_assistant_text", "main_assistant_thinking",
    "main_tool_input", "main_tool_output", "main_summary",
]
AGENT_TYPES = [
    "agent_input_text",         # the agent's prompt and the messages sent to it
    "agent_meta_text",          # reminders and skill bodies inside an agent's transcript
    "agent_assistant_text", "agent_assistant_thinking",
    "agent_tool_input", "agent_tool_output", "agent_summary",
]
# Files next to the transcripts. Searched only when -t names them.
FILE_TYPES = [
    "plan",                     # ~/.claude/plans/*.md (each version)
    "memory",                   # projects/<project>/memory/** (each version)
    "tool_file",                # <session>/tool-results/*: files saved by tools
    "subagent_meta",            # <session>/subagents/agent-*.meta.json
    "prompt_history",           # ~/.claude/history.jsonl with its pasted texts
    "paste",                    # ~/.claude/paste-cache/*
]
ALL_TYPES = MAIN_TYPES + AGENT_TYPES + FILE_TYPES

# What a message's origin can be (the --origin filter); others are stored as Claude Code names them
ORIGINS = ["human", "agent", "coordinator", "task-notification"]


# -----------------------------------------------------------------------------
# Regex -> the literals a match must contain (AND/OR from alternations), for the
# SQL prefilter of a search
# -----------------------------------------------------------------------------

try:
    import re._parser as sre_parse
    _HAVE_SRE_PARSE = True
except ImportError:
    try:
        import sre_parse
        _HAVE_SRE_PARSE = True
    except ImportError:
        _HAVE_SRE_PARSE = False


def _extract_fts_tree(pattern):
    """Extract the tree of required literals from a regex pattern.

    Returns tree of ('LIT', str), ('AND', [...]), ('OR', [...]) or None.
    Handles alternation as OR, sequential literals as AND.
    Skips negated contexts and optional quantifiers.
    """
    if not _HAVE_SRE_PARSE:
        return _extract_fts_tree_naive(pattern)
    try:
        parsed = sre_parse.parse(pattern)
    except Exception:
        return _extract_fts_tree_naive(pattern)
    return _extract_from_items(list(parsed))


def _extract_from_items(items):
    """Extract from sequence of parse items (implicit AND)."""
    parts = []
    current_lit = []

    for op, val in items:
        if op == sre_parse.LITERAL:
            current_lit.append(chr(val))
            continue

        # For BRANCH: don't flush yet, prefix might combine with branches
        if op == sre_parse.BRANCH:
            prefix = ''.join(current_lit)
            current_lit = []
            or_result = _extract_or(val[1], prefix)
            if or_result:
                parts.append(or_result)
            elif len(prefix) >= 3:
                parts.append(('LIT', prefix))
            continue

        # Flush accumulated literal
        if current_lit:
            s = ''.join(current_lit)
            if len(s) >= 3:
                parts.append(('LIT', s))
            current_lit = []

        if op == sre_parse.SUBPATTERN:
            sub = _extract_from_items(list(val[3]))
            if sub:
                parts.append(sub)

        elif op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT):
            min_count = val[0]
            if min_count >= 1:
                sub = _extract_from_items(list(val[2]))
                if sub:
                    parts.append(sub)

        elif op == sre_parse.ASSERT_NOT:
            pass  # Negated context, skip

    if current_lit:
        s = ''.join(current_lit)
        if len(s) >= 3:
            parts.append(('LIT', s))

    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return ('AND', parts)


def _extract_or(branches, prefix=""):
    """Extract OR from alternation branches."""
    results = []
    for branch in branches:
        if prefix:
            b = _extract_from_items_raw(branch)
            b = _prepend_prefix(b, prefix)
        else:
            b = _extract_from_items(branch)
        if b is None:
            return None
        results.append(b)

    if not results:
        return None
    if len(results) == 1:
        return results[0]
    return ('OR', results)


def _extract_from_items_raw(items):
    """Extract from items without min-length filter (for prefix combination)."""
    parts = []
    current_lit = []

    for op, val in items:
        if op == sre_parse.LITERAL:
            current_lit.append(chr(val))
            continue

        if current_lit:
            parts.append(('LIT', ''.join(current_lit)))
            current_lit = []

        if op == sre_parse.SUBPATTERN:
            sub = _extract_from_items_raw(list(val[3]))
            if sub:
                parts.append(sub)
        elif op == sre_parse.BRANCH:
            or_result = _extract_or(val[1])
            if or_result:
                parts.append(or_result)

    if current_lit:
        parts.append(('LIT', ''.join(current_lit)))

    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return ('AND', parts)


def _prepend_prefix(node, prefix):
    """Prepend a literal prefix to a node's first literal."""
    if node is None:
        return ('LIT', prefix) if len(prefix) >= 3 else None

    if node[0] == 'LIT':
        combined = prefix + node[1]
        return ('LIT', combined) if len(combined) >= 3 else None

    if node[0] == 'AND':
        children = list(node[1])
        if children:
            children[0] = _prepend_prefix(children[0], prefix)
            children = [c for c in children if c and (c[0] != 'LIT' or len(c[1]) >= 3)]
        if not children:
            return ('LIT', prefix) if len(prefix) >= 3 else None
        if len(children) == 1:
            return children[0]
        return ('AND', children)

    if node[0] == 'OR':
        new_branches = [_prepend_prefix(b, prefix) for b in node[1]]
        if any(b is None for b in new_branches):
            return None
        return ('OR', new_branches)

    return node


def _extract_fts_tree_naive(pattern):
    """Fallback: extract literals by splitting on metacharacters."""
    chunks = re.split(r'[\\.*+?^${}()|[\]\s]+', pattern)
    lits = [('LIT', c) for c in chunks if len(c) >= 3]
    if not lits:
        return None
    if len(lits) == 1:
        return lits[0]
    return ('AND', lits)


def _is_regex(pattern):
    """Check if pattern contains regex metacharacters."""
    return bool(re.search(r'[\\.*+?^${}()|[\]]', pattern))


# -----------------------------------------------------------------------------
# Reading the index (SQLite)
# -----------------------------------------------------------------------------

# Record types by what scopes them: a session, a project, or nothing
_SESSION_SCOPED = MAIN_TYPES + AGENT_TYPES + ["tool_file", "subagent_meta", "prompt_history"]
_PROJECT_SCOPED = ["memory"]
_UNSCOPED = ["plan", "paste"]

_index = None  # open_index()'s connection; False once it is known that there is none


def open_index():
    """Read-only connection to the index, or None if there is none this version can read."""
    global _index
    if _index is None:
        _index = _connect_index() or False
    return _index or None


def _connect_index():
    if not DB_PATH.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{urllib.parse.quote(str(DB_PATH))}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != INDEX_VERSION:
            what = ("the indexer has not migrated it yet (restart claude-history-indexer)"
                    if version < INDEX_VERSION else "update this skill")
            print(f"Index {DB_PATH} has format {version}, this search reads format {INDEX_VERSION}: "
                  f"{what}. Reading the transcript files instead.", file=sys.stderr)
            db.close()
            return None
        if not db.execute("SELECT 1 FROM records LIMIT 1").fetchone():
            db.close()
            return None
        return db
    except sqlite3.Error:
        return None


def db_available():
    """Check if SQLite database is available and has data."""
    return open_index() is not None


def _like_prefix(value):
    """LIKE pattern for "starts with value" (with ESCAPE '\\')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _prefix_range(column, prefix):
    """SQL condition and parameters for "column starts with prefix", in a form that
    can use an index on the column (LIKE cannot: it ignores the case of ASCII letters)."""
    return f"({column} >= ? AND {column} < ?)", [prefix, prefix + "\U0010ffff"]


def _session_condition(db, prefix):
    """SQL condition and parameters for "session id starts with prefix".

    The ids are looked up first so that the search can use the index on session_id.
    """
    ids = [row[0] for row in db.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ESCAPE '\\'", (_like_prefix(prefix),))]
    if ids:
        return f"session_id IN ({','.join('?' * len(ids))})", ids
    return _prefix_range("session_id", prefix)


def _project_condition(project_name, with_subdirs=False):
    if with_subdirs:
        return "(project_name = ? OR project_name LIKE ? ESCAPE '\\')", [project_name,
                                                                         _like_prefix(project_name + "-")]
    return "project_name = ?", [project_name]


def _row_to_result(row):
    """A records row as the dict the display functions take (timestamp in local time)."""
    result = dict(row)
    result["type"] = result.pop("record_type")
    result["content"] = result["content"] or ""
    result["uuid"] = result["uuid"] or ""
    result["timestamp_utc"] = result["timestamp"] or ""
    result["timestamp"] = utc_time_to_local(result["timestamp"] or "")
    return result


def search_sqlite(
    db,
    term=None,
    matcher=None,
    is_regex=True,
    type_list=None,
    tool=None,
    time_from=None,
    time_to=None,
    project_name=None,
    with_subdirs=False,
    session_id=None,
    agent_id=None,
    origin=None,
    ignore_case=False,
):
    """Search records in SQLite.

    Content is prefiltered in SQL with LIKE conditions derived from the term
    (a superset of the real matcher's semantics), then every row is verified
    with `matcher` so results are identical to the JSONL path.

    type_list: record types (None: all). A session filter applies to the types
    that belong to a session, a project filter also to memory notes; plans and
    the paste cache belong to neither.
    time_from/time_to are naive local datetimes; returned timestamps are local.
    session_id and agent_id are prefixes. Returns None on an SQLite error.
    """
    params = []
    conditions = []

    # Scope, by kind of record
    types = type_list if type_list is not None else ALL_TYPES
    scopes = []
    for group, by_session, by_project in ((_SESSION_SCOPED, True, True), (_PROJECT_SCOPED, False, True),
                                          (_UNSCOPED, False, False)):
        wanted = [t for t in types if t in group]
        if not wanted:
            continue
        parts = [f"record_type IN ({','.join('?' * len(wanted))})"]
        scope_params = list(wanted)
        if by_session and session_id:
            sql, more = _session_condition(db, session_id)
            parts.append(sql)
            scope_params += more
        if by_project and project_name:
            sql, more = _project_condition(project_name, with_subdirs)
            parts.append(sql)
            scope_params += more
        scopes.append("(" + " AND ".join(parts) + ")")
        params += scope_params
    if not scopes:
        return []
    conditions.append("(" + " OR ".join(scopes) + ")")

    # Content prefilter (never excludes true matches; matcher verifies below)
    if term:
        prefilter = _term_to_like(term, is_regex, ignore_case)
        if prefilter:
            like_sql, like_params = prefilter
            conditions.append(like_sql)
            params.extend(like_params)

    if tool:
        conditions.append("tool_name = ?")
        params.append(tool)
    if origin:
        conditions.append("origin = ?")
        params.append(origin)
    if time_from:
        conditions.append("timestamp >= ?")
        params.append(local_to_utc_time(time_from))
    if time_to:
        conditions.append("timestamp <= ?")
        params.append(local_to_utc_time(time_to))
    if agent_id:
        conditions.append("agent_id LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(agent_id))

    sql = "SELECT * FROM records WHERE " + " AND ".join(conditions) + " ORDER BY timestamp ASC, id ASC"
    try:
        rows = db.execute(sql, params)
        # Convert to expected format, verifying content against the real matcher
        return [_row_to_result(row) for row in rows if not matcher or matcher(row["content"] or "")]
    except sqlite3.Error as e:
        print(f"SQLite error: {e}", file=sys.stderr)
        return None  # Signal to fall back to JSONL


def context_sqlite(db, matches, n):
    """The matches with n items before and after each, in the order of their transcript.

    Neighbors are the items of the same file (a session's transcript or an agent's),
    whatever their type. Each result gets is_match and context_for (the uuid of the
    match it surrounds).
    """
    match_ids = {m["id"] for m in matches}
    results, seen = [], set()
    for match in matches:
        position = (match["source_file"], match["byte_offset"] or 0, match["byte_offset"] or 0, match["id"])
        before = db.execute(
            "SELECT * FROM records WHERE source_file = ? AND (byte_offset < ? OR (byte_offset = ? AND id < ?)) "
            "ORDER BY byte_offset DESC, id DESC LIMIT ?", (*position, n)).fetchall()
        after = db.execute(
            "SELECT * FROM records WHERE source_file = ? AND (byte_offset > ? OR (byte_offset = ? AND id > ?)) "
            "ORDER BY byte_offset ASC, id ASC LIMIT ?", (*position, n)).fetchall()
        group = [_row_to_result(r) for r in reversed(before)] + [match] + [_row_to_result(r) for r in after]
        for result in group:
            if result["id"] in seen:
                continue
            seen.add(result["id"])
            result["is_match"] = result["id"] in match_ids
            result["context_for"] = match["uuid"]
            results.append(result)
    return results


def expand_sqlite(db, uuid_prefix, session_prefix=None, agent_prefix=None):
    """Everything the index has of one transcript record, or of one indexed file.

    Returns (result, candidates): result as expand_uuid() gives it plus "rows" (its
    items as search results), or None with the rows of the several records that the
    prefix matches.
    """
    sql, params = _prefix_range("uuid", uuid_prefix[:12])
    conditions = [sql]
    if session_prefix:
        sql, more = _session_condition(db, session_prefix)
        conditions.append(sql)
        params += more
    if agent_prefix:
        conditions.append("agent_id LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(agent_prefix))
    rows = db.execute("SELECT * FROM records WHERE " + " AND ".join(conditions)
                      + " ORDER BY timestamp, byte_offset, id", params).fetchall()
    if not rows:
        return None, []
    if len({r["uuid"] for r in rows}) > 1:
        return None, rows  # different records: a longer prefix is needed

    first = rows[0]
    result = {
        "uuid": first["uuid"],
        "timestamp": utc_time_to_local(first["timestamp"] or ""),
        "type": first["record_type"],
        "session_id": first["session_id"],
        "agent_id": first["agent_id"],
        "source_file": first["source_file"],
        "content": None,
        "thinking": None,
        "tool_uses": [],
        "rows": [_row_to_result(r) for r in rows],
    }
    kinds = {r["record_type"].split("_", 1)[-1] for r in rows}
    if kinds & {"assistant_text", "assistant_thinking", "tool_input"}:
        result["type"] = "assistant"
    elif kinds & {"user_text", "meta_text", "input_text", "tool_output", "summary"}:
        result["type"] = "user"
    texts, thinking = [], []
    for row in rows:
        kind = row["record_type"]
        if kind.endswith("tool_input"):
            output = db.execute(
                "SELECT * FROM records WHERE tool_use_id = ? AND tool_use_id != '' "
                "AND record_type IN ('main_tool_output', 'agent_tool_output') LIMIT 1",
                (row["tool_use_id"],)).fetchone()
            result["tool_uses"].append({
                "name": row["tool_name"], "input": row["content"], "input_json": row["input_json"],
                "output": output["content"] if output else None,
                "result_json": output["result_json"] if output else None,
                "is_error": bool(output["is_error"]) if output else False})
            if output:
                result["rows"].append(_row_to_result(output))
        elif kind.endswith("tool_output"):
            call = db.execute(
                "SELECT * FROM records WHERE tool_use_id = ? AND tool_use_id != '' "
                "AND record_type IN ('main_tool_input', 'agent_tool_input') LIMIT 1",
                (row["tool_use_id"],)).fetchone()
            result["tool_uses"].append({
                "name": row["tool_name"], "input": call["content"] if call else None,
                "input_json": call["input_json"] if call else None,
                "output": row["content"], "result_json": row["result_json"],
                "is_error": bool(row["is_error"])})
            if call:
                result["rows"].insert(0, _row_to_result(call))
        elif kind.endswith("thinking"):
            thinking.append(row["content"] or "")
        else:
            texts.append(row["content"] or "")
    result["content"] = "\n".join(texts) or None
    result["thinking"] = "\n".join(thinking) or None
    return result, []


def get_blob(db, digest_prefix):
    """(hash, media type, data) of the blobs whose SHA-256 starts with digest_prefix (at most 5)."""
    sql, params = _prefix_range("hash", digest_prefix.removeprefix("sha256:").lower())
    return [tuple(row) for row in db.execute(
        f"SELECT hash, media_type, data FROM blobs WHERE {sql} LIMIT 5", params)]


def snapshots_sqlite(db, session_id=None, project_name=None, with_subdirs=False, filename_filter=None,
                     backup_prefix=None, version=None, msgid=None, time_from=None, time_to=None, term=None):
    """File snapshots from the index, oldest first: one per stored file version.

    session_id is a prefix; time_from/time_to are naive local datetimes; term is
    searched in the content without regard to case. Timestamps are returned local.
    """
    conditions, params = [], []
    if session_id:
        sql, more = _session_condition(db, session_id)
        conditions.append(sql)
        params += more
    if project_name:
        sql, more = _project_condition(project_name, with_subdirs)
        conditions.append(f"session_id IN (SELECT session_id FROM sessions WHERE {sql})")
        params += more
    if filename_filter:
        conditions.append("(file_path LIKE ? ESCAPE '\\' OR backup_id LIKE ? ESCAPE '\\')")
        params += ["%" + _like_prefix(filename_filter)] * 2
    if backup_prefix:
        conditions.append("backup_id LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(backup_prefix))
    if version:
        conditions.append("backup_id LIKE ? ESCAPE '\\'")
        params.append("%@" + _like_prefix(version)[:-1])
    if msgid:
        conditions.append("message_id LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(msgid))
    if time_from:
        conditions.append("timestamp >= ?")
        params.append(local_to_utc_time(time_from))
    if time_to:
        conditions.append("timestamp <= ?")
        params.append(local_to_utc_time(time_to))
    if term:
        conditions.append("content LIKE ? ESCAPE '\\'")
        params.append("%" + _like_prefix(term))
    sql = "SELECT * FROM snapshots" + (" WHERE " + " AND ".join(conditions) if conditions else "") \
        + " ORDER BY timestamp, backup_id"
    results = []
    for row in db.execute(sql, params):
        result = dict(row)
        if term and term.lower() not in (result["content"] or "").lower():
            continue  # LIKE ignores case for ASCII only
        result["timestamp"] = utc_time_to_local(result["timestamp"] or "")
        results.append(result)
    return results


def sessions_sqlite(db, project_name=None, with_subdirs=False, detailed=False):
    """Sessions in the index, newest first, as list_sessions() gives them, plus the
    project, the title and whether the transcript file still exists."""
    sql, params = "SELECT * FROM sessions", []
    if project_name:
        condition, params = _project_condition(project_name, with_subdirs)
        sql += " WHERE " + condition
    main_types = ",".join(f"'{t}'" for t in MAIN_TYPES)
    results = []
    for row in db.execute(sql, params).fetchall():
        sid = row["session_id"]
        info = {"id": sid, "project": row["project_name"], "title": row["custom_title"] or row["title"],
                "last_ts": row["last_ts"] or "",
                "mtime": utc_time_to_local(row["last_ts"] or "")[:16],
                "on_disk": bool(row["source_file"]) and os.path.exists(row["source_file"])}
        info["msgs"] = db.execute(
            f"SELECT COUNT(DISTINCT uuid) FROM records WHERE session_id = ? AND record_type IN ({main_types})",
            (sid,)).fetchone()[0]
        preview = db.execute(
            "SELECT substr(content, 1, 200) FROM records WHERE session_id = ? AND record_type = 'main_user_text' "
            "ORDER BY timestamp, id LIMIT 1", (sid,)).fetchone()
        info["preview"] = (preview[0] if preview else "").replace("\n", " ").strip()
        if detailed:
            info["duration"] = _duration(row["first_ts"], row["last_ts"])
            info["tools"] = dict(db.execute(
                "SELECT tool_name, COUNT(*) FROM records WHERE session_id = ? AND record_type = 'main_tool_input' "
                "GROUP BY tool_name", (sid,)).fetchall())
            info["files_edited"] = db.execute(
                "SELECT COUNT(DISTINCT substr(content, 1, instr(content || char(10), char(10)) - 1)) FROM records "
                "WHERE session_id = ? AND record_type = 'main_tool_input' AND tool_name IN ('Edit', 'Write')",
                (sid,)).fetchone()[0]
        results.append(info)
    return sorted(results, key=lambda s: s["last_ts"], reverse=True)


def projects_sqlite(db):
    """Projects in the index as (name, path, number of sessions, last activity), newest first."""
    rows = db.execute("SELECT project_name, COUNT(*), MAX(last_ts), MAX(cwd) FROM sessions "
                      "WHERE project_name IS NOT NULL GROUP BY project_name").fetchall()
    result = [(name, cwd or "/" + name[1:].replace("-", "/"), n, utc_time_to_local(last or "")[:16])
              for name, n, last, cwd in rows]
    return sorted(result, key=lambda x: x[3], reverse=True)


def _duration(first_ts, last_ts):
    """"3h 20m" between two index timestamps."""
    try:
        delta = datetime.strptime(last_ts, TIME_FORMAT) - datetime.strptime(first_ts, TIME_FORMAT)
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    return f"{hours}h {rem // 60}m" if hours > 0 else f"{rem // 60}m"


def _term_to_like(term, is_regex, ignore_case):
    """Build a SQL LIKE prefilter for a search term, or None for no prefilter.

    The prefilter must be a superset of the matcher: LIKE is substring-based
    and ASCII-case-insensitive, so it can only ever over-match. Non-ASCII
    terms with -i get no prefilter (LIKE is case-sensitive beyond ASCII,
    which could drop true matches).
    """
    if ignore_case and any(ord(c) > 127 for c in term):
        return None
    if is_regex and _is_regex(term):
        return _tree_to_like(_extract_fts_tree(term))
    return _tree_to_like(('LIT', term))


def _tree_to_like(node):
    """Convert an extracted literal tree to (sql_condition, params) or None."""
    if node is None:
        return None

    if node[0] == 'LIT':
        esc = node[1].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        return ("content LIKE ? ESCAPE '\\'", [f"%{esc}%"])

    if node[0] == 'AND':
        # Dropping an unconvertible AND-part only weakens the filter (still a superset)
        subs = [s for s in (_tree_to_like(n) for n in node[1]) if s]
        if not subs:
            return None
        return ("(" + " AND ".join(s[0] for s in subs) + ")",
                [p for s in subs for p in s[1]])

    if node[0] == 'OR':
        # A dropped OR branch would exclude true matches, so all must convert
        subs = [_tree_to_like(n) for n in node[1]]
        if not subs or any(s is None for s in subs):
            return None
        return ("(" + " OR ".join(s[0] for s in subs) + ")",
                [p for s in subs for p in s[1]])

    return None


def expand_type_pattern(pattern):
    """Expand a type pattern (with wildcards) to list of matching types."""
    if pattern is None:
        return None  # No filter
    return [t for t in ALL_TYPES if fnmatch.fnmatch(t, pattern)]


def compile_search_pattern(term, ignore_case=False, is_regex=False):
    """Compile search term into a matcher function.

    Returns a function that takes content string and returns True if it matches.
    """
    if term is None:
        return None

    if is_regex:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            pattern = re.compile(term, flags)
            return lambda content: pattern.search(content) is not None
        except re.error as e:
            print(f"Invalid regex: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        if ignore_case:
            term_lower = term.lower()
            return lambda content: term_lower in content.lower()
        else:
            return lambda content: term in content


PROJECTS_DIR = CLAUDE_DIR / "projects"

# Characters of an item that a listing shows, and of one part that --expand shows, without --full
LISTING_CUT = 2000
EXPAND_CUT = 100_000


def main():
    parser = argparse.ArgumentParser(description="Search conversation history")
    parser.add_argument("term", nargs="?", help="Search term")
    parser.add_argument("-t", "--type",
                        help="Record type pattern (supports * wildcard). Types: "
                             + ", ".join(ALL_TYPES) + ". Examples: main_*, *_text, *_assistant_*. "
                             "Default: the main_* types (with --agent also agent_*)")
    parser.add_argument("--tool", help="Tool name filter (Bash, Read, Write, Edit, etc.)")
    parser.add_argument("--origin", metavar="ORIGIN",
                        help="Only messages of this origin: " + ", ".join(ORIGINS) + " (others as Claude Code "
                             "names them, e.g. scheduled)")
    parser.add_argument("-i", "--ignore-case", action="store_true",
                        help="Case-insensitive search")
    parser.add_argument("-r", "--regex", action="store_true", default=True,
                        help="Interpret TERM as regex (default: on)")
    parser.add_argument("-L", "--literal", action="store_false", dest="regex",
                        help="Literal search (disable regex)")
    parser.add_argument("--first", type=int, help="Return first N results")
    parser.add_argument("--last", type=int, help="Return last N results")
    parser.add_argument("-c", "--compact", action="store_true",
                        help="Compact one-line listing: UUID, time, type, content preview")
    parser.add_argument("--full", action="store_true",
                        help=f"Print contents in full: no cut after {LISTING_CUT} characters in listings, "
                             f"none after {EXPAND_CUT} per part with --expand")
    parser.add_argument("--json", action="store_true",
                        help="Print results as JSON lines, in full, with a tool call's exact input and an "
                             "edit's result (originalFile, structuredPatch); for search and --expand")
    parser.add_argument("--after", dest="time_from", metavar="TIME",
                        help="After time (local), e.g. '2025-12-23 10:30:00' (same format as header), or shorter: 10:30, 12-23 10:30")
    parser.add_argument("--before", dest="time_to", metavar="TIME",
                        help="Before time (local), e.g. '2025-12-23 10:30:00' (same format as header), or shorter: 10:30, 12-23 10:30")
    parser.add_argument("--expand", metavar="UUID",
                        help="Get full context for a UUID, as printed in a result's header "
                             "(session:uuid, session:agent:uuid, or the uuid alone)")
    parser.add_argument("-C", "--context", type=int, metavar="N",
                        help="Show N messages before and after each match")
    parser.add_argument("--snapshots", nargs="?", const="", metavar="FILENAME",
                        help="List file snapshots (optionally filter by filename)")
    parser.add_argument("--get-snapshot", metavar="ID",
                        help="Get file content of a snapshot by backup ID (prefix match), "
                             "optionally session:ID")
    parser.add_argument("--msgid", metavar="MSGID",
                        help="Filter snapshots by message ID (prefix match)")
    parser.add_argument("--get-blob", metavar="HASH",
                        help="Write an image or other binary content named by a placeholder "
                             "([image sha256:HASH ...]) to --output, or to standard output")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="File to write for --get-blob and --get-snapshot (default: standard output)")
    parser.add_argument("--cwd", help="Working directory")

    # Cross-session options
    parser.add_argument("--list-sessions", action="store_true",
                        help="List sessions in project")
    parser.add_argument("--list-projects", action="store_true",
                        help="List all projects")
    parser.add_argument("--session", metavar="ID",
                        help="Session ID to search (prefix match)")
    parser.add_argument("--project", metavar="NAME",
                        help="Project name/path to search (prefix match)")
    parser.add_argument("--all-sessions", action="store_true",
                        help="Search all sessions in project")
    parser.add_argument("--all-projects", action="store_true",
                        help="Search all projects")
    parser.add_argument("--agent", metavar="ID",
                        help="Filter to specific agent by ID prefix")

    args = parser.parse_args()

    # Mode: list projects
    if args.list_projects:
        run_list_projects(args)
        return

    # Mode: list sessions
    if args.list_sessions:
        run_list_sessions(args)
        return

    # Mode: expand UUID
    if args.expand:
        run_expand(args)
        return

    # Mode: get a blob
    if args.get_blob:
        run_get_blob(args)
        return

    # Mode: get snapshot (by backup ID only)
    if args.get_snapshot:
        run_get_snapshot(args)
        return

    # Mode: snapshots
    if args.snapshots is not None:
        run_snapshots(args)
        return

    # Mode: search
    run_search(args)


def _cut(text, limit, full):
    """text, or its first `limit` characters with a note saying how much there is."""
    if full or len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... {limit} of {len(text)} characters shown; --full shows all]"


def _write_out(data, output):
    """Write bytes to the file `output`, or to standard output."""
    if output:
        Path(output).write_bytes(data)
        print(f"Wrote {len(data)} bytes to {output}", file=sys.stderr)
    else:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _scope(args, db=None):
    """What the scope options select: (project name, with its subprojects, session id prefix);
    None where there is no restriction.

    Default: the session this command runs in. --all-sessions: all sessions of its
    project. --project: all sessions of that project. --session: that session,
    whichever project it is in. --all-projects: everything.
    """
    project_name, with_subdirs, session_id = None, False, args.session
    if args.all_projects:
        return None, False, session_id
    if args.project:
        with_subdirs = args.project.endswith("/**")
        project_name = _path_to_project_name(args.project.removesuffix("/**").rstrip("/"))
    elif not args.session:
        project_name, current = _current_project_and_session(args, db)
        if not args.all_sessions:
            session_id = current
    return project_name, with_subdirs, session_id


def _current_project_and_session(args, db=None):
    """(project name, session id) that this command runs in, or that --cwd names.

    A project whose folder Claude Code has deleted is still found if the index has it;
    its newest session then counts as the current one.
    """
    project_dirs = resolve_project_dirs(None, args.cwd)
    if project_dirs:
        return project_dirs[0].name, get_current_session_id(project_dirs[0])
    name = _path_to_project_name(args.cwd or os.getcwd())
    if db is not None:
        row = db.execute("SELECT session_id FROM sessions WHERE project_name = ? "
                         "ORDER BY last_ts DESC LIMIT 1", (name,)).fetchone()
        if row:
            return name, row[0]
    _no_project_found()


def _split_handle(handle):
    """A result's handle "session:agent:uuid", "session:uuid", "agent:uuid" or "uuid"
    -> (prefixes before the uuid, uuid)."""
    parts = handle.split(":")
    return parts[:-1], parts[-1]


def run_expand(args):
    """Expand a single record by UUID prefix. Supports session:agent:uuid format."""
    prefixes, uuid_prefix = _split_handle(args.expand)
    db = open_index()
    if db is not None:
        if len(prefixes) >= 2:
            attempts = [(prefixes[0], prefixes[1])]
        elif len(prefixes) == 1:
            # A session id prefix or an agent id prefix
            attempts = [(prefixes[0], None), (None, prefixes[0])]
        else:
            attempts = [(args.session, args.agent)]
        result, candidates = None, []
        for session_prefix, agent_prefix in attempts:
            result, candidates = expand_sqlite(db, uuid_prefix, session_prefix, agent_prefix)
            if result or candidates:
                break
        if candidates:
            print(f"Several records start with '{args.expand}':", file=sys.stderr)
            for handle in sorted({_handle(_row_to_result(r)) + f" | {utc_time_to_local(r['timestamp'] or '')}"
                                  for r in candidates})[:20]:
                print(f"  {handle}", file=sys.stderr)
            sys.exit(1)
        if result:
            if args.json:
                for row in result["rows"]:
                    print(json.dumps(_json_item(row), ensure_ascii=False))
            else:
                _print_expanded_result(result, full=args.full)
            return
        # Not in the index (yet): look in the transcript files

    # Parse the expand argument which could be:
    # - uuid
    # - session:uuid (from multi-session search)
    # - agent:uuid (from agent search)
    # - session:agent:uuid (from multi-session agent search)
    session_id = None
    agent_id = None
    if len(prefixes) >= 2:
        session_id, agent_id = prefixes[0], prefixes[1]
    elif len(prefixes) == 1:
        # Agent IDs are 7 or 17 characters, session ID prefixes are 8
        if len(prefixes[0]) == 8:
            session_id = prefixes[0]
        else:
            agent_id = prefixes[0]

    # If session_id prefix is given, search all projects for matching session
    if session_id:
        all_project_dirs = resolve_project_dirs(args.project, args.cwd, all_projects=True)
        for project_dir in all_project_dirs:
            # Look for session file matching the prefix
            matching_sessions = [f for f in project_dir.glob("*.jsonl")
                                 if "agent-" not in f.name and f.stem.startswith(session_id)]
            if matching_sessions:
                result = expand_uuid(uuid_prefix, session_files=matching_sessions,
                                     project_dir=project_dir, agent_id=agent_id)
                if result:
                    _print_expanded_result(dict(result, agent_id=agent_id), full=args.full)
                    return
        print(f"No record found with UUID starting with '{args.expand}'")
        return

    # No session prefix - use normal project resolution
    project_dirs = resolve_project_dirs(args.project, args.cwd)
    if not project_dirs:
        if db is not None:
            print(f"No record found with UUID starting with '{args.expand}'")
            return
        _no_project_found(args.project)

    session_files = resolve_session_files(args, project_dirs[0])
    result = expand_uuid(uuid_prefix, session_files=session_files, project_dir=project_dirs[0], agent_id=agent_id)
    if not result:
        print(f"No record found with UUID starting with '{args.expand}'")
        return

    _print_expanded_result(dict(result, agent_id=agent_id), full=args.full)


def _print_expanded_result(result, full=False):
    """Print the expanded record result."""
    sid = f"{result['session_id'][:8]}:" if result.get("session_id") else ""
    agent_prefix = f"{result['agent_id']}:" if result.get("agent_id") else ""
    print(f"== [ {sid}{agent_prefix}{result['uuid']} | {result['timestamp']} | {result['type']} ] ==")
    if result["content"]:
        print(f"\n--- Content ---\n{_cut(result['content'], EXPAND_CUT, full)}")
    if result["thinking"]:
        print(f"\n--- Thinking ---\n{_cut(result['thinking'], EXPAND_CUT, full)}")
    for tu in result["tool_uses"]:
        print(f"\n--- Tool: {tu['name']} ---")
        if tu["input"] is not None:
            print(f"Input:\n{_cut(tu['input'], EXPAND_CUT, full)}")
        if tu["output"]:
            output_str = tu["output"] if isinstance(tu["output"], str) else str(tu["output"])
            label = "Output (error)" if tu.get("is_error") else "Output"
            print(f"{label}:\n{_cut(output_str, EXPAND_CUT, full)}")
        if tu.get("result_json"):
            try:
                keys = ", ".join(k for k, v in json.loads(tu["result_json"]).items() if v is not None)
            except ValueError:
                keys = ""
            print(f"Result details kept ({keys}): add --json to print them")


def run_get_blob(args):
    """Write the binary content that a placeholder in a text names."""
    db = open_index()
    if db is None:
        print("No index: images are kept in the index only", file=sys.stderr)
        sys.exit(1)
    match = PLACEHOLDER_RE.search(args.get_blob)
    digest = match.group(2) if match else args.get_blob.strip("[]")
    blobs = get_blob(db, digest)
    if not blobs:
        print(f"No blob found with a hash starting with '{digest}'", file=sys.stderr)
        sys.exit(1)
    if len(blobs) > 1:
        print(f"Several blobs start with '{digest}':", file=sys.stderr)
        for blob_hash, media_type, data in blobs:
            print(f"  {blob_hash} {media_type} {len(data)} bytes", file=sys.stderr)
        sys.exit(1)
    blob_hash, media_type, data = blobs[0]
    print(f"{blob_hash} {media_type} {len(data)} bytes", file=sys.stderr)
    _write_out(data, args.output)


def _snapshot_bytes(db, snapshot):
    """A snapshot's content as bytes, or None if neither the index nor the disk has it."""
    if snapshot["content"] is not None:
        return snapshot["content"].encode("utf-8", errors="surrogatepass")
    if snapshot["blob_hash"]:
        row = db.execute("SELECT data FROM blobs WHERE hash = ?", (snapshot["blob_hash"],)).fetchone()
        if row:
            return row[0]
    try:
        return (CLAUDE_DIR / "file-history" / snapshot["session_id"] / snapshot["backup_id"]).read_bytes()
    except (OSError, TypeError):
        return None


def run_get_snapshot(args):
    """Get raw content of a snapshot by backup ID.

    IDs look like "7f35193717348549@v1"; the hash part may be given as a
    prefix ("7f3519@v1"), but the version must match exactly if present.
    A session id prefix may come first ("ae6a59bd:7f3519@v1"); without it the
    snapshot is looked for in the current session, then in all sessions.
    """
    prefixes, wanted = _split_handle(args.get_snapshot)
    hash_prefix, _, version = wanted.partition("@")
    db = open_index()
    if db is None:
        _run_get_snapshot_jsonl(args, hash_prefix, version)
        return

    if prefixes:
        scopes = [dict(session_id=prefixes[0])]
    elif args.session or args.project or args.all_projects or args.all_sessions:
        project_name, with_subdirs, session_id = _scope(args, db)
        scopes = [dict(session_id=session_id, project_name=project_name, with_subdirs=with_subdirs)]
    else:
        # The current session if there is one, else anywhere
        scopes = [{}]
        project_dirs = resolve_project_dirs(None, args.cwd)
        if project_dirs:
            current = get_current_session_id(project_dirs[0])
            if current:
                scopes.insert(0, dict(session_id=current))
    matches = []
    for scope in scopes:
        matches = snapshots_sqlite(db, backup_prefix=hash_prefix, version=version or None, **scope)
        if matches:
            break
    if not matches:
        print(f"No snapshot found with ID starting with '{args.get_snapshot}'", file=sys.stderr)
        sys.exit(1)
    contents = {}
    for s in matches:
        contents.setdefault(_snapshot_bytes(db, s), []).append(s)
    if len(contents) > 1:
        # Different files or versions, or the same backup name with other content in another session
        print(f"Multiple matches for '{args.get_snapshot}':", file=sys.stderr)
        for s in matches:
            print(f"  {s['session_id'][:8]}:{s['backup_id']} | {s['timestamp']} | {s['file_path'] or '?'}",
                  file=sys.stderr)
        sys.exit(1)
    data = next(iter(contents))
    if data is None:
        s = matches[0]
        print(f"The content of {s['session_id'][:8]}:{s['backup_id']} ({s['file_path'] or '?'}) is not in the "
              "index: its backup file was gone before the indexer saw it", file=sys.stderr)
        sys.exit(1)
    _write_out(data, args.output)


def _run_get_snapshot_jsonl(args, hash_prefix, version):
    """--get-snapshot without an index: from the current session's transcript and backup files."""
    snapshots = list_snapshots(None, args.cwd)
    if version:
        def id_matches(sid):
            sid_hash, _, sid_version = sid.partition("@")
            return sid_hash.startswith(hash_prefix) and sid_version == version
    else:
        def id_matches(sid):
            return sid.startswith(hash_prefix)
    matches = [s for s in snapshots if id_matches(s['id'])]
    if not matches:
        print(f"No snapshot found with ID starting with '{args.get_snapshot}'", file=sys.stderr)
        sys.exit(1)
    # Deduplicate by backup ID (same file may appear in multiple snapshot records)
    unique = {s['id']: s for s in matches}
    if len(unique) > 1:
        print(f"Multiple matches for '{args.get_snapshot}':", file=sys.stderr)
        for sid in unique:
            print(f"  {sid}", file=sys.stderr)
        sys.exit(1)
    s = next(iter(unique.values()))
    if s["data"] is not None:
        _write_out(s["data"], args.output)


def run_snapshots(args):
    """List and filter file snapshots."""
    time_from = parse_local_time(args.time_from) if args.time_from else None
    time_to = parse_local_time(args.time_to) if args.time_to else None

    if args.time_from and not time_from:
        print(f"Invalid --after time format: {args.time_from}", file=sys.stderr)
        sys.exit(1)
    if args.time_to and not time_to:
        print(f"Invalid --before time format: {args.time_to}", file=sys.stderr)
        sys.exit(1)

    db = open_index()
    if db is not None:
        project_name, with_subdirs, session_id = _scope(args, db)
        rows = snapshots_sqlite(db, session_id=session_id, project_name=project_name,
                                with_subdirs=with_subdirs, filename_filter=args.snapshots or None,
                                msgid=args.msgid, time_from=time_from, time_to=time_to, term=args.term)
        several_sessions = len({r["session_id"] for r in rows}) > 1
        snapshots = []
        for r in rows:
            content = r["content"]
            if content is not None:
                size = f"{r['size'] or 0} bytes"
            elif r["blob_hash"]:
                size = f"{r['size'] or 0} bytes, binary"
            else:
                # Named in the transcript, but the backup file was gone before the indexer saw it
                size = "content not in the index"
            snapshots.append({
                "id": (f"{r['session_id'][:8]}:" if several_sessions else "") + r["backup_id"],
                "msg_id": (r["message_id"] or "")[:12], "timestamp": r["timestamp"],
                "file": r["file_path"] or "?", "content": content, "size": size})
    else:
        snapshots = _snapshots_jsonl(args, time_from, time_to)

    if not snapshots:
        print("No snapshots found")
        return

    for s in snapshots:
        if args.compact:
            print(f"[ {s['id']:20} | {s['msg_id']} | {s['timestamp']} | {s['file']} ({s['size']}) ]")
        else:
            print(f"== [ {s['id']} | {s['msg_id']} | {s['timestamp']} | {s['file']} ({s['size']}) ] ==")
            if s["content"]:
                if args.term:
                    # Show matching lines with context
                    lines = s["content"].splitlines()
                    term_lower = args.term.lower()
                    for i, line in enumerate(lines):
                        if term_lower in line.lower():
                            context = lines[max(0, i-1):i+2]
                            for ctx_line in context:
                                print(f"    {ctx_line[:120]}")
                            break  # Only show first match per file
                else:
                    lines = s["content"].splitlines()[:3]
                    for line in lines:
                        print(f"    {line[:120]}")
            print()


def _snapshots_jsonl(args, time_from, time_to):
    """--snapshots without an index: from the current session's transcript and backup files."""
    snapshots = list_snapshots(
        args.snapshots if args.snapshots else None,
        args.cwd
    )
    # Filter by time range
    if time_from or time_to:
        filtered = []
        for s in snapshots:
            snap_time = utc_to_local_dt(s['timestamp'])
            if snap_time:
                if time_from and snap_time < time_from:
                    continue
                if time_to and snap_time > time_to:
                    continue
            filtered.append(s)
        snapshots = filtered

    # Filter by msgid if provided
    if args.msgid:
        snapshots = [s for s in snapshots if s['msg_id'].startswith(args.msgid)]

    # Filter by content search term if provided
    if args.term:
        snapshots = [s for s in snapshots if s['content'] and args.term.lower() in s['content'].lower()]

    return [dict(s, timestamp=utc_to_local(s['timestamp']), size=f"{s['size']} bytes") for s in snapshots]


def run_search(args):
    """Search conversation history."""
    time_from = parse_local_time(args.time_from) if args.time_from else None
    time_to = parse_local_time(args.time_to) if args.time_to else None

    if args.time_from and not time_from:
        print(f"Invalid --after time format: {args.time_from}")
        return
    if args.time_to and not time_to:
        print(f"Invalid --before time format: {args.time_to}")
        return

    # Expand type pattern to list of matching types
    type_list = expand_type_pattern(args.type) if args.type else None
    if args.type and not type_list:
        print(f"No types match pattern: {args.type}")
        return

    # Compile search pattern (validates regex before either backend runs)
    matcher = compile_search_pattern(args.term, args.ignore_case, args.regex)

    # The index first; without one, the transcript files
    db = open_index()
    if db is not None:
        results = _run_search_sqlite(db, args, type_list, time_from, time_to, matcher)
        if results is not None:
            _display_results(results, args)
            return
        # Fall through to JSONL if SQLite failed

    # Determine which sources to search based on matching types
    search_main = type_list is None or any(t in MAIN_TYPES for t in type_list)
    search_agents = args.agent or (type_list and any(t in AGENT_TYPES for t in type_list))

    # Resolve which sessions to search. Session IDs are unique across projects,
    # so --session without --project looks in all of them.
    project_dirs = resolve_project_dirs(
        args.project, args.cwd,
        all_projects=args.all_projects or bool(args.session and not args.project))
    if not project_dirs:
        _no_project_found(args.project)

    session_files = []
    for pd in project_dirs:
        session_files.extend(resolve_session_files(args, pd))
    if not session_files:
        print("No sessions found")
        return

    all_results = []

    def do_search(history_file, agent_id=None):
        if args.context:
            return search_with_context(
                matcher=matcher,
                type_list=type_list,
                tool=args.tool,
                time_from=time_from,
                time_to=time_to,
                history_file=history_file,
                context_n=args.context,
                agent_id=agent_id,
                origin=args.origin,
            )
        else:
            return search_history(
                matcher=matcher,
                type_list=type_list,
                tool=args.tool,
                time_from=time_from,
                time_to=time_to,
                history_file=history_file,
                agent_id=agent_id,
                origin=args.origin,
            )

    for sf in session_files:
        # Search main session
        if search_main:
            results = do_search(sf)
            for r in results:
                r["session_id"] = sf.stem[:8]
            all_results.extend(results)

        # Search agent files if needed
        if search_agents:
            project_dir = sf.parent
            agent_files = get_agent_files(project_dir, session_id=sf.stem, agent_filter=args.agent)
            for af in agent_files:
                agent_id = af.stem.replace("agent-", "")
                results = do_search(af, agent_id=agent_id)
                for r in results:
                    r["session_id"] = sf.stem[:8]
                all_results.extend(results)

    # Apply first/last after aggregation (count only matches for context mode)
    if args.first or args.last:
        if args.context:
            # Count matches only, but keep their context
            matches = [r for r in all_results if r.get("is_match", True)]
            if args.first:
                matches = matches[:args.first]
            if args.last:
                matches = matches[-args.last:]
            match_uuids = {r["uuid"] for r in matches}
            all_results = [r for r in all_results
                           if r["uuid"] in match_uuids or r.get("context_for") in match_uuids]
        else:
            if args.first:
                all_results = all_results[:args.first]
            if args.last:
                all_results = all_results[-args.last:]

    _display_results(all_results, args)


def _run_search_sqlite(db, args, type_list, time_from, time_to, matcher):
    """Run search using SQLite. Returns results or None to fall back to JSONL."""
    # Default scope: main types only, plus agent types when --agent is given.
    # Agent records and files (plans, memory, ...) require an explicit -t.
    if type_list is None:
        type_list = MAIN_TYPES + (AGENT_TYPES if args.agent else [])

    project_name, with_subdirs, session_id = _scope(args, db)
    results = search_sqlite(
        db,
        term=args.term,
        matcher=matcher,
        is_regex=args.regex,
        type_list=type_list,
        tool=args.tool,
        time_from=time_from,
        time_to=time_to,
        project_name=project_name,
        with_subdirs=with_subdirs,
        session_id=session_id,
        agent_id=args.agent,
        origin=args.origin,
        ignore_case=args.ignore_case,
    )

    if results is None:
        return None  # Fall back to JSONL

    # Apply first/last limits
    if args.first:
        results = results[:args.first]
    if args.last:
        results = results[-args.last:]

    if args.context:
        results = context_sqlite(db, results, args.context)

    return results


def _handle(r):
    """What names a result in a header and for --expand: session:agent:uuid."""
    sid = f"{r['session_id'][:8]}:" if r.get("session_id") else ""
    agent_prefix = f"{r['agent_id']}:" if r.get("agent_id") else ""
    return f"{sid}{agent_prefix}{(r.get('uuid') or '')[:12]}"


def _json_item(r):
    """A result as a JSON object: the index's columns, the stored JSON parsed."""
    item = {key: r.get(key) for key in ("uuid", "session_id", "agent_id", "timestamp", "type", "tool_name",
                                        "tool_use_id", "origin", "model", "source_file")}
    item["content"] = r.get("content") or ""
    if r.get("is_error"):
        item["is_error"] = True
    for key, name in (("input_json", "input"), ("result_json", "result")):
        if r.get(key):
            try:
                item[name] = json.loads(r[key])
            except ValueError:
                item[name] = r[key]
    if "is_match" in r:
        item["is_match"] = r["is_match"]
    return item


def _display_results(results, args):
    """Display search results."""
    if not results:
        print("No results found")
        return

    if args.json:
        for r in results:
            print(json.dumps(_json_item(r), ensure_ascii=False))
        return

    prev_context_for = None
    for r in results:
        marker = (">" if r.get("is_match", True) else " ") if args.context else " "

        # Print separator between context groups
        if args.context and r.get("context_for") != prev_context_for and prev_context_for is not None:
            print("---")
        prev_context_for = r.get("context_for")

        # Type label: with the tool's name, or where a message came from if not from the human
        type_label = r["type"]
        if r.get("tool_name"):
            type_label = f"{r['type']}:{r['tool_name']}"
        elif r.get("origin") and r["origin"] != "human":
            type_label = f"{r['type']}:{r['origin']}"

        content = r.get("content", "") or ""
        timestamp = r.get("timestamp", "")

        if args.compact:
            # One line per item, whatever line breaks the text has
            preview = " ".join(content[:400].split())[:80]
            print(f"[{marker}{_handle(r)} | {timestamp} | {type_label:25} ] {preview}")
        else:
            print(f"=={marker}[ {_handle(r)} | {timestamp} | {type_label} ] ==")
            if content:
                print(_cut(content, LISTING_CUT, args.full))
            print()


def run_list_projects(args):
    """List all projects."""
    db = open_index()
    projects = projects_sqlite(db) if db is not None else list_projects()
    if not projects:
        print("No projects found")
        return
    for name, path, n_sessions, mtime in projects:
        print(f"[ {name[:25]:25} | {n_sessions:2} sess | {mtime} ] {path}")


def run_list_sessions(args):
    """List sessions in a project."""
    db = open_index()
    if db is not None:
        project_name, with_subdirs = None, False
        current_id = None
        if args.project and not args.all_projects:
            with_subdirs = args.project.endswith("/**")
            project_name = _path_to_project_name(args.project.removesuffix("/**").rstrip("/"))
        if not args.cwd:
            current_id = detect_current_session()[1]
        if not args.project and not args.all_projects:
            project_name, newest = _current_project_and_session(args, db)
            current_id = current_id or newest
        sessions = sessions_sqlite(db, project_name, with_subdirs, detailed=not args.compact)
        groups = {}
        for s in sessions:
            groups.setdefault(s["project"], []).append(s)
    else:
        project_dirs = resolve_project_dirs(args.project, args.cwd, all_projects=args.all_projects)
        if not project_dirs:
            _no_project_found(args.project)

        # For current session detection, only check current project
        current_proj = resolve_project_dirs(None, args.cwd)
        current_id = get_current_session_id(current_proj[0]) if current_proj else None
        groups = {pd.name: [dict(s, on_disk=True) for s in list_sessions(pd, detailed=not args.compact)]
                  for pd in project_dirs}

    if not groups:
        print("No sessions found")
        return
    for project, sessions in groups.items():
        if len(groups) > 1:
            print(f"\n{project}:")
        for s in sessions:
            marker = " *" if s["id"] == current_id else "  "
            gone = "" if s["on_disk"] else " (transcript deleted; in the index)"
            title = s.get("title") or ""
            if args.compact:
                print(f"[{marker}{s['id'][:8]} | {s['mtime']} | {s['msgs']:3} msgs ] "
                      f"{(title or s['preview'])[:60]}{gone}")
            else:
                duration = s.get("duration", "")
                files = s.get("files_edited", 0)
                tools = ", ".join(f"{k}:{v}" for k, v in sorted(s.get("tools", {}).items(), key=lambda x: -x[1])[:3])
                print(f"[{marker}{s['id'][:8]} | {s['mtime']} | {s['msgs']:3} msgs | {duration:>12} | {files:2} files ]{gone}")
                if title:
                    print(f"    title: {title}")
                if tools:
                    print(f"    tools: {tools}")
                if s['preview']:
                    print(f"    {s['preview'][:80]}")
                print()


# -----------------------------------------------------------------------------
# Session/project resolution
# -----------------------------------------------------------------------------

_current_session = None  # cache for detect_current_session()


def detect_current_session():
    """Find the session this command runs in, across all projects.

    Claude Code exports the session ID as CLAUDE_CODE_SESSION_ID to the commands
    it runs; subagents get their parent session's ID. Without it (older Claude
    Code versions, or a shell outside Claude Code), look for our own invocation
    in the recently written transcripts.

    Returns (project_dir, session_id) or (None, None) if not found.
    """
    global _current_session
    if _current_session is None:
        _current_session = (_session_from_env() or _session_from_invocation()
                            or (None, None))
    return _current_session


def _session_from_env():
    """(project_dir, session_id) for CLAUDE_CODE_SESSION_ID, or None."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not re.fullmatch(r"[0-9A-Za-z-]+", sid):
        return None
    for project_dir in _project_dirs():
        if (project_dir / f"{sid}.jsonl").is_file():
            return project_dir, sid
    return None


def _session_from_invocation():
    """(project_dir, session_id) of a transcript that recorded this command, or None."""
    my_args = sys.argv[1:]
    now = time.time()

    # Find all recently modified JSONL files across all projects (last 30 sec)
    candidates = []
    for project_dir in _project_dirs():
        for f in project_dir.glob("*.jsonl"):
            if "agent-" in f.name:
                continue
            mtime = _mtime(f)
            if mtime is not None and now - mtime < 30:
                candidates.append((f, mtime, project_dir))

    # Sort by most recent first
    candidates.sort(key=lambda x: -x[1])

    for f, mtime, project_dir in candidates:
        if _find_our_invocation_in_tail(f, my_args):
            age = now - mtime
            print(f"Auto-detected session (JSONL age: {age:.1f}s)", file=sys.stderr)
            return project_dir, f.stem
    return None


def get_current_session_id(project_dir):
    """The session this command runs in if it belongs to project_dir, else the project's newest."""
    detected_dir, sid = detect_current_session()
    if sid and detected_dir.name == project_dir.name:
        return sid
    return _newest_session_id(project_dir)


def _shell_tokens(command):
    """Split a shell command into words and operators (|, &&, ;, >, 2>&1 -> 2 >& 1, ...)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def _is_shell_operator(token):
    return bool(token) and set(token) <= set("();<>|&")


def _command_runs_script(command, script_name, args):
    """Whether a shell command calls script_name with exactly these args.

    The call's arguments end at a pipe, a command separator, a redirection or a
    closing parenthesis, as in `... search_history.py -c --last 5 2>&1 | tail`.
    """
    tokens = _shell_tokens(command)
    n = len(args)
    for i, token in enumerate(tokens):
        if token != script_name and not token.endswith("/" + script_name):
            continue
        if tokens[i + 1:i + 1 + n] != args:
            continue
        rest = tokens[i + 1 + n:]
        if not rest or _is_shell_operator(rest[0]):
            return True
        # A file descriptor number before a redirection (the 2 of 2>&1 or 2>/dev/null)
        if rest[0].isdigit() and len(rest) > 1 and _is_shell_operator(rest[1]) and rest[1][0] in "<>":
            return True
    return False


def _find_our_invocation_in_tail(path, my_args, tail_bytes=100000):
    """Check if this JSONL file's tail contains a Bash call of this script with my_args."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes), 0)
            tail = f.read().decode('utf-8', errors='ignore')
    except OSError:
        return False

    # Find all "command":"..." patterns (Bash tool inputs)
    for match in re.finditer(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', tail):
        try:
            cmd = json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            continue
        if 'search_history.py' in cmd and _command_runs_script(cmd, 'search_history.py', my_args):
            return True
    return False


def _no_project_found(project_arg=None):
    if project_arg:
        print(f"Project not found: {project_arg}", file=sys.stderr)
    else:
        print("No current session or project found: run this from within Claude Code, or pass "
              "--project, --session, --all-projects or --cwd", file=sys.stderr)
    sys.exit(1)


def _mtime(path):
    """Modification time, or None if the path vanished meanwhile."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _by_mtime(paths):
    """Paths sorted newest first, leaving out any that vanished meanwhile."""
    stamped = [(m, p) for p in paths if (m := _mtime(p)) is not None]
    return [p for m, p in sorted(stamped, key=lambda x: x[0], reverse=True)]


def _project_dirs():
    """All project directories (unsorted)."""
    try:
        return [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]
    except OSError:
        return []


def _session_files(project_dir):
    """Main session transcripts of a project (unsorted)."""
    return [f for f in project_dir.glob("*.jsonl") if "agent-" not in f.name]


def _newest_session_id(project_dir):
    files = _by_mtime(_session_files(project_dir))
    return files[0].stem if files else None


def resolve_project_dirs(project_arg, cwd=None, all_projects=False):
    """Resolve project dir(s) from --project, the current session or cwd. Returns list.

    Use /** suffix to include subdirs: /path/to/proj/** matches proj and children.
    Without /**: /path/to/proj matches only that exact project.
    all_projects: if True, return all project directories.
    Default: the project of the session this command runs in; with an explicit
    cwd, or if that session is unknown, the project of the (current) directory.
    """
    if all_projects:
        return _by_mtime(_project_dirs())
    if project_arg:
        include_subdirs = project_arg.endswith("/**")
        path = project_arg.removesuffix("/**").rstrip("/")
        prefix = _path_to_project_name(path)
        if include_subdirs:
            # Match exact or followed by - (path component boundary)
            matches = [d for d in _project_dirs()
                       if d.name == prefix or d.name.startswith(prefix + "-")]
        else:
            matches = [d for d in _project_dirs() if d.name == prefix]
        return sorted(matches, key=lambda d: len(d.name))  # shortest first
    if cwd is None:
        detected_dir, _ = detect_current_session()
        if detected_dir:
            return [detected_dir]
    d = PROJECTS_DIR / _path_to_project_name(cwd or os.getcwd())
    return [d] if d.is_dir() else []


def _path_to_project_name(path):
    """Convert filesystem path to Claude project directory name (as Claude Code
    does: every character other than a letter or digit becomes '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def resolve_session_files(args, project_dir):
    """Get list of session files to search based on args."""
    all_sessions = _session_files(project_dir)
    if args.session:
        # Prefix match on session ID
        return _by_mtime(f for f in all_sessions if f.stem.startswith(args.session))
    # As with the index: a named project, or all projects, means all of their sessions
    if args.all_sessions or args.project or args.all_projects:
        return _by_mtime(all_sessions)
    # Default: current/most recent session only
    sid = get_current_session_id(project_dir)
    f = project_dir / f"{sid}.jsonl" if sid else None
    return [f] if f and f.exists() else []


def get_agent_files(project_dir, session_id=None, agent_filter=None):
    """Get agent files, optionally filtered by session or agent ID prefix."""
    # Older Claude Code versions kept agent files next to the sessions, newer ones in <session>/subagents/
    agent_files = list(project_dir.glob("agent-*.jsonl")) + list(project_dir.glob("*/subagents/agent-*.jsonl"))

    if agent_filter:
        # Filter by agent ID prefix
        agent_files = [f for f in agent_files if f.stem.replace("agent-", "").startswith(agent_filter)]

    if session_id:
        # Filter by session ID (need to read first record to check)
        filtered = []
        for f in agent_files:
            try:
                with open(f) as fh:
                    first_line = fh.readline()
                    if first_line:
                        rec = json.loads(first_line)
                        if rec.get("sessionId", "").startswith(session_id):
                            filtered.append(f)
            except Exception:
                pass
        agent_files = filtered

    return _by_mtime(agent_files)


def list_projects():
    """Return list of (name, path, n_sessions, mtime) tuples."""
    result = []
    for d in _project_dirs():
        sessions = _by_mtime(_session_files(d))
        if not sessions:
            continue
        newest_mtime = _mtime(sessions[0]) or 0
        mtime = datetime.fromtimestamp(newest_mtime).strftime("%Y-%m-%d %H:%M")
        path = "/" + d.name[1:].replace("-", "/")
        result.append((d.name, path, len(sessions), mtime))
    return sorted(result, key=lambda x: x[3], reverse=True)


def list_sessions(project_dir, detailed=False):
    """Return list of session info dicts."""
    result = []
    for f in _session_files(project_dir):
        f_mtime = _mtime(f)
        if f_mtime is None:  # vanished meanwhile
            continue
        mtime = datetime.fromtimestamp(f_mtime).strftime("%Y-%m-%d %H:%M")
        info = _session_summary(f, detailed=detailed)
        info["id"] = f.stem
        info["mtime"] = mtime
        result.append(info)
    return sorted(result, key=lambda x: x["mtime"], reverse=True)


def _session_summary(path, detailed=False):
    """Return session summary dict."""
    n_msgs = 0
    preview = ""
    first_ts = None
    last_ts = None
    tools = {}
    files_edited = set()

    try:
        for line in open(path):
            rec = json.loads(line)
            ts = rec.get("timestamp")

            if rec.get("type") in ("user", "assistant"):
                n_msgs += 1
                if ts:
                    if not first_ts:
                        first_ts = ts
                    last_ts = ts

            if rec.get("type") == "user" and not preview:
                preview = _get_text_content(rec.get("message", {}).get("content", ""))

            if detailed and rec.get("type") == "assistant":
                content = rec.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if item.get("type") == "tool_use":
                            tool_name = item.get("name", "")
                            tools[tool_name] = tools.get(tool_name, 0) + 1
                            if tool_name in ("Edit", "Write"):
                                fp = item.get("input", {}).get("file_path", "")
                                if fp:
                                    files_edited.add(fp)
    except Exception:
        pass

    result = {
        "msgs": n_msgs,
        "preview": preview.replace("\n", " ").strip(),
    }

    if detailed:
        duration = ""
        if first_ts and last_ts:
            try:
                t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                delta = t2 - t1
                hours, rem = divmod(int(delta.total_seconds()), 3600)
                mins = rem // 60
                if hours > 0:
                    duration = f"{hours}h {mins}m"
                else:
                    duration = f"{mins}m"
            except Exception:
                pass
        result["duration"] = duration
        result["tools"] = tools
        result["files_edited"] = len(files_edited)

    return result


def _get_text_content(content):
    """Extract text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if item.get("type") == "text":
                return item.get("text", "")
    return ""


# -----------------------------------------------------------------------------
# Transcript records -> searchable items
# -----------------------------------------------------------------------------

# What stands in the text for binary content kept in the index's blobs table
PLACEHOLDER_RE = re.compile(r"\[(image|document|file) sha256:([0-9a-f]{64}) (\S+) (\d+) bytes\]")

# toolUseResult fields kept with a tool's output: what an edit needs to be replayed or
# undone (the file before the edit and the patch), and where a large output was saved
RESULT_KEYS = ("type", "filePath", "oldString", "newString", "replaceAll", "originalFile",
               "structuredPatch", "userModified", "bashEditDiff", "persistedOutputPath",
               "persistedOutputSize")
RESULT_KEYS_WORTH_KEEPING = ("originalFile", "structuredPatch", "bashEditDiff", "persistedOutputPath")

# Leading tags of user-role text that the human did not type, with the origin they imply
_LEAD_TAG = re.compile(r"\s*<([a-zA-Z_-]+)[\s>]")
_LEAD_ORIGIN = {
    "task-notification": "task-notification",
    "agent-message": "agent",
    "cross-session-message": "agent",
    "system-reminder": None,
}


def blob_placeholder(kind, digest, media_type, size):
    """The text that stands for a blob: its kind, SHA-256, media type and size."""
    return f"[{kind} sha256:{digest} {media_type or 'application/octet-stream'} {size} bytes]"


def _blob_block(block, blob_sink):
    """Placeholder for an image or document block holding base64 data, else None.

    blob_sink(data, media_type) stores the decoded bytes and returns their SHA-256;
    without it the bytes are only hashed.
    """
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data)
    except (binascii.Error, ValueError):
        return None
    media_type = source.get("media_type") or "application/octet-stream"
    digest = blob_sink(raw, media_type) if blob_sink else hashlib.sha256(raw).hexdigest()
    return blob_placeholder(block.get("type") or "file", digest, media_type, len(raw))


def render_blocks(blocks, blob_sink=None):
    """Text of a list of content blocks (a message's content, a tool result, a queued prompt).

    Text blocks are taken as they are; an image or a document becomes a placeholder
    (see _blob_block); anything else is kept as JSON.
    """
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif not isinstance(block, dict):
            parts.append(json.dumps(block, ensure_ascii=False))
        elif block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif block.get("type") in ("image", "document"):
            parts.append(_blob_block(block, blob_sink) or json.dumps(block, ensure_ascii=False))
        elif block.get("type") == "tool_reference":
            parts.append(f"[tool_reference {block.get('tool_name', '')}]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(parts)


# Claude Code's names for origins -> the names used here
_ORIGIN_NAMES = {"peer": "agent", "task_notification": "task-notification",
                 "auto_continuation": "auto-continuation"}


def _origin_name(origin):
    """An origin as Claude Code states it ({"kind": ...} or a name) -> its name here, or None."""
    if isinstance(origin, dict):
        origin = origin.get("kind")
    if not isinstance(origin, str) or not origin:
        return None
    return _ORIGIN_NAMES.get(origin, origin)


def _text_type(agent_id, is_meta, origin, text, turn_origin=None):
    """(record type, origin) of user-role text.

    In the main conversation main_user_text is what the human typed and
    main_meta_text everything else; an agent's transcript has agent_input_text
    (its prompt and the messages sent to it) and agent_meta_text (reminders,
    skill bodies).

    origin: what the message itself states; it decides the type. Without it the
    meta flag and a leading tag decide, and turn_origin (what started the turn
    the message belongs to) only fills in the origin that is returned.
    """
    lead_is_meta = False
    if origin is None:
        m = _LEAD_TAG.match(text)
        if m and m.group(1) in _LEAD_ORIGIN:
            lead_is_meta = True
            origin = _LEAD_ORIGIN[m.group(1)]
    shown = origin if origin is not None else turn_origin
    if agent_id:
        if origin is None and (is_meta or lead_is_meta):
            return "agent_meta_text", shown
        return "agent_input_text", shown
    if origin == "human" or (origin is None and not is_meta and not lead_is_meta):
        return "main_user_text", shown
    return "main_meta_text", shown


def expand_record(rec, records=None, agent_id=None, blob_sink=None, read_persisted=None):
    """Expand a transcript record into searchable items ("virtual records").

    Types: see MAIN_TYPES and AGENT_TYPES. A user or assistant record gives one
    item for its text (text blocks joined, images as placeholders), one for its
    thinking, and one per tool call or tool result. An attachment record of type
    queued_command (a message delivered while Claude was working) gives one text item.

    blob_sink(data, media_type) -> sha256 hex: receives decoded images and documents.
    read_persisted(path) -> text or None: reads a tool output that Claude Code saved
    to <session>/tool-results/ and left only a preview of in the transcript.

    Yields dicts with: type, content, tool_name, tool_use_id, uuid, timestamp (local),
    agent_id, rec (the record), and for the index: legacy_type (the type index
    version 1 gave the item, if it differs), model, is_meta, origin, parent_uuid,
    input_json, result_json, is_error, and persisted_missing (a tool output whose
    saved file could not be read, so that content is only the preview).
    """
    json_type = rec.get("type")
    # Only user and assistant records are messages, plus the attachments that carry
    # one. Others can hold message content too: "api-request-blob" records repeat
    # the API request's messages, without uuid or timestamp.
    if json_type == "attachment":
        attachment = rec.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return
    elif json_type not in ("user", "assistant"):
        return

    uuid = (rec.get("uuid") or "")[:12]
    timestamp = utc_to_local(rec.get("timestamp", ""))
    parent_uuid = (rec.get("parentUuid") or "")[:12] or None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        msg = {}
    model = msg.get("model") if json_type == "assistant" else None
    prefix = "agent_" if agent_id else "main_"

    def make_result(vtype, content, tool_name=None, **extra):
        result = {"type": vtype, "content": content, "tool_name": tool_name, "tool_use_id": "",
                  "uuid": uuid, "timestamp": timestamp, "agent_id": agent_id, "rec": rec,
                  "legacy_type": None, "model": model, "is_meta": False, "origin": None,
                  "parent_uuid": parent_uuid, "input_json": None, "result_json": None,
                  "is_error": False, "persisted_missing": False}
        result.update(extra)
        return result

    def user_text(text, is_meta, origin, legacy=True):
        vtype, origin = _text_type(agent_id, is_meta, origin, text, _origin_name(rec.get("turnOrigin")))
        legacy_type = ("agent_input_text" if agent_id else "main_user_text") if legacy else None
        return make_result(vtype, text, is_meta=is_meta, origin=origin,
                           legacy_type=legacy_type if legacy_type != vtype else None)

    # A message delivered mid-turn
    if json_type == "attachment":
        prompt = attachment.get("prompt")
        text = render_blocks(prompt, blob_sink) if isinstance(prompt, list) else prompt
        if isinstance(text, str) and text.strip():
            origin = _origin_name(attachment.get("origin"))
            if origin is None and attachment.get("commandMode") == "task-notification":
                origin = "task-notification"
            yield user_text(text.strip(), bool(attachment.get("isMeta")), origin, legacy=False)
        return

    # Summary (compaction)
    if rec.get("isCompactSummary"):
        text = msg.get("content", "")
        if isinstance(text, list):
            text = render_blocks(text, blob_sink)
        if isinstance(text, str) and text.strip():
            yield make_result(prefix + "summary", text,
                              legacy_type="main_summary" if agent_id else None)
        return

    content = msg.get("content", "")
    is_meta = bool(rec.get("isMeta"))

    # Simple string content (user message)
    if isinstance(content, str):
        if json_type == "user" and content.strip():
            yield user_text(content, is_meta, _origin_name(rec.get("origin")))
        return

    if not isinstance(content, list):
        return

    blocks = [b for b in content if isinstance(b, dict)]

    # Text, with pasted images and documents in their place
    text = render_blocks([b for b in blocks if b.get("type") in ("text", "image", "document")],
                         blob_sink).strip()
    if text:
        if json_type == "user":
            yield user_text(text, is_meta, _origin_name(rec.get("origin")))
        else:
            yield make_result(prefix + "assistant_text", text)

    # Thinking. Most thinking blocks carry only a signature and no text.
    thinking = "\n".join(t for b in blocks if b.get("type") == "thinking"
                         for t in [(b.get("thinking") or "").strip()] if t)
    if thinking:
        yield make_result(prefix + "assistant_thinking", thinking)

    for block in blocks:
        block_type = block.get("type")

        if block_type == "tool_use":
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})
            yield make_result(prefix + "tool_input", format_tool_input(tool_name, tool_input),
                              tool_name, tool_use_id=block.get("id", ""),
                              input_json=json.dumps(tool_input, ensure_ascii=False))

        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_content = render_blocks(result_content, blob_sink)
            elif not isinstance(result_content, str):
                result_content = "" if result_content is None else json.dumps(result_content,
                                                                             ensure_ascii=False)
            tool_result = rec.get("toolUseResult")
            kept, persisted_missing = None, False
            if isinstance(tool_result, dict):
                if any(tool_result.get(k) is not None for k in RESULT_KEYS_WORTH_KEEPING):
                    kept = {k: tool_result[k] for k in RESULT_KEYS if k in tool_result}
                # A large output: the transcript has a preview, the file has all of it
                saved = tool_result.get("persistedOutputPath")
                if read_persisted and isinstance(saved, str) and saved:
                    full = read_persisted(saved)
                    if full is not None:
                        result_content = full
                    else:
                        persisted_missing = True
            # Try to find the tool name from the matching tool_use
            tool_name = None
            if records:
                tool_name = _find_tool_name_for_result(records, tool_use_id)
            yield make_result(prefix + "tool_output", result_content, tool_name,
                              tool_use_id=tool_use_id, is_error=bool(block.get("is_error")),
                              result_json=json.dumps(kept, ensure_ascii=False) if kept else None,
                              persisted_missing=persisted_missing)


def _find_tool_name_for_result(records, tool_use_id):
    """Find the tool name for a tool_result by matching tool_use_id."""
    for rec in records:
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return block.get("name")
    return None


# -----------------------------------------------------------------------------
# Core operations
# -----------------------------------------------------------------------------

def search_with_context(
    matcher=None,
    type_list=None,
    tool=None,
    time_from=None,
    time_to=None,
    history_file=None,
    context_n=1,
    agent_id=None,
    origin=None,
):
    """Search with N messages of context before/after each match.

    Uses virtual record types via expand_record().
    matcher: function that takes content and returns True if it matches.
    type_list: list of type names to match (from expand_type_pattern).
    agent_id: if set, records are expanded with agent_ prefix types.
    """
    records = load_history_file(history_file) if history_file else []
    if not records:
        return []

    # Expand all records into virtual records
    virtual_records = []
    for rec in records:
        for vr in expand_record(rec, records, agent_id=agent_id):
            virtual_records.append(vr)

    # First pass: find matching indices
    match_indices = set()

    for idx, vr in enumerate(virtual_records):
        if not _vr_matches(vr, matcher, type_list, tool, time_from, time_to, origin):
            continue
        match_indices.add(idx)

    if not match_indices:
        return []

    # Expand to include context
    expanded = set()
    for idx in match_indices:
        for offset in range(-context_n, context_n + 1):
            if 0 <= idx + offset < len(virtual_records):
                expanded.add(idx + offset)

    # Build results
    results = []
    for idx in sorted(expanded):
        vr = virtual_records[idx]
        is_match = idx in match_indices
        # Find the match this context belongs to
        context_for = None
        for m_idx in sorted(match_indices):
            if abs(idx - m_idx) <= context_n:
                context_for = virtual_records[m_idx]["uuid"]
                break

        vr = dict(vr)  # copy
        vr["is_match"] = is_match
        vr["context_for"] = context_for
        results.append(vr)

    return results


def _vr_matches(vr, matcher, type_list, tool, time_from, time_to, origin=None):
    """Check if virtual record matches search criteria."""
    if type_list and vr["type"] not in type_list:
        return False

    if tool and vr.get("tool_name") != tool:
        return False

    if origin and vr.get("origin") != origin:
        return False

    if time_from or time_to:
        rec_time = utc_to_local_dt(vr["rec"].get("timestamp", ""))
        if rec_time:
            if time_from and rec_time < time_from:
                return False
            if time_to and rec_time > time_to:
                return False

    if matcher and not matcher(vr["content"]):
        return False

    return True


def search_history(
    matcher=None,
    type_list=None,
    tool=None,
    time_from=None,
    time_to=None,
    history_file=None,
    cwd=None,
    agent_id=None,
    origin=None,
):
    """Search history with filters using virtual record types.

    matcher: function that takes content and returns True if it matches.
    type_list: list of type names to match (from expand_type_pattern).
    agent_id: if set, records are expanded with agent_ prefix types.
    """
    if history_file:
        records = load_history_file(history_file)
    else:
        records = load_history(cwd)
    if not records:
        return []

    results = []
    for rec in records:
        # Time range filter on original record
        if time_from or time_to:
            rec_time = utc_to_local_dt(rec.get("timestamp", ""))
            if rec_time:
                if time_from and rec_time < time_from:
                    continue
                if time_to and rec_time > time_to:
                    continue

        # Expand into virtual records
        for vr in expand_record(rec, records, agent_id=agent_id):
            # Type filter
            if type_list and vr["type"] not in type_list:
                continue

            # Tool name filter
            if tool and vr.get("tool_name") != tool:
                continue

            if origin and vr.get("origin") != origin:
                continue

            # Content filter
            if matcher and not matcher(vr["content"]):
                continue

            results.append(vr)

    return results


def expand_uuid(uuid_prefix, session_files=None, project_dir=None, agent_id=None):
    """Get full context for a record by UUID prefix.

    session_files: list of session files to search in (for non-agent).
    project_dir: project directory (for agent file lookup).
    agent_id: if provided, search in the agent file instead of main session.
    """
    if project_dir is None:
        project_dirs = resolve_project_dirs(None, None)
        project_dir = project_dirs[0] if project_dirs else None
    if not project_dir:
        return None

    if agent_id:
        # Search in agent file
        agent_files = get_agent_files(project_dir, agent_filter=agent_id)
        if not agent_files:
            return None
        records = load_history_file(agent_files[0])
    else:
        # Search in specified session file(s)
        if not session_files:
            return None
        # Search through all provided session files
        records = []
        for sf in session_files:
            records.extend(load_history_file(sf))

    # Find the record
    target = None
    for rec in records:
        uuid = rec.get("uuid", "")
        if uuid and uuid.startswith(uuid_prefix):
            target = rec
            break

    if not target:
        return None

    result = {
        "uuid": target.get("uuid"),
        "timestamp": utc_to_local(target.get("timestamp", "")),
        "type": target.get("type"),
        "content": None,
        "thinking": None,
        "tool_uses": [],
    }

    msg = target.get("message", {})
    content = msg.get("content", "")

    result["content"] = extract_text(content)
    result["thinking"] = extract_thinking(content)

    for tool_use in extract_tool_uses(content):
        tool_data = {
            "name": tool_use.get("name"),
            "input": format_tool_input(tool_use.get("name", ""), tool_use.get("input", {})),
            "output": find_tool_result(records, tool_use.get("id", "")),
        }
        result["tool_uses"].append(tool_data)

    return result


def list_snapshots(filename_filter=None, cwd=None):
    """List file history snapshots."""
    history_file = get_history_file(cwd)
    if not history_file:
        return []

    # Session ID is the filename without extension
    session_id = history_file.stem
    file_history_dir = CLAUDE_DIR / "file-history" / session_id

    records = load_history(cwd)
    snapshots = []

    for rec in records:
        if rec.get("type") != "file-history-snapshot":
            continue

        snapshot = rec.get("snapshot", {})
        message_id = rec.get("messageId", "")
        backups = snapshot.get("trackedFileBackups", {})
        timestamp = snapshot.get("timestamp", "")

        for filepath, meta in backups.items():
            if filename_filter and filename_filter not in filepath:
                continue

            if not isinstance(meta, dict):
                continue

            backup_filename = meta.get("backupFileName")
            backup_time = meta.get("backupTime", timestamp)

            # Read actual content from backup file
            content = None
            data = None
            size = 0
            if backup_filename and file_history_dir.exists():
                backup_path = file_history_dir / backup_filename
                if backup_path.exists():
                    try:
                        data = backup_path.read_bytes()
                        content = data.decode("utf-8", errors="replace")
                        size = len(data)
                    except OSError:
                        content = "(unable to read)"

            snapshots.append({
                "id": backup_filename or "",
                "msg_id": message_id[:12],
                "timestamp": backup_time,
                "file": filepath,
                "content": content,
                "data": data,
                "size": size,
                "backup_file": backup_filename,
            })

    return snapshots


# -----------------------------------------------------------------------------
# Data loading and extraction
# -----------------------------------------------------------------------------

def load_history(cwd=None):
    """Load all records from history file."""
    history_file = get_history_file(cwd)
    if not history_file:
        return []
    return load_history_file(history_file)


def load_history_file(path):
    """Load all records from a specific history file (none if it vanished)."""
    records = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return records


def get_history_file(cwd=None):
    """Find the current conversation's history file.

    Default: the session this command runs in; with an explicit cwd, or if that
    session is unknown, the newest session of the (current) directory's project.
    """
    if cwd is None:
        project_dir, sid = detect_current_session()
        if sid:
            return project_dir / f"{sid}.jsonl"
        cwd = os.getcwd()
    project_dir = PROJECTS_DIR / _path_to_project_name(cwd)
    files = _by_mtime(_session_files(project_dir))
    return files[0] if files else None


def find_tool_result(records, tool_use_id):
    """Find the tool_result for a given tool_use_id."""
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if item.get("type") == "tool_result" and item.get("tool_use_id") == tool_use_id:
                return item.get("content", "")
    return None


def extract_text(content):
    """Extract text from message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    return ""


def extract_thinking(content):
    """Extract thinking blocks from message content."""
    if not isinstance(content, list):
        return ""
    return "\n".join(item.get("thinking", "") for item in content if item.get("type") == "thinking")


def extract_tool_uses(content):
    """Extract tool uses from message content."""
    if not isinstance(content, list):
        return []
    return [item for item in content if item.get("type") == "tool_use"]


def format_tool_input(tool_name, tool_input):
    """A tool call's input as text to search and read, in full.

    Bash: the command. Read: the path. Write: the path, then the content.
    Edit: the path, then the old text after "-" and the new text after "+".
    Other tools: one "key: value" line per input field, strings as they are.
    The exact input is kept beside this as JSON (input_json in the index).
    """
    if not isinstance(tool_input, dict):
        return tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)

    def field(key):
        value = tool_input.get(key, "")
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    if tool_name == "Bash" and "command" in tool_input:
        return field("command")
    if tool_name == "Read" and "file_path" in tool_input:
        return field("file_path")
    if tool_name == "Write" and "file_path" in tool_input:
        return f"{field('file_path')}\n{field('content')}"
    if tool_name == "Edit" and "file_path" in tool_input:
        return f"{field('file_path')}\n-{field('old_string')}\n+{field('new_string')}"
    return "\n".join(f"{key}: {field(key)}" for key in tool_input)


# -----------------------------------------------------------------------------
# Time utilities
# -----------------------------------------------------------------------------

def utc_to_local(ts):
    """Convert UTC ISO timestamp to local time string."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts[:19] if ts else ""


def utc_index_time(ts):
    """Convert a transcript's ISO timestamp to the index's UTC time string."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TIME_FORMAT)


def mtime_to_utc_time(mtime):
    """Convert a file modification time to the index's UTC time string."""
    return datetime.fromtimestamp(mtime, timezone.utc).strftime(TIME_FORMAT)


def local_to_utc_time(dt):
    """Convert a naive local datetime (as from parse_local_time) to the index's UTC time string."""
    return dt.astimezone(timezone.utc).strftime(TIME_FORMAT)


def utc_time_to_local(s):
    """Convert the index's UTC time string to a local time string."""
    try:
        dt = datetime.strptime(s, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return s
    return dt.astimezone().strftime(TIME_FORMAT)


def utc_to_local_dt(ts):
    """Convert UTC ISO timestamp to local datetime (for comparison)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def parse_local_time(ts):
    """Parse user-provided local time string to datetime.

    Accepts: "10:30", "2025-12-23", "2025-12-23 10:30", "12-23 10:30", etc.
    """
    if not ts:
        return None

    today = datetime.now().date()
    formats = [
        ("%Y-%m-%d %H:%M:%S", False),
        ("%Y-%m-%d %H:%M", False),
        ("%Y-%m-%d", False),
        ("%m-%d %H:%M", True),  # needs year
        ("%m-%d", True),        # needs year
        ("%H:%M:%S", True),     # needs date
        ("%H:%M", True),        # needs date
    ]

    for fmt, needs_context in formats:
        try:
            dt = datetime.strptime(ts, fmt)
            if needs_context:
                if "%Y" not in fmt and "%m" not in fmt:
                    # Time only - use today's date
                    dt = dt.replace(year=today.year, month=today.month, day=today.day)
                elif "%Y" not in fmt:
                    # Month-day only - use current year
                    dt = dt.replace(year=today.year)
            return dt
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader (head, a pager) closed the pipe: stop quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
