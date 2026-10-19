"""Redis is gone from this project and is not coming back.

The word must not appear anywhere in the repository - code, config, dependency
files, deploy files, docs, tests - except in git history and in THIS file (which
has to spell it to look for it). A stale setup note or a leftover ``.env`` key is
how a removed service gets quietly re-added, so the guard is deliberately broad.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

THIS = pathlib.Path(__file__).resolve()
PROJECT = THIS.parent.parent                     # .../predictions-bot
PATTERN = re.compile("redis", re.IGNORECASE)     # also catches aioredis / hiredis / REDIS_URL


def _repo_root() -> pathlib.Path:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=PROJECT,
                                      text=True, stderr=subprocess.DEVNULL).strip()
        return pathlib.Path(out)
    except (OSError, subprocess.CalledProcessError):
        return PROJECT.parent


def _repo_files(root: pathlib.Path):
    """Every file git would consider part of the repo: tracked + untracked, minus
    anything ignored (.env, .venv, caches, agent worktrees, the local database)."""
    try:
        raw = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                      cwd=root, stderr=subprocess.DEVNULL)
        rels = [r for r in raw.decode("utf-8", "replace").split("\0") if r]
        return [root / r for r in rels if (root / r).is_file()]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "__pycache__", ".pytest_cache", ".kilo", ".kilocode", "node_modules", "data"}
        return [p for p in root.rglob("*") if p.is_file() and not (skip & set(p.relative_to(root).parts))]


def offenders(files, root: pathlib.Path):
    """Files whose path or content mention the word (this file excluded)."""
    found = []
    for path in files:
        if path.resolve() == THIS:
            continue
        rel = path.relative_to(root).as_posix()
        # Bundled font licenses contain "redistributed"; retain their legal text verbatim.
        if rel in {'predictions-bot/assets/kovqi/OFL.txt', 'predictions-bot/assets/kovqi/OFL-Barlow.txt'}:
            continue
        if PATTERN.search(rel):
            found.append(f"{rel}  (file name)")
            continue
        try:
            text = path.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        hit = PATTERN.search(text)
        if hit:
            line = text.count("\n", 0, hit.start()) + 1
            found.append(f"{rel}:{line}")
    return found


def test_the_word_appears_nowhere_in_the_repo():
    root = _repo_root()
    files = _repo_files(root)

    assert len(files) > 50 and any(f.name == "config.py" for f in files)        # the scan is not vacuous
    assert offenders(files, root) == [], "Redis was removed on purpose; do not re-add it (see tests/test_no_redis.py)"


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production"])
def test_no_local_env_file_carries_a_redis_setting(name):
    """.env files are gitignored, so the repo scan cannot see them - check the ones
    the bot could actually load, so a stale key cannot come back on a dev machine."""
    for base in {PROJECT, _repo_root()}:
        f = base / name
        if f.is_file():
            assert not PATTERN.search(f.read_text(encoding="utf-8", errors="ignore")), \
                f"{f} still mentions Redis - delete that line (the value must not be printed)"


def test_the_detector_itself_works(tmp_path):
    """A guard that can't detect anything protects nothing."""
    (tmp_path / "a.py").write_text("import Redis\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("fine\nsee REDIS_URL\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("clean", encoding="utf-8")
    (tmp_path / "hiredis_notes.txt").write_text("clean content", encoding="utf-8")
    files = sorted(tmp_path.iterdir())

    assert sorted(offenders(files, tmp_path)) == ["a.py:1", "b.md:2", "hiredis_notes.txt  (file name)"]
