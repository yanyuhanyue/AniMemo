from __future__ import annotations

from .canonical import SUPPORTED_FIELDS, field_value, fingerprint, validate_baselines

STATES = frozenset(
    (
        "uninitialized",
        "uninitialized_equal",
        "in_sync",
        "local_changed",
        "remote_changed",
        "converged",
        "conflict",
        "remote_missing",
        "unsupported",
    )
)


def _state(baseline, local, remote):
    if baseline is None:
        return "uninitialized_equal" if local == remote else "uninitialized"
    local_changed = local != baseline
    remote_changed = remote != baseline
    if not local_changed and not remote_changed:
        return "in_sync"
    if local_changed and not remote_changed:
        return "local_changed"
    if not local_changed and remote_changed:
        return "remote_changed"
    if local == remote:
        return "converged"
    return "conflict"


def _actions(state, *, pull_supported, push_supported):
    if state in {"in_sync", "unsupported", "remote_missing"}:
        return ["push_local"] if state == "remote_missing" and push_supported else []
    if state in {"uninitialized_equal", "converged"}:
        return ["accept_equal"]
    actions = []
    if pull_supported:
        actions.append("pull_remote")
    if push_supported:
        actions.append("push_local")
    return actions


def plan_collection(
    *,
    baseline,
    local,
    remote,
    push_capabilities,
    pull_capabilities=None,
    remote_missing=False,
):
    baseline = validate_baselines({} if baseline is None else baseline)
    fields = []
    for field in SUPPORTED_FIELDS:
        local_value = local[field]
        remote_value = field_value() if remote_missing else remote[field]
        baseline_value = baseline.get(field)
        push_capability = push_capabilities.get(field, {})
        push_supported = bool(push_capability.get("supported"))
        push_block_reason = (
            None
            if push_supported
            else str(push_capability.get("reason") or "provider_push_not_supported")
        )
        pull_capability = (pull_capabilities or {}).get(field, {})
        pull_supported = (
            bool(pull_capability.get("supported"))
            if pull_capabilities is not None
            else not remote_missing
        )
        pull_block_reason = (
            None
            if pull_supported
            else str(
                pull_capability.get("reason")
                or ("remote_collection_missing" if remote_missing else "remote_value_not_representable")
            )
        )
        if remote_missing:
            state = "remote_missing"
        elif not pull_supported:
            state = "unsupported"
        else:
            state = _state(baseline_value, local_value, remote_value)
        fields.append(
            {
                "field": field,
                "state": state,
                "baseline": baseline_value,
                "local": local_value,
                "remote": remote_value,
                "pull_supported": pull_supported,
                "pull_block_reason": pull_block_reason,
                "push_supported": push_supported,
                "push_block_reason": push_block_reason,
                "recommended_actions": _actions(
                    state,
                    pull_supported=pull_supported,
                    push_supported=push_supported,
                ),
            }
        )
    return {
        "schema_version": 1,
        "fields": fields,
        "fingerprints": {
            "baseline": fingerprint(baseline),
            "local": fingerprint(local),
            "remote": fingerprint({}) if remote_missing else fingerprint(remote),
        },
    }
