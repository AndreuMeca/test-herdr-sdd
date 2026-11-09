#!/usr/bin/env python3
"""SDD harness launcher on herdr. Python 3 stdlib only. Needs git; start and drive also need herdr, its terminal app or
its desktop app (`pick_runtime`).

Rules: harness/docs/sdd.md. Config: harness/config.json. Own tests: python3 harness/selftest.py.
The driver exits 0 when it stopped at a gate or a question, 2 when an agent or a cap stopped it, 1 when it failed.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CMD = "python3 harness/harness.py"  # how every message names the launcher
# A feature's state is never written: it is read from the files and refs on disk (docs/sdd.md §1).
STATES = ("spec", "spec_review", "spec_ready", "implementing", "review", "reviewed", "blocked", "done")
ROLES = {"spec_author": "spec", "spec_reviewer": "srev", "implementer": "impl", "reviewer": "rev"}
DONE_WHEN = {  # the states a role run must leave the feature in
    "spec_author": ("spec_review", "blocked"),
    "spec_reviewer": ("spec_ready", "spec", "blocked"),
    "implementer": ("review", "blocked"),
    "reviewer": ("reviewed", "implementing", "blocked"),
}
# What no role run may change (rule 1): the harness, and the files that steer the next agent, at the root or nested
# (git glob pathspecs: `**/` is any folder depth, a trailing `/**` is everything inside).
PROTECTED = (
    "harness/**",
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/.claude/**",
    "**/.codex/**",
    "**/.mcp.json",
    "**/GEMINI.md",
    "**/.gemini/**",
    "**/.opencode/**",
    "**/opencode.json",
)
WRITE_SET = {  # what the launcher commits after a role run, one tuple of pathspecs per `git add`; {dir}: the folder
    "spec_author": (("{dir}/spec.md", "{dir}/tasks.md", "{dir}/questions.md"),),
    "spec_reviewer": (("{dir}/spec_review.md", "{dir}/questions.md"),),
    "implementer": (
        (".", *(f":(exclude,glob){p}" for p in PROTECTED)),
        ("{dir}/tasks.md", "{dir}/log.md", "{dir}/questions.md"),
    ),
    "reviewer": (("{dir}/review.md", "{dir}/questions.md"),),
}
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")  # at most 20: a herdr agent name (`impl-<slug>-<6 hex>`) is at most 32
FEATURE_FILES = ("spec.md", "tasks.md", "log.md", "questions.md")  # plus the review forms (REVIEWS below)
SPEC_SECTIONS = ("Intent", "Requirements", "Design", "Verification")  # the `## ` headings of spec.md, in this order
# what the template itself ships: a repo with nothing tracked beyond these has no project code yet
TEMPLATE_PATHS = ("harness/", "AGENTS.md", "README.md", "LICENSE", ".gitignore")
SECTION_PLACEHOLDERS = ("<one paragraph", "<language", "<style")  # the template's AGENTS.md §1, until filled in
REQ_RE = re.compile(r"^- (R\d+):\s*(.*)$")
BOX_RE = re.compile(r"^- \[[ xX]\]")  # a checkbox line, `- [ ]` or `- [x]`
TASK_RE = re.compile(r"^- \[([ xX])\] (T\d+):\s*(.*)$")
# sdd §3: WHILE/WHEN/WHERE clauses (combinable), an optional `IF ..., THEN`, then `THE <subject> SHALL <response>`
EARS_RE = re.compile(r"^(?:(?:WHILE|WHEN|WHERE) .+?, )*(?:IF .+?, THEN )?THE \S.*? SHALL \S")
MARKER_RE = r"^(<{7} |>{7} )"  # a conflict marker line, as `git grep -E` sees it
RULES = Path(__file__).resolve().parent / "docs" / "sdd.md"  # the rules; §5 and §6 list the review checkpoints
EXIT_DONE, EXIT_ERROR, EXIT_ATTENTION = 0, 1, 2
DRIVE_WAIT_MS = 300_000  # the driver waits for a role in slices of this length
DEFAULT_ROLE_MINUTES = 180  # the driver stops for a human when one role run takes longer (max_role_minutes)
FIX_TRIES = 3  # how often the driver sends a role back to its own work before stopping for the human
REFS = "refs/harness/{slug}/"  # the launcher's marks for a feature; `merge` drops them all
APPROVED_REF = REFS + "approved"  # set by `approve`: the spec at this commit is the approved one


@dataclass(frozen=True)
class Review:
    """One of the two reviews (sdd §2), keyed in REVIEWS by the role that fills its form: how the launcher's commits
    and questions name it, the form in the feature folder, the ref set when a round starts (the review is of HEAD
    while only the form and questions.md changed since), and the sdd.md section whose numbered lines seed the boxes."""

    name: str
    file: str
    ref: str
    heading: str


REVIEWS = {  # the review of the spec, before `approve`, and the review of the code, before `merge`
    "spec_reviewer": Review(
        "spec review", "spec_review.md", REFS + "spec-review-start", "5. Spec reviewer checkpoints"
    ),
    "reviewer": Review("review", "review.md", REFS + "review-start", "6. Reviewer checkpoints"),
}
# config.json speaks one vocabulary for every CLI; CLIS translates it (harness/help/config.md has the same tables)
EFFORT_LEVELS = ("low", "medium", "high", "extra", "max")
PERMISSION_LEVELS = ("guarded", "full")  # a role agent runs unattended: it must never stop to ask
PERMISSION_WORDS = {
    "guarded": "never asks, the CLI blocks risky actions",
    "full": "never asks, no limits",
}
PROVIDER_EFFORTS = {"minimal": "low", "xhigh": "extra"}  # provider words people may type, and ours


@dataclass(frozen=True)
class Cli:
    """Everything the launcher knows about one agent CLI: how it takes our effort and permission words, how
    `doctor --models` asks it one question, and which startup dialogs it may show that are safe to accept.
    Adding a CLI is adding one entry to CLIS (and one column to the tables in harness/help/config.md)."""

    effort_flag: tuple  # the flag(s) for a reasoning effort, "{}" standing for the CLI's own level
    efforts: dict  # our effort level -> the CLI's
    permissions: dict  # our permission level -> the CLI's flags; a level missing here is one the CLI lacks
    guarded_by: str = ""  # what limits the agent under `guarded`, for `doctor` to say
    probe: tuple = ()  # a one-shot command for `doctor --models`; "{model}" and "{prompt}" are filled in
    startup_dialogs: tuple = ()  # (screen text, keys) per first-run dialog the launcher answers

    def effort_args(self, level: str) -> list:
        """The flags for one of our effort levels; empty when unset (the CLI's default)."""
        return [part.format(self.efforts[level]) for part in self.effort_flag] if level else []

    def permission_args(self, level: str) -> list:
        """The flags for one of our permission levels (config.json requires one)."""
        return list(self.permissions[level])

    def probe_cmd(self, model: str, prompt: str, effort: str = "") -> list:
        """The one-shot command of `doctor --models`: the role's effort flags go before the prompt, which is last; no
        model flag when the role runs the CLI's default model."""
        parts = list(self.probe)
        if not model:
            i = parts.index("{model}")
            del parts[i - 1 : i + 1]
        cmd = [part.format(model=model, prompt=prompt) for part in parts]
        return [*cmd[:-1], *self.effort_args(effort), cmd[-1]]

    def dialog(self, screen_text: str) -> tuple | None:
        """(text, keys) of the startup dialog on screen, or None when it is not one the launcher knows."""
        return next(((text, keys) for text, keys in self.startup_dialogs if text in screen_text), None)


SAME_EFFORTS = {"low": "low", "medium": "medium", "high": "high"}
CLIS = {
    "claude": Cli(
        effort_flag=("--effort", "{}"),
        efforts={**SAME_EFFORTS, "extra": "xhigh", "max": "max"},
        permissions={
            "guarded": ["--permission-mode", "auto"],
            "full": ["--dangerously-skip-permissions"],
        },
        guarded_by="auto mode's classifier",
        probe=("claude", "-p", "--model", "{model}", "{prompt}"),
        startup_dialogs=(  # both default to "No, exit"; the accepting option is the next one down
            ("trust this folder", ("down", "enter")),
            ("Bypass Permissions mode", ("down", "enter")),
        ),
    ),
    "codex": Cli(
        effort_flag=("-c", "model_reasoning_effort={}"),
        efforts={**SAME_EFFORTS, "extra": "xhigh", "max": "xhigh"},  # codex has nothing above xhigh
        permissions={
            "guarded": ["--sandbox", "workspace-write", "--ask-for-approval", "never"],
            "full": ["--dangerously-bypass-approvals-and-sandbox"],
        },
        guarded_by="its sandbox: writes only in the worktree (the launcher commits for it)",
        probe=("codex", "exec", "-m", "{model}", "--skip-git-repo-check", "{prompt}"),
        startup_dialogs=(
            ("Trust this folder?", ("enter",)),  # "Trust and continue" is its default
            (
                "Update available",
                ("down", "down", "enter"),
            ),  # 1. Update now (default)  2. Skip  3. Skip until next version
        ),
    ),
    "pi": Cli(
        effort_flag=("--thinking", "{}"),
        efforts={**SAME_EFFORTS, "extra": "xhigh", "max": "max"},
        permissions={"full": []},  # pi has no permission modes: it never asks
    ),
}
SIDEBAR_ROWS = '[ui.sidebar.spaces]\nrows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]'


# ----------------------------------------------------------------------------- shell / git
class Die(SystemExit):
    """What `die` raises: the exit code, plus the message for a caller that reports it elsewhere."""

    def __init__(self, msg: str, code: int):
        super().__init__(code)
        self.msg = msg


def die(msg: str, code: int = EXIT_ERROR):
    """Stop with a message: `main` prints it; a caller that catches Die reports it its own way."""
    raise Die(msg, code)


def sh(args, cwd=None, check=True, stdin=None):
    p = subprocess.run([str(a) for a in args], cwd=cwd, text=True, capture_output=True, stdin=stdin)
    if check and p.returncode != 0:
        die(f"{' '.join(str(a) for a in args)}\n{(p.stderr or p.stdout).strip()}")
    return p


def shell(cmd: str, cwd: Path) -> tuple[int, str]:
    """Run a configured shell command; (exit code, the tail of its output)."""
    p = subprocess.run(cmd, shell=True, cwd=cwd, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout + p.stderr).strip()[-1500:]


class Git:
    """git in one checkout (the main one or a feature worktree). Every git call of the launcher goes through here."""

    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def here(cls) -> Git:
        """The checkout the current directory belongs to."""
        return cls(Path(sh(["git", "rev-parse", "--show-toplevel"]).stdout.strip()))

    def proc(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        return sh(["git", *args], cwd=self.path, check=check)

    def run(self, *args, check: bool = True) -> str:
        """stdout of `git <args>`, stripped; dies on failure unless check=False."""
        return self.proc(*args, check=check).stdout.strip()

    def ok(self, *args) -> bool:
        return self.proc(*args, check=False).returncode == 0

    def paths(self, cmd: str, *args) -> set[str]:
        """Paths from `git <cmd> -z <args>` (NUL-separated, never quoted)."""
        return {p for p in self.proc(cmd, "-z", *args, check=False).stdout.split("\0") if p}

    # --- where we are
    def head(self) -> str:
        return self.run("rev-parse", "HEAD")

    def branch(self) -> str:
        return self.run("branch", "--show-current")

    def is_main(self) -> bool:
        common = self.run("rev-parse", "--git-common-dir")
        return common == ".git" or Path(common).resolve() == (self.path / ".git").resolve()

    def main_checkout(self) -> Path:
        """The main checkout of the repo this checkout belongs to (itself when it is one)."""
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir")).parent

    def git_dir(self) -> Path:
        """This checkout's own git dir (a worktree's lives under the main checkout's .git)."""
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-dir"))

    def worktrees(self) -> list[tuple[Path, str]]:
        """[(path, branch)] from `git worktree list --porcelain`; the main checkout comes first."""
        items, path, branch = [], None, ""
        for line in self.run("worktree", "list", "--porcelain").splitlines() + [""]:
            if line.startswith("worktree "):
                path = Path(line[len("worktree ") :])
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/") :]
            elif line == "" and path is not None:
                items.append((path, branch))
                path, branch = None, ""
        return items

    # --- refs: the launcher's marks, never an agent's
    def ref_exists(self, ref: str) -> bool:
        return self.ok("rev-parse", "-q", "--verify", ref)

    def set_ref(self, ref: str, rev: str = "HEAD"):
        self.run("update-ref", ref, rev)

    def drop_refs(self, prefix: str):
        for ref in self.run("for-each-ref", "--format=%(refname)", prefix, check=False).splitlines():
            self.proc("update-ref", "-d", ref, check=False)

    # --- the working tree
    def porcelain(self) -> list[tuple[str, str]]:
        """[(XY, path)] from `git status --porcelain`, untracked files one by one, both paths of a rename."""
        fields = self.proc("status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0")
        items, i = [], 0
        while i < len(fields):
            entry, i = fields[i], i + 1
            if len(entry) < 4:
                continue
            xy = entry[:2]
            items.append((xy, entry[3:]))
            if xy[0] in "RC":
                items.append((xy, fields[i]))  # the original path follows as its own field
                i += 1
        return items

    def dirty(self, untracked: bool = True) -> bool:
        """Uncommitted changes; untracked files count unless `untracked` is False."""
        return bool(self.run("status", "--porcelain", *([] if untracked else ["--untracked-files=no"])))

    def tracked(self, path: str) -> bool:
        return self.ok("ls-files", "--error-unmatch", "--", path)

    def changed_paths(self, base: str) -> list[str]:
        """Paths the branch changed since it forked from (or last merged) the base branch, plus uncommitted and
        untracked ones; a renamed file counts under both names."""
        committed = self.paths("diff", "--name-only", "--no-renames", f"{base}...HEAD")
        return sorted(committed | {p for _, p in self.porcelain()})

    def project_paths(self) -> set[str]:
        """Tracked files that are the project's, not the template's (TEMPLATE_PATHS); empty = no project code yet."""
        return {p for p in self.paths("ls-files") if not p.startswith(TEMPLATE_PATHS)}

    def is_ignored(self, path) -> bool:
        return self.ok("check-ignore", "-q", path)

    def show(self, rev: str, path: str) -> str:
        """A file as it is at `rev`; empty when it is not there."""
        return self.run("show", f"{rev}:{path}", check=False)

    def commit(self, msg: str, *paths):
        """Stage `paths` and commit them, only them."""
        self.run("add", "-A", "--", *paths)
        self.run("commit", "-q", "-m", msg, "--", *paths)

    # --- a role run's commit (rule 1): fold, stage the write set, commit, restore the rest
    def reset_to(self, rev: str):
        """HEAD and the index back at `rev`, the working tree as it is: commits made since become plain changes."""
        self.run("reset", "-q", "--mixed", rev)

    def add_all(self, pathspecs):
        """`git add -A` for the pathspecs that can match something (a named file that is neither on disk nor tracked
        would make git fail)."""
        specs = [p for p in pathspecs if p.startswith(":(") or p == "." or (self.path / p).exists() or self.tracked(p)]
        if any(not p.startswith(":(") for p in specs):
            self.run("add", "-A", "--", *specs)

    def staged(self) -> bool:
        return not self.ok("diff", "--cached", "--quiet")

    def commit_index(self, msg: str):
        self.run("commit", "-q", "-m", msg)

    def restore_rest(self) -> list[str]:
        """Discard every change and untracked file left in the working tree (ignored files stay); the paths."""
        left = sorted({p for _, p in self.porcelain()})
        if left:
            self.proc("restore", "--staged", "--worktree", "--", ".", check=False)
            self.proc("clean", "-qfd", check=False)
        return left

    def merge_in(self, base: str, msg: str) -> list[str]:
        """Merge the base branch into this checkout's branch. Empty when clean (or nothing to merge); otherwise the
        conflicted paths, committed as they are, markers included, so the implementer can resolve them in the files.
        A conflict git cannot mark in the file (a binary, a file deleted on one side) is aborted: dies naming it."""
        p = self.proc("merge", "--no-edit", "-m", msg, base, check=False)
        if p.returncode == 0:
            return []
        conflicted = sorted(self.paths("diff", "--name-only", "--diff-filter=U"))
        unmarked = sorted(set(conflicted) - self.paths("grep", "-l", "-E", MARKER_RE, "--", *conflicted))
        if not conflicted or unmarked:
            self.proc("merge", "--abort", check=False)
            why = f"conflicts git cannot mark in the file: {', '.join(unmarked)}" if unmarked else ""
            die(f"git merge {base} failed: {why or (p.stderr or p.stdout).strip()}")
        self.run("add", "-A")
        self.run("commit", "-q", "-m", f"{msg}, conflicts in {', '.join(conflicted)}")
        return conflicted

    def conflict_markers(self, base: str) -> list[str]:
        """Files the branch changed that still hold a conflict marker line."""
        files = [p for p in self.changed_paths(base) if (self.path / p).is_file()]
        return sorted(self.paths("grep", "-l", "-E", MARKER_RE, "--", *files)) if files else []


# ----------------------------------------------------------------------------- config / worktree files
def valid_pattern(x) -> bool:
    return isinstance(x, str) and bool(Path(x).parts) and not x.startswith("/") and ".." not in Path(x).parts


@dataclass(frozen=True)
class AgentConfig:
    """One `agents.<role>` entry of config.json, in the file's own words (see EFFORT_LEVELS, PERMISSION_LEVELS);
    its Cli (when the launcher knows the CLI) turns them into flags."""

    role: str
    harness: str
    model: str = ""
    effort: str = ""
    permissions: str = ""
    args: tuple = ()

    @classmethod
    def parse(cls, role: str, raw, where: str) -> AgentConfig:
        """The entry after checking it; dies naming the first problem. The flags `effort` and `permissions` translate
        to are never written by hand in `args`."""
        if not isinstance(raw, dict) or not isinstance(raw.get("harness"), str) or not raw["harness"]:
            die(f"{where}.harness is required: the agent CLI's name")
        name, effort, perm = raw["harness"], raw.get("effort", ""), raw.get("permissions", "")
        cli = CLIS.get(name)
        if not isinstance(raw.get("model") or "", str):
            die(f"{where}.model must be a string")
        if not isinstance(effort, str) or (effort and effort not in EFFORT_LEVELS):
            hint = f" ('{effort}' is the provider's word; here it is '{PROVIDER_EFFORTS[effort]}')" if (
                effort in PROVIDER_EFFORTS
            ) else ""  # fmt: skip
            die(f"{where}.effort must be one of {', '.join(EFFORT_LEVELS)}{hint}")
        if not isinstance(perm, str) or (perm and perm not in PERMISSION_LEVELS) or (cli and not perm):
            why = f"'{perm}' is not one of them" if perm else "it is required"  # a CLI not in CLIS: optional, verbatim
            die(f"{where}.permissions: {why}; a role agent runs unattended: use {' or '.join(PERMISSION_LEVELS)}")
        if cli and perm not in cli.permissions:
            die(f"{where}.permissions '{perm}' is not available for {name} (it has: {', '.join(cli.permissions)})")
        args = raw.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            die(f"{where}.args must be a list of strings")
        return cls(role, name, raw.get("model") or "", effort, perm, tuple(args))

    @property
    def cli(self) -> Cli | None:
        return CLIS.get(self.harness)

    def cli_args(self) -> list:
        """The CLI flags after `--`: model, effort, permissions (translated by its Cli; a CLI not in CLIS gets
        neither, `doctor` says so), then `args` verbatim."""
        cli = self.cli
        model = ["--model", self.model] if self.model else []
        flags = [*cli.effort_args(self.effort), *cli.permission_args(self.permissions)] if cli else []
        flags = [*flags, *self.args]
        return ["--", *model, *flags] if (model or flags) else []

    def describe(self) -> list[str]:
        """How `doctor` shows its effort and permissions (our words, what they mean) and the flags the CLI gets."""
        cli, effort, perm = self.cli, self.effort, self.permissions
        if not effort:
            e = "effort       - (the CLI's default)"
        elif not cli:
            e = f"effort       {effort} (not translated for {self.harness}: put its flag in args)"
        else:
            native = cli.efforts[effort]
            e = f"effort       {effort}" + (f" ({self.harness}: {native})" if native != effort else "")
        if not cli:
            p = f"permissions  {perm or '-'} (not translated for {self.harness}: put its flags in args)"
        else:
            how = f"; {cli.guarded_by}" if perm == "guarded" and cli.guarded_by else ""
            p = f"permissions  {perm}: {PERMISSION_WORDS[perm]}{how}"
        return [e, p, "flags        " + (" ".join(self.cli_args()[1:]) or "-")]


@dataclass(frozen=True)
class Config:
    """harness/config.json, checked. Always the main checkout's copy: a branch cannot change what it is checked
    with."""

    base_branch: str
    test_cmd: str
    setup_cmd: str
    copy_files: tuple
    max_review_rounds: int
    max_role_minutes: int
    gates: dict  # "approve" / "merge" -> "human" | "auto"
    agents: dict  # role -> AgentConfig

    @classmethod
    def load(cls, root: Path) -> Config:
        p = Git(root).main_checkout() / "harness" / "config.json"
        try:
            raw = json.loads(p.read_text())
        except FileNotFoundError:
            die(f"missing {p}")
        except json.JSONDecodeError as e:
            die(f"{p}: {e}")
        return cls.parse(raw, p)

    @classmethod
    def parse(cls, raw: dict, p: Path) -> Config:
        """The config after checking every key; dies naming the first problem."""
        for key in ("base_branch", "test_cmd", "agents"):
            if key not in raw:
                die(f"{p}: missing '{key}'")
        for key in ("base_branch", "test_cmd"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                die(f"{p}: {key} must be a non-empty string")
        agents = raw["agents"] if isinstance(raw["agents"], dict) else {}
        agents = {role: AgentConfig.parse(role, agents.get(role), f"{p}: agents.{role}") for role in ROLES}
        if not isinstance(raw.get("gates") or {}, dict):
            die(f'{p}: gates must be an object: {{"approve": "human"|"auto", "merge": ...}}')
        gates = {gate: (raw.get("gates") or {}).get(gate, "human") for gate in ("approve", "merge")}
        for gate, who in gates.items():
            if who not in ("human", "auto"):
                die(f"{p}: gates.{gate} must be 'human' or 'auto'")
        copy_files = raw.get("copy_files", [])
        if not isinstance(copy_files, list) or not all(valid_pattern(x) for x in copy_files):
            die(f"{p}: copy_files must be a list of paths or globs relative to the repo root")
        cls.check_limits(raw, p)
        return cls(
            base_branch=raw["base_branch"],
            test_cmd=raw["test_cmd"],
            setup_cmd=raw.get("setup_cmd", ""),
            copy_files=tuple(copy_files),
            max_review_rounds=raw.get("max_review_rounds", 3),
            max_role_minutes=raw.get("max_role_minutes", DEFAULT_ROLE_MINUTES),
            gates=gates,
            agents=agents,
        )

    @staticmethod
    def check_limits(raw: dict, p: Path):
        for key, default in (("max_review_rounds", 3), ("max_role_minutes", DEFAULT_ROLE_MINUTES)):
            n = raw.get(key, default)
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                die(f"{p}: {key} must be an integer >= 0 (0 = no limit)")
        if not isinstance(raw.get("setup_cmd", ""), str):
            die(f'{p}: setup_cmd must be a shell command or ""')

    def agent(self, role: str) -> AgentConfig:
        return self.agents[role]


class WorktreeSetup:
    """What a new worktree needs beyond what git tracks: the gitignored files of copy_files, copied from the main
    checkout, then setup_cmd."""

    def __init__(self, root: Path, wt: Path, cfg: Config):
        self.root, self.wt, self.cfg = root, wt, cfg

    @staticmethod
    def copy_path(s: Path, d: Path):
        """Copy a file, symlink or whole folder; the copy replaces whatever is at `d`."""
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_symlink() or d.is_file():
            d.unlink()
        elif d.is_dir():
            shutil.rmtree(d)
        if s.is_dir():
            shutil.copytree(s, d, symlinks=True)
        else:
            shutil.copy2(s, d, follow_symlinks=False)

    def copy_ignored(self, patterns: list[str]) -> str:
        """Copy the gitignored paths matching `patterns` from the main checkout into the worktree. One report line."""
        src, dst = self.root, self.wt
        hits, missing = set(), []
        for pat in patterns:
            found = [s.relative_to(src) for s in src.glob(pat)]
            if not found:
                missing.append(pat)
            hits.update(found)
        done, skipped, failed = set(), [], []
        for rel in sorted(hits):  # parents sort before their children
            if any(p in done for p in rel.parents):
                continue  # inside a folder already copied whole
            if not Git(src).is_ignored(rel):
                skipped.append(str(rel))  # tracked or not ignored: the worktree already has git's version
                continue
            try:
                self.copy_path(src / rel, dst / rel)
                done.add(rel)
            except OSError as e:
                failed.append(f"{rel} ({e})")
        parts = [f"copied {', '.join(str(r) for r in sorted(done))}" if done else "nothing copied"]
        if missing:
            parts.append(f"not in the main checkout: {', '.join(missing)}")
        if skipped:
            parts.append(f"skipped, not gitignored: {', '.join(skipped)}")
        if failed:
            parts.append(f"FAILED: {'; '.join(failed)}")
        return "; ".join(parts)

    def run(self):
        """Copy the gitignored files, then run setup_cmd; prints one line for each."""
        if self.cfg.copy_files:
            print("copy_files:", self.copy_ignored(list(self.cfg.copy_files)))
        if self.cfg.setup_cmd and not Git(self.root).project_paths():
            print("setup_cmd: skipped (no project code yet; the first feature sets it up)")
        elif self.cfg.setup_cmd:
            code, out = shell(self.cfg.setup_cmd, self.wt)
            print("setup_cmd:", "ok" if code == 0 else f"FAILED (exit {code}); fix it in the worktree\n{out[-800:]}")


# ----------------------------------------------------------------------------- feature files
class Markdown:
    """The two things the launcher reads in markdown: `## ` sections and HTML comments."""

    @staticmethod
    def without_comments(text: str) -> str:
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)

    @staticmethod
    def section(text: str, heading: str) -> list[str] | None:
        """The lines under `## <heading>` (case-insensitive) up to the next `## `; None when there is no such
        heading (a renamed or decorated heading is not the section)."""
        out, inside, found = [], False, False
        for line in text.splitlines():
            if line.startswith("## "):
                inside = line[3:].strip().lower() == heading.lower()
                found = found or inside
                continue
            if inside:
                out.append(line)
        return out if found else None


@functools.cache
def checkpoints(role: str) -> tuple:
    """The checkpoints the role's review form is seeded with, one box each: the numbered lines of the docs/sdd.md
    section REVIEWS names (§5 for the spec reviewer, §6 for the reviewer), in order."""
    kind = REVIEWS[role]
    lines = Markdown.section(RULES.read_text(), kind.heading) or []
    found = tuple(line.strip() for line in lines if re.match(r"^\d+\. ", line.strip()))
    if not found:  # a renamed heading would seed a form with no boxes, which verdict_of would read as APPROVED
        die(f"{RULES} has no numbered line under '## {kind.heading}': {kind.file} cannot be seeded")
    return found


@dataclass(frozen=True)
class Task:
    """One `- [ ] T<n>: <text> (R1, R2)` line of tasks.md."""

    id: str
    text: str
    ticked: bool

    @property
    def cites(self) -> set[str]:
        """The R ids in the task's trailing parenthesis, `(R1, R2)`; an id elsewhere in the text is prose."""
        m = re.search(r"\(([^()]*)\)\s*$", self.text)
        return set(re.findall(r"\bR\d+\b", m[1])) if m else set()


class FeatureFiles:
    """The folder harness/features/<slug>/ in one checkout: what spec.md, tasks.md, log.md, questions.md and the
    review forms say, and the writes the launcher itself makes there (a question at a cap, the answer, clearing a
    review), each committed."""

    def __init__(self, checkout: Path, slug: str):
        self.checkout, self.slug, self.git = Path(checkout), slug, Git(checkout)
        self.rel = f"harness/features/{slug}"  # as git names it
        self.dir = self.checkout / "harness" / "features" / slug
        self.spec, self.tasks_md, self.log, self.questions_md = (self.dir / n for n in FEATURE_FILES)

    def form(self, role: str) -> Path:
        """The review form the role fills (REVIEWS): spec_review.md or review.md."""
        return self.dir / REVIEWS[role].file

    # --- reading
    def spec_text(self) -> str:
        """spec.md without its HTML comments (the template's guidance is never content)."""
        return Markdown.without_comments(self.spec.read_text())

    def requirements(self) -> tuple[dict[str, str], list[str]]:
        """({id: text}, ids that appear more than once)."""
        reqs, dups = {}, []
        for line in Markdown.section(self.spec_text(), "Requirements") or []:
            m = REQ_RE.match(line.strip())
            if m:
                if m[1] in reqs:
                    dups.append(m[1])
                reqs[m[1]] = m[2].strip()
        return reqs, dups

    @staticmethod
    def active(reqs: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in reqs.items() if v.rstrip(".").strip().lower() != "withdrawn"}

    def verification(self) -> tuple[dict[str, list[str]], list[str]]:
        """({R id: test files}, ids with more than one row) from the `| R<n> | ... |` rows under ## Verification: the
        backticked strings of the row, each cut at `::`."""
        rows, dups = {}, []
        for line in Markdown.section(self.spec_text(), "Verification") or []:
            m = re.match(r"^\|\s*(R\d+)\s*\|(.*)$", line.strip())
            if m:
                if m[1] in rows:
                    dups.append(m[1])
                found = (s.split("::")[0].strip() for s in re.findall(r"`([^`]+)`", m[2]))
                rows[m[1]] = [s for s in found if s]
        return rows, dups

    def tasks(self) -> list[Task] | None:
        """The task lines of tasks.md in order; None when the file is missing."""
        if not self.tasks_md.exists():
            return None
        text = Markdown.without_comments(self.tasks_md.read_text())
        found = (TASK_RE.match(line.strip()) for line in text.splitlines())
        return [Task(m[2], m[3], m[1] != " ") for m in found if m]

    @staticmethod
    def lines(lines: list[str]) -> list[str]:
        """The non-blank lines, stripped: under ## Findings each is a finding, in questions.md each is a question.
        What is written counts, not how it is marked (a bullet, a number, prose)."""
        return [line.strip() for line in lines if line.strip()]

    def questions(self) -> list[str]:
        """The lines of questions.md below its heading: a question for the human means the feature is blocked."""
        if not self.questions_md.exists():
            return []
        text = Markdown.without_comments(self.questions_md.read_text())
        return self.lines([line for line in text.splitlines() if not line.startswith("#")])

    @classmethod
    def review_form(cls, text: str) -> tuple[int, int, list[str]] | None:
        """(checkpoints ticked, checkpoints listed, findings) of a review form: its `## Checked` boxes and the lines
        of `## Findings`. None when either section is missing: a form without them says nothing."""
        text = Markdown.without_comments(text)
        checked, findings = Markdown.section(text, "Checked"), Markdown.section(text, "Findings")
        if checked is None or findings is None:
            return None
        boxes = [line.strip()[3] for line in checked if BOX_RE.match(line.strip())]
        return boxes.count("x") + boxes.count("X"), len(boxes), cls.lines(findings)

    @classmethod
    def verdict_of(cls, text: str, role: str) -> str:
        """What the role's review form says, from the form alone: a finding means CHANGES_REQUESTED; every checkpoint
        ticked and no finding means APPROVED; anything else is no verdict."""
        form = cls.review_form(text)
        if form is None:
            return ""
        ticked, boxes, findings = form
        if findings:
            return "CHANGES_REQUESTED"
        return "APPROVED" if ticked == len(checkpoints(role)) == boxes else ""

    def write_set(self, role: str) -> list[list[str]]:
        """The pathspec groups the launcher commits after a run of `role` (rule 1)."""
        return [[p.replace("{dir}", self.rel) for p in group] for group in WRITE_SET[role]]

    # --- history on this branch
    def review_rounds(self, role: str, base: str) -> int:
        """Rounds of the role's review on this branch that ended in CHANGES_REQUESTED: the forms the launcher cleared
        before the next round, plus the form on disk now."""
        kind = REVIEWS[role]
        path = f"{self.rel}/{kind.file}"
        grep = f"^{self.slug}: clear {kind.name}$"
        clears = self.git.run("log", "--format=%H", "--grep", grep, f"{base}..HEAD", "--", path, check=False)
        verdicts = [self.verdict_of(self.git.show(f"{sha}^", path), role) for sha in clears.split()]
        if self.form(role).exists():
            verdicts.append(self.verdict_of(self.form(role).read_text(), role))
        return verdicts.count("CHANGES_REQUESTED")

    # --- the launcher's own writes, each committed
    def ask(self, text: str):
        """A question from the launcher itself (a cap was hit): the feature is blocked until the human answers."""
        head = "" if self.questions_md.exists() else "# Questions\n"
        with self.questions_md.open("a") as fh:
            fh.write(f"{head}- launcher ({datetime.now():%Y-%m-%d %H:%M}): {text}\n")
        self.git.commit(f"{self.slug}: blocked by the launcher", self.rel + "/questions.md")

    def answer(self) -> str:
        """The human answered (`drive <slug> "<answer>"`): the questions, cleared from questions.md and committed."""
        asked = "\n".join(self.questions())
        self.questions_md.write_text("# Questions\n")
        self.git.commit(f"{self.slug}: questions answered", self.rel + "/questions.md")
        return asked

    def clear_review(self, role: str):
        """Remove the role's form before a round (committed when tracked): a review always belongs to one round."""
        kind = REVIEWS[role]
        rel = f"{self.rel}/{kind.file}"
        if self.git.tracked(rel):
            self.git.run("rm", "-q", rel)
            self.git.run("commit", "-q", "-m", f"{self.slug}: clear {kind.name}", "--", rel)
        elif self.form(role).exists():
            self.form(role).unlink()

    def seed_review(self, role: str):
        """The role's form for a new round: an empty ## Findings and one unticked box per checkpoint (sdd §5 or §6).
        The reviewing role ticks and writes findings; the form is the verdict (verdict_of)."""
        boxes = "".join(f"- [ ] {c}\n" for c in checkpoints(role))
        self.form(role).write_text(f"# Review\n\n## Findings\n\n## Checked\n{boxes}")


@dataclass(frozen=True)
class Feature:
    """A feature as `status` finds it across every worktree."""

    slug: str
    wt: Path  # the checkout holding its folder: its own worktree while live, else the base checkout
    branch: str  # that checkout's branch
    state: str

    @property
    def files(self) -> FeatureFiles:
        return FeatureFiles(self.wt, self.slug)

    @property
    def dir(self) -> Path:
        return self.files.dir

    @property
    def live(self) -> bool:
        return self.branch == self.slug

    @classmethod
    def all(cls, root: Path, base: str) -> dict[str, Feature]:
        """slug -> Feature across every worktree; a worktree on the feature's own branch wins over the base copy."""
        found: dict[str, Feature] = {}
        for wt, branch in Git(root).worktrees():
            if not wt.exists():
                continue
            for spec in sorted((wt / "harness" / "features").glob("*/spec.md")):
                slug = spec.parent.name
                if slug.startswith("_"):
                    continue
                if slug not in found or branch == slug:
                    found[slug] = cls(slug, wt, branch, Checker(wt, slug, base).state())
        return found

    @classmethod
    def find(cls, root: Path, slug: str, base: str) -> Feature:
        f = cls.all(root, base).get(slug)
        if not f:
            die(f"no feature '{slug}' in any worktree (see: {CMD} status)")
        return f

    @classmethod
    def live_one(cls, root: Path, slug: str, base: str) -> Feature:
        """The feature, which must have its own worktree."""
        f = cls.find(root, slug, base)
        if not f.live:
            die(
                f"'{slug}' has no live worktree ({f.state} on '{f.branch or 'detached'}'); "
                f"create one with: {CMD} start {slug}"
            )
        return f


# ----------------------------------------------------------------------------- lifecycle words
def next_action(st: str, gates: dict | None = None) -> str:
    gates = gates or {}
    return {
        "spec": "run spec_author",
        "spec_review": "run spec_reviewer",
        "spec_ready": f"approve ({gates.get('approve', 'human')})",
        "implementing": "run implementer",
        "review": "run reviewer",
        "reviewed": f"merge ({gates.get('merge', 'human')})",
        "blocked": "human: read questions.md",
        "done": "-",
    }[st]


def needs_human(st: str, gates: dict) -> bool:
    """Whose move it is at this state: the human's at a question or at a gate set to `human`."""
    gate = {"spec_ready": "approve", "reviewed": "merge"}.get(st)
    return st == "blocked" or (gate is not None and gates.get(gate, "human") == "human")


def state_text(st: str, gates: dict, running: str = "") -> str:
    """One short line for the sidebar and the board: where the feature stands and whose move it is."""
    if running:
        return f"{running} · working"
    nxt = next_action(st, gates)
    if nxt.startswith("run "):
        return f"{st.replace('_', ' ')} · {nxt[4:]} next"
    if st in ("spec_ready", "reviewed"):
        gate = nxt.split(" ")[0]
        return f"{st.replace('_', ' ')} · " + (f"needs you: {gate}" if needs_human(st, gates) else f"auto-{gate}")
    return "blocked · needs you" if st == "blocked" else "done"


# ----------------------------------------------------------------------------- herdr
class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Lock:
    """A lock file in a git dir, held with flock for as long as its holder lives, so a dead holder leaves no stale
    lock: `harness-driver.pid` in a worktree's (one driver per feature), `harness-merge.pid` in the main checkout's
    (one merge at a time). The file holds the holder's pid, for messages. The driver also rewrites a start stamp
    first thing (`harness-driver.started`), so a launch can tell that a driver started."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.stamp = self.path.with_name("harness-driver.started")
        self.msg = self.path.with_name("harness-driver.msg")  # the message `drive --tab` hands to the driver
        self.fd: int | None = None

    @classmethod
    def driver(cls, wt: Path) -> Lock:
        return cls(Git(wt).git_dir() / "harness-driver.pid")

    @classmethod
    def merge(cls, root: Path) -> Lock:
        return cls(Git(root).git_dir() / "harness-merge.pid")

    def stamp_value(self) -> tuple:
        """(mtime, content) of the start stamp, or () when there is none. A new driver changes it even if it exits at
        once."""
        try:
            return self.stamp.stat().st_mtime_ns, self.stamp.read_text()
        except FileNotFoundError:
            return ()

    def write_stamp(self):
        self.stamp.write_text(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    def running(self) -> int:
        """pid of a live holder, else 0 (-1 while the holder has not written its pid yet)."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return 0
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return 0
        except OSError:
            try:
                return int(self.path.read_text().strip())
            except ValueError:
                return -1
        finally:
            os.close(fd)

    def acquire(self) -> int:
        """Take the lock: 0 when it is ours now (until release() or the process ends), else the holder's pid."""
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return self.running() or -1
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return 0

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Herdr:
    """Everything the launcher asks of herdr (https://herdr.dev), through the `herdr` CLI of its terminal app: agents in
    panes, tabs, worktree workspaces, the sidebar. HerdrApp is the same contract on the desktop app. One instance,
    HERDR (`pick_runtime`); tests replace its methods."""

    label = "herdr"  # how `doctor` names the runtime
    binary = "herdr"
    install = "https://herdr.dev/docs/install/"
    dialog_states = ("blocked",)  # the agent states in which a startup dialog may be on its screen
    startup_s = 60  # how long an agent gets to reach idle
    sidebar_config = True  # the sidebar shows feature states only once the human's config names them

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def self_id(self) -> str:
        """The address of the pane this process runs in, for the driver to prompt; empty outside herdr."""
        return os.environ.get("HERDR_PANE_ID", "")

    def read_hint(self, name: str) -> str:
        """The command that shows an agent's screen to the human."""
        return f"herdr agent read {name} --source recent-unwrapped --lines 120"

    def pane_hint(self, pane: str) -> str:
        return f"herdr pane read {pane} --lines 40"

    def reopen_hint(self, root: Path, branch: str) -> str:
        return f"herdr worktree open --cwd {root} --branch {branch}"

    def call(self, *args) -> dict:
        """Run a herdr CLI command; return its JSON `result`. Errors (JSON on stderr) raise HerdrError."""
        if not self.available():
            die(f"{self.binary} not found in PATH ({self.install})")
        p = subprocess.run(["herdr", *[str(a) for a in args]], text=True, capture_output=True)
        if p.returncode != 0:
            try:
                err = json.loads(p.stderr.strip().splitlines()[-1])["error"]
            except Exception:
                raise HerdrError("cli_error", (p.stderr or p.stdout).strip()[:500]) from None
            raise HerdrError(err.get("code", "error"), err.get("message", ""))
        try:
            return json.loads(p.stdout).get("result", {})
        except Exception:
            return {"raw": p.stdout}

    # --- workspaces and worktrees
    def repo_source(self, root: Path) -> list:
        """How to name the repo to `herdr worktree create|open`. The caller's own workspace when it is open on the
        main checkout, so new worktrees nest under it like the sidebar's "new worktree"; else the path, for which
        herdr may open a second workspace on the main checkout."""
        ws = os.environ.get("HERDR_WORKSPACE_ID")
        if ws:
            try:
                src = self.call("worktree", "list", "--workspace", ws).get("source", {})
            except HerdrError:
                src = {}
            if src.get("source_checkout_path") and Path(src["source_checkout_path"]).resolve() == root.resolve():
                return ["--workspace", ws]
        return ["--cwd", root]

    def workspace_of(self, wt: Path) -> str | None:
        """The herdr workspace id open on this worktree, or None. Never opens one."""
        try:
            wts = self.call("worktree", "list", "--cwd", Git(wt).main_checkout()).get("worktrees", [])
        except HerdrError:
            return None
        return next(
            (w.get("open_workspace_id") for w in wts if Path(w.get("path", "")).resolve() == wt.resolve()), None
        )

    def open_workspace(self, root: Path, wt: Path) -> str:
        """The herdr workspace id of this worktree; opens the workspace if it is closed."""
        return (
            self.workspace_of(wt)
            or self.call("worktree", "open", *self.repo_source(root), "--path", wt, "--no-focus")["workspace"][
                "workspace_id"
            ]
        )

    def create_worktree(self, root: Path, slug: str, base: str) -> dict:
        """herdr's response for the new worktree: its path, workspace, tab and root pane."""
        source = self.repo_source(root)
        try:
            return self.call(
                "worktree", "create", *source, "--branch", slug, "--base", base, "--label", slug, "--no-focus"
            )
        except HerdrError as e:
            die(f"herdr worktree create -> {e.code}: {e.message}")

    def remove_worktree(self, wt: Path) -> bool:
        """Remove the worktree with its workspace; False when herdr has no workspace for it (or failed), so the
        caller falls back to git."""
        try:
            ws_id = self.workspace_of(wt) if self.available() else None
            if ws_id:
                self.call("worktree", "remove", "--workspace", ws_id, "--force")
                return True
        except HerdrError as e:
            print(f"herdr worktree remove failed ({e.code}: {e.message}); falling back to git")
        return False

    def workspaces(self) -> list:
        return self.call("workspace", "list").get("workspaces", [])

    def new_tab(self, ws_id: str, wt: Path, label: str) -> str:
        """A new tab in the workspace, opened at the worktree; returns its root pane id."""
        tab = self.call("tab", "create", "--workspace", ws_id, "--cwd", wt, "--label", label, "--no-focus")
        try:
            pane = tab["root_pane"]["pane_id"]
        except (KeyError, TypeError):
            return die(f"tab create returned no pane_id: {tab}")
        time.sleep(1.5)  # the new pane's shell needs a moment to reach its prompt
        return pane

    def tab_of(self, ws_id: str, label: str) -> str | None:
        """The id of the tab labelled `label` in the workspace, if any."""
        tabs = self.call("tab", "list", "--workspace", ws_id).get("tabs", [])
        return next((t.get("tab_id") for t in tabs if t.get("label") == label), None)

    def close_tab(self, tab: str):
        """Close a tab, ending what runs in it."""
        self.call("tab", "close", tab)

    def close_role_tab(self, wt: Path, role: str):
        """Close the role's tab in the worktree's workspace (ending the agent in it), if there is one. Never fatal."""
        with contextlib.suppress(HerdrError):
            ws_id = self.workspace_of(wt)
            tab = self.tab_of(ws_id, role) if ws_id else None
            if tab:
                self.close_tab(tab)

    def sidebar(self, wt: Path, text: str):
        """Show the feature's state on its workspace in herdr's sidebar (Space token `$harness`, see help/config.md).
        Never fatal."""
        if not self.available():
            return
        with contextlib.suppress(HerdrError):
            ws_id = self.workspace_of(wt)
            if ws_id:
                token = f"harness={text[:80]}"
                self.call("workspace", "report-metadata", ws_id, "--source", "harness", "--token", token)

    # --- agents
    def prompt(self, name: str, text: str):
        self.call("agent", "prompt", name, text)

    def notify(self, pane: str, text: str):
        """Prompt the agent hosted in `pane` (the one that launched the driver). Reported, never fatal."""
        try:
            self.prompt(pane, text)
        except HerdrError as e:
            if e.code != "agent_not_found":  # a plain shell in that pane: nobody to prompt, `status` says it all
                print(f"(could not notify pane {pane}: {e.code}: {e.message})")

    def wait(self, name: str, timeout_ms: int) -> tuple[bool, str]:
        """(settled, note) after waiting for the agent to leave the working state, at most timeout_ms."""
        try:
            self.call("agent", "wait", name, "--timeout", timeout_ms)
            return True, ""
        except HerdrError as e:
            if e.code == "timeout":
                return False, ""
            return True, e.code

    def state(self, name: str) -> str:
        try:
            return self.call("agent", "get", name).get("agent", {}).get("agent_status") or "?"
        except HerdrError:
            return "gone"

    def screen(self, name: str, lines: int = 40, source: str = "recent-unwrapped") -> str:
        """The agent's output as herdr prints it, plain text (`visible`: what is on screen now); empty when herdr
        cannot read it."""
        try:
            return self.call("agent", "read", name, "--source", source, "--lines", lines).get("raw", "")
        except HerdrError:
            return ""

    def settle_startup(self, name: str, agent: AgentConfig, timeout_s: int = 60) -> str:
        """Answer the startup dialogs its CLI is known to show (Cli.startup_dialogs, matched on the visible screen)
        and wait for idle. Returns the agent's state: idle or done means ready for prompts; blocked (a dialog the
        launcher does not know) or working (still starting after timeout_s) is the human's call."""
        cli = agent.cli
        deadline = time.time() + timeout_s
        state = self.state(name)
        while time.time() < deadline and state not in ("idle", "done", "gone"):
            dialog = None
            if state in self.dialog_states:
                dialog = cli.dialog(self.screen(name, source="visible")) if cli else None
                if not dialog and state == "blocked":
                    return state
            if dialog:
                text, keys = dialog
                print(f"{name}: answered the '{text}' dialog ({' '.join(keys)})")
                self.press(name, keys)
            else:
                time.sleep(1)
            state = self.state(name)
        return state

    def press(self, name: str, keys: tuple):
        """Answer a startup dialog."""
        self.call("agent", "send-keys", name, *keys)
        with contextlib.suppress(HerdrError):  # the keys take a moment to land; `blocked` would return at once
            self.call("agent", "wait", name, "--until", "idle", "--until", "done", "--timeout", 10_000)

    def start_agent(self, ws_id: str, wt: Path, role: str, name: str, agent: AgentConfig):
        """A new tab for the role, with the agent started in it."""
        pane_id = self.new_tab(ws_id, wt, role)
        try:
            self.call(
                "agent", "start", name, "--kind", agent.harness, "--pane", pane_id, "--timeout", 120_000,
                *agent.cli_args(),
            )  # fmt: skip
        except HerdrError as e:
            if e.code != "agent_not_ready":  # not ready: a startup dialog, which settle_startup answers
                raise

    def run_role(
        self, root: Path, wt: Path, role: str, slug: str, text: str, timeout_ms: int, agent: AgentConfig
    ) -> tuple[bool, str]:
        """One role run = one fresh agent: close the role's previous tab (and the agent in it), open a new one in the
        worktree's workspace, start the agent with the config as it is now, answer its startup dialogs, prompt it.
        Returns (settled, note)."""
        name = agent_name(role, slug, root)
        ws_id = self.open_workspace(root, wt)
        old = self.tab_of(ws_id, role)
        if old:
            self.close_tab(old)
        self.start_agent(ws_id, wt, role, name, agent)
        state = self.settle_startup(name, agent, self.startup_s)
        if state not in ("idle", "done"):
            return True, f"{state} at startup"
        return self.submit(name, text, timeout_ms)

    def submit(self, name: str, text: str, timeout_ms: int) -> tuple[bool, str]:
        """Prompt a ready agent and wait for its turn, at most timeout_ms. Returns (settled, note)."""
        try:
            self.call("agent", "prompt", name, text, "--wait", "--timeout", timeout_ms)
            return True, ""
        except HerdrError as e:
            if e.code == "timeout":
                return False, ""
            if e.code == "agent_prompt_stalled" and self.state(name) == "idle":
                # the text sits unsubmitted in the input box (seen live right after a startup dialog): Enter once
                print(f"{name}: the prompt stalled with the agent idle; sending Enter")
                self.call("agent", "send-keys", name, "enter")
                return self.wait(name, timeout_ms)
            if e.code in ("agent_blocked", "agent_prompt_stalled"):
                return True, e.code
            raise

    # --- the driver's tab
    def driver_tab_pane(self, ws_id: str) -> str | None:
        """The root pane of the `driver` tab in this workspace, if any."""
        tabs = self.call("tab", "list", "--workspace", ws_id).get("tabs", [])
        tab = next((t for t in tabs if t.get("label") == "driver"), None)
        if not tab:
            return None
        panes = self.call("pane", "list", "--workspace", ws_id).get("panes", [])
        # herdr lists a tab's Sidebar pane (no shell) before its shell pane: never that one, nor an agent's pane
        ours = [p for p in panes if p.get("tab_id") == tab.get("tab_id") and p.get("label") != "Sidebar"]
        pane = next((p for p in ours if not p.get("agent")), None)  # the driver runs in a plain shell pane
        return pane.get("pane_id") if pane else None

    def launch_driver(self, root: Path, wt: Path, slug: str, msg: str, notify_pane: str) -> str:
        """Run `drive <slug>` in the `driver` tab of the feature's workspace (created if missing). Returns the pane
        id."""
        ws_id = self.open_workspace(root, wt)
        pane = self.driver_tab_pane(ws_id) or self.new_tab(ws_id, wt, "driver")
        lock = Lock.driver(wt)
        cmd = [sys.executable, str(Path(__file__).resolve()), "drive"]
        cmd += ["--notify", notify_pane] if notify_pane else []
        if msg:  # the command is typed into the tab's shell: the message goes through a file, whatever its size
            lock.msg.write_text(msg)
            cmd += ["--msg-file", str(lock.msg)]
        cmd += ["--", slug]
        before = lock.stamp_value()  # every driver rewrites the stamp first thing: a start leaves a trace
        self.run_in_pane(pane, shlex.join(cmd))
        for _ in range(60):  # a shell with a slow init takes a while to run the command
            if lock.stamp_value() != before:
                return pane
            time.sleep(0.5)
        pid = lock.running()
        if pid:  # another `drive --tab` won the lock first
            raise HerdrError("driver_running", f"a driver (pid {pid}) is already running {slug}")
        raise HerdrError(
            "driver_not_started",
            f"the driver did not start in pane {pane}; look at it: {self.pane_hint(pane)}",
        )

    def run_in_pane(self, pane: str, command: str):
        """Type a command into the shell of a pane."""
        self.call("pane", "run", pane, command)

    def resume(self, root: Path, wt: Path, slug: str) -> int:
        """After a human gate: relaunch the driver in its tab, else say what to run."""
        if not self.available():
            print(f"next (inside herdr): {CMD} drive {slug} --tab")
            return 0
        try:
            ws_id = self.workspace_of(wt)
            if ws_id and self.driver_tab_pane(ws_id) and not Lock.driver(wt).running():
                pane = self.launch_driver(root, wt, slug, "", self.self_id())
                print(f"driver resumed in pane {pane}")
                return 0
        except HerdrError as e:
            print(f"warning: could not resume the driver ({e.code}: {e.message})")
        print(f"next: {CMD} drive {slug} --tab")
        return 0


class HerdrApp(Herdr):
    """The same contract on the Herdr desktop app, a separate program from the terminal app: herdr-core runs its
    terminals, `hc` drives them, and the window shows them. What needs no window goes through `hc`, so a driver runs
    with the window closed and never takes the human's focus: a terminal labelled `herdr.space=<id>` and
    `herdr.tab=<id>` shows up in that space and tab, whose ids the launcher derives itself; there is no other record of
    a workspace. A tab is its terminal; the last one closing closes the space. `herdr-app do` only names things, and
    only while a window is open. A worktree is plain git, where the app's own `worktree` mod puts them."""

    label = "herdr app"
    binary = "hc"
    install = "https://herdr.dev/download/"
    dialog_states = ("blocked", "?")  # a CLI reports no status at all while its first dialog is up
    startup_s = 120  # no `agent start` waits for it first
    sidebar_config = False  # the space's name carries the state

    def self_id(self) -> str:
        return os.environ.get("HC_TERMINAL_ID", "")

    def read_hint(self, name: str) -> str:
        return f"hc read {name} --lines 120"

    def pane_hint(self, pane: str) -> str:
        return f"hc read {pane} --lines 40"

    def reopen_hint(self, root: Path, branch: str) -> str:
        return f"git worktree add {self.worktree_path(root, branch)} {branch}"

    def call(self, *args) -> dict:
        """Run an hc command; return its JSON (`{"raw": text}` for the commands that print text). Errors raise
        HerdrError. hc answers JSON because its output is a pipe here; a nonzero exit alone is no error: `wait` passes
        on the exit code of what it waited for."""
        if not self.available():
            die(f"{self.binary} not found in PATH ({self.install})")
        p = subprocess.run(["hc", *[str(a) for a in args]], text=True, capture_output=True)
        for stream in (p.stdout, p.stderr):
            try:
                out = json.loads(stream)
            except ValueError:
                continue
            if isinstance(out, dict) and "error" in out:
                err = out["error"]
                raise HerdrError(err.get("kind", "error"), err.get("message", ""))
            if isinstance(out, dict) and stream is p.stdout:
                return out
        if p.returncode != 0:
            raise HerdrError("cli_error", (p.stderr or p.stdout).strip()[:500])
        return {"raw": p.stdout}

    def window(self, *args) -> int:
        """Run an app command in the open window. Its exit code: 0 done, 1 refused, 2 no window. Never fatal."""
        exe = shutil.which("herdr-app")
        if not exe:
            return 2
        try:
            return subprocess.run([exe, "do", *args], capture_output=True, timeout=5).returncode
        except (OSError, subprocess.SubprocessError):
            return 2

    # --- workspaces and worktrees
    @staticmethod
    def app_id(prefix: str, *parts) -> str:
        """An id for the app's labels, the same on every call: it is how the launcher finds its spaces and tabs."""
        return prefix + hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:32]

    def space_id(self, wt: Path) -> str:
        return self.app_id("sp", Path(wt).resolve())

    def tab_labels(self, space: str, tab: str) -> list:
        return ["--label", f"herdr.space={space}", "--label", f"herdr.tab={tab}"]

    def terminals(self, *labels) -> list:
        """The terminals with these `--label` flags, finished ones included."""
        return self.call("ls", "--all", *labels).get("terminals", [])

    def live(self, *labels) -> list:
        return [t for t in self.terminals(*labels) if t.get("state") == "live"]

    def worktree_path(self, root: Path, branch: str) -> Path:
        return Path.home() / ".herdr" / "worktrees" / Path(root).name / branch

    def workspace_of(self, wt: Path) -> str | None:
        space = self.space_id(wt)
        try:
            return space if self.live("--label", f"herdr.space={space}") else None
        except HerdrError:
            return None

    def open_workspace(self, root: Path, wt: Path) -> str:
        return self.space_id(wt)  # the space exists once a terminal carries its label

    def create_worktree(self, root: Path, slug: str, base: str) -> dict:
        wt = self.worktree_path(root, slug)
        Git(root).run("worktree", "add", "-q", "-b", slug, str(wt), base)
        return {"worktree": {"path": str(wt)}, "workspace": {"workspace_id": self.space_id(wt)}}

    def remove_worktree(self, wt: Path) -> bool:
        """End the space's terminals, which closes it; False: the worktree itself is git's to remove."""
        with contextlib.suppress(HerdrError):
            self.end(self.terminals("--label", f"herdr.space={self.space_id(wt)}"))
        return False

    def end(self, terminals: list):
        """End these terminals and drop their records, so a name stays free for the next run."""
        for t in terminals:
            if t.get("state") == "live":
                self.call("kill", t["address"])
            self.call("rm", t["address"])

    def workspaces(self) -> list:
        self.call("server", "status")  # what `doctor` asks of this: that the runtime answers
        return []

    def new_tab(self, ws_id: str, wt: Path, label: str) -> str:
        """A shell in a new tab of the space; returns its address."""
        return self.run_terminal(ws_id, wt, label, f"{label}-{ws_id[2:14]}")

    def run_terminal(self, ws_id: str, wt: Path, label: str, name: str, *argv) -> str:
        """Start a terminal in the space's tab `label`, neither a child of this process nor reporting to it: as with a
        pane, what the launcher starts outlives the launcher."""
        tab = self.app_id("tb", ws_id, label)
        res = self.call(
            "run", "--json", "--bg", "--no-parent", "--no-notify", "--name", name, "--cwd", wt,
            *self.tab_labels(ws_id, tab), *(["--", *argv] if argv else []),
        )  # fmt: skip
        for _ in range(4):  # an open window adopts the terminal a moment later and refuses the name until then
            if self.window("tab.rename", "--id", tab, "--name", label) != 1:
                break
            time.sleep(0.5)
        return res["address"]

    def tab_of(self, ws_id: str, label: str) -> str | None:
        tab = self.app_id("tb", ws_id, label)
        return tab if self.terminals(*self.tab_labels(ws_id, tab)) else None

    def close_tab(self, tab: str):
        self.end(self.terminals("--label", f"herdr.tab={tab}"))

    def sidebar(self, wt: Path, text: str):
        """The space's name carries the feature's state: the sidebar shows it with nothing to configure."""
        if self.available() and self.workspace_of(wt):
            self.window("space.rename", "--id", self.space_id(wt), "--name", f"{Path(wt).name} · {text[:80]}")

    # --- agents
    def prompt(self, name: str, text: str):
        self.call("send", name, text)

    def notify(self, pane: str, text: str):
        """Prompt the agent in terminal `pane`. A terminal with no reported status is a shell, where `send` would run
        the text as a command."""
        try:
            if self.call("get", pane).get("status"):
                self.prompt(pane, text)
        except HerdrError as e:
            if e.code != "not_found":
                print(f"(could not notify terminal {pane}: {e.code}: {e.message})")

    def wait(self, name: str, timeout_ms: int) -> tuple[bool, str]:
        try:
            res = self.call("wait", name, "--timeout", max(1, timeout_ms // 1000))
        except HerdrError as e:
            return True, e.code
        return res.get("reason") not in ("timeout", "message"), ""

    def state(self, name: str) -> str:
        try:
            t = self.call("get", name)
        except HerdrError:
            return "gone"
        if t.get("state") != "live":
            return "gone"
        return (t.get("status") or {}).get("state") or "?"

    def screen(self, name: str, lines: int = 40, source: str = "recent-unwrapped") -> str:
        try:
            return self.call("read", name, "--lines", 0 if source == "visible" else lines).get("raw", "")
        except HerdrError:
            return ""

    def press(self, name: str, keys: tuple):
        self.call("keys", name, *keys)
        time.sleep(1)  # the keys take a moment to land

    def start_agent(self, ws_id: str, wt: Path, role: str, name: str, agent: AgentConfig):
        self.run_terminal(ws_id, wt, role, name, agent.harness, *agent.cli_args())

    def submit(self, name: str, text: str, timeout_ms: int) -> tuple[bool, str]:
        try:
            res = self.call("send", "--wait", "--timeout", max(1, timeout_ms // 1000), name, text)
        except HerdrError as e:
            if e.code == "blocked":
                return True, "agent_blocked"
            raise
        if res.get("started") is False:  # the text sits unsubmitted in the input box
            if self.state(name) != "idle":
                return True, "agent_prompt_stalled"
            print(f"{name}: the prompt stalled with the agent idle; sending Enter")
            self.call("keys", name, "enter")
            return self.wait(name, timeout_ms)
        waited = res.get("wait") or {}
        if waited.get("reason") in ("timeout", "message"):
            return False, ""
        return True, "agent_blocked" if waited.get("status") == "blocked" else ""

    # --- the driver's tab
    def driver_tab_pane(self, ws_id: str) -> str | None:
        tab = self.app_id("tb", ws_id, "driver")
        return next((t["address"] for t in self.live(*self.tab_labels(ws_id, tab))), None)

    def run_in_pane(self, pane: str, command: str):
        self.call("send", pane, command)


def pick_runtime(env=os.environ) -> Herdr:
    """The runtime this process lives in, innermost first: the terminal app sets HERDR_ENV in its panes (also when it
    runs inside a terminal of the app), herdr-core sets HC_TERMINAL_ID in its terminals. Started outside both: the one
    installed; both installed: the terminal app, whose `start` works from anywhere. The two share no agents: a feature
    is plain git, so another runtime can `drive` it, in a workspace of its own."""
    if env.get("HERDR_ENV") == "1":
        return Herdr()
    if env.get("HC_TERMINAL_ID") or (shutil.which("hc") and not shutil.which("herdr")):
        return HerdrApp()
    return Herdr()


HERDR = pick_runtime()


def agent_name(role: str, slug: str, root: Path) -> str:
    """`impl-<slug>-<6 hex>`: herdr's agent names are unique per server and at most 32 characters, so two repos
    driving one slug differ by a hash of the main checkout's path, and SLUG_RE caps the slug at 20."""
    repo = hashlib.sha1(str(Git(root).main_checkout().resolve()).encode()).hexdigest()[:6]
    return f"{ROLES[role]}-{slug}-{repo}"


# ----------------------------------------------------------------------------- check
class Checker:
    """The state of one feature in one checkout, read from its files and refs (docs/sdd.md §1), and the rules of §8
    that apply to it, one method per rule (rule 2: the spec's sections, then its requirements). Rule 1 is mechanical
    (RoleRun.finish); rule 5's tests are `tests()`."""

    def __init__(self, checkout: Path, slug: str, base: str):
        self.files, self.git = FeatureFiles(checkout, slug), Git(checkout)
        self.slug, self.base, self.checkout = slug, base, Path(checkout)
        self.approved_ref = APPROVED_REF.format(slug=slug)

    def approved(self) -> bool:
        return self.git.ref_exists(self.approved_ref)

    def state(self) -> str:
        """Where the feature stands, in this order of evidence: not on its own branch (done), a question for the human
        (blocked), then the stage of one review: the spec's before approval (rules 2-4, then the spec review of HEAD),
        the code's after it (rules 2-5 without the tests, then the review of HEAD)."""
        if self.git.branch() != self.slug:
            if self.git.is_main():
                return "done"
            die(
                f"{self.checkout} is on '{self.git.branch() or 'detached'}', not on the branch '{self.slug}' of the "
                "feature folder it holds: check that branch out there, or remove the worktree"
            )
        if not self.files.spec.exists():
            return "spec"
        if self.files.questions():
            return "blocked"
        if not self.approved():
            return self.stage("spec_reviewer", self.spec_errors(), "spec_review", "spec_ready", "spec")
        return self.stage("reviewer", self.spec_errors() or self.complete(), "review", "reviewed", "implementing")

    def stage(self, role: str, gaps: list, pending: str, ready: str, back: str) -> str:
        """The state at one review's stage: a gap in what the review judges sends the feature back to the role that
        produces it; else the review of HEAD decides (APPROVED: ready, CHANGES_REQUESTED: back); else a round is due."""
        if gaps:
            return back
        verdict = self.review(role)[0]
        return (ready if verdict == "APPROVED" else back) if verdict else pending

    def errors(self) -> list[str]:
        """What is wrong or missing at the feature's state: rules 2-4 always, rule 5 (without the tests) once approved,
        rule 6 for a review of HEAD."""
        if not self.files.spec.exists():
            return [f"{self.slug}: missing spec.md"]
        if self.git.branch() != self.slug:
            return []
        errs = self.spec_errors()
        if self.approved():
            errs += self.complete()
        return errs + [e for role in REVIEWS for e in self.review(role)[1]]

    def spec_errors(self) -> list[str]:
        return self.sections() + self.requirements() + self.tasks() + self.verification()

    # --- rule 2
    def sections(self) -> list[str]:
        """spec.md has its four `## ` headings in order (other headings are tolerated); Intent and Design are not
        empty (Requirements and Verification have their own rules)."""
        text = self.files.spec_text()
        wanted = [s.lower() for s in SPEC_SECTIONS]
        found = [line[3:].strip().lower() for line in text.splitlines() if line.startswith("## ")]
        errs = []
        if [s for s in found if s in wanted] != wanted:
            names = ", ".join(f"## {s}" for s in SPEC_SECTIONS)
            errs.append(f"{self.slug}: spec.md needs {names}, in that order (harness/docs/sdd.md §2)")
        for s in ("Intent", "Design"):
            lines = Markdown.section(text, s)
            if lines is not None and not "".join(lines).strip():
                errs.append(f"{self.slug}: ## {s} is empty")
        return errs

    def requirements(self) -> list[str]:
        """Ids unique, at least one active requirement, each with exactly one SHALL in an EARS pattern and no template
        placeholder."""
        reqs, dups = self.files.requirements()
        active = FeatureFiles.active(reqs)
        errs = [f"{self.slug}: duplicate requirement id {r}" for r in dups]
        if not active:
            errs.append(f"{self.slug}: no active requirement line '- R<n>: ... SHALL ...' under ## Requirements")
        for k, v in active.items():
            shalls = len(re.findall(r"\bSHALL\b", v))
            if shalls == 0:
                errs.append(f"{self.slug}: {k} has no SHALL")
            elif shalls > 1:
                errs.append(f"{self.slug}: {k} has {shalls} SHALLs; one per requirement")
            elif not EARS_RE.match(v):
                errs.append(f"{self.slug}: {k} follows no EARS pattern (harness/docs/sdd.md §3)")
        return errs

    # --- rule 3
    def tasks(self) -> list[str]:
        """Task ids unique; every task cites existing requirements; every active requirement is cited."""
        tasks = self.files.tasks()
        if tasks is None:
            return [f"{self.slug}: missing tasks.md"]
        reqs, _ = self.files.requirements()
        errs, covered, seen = [], set(), set()
        for t in tasks:
            if t.id in seen:
                errs.append(f"{self.slug}: duplicate task id {t.id}")
            seen.add(t.id)
            if not t.cites:
                errs.append(f"{self.slug}: {t.id} cites no requirement")
            errs += [f"{self.slug}: {t.id} cites unknown {c}" for c in sorted(t.cites - reqs.keys())]
            covered |= t.cites
        if not tasks:
            errs.append(f"{self.slug}: no task line '- [ ] T<n>: ... (R<n>)' in tasks.md")
        missing = sorted(FeatureFiles.active(reqs).keys() - covered, key=lambda r: int(r[1:]))
        return errs + [f"{self.slug}: {r} is not covered by any task" for r in missing]

    # --- rule 4
    @staticmethod
    def token_pattern(slug: str, r: str) -> str:
        """git grep -E pattern for the token `<slug>:<R>` as a whole: not inside `x<slug>:`, `a-<slug>:`, `<R>0` or
        `<R>x`. Slugs are [a-z0-9-] and ids R<digits>, so neither needs escaping."""
        return f"(^|[^A-Za-z0-9_-]){slug}:{r}([^A-Za-z0-9_]|$)"

    def active_rows(self) -> list[tuple[str, list[str]]]:
        """(R id, test files of its ## Verification row) for every active requirement, in id order."""
        active = FeatureFiles.active(self.files.requirements()[0])
        rows = self.files.verification()[0]
        return [(r, rows.get(r) or []) for r in sorted(active, key=lambda x: int(x[1:]))]

    def verification(self) -> list[str]:
        """Every active requirement has one ## Verification row naming test files: relative paths outside harness/,
        no folder, no glob."""
        errs = [f"{self.slug}: {r} has more than one row in ## Verification" for r in self.files.verification()[1]]
        for r, files in self.active_rows():
            if not files:
                errs.append(f"{self.slug}: {r} has no test file in ## Verification")
                continue
            for f in files:
                if f.startswith("harness/") or f.endswith("/") or any(c in f for c in "*?[") or not valid_pattern(f):
                    errs.append(
                        f"{self.slug}: {r} in ## Verification must name a test file, a relative path outside "
                        f"harness/, not {f}"
                    )
        return errs

    # --- rule 5 (without the tests)
    def complete(self) -> list[str]:
        """What the implementer still owes: every task ticked, log.md, no conflict markers, every active requirement's
        token in one of the files its ## Verification row names."""
        errs = [f"{self.slug}: {t.id} is not ticked" for t in self.files.tasks() or [] if not t.ticked]
        if not self.files.log.exists():
            errs.append(f"{self.slug}: missing log.md")
        markers = self.git.conflict_markers(self.base)
        if markers:
            errs.append(f"{self.slug}: conflict markers left in {', '.join(markers)}")
        for r, files in self.active_rows():
            real = [f for f in files if (self.checkout / f).is_file()]
            if not files:
                continue  # rule 4 says so already
            if not real:
                errs.append(f"{self.slug}: no test file named for {r} in ## Verification exists ({', '.join(files)})")
            elif not self.git.ok("grep", "--untracked", "-q", "-E", self.token_pattern(self.slug, r), "--", *real):
                errs.append(
                    f"{self.slug}: no test named for {r} in ## Verification carries the token {self.slug}:{r} "
                    f"({', '.join(real)})"
                )
        return errs

    # --- rule 5, the tests
    @staticmethod
    def tests(checkout: Path, cfg: Config) -> list[str]:
        code, out = shell(cfg.test_cmd, checkout)
        return [] if code == 0 else [f"tests failed (exit {code}: {cfg.test_cmd})\n{out}"]

    # --- rule 6
    def review(self, role: str) -> tuple[str, list[str]]:
        """(verdict, errors) of the role's review of HEAD: its form exists, the launcher started the round, and no
        non-merge commit since then touched anything but the form or questions.md (otherwise the review is of an
        older spec or code and says nothing). The form decides the verdict (FeatureFiles.verdict_of); a form that
        decides nothing is the error."""
        kind, form = REVIEWS[role], self.files.form(role)
        ref = kind.ref.format(slug=self.slug)
        if not form.exists() or not self.git.ref_exists(ref):
            return "", []
        touched = self.git.paths("log", "--format=", "--name-only", "--no-merges", f"{ref}..HEAD", f"^{self.base}")
        if touched - {f"{self.files.rel}/{kind.file}", f"{self.files.rel}/questions.md"}:
            return "", []
        text = form.read_text()
        verdict = FeatureFiles.verdict_of(text, role)
        if verdict:
            return verdict, []
        parsed = FeatureFiles.review_form(text)
        if parsed is None:
            return "", [f"{self.slug}: {kind.file} needs a `## Findings` and a `## Checked` section (sdd §2)"]
        ticked, boxes, _ = parsed
        n = len(checkpoints(role))
        if boxes != n:
            return "", [f"{self.slug}: {kind.file} lists {boxes} checkpoints, the form has {n}: keep the seeded boxes"]
        return "", [
            f"{self.slug}: {kind.file} ticks {ticked} of the {n} checkpoints and lists no finding: "
            "tick them all (APPROVED) or write what is wrong under ## Findings (CHANGES_REQUESTED)"
        ]

    @classmethod
    def run(cls, checkout: Path, slugs: list[str], tests: bool | None) -> list[str]:
        """`check`: each feature's state and errors, then the tests. tests: None = only if any feature is at review or
        later; True/False = force."""
        cfg = Config.load(checkout)
        errs, need_tests = [], bool(tests)
        for s in slugs:
            c = cls(checkout, s, cfg.base_branch)
            st = c.state()
            print(f"{s}: {st}")
            errs += c.errors()
            if tests is None and st in ("review", "reviewed"):
                need_tests = True
        return errs + (cls.tests(checkout, cfg) if need_tests else [])


# ----------------------------------------------------------------------------- running a role
@dataclass(frozen=True)
class Outcome:
    """How a role run ended, judged from the files on disk after the launcher committed them, never from the agent's
    screen alone."""

    result: str  # DONE | NEEDS_ATTENTION
    note: str
    errors: list
    agent_state: str


class RoleRun:
    """One role agent's run on one feature in its worktree, for the driver: the prompt it gets, the commit of what
    it wrote (rule 1), and whether the feature's state afterwards means it is done."""

    def __init__(self, cfg: Config, root: Path, wt: Path, role: str, slug: str):
        self.cfg, self.root, self.wt, self.role, self.slug = cfg, root, Path(wt), role, slug
        self.name, self.agent = agent_name(role, slug, root), cfg.agent(role)
        self.files, self.git = FeatureFiles(wt, slug), Git(wt)
        self.checker = Checker(wt, slug, cfg.base_branch)
        self.start_sha = ""

    @staticmethod
    def prompt_for(role: str, slug: str, msg: str, base: str, bootstrap: bool = False) -> str:
        """The job line: the role file, the feature, what the launcher knows (the base branch; whether the project has
        code yet), then the driver's message."""
        facts = f"base branch: {base}" + (
            "; no project code yet: this feature sets the project up" if bootstrap else ""
        )
        text = f"Read harness/roles/{role}.md and follow it for harness/features/{slug}/ ({facts})."
        return text + (" " + " ".join(msg.split()) if msg else "")

    def start(self):
        """Remember where the run starts: finish() folds everything after it."""
        self.start_sha = self.git.head()

    def send(self, msg: str, timeout_ms: int) -> tuple[bool, str]:
        """Prompt the role agent with its job (starting it if needed); (settled, note)."""
        text = self.prompt_for(self.role, self.slug, msg, self.cfg.base_branch, not self.git.project_paths())
        return HERDR.run_role(self.root, self.wt, self.role, self.slug, text, timeout_ms, self.agent)

    def finish(self) -> list[str]:
        """Rule 1: fold any commit the agent made, commit its write set as one `<slug>: <role>` commit, discard the
        rest. Returns the discarded paths."""
        self.git.reset_to(self.start_sha)
        for group in self.files.write_set(self.role):
            self.git.add_all(group)
        if self.git.staged():
            self.git.commit_index(f"{self.slug}: {self.role}")
        return self.git.restore_rest()

    def outcome(self, note: str, discarded: list[str]) -> Outcome:
        """DONE when the feature's state is one the role must leave it in, whatever the agent's own state says; a
        reviewing role whose CHANGES_REQUESTED is the max_review_rounds-th blocks the feature for the human. Otherwise
        what is missing goes back to the role (errors); a run with nothing to send back stops for the human (note)."""
        role, slug = self.role, self.slug
        state = HERDR.state(self.name)
        st = self.checker.state()
        errs = [f"{slug}: discarded, outside the {role}'s files: {', '.join(discarded)}"] if discarded else []
        if st in DONE_WHEN[role]:
            kind, cap = REVIEWS.get(role), self.cfg.max_review_rounds
            if kind and cap and self.checker.review(role)[0] == "CHANGES_REQUESTED":
                rounds = self.files.review_rounds(role, self.cfg.base_branch)
                if rounds >= cap:
                    self.files.ask(
                        f"{rounds} {kind.name} rounds ended in CHANGES_REQUESTED (max_review_rounds {cap}); "
                        f"the findings are in {kind.file}"
                    )
                    return Outcome("NEEDS_ATTENTION", f"blocked after {rounds} {kind.name} rounds", errs, state)
            return Outcome("DONE", "", errs, state)
        if state == "blocked" or note:
            return Outcome("NEEDS_ATTENTION", note, [], state)
        errs += self.checker.errors()
        if errs:
            return Outcome("NEEDS_ATTENTION", "check fails", errs, state)
        return Outcome("NEEDS_ATTENTION", f"left the feature at '{st}' and changed nothing that counts", [], state)


# ----------------------------------------------------------------------------- driver
class _Stop(Exception):  # noqa: N818 - a control-flow signal (the driver's exit code), not an error
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class Driver:
    """The orchestrator's loop as a process: read the feature's state, run the role it names, commit what the role
    wrote, send what is missing back, approve and merge when the gates are auto, stop (and notify) when the feature
    needs a human. The first message goes to the first role run; at a gate or when blocked it goes to the role that
    produced what is being judged."""

    def __init__(self, root: Path, cfg: Config, f: Feature, msg: str, notify_pane: str):
        self.root, self.cfg, self.f, self.msg, self.notify_pane = root, cfg, f, msg, notify_pane
        self.slug, self.wt, self.base, self.gates = f.slug, f.wt, cfg.base_branch, cfg.gates
        self.files, self.fdir = f.files, f.dir
        self.checker = Checker(self.wt, self.slug, self.base)
        self.lock = Lock.driver(self.wt)
        self.text = ""  # the message of the current step
        self.last_role, self.retries = "", 0  # the same role running again is a retry; another role resets it

    def run(self) -> int:
        holder = self.lock.acquire()
        if holder:
            who = f"a driver (pid {holder})" if holder > 0 else "another driver"
            print(f"error: {who} is already running {self.slug}", file=sys.stderr)
            return EXIT_ERROR
        self.lock.write_stamp()
        self.log(
            f"driving {self.slug} in {self.wt}" + (f"; will notify pane {self.notify_pane}" if self.notify_pane else "")
        )
        try:
            while True:
                self.step()
        except _Stop as s:
            return s.code
        except (Die, Exception) as e:  # the launcher itself failed: the human must still hear of it
            if isinstance(e, HerdrError):
                why = e.message
            elif isinstance(e, Die):
                why = e.msg
            else:
                traceback.print_exc()
                why = f"{type(e).__name__}: {e}"
            pending = f"; undelivered message: {' '.join(self.text.split())[:200]}" if self.text else ""
            line = f"harness: {self.slug} driver failed: {next(iter(why.splitlines()), '')}{pending}"
            self.log(line)
            with contextlib.suppress(Die, Exception):
                HERDR.sidebar(self.wt, state_text(self.checker.state(), self.gates))
            if self.notify_pane:
                HERDR.notify(self.notify_pane, line + f" (board: {CMD} status)")
            return EXIT_ERROR
        finally:
            self.lock.release()

    @staticmethod
    def log(text: str):
        print(f"[{datetime.now():%H:%M}]", text)

    def stop(self, text: str, code: int = EXIT_DONE):
        HERDR.sidebar(self.wt, state_text(self.checker.state(), self.gates))
        line = f"harness: {self.slug} {text}"
        self.log(line)
        if self.notify_pane:
            HERDR.notify(self.notify_pane, line + f" (board: {CMD} status)")
        raise _Stop(code)

    def step(self):
        self.cfg = Config.load(self.root)  # config.json as it is now: a change applies to the next run
        self.gates = self.cfg.gates
        self.finish_leftovers()
        st = self.checker.state()
        HERDR.sidebar(self.wt, state_text(st, self.gates))
        self.text, self.msg = self.msg, ""
        at = {
            "done": lambda t: self.stop("is done"),
            "blocked": self.at_blocked,
            "spec": lambda t: ("spec_author", t),  # the role file says what to do; a message says what is special
            "spec_review": lambda t: self.new_round("spec_reviewer", t),
            "spec_ready": self.at_approve_gate,
            "implementing": lambda t: ("implementer", self.with_conflicts(t)),
            "review": self.before_review,
            "reviewed": self.at_merge_gate,
        }
        found = at[st](self.text)
        if found:
            self.run_role(*found)

    def finish_leftovers(self):
        """Uncommitted changes in the worktree are a run the launcher did not finish (max_role_minutes, a driver that
        died): commit them under the role they belong to, as its finish() would have, before reading the state."""
        left = sorted({p for _, p in Git(self.wt).porcelain()})
        if not left:
            return
        judge, maker = ("reviewer", "implementer") if self.checker.approved() else ("spec_reviewer", "spec_author")
        own = {f"{self.files.rel}/{REVIEWS[judge].file}", f"{self.files.rel}/questions.md"}
        role = judge if set(left) <= own else maker
        self.log(f"committing what an unfinished {role} run left: {', '.join(left[:8])}")
        run = RoleRun(self.cfg, self.root, self.wt, role, self.slug)
        run.start()
        discarded = run.finish()
        if discarded:
            self.log(f"discarded, outside the {role}'s files: {', '.join(discarded)}")

    def with_conflicts(self, text: str) -> str:
        """The implementer's message, plus the conflict a base merge left in the files (markers), if any."""
        files = Git(self.wt).conflict_markers(self.base)
        if files:
            text += f"\nThe base branch '{self.base}' was merged in and conflicts in {', '.join(files)}: resolve them."
        return text.strip()

    def at_blocked(self, text: str) -> tuple[str, str] | None:
        """A question waits in questions.md: stop for the human, or clear it and hand their answer to the role the
        state names next (the one whose work the question interrupted)."""
        if not text:
            self.stop(
                f"blocked, needs you: read {self.files.questions_md}; "
                f'answer with `{CMD} drive {self.slug} "<answer>" --tab`'
            )
        asked = self.files.answer()
        self.msg = f"The human answered the questions in questions.md.\nQuestions:\n{asked}\nAnswer: {text}"
        return None

    def at_approve_gate(self, text: str) -> tuple[str, str] | None:
        if text:  # feedback for the spec, whoever approves
            return "spec_author", text
        if self.gates["approve"] == "auto":
            self.git_approve()
            return None
        self.stop(
            f"spec ready, needs you: read {self.fdir / 'spec.md'}, then `{CMD} approve {self.slug}`, "
            f'or `{CMD} drive {self.slug} "<feedback for the spec author>" --tab`'
        )

    def git_approve(self):
        Git(self.wt).set_ref(APPROVED_REF.format(slug=self.slug))
        self.log(f"approved {self.slug} (auto)")

    def at_merge_gate(self, text: str) -> tuple[str, str] | None:
        if text:  # feedback for the code, whoever merges
            return "implementer", text
        if self.gates["merge"] == "auto":
            self.spawn_merge()
        self.stop(f"reviewed, needs you: `{CMD} merge {self.slug}` from the main checkout")

    def before_review(self, text: str) -> tuple[str, str] | None:
        """The reviewer sees the branch as it will land, and only when it is green: the base branch merged in (a
        conflict is committed with its markers, which rule 5 sends to the implementer) and test_cmd green."""
        try:
            conflicted = Git(self.wt).merge_in(self.base, f"{self.slug}: merge {self.base}")
        except Die as e:  # a conflict git cannot mark in the file: the merge is aborted, the human resolves it
            self.stop(f"needs you: {e.msg}", EXIT_ATTENTION)
        if conflicted:
            self.log(f"merging {self.base} in conflicts in {', '.join(conflicted)}; the implementer resolves them")
            self.msg = text  # the markers make the state `implementing`; the implementer's message names them
            return None
        errs = Checker.tests(self.wt, self.cfg)
        if errs:
            out = "\n".join(errs)[-1500:]
            return "implementer", f"{text}\ntest_cmd fails with {self.base} merged in:\n{out}".strip()
        return self.new_round("reviewer", text)

    def new_round(self, role: str, text: str) -> tuple[str, str]:
        """A round of the role's review: its form cleared and seeded, the round marked (the review is of HEAD from
        here, sdd §1), then the role runs."""
        self.files.clear_review(role)
        self.files.seed_review(role)
        Git(self.wt).set_ref(REVIEWS[role].ref.format(slug=self.slug))
        return role, text

    def wait_role(self, role: str, settled: bool, note: str, started: float) -> str:
        """Wait until the role agent settles; returns its note. Past max_role_minutes since `started`, stop for the
        human instead (the agent is left running: whether to stop it is theirs to decide)."""
        name, limit = agent_name(role, self.slug, self.root), self.cfg.max_role_minutes
        while not settled:
            minutes = int((time.time() - started) // 60)
            if limit and minutes >= limit:
                self.stop(
                    f"needs you: {role} still working after {minutes} min (max_role_minutes {limit}); "
                    f"inspect: {HERDR.read_hint(name)}",
                    EXIT_ATTENTION,
                )
            settled, note = HERDR.wait(name, DRIVE_WAIT_MS)
        return note

    def run_role(self, role: str, text: str):
        self.retries = self.retries + 1 if role == self.last_role else 0
        self.last_role = role
        if self.retries >= FIX_TRIES:
            st = self.checker.state()
            self.files.ask(
                f"{role} ran {FIX_TRIES} times in a row and the feature is still at '{st}'. Last message to it:\n{text}"
            )
            self.stop(f"blocked, needs you: {role} ran {FIX_TRIES} times in a row, still at '{st}'", EXIT_ATTENTION)
        run = RoleRun(self.cfg, self.root, self.wt, role, self.slug)
        run.start()
        self.log(f"run {role}" + (f": {' '.join(text.split())[:120]}" if text else ""))
        HERDR.sidebar(self.wt, state_text(self.checker.state(), self.gates, running=role))
        started = time.time()
        settled, note = run.send(text, DRIVE_WAIT_MS)
        note = self.wait_role(role, settled, note, started)
        out = run.outcome(note, run.finish())
        self.log(f"{run.name}: state={out.agent_state} -> {out.result}" + (f" [{out.note}]" if out.note else ""))
        if out.errors:
            print("\n".join(out.errors))
        if out.result == "DONE":
            HERDR.close_role_tab(self.wt, role)  # the run is judged; a tab stays only when the human must look
            return
        if self.checker.state() == "blocked":  # the launcher asked the human (a cap): their move
            self.stop(f"blocked, needs you: {out.note}; read {self.files.questions_md}", EXIT_ATTENTION)
        if out.errors:  # back to the same role with what is missing; the loop reads the state again
            HERDR.close_role_tab(self.wt, role)
            self.msg = "check fails:\n" + "\n".join(out.errors) + "\nFix it."
            return
        where = "its agent is gone" if out.agent_state == "gone" else f"inspect: {HERDR.read_hint(run.name)}"
        self.stop(f"needs you: {role} stopped ({out.note or 'see its screen'}); {where}", EXIT_ATTENTION)

    def spawn_merge(self):
        """`merge` removes this worktree, its workspace and this very pane: run it detached from the main checkout."""
        main = Git(self.wt).main_checkout()
        log = main / ".git" / f"harness-merge-{self.slug}.log"
        self.lock.release()  # `merge` refuses while a driver runs; this one is done
        cmd = [sys.executable, str(Path(__file__).resolve()), "merge", self.slug]
        cmd += ["--notify", self.notify_pane] if self.notify_pane else []  # `merge` reports its own outcome
        env = {k: v for k, v in os.environ.items() if k != "HC_SCOPE"}  # with it, ending this terminal ends `merge`
        with log.open("w") as fh:
            subprocess.Popen(
                cmd, cwd=main, env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )  # fmt: skip
        self.log(f"merging {self.slug} in the background (log: {log}); this workspace closes when it is done")
        raise _Stop(EXIT_DONE)


# ----------------------------------------------------------------------------- doctor
def probe_model(cli: Cli, model: str, cwd: Path, effort: str = "", timeout_s: int = 120) -> tuple[bool, str]:
    """(answered, output tail) of one tiny prompt to the CLI with this model and effort."""
    try:
        p = subprocess.run(
            cli.probe_cmd(model, "Reply with the word OK.", effort),
            cwd=cwd, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout_s,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return False, f"no answer within {timeout_s} s"
    except OSError as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-1500:]


class Doctor:
    """The setup, then tools, config and leftovers of the repo at `root` (its main checkout). `run()` prints it."""

    herdr_cfg = Path.home() / ".config" / "herdr" / "config.toml"  # where the sidebar rows go

    def __init__(self, root: Path, tests: bool = True, models: bool = False):
        self.root, self.tests, self.models = root, tests, models
        self.problems: list[str] = []
        self.notes: list[str] = []

    def check(self, cond: bool, msg: str, fix: str = ""):
        print(("ok    " if cond else "FAIL  ") + msg + ("" if cond or not fix else f"\n      fix: {fix}"))
        if not cond:
            self.problems.append(msg)

    @staticmethod
    def setup(cfg: Config):
        """The setup the next feature runs with, for the orchestrator to show the human before `start`."""
        print("harness/config.json")
        print(f"  base_branch        {cfg.base_branch}")
        print(f"  test_cmd           {cfg.test_cmd}")
        print(f"  setup_cmd          {cfg.setup_cmd or '-'}")
        print(f"  copy_files         {', '.join(cfg.copy_files) or '-'}")
        print(f"  max_review_rounds  {cfg.max_review_rounds or 'no limit'}")
        print(f"  max_role_minutes   {cfg.max_role_minutes or 'no limit'}")
        print(f"  gates              approve: {cfg.gates['approve']}   merge: {cfg.gates['merge']}")
        for role in ROLES:
            agent = cfg.agent(role)
            kind, model = agent.harness, agent.model or "(default model)"
            missing = "   <- not in PATH" if not shutil.which(kind) else ""
            print(f"  {role:<18} {kind:<7} {model}{missing}")
            for line in agent.describe():
                print(f"  {'':<18} {line}")
        print("  effort: " + " < ".join(EFFORT_LEVELS) + "   permissions: " + ", ".join(PERMISSION_LEVELS))
        print("  orchestrator       the agent you started (not in this file): its model and permissions are its own")
        print("edit harness/config.json (keys: harness/help/config.md); the launcher validates it on every command")

    def run(self) -> int:
        cfg = Config.load(self.root)  # dies naming the problem when the file is not valid
        self.setup(cfg)
        print()
        self.check(shutil.which("git") is not None, "git in PATH")
        self.check(HERDR.available(), f"{HERDR.binary} in PATH (runtime: {HERDR.label})", HERDR.install)
        self.config(cfg)
        self.project_section()
        self.baseline(cfg)
        if self.models:
            self.models_check(cfg)
        self.git_leftovers(cfg.base_branch)
        if HERDR.available():
            self.herdr_state()
        for n in self.notes:
            print("note  " + n)
        print(f"\n{len(self.problems)} problem(s)" if self.problems else "\nall good")
        return 1 if self.problems else 0

    def config(self, cfg: Config):
        self.check(
            Git(self.root).ref_exists(f"refs/heads/{cfg.base_branch}"), f"base branch '{cfg.base_branch}' exists"
        )
        for role in ROLES:
            kind = cfg.agent(role).harness
            self.check(
                shutil.which(kind) is not None,
                f"{role}: '{kind}' CLI in PATH",
                f"install it and log in, or change agents.{role}.harness",
            )
        for pat in cfg.copy_files:
            if list(self.root.glob(pat)):
                self.check(True, f"copy_files: {pat} is in the main checkout")
            else:
                self.notes.append(f"copy_files: {pat} is not in the main checkout, so new worktrees will not get it")

    def project_section(self):
        """AGENTS.md §1 still holding the template's placeholders: the orchestrator fills it in."""
        p = self.root / "AGENTS.md"
        text = p.read_text() if p.exists() else ""
        start = text.find("## 1.")
        section = text[start : text.find("## 2.", start)] if start >= 0 else ""
        if any(t in section for t in SECTION_PLACEHOLDERS):
            self.notes.append("AGENTS.md §1 is not filled in yet: the orchestrator asks you before the first feature")

    def baseline(self, cfg: Config):
        """test_cmd on the main checkout: a red baseline would be blamed on the first feature's implementer."""
        if not self.tests:
            self.notes.append("test_cmd not run (--no-tests)")
            return
        if not Git(self.root).project_paths():
            self.notes.append(
                "no project code yet (only the template's files): test_cmd not run; "
                "the first feature sets the project up so that it passes"
            )
            return
        code, out = shell(cfg.test_cmd, self.root)
        fix = (
            "a red baseline gets blamed on the first feature's implementer: set up the project "
            "(e.g. pyproject.toml and tests/ for the shipped uv/pytest commands) or change test_cmd"
        )
        self.check(code == 0, f"test_cmd green on the main checkout ({cfg.test_cmd})", fix)
        if code != 0:
            print("\n".join("      | " + line for line in out.splitlines()[-15:]))

    def models_check(self, cfg: Config):
        """`--models`: one tiny prompt per (CLI, model) pair, so a wrong model id fails here, not mid-feature."""
        seen: dict[tuple, bool] = {}
        for role in ROLES:
            agent = cfg.agent(role)
            kind, model, cli = agent.harness, agent.model, agent.cli
            if not (cli and cli.probe):
                self.notes.append(f"{role}: {kind} has no probe here, its model is not checked")
                continue
            if (kind, model) not in seen:
                seen[(kind, model)], out = probe_model(cli, model, self.root, agent.effort)
                if not seen[(kind, model)]:
                    print("\n".join("      | " + line for line in out.splitlines()[-8:]))
            fix = f"its output is above: a wrong id (agents.{role}.model), not logged in, or no credits for it"
            self.check(seen[(kind, model)], f"{role}: {kind} answers with model {model or '(default model)'}", fix)

    def git_leftovers(self, base: str):
        root = self.root
        feats = Feature.all(root, base)
        wts = Git(root).worktrees()
        live = {branch for _, branch in wts}
        for wt, branch in wts[1:]:
            f = feats.get(branch)
            if not wt.exists():
                self.check(False, f"worktree {wt} is missing on disk", "git worktree prune")
            elif not f or f.wt != wt:
                self.check(
                    False, f"worktree {wt} ({branch or 'detached'}) has no feature folder", f"git worktree remove {wt}"
                )
            else:
                self.check(True, f"worktree {wt}: {branch} ({f.state})")
        for b in Git(root).run("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
            merged = (root / "harness" / "features" / b / "spec.md").exists()
            if b != base and b not in live and (merged or Git(root).ref_exists(APPROVED_REF.format(slug=b))):
                fix = f"git branch -D {b}" if merged else HERDR.reopen_hint(root, b)
                why = "its feature is merged" if merged else "its feature is not merged"
                self.check(False, f"branch '{b}' has no worktree ({why})", fix)
        for ref in Git(root).run("for-each-ref", "--format=%(refname)", "refs/harness", check=False).splitlines():
            slug = ref.split("/")[2] if ref.count("/") >= 3 else ""
            if slug not in live:
                self.check(False, f"stale {ref}", f"git update-ref -d {ref}")

    def herdr_state(self):
        root = self.root
        try:
            wss = HERDR.workspaces()
        except HerdrError as e:
            self.check(False, f"{HERDR.label} server reachable ({e.code}: {e.message})", f"open {HERDR.label}")
            return
        self.check(True, f"{HERDR.label} server reachable")
        for w in wss:
            info = w.get("worktree") or {}
            checkout = Path(info["checkout_path"]) if info.get("checkout_path") else None
            linked_here = (
                info.get("is_linked_worktree")
                and info.get("repo_root")
                and Path(info["repo_root"]).resolve() == root.resolve()
            )
            if linked_here and checkout and not checkout.exists():
                self.check(
                    False,
                    f"workspace {w['workspace_id']} '{w.get('label')}' points at a removed worktree",
                    f"herdr workspace close {w['workspace_id']}",
                )
        cfgp = self.herdr_cfg
        text = cfgp.read_text() if cfgp.exists() else ""
        if HERDR.sidebar_config and "$harness" not in text:
            self.notes.append(
                f"herdr's sidebar does not show feature states yet. Add to {cfgp}:\n{SIDEBAR_ROWS}\n"
                "then: herdr server reload-config"
            )
        self.notes.append("the launcher's own tests: python3 harness/selftest.py")


# ----------------------------------------------------------------------------- commands
def cmd_status(a):
    root = Git.here().path
    cfg = Config.load(root)
    feats = Feature.all(root, cfg.base_branch)
    if not feats:
        print(f'no features yet. start one: {CMD} start <slug> "intent"')
        return 0
    home = str(Path.home())
    groups: dict[str, list[tuple]] = {"needs you": [], "to run": [], "working": [], "done": []}
    for slug, f in sorted(feats.items()):
        nxt = next_action(f.state, cfg.gates)
        where = f"{f.branch or '(detached)'}@{str(f.wt).replace(home, '~', 1)}"
        if f.state == "done":
            group = "done"
        elif needs_human(f.state, cfg.gates):
            group = "needs you"
        elif f.live and Lock.driver(f.wt).running():
            group, nxt = "working", nxt + "  (driver running)"
        else:
            group = "to run"  # nothing runs on its own: the orchestrator starts the driver
        groups[group].append((slug, f.state, where, nxt))
    print(" · ".join(f"{len(v)} {k}" for k, v in groups.items() if v))
    header = ("SLUG", "STATE", "BRANCH@PATH", "NEXT")
    widths = [max(len(r[i]) for g in groups.values() for r in [header, *g]) for i in range(4)]
    print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(header)).rstrip())
    for group, rows in groups.items():
        if rows:
            print(group)
            for r in rows:
                print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return 0


def cmd_check(a):
    root = Git.here().path
    if a.slug:
        slugs = [a.slug]
        if not FeatureFiles(root, a.slug).spec.exists():
            root = Feature.find(root, a.slug, Config.load(root).base_branch).wt  # it lives in another worktree
            print(f"checking {a.slug} in {root}")
    else:  # a feature is a folder with a spec.md, as `status` finds them
        slugs = sorted(
            p.parent.name
            for p in (root / "harness" / "features").glob("*/spec.md")
            if not p.parent.name.startswith("_")
        )
    tests = True if a.tests else (False if a.no_tests else None)
    errs = Checker.run(root, slugs, tests)
    if errs:
        print("\n".join(errs))
        return 1
    print(f"OK ({', '.join(slugs) or 'no features'})")
    return 0


def start_preconditions(root: Path, cfg: Config, slug: str):
    if not SLUG_RE.fullmatch(slug):
        die(f"slug must match {SLUG_RE.pattern} (lowercase, digits, dashes; at most 20 characters)")
    if not Git(root).is_main():
        die("run start from the main checkout, not from a worktree")
    if Git(root).branch() != cfg.base_branch:
        die(f"run start from the base branch '{cfg.base_branch}' (currently on '{Git(root).branch() or 'detached'}')")
    if Git(root).ref_exists(f"refs/heads/{slug}"):
        die(f"branch '{slug}' already exists")
    if slug in Feature.all(root, cfg.base_branch):
        die(f"feature '{slug}' already exists (see: {CMD} status)")
    if not (root / "harness" / "features" / "_template" / "spec.md").exists():
        die("harness/features/_template/spec.md is missing")
    if Git(root).dirty():
        print("warning: the base checkout has uncommitted changes; the worktree starts from the last commit")


def seed_feature(wt: Path, slug: str, intent: str):
    """The feature folder from the template, committed on the new branch."""
    fdir = FeatureFiles(wt, slug).dir
    shutil.copytree(wt / "harness" / "features" / "_template", fdir)
    title = slug.replace("-", " ").capitalize()
    intent = intent or "To be defined with the human: ask what this feature must do before writing requirements."
    for p in fdir.iterdir():
        p.write_text(p.read_text().replace("{{title}}", title).replace("{{slug}}", slug).replace("{{intent}}", intent))
    Git(wt).run("add", f"harness/features/{slug}")
    Git(wt).run("commit", "-q", "-m", f"{slug}: new feature")  # everything staged, as before


def cmd_start(a):
    root = Git.here().path
    cfg = Config.load(root)
    start_preconditions(root, cfg, a.slug)
    out = io.StringIO()  # the setup is checked before every feature; not test_cmd: the driver runs it before a review
    with contextlib.redirect_stdout(out):
        doc = Doctor(root, tests=False)
        doc.run()
    if doc.problems:
        die(out.getvalue().rstrip() + f"\ndoctor finds {len(doc.problems)} problem(s): fix them, then start again")
    print("doctor: no problem")
    res = HERDR.create_worktree(root, a.slug, cfg.base_branch)
    wt = Path(res["worktree"]["path"])
    seed_feature(wt, a.slug, a.intent)
    WorktreeSetup(root, wt, cfg).run()
    HERDR.sidebar(wt, state_text("spec", cfg.gates))
    ws_id = res["workspace"]["workspace_id"]
    print(f"worktree: {wt}\nworkspace: {ws_id}  branch: {a.slug}  feature: harness/features/{a.slug}/")
    try:
        return drive_in_tab(root, wt, a.slug, "")
    except HerdrError as e:
        die(f"herdr {e.code}: {e.message}\nthe feature exists; start its driver: {CMD} drive {a.slug} --tab")


def drive_in_tab(root: Path, wt: Path, slug: str, msg: str) -> int:
    """Run the driver in the `driver` tab of the feature's workspace; it prompts this pane when it stops."""
    pane = HERDR.launch_driver(Git(root).main_checkout(), wt, slug, msg, HERDR.self_id())
    print(
        f"driver for {slug}: pane {pane}, tab 'driver' of its workspace. It runs the roles one after another "
        "and stops when the feature needs a human; then it prompts this pane (if an agent runs here) "
        "and `status` says what to do."
    )
    print(f"watch: {HERDR.pane_hint(pane)}")
    return 0


def cmd_approve(a):
    root = Git.here().path
    cfg = Config.load(root)
    f = Feature.find(root, a.slug, cfg.base_branch)
    pid = Lock.driver(f.wt).running()
    if pid:
        die(f"a driver (pid {pid}) is running {a.slug}; wait until status shows 'needs you'")
    ref = APPROVED_REF.format(slug=a.slug)
    if a.undo and (not f.live or not Git(f.wt).ref_exists(ref)):
        die(f"'{a.slug}' is not approved ({f.state})")
    if not a.undo and f.state != "spec_ready":
        errs = Checker(f.wt, a.slug, cfg.base_branch).errors()
        print("\n".join(errs))
        die(f"'{a.slug}' is {f.state}, expected spec_ready")
    if Git(f.wt).dirty():  # an unfinished run's files: once committed under their role, they are judged, not lost
        die(f"{f.wt} has uncommitted changes from an unfinished run; `{CMD} drive {a.slug} --tab` commits them")
    if a.undo:  # the spec may change again: `drive <slug> "<what changes>" --tab` runs the spec author
        Git(f.wt).run("update-ref", "-d", ref)
        # the spec was frozen while approved, so the spec review that stood at the approval stands again (sdd §1)
        Git(f.wt).set_ref(REVIEWS["spec_reviewer"].ref.format(slug=a.slug))
        state = Checker(f.wt, a.slug, cfg.base_branch).state()
        print(f'approval of {a.slug} withdrawn ({state}); next: {CMD} drive {a.slug} "<what changes>" --tab')
        HERDR.sidebar(f.wt, state_text(state, cfg.gates))
        return 0
    Git(f.wt).set_ref(ref)
    print(f"approved {a.slug} (the spec at {Git(f.wt).run('rev-parse', '--short', 'HEAD')})")
    HERDR.sidebar(f.wt, state_text("implementing", cfg.gates))
    return HERDR.resume(Git(root).main_checkout(), f.wt, a.slug)


def cmd_drive(a):
    root = Git.here().path
    cfg = Config.load(root)
    f = Feature.live_one(root, a.slug, cfg.base_branch)
    pid = Lock.driver(f.wt).running()
    if pid:
        die(f"a driver (pid {pid}) is already running {a.slug}")
    if a.tab:
        return drive_in_tab(root, f.wt, a.slug, a.msg)
    msg = a.msg
    if a.msg_file:  # what `drive --tab` typed into the driver tab: the message, read once
        msg = Path(a.msg_file).read_text()
        Path(a.msg_file).unlink()
    return Driver(root, cfg, f, msg, a.notify).run()


def merge_preconditions(root: Path, cfg: Config, slug: str) -> Feature:
    base = cfg.base_branch
    if not Git(root).is_main():
        die("run merge from the main checkout, never from inside a worktree")
    if Git(root).branch() != base:
        die(f"run merge from the base branch '{base}' (currently on '{Git(root).branch() or 'detached'}')")
    if Git(root).dirty(untracked=False):
        die("the main checkout has uncommitted changes; commit or stash them first")
    f = Feature.find(root, slug, base)
    if not f.live or f.wt == root:
        die(f"no live worktree on branch '{slug}' (already merged?)")
    if f.state != "reviewed":
        die(f"'{slug}' is {f.state}; need a review of the branch as it stands that says APPROVED")
    if Git(f.wt).dirty():
        die(f"worktree {f.wt} has uncommitted changes; run the driver so they are committed, or discard them")
    pid = Lock.driver(f.wt).running()
    if pid:
        die(f"a driver (pid {pid}) is still running {slug}; wait until status shows 'merge (human)'")
    return f


def land(root: Path, wt: Path, slug: str, base: str):
    """Base merged into the branch and checked, then the branch merged into the base. A conflict or a failed check
    means the review no longer stands for what would land: it is cleared, the feature goes back through the loop
    (implementer, tests, reviewer), and `merge` is run again."""
    conflicted = Git(wt).merge_in(base, f"{slug}: merge {base}")
    errs = [] if conflicted else Checker.run(wt, [slug], True)
    if conflicted or errs:
        FeatureFiles(wt, slug).clear_review("reviewer")
        print("\n".join(errs))
        why = f"conflicts in {', '.join(conflicted)}" if conflicted else "the check fails"
        die(f"'{base}' merged into '{slug}': {why}; not merging. Run `{CMD} drive {slug} --tab`, then merge again")
    if not Git(root).ok("merge", "--no-ff", "-m", f"{slug}: merge", slug):
        Git(root).proc("merge", "--abort", check=False)
        die(f"merging '{slug}' into '{base}' failed; '{base}' is left as it was")


def remove_worktree(root: Path, wt: Path, slug: str) -> str:
    """The worktree (with its herdr workspace when there is one), then the launcher's refs and the branch. Returns
    what went wrong, or an empty string."""
    if not HERDR.remove_worktree(wt) and not Git(root).ok("worktree", "remove", "--force", wt):
        return f"could not remove the worktree {wt}; the feature stays reviewed: run merge again once it can go"
    Git(root).proc("worktree", "prune", check=False)
    Git(root).drop_refs(REFS.format(slug=slug))
    if not Git(root).ok("branch", "-d", slug):
        return f"could not delete the branch {slug}: git branch -d {slug}"
    return ""


def cmd_merge(a):
    root = Git.here().path
    cfg = Config.load(root)
    lock = Lock.merge(root)
    log = root / ".git" / f"harness-merge-{a.slug}.log"
    try:
        f = merge_preconditions(root, cfg, a.slug)
        if lock.acquire():
            die(f"another merge is running (pid {lock.running()}); run merge again when it is done")
        try:
            land(root, f.wt, a.slug, cfg.base_branch)
        finally:
            lock.release()
    except Die as e:  # a detached merge has nobody watching its output: say why it failed
        if a.notify:
            HERDR.notify(a.notify, f"harness: {a.slug} merge failed: {e.msg.splitlines()[0]} (log: {log})")
        raise
    sha = Git(root).run("rev-parse", "--short", "HEAD")
    print(f"merged {a.slug} into {cfg.base_branch} at {sha}")
    problem = remove_worktree(root, f.wt, a.slug)
    print(problem or "worktree removed; branch deleted")
    if a.notify:
        tail = f"; cleanup failed: {problem}" if problem else ""
        HERDR.notify(a.notify, f"harness: {a.slug} merged into {cfg.base_branch} at {sha}{tail}")
    return 0


def cmd_doctor(a):
    return Doctor(Git.here().main_checkout(), tests=not a.no_tests, models=a.models).run()


# ----------------------------------------------------------------------------- cli
def parser() -> argparse.ArgumentParser:
    """The command list: `--help` is its only copy."""
    ap = argparse.ArgumentParser(prog=CMD, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    s = sub.add_parser("status", help="the board across every worktree, grouped by whose move it is (anyone)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("start", help="new worktree, branch and feature folder, then `drive --tab` (base branch)")
    s.add_argument("slug")
    s.add_argument("intent", nargs="?", default="", help="what the feature must do, verbatim for the spec author")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("drive", help="run the loop until a human is needed; --tab: in the feature's `driver` tab")
    s.add_argument("slug")
    s.add_argument(
        "msg", nargs="?", default="", help="message for the first role run (at a gate: for the role being judged)"
    )
    s.add_argument(
        "--tab", action="store_true", help="run it in the 'driver' tab of the feature's workspace and return at once"
    )
    s.add_argument(
        "--notify",
        default="",
        metavar="PANE",
        help="pane or terminal (hosting an agent) to prompt when the driver stops",
    )
    s.add_argument("--msg-file", default="", help=argparse.SUPPRESS)  # how `drive --tab` hands over the message
    s.set_defaults(fn=cmd_drive)

    s = sub.add_parser("approve", help="approve a spec; the driver continues (human)")
    s.add_argument("slug")
    s.add_argument("--undo", action="store_true", help="withdraw the approval: the spec may change again")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("merge", help="merge an approved, reviewed feature; remove the worktree (human, base branch)")
    s.add_argument("slug")
    s.add_argument(
        "--notify", default="", metavar="PANE", help="pane or terminal (hosting an agent) to prompt with the outcome"
    )
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("check", help="this checkout's features: state and what is missing (everyone)")
    s.add_argument("slug", nargs="?")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--tests", action="store_true", help="also run test_cmd (by default: from state review on)")
    g.add_argument("--no-tests", action="store_true", help="never run test_cmd")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("doctor", help="the setup, then tools, config, a green test_cmd, leftovers (anyone)")
    s.add_argument("--no-tests", action="store_true", help="skip running test_cmd on the main checkout")
    s.add_argument(
        "--models", action="store_true", help="also send each Claude Code and Codex role one tiny prompt with its model"
    )
    s.set_defaults(fn=cmd_doctor)
    return ap


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except HerdrError as e:
        print(f"error: herdr {e.code}: {e.message}", file=sys.stderr)
        return EXIT_ERROR
    except Die as e:
        print(f"error: {e.msg}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
