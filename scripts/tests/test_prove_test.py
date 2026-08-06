"""Regression tests for scripts/prove_test.sh's verdict.

PR review: a commit that changed only test files reported PROVEN. The fail step was
skipped -- no source was rolled back, so nothing was ever run against the bug -- yet the
verdict stayed zero and a green step 2 alone printed the one word the script exists to
withhold. PROVEN must require step 1 to have actually run and failed.

These build a throwaway git repository per case rather than stubbing, because the behaviour
under test IS the interaction between `git diff --name-status`, the rollback, and pytest.

Not wired into CI (nothing runs scripts/ today), and `make prove` cannot certify a change to
prove_test.sh itself -- it only rolls back *.py, so a .sh fix is never removed in step 1.
Run directly: python3 -m pytest scripts/tests/test_prove_test.py
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prove_test.sh"

PROVEN, REJECTED, ABORT, UNPROVEN = 0, 1, 2, 3


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    """A minimal repo laid out the way prove_test.sh expects: services/<svc>/tests/."""
    repo = tmp_path / "repo"
    (repo / "services" / "svc" / "tests").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "prove_test.sh").write_bytes(SCRIPT.read_bytes())
    (repo / "scripts" / "prove_test.sh").chmod(0o755)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _write(repo: Path, rel: str, body: str) -> None:
    (repo / rel).write_text(textwrap.dedent(body).lstrip())


def _run(repo: Path) -> subprocess.CompletedProcess:
    # PYTHONDONTWRITEBYTECODE is load-bearing, not hygiene. The script rolls a source file
    # back and forward again within the same second, and CPython decides a cached .pyc is
    # fresh from (mtime seconds, size) -- so two revisions of equal length inside one second
    # let step 2 import step 1's stale bytecode and fail against the rolled-back source.
    # The revisions below are also deliberately different lengths, so neither guard alone is
    # all that stands between this suite and an intermittent failure.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        ["./scripts/prove_test.sh", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_plain(repo: Path) -> subprocess.CompletedProcess:
    """Run the script WITHOUT PYTHONDONTWRITEBYTECODE in the environment.

    _run sets it for the caller, which masks whether the SCRIPT disables bytecode itself.
    The stale-.pyc-reuse guard has to hold when the caller does nothing, so this runner
    strips the variable and lets prove_test.sh be the only thing that can set it.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    return subprocess.run(
        ["./scripts/prove_test.sh", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_test_only_commit_is_unproven_not_proven(tmp_path):
    """The regression: tests changed, no source changed, tests pass -> must NOT be PROVEN."""
    repo = _repo(tmp_path)
    _write(repo, "services/svc/app.py", "def f():\n    return 1\n")
    _write(repo, "services/svc/tests/test_f.py", "def test_f():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    # Second commit touches ONLY the test file -- the shape that used to print PROVEN.
    _write(
        repo,
        "services/svc/tests/test_f.py",
        """
        def test_f():
            assert True

        def test_g():
            assert True
        """,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tests only")

    res = _run(repo)
    assert "PROVEN" not in res.stdout.replace("UNPROVEN", ""), res.stdout
    assert "UNPROVEN" in res.stdout, res.stdout
    assert res.returncode == UNPROVEN, res.stdout


def test_real_fix_with_a_red_test_is_proven(tmp_path):
    """The gate still passes what it should: a test that fails at the parent and passes here.

    Without this the fix above could be 'never say PROVEN', which would be equally wrong.
    """
    repo = _repo(tmp_path)
    _write(repo, "services/svc/app.py", "def f():\n    return 1\n")
    _write(
        repo,
        "services/svc/tests/test_f.py",
        "def test_placeholder():\n    assert True\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with the bug")

    # Fix the source AND add the test that catches it -- the shape prove_test.sh certifies.
    _write(repo, "services/svc/app.py", "def f():\n    return 22222\n")
    _write(
        repo,
        "services/svc/tests/test_f.py",
        """
        from app import f

        def test_f_returns_the_fixed_value():
            assert f() == 22222
        """,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix + regression test")

    res = _run(repo)
    assert "PROVEN" in res.stdout, res.stdout
    assert "UNPROVEN" not in res.stdout, res.stdout
    assert res.returncode == PROVEN, res.stdout


def test_toothless_test_is_rejected(tmp_path):
    """A test that passes even with the source rolled back proves nothing -> REJECTED."""
    repo = _repo(tmp_path)
    _write(repo, "services/svc/app.py", "def f():\n    return 1\n")
    _write(
        repo,
        "services/svc/tests/test_f.py",
        "def test_placeholder():\n    assert True\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    # Source changes, but the new test does not depend on the change at all.
    _write(repo, "services/svc/app.py", "def f():\n    return 22222\n")
    _write(
        repo,
        "services/svc/tests/test_f.py",
        """
        def test_placeholder():
            assert True

        def test_says_nothing():
            assert 1 + 1 == 2
        """,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix with a toothless test")

    res = _run(repo)
    assert "REJECTED" in res.stdout, res.stdout
    assert res.returncode == REJECTED, res.stdout


def test_commit_with_no_test_file_aborts(tmp_path):
    """Nothing to prove is an abort, distinct from both UNPROVEN and REJECTED."""
    repo = _repo(tmp_path)
    _write(repo, "services/svc/app.py", "def f():\n    return 1\n")
    _write(repo, "services/svc/tests/test_f.py", "def test_f():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _write(repo, "services/svc/app.py", "def f():\n    return 22222\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "source only")

    res = _run(repo)
    assert res.returncode == ABORT, res.stdout + res.stderr


def test_script_disables_bytecode_writes(tmp_path):
    """PR review (bytecode reuse): the script must disable bytecode itself, not rely on the
    caller. Run without PYTHONDONTWRITEBYTECODE and assert no .pyc survives -- writing one
    during a rollback run is the mechanism by which step 2 re-imports step 1's stale bytecode.
    The two revisions are the same byte length (return 1 / return 2), the collision case.
    """
    repo = _repo(tmp_path)
    _write(repo, "services/svc/app.py", "def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with the bug")

    _write(repo, "services/svc/app.py", "def f():\n    return 2\n")
    _write(
        repo,
        "services/svc/tests/test_f.py",
        """
        from app import f

        def test_f():
            assert f() == 2
        """,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix + regression test")

    res = _run_plain(repo)
    assert res.returncode == PROVEN, res.stdout
    leftover = [str(p) for p in (repo / "services").rglob("*.pyc")]
    assert not leftover, (
        f"prove_test.sh left compiled bytecode: {leftover}\n{res.stdout}"
    )


def test_fix_that_deletes_source_is_proven_and_leaves_no_debris(tmp_path):
    """PR review (deleted-source restore): a fix that DELETES a source file must restore to
    FIX (file gone, index clean). The old restore ran `git checkout FIX -- <deleted>`, which
    errored and left the parent copy -- step 2 then ran the bug (REJECTED) and the tree exited
    dirty with the file still staged.
    """
    repo = _repo(tmp_path)
    _write(repo, "services/svc/legacy.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base ships legacy")

    # The fix DELETES legacy.py; the test passes only once it is gone (import must fail).
    _git(repo, "rm", "-q", "services/svc/legacy.py")
    _write(
        repo,
        "services/svc/tests/test_gone.py",
        """
        import importlib

        def test_legacy_absent():
            try:
                importlib.import_module("legacy")
            except ModuleNotFoundError:
                return
            raise AssertionError("legacy should have been deleted")
        """,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete legacy + regression test")

    res = _run(repo)
    assert res.returncode == PROVEN, res.stdout
    # The deleted file must not be resurrected in the worktree or left staged in the index.
    assert not (repo / "services" / "svc" / "legacy.py").exists(), res.stdout
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "legacy.py" not in dirty, (
        f"deleted file left in the tree: {dirty!r}\n{res.stdout}"
    )


@pytest.mark.parametrize("label", ["PROVEN", "UNPROVEN", "REJECTED", "ABORT"])
def test_exit_codes_are_documented(label):
    """The codes are a contract now, so the header has to keep naming them."""
    header = SCRIPT.read_text().split("set -uo pipefail")[0]
    assert label in header
