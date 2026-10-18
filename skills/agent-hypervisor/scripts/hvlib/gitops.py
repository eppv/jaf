"""Git operations: worktrees, branches, commits, merges, spec materialisation.

Every git side effect in the hypervisor goes through this module, so the
"dispatcher is the only one who writes git" invariant has a single address.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitError",
    "GitResult",
    "git",
    "ensure_repo",
    "worktree_add",
    "worktree_remove",
    "worktree_list",
    "is_dirty",
    "commit_all",
    "head_sha",
    "ref_exists",
    "tag_exists",
    "create_tag",
    "merge_no_ff",
    "changed_files",
    "create_orphan_branch",
    "materialize_spec",
    "commit_spec_env",
    "env_sha",
    "setup_excludes",
    "delete_branch",
]

IDENTITY = ["-c", "user.name=hv dispatcher", "-c", "user.email=hv@localhost"]

# Dispatcher-owned exclusions live in .git/info/exclude, so the project's own
# .gitignore stays the project's business. Gate runs (compileall, venv, node)
# must not leak build noise into the code history.
DEFAULT_EXCLUDES = [
    "run/",
    "worktrees/",
    ".hv/",
    ".spec/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".venv/",
    "node_modules/",
    "target/",
    "dist/",
]


class GitError(RuntimeError):
    """A git command that the dispatcher depends on has failed."""


@dataclass
class GitResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __str__(self) -> str:
        return (self.stdout + self.stderr).strip()


def git(cwd: Path | str, *args: str, check: bool = False, input_text: str | None = None) -> GitResult:
    cwd = Path(cwd)
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_text,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    result = GitResult(list(args), cwd, proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {result}")
    return result


def ensure_repo(root: Path, init: bool = False) -> None:
    root = Path(root)
    result = git(root, "rev-parse", "--git-dir")
    if result.ok:
        return
    if not init:
        raise GitError(f"{root} is not a git repository")
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main", check=True)
    # make the first commit possible without a user identity configured
    git(root, *IDENTITY, "commit", "--allow-empty", "-q", "-m", "hv: initial commit", check=True)


def head_sha(cwd: Path, ref: str = "HEAD") -> str:
    result = git(cwd, "rev-parse", ref)
    if not result.ok:
        raise GitError(f"cannot resolve {ref} in {cwd}: {result}")
    return result.stdout.strip()


def ref_exists(cwd: Path, ref: str) -> bool:
    return git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").ok


def tag_exists(cwd: Path, name: str) -> bool:
    return git(cwd, "rev-parse", "--verify", "--quiet", f"refs/tags/{name}").ok


def create_tag(cwd: Path, name: str, message: str) -> None:
    git(cwd, *IDENTITY, "tag", "-a", name, "-m", message, check=True)


def delete_branch(cwd: Path, name: str) -> None:
    git(cwd, "branch", "-D", name, check=False)


def worktree_list(cwd: Path) -> list[dict]:
    result = git(cwd, "worktree", "list", "--porcelain", check=True)
    entries: list[dict] = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def worktree_add(cwd: Path, path: Path, branch: str | None = None, from_ref: str | None = None,
                 detach: bool = False) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", "-q"]
    if detach:
        args.append("--detach")
    elif branch:
        args += ["-b", branch]
    args.append(str(path))
    if from_ref:
        args.append(from_ref)
    git(cwd, *args, check=True)
    return path


def worktree_remove(cwd: Path, path: Path, force: bool = True) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    git(cwd, *args, check=False)
    git(cwd, "worktree", "prune", check=False)


def is_dirty(path: Path) -> bool:
    result = git(path, "status", "--porcelain")
    return bool(result.stdout.strip())


def commit_all(path: Path, message: str, allow_empty: bool = True) -> bool:
    """Stage everything and commit. Returns True when a commit was created."""
    git(path, "add", "-A", check=True)
    args = [*IDENTITY, "commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    result = git(path, *args)
    if not result.ok:
        raise GitError(f"commit failed in {path}: {result}")
    return True


def changed_files(path: Path, base: str, head: str = "HEAD") -> list[str]:
    result = git(path, "diff", "--name-only", f"{base}...{head}")
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def merge_no_ff(worktree: Path, branch: str, message: str) -> tuple[bool, str]:
    result = git(worktree, *IDENTITY, "merge", "--no-ff", "--no-edit", "-m", message, branch)
    if result.ok:
        return True, str(result)
    git(worktree, "merge", "--abort", check=False)
    return False, str(result)


def create_orphan_branch(root: Path, branch: str, files: dict[str, str], message: str) -> str:
    """Create a branch whose tree is exactly ``files`` (a fresh root commit)."""
    root = Path(root)
    if ref_exists(root, branch):
        return head_sha(root, branch)
    with tempfile.TemporaryDirectory(prefix="hv-spec-tree-") as tree_tmp, \
            tempfile.TemporaryDirectory(prefix="hv-spec-index-") as index_tmp:
        tmp_path = Path(tree_tmp)
        for rel, content in files.items():
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # the index must live outside the work tree, otherwise it stages itself
        index = Path(index_tmp) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        proc = subprocess.run(
            ["git", "--work-tree", str(tmp_path), "add", "-A", "--", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            raise GitError(f"cannot stage spec skeleton: {proc.stderr.strip()}")
        # write-tree must use the same index
        proc = subprocess.run(
            ["git", "write-tree"], cwd=str(root), capture_output=True, text=True, env=env
        )
        if proc.returncode != 0:
            raise GitError(f"cannot write spec tree: {proc.stderr.strip()}")
        tree_sha = proc.stdout.strip()
        commit = subprocess.run(
            ["git", *IDENTITY, "commit-tree", tree_sha, "-m", message],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            raise GitError(f"cannot create spec commit: {commit.stderr.strip()}")
        sha = commit.stdout.strip()
    git(root, "branch", branch, sha, check=True)
    return sha


def setup_excludes(root: Path, entries: list[str]) -> None:
    """Add dispatcher-owned paths to .git/info/exclude (shared by worktrees)."""
    git_dir = git(root, "rev-parse", "--git-common-dir")
    if not git_dir.ok:
        return
    common = Path(git_dir.stdout.strip())
    if not common.is_absolute():
        common = (Path(root) / common).resolve()
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    wanted = [*DEFAULT_EXCLUDES, *entries]
    missing = [entry for entry in wanted if entry not in existing]
    if missing:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and existing[-1] != "":
                handle.write("\n")
            for entry in missing:
                handle.write(f"{entry}\n")


def materialize_spec(spec_worktree: Path, branch: str, dest: Path, writable: bool = False) -> Path:
    """Copy the pinned spec tree into ``dest`` (a plain copy, not a git object)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(spec_worktree), "archive", branch],
        capture_output=True,
    )
    if archive.returncode != 0:
        raise GitError(f"cannot archive {branch}: {archive.stderr.decode(errors='replace')}")
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True
    )
    if extract.returncode != 0:
        raise GitError(f"cannot unpack spec tree: {extract.stderr.decode(errors='replace')}")
    if not writable:
        for path in dest.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
    return dest


