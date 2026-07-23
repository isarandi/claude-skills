#!/usr/bin/env python3
"""Stage git changes with fine-grained 3-level hierarchical selection.

Display format:
    [1.2.3] +line   where 1=U3 hunk, 2=U0 sub-hunk, 3=line within U0

Selectors:
    2           Stage entire U3 hunk 2
    2.1         Stage U0 sub-hunk 1 of hunk 2
    2.{3..5}    Stage U0 sub-hunks 3,4,5 of hunk 2
    2.1.3       Stage line 3 of sub-hunk 2.1
    2.1.{1..3}  Stage lines 1-3 of sub-hunk 2.1

Persistent IDs: every --print stores its numbering in .git/stage-hunks/.
Selectors always refer to the numbering of the LAST --print of that file,
no matter how much has been staged, unstaged or committed since - staging
never renumbers anything. Only a new --print (or editing the file, which
invalidates the stored numbering) establishes a new frame of reference.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class DiffLine:
    """A line from the diff with its metadata."""

    content: str  # Full line including +/- prefix
    line_type: str  # '+', '-', ' ' or '@@'
    u3_idx: int = 0
    u0_idx: int = 0
    line_idx: int = 0  # Line within U0 hunk (1-indexed, 0 for context)
    # Identity anchor. The worktree file is untouched by staging, so the
    # worktree line position identifies a change line across any sequence
    # of stagings: the line number a '+' line occupies, or the position a
    # '-' line's deletion run sits at. Identical deleted lines at the same
    # position are interchangeable and matched as a group.
    new_ln: int = 0


def run_git(args: list[str]) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(["git"] + args, capture_output=True, text=True)
    return result.stdout


def parse_hunk_header(line: str) -> tuple[int, int, int, int, str] | None:
    """Parse @@ -old,len +new,len @@ context."""
    match = re.match(r"@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@(.*)", line)
    if not match:
        return None
    old_start = int(match.group(1))
    old_len = int(match.group(2)) if match.group(2) else 1
    new_start = int(match.group(3))
    new_len = int(match.group(4)) if match.group(4) else 1
    context = match.group(5)
    return old_start, old_len, new_start, new_len, context


def build_hierarchy(filepath: str) -> tuple[list[list[DiffLine]], str]:
    """Build 3-level hierarchy by comparing U3 and U0 diffs.

    Returns:
        - List of U3 hunks, each containing DiffLine objects with hierarchy IDs
        - File header string
    """
    u3_diff = run_git(["diff", "-U3", "--", filepath])
    u0_diff = run_git(["diff", "-U0", "--", filepath])

    if not u3_diff.strip():
        return [], ""

    # Extract file header
    lines = u3_diff.split("\n")
    header_lines = []
    diff_start = 0
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            diff_start = i
            break
        header_lines.append(line)
    file_header = "\n".join(header_lines)

    # Parse U0 hunks to get line number -> (u0_hunk_index within u3) mapping
    u0_hunks = []
    for line in u0_diff.split("\n"):
        if line.startswith("@@"):
            parsed = parse_hunk_header(line)
            if parsed:
                old_start, old_len, new_start, new_len, _ = parsed
                u0_hunks.append({
                    "old_start": old_start,
                    "old_end": old_start + max(old_len, 1),
                    "new_start": new_start,
                    "new_end": new_start + max(new_len, 1),
                })

    # Parse U3 diff into hunks with hierarchy
    u3_hunks = []
    current_hunk = []
    u3_idx = 0
    current_u3_old_start = 0
    current_u3_old_end = 0
    current_u3_new_start = 0
    current_u3_new_end = 0

    old_line = 0
    new_line = 0

    def get_u0_idx_for_position(old_pos, new_pos, change_type):
        """Find which U0 sub-hunk this position belongs to."""
        relevant_u0s = []
        for i, u0 in enumerate(u0_hunks):
            in_u3 = (
                (u0["new_start"] >= current_u3_new_start and u0["new_start"] < current_u3_new_end)
                or (u0["old_start"] >= current_u3_old_start and u0["old_start"] < current_u3_old_end)
            )
            if in_u3:
                relevant_u0s.append((i, u0))

        for rel_idx, (_, u0) in enumerate(relevant_u0s, 1):
            if change_type == "+":
                if u0["new_start"] <= new_pos < u0["new_end"]:
                    return rel_idx
            elif change_type == "-":
                if u0["old_start"] <= old_pos < u0["old_end"]:
                    return rel_idx
        return 0

    u0_line_counts = {}

    for line in lines[diff_start:]:
        if line.startswith("@@"):
            if current_hunk:
                u3_hunks.append(current_hunk)
            current_hunk = []
            u3_idx += 1
            u0_line_counts = {}

            parsed = parse_hunk_header(line)
            if parsed:
                old_start, old_len, new_start, new_len, ctx = parsed
                current_u3_old_start = old_start
                current_u3_old_end = old_start + old_len
                current_u3_new_start = new_start
                current_u3_new_end = new_start + new_len
                old_line = old_start
                new_line = new_start

            current_hunk.append(DiffLine(
                content=line,
                line_type="@@",
                u3_idx=u3_idx,
            ))
        elif line.startswith("+"):
            u0_idx = get_u0_idx_for_position(old_line, new_line, "+")
            u0_line_counts[u0_idx] = u0_line_counts.get(u0_idx, 0) + 1
            current_hunk.append(DiffLine(
                content=line,
                line_type="+",
                u3_idx=u3_idx,
                u0_idx=u0_idx,
                line_idx=u0_line_counts[u0_idx],
                new_ln=new_line,
            ))
            new_line += 1
        elif line.startswith("-"):
            u0_idx = get_u0_idx_for_position(old_line, new_line, "-")
            u0_line_counts[u0_idx] = u0_line_counts.get(u0_idx, 0) + 1
            current_hunk.append(DiffLine(
                content=line,
                line_type="-",
                u3_idx=u3_idx,
                u0_idx=u0_idx,
                line_idx=u0_line_counts[u0_idx],
                new_ln=new_line,
            ))
            old_line += 1
        elif line.startswith(" "):
            current_hunk.append(DiffLine(
                content=line,
                line_type=" ",
                u3_idx=u3_idx,
            ))
            old_line += 1
            new_line += 1

    if current_hunk:
        u3_hunks.append(current_hunk)

    return u3_hunks, file_header


def leaf_key(dl: DiffLine) -> tuple:
    """Staging-invariant identity of a change line (see DiffLine)."""
    return (dl.line_type, dl.new_ln, dl.content)


def assign_snapshot_to_current(snap: dict, u3_hunks: list) -> list:
    """Map each snapshot leaf to its current DiffLine, or None if staged.

    Lines are matched by their staging-invariant key. Identical deleted
    lines at the same position share a key; they are interchangeable, so
    they are assigned pairwise in order and the surplus counts as staged.
    """
    current_groups = {}
    for dl in iter_leaves(u3_hunks):
        current_groups.setdefault(leaf_key(dl), []).append(dl)

    taken = {}
    assignment = []
    for leaf in snap["leaves"]:
        key = tuple(leaf["key"])
        idx = taken.get(key, 0)
        group = current_groups.get(key, [])
        assignment.append(group[idx] if idx < len(group) else None)
        taken[key] = idx + 1
    return assignment


def iter_leaves(u3_hunks: list):
    for hunk in u3_hunks:
        for dl in hunk:
            if dl.line_type in ("+", "-"):
                yield dl


# ── Snapshot persistence ─────────────────────────────────────────────


def snapshot_path(filepath: str) -> str:
    git_dir = run_git(["rev-parse", "--git-dir"]).strip()
    if not git_dir:
        print("Error: not inside a git repository")
        sys.exit(1)
    snap_dir = os.path.join(git_dir, "stage-hunks")
    os.makedirs(snap_dir, exist_ok=True)
    digest = hashlib.sha1(os.path.abspath(filepath).encode()).hexdigest()
    return os.path.join(snap_dir, digest + ".json")


def worktree_sha(filepath: str) -> str:
    try:
        with open(filepath, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""


def save_snapshot(filepath: str, u3_hunks: list) -> dict:
    snap = {
        "version": 2,
        "file": filepath,
        "worktree_sha": worktree_sha(filepath),
        # Full listing (including context) so read views can render the
        # stored frame even after parts of it were staged away
        "hunks": [
            [
                {"t": dl.line_type, "c": dl.content, "u3": dl.u3_idx,
                 "u0": dl.u0_idx, "li": dl.line_idx, "nl": dl.new_ln}
                for dl in hunk
            ]
            for hunk in u3_hunks
        ],
    }
    _attach_leaves(snap)
    with open(snapshot_path(filepath), "w") as f:
        json.dump({k: v for k, v in snap.items() if k != "leaves"}, f)
    return snap


def _attach_leaves(snap: dict) -> None:
    """Derive the leaf list (selection targets) from the stored hunks."""
    snap["leaves"] = [
        {"id": [ln["u3"], ln["u0"], ln["li"]], "key": [ln["t"], ln["nl"], ln["c"]]}
        for hunk in snap["hunks"]
        for ln in hunk
        if ln["t"] in ("+", "-")
    ]


def load_snapshot(filepath: str) -> dict | None:
    try:
        with open(snapshot_path(filepath)) as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if snap.get("version") != 2:
        return None
    _attach_leaves(snap)
    return snap


def get_or_create_snapshot(filepath: str) -> dict:
    """Load the last-printed numbering; fall back to the current diff.

    Exits with an error when the worktree file changed since the stored
    numbering was printed: the IDs the caller remembers would silently mean
    different lines.
    """
    snap = load_snapshot(filepath)
    if snap is None:
        u3_hunks, _ = build_hierarchy(filepath)
        if not u3_hunks:
            print(f"No changes in {filepath}")
            sys.exit(1)
        return save_snapshot(filepath, u3_hunks)
    if snap["worktree_sha"] != worktree_sha(filepath):
        print(f"Error: {filepath} was modified since its numbering was "
              f"established; the stored IDs no longer match the file.")
        print("Run --renumber to establish fresh IDs.")
        sys.exit(1)
    return snap


# ── Display ──────────────────────────────────────────────────────────


def staged_marks(snap: dict) -> dict:
    """Map id(leaf) -> True when that leaf is no longer in the unstaged diff."""
    u3_hunks, _ = build_hierarchy(snap["file"])
    assignment = assign_snapshot_to_current(snap, u3_hunks)
    return {id(leaf): dl is None for leaf, dl in zip(snap["leaves"], assignment)}


def _iter_snap_lines(snap: dict):
    """Yield (line_dict, leaf_or_None) walking the stored listing in order."""
    leaf_iter = iter(snap["leaves"])
    for hunk in snap["hunks"]:
        for ln in hunk:
            leaf = next(leaf_iter) if ln["t"] in ("+", "-") else None
            yield ln, leaf


def read_view_snapshot(filepath: str) -> dict:
    """Load the frame for a read view; create one only if none exists."""
    return get_or_create_snapshot(filepath)


def print_full(filepath: str, selectors: list[str] | None = None):
    """Render the stored frame with live staged status.

    With selectors, show only the hunks they refer to.
    """
    snap = read_view_snapshot(filepath)
    marks = staged_marks(snap)

    wanted_u3 = None
    if selectors:
        chosen = resolve_selectors(snap, selectors, all_except=False)
        wanted_u3 = {leaf["id"][0] for leaf in chosen}

    n_lines = 0
    n_staged = 0
    for ln, leaf in _iter_snap_lines(snap):
        if leaf is not None:
            n_lines += 1
            if marks.get(id(leaf)):
                n_staged += 1

    print(f"Changes in {filepath} (stored numbering; {n_staged}/{n_lines} "
          f"change lines staged):")
    print()
    for ln, leaf in _iter_snap_lines(snap):
        u3 = ln["u3"]
        if wanted_u3 is not None and u3 not in wanted_u3:
            continue
        if ln["t"] == "@@":
            print("━" * 60)
            print(f"[{u3}] {ln['c']}")
            print("━" * 60)
        elif leaf is not None:
            idx_str = f"[{ln['u3']}.{ln['u0']}.{ln['li']}]"
            mark = "✓" if marks.get(id(leaf)) else " "
            print(f"{idx_str:12}{mark} {ln['c']}")
        else:
            print(f"{'':13}{ln['c']}")
    print()
    print("Selectors: N (hunk) | N.M (sub-hunk) | N.M.L (line) | N.{1..3} (range)"
          "  |  ✓ = staged")
    if n_lines > 60 and wanted_u3 is None:
        print(f"({len(snap['hunks'])} hunks - try --outline for the map, "
              f"--find <regex> to locate content)")


def print_outline(filepath: str):
    """One line per hunk: header, counts, staged status, first change."""
    snap = read_view_snapshot(filepath)
    marks = staged_marks(snap)

    print(f"Outline of {filepath} (stored numbering):")
    for hunk in snap["hunks"]:
        u3 = hunk[0]["u3"]
        leaves = [ln for ln in hunk if ln["t"] in ("+", "-")]
        plus = sum(1 for ln in leaves if ln["t"] == "+")
        minus = sum(1 for ln in leaves if ln["t"] == "-")
        subs = len({ln["u0"] for ln in leaves})
        header = hunk[0]["c"] if hunk[0]["t"] == "@@" else ""
        ctx = re.sub(r"@@ [^@]* @@", "", header).strip()
        first = leaves[0]["c"] if leaves else ""
        # Count staged among this hunk's leaves
        hunk_leaf_ids = []
        for ln, leaf in _iter_snap_lines(snap):
            if leaf is not None and ln["u3"] == u3:
                hunk_leaf_ids.append(id(leaf))
        staged = sum(1 for lid in hunk_leaf_ids if marks.get(lid))
        status = f", {staged}/{len(hunk_leaf_ids)} staged" if staged else ""
        loc = re.search(r"@@ ([^@]*) @@", header)
        loc_str = loc.group(1).strip() if loc else ""
        ctx_str = f" {ctx}" if ctx else ""
        print(f"  [{u3}] {loc_str}{ctx_str}  (+{plus}/-{minus}, "
              f"{subs} sub-hunk(s){status})  {first[:50]}")


def print_find(filepath: str, pattern: str):
    """Show changed lines matching a regex, with their stored IDs."""
    try:
        regex = re.compile(pattern)
    except re.error as e:
        print(f"Error: invalid regex '{pattern}': {e}")
        sys.exit(1)
    snap = read_view_snapshot(filepath)
    marks = staged_marks(snap)

    matches = []
    for ln, leaf in _iter_snap_lines(snap):
        if leaf is not None and regex.search(ln["c"][1:]):
            matches.append((ln, leaf))
    if not matches:
        print(f"No changed lines in {filepath} match '{pattern}'.")
        return
    print(f"Matches in {filepath} (stored numbering):")
    last_u3 = None
    for ln, leaf in matches:
        if ln["u3"] != last_u3:
            header = snap["hunks"][ln["u3"] - 1][0]["c"]
            print(f"  [{ln['u3']}] {header}")
            last_u3 = ln["u3"]
        idx_str = f"[{ln['u3']}.{ln['u0']}.{ln['li']}]"
        mark = "✓" if marks.get(id(leaf)) else " "
        print(f"    {idx_str:12}{mark} {ln['c'][:80]}")


def renumber_file(filepath: str, view: str = "outline", find_pattern: str | None = None):
    """Establish a fresh numbering frame from the current diff."""
    u3_hunks, _file_header = build_hierarchy(filepath)
    if not u3_hunks:
        try:
            os.remove(snapshot_path(filepath))
        except OSError:
            pass
        print(f"No changes in {filepath}")
        return
    snap = save_snapshot(filepath, u3_hunks)
    n_leaves = len(snap["leaves"])
    print(f"Renumbered {filepath}: {len(snap['hunks'])} hunk(s), "
          f"{n_leaves} change line(s).")
    if view == "print":
        print()
        print_full(filepath)
    elif view == "find" and find_pattern is not None:
        print()
        print_find(filepath, find_pattern)
    else:
        print_outline(filepath)


def repo_overview():
    """Summarize every changed file in the repository."""
    names = [n for n in run_git(["diff", "--name-only"]).split("\n") if n.strip()]
    if not names:
        print("No unstaged changes in the repository.")
        return
    print("Unstaged changes:")
    for name in names:
        u3_hunks, _ = build_hierarchy(name)
        plus = sum(1 for dl in iter_leaves(u3_hunks) if dl.line_type == "+")
        minus = sum(1 for dl in iter_leaves(u3_hunks) if dl.line_type == "-")
        print(f"  {name}: {len(u3_hunks)} hunk(s), +{plus}/-{minus}")
    print()
    print("Run stage-hunks.py <file> --print for IDs.")


# ── Selector handling ────────────────────────────────────────────────


def parse_selector(selector: str) -> list[tuple[int, int | None, int | None]]:
    """Parse a selector and expand ranges. Returns list of (u3, u0, line) tuples."""
    range_match = re.match(r"(\d+)(?:\.(\d+))?\.?\{(\d+)\.\.(\d+)\}", selector)
    if range_match:
        u3 = int(range_match.group(1))
        u0 = int(range_match.group(2)) if range_match.group(2) else None
        start = int(range_match.group(3))
        end = int(range_match.group(4))

        results = []
        for i in range(start, end + 1):
            if u0 is None:
                results.append((u3, i, None))
            else:
                results.append((u3, u0, i))
        return results

    parts = selector.split(".")
    u3 = int(parts[0])
    u0 = int(parts[1]) if len(parts) > 1 else None
    line = int(parts[2]) if len(parts) > 2 else None
    return [(u3, u0, line)]


def selector_id(item: tuple) -> str:
    """Human-readable form of a (u3, u0, line) selection tuple."""
    return ".".join(str(part) for part in item if part is not None)


def selection_matches_id(selection: tuple, leaf_id: list) -> bool:
    u3, u0, line = selection
    if leaf_id[0] != u3:
        return False
    if u0 is not None and leaf_id[1] != u0:
        return False
    if line is not None and leaf_id[2] != line:
        return False
    return True


def resolve_selectors(snap: dict, selectors: list[str], all_except: bool) -> list[dict]:
    """Expand selectors against the stored numbering; abort on dead IDs.

    Returns the selected snapshot leaves. With all_except=True the selectors
    are exclusions from the full set (and must still exist).
    """
    leaves = snap["leaves"]
    selections = []
    dead = []
    for sel in selectors:
        try:
            items = parse_selector(sel)
        except (ValueError, IndexError):
            print(f"Error: malformed selector '{sel}'")
            sys.exit(1)
        missing = [
            item for item in items
            if not any(selection_matches_id(item, leaf["id"]) for leaf in leaves)
        ]
        if missing:
            dead.append((sel, missing))
        selections.extend(items)

    if dead:
        print("Error: selectors that match nothing in the stored numbering:")
        for sel, missing in dead:
            ids = ", ".join(selector_id(m) for m in missing)
            print(f"  '{sel}' -> no such ID: {ids}")
        print("Nothing was done. Use --print to see the stored numbering, "
              "or --renumber if the changes have moved on.")
        sys.exit(1)

    def selected(leaf):
        hit = any(selection_matches_id(s, leaf["id"]) for s in selections)
        return not hit if all_except else hit

    return [leaf for leaf in leaves if selected(leaf)]


def leaf_id_str(leaf: dict) -> str:
    return ".".join(str(p) for p in leaf["id"])


# ── Patch building ───────────────────────────────────────────────────


def build_patch(u3_hunks: list, file_header: str, selected_lines: set) -> str:
    """Build a patch containing only the selected change lines.

    Applied forward to the index: unselected '+' lines are dropped,
    unselected '-' become context.
    """
    patch_lines = [file_header]

    for hunk in u3_hunks:
        hunk_has_selection = any(id(dl) in selected_lines for dl in hunk)
        if not hunk_has_selection:
            continue

        hunk_content = []
        old_count = 0
        new_count = 0
        header_line = None

        for diff_line in hunk:
            if diff_line.line_type == "@@":
                header_line = diff_line.content
            elif diff_line.line_type == " ":
                hunk_content.append(diff_line.content)
                old_count += 1
                new_count += 1
            elif diff_line.line_type == "+":
                if id(diff_line) in selected_lines:
                    hunk_content.append(diff_line.content)
                    new_count += 1
                # unselected '+': drop the line entirely
            elif diff_line.line_type == "-":
                if id(diff_line) in selected_lines:
                    hunk_content.append(diff_line.content)
                    old_count += 1
                else:
                    hunk_content.append(" " + diff_line.content[1:])
                    old_count += 1
                    new_count += 1

        if header_line:
            parsed = parse_hunk_header(header_line)
            if parsed:
                old_start, _, new_start, _, ctx = parsed
                new_header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{ctx}"
                patch_lines.append(new_header)
                patch_lines.extend(hunk_content)

    return "\n".join(patch_lines) + "\n"


def patch_counts(patch: str) -> tuple[int, int, int]:
    plus = sum(1 for ln in patch.splitlines()
               if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in patch.splitlines()
                if ln.startswith("-") and not ln.startswith("---"))
    n_hunks = sum(1 for ln in patch.splitlines() if ln.startswith("@@"))
    return plus, minus, n_hunks


def print_remaining(filepath: str, snap: dict):
    """Show what is still unstaged, in the numbering of the last --print."""
    u3_hunks, _ = build_hierarchy(filepath)
    assignment = assign_snapshot_to_current(snap, u3_hunks)
    remaining = [leaf for leaf, dl in zip(snap["leaves"], assignment) if dl is not None]
    if not remaining:
        print(f"All changes in the stored numbering of {filepath} are staged.")
        return
    print(f"Remaining unstaged in {filepath} (stored numbering):")
    by_u3 = {}
    for leaf in remaining:
        by_u3.setdefault(leaf["id"][0], []).append(leaf)
    for u3 in sorted(by_u3):
        leaves = by_u3[u3]
        first = leaves[0]["key"][2]
        suffix = f" (+{len(leaves) - 1} more line(s))" if len(leaves) > 1 else ""
        print(f"  [{u3}] {first[:70]}{suffix}")


# ── Staging ──────────────────────────────────────────────────────────


def stage_selections(filepath: str, selectors: list[str], all_except: bool = False,
                     dry_run: bool = False):
    """Stage the selected changes, addressed in the last-printed numbering."""
    snap = get_or_create_snapshot(filepath)

    if "--all" in selectors or "-a" in selectors:
        chosen = list(snap["leaves"])
    else:
        chosen = resolve_selectors(snap, selectors, all_except)

    if not chosen:
        print("Selection is empty (everything was excluded).")
        sys.exit(1)

    u3_hunks, file_header = build_hierarchy(filepath)
    assignment = assign_snapshot_to_current(snap, u3_hunks)
    assigned = {id(leaf): dl for leaf, dl in zip(snap["leaves"], assignment)}

    stageable = []
    already = []
    for leaf in chosen:
        dl = assigned.get(id(leaf))
        if dl is None:
            already.append(leaf)
        else:
            stageable.append(dl)

    if not stageable:
        ids = ", ".join(leaf_id_str(leaf) for leaf in already[:10])
        print(f"Error: nothing left to stage - the selected lines ({ids}"
              f"{', ...' if len(already) > 10 else ''}) are already staged "
              f"(or committed).")
        sys.exit(1)

    selected_lines = {id(dl) for dl in stageable}
    patch = build_patch(u3_hunks, file_header, selected_lines)

    plus, minus, n_hunks = patch_counts(patch)
    if dry_run:
        print(f"Would stage: +{plus}/-{minus} lines across {n_hunks} hunk(s)")
        if already:
            print(f"({len(already)} selected line(s) already staged)")
        print()
        print(patch, end="")
        return

    proc = subprocess.run(
        ["git", "apply", "--cached", "-v"],
        input=patch,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        print("Failed to apply patch:")
        print(proc.stderr)
        print("\nPatch was:")
        print(patch)
        sys.exit(1)

    print(f"Staged: +{plus}/-{minus} lines across {n_hunks} hunk(s)")
    if already:
        print(f"({len(already)} selected line(s) were already staged)")
    print_remaining(filepath, snap)


# ── Entry point ──────────────────────────────────────────────────────


USAGE = """\
Usage: stage-hunks.py [<file>] [selectors...] [flags]

