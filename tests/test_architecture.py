"""core / contracts 纯度检查（ADR-005）：不得导入 I/O、网络、进程、时钟模块，不得读墙钟。"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "qdii"
BANNED_MODULES = {
    "asyncio", "http", "httpx", "io", "os", "pathlib", "requests", "random", "shutil",
    "socket", "sqlite3", "subprocess", "time", "urllib",
    "qdii.io", "qdii.pipeline", "qdii.apps",
}
BANNED_CALLS = {"now", "utcnow", "today", "time_ns", "monotonic", "monotonic_ns"}


def _py_files(pkg: str):
    return sorted((SRC / pkg).rglob("*.py"))


@pytest.mark.parametrize("path", _py_files("core") + _py_files("contracts"), ids=lambda p: p.name)
def test_pure_layers(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not any(name == b or name.startswith(b + ".") for b in BANNED_MODULES), f"{path}: import {name}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in BANNED_CALLS, f"{path}:{node.lineno} calls .{node.func.attr}()"
