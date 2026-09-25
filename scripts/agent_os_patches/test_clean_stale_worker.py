"""Tests for the agent-os#18 workaround `scripts/clean_stale_worker.sh`, squash-aware since #187.

Real git in temporary directories (a bare origin, a "main checkout" clone and a worker worktree
next to it, laid out as `<main>/../studentassistant-<backend>`), a fake `gh` that prints canned
REST output. No network. Run with the mechanism's interpreter:

    agent_os/.venv/bin/pytest scripts/agent_os_patches -q
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "clean_stale_worker.sh"
BACKEND = "bot"
ISSUE = "41"
BRANCH = f"task/{ISSUE}-thing"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True, capture_output=True)
    git(main, "switch", "-q", "-c", "main")
    (main / "README").write_text("base\n")
    git(main, "add", "README")
    git(main, "commit", "-q", "-m", "base")
    git(main, "push", "-q", "origin", "main")
    (main / ".cache").mkdir()
    (main / ".cache" / f"worker_{BACKEND}.issue").write_text(ISSUE + "\n")

    wt = tmp_path / f"studentassistant-{BACKEND}"
    git(main, "worktree", "add", "-q", "-b", BRANCH, str(wt), "origin/main")
    for i in (1, 2):
        (wt / f"f{i}.txt").write_text(f"stage {i}\n")
        git(wt, "add", f"f{i}.txt")
        git(wt, "commit", "-q", "-m", f"stage {i}")
    git(wt, "push", "-q", "origin", BRANCH)
    (wt / "scratchpad").mkdir()
    (wt / "scratchpad" / "progress.log").write_text("diary\n")
    (wt / "scratchpad" / "notes.py").write_text("scratch\n")

    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$FAKE_GH_LOG"\n'
        '[ -n "${FAKE_GH_FAIL:-}" ] && exit 1\n'
        'printf "%s" "${FAKE_GH_OUT:-}"\n'
    )
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
    return {"tmp": tmp_path, "main": main, "wt": wt, "gh": fake_gh, "log": tmp_path / "gh.log"}


def squash_merge(w: dict) -> None:
    main = w["main"]
    git(main, "merge", "-q", "--squash", f"origin/{BRANCH}")
    git(main, "commit", "-q", "-m", f"Squashed {BRANCH} (#159)")
    git(main, "push", "-q", "origin", "main")


def run(w: dict, *args: str, gh_out: str = "", gh_fail: bool = False):
    env = dict(
        os.environ,
        SA_STALE_MAIN=str(w["main"]),
        SA_STALE_GH=str(w["gh"]),
        SA_STALE_REPO="owner/repo",
        FAKE_GH_LOG=str(w["log"]),
        FAKE_GH_OUT=gh_out,
    )
    if gh_fail:
        env["FAKE_GH_FAIL"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT), *args, BACKEND], env=env, capture_output=True, text=True
    )


def gh_calls(w: dict) -> list[str]:
    return w["log"].read_text().splitlines() if w["log"].exists() else []


def branches(w: dict) -> list[str]:
    return git(w["main"], "branch", "--format=%(refname:short)").split()


def assert_cleaned(w: dict) -> None:
    wt = w["wt"]
    assert git(wt, "rev-parse", "HEAD") == git(w["main"], "rev-parse", "origin/main")
    assert not (wt / "scratchpad").exists()
    archived = list((w["main"] / ".cache" / "stale_diaries").iterdir())
    assert len(archived) == 1 and (archived[0] / "notes.py").exists()
    assert BRANCH not in branches(w)


def assert_untouched(w: dict, head: str) -> None:
    assert git(w["wt"], "symbolic-ref", "--short", "HEAD") == BRANCH
    assert git(w["wt"], "rev-parse", "HEAD") == head
    assert (w["wt"] / "scratchpad" / "progress.log").exists()
    assert BRANCH in branches(w)


def test_squash_merged_pr_containing_head_is_cleaned_with_one_rest_call(world):
    squash_merge(world)
    head = git(world["wt"], "rev-parse", "HEAD")
    r = run(world, gh_out=f"159 {head}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr #159" in r.stdout
    calls = gh_calls(world)
    assert len(calls) == 1
    assert calls[0].startswith(f"api repos/owner/repo/pulls?head=owner:{BRANCH}&state=closed")
    assert_cleaned(world)


def test_squash_merged_without_pr_answer_falls_back_to_content(world):
    squash_merge(world)
    r = run(world, gh_fail=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "content" in r.stdout
    assert_cleaned(world)


def test_ancestry_merge_needs_no_github_call(world):
    git(world["main"], "merge", "-q", "--no-ff", "-m", "merge", f"origin/{BRANCH}")
    git(world["main"], "push", "-q", "origin", "main")
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ancestry" in r.stdout
    assert gh_calls(world) == []
    assert_cleaned(world)


def test_unmerged_work_is_refused_and_left_alone(world):
    head = git(world["wt"], "rev-parse", "HEAD")
    r = run(world, gh_out="")
    assert r.returncode == 1
    assert "refusing" in r.stdout
    assert_untouched(world, head)


def test_commit_after_the_squash_merge_is_refused(world):
    squash_merge(world)
    merged_head = git(world["wt"], "rev-parse", "HEAD")
    (world["wt"] / "f1.txt").write_text("fix after merge\n")
    git(world["wt"], "commit", "-q", "-am", "late fix")
    head = git(world["wt"], "rev-parse", "HEAD")
    r = run(world, gh_out=f"159 {merged_head}\n")
    assert r.returncode == 1
    assert "late fix" in r.stdout
    assert_untouched(world, head)


def test_untracked_file_outside_scratchpad_is_refused(world):
    squash_merge(world)
    head = git(world["wt"], "rev-parse", "HEAD")
    (world["wt"] / "stray.txt").write_text("work\n")
    r = run(world, gh_out=f"159 {head}\n")
    assert r.returncode == 1
    assert "stray.txt" in r.stdout
    assert_untouched(world, head)


def test_dry_run_reports_and_touches_nothing(world):
    squash_merge(world)
    head = git(world["wt"], "rev-parse", "HEAD")
    r = run(world, "--dry-run", gh_out=f"159 {head}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"would delete branch {BRANCH}" in r.stdout
    assert_untouched(world, head)
    assert not (world["main"] / ".cache" / "stale_diaries").exists()
