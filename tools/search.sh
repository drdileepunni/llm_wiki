#!/bin/bash
# Usage: ./tools/search.sh "query terms"
# Searches wiki/ directory for matching pages, ranked by match count

QUERY="$*"
WIKI_DIR="$(dirname "$0")/../wiki"

echo "Searching wiki for: $QUERY"
echo "---"

grep -ril "$QUERY" "$WIKI_DIR" | while read -r file; do
    count=$(grep -ic "$QUERY" "$file")
    echo "$count $file"
done | sort -rn | head -20 | while read -r count file; do
    title=$(grep "^title:" "$file" | head -1 | sed 's/title: //')
    echo "[$count matches] $title — $file"
done
