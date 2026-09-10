"""Windows-safe script launcher (bash for .sh, sys.executable for .py)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import lib


def test_resolve_bash_posix_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(lib.os, "name", "posix")
    assert lib.resolve_bash() is None


def test_resolve_bash_windows_uses_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lib.os, "name", "nt")
    bash = tmp_path / "bash.exe"
    bash.write_text("")
    monkeypatch.setenv("EXTENSIONS_SERVICE_BASH", str(bash))
    assert lib.resolve_bash() == str(bash)


def test_resolve_bash_windows_rejects_wsl_only(monkeypatch) -> None:
    monkeypatch.setattr(lib.os, "name", "nt")
    monkeypatch.delenv("EXTENSIONS_SERVICE_BASH", raising=False)

    class Missing:
        def is_file(self) -> bool:
            return False

    monkeypatch.setattr(lib, "Path", lambda *_a, **_k: Missing())
    monkeypatch.setattr(
        lib.shutil, "which", lambda _n: r"C:\Windows\System32\bash.exe"
    )
    try:
        lib.resolve_bash()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "EXTENSIONS_SERVICE_BASH" in str(exc)


def test_run_service_script_uses_bash_on_windows(monkeypatch, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sh = scripts / "build_lambda_package.sh"
    sh.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(lib, "get_script_dir", lambda: scripts)
    monkeypatch.setattr(lib, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(lib, "resolve_bash", lambda: r"C:\Program Files\Git\bin\bash.exe")
    captured: dict = {}

    def fake_run(cmd, cwd=None, env=None):
        captured["cmd"] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    assert lib.run_service_script("build_lambda_package.sh") == 0
    assert captured["cmd"][0].endswith("bash.exe")
    assert captured["cmd"][1] == str(sh)


def test_run_service_script_py_uses_sys_executable(monkeypatch, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    py = scripts / "test_lambda_handler.py"
    py.write_text("print(1)\n")
    monkeypatch.setattr(lib, "get_script_dir", lambda: scripts)
    monkeypatch.setattr(lib, "get_workspace_root", lambda: tmp_path)
    captured: dict = {}

    def fake_run(cmd, cwd=None, env=None):
        captured["cmd"] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    assert lib.run_service_script("test_lambda_handler.py", extra_args=["h"]) == 0
    assert captured["cmd"][0] == lib.sys.executable
    assert captured["cmd"][1] == str(py)
    assert captured["cmd"][2] == "h"


def test_run_service_script_posix_runs_sh_directly(monkeypatch, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sh = scripts / "build_lambda_package.sh"
    sh.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(lib, "get_script_dir", lambda: scripts)
    monkeypatch.setattr(lib, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(lib, "resolve_bash", lambda: None)
    captured: dict = {}

    def fake_run(cmd, cwd=None, env=None):
        captured["cmd"] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    assert lib.run_service_script("build_lambda_package.sh") == 0
    assert captured["cmd"] == [str(sh)]
