"""Dependencies flow inward: integrations -> runtime -> platform. Entry points (cli, ui) sit outside.

A module may import its own layer or a layer further in, never one further out.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "livetest"
RANK = {"platform": 0, "runtime": 1, "integrations": 2}  # anything else in livetest/ is entry: 3


def layer(module: str) -> int:
    parts = module.split(".")
    return RANK.get(parts[1], 3) if len(parts) > 1 else 3


def test_imports_flow_inward():
    bad = []
    for file in PACKAGE.rglob("*.py"):
        own = layer(".".join(("livetest", *file.relative_to(PACKAGE).with_suffix("").parts)))
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{file}: use absolute imports (livetest.x.y)"
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            bad += [f"{file.relative_to(PACKAGE)} imports {n}"
                    for n in names if n.split(".")[0] == "livetest" and layer(n) > own]
    assert not bad, "imports pointing outward:\n" + "\n".join(bad)


def test_layer_ranking():
    assert layer("livetest.platform.db") < layer("livetest.runtime.store") < layer("livetest.integrations.otel")
    assert layer("livetest.cli") == layer("livetest") == 3
