---
name: search-history
description: Search conversation history that got lost due to context compaction esp. to find exact code or commands from earlier. Only use when user explicitly asks to check history, or when you need very specific info that is no longer in context. (user)
allowed-tools: Bash
---

# Search Conversation History

Search through conversation history to recover code, commands, or context that was lost to compaction.

Searches are backed by a constantly-updating SQLite database with full-text indexing, so speed is not a concern. However, scope searches appropriately for relevance:
- Something from earlier in this session (before compaction): search current session only (default)
- Something from a prior session in same project: use `--all-sessions`
- User forgot where something was discussed: search all projects with `--all-projects`

## When to use

- User explicitly asks to "check history" or insists you recall something from earlier in exact way
- You need exact code/commands that were summarized away
- You're certain that crucial specific info existed earlier but is no longer in context
- User asks about something from a previous conversation in same or different project

## When NOT to use

- General exploration or curiosity
- Information obtainable by reading current files
- Information the user can easily provide

## Typical Workflows

### 1. Find something from earlier today

```bash
# Get a quick overview of the session
search_history.py -c --after "09:00"

# Narrow down to a time window
search_history.py -c --after "10:00" --before "11:00"

# Expand a specific entry to see full details
search_history.py --expand abc123
```

### 2. Recover lost code or command

```bash
# Search for a keyword in tool inputs
search_history.py "pytest" -t main_tool_input --tool Bash

# Search in tool outputs
search_history.py "error" -t '*_tool_output'

# Show context: 2 messages before and after each match
search_history.py "pytest" -C 2

# Found it? Expand to get the full content
search_history.py --expand def456
```

### 3. Find what was discussed about a topic

```bash
# Search user messages
search_history.py -t main_user_text "authentication"

# Search assistant responses
search_history.py -t '*_assistant_text' "authentication"

# Search thinking to see reasoning
search_history.py -t '*_thinking' "authentication"
```

### 4. Review file changes

```bash
# List all file snapshots (with preview)
search_history.py --snapshots

# Compact listing
search_history.py --snapshots -c

# Filter by time, file, search content
search_history.py --snapshots --after "08:00" "config.py"

# Get specific version
search_history.py --get-snapshot "7f3519"
```

### 5. Search across sessions/projects

By default, searches one session per project:
- Current project: the active session (detected via self-lookup in JSONL)
- Other projects: most recently modified session

Use `--all-sessions` to search all sessions.

```bash
# List sessions in current project (* marks current)
search_history.py --list-sessions

# List all projects
search_history.py --list-projects

# Search all sessions in current project
search_history.py "term" --all-sessions

# Search all projects (when you forgot where something was discussed)
search_history.py "term" --all-projects

# Search in a different project (exact path, most recent session only)
search_history.py "term" --project /home/user/myproject

# Include subdirectory projects with /** (only this glob pattern supported)
search_history.py "term" --project /home/user/myproject/**

# All sessions in all matched projects
search_history.py "term" --project /home/user/myproject/** --all-sessions

# Search specific session by ID prefix
search_history.py "term" --session ae6a59bd
```

## Usage

```bash
python ~/.claude/skills/search-history/search_history.py [OPTIONS] [TERM]
```

## Search

```bash
# Basic search across all record types
search_history.py "pytest"

# Filter by record type (supports * wildcard)
search_history.py -t main_user_text "feature"           # User messages
search_history.py -t main_assistant_text "error"        # Assistant text
search_history.py -t main_assistant_thinking "strategy" # Thinking blocks
search_history.py -t main_tool_input "pytest"           # Tool calls
search_history.py -t main_tool_output "error"           # Tool results
search_history.py -t main_summary "implemented"         # Compaction summaries

# Wildcard patterns
search_history.py -t 'main_*' "error"                  # All main session types
search_history.py -t '*_text' "feature"                # All text (user + assistant)
search_history.py -t '*_tool_*' "pytest"               # All tool input/output
search_history.py -t '*_assistant_*' "strategy"        # All assistant (text + thinking)

# Filter by tool (for *_tool_* types)
search_history.py -t main_tool_input --tool Bash "pytest"
search_history.py -t '*_tool_output' --tool Edit "error"

# Case-insensitive search (-i)
search_history.py -i "error"                   # Matches ERROR, Error, error

# Regex is on by default
search_history.py 'def \w+\('                  # Function definitions
search_history.py -i 'error|warning'           # Regex + case-insensitive

# Literal search (-L) disables regex
search_history.py -L "file.txt"                # Searches for literal "file.txt" (dot not wildcard)

# Limit results
search_history.py --first 1
search_history.py --last 5

# Time-based filtering (local time, same format as output headers)
search_history.py --after "08:00"
search_history.py --before "2025-12-23 10:30:00"
search_history.py --after "12-23 08:00" --before "12-23 18:00"

# Compact one-line listing
search_history.py -c --last 20
```

