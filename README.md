# claude-skills

Two skills for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview):

| Skill | Purpose |
|-------|---------|
| [`git-staging`](skills/git-staging/SKILL.md) | Stages git changes at the level of a hunk, a sub-hunk or a single line, using stable numeric identifiers. It does the job of `git add -p` without the interactive session. |
| [`search-history`](skills/search-history/SKILL.md) | Searches past Claude Code conversations, including the parts lost to context compaction: messages, thinking, tool calls and their output, subagent transcripts, and file snapshots. |

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
- `search-history`: Python 3.10 or later, built against SQLite with FTS5 (standard in current distributions). The optional indexer daemon also needs [uv](https://docs.astral.sh/uv/), plus either Linux with a systemd user session (uv at `~/.local/bin/uv`) or macOS (uv anywhere on `PATH`, e.g. from Homebrew). On Linux, uv installs the daemon's one dependency, `inotify_simple`, by itself; on macOS the daemon needs nothing beyond the standard library.

### The search-history indexer

`search_history.py` works without any setup. It reads the JSONL transcripts under `~/.claude/projects/` directly. The daemon keeps a full-text index of those transcripts, of the plans in `~/.claude/plans/` and of the file snapshots in `~/.claude/file-history/`. The index lives in an SQLite database at `~/.local/share/claude-history-index/history.db`. It is placed outside `~/.claude` so that writing to it does not wake Claude Code's own file watchers. When the database exists, searches use it.

To install the daemon as a systemd user service (Linux) or a launchd agent (macOS):

```bash
~/.claude/skills/search-history/install.sh
systemctl --user status claude-history-indexer.service       # Linux
launchctl print gui/$(id -u)/local.claude-history-indexer    # macOS
```

The daemon indexes the existing history on its first start and then follows changes. On Linux it uses inotify, so changes are indexed as they happen; the unit runs it at nice 19 and in the idle I/O class, limited to 256 MB of memory and 10 % of one CPU. macOS has no inotify, so there the daemon rescans every 5 seconds. A rescan only checks modification times and costs a few milliseconds when nothing changed. The launchd agent runs it as a background process at nice 19 with low-priority I/O, and logs to `~/Library/Logs/claude-history-indexer.log`.

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
$H --expand abc123                           # one record in full
$H term --all-sessions                       # every session of this project
$H term --all-projects                       # every project
$H --snapshots config.py                     # earlier versions of a file
$H --get-snapshot 7f3519@v2 > old.py
```

Each record has a type:

- the main conversation: `main_user_text`, `main_assistant_text`, `main_assistant_thinking`, `main_tool_input`, `main_tool_output` and `main_summary`;
- subagents: the corresponding `agent_*` types.

The `-t` option accepts shell-style wildcards over these names. By default a search covers the current session only. The `SKILL.md` lists all the options.

`history_db.py` and `plot_activity.py` are older utilities. They build and read a database at `~/.claude/history.db` and are not used by the skill or the daemon.

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
    daemon.py                        indexer (inotify on Linux, polling on macOS)
    schema.sql                       index schema (FTS5)
    install.sh                       installs the systemd user unit or launchd agent
    claude-history-indexer.service
    history_db.py, plot_activity.py  older utilities
```

## License

MIT; see [LICENSE](LICENSE).
