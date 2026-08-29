"""Packaging regression guard for the new top-level watchlist package."""

from __future__ import annotations

import tomllib
from pathlib import Path


IDO_COMMANDS = {
    "hermes-ido-scan",
    "hermes-ido-remind",
    "hermes-ido-setup",
}


def test_watchlist_is_in_the_packages_find_allowlist():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    include = config["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "watchlist" in include
    assert "watchlist.*" in include


def test_watchlist_commands_are_exposed_by_supported_installers():
    root = Path(__file__).resolve().parents[1]
    install_sh = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    install_ps1 = (root / "scripts" / "install.ps1").read_text(encoding="utf-8")
    nix_package = (root / "nix" / "hermes-agent.nix").read_text(encoding="utf-8")
    nix_checks = (root / "nix" / "checks.nix").read_text(encoding="utf-8")

    for command in IDO_COMMANDS:
        assert command in install_sh
        assert command in install_ps1
        assert command in nix_package
        assert command in nix_checks
