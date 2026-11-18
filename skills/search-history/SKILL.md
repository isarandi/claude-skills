---
name: search-history
description: Search conversation history that got lost due to context compaction esp. to find exact code or commands from earlier. Only use when user explicitly asks to check history, or when you need very specific info that is no longer in context. (user)
allowed-tools: Bash
---

# Search Conversation History

Search through conversation history to recover code, commands, or context that was lost to compaction.

Searches read an SQLite index that a background daemon keeps up to date within seconds, so speed is not a concern. The index is also the archive: it keeps sessions, subagent transcripts, tool outputs and file snapshots after Claude Code has deleted their files, and searches cover them the same way. Scope searches appropriately for relevance:
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

# Expand a specific entry to see full details (copy the handle from the listing)
search_history.py --expand ae6a59bd:abc123def456
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
search_history.py --expand ae6a59bd:def456abc789
```

### 3. Find what was discussed about a topic

```bash
# What the user typed (including messages sent while Claude was working)
search_history.py -t main_user_text "authentication"

# Search assistant responses
search_history.py -t '*_assistant_text' "authentication"

# Search thinking to see reasoning
search_history.py -t '*_thinking' "authentication"
```

### 4. Review file changes

```bash
# List the file snapshots of this session (with preview)
search_history.py --snapshots

# Compact listing
search_history.py --snapshots -c

# Filter by time, file, search content
search_history.py --snapshots --after "08:00" "config.py"

# Get specific version
search_history.py --get-snapshot "7f3519@v1"
```

### 5. Search across sessions/projects

By default a search covers the current session: the one Claude Code names in `CLAUDE_CODE_SESSION_ID`.

```bash
# List sessions in current project (* marks current)
search_history.py --list-sessions

# List all projects
search_history.py --list-projects

# Search all sessions in current project
search_history.py "term" --all-sessions

# Search all projects (when you forgot where something was discussed)
search_history.py "term" --all-projects

# Search all sessions of a different project (exact path)
search_history.py "term" --project /home/user/myproject