Establish numbering (the only operation that renumbers):
  stage-hunks.py src/foo.py --renumber   # New frame + ack + outline
  stage-hunks.py src/foo.py --renumber --print        # ... + full listing
  stage-hunks.py src/foo.py --renumber --find <re>    # ... + content search

Read views (never renumber; render the stored frame with staged marks):
  stage-hunks.py --print                 # Overview of all changed files
  stage-hunks.py src/foo.py --print      # Full listing
  stage-hunks.py src/foo.py --print 2 5  # Only hunks 2 and 5
  stage-hunks.py src/foo.py --outline    # One line per hunk
  stage-hunks.py src/foo.py --find <re>  # Changed lines matching a regex

Stage (addressed in the stored numbering):
  stage-hunks.py src/foo.py 2 3.1 5.1.{1..3}
  stage-hunks.py src/foo.py --all
  stage-hunks.py src/foo.py --all-except 3 5.2
  stage-hunks.py src/foo.py 2 --dry-run  # Show the patch, change nothing

To undo staging, use git restore --staged <file>; the stored numbering stays
valid and the same IDs can be staged again.

IDs always refer to the stored numbering (established by the first contact
with a file, or explicitly by --renumber). Editing the file invalidates it."""


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(1)

    def take_flag(name):
        if name in args:
            args.remove(name)
            return True
        return False

    dry_run = take_flag("--dry-run")
    all_except = take_flag("--all-except")
    renumber = take_flag("--renumber")
    outline = take_flag("--outline")
    do_print = take_flag("--print") or take_flag("-p")

    find_pattern = None
    if "--find" in args:
        i = args.index("--find")
        if i + 1 >= len(args):
            print("Error: --find requires a regex argument")
            sys.exit(1)
        find_pattern = args[i + 1]
        del args[i:i + 2]

    if not args:
        if do_print:
            repo_overview()
            return
        print(USAGE)
        sys.exit(1)

    filepath = args[0]
    selectors = args[1:]

    if renumber:
        view = "print" if do_print else ("find" if find_pattern else "outline")
        renumber_file(filepath, view=view, find_pattern=find_pattern)
    elif find_pattern is not None:
        print_find(filepath, find_pattern)
    elif outline:
        print_outline(filepath)
    elif do_print:
        print_full(filepath, selectors or None)
    elif all_except:
        stage_selections(filepath, selectors, all_except=True, dry_run=dry_run)
    elif selectors:
        stage_selections(filepath, selectors, dry_run=dry_run)
    else:
        # Bare file argument: show the stored frame
        print_full(filepath)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Output was piped into something that stopped reading (head, grep -m);
        # exit quietly instead of dumping a traceback
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
