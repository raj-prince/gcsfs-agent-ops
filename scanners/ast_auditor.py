"""AST Auditor for gcsfs Python source code.

Detects anti-patterns such as:
1. Fire-and-forget asyncio.create_task() calls.
2. Silent exception swallowing (except: pass).
3. Exception re-raising lacking 'from e' chaining.
"""

from __future__ import annotations

import ast
import os
from typing import Any


class CodebaseAuditor:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.findings: list[dict[str, Any]] = []

    def audit(self) -> list[dict[str, Any]]:
        self.findings = []
        for dirpath, _, filenames in os.walk(self.root_dir):
            if "tests" in dirpath or ".git" in dirpath or ".venv" in dirpath:
                continue
            for f in filenames:
                if f.endswith(".py"):
                    full_path = os.path.join(dirpath, f)
                    self._audit_file(full_path)
        return self.findings

    def _audit_file(self, file_path: str) -> None:
        rel_path = os.path.relpath(file_path, self.root_dir)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            tree = ast.parse(code, filename=file_path)
        except Exception:
            return

        parent_map = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p):
                parent_map[c] = p

        for node in ast.walk(tree):
            # 1. Fire-and-forget create_task
            if isinstance(node, ast.Call):
                call_str = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
                if "create_task" in call_str:
                    parent = parent_map.get(node)
                    if isinstance(parent, ast.Expr):
                        self.findings.append({
                            "id": f"TASK-{len(self.findings)+1:03d}",
                            "rule": "async-create-task",
                            "severity": "HIGH",
                            "file": rel_path,
                            "line": node.lineno,
                            "summary": f"Unassigned fire-and-forget '{call_str}' without retained reference.",
                            "suggestion": "Store task reference in a set or attach a done_callback to avoid premature GC and missed exceptions."
                        })

            # 2. Silent exception swallowing
            if isinstance(node, ast.ExceptHandler):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    exc_name = ast.unparse(node.type) if node.type and hasattr(ast, "unparse") else "bare except"
                    self.findings.append({
                        "id": f"ERR-{len(self.findings)+1:03d}",
                        "rule": "error-no-silent-exceptions",
                        "severity": "MEDIUM",
                        "file": rel_path,
                        "line": node.lineno,
                        "summary": f"Silent exception handler: 'except {exc_name}: pass'",
                        "suggestion": "Log the exception or add an explicit comment detailing why ignoring is safe."
                    })

            # 3. Exception re-raising lacking 'from e' chaining
            if isinstance(node, ast.Raise):
                if node.exc is not None and node.cause is None:
                    curr = node
                    in_except = False
                    while curr in parent_map:
                        curr = parent_map[curr]
                        if isinstance(curr, ast.ExceptHandler):
                            in_except = True
                            break
                    if in_except:
                        raise_str = ast.unparse(node) if hasattr(ast, "unparse") else "raise ..."
                        self.findings.append({
                            "id": f"CHAIN-{len(self.findings)+1:03d}",
                            "rule": "error-exception-chaining",
                            "severity": "LOW",
                            "file": rel_path,
                            "line": node.lineno,
                            "summary": f"Exception raised inside except block without explicit cause: '{raise_str}'",
                            "suggestion": "Use 'raise ... from exc' to preserve traceback context."
                        })