# Include subdirectory projects with /** (only this glob pattern supported)
search_history.py "term" --project /home/user/myproject/**

# Search specific session by ID prefix, whichever project it is in
search_history.py "term" --session ae6a59bd
```

Sessions whose transcript Claude Code has deleted stay in the index. `--list-sessions` marks them with `(transcript deleted; in the index)`, and all commands work on them as on any other session.

### 6. Rebuild a file from its edits

The index keeps each tool call's exact input, and for `Edit` and `Write` also what the tool reported back (`originalFile`, the file before the edit, and `structuredPatch`), where the transcript has them. `--json` prints them:

```bash
# All Write and Edit calls that touched a file, oldest first, one JSON object per line
search_history.py "src/config.py" -t '*_tool_input' --tool Edit --all-sessions --json
search_history.py "src/config.py" -t '*_tool_input' --tool Write --all-sessions --json

# One call with its result: "input" has old_string/new_string or content,
# "result" has originalFile and structuredPatch, "is_error" marks a failed call
search_history.py --expand ae6a59bd:def456abc789 --json
```

File snapshots (workflow 4) give whole earlier versions directly, where they exist.

## Usage

```bash
~/.claude/skills/search-history/search_history.py [OPTIONS] [TERM]
# or, equivalently:
python3 ~/.claude/skills/search-history/search_history.py [OPTIONS] [TERM]
```

The script is executable; the examples abbreviate its path to `search_history.py`. It can be run from any directory: the current session is the one Claude Code names in `CLAUDE_CODE_SESSION_ID`, whatever the shell's working directory (subagents share their parent's session). Times are in the local time zone of the machine that runs the search, in input and output; the index stores them in UTC.

## Search

```bash
# Basic search across the main conversation's record types
search_history.py "pytest"

# Filter by record type (supports * wildcard)
search_history.py -t main_user_text "feature"           # What the user typed
search_history.py -t main_meta_text "finished"          # Messages from agents, task notifications, reminders
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

# Filter by where a message came from
search_history.py --origin human "deadline"            # typed by the user
search_history.py --origin agent -t main_meta_text     # sent by another agent or session
search_history.py --origin task-notification --last 5

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

# Contents in full (a listing cuts each item after 2,000 characters and says so)
search_history.py -t main_tool_output --tool Bash --last 1 --full
```

A search term is looked for in the text of each item. For a tool call that text is the command (`Bash`), the path (`Read`), the path and the content (`Write`), the path with the old and the new text (`Edit`), or `key: value` lines for other tools, all in full. For a tool result it is the whole output, also when Claude Code saved a large output to a file and showed only a preview.

## Searching Agents

When Claude spawns subagents, their conversations are stored in separate `agent-*.jsonl` files (in `<session>/subagents/`). Use `agent_` prefixed types to search inside agents:

```bash
# Search agent conversations
search_history.py -t agent_input_text "search"        # Prompts and messages sent to agents
search_history.py -t agent_assistant_text "found"     # Agent text responses
search_history.py -t agent_assistant_thinking "why"   # Agent reasoning
search_history.py -t agent_tool_input "pytest"        # Tools agents called
search_history.py -t agent_tool_output "error"        # Tool results in agents
search_history.py -t agent_summary "so far"           # Compaction summaries of agents

# Wildcard for all agent types
search_history.py -t 'agent_*' "error"

# Filter by tool within agents
search_history.py -t agent_tool_input --tool Grep "pattern"

# Filter to specific agent by ID prefix (searches its records without -t)
search_history.py --agent ae16a9f

# What kind of agent it was: type, description, model
search_history.py -t subagent_meta --agent ae16a9f

# Output shows the session and the agent ID before the record's ID
[ ae6a59bd:ae16a9f2b0c4d6e8f:2a306e0e-1b2 | 2025-12-25 10:46:47 | agent_assistant_text ] I found...
```

## Other Indexed Files

These types are searched only when `-t` names them:

```bash
search_history.py -t memory "style" --all-projects      # memory notes; an edited note keeps its earlier versions
search_history.py -t prompt_history "deploy" --all-projects  # what was typed at the prompt, with pasted texts
search_history.py -t tool_file "Quarterly" --all-sessions    # files tools saved: artifact pages, fetched PDFs (name, type, size), task outputs
search_history.py -t subagent_meta "Explore" --all-sessions  # subagent meta files
search_history.py -t plan "migration" --all-projects    # plan files
```

## Images and Other Binary Content

Images (screenshots, pasted pictures) and documents are not part of the text. A placeholder stands where they were:

```
[image sha256:3fa9c0…(64 hex digits) image/png 48213 bytes]
```

`--get-blob` writes the content out; a prefix of the hash, or the whole placeholder, names it:

```bash
search_history.py --get-blob 3fa9c0d1 -o /tmp/screenshot.png   # then read the file
```

## Output Formats

### Full output (default)

```
$ search_history.py -t main_assistant_text --last 1
== [ ae6a59bd:abc123def456 | 2025-12-23 10:30:00 | main_assistant_text ] ==
Let me check the file structure first.

$ search_history.py -t main_tool_input --tool Bash --last 1
== [ ae6a59bd:def456abc789 | 2025-12-23 10:31:00 | main_tool_input:Bash ] ==
ls -la src/

$ search_history.py -t main_tool_output --tool Bash --last 1
== [ ae6a59bd:789abc012def | 2025-12-23 10:31:05 | main_tool_output:Bash ] ==
total 24
drwxr-xr-x 3 user user 4096 Dec 23 10:30 .
...
```

The header holds the record's handle (`session:record` or `session:agent:record`), the local time and the type. The type is followed by the tool's name, or by the origin of a message that the user did not type (`main_meta_text:agent`, `main_meta_text:task-notification`).

### Compact output (-c)

```
$ search_history.py -c --last 5
[ ae6a59bd:abc123def456 | 2025-12-23 10:30:00 | main_assistant_text       ] Let me check the file structure first.
[ ae6a59bd:def456abc789 | 2025-12-23 10:31:00 | main_tool_input:Bash      ] ls -la src/
[ ae6a59bd:789abc012def | 2025-12-23 10:31:05 | main_tool_output:Bash     ] total 24 drwxr-xr-x 3 user user 4096...
[ ae6a59bd:012def345abc | 2025-12-23 10:32:00 | main_user_text            ] now add the tests
```

### Expanded output (--expand)

```
$ search_history.py --expand ae6a59bd:abc123def456
== [ ae6a59bd:abc123def456 | 2025-12-23 10:30:00 | assistant ] ==

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

`--expand` takes the handle as a listing prints it. The record's ID alone, or a prefix of it, also works as long as it names one record. Each part is printed in full up to 100,000 characters; `--full` lifts that limit too.

### JSON output (--json)

With `--json`, a search or `--expand` prints one JSON object per item, in full: `uuid`, `session_id`, `agent_id`, `timestamp` (local), `type`, `tool_name`, `tool_use_id`, `origin`, `model`, `content`, and where present `input` (a tool call's exact input), `result` (an edit's `originalFile`, `structuredPatch` and related fields) and `is_error`.

## File Snapshots

View historical file states (backed up by Claude Code before edits). The index keeps them after Claude Code has removed the backup files, which it does for superseded versions once a session has more than 100 checkpoints.

### Listing snapshots

```bash
# List the snapshots of the current session (with 3-line preview, default)
search_history.py --snapshots

# Compact listing (no preview)
search_history.py --snapshots -c

# Filter by filename
search_history.py --snapshots "config.py"

# Another session, a whole project, or everything
search_history.py --snapshots "config.py" --session ae6a59bd
search_history.py --snapshots "config.py" --all-sessions
search_history.py --snapshots "config.py" --all-projects

# Filter by the ID of the message before which the file looked like this
search_history.py --snapshots --msgid "f5a34ab8"

# Filter by time range
search_history.py --snapshots --after "08:00" --before "12:00"
search_history.py --snapshots --after "2025-12-24"

# Search snapshot contents (shows match with 1 line context; not a regex, case ignored)
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

Each file version is listed once, with the time of the backup and the message before which the file looked like this. When the listing spans several sessions, the ID is prefixed with the session (`ae6a59bd:7f35193717348549@v1`). `(content not in the index)` in place of the size marks a version that the transcript names but whose backup file was gone before the indexer saw it.

With search term (shows match + context):
```
== [ 7f35193717348549@v3 | 83c7c287-93a | 2025-12-24 07:01:16 | /path/to/file.py (13193 bytes) ] ==

    def utc_to_local(ts):
        """Convert UTC ISO timestamp to local time string."""
```

### Getting snapshot content

```bash
# Get the file content by backup ID, byte for byte (stdout only, errors to stderr)
search_history.py --get-snapshot "7f35193717348549@v1"

# Prefix match on hash part (version suffix required)
search_history.py --get-snapshot "7f3519@v1"

# Save to file
search_history.py --get-snapshot "7f3519@v1" -o old_version.py

# From a named session (as a listing over several sessions prints the ID)
search_history.py --get-snapshot "ae6a59bd:7f3519@v1"

# Diff two versions
diff <(search_history.py --get-snapshot "7f3519@v1") \
     <(search_history.py --get-snapshot "7f3519@v2")

# Diff snapshot with current file
diff <(search_history.py --get-snapshot "7f3519@v1") /path/to/file.py
```

Without a session, the ID is looked for in the current session first and then in all sessions.

## Options Reference

### Search options

| Option | Description |
|--------|-------------|
| `TERM` | Search term (optional, regex by default, case-sensitive) |
| `-i, --ignore-case` | Case-insensitive search |
| `-L, --literal` | Literal search (disable regex) |
| `-t, --type` | Record type pattern (supports `*` wildcard, see below) |
| `--tool NAME` | Filter by tool: `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Agent`, ... |
| `--origin ORIGIN` | Filter messages by origin: `human`, `agent`, `coordinator`, `task-notification` |
| `--first N` | Return first N results |
| `--last N` | Return last N results |
| `--after TIME` | After time (local): `10:30`, `12-23 10:30`, `2025-12-23 10:30:00` |
| `--before TIME` | Before time (local): `10:30`, `12-23 10:30`, `2025-12-23 10:30:00` |
| `-c, --compact` | Compact one-line listing (no content/preview) |
| `--full` | No cut after 2,000 characters per item (with `--expand`: after 100,000 per part) |
| `--json` | One JSON object per item, in full, with exact tool inputs and edit results |
| `-C, --context N` | Show N items before and after each match (from the same transcript) |
| `--expand HANDLE` | Everything of one record: `session:record` or `session:agent:record` as printed in a result's header, or the record's ID alone (prefix match) |
| `--get-blob HASH` | Write an image or other binary content (hash prefix or whole placeholder) to `-o FILE` or standard output |
| `-o, --output FILE` | Target file for `--get-blob` and `--get-snapshot` |

**Record types (`-t`):**
- Main: `main_user_text` (typed by the user, also while Claude was working), `main_meta_text` (messages from agents, task notifications, reminders, skill texts), `main_assistant_text`, `main_assistant_thinking`, `main_tool_input`, `main_tool_output`, `main_summary`
- Agent: `agent_input_text` (the prompt and messages sent to the agent), `agent_meta_text`, `agent_assistant_text`, `agent_assistant_thinking`, `agent_tool_input`, `agent_tool_output`, `agent_summary`
- Files (only with `-t`): `memory`, `prompt_history`, `tool_file`, `subagent_meta`, `plan`, `paste`
- Wildcards: `main_*`, `agent_*`, `*_text`, `*_tool_*`, `*_assistant_*`
- Without `-t`: the main types; with `--agent` also the agent types

### Session/project options

| Option | Description |
|--------|-------------|
| `--list-sessions` | List sessions (default: detailed with duration/tools; -c for compact) |
| `--list-projects` | List all projects |
| `--session ID` | Search specific session (prefix match), in any project |
| `--project PATH` | Search all sessions of the project at path; use `/**` suffix to include subdirs |
| `--all-sessions` | Search all sessions of the current project |
| `--all-projects` | Search all sessions of all projects |
| `--agent ID` | Filter to specific agent by ID prefix |

### Snapshot options

| Option | Description |
|--------|-------------|
| `--snapshots [FILE]` | List file snapshots, optionally filter by filename |
| `--msgid MSGID` | Filter snapshots by message ID (prefix match) |
| `--after TIME` | Filter snapshots after time (same formats as search) |
| `--before TIME` | Filter snapshots before time |
| `-c, --compact` | Compact listing (no preview) |
| `TERM` | Search snapshot contents |
| `--get-snapshot ID` | Get the file content by backup ID (prefix match on hash, e.g. `7f3519@v1`; optionally `session:ID`) |

The session and project options above also select which sessions' snapshots are listed.

## Without the Index

If the daemon is not installed, or its index has an older format, searches read the transcript files under `~/.claude/projects/` directly. This is slower, covers only what is still on disk, and has no file types, images or snapshots of other sessions. `~/.claude/skills/search-history/install.sh` installs the daemon.