## Searching Agents

When Claude spawns subagents (via Task tool), their conversations are stored in separate `agent-*.jsonl` files (in `<session>/subagents/` in current Claude Code versions). Use `agent_` prefixed types to search inside agents:

```bash
# Search agent conversations
search_history.py -t agent_input_text "search"        # Prompts sent to agents
search_history.py -t agent_assistant_text "found"     # Agent text responses
search_history.py -t agent_assistant_thinking "why"   # Agent reasoning
search_history.py -t agent_tool_input "pytest"        # Tools agents called
search_history.py -t agent_tool_output "error"        # Tool results in agents

# Wildcard for all agent types
search_history.py -t 'agent_*' "error"

# Filter by tool within agents
search_history.py -t agent_tool_input --tool Grep "pattern"

# Filter to specific agent by ID prefix
search_history.py -t agent_assistant_text --agent ae16a9f

# Output shows agent ID prefix
[ ae16a9f:2a306e0e | 2025-12-25 10:46:47 | agent_assistant_text ] I found...
```

## Output Formats

### Full output (default)

```
$ search_history.py -t main_assistant_text --last 1
== [ abc123def45 | 2025-12-23 10:30:00 | main_assistant_text ] ==
Let me check the file structure first.

$ search_history.py -t main_tool_input --tool Bash --last 1
== [ def456abc78 | 2025-12-23 10:31:00 | main_tool_input:Bash ] ==
ls -la src/

$ search_history.py -t main_tool_output --tool Bash --last 1
== [ 789ghi012jk | 2025-12-23 10:31:05 | main_tool_output:Bash ] ==
total 24
drwxr-xr-x 3 user user 4096 Dec 23 10:30 .
...
```

### Compact output (-c)

```
$ search_history.py -c --last 5
[ abc123 | 2025-12-23 10:30:00 | main_assistant_text       ] Let me check the file structure first.
[ def456 | 2025-12-23 10:31:00 | main_tool_input:Bash      ] ls -la src/
[ 789ghi | 2025-12-23 10:31:05 | main_tool_output:Bash     ] total 24 drwxr-xr-x 3 user user 4096...
[ 012lmn | 2025-12-23 10:32:00 | main_user_text            ] now add the tests
```

### Expanded output (--expand)

```
$ search_history.py --expand abc123def
== [ abc123def45-full-uuid-here | 2025-12-23 10:30:00 | assistant ] ==

--- Content ---
Let me check the file structure first.

--- Thinking ---
The user wants to understand the project layout...

--- Tool: Bash ---
Input:
ls -la src/
Output:
total 24
drwxr-xr-x 3 user user 4096 Dec 23 10:30 .
...
```

## File Snapshots

View historical file states (backed up before edits).

### Listing snapshots

```bash
# List all snapshots (with 3-line preview, default)
search_history.py --snapshots

# Compact listing (no preview)
search_history.py --snapshots -c

# Filter by filename
search_history.py --snapshots "config.py"

# Filter by message ID
search_history.py --snapshots --msgid "f5a34ab8"

# Filter by time range
search_history.py --snapshots --after "08:00" --before "12:00"
search_history.py --snapshots --after "2025-12-24"

# Search snapshot contents (shows match with 1 line context)
search_history.py "function_name" --snapshots

# Combine filters
search_history.py "search_term" --snapshots "config.py" --after "08:00"
```

### Output formats

