import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


def test_java_example_compiles():
    if shutil.which("javac") is None:
        pytest.skip("本机无 javac，Java 编译验证待外部资源")
    root = Path(__file__).resolve().parents[1]
    source = root / "examples" / "java" / "V1ChatClient.java"
    with tempfile.TemporaryDirectory() as out:
        proc = subprocess.run(
            ["javac", "-encoding", "UTF-8", "-d", out, str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, proc.stderr
