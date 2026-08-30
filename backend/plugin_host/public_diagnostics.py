import re

from config.api_errors import public_failure

PLUGIN_RUNTIME_UNAVAILABLE = "plugin_runtime_unavailable"
PLUGIN_SCAN_FAILED = "plugin_scan_failed"
_REPORT_BOOLEAN_FIELDS = frozenset(
    {
        "contains_backend",
        "uses_external_network",
        "stores_personal_data",
        "accepts_file_uploads",
    }
)
_REPORT_INTEGER_FIELDS = frozenset(
    {"file_count", "package_size", "uncompressed_size"}
)
_PYTHON_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_HOOK_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_PERMISSION_CODE = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z][a-z0-9_-]*)+$"
)
_CSS_SELECTOR = re.compile(r"^[a-z0-9_.#*:\-\[\]=\"' ()>+~]+$")
_PERMISSION_ROLES = frozenset(
    {"reviewer", "user_manager", "operator", "administrator"}
)
_DANGEROUS_CALL = re.compile(
    r"^backend/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py:"
    r"[1-9][0-9]{0,6} uses (?:eval|exec|compile|__import__|os\.system|os\.popen)$"
)
_DANGEROUS_IMPORT = re.compile(
    r"^dangerous import: "
    r"(?:ctypes|django|multiprocessing|os|pathlib|resource|shutil|signal|socket|subprocess|sys)"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)


def runtime_failure(request):
    return public_failure(
        request=request,
        candidate_code=PLUGIN_RUNTIME_UNAVAILABLE,
        status_code=503,
    )


def scan_failure(request):
    return public_failure(
        request=request,
        candidate_code=PLUGIN_SCAN_FAILED,
        status_code=400,
    )


def stable_runtime_error(value):
    """Collapse both current markers and arbitrary legacy text to one safe code."""
    return PLUGIN_RUNTIME_UNAVAILABLE if value else ""


def stable_registry_errors(errors):
    """Registry diagnostics cross an API boundary later, so keep only codes here."""
    return [PLUGIN_RUNTIME_UNAVAILABLE] if errors else []


def _stable_finding(value):
    if value == PLUGIN_SCAN_FAILED:
        return PLUGIN_SCAN_FAILED
    if isinstance(value, str) and (
        _DANGEROUS_CALL.fullmatch(value) or _DANGEROUS_IMPORT.fullmatch(value)
    ):
        return value
    return PLUGIN_SCAN_FAILED


def _stable_string_list(value, pattern, *, maximum_items=256, maximum_length=200):
    if not isinstance(value, list) or len(value) > maximum_items:
        return None
    stable = []
    for item in value:
        if (
            not isinstance(item, str)
            or len(item) > maximum_length
            or pattern.fullmatch(item) is None
        ):
            return None
        stable.append(item)
    return stable


def _stable_permissions(value):
    if not isinstance(value, list) or len(value) > 256:
        return None
    stable = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"code", "roles"}:
            return None
        code = item.get("code")
        roles = item.get("roles")
        if (
            not isinstance(code, str)
            or len(code) > 200
            or _PERMISSION_CODE.fullmatch(code) is None
            or not isinstance(roles, list)
            or len(roles) != len(set(roles))
            or not all(isinstance(role, str) and role in _PERMISSION_ROLES for role in roles)
        ):
            return None
        stable.append({"code": code, "roles": list(roles)})
    return stable


def stable_security_report(report):
    """Project a stored report through the only accepted persistence schema."""
    if not isinstance(report, dict):
        return {"dangerous_findings": [PLUGIN_SCAN_FAILED]} if report else {}
    projected = {}
    for field in _REPORT_BOOLEAN_FIELDS:
        value = report.get(field)
        if isinstance(value, bool):
            projected[field] = value
    for field in _REPORT_INTEGER_FIELDS:
        value = report.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            projected[field] = value
    invalid_list = False
    list_specs = (
        ("hooks", _HOOK_NAME, 64, 80),
        ("backend_imports", _PYTHON_MODULE, 256, 200),
        ("css_global_selectors", _CSS_SELECTOR, 256, 200),
    )
    for field, pattern, maximum_items, maximum_length in list_specs:
        if field not in report:
            continue
        stable = _stable_string_list(
            report.get(field),
            pattern,
            maximum_items=maximum_items,
            maximum_length=maximum_length,
        )
        if stable is None:
            invalid_list = True
        else:
            projected[field] = stable
    if "permissions" in report:
        permissions = _stable_permissions(report.get("permissions"))
        if permissions is None:
            invalid_list = True
        else:
            projected["permissions"] = permissions

    findings = report.get("dangerous_findings")
    if isinstance(findings, list):
        stable_findings = [_stable_finding(item) for item in findings]
    elif findings:
        stable_findings = [PLUGIN_SCAN_FAILED]
    else:
        stable_findings = []
    if invalid_list:
        stable_findings.append(PLUGIN_SCAN_FAILED)
    if stable_findings or "dangerous_findings" in report:
        projected["dangerous_findings"] = list(dict.fromkeys(stable_findings))
    return projected


def public_security_report(report, *, request):
    """Exact-project stored scan data without replaying legacy diagnostics."""
    projected = stable_security_report(report)
    findings = projected.get("dangerous_findings", [])
    projected["dangerous_findings"] = [
        scan_failure(request) if item == PLUGIN_SCAN_FAILED else item
        for item in findings
    ]
    return projected


def public_runtime_error(value, *, request):
    return runtime_failure(request) if value else ""


def public_plugin_payload(plugin, *, request):
    """Turn request-independent registry codes into correlated public failures."""
    projected = dict(plugin)
    errors = projected.get("errors")
    if errors:
        projected["errors"] = [runtime_failure(request)]
    diagnostics = projected.get("diagnostics")
    if isinstance(diagnostics, dict):
        diagnostics = dict(diagnostics)
        diagnostics["last_error"] = public_runtime_error(
            diagnostics.get("last_error"),
            request=request,
        )
        projected["diagnostics"] = diagnostics
    return projected
