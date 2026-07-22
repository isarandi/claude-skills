#!/bin/bash
# Stage specific git hunks by their @@ line identifiers
#
# Usage:
#   stage-hunks.sh <file>                       # List hunks (-U3)
#   stage-hunks.sh <file> --split <N>           # Show sub-hunks of hunk N
#   stage-hunks.sh <file> -- <patterns...>      # Stage matching hunks
#   stage-hunks.sh <file> --split <N> -- <pat>  # Stage sub-hunk
#
# Examples:
#   stage-hunks.sh src/foo.py                   # List all hunks
#   stage-hunks.sh src/foo.py --split 2         # Split hunk 2 into sub-hunks
#   stage-hunks.sh src/foo.py -- +625 +640      # Stage hunks matching +625 or +640

set -e

FILE="$1"
shift || true

if [ -z "$FILE" ]; then
    echo "Usage: stage-hunks.sh <file> [--split N] [-- patterns...]"
    exit 1
fi

# Parse --split flag
SPLIT_HUNK=""
if [ "$1" = "--split" ]; then
    shift
    SPLIT_HUNK="$1"
    shift
fi

# Skip -- separator if present
if [ "$1" = "--" ]; then
    shift
fi

PATTERNS=("$@")

# Get -U3 hunk ranges for mapping
get_u3_ranges() {
    local file="$1"
    git diff -U3 -- "$file" | grep "^@@" | while read -r line; do
        # Parse @@ -old,len +new,len @@ context
        if [[ "$line" =~ @@\ -([0-9]+),?([0-9]*)\ \+([0-9]+),?([0-9]*)\ @@ ]]; then
            local new_start="${BASH_REMATCH[3]}"
            local new_len="${BASH_REMATCH[4]:-1}"
            local new_end=$((new_start + new_len))
            echo "$new_start $new_end $line"
        fi
    done
}

# Find which -U3 hunk a line belongs to
find_parent_hunk() {
    local line_num="$1"
    local hunk_num=0
    while read -r start end header; do
        hunk_num=$((hunk_num + 1))
        if [ "$line_num" -ge "$start" ] && [ "$line_num" -lt "$end" ]; then
            echo "$hunk_num"
            return
        fi
    done
}

# Display hunks with content
display_hunks() {
    local file="$1"
    local context="$2"

    echo "Hunks in $file:"
    echo ""

    local hunk_num=0
    git diff -U"$context" -- "$file" | tail -n +5 | while IFS= read -r line; do
        if [[ "$line" =~ ^@@ ]]; then
            hunk_num=$((hunk_num + 1))
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            echo "[$hunk_num] $line"
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        else
            echo "$line"
        fi
    done
}

# Get the line range of -U3 hunk N
get_hunk_range() {
    local file="$1"
    local target_hunk="$2"
    local hunk_num=0

    git diff -U3 -- "$file" | grep "^@@" | while read -r line; do
        hunk_num=$((hunk_num + 1))
        if [ "$hunk_num" -eq "$target_hunk" ]; then
            if [[ "$line" =~ @@\ -([0-9]+),?([0-9]*)\ \+([0-9]+),?([0-9]*)\ @@ ]]; then
                local new_start="${BASH_REMATCH[3]}"
                local new_len="${BASH_REMATCH[4]:-1}"
                echo "$new_start $((new_start + new_len))"
            fi
            break
        fi
    done
}

# Display sub-hunks of a specific -U3 hunk
display_subhunks() {
    local file="$1"
    local parent_hunk="$2"

    # Get parent hunk range
    local range
    range=$(get_hunk_range "$file" "$parent_hunk")
    if [ -z "$range" ]; then
        echo "Hunk $parent_hunk not found"
        exit 1
    fi

    local range_start range_end
    read -r range_start range_end <<< "$range"

    echo "Sub-hunks of hunk $parent_hunk (lines $range_start-$range_end):"
    echo ""

    local subhunk_num=0
    local in_range=0

    git diff -U0 -- "$file" | tail -n +5 | while IFS= read -r line; do
        if [[ "$line" =~ ^@@\ -([0-9]+),?([0-9]*)\ \+([0-9]+),?([0-9]*)\ @@ ]]; then
            local new_start="${BASH_REMATCH[3]}"

            # Check if this -U0 hunk is within our -U3 hunk range
            if [ "$new_start" -ge "$range_start" ] && [ "$new_start" -lt "$range_end" ]; then
                in_range=1
                subhunk_num=$((subhunk_num + 1))
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                echo "[$parent_hunk.$subhunk_num] $line"
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            else
                in_range=0
            fi
        elif [ "$in_range" -eq 1 ]; then
            echo "$line"
        fi
    done
}

