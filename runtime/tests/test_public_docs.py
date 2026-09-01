import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class PublicDocumentationTests(unittest.TestCase):
    def test_every_runtime_environment_variable_is_documented(self):
        sources = []
        for root in (REPO_ROOT / "runtime" / "app", REPO_ROOT / "scripts"):
            for pattern in ("*.py", "*.ps1"):
                sources.extend(root.rglob(pattern))
        runtime_variables = set()
        for path in sources:
            runtime_variables.update(re.findall(
                r"ARGUS_[A-Z0-9_]+", path.read_text(encoding="utf-8")
            ))
        documentation = (
            (REPO_ROOT / "runtime" / "docs" / "CONFIGURATION.md").read_text(
                encoding="utf-8"
            )
            + (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        )
        missing = sorted(name for name in runtime_variables if name not in documentation)
        self.assertEqual(missing, [], f"undocumented environment variables: {missing}")

    def test_api_reference_lists_every_public_route(self):
        documentation = (
            REPO_ROOT / "runtime" / "docs" / "API_REFERENCE.md"
        ).read_text(encoding="utf-8")
        required_routes = {
            "/api/health", "/api/dashboard", "/api/market/quotes",
            "/api/market/minutes/{symbol}", "/api/assets/{symbol}/chart",
            "/api/stock-comparison", "/api/strategy-lab",
            "/api/quant/methodology", "/api/quant/decision",
            "/api/quant/mandates", "/api/quant/cache", "/api/signals",
            "/api/signals/{id}/acknowledge", "/api/signals/{id}/dismiss",
            "/api/models", "/api/models/{id}/activate", "/api/notifications",
            "/api/notification-subscriptions",
            "/api/notification-subscriptions/{id}/enable",
            "/api/notification-subscriptions/{id}/disable",
            "/api/notifications/test", "/api/notifications/send",
            "/api/portfolio/imports/preview", "/api/portfolio/imports/confirm",
            "/api/portfolio/clear", "/api/assets/resolve", "/api/harness",
            "/api/harness/runs", "/api/harness/runs/{run_key}",
            "/api/harness/runs/{run_key}/resume",
            "/api/harness/runs/{run_key}/cancel",
            "/api/harness/approvals/{approval_key}/resolve",
            "/api/harness/bad-cases", "/api/harness/candidates/generate",
            "/api/harness/evaluate", "/api/harness/autonomous/run",
            "/api/harness/candidates/approve", "/api/harness/versions/rollback",
            "/api/harness/code-evolution/run",
            "/api/harness/code-evolution/rollback", "/api/search/status",
            "/api/search", "/api/search/reindex", "/api/intelligence-sources",
            "/api/reports/library", "/api/reports/sync/status",
            "/api/reports/refresh", "/api/reports/stock",
            "/api/reports/sync/start", "/api/reports/sync/pause",
            "/api/research/backtest", "/api/research/factors/run",
            "/api/collect/bilibili", "/api/collect/x", "/api/collect/sec",
            "/api/risk/evaluate",
        }
        missing = sorted(route for route in required_routes if route not in documentation)
        self.assertEqual(missing, [], f"undocumented API routes: {missing}")

    def test_repository_does_not_document_machine_specific_user_paths(self):
        tracked_public_files = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "scripts" / "zcode_cli.ps1",
            REPO_ROOT / "runtime" / "docs" / "CONFIGURATION.md",
            REPO_ROOT / "runtime" / "docs" / "DATA_COVERAGE.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8")
                             for path in tracked_public_files)
        self.assertNotIn("zongtianyu", combined.lower())
        self.assertNotIn("Documents\\Codex", combined)

    def test_readme_local_links_resolve(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        local_targets = [
            target.split("#", 1)[0]
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
            if not target.startswith(("http://", "https://", "#"))
        ]
        missing = sorted(target for target in local_targets
                         if not (REPO_ROOT / target).exists())
        self.assertEqual(missing, [], f"broken README links: {missing}")

    def test_plugin_manifests_share_identity_and_version(self):
        codex = json.loads((
            REPO_ROOT / ".codex-plugin" / "plugin.json"
        ).read_text(encoding="utf-8"))
        zcode = json.loads((
            REPO_ROOT / ".zcode-plugin" / "plugin.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(codex["name"], zcode["name"])
        self.assertEqual(codex["version"], zcode["version"])
        self.assertEqual(codex["author"]["name"], zcode["author"]["name"])


if __name__ == "__main__":
    unittest.main()
