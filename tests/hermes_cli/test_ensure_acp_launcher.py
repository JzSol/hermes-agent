"""``hermes update`` must self-heal public POSIX launchers."""

import os
import stat
from pathlib import Path

import pytest

from hermes_cli import main
from hermes_cli.main import _ensure_acp_launcher


IDO_COMMANDS = (
    "hermes-ido-scan",
    "hermes-ido-remind",
    "hermes-ido-setup",
)
MANAGED_MARKER = "# Hermes Agent - managed public launcher."


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    return bin_dir


def _create_private_ido_scripts(project_root: Path) -> Path:
    scripts = project_root / "venv" / "bin"
    scripts.mkdir(parents=True)
    for name in IDO_COMMANDS:
        source = scripts / name
        source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        source.chmod(0o755)
    return scripts


def test_update_installs_all_ido_launchers_from_private_venv(
    fake_home, tmp_path, monkeypatch
):
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    project_root = tmp_path / "project with spaces"
    scripts = _create_private_ido_scripts(project_root)
    monkeypatch.setattr(main, "PROJECT_ROOT", project_root)

    _ensure_acp_launcher()

    for name in IDO_COMMANDS:
        launcher = fake_home / name
        assert launcher.is_file()
        assert launcher.stat().st_mode & stat.S_IXUSR
        text = launcher.read_text(encoding="utf-8")
        assert MANAGED_MARKER in text
        assert str(scripts / name) in text
        assert '"$@"' in text


def test_update_does_not_replace_existing_ido_launcher(
    fake_home, tmp_path, monkeypatch
):
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    project_root = tmp_path / "project"
    _create_private_ido_scripts(project_root)
    monkeypatch.setattr(main, "PROJECT_ROOT", project_root)
    existing = fake_home / "hermes-ido-scan"
    existing.write_text("#!/bin/sh\n# user managed\n", encoding="utf-8")

    _ensure_acp_launcher()

    assert existing.read_text(encoding="utf-8") == "#!/bin/sh\n# user managed\n"


def test_update_installs_ido_launchers_in_termux_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    prefix = tmp_path / "data" / "data" / "com.termux" / "files" / "usr"
    termux_bin = prefix / "bin"
    termux_bin.mkdir(parents=True)
    (termux_bin / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    project_root = tmp_path / "project"
    _create_private_ido_scripts(project_root)
    monkeypatch.setattr(main, "PROJECT_ROOT", project_root)
    monkeypatch.setenv("PREFIX", str(prefix))
    monkeypatch.setenv("TERMUX_VERSION", "0.119")

    _ensure_acp_launcher()

    assert all((termux_bin / name).is_file() for name in IDO_COMMANDS)


def test_does_not_follow_symlink_into_venv(fake_home, tmp_path):
    """#21454 failure mode: never write through a symlinked hermes-acp."""
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    console_script = tmp_path / "venv" / "bin" / "hermes-acp"
    console_script.parent.mkdir(parents=True)
    marker = "#!/usr/bin/env python\n# real console script\n"
    console_script.write_text(marker, encoding="utf-8")
    (fake_home / "hermes-acp").symlink_to(console_script)

    _ensure_acp_launcher()

    assert console_script.read_text(encoding="utf-8") == marker
    assert (fake_home / "hermes-acp").is_symlink()


def test_unwritable_bin_dir_is_skipped(fake_home):
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    if not hasattr(os, "geteuid"):
        _ensure_acp_launcher()
        assert not (fake_home / "hermes-acp").exists()
        return
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write permissions")
    fake_home.chmod(0o555)
    try:
        _ensure_acp_launcher()  # must not raise
        assert not (fake_home / "hermes-acp").exists()
    finally:
        fake_home.chmod(0o755)
