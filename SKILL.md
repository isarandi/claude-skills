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
# List all changes with IDs
~/.claude/skills/git-staging/stage-hunks.py src/file.py --print

# Stage all changes
~/.claude/skills/git-staging/stage-hunks.py src/file.py --all

# Stage by selector
~/.claude/skills/git-staging/stage-hunks.py src/file.py 2.1.3
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
Staged: 1.1.1, 2.1

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
# 1. View changes with IDs
~/.claude/skills/git-staging/stage-hunks.py src/file.py --print

# 2. Stage specific changes
~/.claude/skills/git-staging/stage-hunks.py src/file.py 1.1 2.3.{1..2}

# 3. Verify
git diff --staged

# 4. Commit
git commit -m "message"
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

After staging some hunks from a file, the remaining unstaged hunks are renumbered. Always re-run `--print` before staging more hunks from the same file.

## Limitations

- Git is line-based. Cannot split changes within a single line.
- For character-level changes, manually edit file to intermediate state.