Default (with preview):
```
== [ 7f35193717348549@v1 | f5a34ab8-928 | 2025-12-23 06:16:00 | /path/to/file.py (12626 bytes) ] ==
    #!/usr/bin/env python3
    """Module docstring."""
    import argparse

== [ 7f35193717348549@v2 | 96e8759d-5c6 | 2025-12-23 08:05:01 | /path/to/file.py (12804 bytes) ] ==
    #!/usr/bin/env python3
    ...
```

Compact (`-c`):
```
[ 7f35193717348549@v1  | f5a34ab8-928 | 2025-12-23 06:16:00 | /path/to/file.py (12626 bytes) ]
[ 7f35193717348549@v2  | 96e8759d-5c6 | 2025-12-23 08:05:01 | /path/to/file.py (12804 bytes) ]
```

With search term (shows match + context):
```
== [ 7f35193717348549@v3 | 83c7c287-93a | 2025-12-24 07:01:16 | /path/to/file.py (13193 bytes) ] ==

    def utc_to_local(ts):
        """Convert UTC ISO timestamp to local time string."""
```

### Getting snapshot content

```bash
# Get raw file content by backup ID (stdout only, errors to stderr)
search_history.py --get-snapshot "7f35193717348549@v1"

# Prefix match on hash part (version suffix required)
search_history.py --get-snapshot "7f3519@v1"

# Save to file
search_history.py --get-snapshot "7f3519@v1" > old_version.py

# Diff two versions
diff <(search_history.py --get-snapshot "7f3519@v1") \
     <(search_history.py --get-snapshot "7f3519@v2")

# Diff snapshot with current file
diff <(search_history.py --get-snapshot "7f3519@v1") /path/to/file.py
```

## Options Reference

### Search options

| Option | Description |
|--------|-------------|
| `TERM` | Search term (optional, regex by default, case-sensitive) |
| `-i, --ignore-case` | Case-insensitive search |
| `-L, --literal` | Literal search (disable regex) |
| `-t, --type` | Record type pattern (supports `*` wildcard, see below) |
| `--tool NAME` | Filter by tool: `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob` |
| `--first N` | Return first N results |
| `--last N` | Return last N results |
| `--after TIME` | After time (local): `10:30`, `12-23 10:30`, `2025-12-23 10:30:00` |
| `--before TIME` | Before time (local): `10:30`, `12-23 10:30`, `2025-12-23 10:30:00` |
| `-c, --compact` | Compact one-line listing (no content/preview) |
| `-C, --context N` | Show N messages before and after each match |
| `--expand UUID` | Get full context for a UUID (prefix match); use `session:uuid` for multi-session results, `agentid:uuid` for agents |

**Record types (`-t`):**
- Main: `main_user_text`, `main_assistant_text`, `main_assistant_thinking`, `main_tool_input`, `main_tool_output`, `main_summary`
- Agent: `agent_input_text`, `agent_assistant_text`, `agent_assistant_thinking`, `agent_tool_input`, `agent_tool_output`
- Wildcards: `main_*`, `agent_*`, `*_text`, `*_tool_*`, `*_assistant_*`

### Session/project options

| Option | Description |
|--------|-------------|
| `--list-sessions` | List sessions (default: detailed with duration/tools; -c for compact) |
| `--list-projects` | List all projects |
| `--session ID` | Search specific session (prefix match) |
| `--project PATH` | Search in project at path; use `/**` suffix to include subdirs |
| `--all-sessions` | Search all sessions in matched project(s) |
| `--all-projects` | Search all projects |
| `--agent ID` | Filter to specific agent by ID prefix (for agent types) |

### Snapshot options

| Option | Description |
|--------|-------------|
| `--snapshots [FILE]` | List file snapshots, optionally filter by filename |
| `--msgid MSGID` | Filter snapshots by message ID (prefix match) |
| `--after TIME` | Filter snapshots after time (same formats as search) |
| `--before TIME` | Filter snapshots before time |
| `-c, --compact` | Compact listing (no preview) |
| `TERM` | Search snapshot contents |
| `--get-snapshot ID` | Get raw file content by backup ID (prefix match on hash, e.g. `7f3519@v1`) |