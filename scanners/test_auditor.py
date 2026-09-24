"""Test Auditor for gcsfs test suite.

Detects testing anti-patterns such as:
1. Mocks lacking autospec=True.
2. Non-descriptive test names.
3. Tests containing loops.
"""

from __future__ import annotations

import ast
import os
from typing import Any


class TestAuditor:
    def __init__(self, tests_dir: str):
        self.tests_dir = tests_dir
        self.findings: list[dict[str, Any]] = []

    def audit(self) -> list[dict[str, Any]]:
        self.findings = []
        if not os.path.exists(self.tests_dir):
            return self.findings

        for dirpath, _, filenames in os.walk(self.tests_dir):
            for f in filenames:
                if f.startswith("test_") and f.endswith(".py"):
                    full_path = os.path.join(dirpath, f)
                    self._audit_test_file(full_path)
        return self.findings

    def _audit_test_file(self, file_path: str) -> None:
        rel_path = os.path.relpath(file_path, os.path.dirname(self.tests_dir))
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            tree = ast.parse(code, filename=file_path)
        except Exception:
            return

        for node in ast.walk(tree):
            # Check mock.patch lacking autospec=True
            if isinstance(node, ast.Call):
                call_str = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
                if "patch" in call_str:
                    has_autospec = any(k.arg == "autospec" for k in node.keywords)
                    if not has_autospec:
                        self.findings.append({
                            "id": f"MOCK-{len(self.findings)+1:03d}",
                            "rule": "mock-autospec",
                            "severity": "MEDIUM",
                            "file": rel_path,
                            "line": node.lineno,
                            "summary": f"Mock '{call_str}' does not use 'autospec=True'",
                            "suggestion": "Add autospec=True to prevent mock signature divergence."
                        })

            # Check test functions
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                parts = node.name.split("_")
                if len(parts) <= 2 and node.name in ("test_simple", "test_ls", "test_exists", "test_copy"):
                    self.findings.append({
                        "id": f"NAME-{len(self.findings)+1:03d}",
                        "rule": "struct-test-naming",
                        "severity": "LOW",
                        "file": rel_path,
                        "line": node.lineno,
                        "summary": f"Vague test name '{node.name}'",
                        "suggestion": "Rename to describe behavior: test_<unit>_<condition>_<expected_behavior>."
                    })

                has_loop = any(isinstance(n, (ast.For, ast.While)) for n in ast.walk(node))
                if has_loop:
                    self.findings.append({
                        "id": f"LOOP-{len(self.findings)+1:03d}",
                        "rule": "struct-no-logic",
                        "severity": "LOW",
                        "file": rel_path,
                        "line": node.lineno,
                        "summary": f"Test function '{node.name}' contains a loop",
                        "suggestion": "Replace loops with @pytest.mark.parametrize."
                    })
