#!/usr/bin/env python3
"""Search conversation history with filtering by message type, tool, and scope."""
import argparse
import fnmatch
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# SQLite database path (outside ~/.claude to avoid triggering Claude's watchers)
DB_PATH = Path.home() / ".local/share/claude-history-index/history.db"

# All valid record types
ALL_TYPES = [
    "main_user_text", "main_assistant_text", "main_assistant_thinking",
    "main_tool_input", "main_tool_output", "main_summary",
    "agent_input_text", "agent_assistant_text", "agent_assistant_thinking",
    "agent_tool_input", "agent_tool_output",
    "plan",  # Plan files
]
AGENT_TYPES = [t for t in ALL_TYPES if t.startswith("agent_")]
MAIN_TYPES = [t for t in ALL_TYPES if t.startswith("main_")]


# -----------------------------------------------------------------------------
# Regex to FTS query extraction - handles AND/OR from alternations
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
    """Extract FTS query tree from regex pattern.

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


def _tree_to_fts(node):
    """Convert tree to FTS5 query string."""
    if node is None:
        return None

    if node[0] == 'LIT':
        lit = node[1].replace('"', '""')
        return f'"{lit}"'

    if node[0] == 'AND':
        subs = [_tree_to_fts(n) for n in node[1]]
        subs = [s for s in subs if s]
        if not subs:
            return None
        if len(subs) == 1:
            return subs[0]
        return ' '.join(subs)

    if node[0] == 'OR':
        subs = [_tree_to_fts(n) for n in node[1]]
        subs = [s for s in subs if s]
        if not subs:
            return None
        if len(subs) == 1:
            return subs[0]
        return '(' + ' OR '.join(subs) + ')'

    return None


def _extract_fts_tree_naive(pattern):
    """Fallback: extract literals by splitting on metacharacters."""
    chunks = re.split(r'[\\.*+?^${}()|[\]\s]+', pattern)
    lits = [('LIT', c) for c in chunks if len(c) >= 3]
    if not lits:
        return None
    if len(lits) == 1:
        return lits[0]
    return ('AND', lits)


def _extract_fts_query(pattern):
    """Main entry: regex pattern -> FTS query string or None."""
    tree = _extract_fts_tree(pattern)
    return _tree_to_fts(tree)


def _is_regex(pattern):
    """Check if pattern contains regex metacharacters."""
    return bool(re.search(r'[\\.*+?^${}()|[\]]', pattern))


# -----------------------------------------------------------------------------
# SQLite search functions
# -----------------------------------------------------------------------------

def db_available():
    """Check if SQLite database is available and has data."""
    if not DB_PATH.exists():
        return False
    try:
        db = sqlite3.connect(DB_PATH)
        count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        db.close()
        return count > 0
    except (sqlite3.Error, sqlite3.OperationalError):
        return False


def search_sqlite(
    term=None,
    matcher=None,
    is_regex=True,
    type_list=None,
    tool=None,
    time_from=None,
    time_to=None,
    project_name=None,
    session_id=None,
    agent_id=None,
    ignore_case=False,
):
    """Search records in SQLite.

    Content is prefiltered in SQL with LIKE conditions derived from the term
    (a superset of the real matcher's semantics), then every row is verified
    with `matcher` so results are identical to the JSONL path.
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    params = []
    conditions = []
    sql = "SELECT * FROM records WHERE 1=1"

    # Content prefilter (never excludes true matches; matcher verifies below)
    if term:
        prefilter = _term_to_like(term, is_regex, ignore_case)
        if prefilter:
            like_sql, like_params = prefilter
            conditions.append(like_sql)
            params.extend(like_params)

    # Type filter
    if type_list:
        placeholders = ",".join("?" * len(type_list))
        conditions.append(f"record_type IN ({placeholders})")
        params.extend(type_list)

    # Tool filter
    if tool:
        conditions.append("tool_name = ?")
        params.append(tool)

    # Time filters
    if time_from:
        conditions.append("timestamp >= ?")
        params.append(time_from.strftime("%Y-%m-%d %H:%M:%S"))
    if time_to:
        conditions.append("timestamp <= ?")
        params.append(time_to.strftime("%Y-%m-%d %H:%M:%S"))

    # Project filter
    if project_name:
        conditions.append("project_name = ?")
        params.append(project_name)

    # Session filter
    if session_id:
        conditions.append("(session_id LIKE ? OR session_id = ?)")
        params.extend([f"{session_id}%", session_id])

    # Agent filter
    if agent_id:
        conditions.append("agent_id LIKE ?")
        params.append(f"{agent_id}%")

    # Build final query
    if conditions:
        sql += " AND " + " AND ".join(conditions)

    sql += " ORDER BY timestamp ASC"

    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        print(f"SQLite error: {e}", file=sys.stderr)
        db.close()
        return None  # Signal to fall back to JSONL

    db.close()

    # Convert to expected format, verifying content against the real matcher
    results = []
    for row in rows:
        content = row["content"] or ""
        if matcher and not matcher(content):
            continue
        results.append({
            "uuid": row["uuid"] or "",
            "timestamp": row["timestamp"] or "",
            "type": row["record_type"],
            "tool_name": row["tool_name"],
            "content": content,
            "agent_id": row["agent_id"],
            "session_id": row["session_id"] or row["project_name"],
        })

    return results


def _term_to_like(term, is_regex, ignore_case):
    """Build a SQL LIKE prefilter for a search term, or None for no prefilter.

    The prefilter must be a superset of the matcher: LIKE is substring-based
    (unlike token-based FTS) and ASCII-case-insensitive, so it can only ever
    over-match. Non-ASCII terms with -i get no prefilter (LIKE is
    case-sensitive beyond ASCII, which could drop true matches).
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


def search_snapshots_sqlite(
    term=None,
    filename_filter=None,
    time_from=None,
    time_to=None,
    session_id=None,
):
    """Search snapshots using SQLite."""
    if not DB_PATH.exists():
        return None

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    params = []
    conditions = []

    if term:
        if _is_regex(term):
            fts_query = _extract_fts_query(term)
        else:
            fts_query = f'"{term.replace(chr(34), chr(34)+chr(34))}"'

        if fts_query:
            sql = """
                SELECT s.* FROM snapshots s
                JOIN snapshots_fts ON s.id = snapshots_fts.rowid
                WHERE snapshots_fts MATCH ?
            """
            params.append(fts_query)
        else:
            sql = "SELECT * FROM snapshots WHERE 1=1"
    else:
        sql = "SELECT * FROM snapshots WHERE 1=1"

    if filename_filter:
        conditions.append("(file_path LIKE ? OR backup_id LIKE ?)")
        params.extend([f"%{filename_filter}%", f"%{filename_filter}%"])

    if time_from:
        conditions.append("timestamp >= ?")
        params.append(time_from.strftime("%Y-%m-%d %H:%M:%S"))
    if time_to:
        conditions.append("timestamp <= ?")
        params.append(time_to.strftime("%Y-%m-%d %H:%M:%S"))

    if session_id:
        conditions.append("session_id LIKE ?")
        params.append(f"{session_id}%")

    if conditions:
        sql += " AND " + " AND ".join(conditions)

    sql += " ORDER BY timestamp ASC"

    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.Error:
        db.close()
        return None

    db.close()

    return [dict(row) for row in rows]


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

PROJECTS_DIR = Path.home() / ".claude/projects"


def main():
    parser = argparse.ArgumentParser(description="Search conversation history")
    parser.add_argument("term", nargs="?", help="Search term")
    parser.add_argument("-t", "--type",
                        help="Record type pattern (supports * wildcard). Types: main_user_text, "
                             "main_assistant_text, main_assistant_thinking, main_tool_input, main_tool_output, "
                             "main_summary, agent_input_text, agent_assistant_text, agent_assistant_thinking, "
                             "agent_tool_input, agent_tool_output. Examples: main_*, *_text, *_assistant_*")
    parser.add_argument("--tool", help="Tool name filter (Bash, Read, Write, Edit, etc.)")
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
    parser.add_argument("--after", dest="time_from", metavar="TIME",
                        help="After time (local), e.g. '2025-12-23 10:30:00' (same format as header), or shorter: 10:30, 12-23 10:30")
    parser.add_argument("--before", dest="time_to", metavar="TIME",
                        help="Before time (local), e.g. '2025-12-23 10:30:00' (same format as header), or shorter: 10:30, 12-23 10:30")
    parser.add_argument("--expand", metavar="UUID", help="Get full context for a UUID")
    parser.add_argument("-C", "--context", type=int, metavar="N",
                        help="Show N messages before and after each match")
    parser.add_argument("--snapshots", nargs="?", const="", metavar="FILENAME",
                        help="List file snapshots (optionally filter by filename)")
    parser.add_argument("--get-snapshot", metavar="ID",
                        help="Get file content of a snapshot by backup ID (prefix match)")
    parser.add_argument("--msgid", metavar="MSGID",
                        help="Filter snapshots by message ID (prefix match)")
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


def run_expand(args):
    """Expand a single record by UUID prefix. Supports session:agent:uuid format."""
    # Parse the expand argument which could be:
    # - uuid
    # - session:uuid (from multi-session search)
    # - agent:uuid (from agent search)
    # - session:agent:uuid (from multi-session agent search)
    session_id = None
    agent_id = None
    uuid_prefix = args.expand

    parts = args.expand.split(":")
    if len(parts) == 3:
        # session:agent:uuid
        session_id, agent_id, uuid_prefix = parts
    elif len(parts) == 2:
        # Could be session:uuid or agent:uuid
        # Agent IDs are 7 chars, session IDs are 8 chars
        prefix, uuid_prefix = parts
        if len(prefix) == 8:
            session_id = prefix
        else:
            agent_id = prefix
    # else: just uuid

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
                    _print_expanded_result(result, agent_id)
                    return
        print(f"No record found with UUID starting with '{args.expand}'")
        return

    # No session prefix - use normal project resolution
    project_dirs = resolve_project_dirs(args.project, args.cwd)
    if not project_dirs:
        print("Project not found", file=sys.stderr)
        return

    session_files = resolve_session_files(args, project_dirs[0])
    result = expand_uuid(uuid_prefix, session_files=session_files, project_dir=project_dirs[0], agent_id=agent_id)
    if not result:
        print(f"No record found with UUID starting with '{args.expand}'")
        return

    _print_expanded_result(result, agent_id)


def _print_expanded_result(result, agent_id=None):
    """Print the expanded record result."""
    agent_prefix = f"{agent_id}:" if agent_id else ""
    print(f"== [ {agent_prefix}{result['uuid']} | {result['timestamp']} | {result['type']} ] ==")
    if result["content"]:
        print(f"\n--- Content ---\n{result['content']}")
    if result["thinking"]:
        print(f"\n--- Thinking ---\n{result['thinking']}")
    for tu in result["tool_uses"]:
        print(f"\n--- Tool: {tu['name']} ---")
        print(f"Input:\n{tu['input']}")
        if tu["output"]:
            output_str = tu["output"] if isinstance(tu["output"], str) else str(tu["output"])
            print(f"Output:\n{output_str}")


def run_get_snapshot(args):
    """Get raw content of a snapshot by backup ID.

    IDs look like "7f35193717348549@v1"; the hash part may be given as a
    prefix ("7f3519@v1"), but the version must match exactly if present.
    """
    snapshots = list_snapshots(None, args.cwd)
    if "@" in args.get_snapshot:
        hash_prefix, _, version = args.get_snapshot.partition("@")
        def id_matches(sid):
            sid_hash, _, sid_version = sid.partition("@")
            return sid_hash.startswith(hash_prefix) and sid_version == version
    else:
        def id_matches(sid):
            return sid.startswith(args.get_snapshot)
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
    if s["content"]:
        print(s["content"])


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

    if not snapshots:
        print("No snapshots found")
        return

    for s in snapshots:
        if args.compact:
            print(f"[ {s['id']:20} | {s['msg_id']} | {utc_to_local(s['timestamp'])} | {s['file']} ({s['size']} bytes) ]")
        else:
            print(f"== [ {s['id']} | {s['msg_id']} | {utc_to_local(s['timestamp'])} | {s['file']} ({s['size']} bytes) ] ==")
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

    # Try SQLite first (unless context mode, which needs JSONL for now)
    if not args.context and db_available():
        results = _run_search_sqlite(args, type_list, time_from, time_to, matcher)
        if results is not None:
            _display_results(results, args)
            return
        # Fall through to JSONL if SQLite failed

    # Determine which sources to search based on matching types
    search_main = type_list is None or any(t in MAIN_TYPES for t in type_list)
    search_agents = args.agent or (type_list and any(t in AGENT_TYPES for t in type_list))

    # Resolve which sessions to search
    project_dirs = resolve_project_dirs(args.project, args.cwd, all_projects=args.all_projects)
    if not project_dirs:
        print("Project not found", file=sys.stderr)
        sys.exit(1)

    session_files = []
    for pd in project_dirs:
        session_files.extend(resolve_session_files(args, pd))
    if not session_files:
        print("No sessions found")
        return

    multi_session = len(session_files) > 1 or len(project_dirs) > 1

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

    if not all_results:
        print("No results found")
        return

    prev_context_for = None
    for r in all_results:
        sid = f"{r['session_id']}:" if multi_session else ""
        # Show agent ID prefix for agent results
        agent_prefix = f"{r['agent_id']}:" if r.get("agent_id") else ""
        is_match = r.get("is_match", True)
        marker = ">" if is_match and args.context else " "

        # Print separator between context groups
        if args.context and r.get("context_for") != prev_context_for and prev_context_for is not None:
            print("---")
        prev_context_for = r.get("context_for")

        # Format type label for display
        type_label = r["type"]
        if r.get("tool_name"):
            type_label = f"{r['type']}:{r['tool_name']}"

        content = r.get("content", "") or ""

        if args.compact:
            preview = content.replace("\n", " ").strip()[:80]
            print(f"[{marker}{sid}{agent_prefix}{r['uuid']} | {r['timestamp']} | {type_label:25} ] {preview}")
        else:
            print(f"=={marker}[ {sid}{agent_prefix}{r['uuid']} | {r['timestamp']} | {type_label} ] ==")
            if content:
                print(content[:2000])
            print()


def _run_search_sqlite(args, type_list, time_from, time_to, matcher):
    """Run search using SQLite. Returns results or None to fall back to JSONL."""
    # Match the JSONL path's default scope: main types only, plus agent types
    # when --agent is given. Agent/plan records require an explicit -t.
    if type_list is None:
        type_list = MAIN_TYPES + (AGENT_TYPES if args.agent else [])

    # Determine project filter
    project_name = None
    session_id = args.session if hasattr(args, 'session') and args.session else None

    if args.project:
        project_name = _path_to_project_name(args.project.removesuffix("/**").rstrip("/"))
    elif not args.all_projects:
        # Try to auto-detect current session across all projects (CWD-independent)
        detected_proj, detected_session = detect_current_session()
        if detected_proj:
            project_name = detected_proj.name
            if not session_id and not args.all_sessions:
                session_id = detected_session
        else:
            # Fall back to CWD-based project detection
            cwd = args.cwd or os.getcwd()
            project_name = _path_to_project_name(cwd)

    results = search_sqlite(
        term=args.term,
        matcher=matcher,
        is_regex=args.regex,
        type_list=type_list,
        tool=args.tool,
        time_from=time_from,
        time_to=time_to,
        project_name=project_name if not args.all_projects else None,
        session_id=session_id,
        agent_id=args.agent if hasattr(args, 'agent') else None,
        ignore_case=args.ignore_case,
    )

    if results is None:
        return None  # Fall back to JSONL

    # Apply first/last limits
    if args.first:
        results = results[:args.first]
    if args.last:
        results = results[-args.last:]

    return results


def _display_results(results, args, multi_session=True):
    """Display search results."""
    if not results:
        print("No results found")
        return

    for r in results:
        sid = f"{r.get('session_id', '')[:8]}:" if multi_session and r.get('session_id') else ""
        agent_prefix = f"{r['agent_id']}:" if r.get("agent_id") else ""

        type_label = r["type"]
        if r.get("tool_name"):
            type_label = f"{r['type']}:{r['tool_name']}"

        content = r.get("content", "") or ""
        uuid = r.get("uuid", "")[:12]
        timestamp = r.get("timestamp", "")

        if args.compact:
            preview = content.replace("\n", " ").strip()[:80]
            print(f"[ {sid}{agent_prefix}{uuid} | {timestamp} | {type_label:25} ] {preview}")
        else:
            print(f"== [ {sid}{agent_prefix}{uuid} | {timestamp} | {type_label} ] ==")
            if content:
                print(content[:2000])
            print()


def run_list_projects(args):
    """List all projects."""
    projects = list_projects()
    if not projects:
        print("No projects found")
        return
    for name, path, n_sessions, mtime in projects:
        print(f"[ {name[:25]:25} | {n_sessions:2} sess | {mtime} ] {path}")


def run_list_sessions(args):
    """List sessions in a project."""
    project_dirs = resolve_project_dirs(args.project, args.cwd, all_projects=args.all_projects)
    if not project_dirs:
        print("Project not found", file=sys.stderr)
        sys.exit(1)

    # For current session detection, only check current project
    current_proj = resolve_project_dirs(None, args.cwd)
    current_id = get_current_session_id(current_proj[0]) if current_proj else None

    for pd in project_dirs:
        if len(project_dirs) > 1:
            print(f"\n{pd.name}:")
        for s in list_sessions(pd, detailed=not args.compact):
            marker = " *" if s["id"] == current_id else "  "
            if args.compact:
                print(f"[{marker}{s['id'][:8]} | {s['mtime']} | {s['msgs']:3} msgs ] {s['preview'][:60]}")
            else:
                duration = s.get("duration", "")
                files = s.get("files_edited", 0)
                tools = ", ".join(f"{k}:{v}" for k, v in sorted(s.get("tools", {}).items(), key=lambda x: -x[1])[:3])
                print(f"[{marker}{s['id'][:8]} | {s['mtime']} | {s['msgs']:3} msgs | {duration:>12} | {files:2} files ]")
                if tools:
                    print(f"    tools: {tools}")
                if s['preview']:
                    print(f"    {s['preview'][:80]}")
                print()


# -----------------------------------------------------------------------------
# Session/project resolution
# -----------------------------------------------------------------------------

def detect_current_session():
    """Find current session across ALL projects by locating our own invocation.

    Returns (project_dir, session_id) or (None, None) if not found.
    """
    my_args = sys.argv[1:] if len(sys.argv) > 1 else []
    now = time.time()

    # Find all recently modified JSONL files across all projects (last 30 sec)
    candidates = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.jsonl"):
            if "agent-" in f.name:
                continue
            try:
                mtime = f.stat().st_mtime
                if now - mtime < 30:
                    candidates.append((f, mtime, project_dir))
            except OSError:
                continue

    # Sort by most recent first
    candidates.sort(key=lambda x: -x[1])

    # Check each for our own invocation using shlex-based parsing
    for f, mtime, project_dir in candidates:
        if _find_our_invocation_in_tail(f, my_args):
            age = now - mtime
            print(f"Auto-detected session (JSONL age: {age:.1f}s)", file=sys.stderr)
            return project_dir, f.stem

    return None, None


def get_current_session_id(project_dir):
    """Find current session by locating our own invocation, fallback to mtime."""
    my_cmd = " ".join(sys.argv)
    now = time.time()
    recent = [(f, f.stat().st_mtime) for f in project_dir.glob("*.jsonl")
              if "agent-" not in f.name and now - f.stat().st_mtime < 300]
    recent.sort(key=lambda x: -x[1])

    for f, _ in recent:
        if _cmd_in_tail(f, my_cmd):
            return f.stem
    return recent[0][0].stem if recent else _newest_session_id(project_dir)


def _cmd_in_tail(path, cmd, tail_bytes=100000):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail_bytes), 0)
            return cmd in f.read().decode('utf-8', errors='ignore')
    except Exception:
        return False


def _extract_script_args(bash_cmd, script_name):
    """Extract args for a script from a bash command using shlex.

    Given a bash command like:
        cd /tmp && uv run ~/.../search_history.py "term" -t foo 2>&1
    Returns ['term', '-t', 'foo'] if script_name is 'search_history.py'
    """
    import shlex
    import re
    try:
        tokens = shlex.split(bash_cmd)
    except ValueError:
        return None

    # Find the script in tokens
    for i, tok in enumerate(tokens):
        if tok.endswith(script_name) or tok == script_name:
            # Return everything after the script, filtering shell redirections
            args = tokens[i + 1:]
            # Filter out shell redirections like 2>&1, >/dev/null, etc.
            args = [a for a in args if not re.match(r'^[0-9]*[<>]', a)]
            return args
    return None


def _find_our_invocation_in_tail(path, my_args, tail_bytes=100000):
    """Check if this JSONL file's tail contains our invocation.

    Parses Bash commands and compares extracted args with my_args.
    """
    import json
    import re

    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes), 0)
            tail = f.read().decode('utf-8', errors='ignore')
    except Exception:
        return False

    # Find all "command":"..." patterns (Bash tool inputs)
    for match in re.finditer(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', tail):
        cmd_escaped = match.group(1)
        # Unescape JSON string
        try:
            cmd = json.loads(f'"{cmd_escaped}"')
        except json.JSONDecodeError:
            continue

        if 'search_history.py' not in cmd:
            continue

        extracted = _extract_script_args(cmd, 'search_history.py')
        if extracted == my_args:
            return True

    return False


def _newest_session_id(project_dir):
    files = [f for f in project_dir.glob("*.jsonl") if "agent-" not in f.name]
    return max(files, key=os.path.getmtime).stem if files else None


def resolve_project_dirs(project_arg, cwd=None, all_projects=False):
    """Resolve project dir(s) from --project or cwd. Returns list.

    Use /** suffix to include subdirs: /path/to/proj/** matches proj and children.
    Without /**: /path/to/proj matches only that exact project.
    all_projects: if True, return all project directories.
    """
    if all_projects:
        return sorted([d for d in PROJECTS_DIR.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)
    if project_arg:
        include_subdirs = project_arg.endswith("/**")
        path = project_arg.removesuffix("/**").rstrip("/")
        prefix = _path_to_project_name(path)
        if include_subdirs:
            # Match exact or followed by - (path component boundary)
            matches = [d for d in PROJECTS_DIR.iterdir()
                       if d.is_dir() and (d.name == prefix or d.name.startswith(prefix + "-"))]
        else:
            matches = [d for d in PROJECTS_DIR.iterdir()
                       if d.is_dir() and d.name == prefix]
        return sorted(matches, key=lambda d: len(d.name))  # shortest first
    # Try cwd first
    cwd = cwd or os.getcwd()
    name = _path_to_project_name(cwd)
    d = PROJECTS_DIR / name
    if d.exists():
        return [d]
    # cwd didn't match - search all projects for our session
    found = _find_project_by_session()
    return [found] if found else []


def _find_project_by_session():
    """Search all projects for a recent session containing our invocation."""
    my_cmd = " ".join(sys.argv)
    now = time.time()
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.jsonl"):
            if "agent-" in f.name:
                continue
            if now - f.stat().st_mtime > 300:
                continue
            if _cmd_in_tail(f, my_cmd):
                return project_dir
    return None


def _path_to_project_name(path):
    """Convert filesystem path to Claude project directory name."""
    return "-" + path.lstrip("/").replace("/", "-").replace(".", "-").replace("_", "-")


def resolve_session_files(args, project_dir):
    """Get list of session files to search based on args."""
    all_sessions = [f for f in project_dir.glob("*.jsonl") if "agent-" not in f.name]
    if args.all_sessions:
        return sorted(all_sessions, key=os.path.getmtime, reverse=True)
    if args.session:
        # Prefix match on session ID
        return sorted([f for f in all_sessions if f.stem.startswith(args.session)],
                      key=os.path.getmtime, reverse=True)
    # Default: current/most recent session only
    sid = get_current_session_id(project_dir)
    f = project_dir / f"{sid}.jsonl" if sid else None
    return [f] if f and f.exists() else []


def get_agent_files(project_dir, session_id=None, agent_filter=None):
    """Get agent files, optionally filtered by session or agent ID prefix."""
    agent_files = list(project_dir.glob("agent-*.jsonl"))

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

    return sorted(agent_files, key=os.path.getmtime, reverse=True)


def list_projects():
    """Return list of (name, path, n_sessions, mtime) tuples."""
    result = []
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        sessions = [f for f in d.glob("*.jsonl") if "agent-" not in f.name]
        if not sessions:
            continue
        newest = max(sessions, key=os.path.getmtime)
        mtime = datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        path = "/" + d.name[1:].replace("-", "/")
        result.append((d.name, path, len(sessions), mtime))
    return sorted(result, key=lambda x: x[3], reverse=True)


def list_sessions(project_dir, detailed=False):
    """Return list of session info dicts."""
    result = []
    for f in project_dir.glob("*.jsonl"):
        if "agent-" in f.name:
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
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


def expand_record(rec, records=None, agent_id=None):
    """Expand a JSON record into virtual records.

    Main types: main_user_text, main_assistant_text, main_assistant_thinking,
                main_tool_input, main_tool_output, main_summary
    Agent types: agent_input_text, agent_assistant_text, agent_assistant_thinking,
                 agent_tool_input, agent_tool_output

    Yields dicts with: type, content, tool_name, uuid, timestamp, agent_id, and original rec reference.
    """
    uuid = rec.get("uuid", "")[:12]
    timestamp = utc_to_local(rec.get("timestamp", ""))
    json_type = rec.get("type")

    def make_type(base_type):
        """Convert base type to full type name."""
        if agent_id:
            return {
                "user_text": "agent_input_text",
                "assistant_text": "agent_assistant_text",
                "assistant_thinking": "agent_assistant_thinking",
                "tool_input": "agent_tool_input",
                "tool_output": "agent_tool_output",
            }.get(base_type, f"agent_{base_type}")
        return f"main_{base_type}"

    def make_result(vtype, content, tool_name=None, **extra):
        return {"type": vtype, "content": content, "tool_name": tool_name,
                "uuid": uuid, "timestamp": timestamp, "agent_id": agent_id, "rec": rec, **extra}

    # Summary (compaction) - not in agents
    if rec.get("isCompactSummary"):
        text = rec.get("message", {}).get("content", "")
        if isinstance(text, str) and text.strip():
            yield make_result("main_summary", text)
        return

    msg = rec.get("message", {})
    content = msg.get("content", "")

    # Simple string content (user message)
    if isinstance(content, str):
        if json_type == "user" and content.strip():
            yield make_result(make_type("user_text"), content)
        return

    if not isinstance(content, list):
        return

    # Process content blocks
    for block in content:
        block_type = block.get("type")

        if block_type == "text" and json_type == "user":
            text = block.get("text", "").strip()
            if text:
                yield make_result(make_type("user_text"), text)

        elif block_type == "text" and json_type == "assistant":
            text = block.get("text", "").strip()
            if text:
                yield make_result(make_type("assistant_text"), text)

        elif block_type == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking:
                yield make_result(make_type("assistant_thinking"), thinking)

        elif block_type == "tool_use":
            tool_name = block.get("name", "")
            tool_input_str = format_tool_input(tool_name, block.get("input", {}))
            tool_use_id = block.get("id", "")
            yield make_result(make_type("tool_input"), tool_input_str, tool_name, tool_use_id=tool_use_id)

        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_content = "\n".join(str(x.get("text", x)) for x in result_content)
            # Try to find the tool name from the matching tool_use
            tool_name = None
            if records:
                tool_name = _find_tool_name_for_result(records, tool_use_id)
            yield make_result(make_type("tool_output"), str(result_content), tool_name, tool_use_id=tool_use_id)


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
        if not _vr_matches(vr, matcher, type_list, tool, time_from, time_to):
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


def _vr_matches(vr, matcher, type_list, tool, time_from, time_to):
    """Check if virtual record matches search criteria."""
    if type_list and vr["type"] not in type_list:
        return False

    if tool and vr.get("tool_name") != tool:
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
    file_history_dir = Path.home() / ".claude/file-history" / session_id

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
            size = 0
            if backup_filename and file_history_dir.exists():
                backup_path = file_history_dir / backup_filename
                if backup_path.exists():
                    try:
                        content = backup_path.read_text()
                        size = len(content)
                    except Exception:
                        content = "(unable to read)"

            snapshots.append({
                "id": backup_filename or "",
                "msg_id": message_id[:12],
                "timestamp": backup_time,
                "file": filepath,
                "content": content,
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
    """Load all records from a specific history file."""
    records = []
    with open(path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def get_history_file(cwd=None):
    """Find the current conversation's history file."""
    if cwd is None:
        cwd = os.getcwd()
    project_name = _path_to_project_name(cwd)
    project_dir = Path.home() / ".claude/projects" / project_name

    files = sorted(project_dir.glob("*.jsonl"), key=os.path.getmtime, reverse=True)
    files = [f for f in files if "agent-" not in f.name]
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
    """Format tool input for display."""
    if tool_name == "Bash":
        return tool_input.get("command", "")
    elif tool_name in ("Read", "Glob", "Grep"):
        return tool_input.get("file_path", "") or tool_input.get("pattern", "") or tool_input.get("path", "")
    elif tool_name == "Write":
        return f"{tool_input.get('file_path', '')}\n{tool_input.get('content', '')[:500]}"
    elif tool_name == "Edit":
        return f"{tool_input.get('file_path', '')}\n-{tool_input.get('old_string', '')[:200]}\n+{tool_input.get('new_string', '')[:200]}"
    else:
        return json.dumps(tool_input)[:500]


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
    main()
