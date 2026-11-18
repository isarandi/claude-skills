# claude-skills

Two skills for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview):

| Skill | Purpose |
|-------|---------|
| [`git-staging`](skills/git-staging/SKILL.md) | Stages git changes at the level of a hunk, a sub-hunk or a single line, using stable numeric identifiers. It does the job of `git add -p` without the interactive session. |
| [`search-history`](skills/search-history/SKILL.md) | Searches past Claude Code conversations, including the parts lost to context compaction: messages, thinking, tool calls and their output, subagent transcripts, and file snapshots. Its index also serves as an archive: it keeps all of this after Claude Code has deleted the files. |

Each skill is a directory with a `SKILL.md`, which Claude reads when the skill is invoked, and the scripts it calls. The `SKILL.md` files are the full reference. This README covers installation and gives an overview.

## Installation

Claude Code loads personal skills from `~/.claude/skills/<name>/`. Both `SKILL.md` files call their scripts by absolute paths below that directory, for example `~/.claude/skills/git-staging/stage-hunks.py`, so each skill has to be reachable there under its own name. The simplest way is to clone the repository once and symlink the two directories:

```bash
git clone https://github.com/isarandi/claude-skills.git ~/projects/claude-skills
mkdir -p ~/.claude/skills
ln -s ~/projects/claude-skills/skills/git-staging    ~/.claude/skills/git-staging
ln -s ~/projects/claude-skills/skills/search-history ~/.claude/skills/search-history
```

A `git pull` in the clone then updates both skills. Claude Code finds new skills when a session starts.

There is another option. The `.gitignore` at the root ignores everything except itself, this README, the license, `skills/git-staging/` and `skills/search-history/`. As a second safeguard it also lists the credential, settings and transcript files of `~/.claude` by name. With this design the repository can be checked out directly as `~/.claude`, and nothing else in that directory is ever tracked.

### Requirements

