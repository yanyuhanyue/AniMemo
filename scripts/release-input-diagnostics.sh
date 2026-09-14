# Sourced by read-only publication input steps; never print commands or values.
release_input_stage_name=INITIALIZING
release_input_diagnostic() {
  local status="$1" code="$2" line="$3"
  local record
  printf -v record '{"schema":"animemo.release-input-diagnostic/v1","stage":"%s","status":"%s","exit_code":%s,"line":%s}' \
    "$release_input_stage_name" "$status" "$code" "$line"
  printf '%s\n' "$record" >&2
  printf '%s\n' "$record" >> "$RUNNER_TEMP/release-input-diagnostics.jsonl"
}
release_input_stage() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]{0,63}$ ]] || return 2
  release_input_stage_name="$1"
  release_input_diagnostic START null null
}
trap 'release_input_diagnostic FAIL "$?" "$LINENO"' ERR
