"""Tests of the page's scripts that need no browser, run with node (skipped without it)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "hddfancontrol_webui" / "static"
NODE = shutil.which("node")


def function_source(script: str, name: str) -> str:
    """The source of a top-level function of a static script, up to its closing brace at the start of a line."""
    text = (STATIC / script).read_text(encoding="utf-8")
    match = re.search(rf"^function {name}\(.*?^\}}$", text, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    return match.group(0)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_every_survives_a_failing_round() -> None:
    """A round of every() that throws is logged and the next round is still scheduled."""
    program = function_source("app.js", "every") + """
const queue = [];
globalThis.setTimeout = (fn) => queue.push(fn);
globalThis.document = { hidden: false };
const errors = [];
console.error = (error) => errors.push(String(error));
let calls = 0;
every(10, async () => {
  calls += 1;
  if (calls === 1) throw new Error("network down");
});
(async () => {
  for (let i = 0; i < 3 && queue.length; i++) await queue.shift()();
  process.stdout.write(JSON.stringify({ calls, errors, pending: queue.length }));
})();
"""
    result = subprocess.run(
        [NODE, "-e", program], capture_output=True, text=True, timeout=30, check=True
    )  # noqa: S603  # node and our own script
    assert json.loads(result.stdout) == {"calls": 3, "errors": ["Error: network down"], "pending": 1}
