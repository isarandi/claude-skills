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


def build_hierarchy(filepath: str, cached: bool = False) -> tuple[list[list[DiffLine]], str]:
    """Build 3-level hierarchy by comparing U3 and U0 diffs.

    Returns:
        - List of U3 hunks, each containing DiffLine objects with hierarchy IDs
        - File header string
    """
    extra = ["--cached"] if cached else []
    u3_diff = run_git(["diff", "-U3", *extra, "--", filepath])
    u0_diff = run_git(["diff", "-U0", *extra, "--", filepath])

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
    prestaged = bool(run_git(["diff", "--cached", "--name-only", "--", filepath]).strip())
    snap = {
        "file": filepath,
        "worktree_sha": worktree_sha(filepath),
        "prestaged": prestaged,
        "leaves": [
            {"id": [dl.u3_idx, dl.u0_idx, dl.line_idx], "key": list(leaf_key(dl))}
            for dl in iter_leaves(u3_hunks)
        ],
    }
    with open(snapshot_path(filepath), "w") as f:
        json.dump(snap, f)
    return snap


def load_snapshot(filepath: str) -> dict | None:
    try:
        with open(snapshot_path(filepath)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


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
        print(f"Error: {filepath} was modified since the last --print; "
              f"its stored numbering no longer matches the file.")
        print("Re-run --print to get fresh IDs.")
        sys.exit(1)
    return snap


# ── Display ──────────────────────────────────────────────────────────


def display_hunks(filepath: str):
    """Display all hunks with 3-level IDs and store the numbering."""
    u3_hunks, file_header = build_hierarchy(filepath)

    if not u3_hunks:
        # Also drop any stale stored numbering
        try:
            os.remove(snapshot_path(filepath))
        except OSError:
            pass
        print(f"No changes in {filepath}")
        return

    print(f"Changes in {filepath}:")
    print()

    for hunk in u3_hunks:
        for diff_line in hunk:
            if diff_line.line_type == "@@":
                print("━" * 60)
                print(f"[{diff_line.u3_idx}] {diff_line.content}")
                print("━" * 60)
            elif diff_line.line_type in ("+", "-"):
                idx_str = f"[{diff_line.u3_idx}.{diff_line.u0_idx}.{diff_line.line_idx}]"
                print(f"{idx_str:12} {diff_line.content}")
            else:
                print(f"{'':12} {diff_line.content}")
        print()

    print("Selectors: N (hunk) | N.M (sub-hunk) | N.M.L (line) | N.{1..3} (range)")
    save_snapshot(filepath, u3_hunks)
    print("IDs stored: they stay valid until the next --print, even across stagings.")


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
        print("Error: selectors that match nothing in the numbering of the "
              "last --print:")
        for sel, missing in dead:
            ids = ", ".join(selector_id(m) for m in missing)
            print(f"  '{sel}' -> no such ID: {ids}")
        print("Nothing was done. Re-run --print to see current changes.")
        sys.exit(1)

    def selected(leaf):
        hit = any(selection_matches_id(s, leaf["id"]) for s in selections)
        return not hit if all_except else hit

    return [leaf for leaf in leaves if selected(leaf)]


def leaf_id_str(leaf: dict) -> str:
    return ".".join(str(p) for p in leaf["id"])


# ── Patch building ───────────────────────────────────────────────────


def build_patch(u3_hunks: list, file_header: str, selected_lines: set,
                mode: str = "stage") -> str:
    """Build a patch containing only the selected change lines.

    mode='stage' (applied forward to the index):
        unselected '+' lines are dropped, unselected '-' become context.
    mode='unstage' (applied in reverse to the index, on the cached diff):
        unselected '-' lines are dropped (absent from the index either way),
        unselected '+' become context (present in the index and staying).
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
                elif mode == "unstage":
                    hunk_content.append(" " + diff_line.content[1:])
                    old_count += 1
                    new_count += 1
                # stage mode: drop the line entirely
            elif diff_line.line_type == "-":
                if id(diff_line) in selected_lines:
                    hunk_content.append(diff_line.content)
                    old_count += 1
                elif mode == "stage":
                    hunk_content.append(" " + diff_line.content[1:])
                    old_count += 1
                    new_count += 1
                # unstage mode: drop the line entirely

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
        print(f"All changes from the last --print of {filepath} are staged.")
        return
    print(f"Remaining unstaged in {filepath} (IDs from your last --print):")
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
    patch = build_patch(u3_hunks, file_header, selected_lines, mode="stage")

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


# ── Unstaging ────────────────────────────────────────────────────────


def unstage_selections(filepath: str, selectors: list[str], dry_run: bool = False):
    """Reverse-apply selected changes from the index, by last-printed IDs.

    Only supports changes staged since the last --print of a file that had
    nothing staged at print time: with pre-existing staged content, lines in
    the cached diff cannot be attributed to printed IDs unambiguously.
    """
    snap = load_snapshot(filepath)
    if snap is None:
        print(f"Error: no stored numbering for {filepath}; run --print first.")
        sys.exit(1)
    if snap["worktree_sha"] != worktree_sha(filepath):
        print(f"Error: {filepath} was modified since the last --print; "
              f"re-run --print (unstage needs a trustworthy numbering).")
        sys.exit(1)
    if snap.get("prestaged"):
        print("Error: the file already had staged changes when --print ran; "
              "IDs cannot be matched against the index unambiguously. "
              "Use git restore --staged for coarse unstaging.")
        sys.exit(1)

    if "--all" in selectors or "-a" in selectors:
        chosen = list(snap["leaves"])
    else:
        chosen = resolve_selectors(snap, selectors, all_except=False)

    # Anything still assigned to the unstaged diff is not staged
    unstaged_hunks, _ = build_hierarchy(filepath)
    assignment = assign_snapshot_to_current(snap, unstaged_hunks)
    assigned = {id(leaf): dl for leaf, dl in zip(snap["leaves"], assignment)}
    targets = [leaf for leaf in chosen if assigned.get(id(leaf)) is None]
    skipped = [leaf for leaf in chosen if assigned.get(id(leaf)) is not None]

    if not targets:
        print("Error: none of the selected lines are currently staged.")
        sys.exit(1)

    # The cached diff consists exactly of snapshot lines staged since the
    # print (prestaged was empty), in file order - match them one to one.
    cached_hunks, cached_header = build_hierarchy(filepath, cached=True)
    cached_leaves = list(iter_leaves(cached_hunks))
    staged_snap = [leaf for leaf in snap["leaves"] if assigned.get(id(leaf)) is None]
    if len(cached_leaves) != len(staged_snap) or any(
        c.line_type != s["key"][0] or c.content != s["key"][2]
        for c, s in zip(cached_leaves, staged_snap)
    ):
        print("Error: the index changed outside this tool (or a commit was "
              "made); cannot match IDs against the staged content. "
              "Use git restore --staged for coarse unstaging.")
        sys.exit(1)

    target_ids = {id(leaf) for leaf in targets}
    selected_lines = {
        id(c) for c, s in zip(cached_leaves, staged_snap) if id(s) in target_ids
    }

    patch = build_patch(cached_hunks, cached_header, selected_lines, mode="unstage")
    plus, minus, n_hunks = patch_counts(patch)

    if dry_run:
        print(f"Would unstage: +{plus}/-{minus} lines across {n_hunks} hunk(s)")
        if skipped:
            print(f"({len(skipped)} selected line(s) are not staged)")
        print()
        print(patch, end="")
        return

    proc = subprocess.run(
        ["git", "apply", "--cached", "--reverse", "-v"],
        input=patch,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        print("Failed to reverse-apply patch:")
        print(proc.stderr)
        print("\nPatch was:")
        print(patch)
        sys.exit(1)

    print(f"Unstaged: +{plus}/-{minus} lines across {n_hunks} hunk(s)")
    if skipped:
        print(f"({len(skipped)} selected line(s) were not staged to begin with)")
    print_remaining(filepath, snap)


# ── Entry point ──────────────────────────────────────────────────────


USAGE = """\
Usage: stage-hunks.py [<file>] [selectors...] [flags]

Examples:
  stage-hunks.py --print                # Overview of all changed files
  stage-hunks.py src/foo.py --print     # List changes, store the numbering
  stage-hunks.py src/foo.py --all       # Stage all changes
  stage-hunks.py src/foo.py 2           # Stage U3 hunk 2
  stage-hunks.py src/foo.py 2.1         # Stage U0 sub-hunk 2.1
  stage-hunks.py src/foo.py 2.1.3       # Stage line 3 of sub-hunk 2.1
  stage-hunks.py src/foo.py '2.{1..3}'  # Stage sub-hunks 2.1, 2.2, 2.3
  stage-hunks.py src/foo.py 1 3.2 5.1.1 # Mixed selectors
  stage-hunks.py src/foo.py --all-except 3 5.2   # Everything but 3 and 5.2
  stage-hunks.py src/foo.py --unstage 3          # Reverse hunk 3 from the index
  stage-hunks.py src/foo.py 2 --dry-run          # Show the patch, change nothing

IDs always refer to the numbering of the LAST --print of the file; staging
never renumbers. Editing the file invalidates the stored numbering."""


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(1)

    dry_run = "--dry-run" in args
    unstage = "--unstage" in args
    all_except = "--all-except" in args
    args = [a for a in args if a not in ("--dry-run", "--unstage", "--all-except")]

    if args == ["--print"] or args == ["-p"]:
        repo_overview()
        return

    if not args:
        print(USAGE)
        sys.exit(1)

    filepath = args[0]
    selectors = args[1:]

    if not selectors or selectors == ["--print"] or selectors == ["-p"]:
        display_hunks(filepath)
    elif unstage:
        unstage_selections(filepath, selectors, dry_run=dry_run)
    elif all_except:
        stage_selections(filepath, selectors, all_except=True, dry_run=dry_run)
    else:
        stage_selections(filepath, selectors, dry_run=dry_run)


if __name__ == "__main__":
    main()