# Build a patch with context for a -U0 hunk
build_contextual_patch() {
    local file="$1"
    local pattern="$2"
    local context_lines=3

    # Get the -U0 diff
    local u0_diff
    u0_diff=$(git diff -U0 -- "$file")

    # Get file header
    local header
    header=$(echo "$u0_diff" | head -4 | grep -E "^(diff|index|---|\+\+\+)")

    local patch="$header"
    local found=0

    # Find matching hunk
    local in_hunk=0
    local hunk_header=""
    local hunk_content=""
    local old_start=0 old_len=0 new_start=0 new_len=0

    while IFS= read -r line; do
        if [[ "$line" =~ ^@@\ -([0-9]+),?([0-9]*)\ \+([0-9]+),?([0-9]*)\ @@(.*) ]]; then
            # Process previous hunk if it matched
            if [ "$in_hunk" -eq 1 ] && [ "$found" -eq 1 ]; then
                break
            fi

            old_start="${BASH_REMATCH[1]}"
            old_len="${BASH_REMATCH[2]:-0}"
            new_start="${BASH_REMATCH[3]}"
            new_len="${BASH_REMATCH[4]:-0}"
            local context_text="${BASH_REMATCH[5]}"

            hunk_header="$line"
            hunk_content=""
            in_hunk=1

            # Check if matches pattern
            if [[ "$line" == *"$pattern"* ]]; then
                found=1
            else
                found=0
            fi
        elif [ "$in_hunk" -eq 1 ]; then
            hunk_content="$hunk_content"$'\n'"$line"
        fi
    done <<< "$(echo "$u0_diff" | tail -n +5)"

    if [ "$found" -eq 0 ]; then
        echo ""
        return 1
    fi

    # Now build patch with context from the working file
    # Read context before (from original line numbers)
    local ctx_before_start=$((old_start - context_lines))
    [ "$ctx_before_start" -lt 1 ] && ctx_before_start=1
    local ctx_before_len=$((old_start - ctx_before_start))

    local ctx_before=""
    if [ "$ctx_before_len" -gt 0 ]; then
        ctx_before=$(git show HEAD:"$file" 2>/dev/null | sed -n "${ctx_before_start},$((old_start - 1))p" | sed 's/^/ /')
    fi

    # Read context after (from original line numbers)
    local ctx_after_start=$((old_start + old_len))
    local ctx_after_end=$((ctx_after_start + context_lines - 1))
    local ctx_after=""
    if [ "$context_lines" -gt 0 ]; then
        ctx_after=$(git show HEAD:"$file" 2>/dev/null | sed -n "${ctx_after_start},${ctx_after_end}p" | sed 's/^/ /')
    fi

    # Calculate new header with context
    local total_old=$((ctx_before_len + old_len + context_lines))
    local total_new=$((ctx_before_len + new_len + context_lines))
    local new_old_start=$ctx_before_start
    local new_new_start=$((new_start - ctx_before_len))
    [ "$new_new_start" -lt 1 ] && new_new_start=1

    # Build final patch
    patch="$patch"$'\n'"@@ -${new_old_start},${total_old} +${new_new_start},${total_new} @@"
    [ -n "$ctx_before" ] && patch="$patch"$'\n'"$ctx_before"
    patch="$patch$hunk_content"
    [ -n "$ctx_after" ] && patch="$patch"$'\n'"$ctx_after"

    echo "$patch"
}

# Stage hunks matching patterns
stage_hunks() {
    local file="$1"
    shift
    local patterns=("$@")

    local diff
    diff=$(git diff -U3 -- "$file")

    if [ -z "$diff" ]; then
        echo "No changes in $file"
        return 0
    fi

    # Build patch with header
    local patch
    patch=$(echo "$diff" | head -4 | grep -E "^(diff|index|---|\+\+\+)")

    local in_match=0
    local matched_any=0

    while IFS= read -r line; do
        if [[ "$line" =~ ^@@ ]]; then
            in_match=0
            for pattern in "${patterns[@]}"; do
                if [[ "$line" == *"$pattern"* ]]; then
                    in_match=1
                    matched_any=1
                    break
                fi
            done
        fi

        if [ "$in_match" -eq 1 ]; then
            patch="$patch"$'\n'"$line"
        fi
    done <<< "$(echo "$diff" | tail -n +5)"

    if [ "$matched_any" -eq 0 ]; then
        echo "No hunks matched patterns: ${patterns[*]}"
        echo "Available hunks:"
        echo "$diff" | grep "^@@"
        return 1
    fi

    echo "$patch" | git apply --cached
    echo "Staged hunks matching: ${patterns[*]}"
}

# Main logic
if [ -n "$SPLIT_HUNK" ]; then
    if [ ${#PATTERNS[@]} -eq 0 ]; then
        # Just display sub-hunks
        display_subhunks "$FILE" "$SPLIT_HUNK"
    else
        # Stage sub-hunks with reconstructed context
        for pattern in "${PATTERNS[@]}"; do
            patch=$(build_contextual_patch "$FILE" "$pattern")
            if [ -n "$patch" ]; then
                echo "$patch" | git apply --cached
                echo "Staged sub-hunk matching: $pattern"
            else
                echo "No sub-hunk matched: $pattern"
            fi
        done
    fi
elif [ ${#PATTERNS[@]} -eq 0 ]; then
    # List mode
    display_hunks "$FILE" 3
    echo ""
    echo "Tip: Use --split N to see sub-hunks of hunk N"
else
    # Stage mode with -U3
    stage_hunks "$FILE" "${PATTERNS[@]}"
fi
