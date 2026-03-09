#!/bin/bash
# afterFileEdit hook: syntax-check edited Python files.
# Receives JSON on stdin with file_path and edits.

input=$(cat)
file_path=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))" 2>/dev/null)

if [[ "$file_path" == *.py ]] && [[ -f "$file_path" ]]; then
  result=$(python3 -m py_compile "$file_path" 2>&1)
  if [[ $? -ne 0 ]]; then
    echo "{\"additional_context\": \"Syntax error in $file_path: $result\"}"
    exit 0
  fi
fi

echo '{}'
exit 0
