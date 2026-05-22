#!/usr/bin/env bash
# UserPromptSubmit hook — when the user opens with a discussion verb,
# inject a one-line reminder so the assistant defaults to discussion
# (no file writes / edits) until execution is explicitly authorized.
#
# Reads JSON from stdin (Claude Code UserPromptSubmit payload).
# Prints additionalContext to stdout (silent on no-match).
# Never blocks — pure nudge.

set -euo pipefail

payload="$(cat)"
prompt="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("prompt",""))' 2>/dev/null || true)"

# Only inspect the opening of the prompt (first 80 chars, lowercased)
head="$(printf '%s' "$prompt" | head -c 80 | tr '[:upper:]' '[:lower:]')"

# Discussion-mode verbs / phrases at the opening of the prompt
discussion_pattern='^[[:space:]]*(think through|plan |design |discuss |explore |consider |brainstorm |what about|how would you|should we|let.?s think|let.?s discuss|let.?s plan|talk through|walk me through)'

if [[ "$head" =~ $discussion_pattern ]]; then
  cat <<'EOF'
<discussion-mode-nudge>
The user opened with a discussion verb. Default to ANALYSIS ONLY:
- Do not write files, edit code, or dispatch agents.
- Produce inline analysis / options / tradeoffs.
- Wait for explicit execution verbs ("implement", "write it up", "ship",
  "build it", "do it", "apply") or plan approval ("yes, go", "looks good").
- When in doubt, ask: "Discussion or implementation?"
</discussion-mode-nudge>
EOF
fi
