export function initialImportAction(row) {
  const requiresDecision = row?.match_state === "already_bound" || row?.match_state === "possible_local_match";
  return {
    selected: false,
    mode: requiresDecision ? "SKIP" : "CREATE_NEW",
    local_entry_id: row?.possible_local_matches?.[0]?.id || "",
    apply_fields: [],
  };
}

export function buildImportItems(actions) {
  return Object.entries(actions || {})
    .filter(([, action]) => action?.selected && action.mode !== "SKIP")
    .map(([external_id, action]) => ({
      external_id,
      mode: action.mode,
      local_entry_id: action.local_entry_id || null,
      apply_fields: action.apply_fields || [],
    }));
}

export function importResultCount(actions) {
  return buildImportItems(actions).length;
}