def commit_spec_env(spec_worktree: Path, env_dir: Path, message: str, branch: str = "spec") -> str:
    """Copy ``env_dir`` into the spec worktree and commit it there."""
    target = Path(spec_worktree) / "env"
    source = Path(env_dir)
    if not source.exists():
        return head_sha(spec_worktree)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    commit_all(spec_worktree, message)
    return head_sha(spec_worktree, branch) if ref_exists(spec_worktree, branch) else head_sha(spec_worktree)


def env_sha(spec_worktree: Path, branch: str = "spec") -> str | None:
    result = git(spec_worktree, "rev-parse", "--verify", "--quiet", f"{branch}:env")
    if not result.ok:
        return None
    return result.stdout.strip()


def ensure_spec_worktree(root: Path, branch: str, path: Path) -> Path:
    """Make sure a checkout of the spec branch exists at ``path``."""
    path = Path(path)
    if (path / ".git").exists():
        return path
    for entry in worktree_list(root):
        if entry.get("branch", "").endswith(f"refs/heads/{branch}"):
            return Path(entry["worktree"])
    path.parent.mkdir(parents=True, exist_ok=True)
    result = git(root, "worktree", "add", "-q", str(path), branch)
    if not result.ok:
        git(root, "worktree", "add", "-q", "--detach", str(path), branch, check=True)
        git(path, "checkout", "-q", "-B", branch, branch, check=True)
    return path


def ensure_run_dirs(cfg) -> None:
    for directory in (cfg.run_dir, cfg.cost_dir, cfg.logs_dir, cfg.run_dir / "prompts",
                      cfg.worktrees_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
