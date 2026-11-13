"""Source provenance checks; all Git repositories below are synthetic and temporary."""
import subprocess
from pathlib import Path

from tooling.package import build_candidate


def git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Synthetic Source",
         "-c", "user.email=source@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def repository(path):
    path.mkdir()
    git(path, "init", "-q")
    (path / "source.txt").write_text("SYNTHETIC source\n", encoding="utf-8")
    git(path, "add", "source.txt")
    git(path, "commit", "-q", "-m", "Synthetic baseline")
    return path


def test_archive_build_identity_does_not_need_a_git_executable(tmp_path, monkeypatch):
    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("An archive must not require Git.")

    monkeypatch.setattr(build_candidate, "run", unexpected_git)
    assert build_candidate.source_identity(tmp_path) == {
        "source_kind": "archive", "source_commit": None, "source_dirty": None,
    }


def test_source_archive_does_not_borrow_an_enclosing_repository_identity(tmp_path):
    parent = repository(tmp_path / "parent")
    source = parent / "downloaded-source"
    source.mkdir()
    (source / "source.txt").write_text("SYNTHETIC archive contents", encoding="utf-8")
    assert Path(git(source, "rev-parse", "--show-toplevel")).resolve() == parent.resolve()
    assert build_candidate.source_identity(source) == {
        "source_kind": "archive", "source_commit": None, "source_dirty": None,
    }


def test_git_source_preserves_actual_commit_and_dirty_state(tmp_path):
    source = repository(tmp_path / "checkout")
    expected = git(source, "rev-parse", "HEAD")
    assert build_candidate.source_identity(source) == {
        "source_kind": "git", "source_commit": expected, "source_dirty": False,
    }
    (source / "source.txt").write_text("SYNTHETIC uncommitted fix\n", encoding="utf-8")
    assert build_candidate.source_identity(source) == {
        "source_kind": "git", "source_commit": expected, "source_dirty": True,
    }
