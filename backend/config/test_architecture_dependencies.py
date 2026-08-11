import ast
from pathlib import Path

from django.test import SimpleTestCase


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent


def parsed(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(path, package=""):
    modules = set()
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            modules.add(f"{package}.{module}".strip(".") if node.level else module)
    return modules


def function_parameters(path, function_name):
    function = next(
        node for node in ast.walk(parsed(path))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return {argument.arg for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}


class ArchitectureDependencyContractTests(SimpleTestCase):
    def test_auth_core_does_not_depend_on_web_http_adapters(self):
        path = BACKEND_ROOT / "journal" / "auth_tokens.py"
        imports = imported_modules(path, "journal")

        self.assertFalse(imports & {
            "journal.auth_views",
            "journal.web_auth_adapter",
            "rest_framework.response",
            "rest_framework.views",
        })
        self.assertNotIn("request", function_parameters(path, "rotate_refresh"))
        self.assertNotIn("request", function_parameters(path, "revoke_access_token"))

    def test_domain_service_does_not_depend_on_views_or_plugin_runtime(self):
        imports = imported_modules(BACKEND_ROOT / "journal" / "domain_services.py", "journal")

        self.assertFalse(imports & {
            "journal.entry_views",
            "plugin_host.runtime",
            "rest_framework.views",
            "rest_framework.viewsets",
        })

    def test_official_plugin_uses_only_the_public_host_sdk_surface(self):
        imports = imported_modules(REPOSITORY_ROOT / "plugins" / "watch-history-importer" / "backend" / "plugin.py")
        private_host_imports = {
            module for module in imports
            if module.startswith("plugin_host.") and module != "plugin_host.sdk"
        }

        self.assertEqual(private_host_imports, set())
        self.assertFalse(any(module == "journal" or module.startswith("journal.") for module in imports))
