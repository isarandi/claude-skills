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
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class DiffLine:
    """A line from the diff with its metadata."""
    content: str  # Full line including +/- prefix
    line_type: str  # '+', '-', or ' '
    u3_idx: int = 0
    u0_idx: int = 0
    line_idx: int = 0  # Line within U0 hunk (1-indexed, 0 for context)


def run_git(args: list[str]) -> str:
    """Run a git command and return output."""
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
    # First, get U0 hunk boundaries
    u0_hunks = []  # List of (new_start, new_end, old_start, old_end)
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

    # Track line positions for matching with U0
    old_line = 0
    new_line = 0

    # Track which U0 hunks belong to current U3 hunk
    def get_u0_idx_for_position(old_pos, new_pos, change_type):
        """Find which U0 sub-hunk this position belongs to."""
        relevant_u0s = []
        for i, u0 in enumerate(u0_hunks):
            # Check if U0 is within current U3 range
            in_u3 = (
                (u0["new_start"] >= current_u3_new_start and u0["new_start"] < current_u3_new_end) or
                (u0["old_start"] >= current_u3_old_start and u0["old_start"] < current_u3_old_end)
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

    # Track line counts within each U0 sub-hunk
    u0_line_counts = {}  # u0_idx -> count

    for line in lines[diff_start:]:
        if line.startswith("@@"):
            # Save previous hunk
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

            # Add header as a special line
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
        elif line == "":
            # Could be end of diff or empty context line
            if current_hunk and current_hunk[-1].line_type != "@@":
                pass  # Skip trailing empty

    # Save last hunk
    if current_hunk:
        u3_hunks.append(current_hunk)

    return u3_hunks, file_header


def display_hunks(filepath: str):
    """Display all hunks with 3-level IDs."""
    u3_hunks, file_header = build_hierarchy(filepath)

    if not u3_hunks:
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


def parse_selector(selector: str) -> list[tuple[int, int | None, int | None]]:
    """Parse a selector and expand ranges. Returns list of (u3, u0, line) tuples."""
    # Handle range syntax: 2.{1..3} or 2.1.{1..3}
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

    # Simple selectors: 2, 2.1, 2.1.3
    parts = selector.split(".")
    u3 = int(parts[0])
    u0 = int(parts[1]) if len(parts) > 1 else None
    line = int(parts[2]) if len(parts) > 2 else None
    return [(u3, u0, line)]


def matches_selection(diff_line: DiffLine, selections: set) -> bool:
    """Check if a diff line matches any selection."""
    if diff_line.line_type not in ("+", "-"):
        return False

    for u3, u0, line in selections:
        if diff_line.u3_idx != u3:
            continue
        if u0 is None:
            # Whole U3 hunk selected
            return True
        if diff_line.u0_idx != u0:
            continue
        if line is None:
            # Whole U0 sub-hunk selected
            return True
        if diff_line.line_idx == line:
            return True

    return False


def build_patch(u3_hunks: list, file_header: str, selections: set) -> str:
    """Build a patch containing only selected lines.

    Unselected + lines are dropped.
    Unselected - lines become context (space prefix).
    """
    patch_lines = [file_header]

    for hunk in u3_hunks:
        # Check if any line in this hunk is selected
        hunk_has_selection = any(
            matches_selection(dl, selections)
            for dl in hunk
        )
        if not hunk_has_selection:
            continue

        # Process hunk
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
                if matches_selection(diff_line, selections):
                    hunk_content.append(diff_line.content)
                    new_count += 1
                # else: drop the line entirely
            elif diff_line.line_type == "-":
                if matches_selection(diff_line, selections):
                    hunk_content.append(diff_line.content)
                    old_count += 1
                else:
                    # Convert to context line
                    hunk_content.append(" " + diff_line.content[1:])
                    old_count += 1
                    new_count += 1

        # Rebuild header with correct counts
        if header_line:
            parsed = parse_hunk_header(header_line)
            if parsed:
                old_start, _, new_start, _, ctx = parsed
                new_header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{ctx}"
                patch_lines.append(new_header)
                patch_lines.extend(hunk_content)

    return "\n".join(patch_lines) + "\n"


def stage_selections(filepath: str, selectors: list[str]):
    """Stage the selected hunks/lines."""
    u3_hunks, file_header = build_hierarchy(filepath)

    if not u3_hunks:
        print(f"No changes in {filepath}")
        return

    # Expand all selectors
    selections = set()

    # Handle --all: select all U3 hunks
    if "--all" in selectors or "-a" in selectors:
        for i in range(1, len(u3_hunks) + 1):
            selections.add((i, None, None))
    else:
        for sel in selectors:
            for item in parse_selector(sel):
                selections.add(item)

    # Build and apply patch
    patch = build_patch(u3_hunks, file_header, selections)

    if not patch.strip():
        print("No valid selections matched")
        return

    # Apply the patch
    proc = subprocess.run(
        ["git", "apply", "--cached", "-v"],
        input=patch,
        text=True,
        capture_output=True,
    )

    if proc.returncode != 0:
        print(f"Failed to apply patch:")
        print(proc.stderr)
        print("\nPatch was:")
        print(patch)
    else:
        print(f"Staged: {', '.join(selectors)}")


def main():
    if len(sys.argv) < 2:
        print("Usage: stage-hunks.py <file> [selectors...]")
        print()
        print("Examples:")
        print("  stage-hunks.py src/foo.py --print     # List all changes")
        print("  stage-hunks.py src/foo.py --all       # Stage all changes")
        print("  stage-hunks.py src/foo.py 2           # Stage U3 hunk 2")
        print("  stage-hunks.py src/foo.py 2.1         # Stage U0 sub-hunk 2.1")
        print("  stage-hunks.py src/foo.py 2.1.3       # Stage line 3 of sub-hunk 2.1")
        print("  stage-hunks.py src/foo.py '2.{1..3}'  # Stage sub-hunks 2.1, 2.2, 2.3")
        print("  stage-hunks.py src/foo.py 1 3.2 5.1.1 # Mixed selectors")
        sys.exit(1)

    filepath = sys.argv[1]
    selectors = sys.argv[2:]

    if not selectors or selectors == ["--print"] or selectors == ["-p"]:
        display_hunks(filepath)
    else:
        stage_selections(filepath, selectors)


if __name__ == "__main__":
    main()
