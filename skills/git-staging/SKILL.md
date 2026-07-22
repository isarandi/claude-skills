---
name: git-staging
description: Stage git changes by hunk using line number identifiers. Use when committing changes selectively, staging specific hunks, preparing granular commits, or when git add -p workflow is needed.
---

# Git Staging by Hunk

Stage specific changes with 3-level granularity: U3 hunk > U0 sub-hunk > individual line.

## Script

`~/.claude/skills/git-staging/stage-hunks.py`

## Quick Start

```bash
S=~/.claude/skills/git-staging/stage-hunks.py

$S --print                      # Overview of all changed files
$S src/file.py --renumber       # Establish numbering: ack + outline
$S src/file.py --outline        # The map: one line per hunk, staged counts
$S src/file.py --find 'regex'   # Which IDs touch this content?
$S src/file.py --print          # Full listing, staged lines marked with ✓
$S src/file.py --print 2 5      # Full detail of hunks 2 and 5 only

$S src/file.py 2.1.3            # Stage line 3 of sub-hunk 2.1
$S src/file.py --all            # Stage everything
$S src/file.py --all-except 3 5.2
$S src/file.py --unstage 3      # Undo, by the same IDs
$S src/file.py 2 --dry-run      # Preview the patch, touch nothing
```

## Persistent numbering

**`--renumber` is the only operation that (re)numbers.** It stores the
numbering (in `.git/stage-hunks/`), prints a short acknowledgment plus the
outline (combine with `--print` or `--find` for a different first view).
Everything else — `--print`, `--outline`, `--find`, staging, unstaging —
renders and resolves in that stored frame and never renumbers, so one
numbering serves a whole series of commits, including with `git commit`
runs in between. `--print` marks already-staged lines with `✓`.

Two rules complete the picture:

- First contact with a file (no stored numbering yet) establishes one
  implicitly — no ceremony for the common case.
- Editing the file invalidates the stored numbering (content hash check);
  every operation then refuses until you run `--renumber`.

Re-staging an already-staged ID is an error, stale IDs are an error, and
nothing is ever staged partially on a failed call.

## Typical agent workflow

```bash
$S src/file.py --renumber              # once: numbering + outline
$S src/file.py --find 'helper_name'    # map content -> IDs (no grepping)
$S src/file.py 3 5.2 && git commit -m "first commit"
$S src/file.py --outline               # what's left, same IDs
$S src/file.py --all && git commit -m "second commit"
```

## Display Format

```
Changes in src/file.py:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[1] @@ -45,8 +45,12 @@ def calculate_total
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                 return sum(items)

[1.1.1]      +    def validate(self):
[1.1.2]      +        """Validate items."""
[1.1.3]      +        return all(i > 0 for i in self.items)

                 def get_average(self):
                     total = self.calculate_total()
[1.2.1]      -        return total / len(self.items)
[1.2.2]      +        count = len(self.items)
[1.2.3]      +        return total / count if count else 0

                 def reset(self):
```

**ID format: `[U3.U0.LINE]`**
- **U3**: Hunk with 3 lines of context (standard git hunk)
- **U0**: Sub-hunk with zero context (consecutive changes)
- **LINE**: Individual line within the U0 sub-hunk

Context lines (unchanged) show without IDs for orientation.

## Selectors

| Selector | Stages |
|----------|--------|
| `--all` | All changes |
| `2` | Entire U3 hunk 2 |
| `2.1` | U0 sub-hunk 1 of hunk 2 |
| `2.1.3` | Line 3 of sub-hunk 2.1 |
| `2.{1..3}` | U0 sub-hunks 1, 2, 3 of hunk 2 |
| `2.1.{2..4}` | Lines 2, 3, 4 of sub-hunk 2.1 |
| `1.1.1 2.3` | Multiple selectors (space-separated) |
| `2 3.1.3 4.2 5 7.1` | Mixed: whole hunks + sub-hunks + lines |

Note: Quoting range selectors (`'2.{1..3}'`) is advisable to prevent bash expansion, though unquoted works too since bash expands `2.{1..3}` to `2.1 2.2 2.3` which the script handles as multiple selectors.

## Interaction Example

```
$ ~/.claude/skills/git-staging/stage-hunks.py example.py --print
Changes in example.py:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[1] @@ -1,12 +1,21 @@
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[1.1.1]      +"""A simple calculator class."""
[1.1.2]      +
              class Calculator:
[1.2.1]      +    """Perform basic arithmetic operations."""
[1.2.2]      +
                  def __init__(self):
                      self.value = 0
[1.3.1]      +        self.history = []

                  def add(self, x):
[1.4.1]      +        """Add x to value."""
[1.4.2]      +        self.history.append(f"add {x}")
                      self.value += x
                      return self

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[2] @@ -15,12 +24,19 @@ class Calculator:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                  def divide(self, x):
[2.1.1]      +        if x == 0:
[2.1.2]      +            raise ValueError("Cannot divide by zero")
                      self.value /= x
                      return self

                  def reset(self):
                      self.value = 0
[2.2.1]      +        self.history.clear()
                      return self

Selectors: N (hunk) | N.M (sub-hunk) | N.M.L (line) | N.{1..3} (range)

$ ~/.claude/skills/git-staging/stage-hunks.py example.py 1.1.1 2.1
Staged: +3/-0 lines across 2 hunk(s)
Remaining unstaged in example.py (IDs from your last --print):
  [1] +    """Perform basic arithmetic operations."""  (+4 more line(s))
  [2] +        self.history.clear()

$ git diff --staged
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1,3 +1,4 @@
+"""A simple calculator class."""
 class Calculator:
     def __init__(self):
         self.value = 0
@@ -15,6 +16,8 @@ class Calculator:

     def divide(self, x):
+        if x == 0:
+            raise ValueError("Cannot divide by zero")
         self.value /= x
         return self
```

## Workflow

```bash
# 1. Establish numbering and see the outline
~/.claude/skills/git-staging/stage-hunks.py src/file.py --renumber

# 2. Stage specific changes (repeatable with the same numbering)
~/.claude/skills/git-staging/stage-hunks.py src/file.py 1.1 2.3.{1..2}

# 3. Verify
git diff --staged

# 4. Commit
git commit -m "message"

# 5. Keep staging the next commit from the SAME numbering - no renumbering
~/.claude/skills/git-staging/stage-hunks.py src/file.py 3 4.1
```

## How It Works

1. **U3 diff** (3 context lines): Groups nearby changes into hunks
2. **U0 diff** (0 context lines): Splits into minimal sub-hunks
3. **Line tracking**: Numbers each line within sub-hunks
4. **Patch building**: Constructs valid patches by:
   - Keeping selected `+` lines
   - Keeping selected `-` lines
   - Converting unselected `-` lines to context
   - Dropping unselected `+` lines
   - Recalculating hunk headers

## Important

- Selectors are **validated against the stored numbering**: any selector
  (or range element) that matches no printed ID aborts with exit code 1 and
  stages nothing. Selecting only already-staged lines is an error too.
- The `Staged:`/`Unstaged:` line reports what actually happened (counted
  from the applied patch), not an echo of your arguments. If the counts
  don't match your intent, inspect with `git diff --staged` before
  committing.
- `--unstage` handles changes staged since the last `--print` of a file
  that had **nothing staged at print time**; with pre-existing staged
  content (or after the staged lines were committed) it refuses and points
  you to `git restore --staged`.
- Identical deleted lines at the same position are interchangeable; the
  tool treats them as a group, which is semantically equivalent.

## Limitations

- Git is line-based. Cannot split changes within a single line.
- For character-level changes, manually edit file to intermediate state.