- `git-staging`: Python 3.10 or later and git. It uses only the standard library.
- `search-history`: Python 3.10 or later with its standard `sqlite3` module. The indexer daemon also needs [uv](https://docs.astral.sh/uv/), plus either Linux with a systemd user session (uv at `~/.local/bin/uv`) or macOS (uv anywhere on `PATH`, e.g. from Homebrew). On Linux, uv installs the daemon's one dependency, `inotify_simple`, by itself; on macOS the daemon needs nothing beyond the standard library.

### The search-history indexer

`search_history.py` works without any setup: it then reads the JSONL transcripts under `~/.claude/projects/` directly. With the daemon installed, searches read its index instead, an SQLite database at `~/.local/share/claude-history-index/history.db`. It is placed outside `~/.claude` so that writing to it does not wake Claude Code's own file watchers.

The index holds:

- the transcripts of sessions and subagents: messages (also those delivered while Claude was working), thinking, tool calls with their full input, and tool results, including the large ones that Claude Code saves to `<session>/tool-results/` and shows only a preview of;
- for edits, the tool's own record of the change (`originalFile`, `structuredPatch`), so that a file can be rebuilt from its edits;
- images and other binary content, each stored once under its SHA-256, with a placeholder in the text;
- the other files under `<session>/tool-results/` (artifact pages, fetched PDFs), subagent meta files, memory notes and plans with their earlier versions, the prompt history (`history.jsonl`) with its pasted texts;
- the file snapshots of `~/.claude/file-history/`, with the path of the file each one is a version of.

Nothing is removed from the index when Claude Code deletes a file, so the index is the archive of what Claude Code's cleanup takes away. Times are stored in UTC and shown in the local time of the machine that runs the search. The database is per machine and has no second copy unless you make one: it belongs in your backup.

To install the daemon as a systemd user service (Linux) or a launchd agent (macOS), or to restart it after an update of this repository:

```bash
~/.claude/skills/search-history/install.sh
systemctl --user status claude-history-indexer.service       # Linux
launchctl print gui/$(id -u)/local.claude-history-indexer    # macOS
```

The daemon indexes the existing history on its first start and then follows changes. On Linux it uses inotify for transcripts and snapshots, so they are indexed as they are written, reading only the lines added since the last time; the other files are picked up by a full rescan every two minutes, which also serves as a safety net. The unit runs it at nice 19 and in the idle I/O class, limited to 1 GB of memory and 10 % of one CPU. macOS has no inotify, so there the daemon rescans the transcripts every 5 seconds. A rescan only checks modification times and costs a few milliseconds when nothing changed. The launchd agent runs it as a background process at nice 19 with low-priority I/O, and logs to `~/Library/Logs/claude-history-indexer.log`.

An index written by an earlier version is migrated in place when the new daemon first starts (the format number is in the database's `user_version`; this version writes format 2). The migration keeps all rows, including those of sessions whose files are gone, parses the transcripts still on disk again to complete them, and ends with a `VACUUM`. It needs free disk space of about the size of the database and takes a few minutes under the unit's CPU limit; an interrupted migration is resumed by the next start. Until it has finished, searches read the transcript files. Make a copy of the database first if it holds sessions that exist nowhere else. To run the migration by hand, without the CPU limit:

```bash
systemctl --user stop claude-history-indexer.service
uv run ~/.claude/skills/search-history/daemon.py --once     # bring the index up to date and exit
systemctl --user start claude-history-indexer.service
```

A transcript that is kept elsewhere (in a backup, on another machine) can be added to the index, or used to complete rows that an earlier version stored in shortened form:

```bash
uv run ~/.claude/skills/search-history/daemon.py --ingest /backup/SESSION.jsonl \
    --as ~/.claude/projects/-home-user-project/SESSION.jsonl
```

The tests need only Python: `python3 -m unittest discover -s skills/search-history/tests`.

## git-staging

`stage-hunks.py` gives every changed line an identifier of the form `HUNK.SUBHUNK.LINE`:

- `HUNK` is a hunk of the ordinary three-line-context diff (`-U3`).
- `SUBHUNK` is a run of consecutive changed lines inside it (`-U0`).
- `LINE` is one line within that run.

A selector may name a whole hunk (`2`), a sub-hunk (`2.1`), a line (`2.1.3`) or a range (`2.{1..3}`). Several selectors can be given at once. The tool builds a patch from the selected lines and applies it to the index.

```bash
S=~/.claude/skills/git-staging/stage-hunks.py
$S src/file.py --renumber            # number the changes, print a one-line-per-hunk outline
$S src/file.py --find 'helper_name'  # which identifiers touch this text
$S src/file.py 3 5.2 && git commit -m "first part"
$S src/file.py --outline             # what remains, under the same identifiers
$S src/file.py --all && git commit -m "the rest"
```

Only `--renumber` assigns identifiers. The numbering is stored in `.git/stage-hunks/`, and staging and committing leave it unchanged, so a single numbering serves a whole series of commits. Editing the file invalidates the stored numbering: a content hash detects the change, and the tool then refuses to stage until `--renumber` is run again.

Every selector is checked before anything is staged. A selector that matches no identifier aborts the call, and nothing is staged. After staging, the tool reports the number of lines added and removed, counted from the patch it actually applied.

`stage-hunks.sh` is the earlier shell implementation. The skill no longer uses it.

## search-history

```bash
H=~/.claude/skills/search-history/search_history.py
$H -c --after 09:00                          # compact timeline of the current session
$H pytest -t main_tool_input --tool Bash     # Bash commands that mention pytest
$H -i 'error|warning' -t '*_tool_output' -C 2
$H --expand ae6a59bd:abc123def456            # one record in full
$H term --all-sessions                       # all sessions of this project
$H term --all-projects                       # all projects
$H -t main_user_text --origin human deadline # what the user typed
$H --snapshots config.py                     # earlier versions of a file
$H --get-snapshot 7f3519@v2 -o old.py
$H src/config.py -t '*_tool_input' --tool Edit --json   # the edits of a file, exact
```

Each record has a type:

- the main conversation: `main_user_text`, `main_meta_text` (messages from agents, task notifications, reminders), `main_assistant_text`, `main_assistant_thinking`, `main_tool_input`, `main_tool_output` and `main_summary`;
- subagents: the corresponding `agent_*` types;
- files, searched only when `-t` names them: `memory`, `prompt_history`, `tool_file`, `subagent_meta`, `plan` and `paste`.

The `-t` option accepts shell-style wildcards over these names. By default a search covers the current session only. The `SKILL.md` lists all the options.

`plot_activity.py` plots when the user wrote messages, by time of day, day of the week and date, from the index. It needs matplotlib and is not used by the skill or the daemon.

## Layout

```
skills/
  git-staging/
    SKILL.md
    stage-hunks.py                   the tool
    stage-hunks.sh                   earlier shell version
  search-history/
    SKILL.md
    search_history.py                search front end; reads the index or the raw JSONL
    daemon.py                        indexer (inotify on Linux, polling on macOS); holds the index schema
    install.sh                       installs or restarts the systemd user unit or launchd agent
    claude-history-indexer.service
    plot_activity.py                 activity plot from the index
    tests/                           unit tests of the parser, the indexer, the migration and the command line
```

## License

MIT; see [LICENSE](LICENSE).
