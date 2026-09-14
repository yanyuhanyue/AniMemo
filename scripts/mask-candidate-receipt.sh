# Read the dispatch payload before any step places the wire in its logged env.
set -euo pipefail
wire="$(jq -er '.inputs.candidate_acceptance_receipt_b64url // "" | select(type == "string")' "$GITHUB_EVENT_PATH")"
if [[ -n "$wire" ]]; then
  [[ "$wire" =~ ^[A-Za-z0-9_-]+$ ]] && (( ${#wire} <= 49152 ))
  printf '::add-mask::%s\n' "$wire"
fi
