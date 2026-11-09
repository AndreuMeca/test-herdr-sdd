#!/usr/bin/env python3
"""The launcher's own tests: python3 harness/selftest.py [-v].

Stdlib unittest, one temporary git repo (main checkout + one feature worktree) per test, no herdr: the launcher
talks to herdr only through its HERDR adapter, whose methods the tests replace (`stub_herdr`), or a stand-in
`herdr` or `hc` binary on PATH answers. Not named test_*.py on purpose, so a project's own pytest run does not
collect it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from argparse import Namespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["HERDR_ENV"] = "1"  # the terminal app's adapter, wherever these tests run; HerdrAppAdapter picks the other
os.environ.pop("HC_TERMINAL_ID", None)
_spec = importlib.util.spec_from_file_location("harness", HERE / "harness.py")
h = importlib.util.module_from_spec(_spec)
sys.modules["harness"] = h  # dataclasses resolve their annotations through sys.modules
_spec.loader.exec_module(h)
CHECKPOINTS = len(h.checkpoints("reviewer"))
SPEC_CHECKPOINTS = len(h.checkpoints("spec_reviewer"))

R1 = "- R1: WHEN f is called, THE SYSTEM SHALL return 1."
NO_TOKEN = "feat: no test named for R1 in ## Verification carries the token feat:R1 (tests/test_feat.py)"
SPEC = """# Feat

## Intent
do x

## Requirements
{reqs}

## Design
src/m.py

## Verification
| R | Test |
|---|---|
{rows}
"""
AGENTS = {r: {"harness": "claude", "permissions": "full"} for r in h.ROLES}
APPROVED, REVIEW_START = "refs/harness/feat/approved", "refs/harness/feat/review-start"
SPEC_REVIEW_START = "refs/harness/feat/spec-review-start"


def review_text(ticked=CHECKPOINTS, findings="", boxes=CHECKPOINTS) -> str:
    """A review form as a reviewing role leaves it: `ticked` of its `boxes` checkpoints (sdd §5 or §6) `[x]`, the
    rest `[ ]`, and whatever it wrote under ## Findings (nothing = APPROVED when every box is ticked; anything =
    CHANGES_REQUESTED)."""
    lines = "".join(f"- [{'x' if i < ticked else ' '}] {i + 1}\n" for i in range(boxes))
    return f"# Review\n\n## Findings\n{findings}\n\n## Checked\n{lines}"


class Repo:
    """A temporary main checkout with the harness copied in, plus one feature worktree `feat` on branch `feat`."""

    def __init__(self, test_cmd="true", gates=None, rounds=3):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-selftest-"))
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        shutil.copytree(HERE, self.root / "harness", ignore=shutil.ignore_patterns("__pycache__", "selftest.py"))
        self.write_cfg(test_cmd=test_cmd, gates=gates or {}, max_review_rounds=rounds)
        (self.root / "src").mkdir()
        (self.root / "src" / "m.py").write_text("def f():\n    return 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / ".keep").write_text("")
        (self.root / ".gitignore").write_text(".env\ndata/\n")
        self.commit("init")
        self.wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(self.wt))
        self.fdir = self.wt / "harness" / "features" / "feat"
        shutil.copytree(self.wt / "harness" / "features" / "_template", self.fdir)
        for p in self.fdir.iterdir():
            text = p.read_text().replace("{{title}}", "Feat").replace("{{slug}}", "feat").replace("{{intent}}", "do x")
            p.write_text(text)
        self.commit("feat: new feature", cwd=self.wt)

    def write_cfg(self, **extra):
        cfg = {"base_branch": "main", "test_cmd": "true", "copy_files": [".env"], "agents": AGENTS, **extra}
        (self.root / "harness" / "config.json").write_text(json.dumps(cfg))

    def git(self, *args, cwd=None) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.root, check=True, text=True, capture_output=True)
        return p.stdout.strip()

    def commit(self, msg, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", msg, cwd=cwd)

    def log(self, cwd=None) -> list[str]:
        """Commit subjects on the branch, oldest first."""
        return self.git("log", "--reverse", "--format=%s", "main..HEAD", cwd=cwd or self.wt).splitlines()

    # --- what the roles leave on disk (they never commit: the launcher does)
    def spec(self, reqs=R1, rows=None):
        """spec.md; `rows`: the ## Verification rows, by default one per requirement naming tests/test_feat.py."""
        if rows is None:
            ids = re.findall(r"^- (R\d+):", reqs, re.M)
            rows = "\n".join(f"| {r} | `tests/test_feat.py` (feat:{r}) |" for r in ids)
        (self.fdir / "spec.md").write_text(SPEC.format(reqs=reqs, rows=rows))

    def tasks(self, lines="- [ ] T1: test f (R1)"):
        (self.fdir / "tasks.md").write_text("# Tasks\n" + lines + "\n")

    def questions(self, text="- Should f return 2?"):
        (self.fdir / "questions.md").write_text("# Questions\n" + text + "\n")

    def implemented(self, with_test=True):
        """What a finished implementer leaves: ticked tasks, a log, a test carrying the token."""
        self.tasks("- [x] T1: test f (R1)")
        (self.fdir / "log.md").write_text("# Log\n## implementer\n- done: all\n")
        if with_test:
            test = "# feat:R1\ndef test_f():\n    from src.m import f\n    assert f() == 1\n"
            (self.wt / "tests" / "test_feat.py").write_text(test)

    def review(self, verdict, ticked=None, findings=None, role="reviewer"):
        """The role's review form as it writes it (review_text): a finding when the verdict is CHANGES_REQUESTED,
        nothing when APPROVED (the form is the verdict); `ticked` boxes, all by default."""
        boxes = len(h.checkpoints(role))
        if findings is None:
            findings = "" if verdict == "APPROVED" else "- R1: the error path is not tested (src/m.py:1)"
        (self.fdir / h.REVIEWS[role].file).write_text(review_text(boxes if ticked is None else ticked, findings, boxes))

    # --- the launcher's marks
    def approve(self):
        self.git("update-ref", APPROVED, "HEAD", cwd=self.wt)

    def ref(self, rev="HEAD", role="reviewer"):
        self.git("update-ref", h.REVIEWS[role].ref.format(slug="feat"), rev, cwd=self.wt)

    # --- scenarios, as the launcher would have committed them
    def spec_review_ready(self):
        self.spec()
        self.tasks()
        self.commit("feat: spec_author", cwd=self.wt)

    def spec_reviewed(self, verdict="APPROVED", **kw):
        self.spec_review_ready()
        self.ref(role="spec_reviewer")
        self.review(verdict, role="spec_reviewer", **kw)
        self.commit("feat: spec_reviewer", cwd=self.wt)

    def spec_ready(self):
        self.spec_reviewed("APPROVED")

    def approved(self):
        self.spec_ready()
        self.approve()

    def review_ready(self, with_test=True):
        self.approved()
        self.implemented(with_test)
        self.commit("feat: implementer", cwd=self.wt)

    def reviewed(self, verdict="APPROVED", **kw):
        self.review_ready()
        self.ref()
        self.review(verdict, **kw)
        self.commit("feat: reviewer", cwd=self.wt)

    # --- reading
    def checker(self):
        return h.Checker(self.wt, "feat", "main")

    def state(self) -> str:
        return self.checker().state()

    def check(self) -> list[str]:
        return self.checker().errors()

    def cfg(self):
        return h.Config.load(self.wt)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def agent(raw: dict, role: str = "implementer"):
    """An AgentConfig parsed the way the launcher parses config.json; `permissions` defaults to full."""
    return h.AgentConfig.parse(role, {"permissions": "full", **raw}, "test")


def hold_lock(case, lock) -> subprocess.Popen:
    """A live process holding `lock` (a Lock) the way a driver or a merge does; killed when the test ends (or by the
    test, to release it)."""
    code = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.ftruncate(fd, 0)\n"
        "os.write(fd, str(os.getpid()).encode())\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code, str(lock.path)], stdout=subprocess.PIPE, text=True)
    case.addCleanup(p.stdout.close)
    case.addCleanup(p.wait)
    case.addCleanup(p.kill)
    assert p.stdout.readline().strip() == "held"
    return p


def no_herdr(*args):
    raise AssertionError(f"herdr must not be called in tests: {args}")


# A stand-in `herdr` binary for the adapter tests: answers from a JSON state file, logs every argv it gets, and
# behaves like the real CLI at the boundary the launcher relies on (JSON result on stdout, JSON error on stderr).
FAKE_HERDR = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state_path = os.environ["FAKE_HERDR_STATE"]
state = json.load(open(state_path))
agents = state.setdefault("agents", {})


def save():
    json.dump(state, open(state_path, "w"))


def ok(result):
    print(json.dumps({"id": "fake", "result": result}))
    sys.exit(0)


def err(code):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": "fake " + code}}) + "\n")
    sys.exit(1)


def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None


cmd = " ".join(args[:2])
if cmd == "agent get":
    a = agents.get(args[2])
    ok({"agent": {"agent_status": a["status"]}}) if a else err("agent_not_found")
elif cmd == "agent start":
    agents[args[2]] = {"status": state.get("start_status", "idle"), "pane": opt("--pane")}
    save()
    if agents[args[2]]["status"] == "blocked":  # like herdr: the agent exists, but a startup dialog holds it
        err("agent_not_ready")
    ok({"agent": {"pane_id": opt("--pane")}})
elif cmd == "agent prompt":
    a = agents.get(args[2])
    if not a:
        err("agent_not_found")
    if state.get("prompt_error"):
        err(state["prompt_error"])
    a["status"] = state.get("after_prompt", "idle")
    save()
    ok({"agent": {"agent_status": a["status"]}})
elif cmd == "agent wait":
    if args[2] not in agents:
        err("agent_not_found")
    if state.get("wait_error"):
        err(state["wait_error"])
    until = [args[i + 1] for i, a in enumerate(args) if a == "--until"] or ["idle", "done", "blocked"]
    if agents[args[2]]["status"] not in until:  # like herdr: blocked is settled unless --until says otherwise
        err("timeout")
    ok({"agent": {"agent_status": agents[args[2]]["status"]}})
elif cmd == "agent read":
    print(state.get("screen", ""))
elif cmd == "agent send-keys":
    a = agents.get(args[2])
    if a and "enter" in args[3:]:  # the dialog was answered
        a["status"] = "idle"
        save()
    ok({"type": "ok"})
elif cmd == "tab create":
    ok({"tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p2"}})
elif cmd == "tab list":
    ok({"tabs": state.get("tabs", [])})
elif cmd == "pane list":
    ok({"panes": state.get("panes", [])})
elif cmd == "worktree list":
    ok({"source": state.get("source", {}), "worktrees": state.get("worktrees", [])})
elif cmd == "worktree create":
    ok({"worktree": {"path": state["new_worktree"]}, "workspace": {"workspace_id": "w9"},
        "tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}})
elif cmd == "worktree open":
    ok({"workspace": {"workspace_id": "w9"}})
elif cmd == "worktree remove":
    wt = next((w["path"] for w in state.get("worktrees", []) if w.get("open_workspace_id") == opt("--workspace")), None)
    if wt:
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=state["repo"], check=False)
    ok({"type": "worktree_removed"})
elif cmd == "workspace list":
    ok({"workspaces": state.get("workspaces", [])})
elif cmd == "pane run":
    if state.get("touch_on_pane_run"):  # the driver that would start here writes its lock first thing
        with open(state["touch_on_pane_run"], "a") as fh:  # a new line per launch, like a new driver's stamp
            fh.write("started\n")
    ok({"type": "ok"})
else:
    ok({"type": "ok"})
"""


# A stand-in `hc`, the same idea for the desktop app's adapter: JSON on stdout, errors as `{"error": {"kind": ...}}`.
FAKE_HC = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state_path = os.environ["FAKE_HERDR_STATE"]
state = json.load(open(state_path))
terms = state.setdefault("terminals", [])


def save():
    json.dump(state, open(state_path, "w"))


def ok(result):
    print(json.dumps(result))
    sys.exit(0)


def err(kind, code=3):
    print(json.dumps({"error": {"kind": kind, "message": "fake " + kind}}))
    sys.exit(code)


def find(ref):
    t = next((t for t in terms if ref in (t["address"], t.get("name"))), None)
    return t or err("not_found")


def labels():
    return [args[i + 1] for i, a in enumerate(args) if a == "--label"]


def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None


cmd = args[0]
if cmd == "run":
    if any(t.get("name") == opt("--name") and t["state"] == "live" for t in terms):
        err("name_taken", 1)
    argv = args[args.index("--") + 1:] if "--" in args else []
    status = state.get("start_status", "idle")
    t = {"address": "t%d" % (len(terms) + 1), "name": opt("--name"), "state": "live", "labels": labels(), "argv": argv,
         "status": {"state": status} if argv and status != "none" else None}
    terms.append(t)
    save()
    ok(t)
elif cmd == "ls":
    ok({"terminals": [t for t in terms if all(l in t["labels"] for l in labels())]})
elif cmd == "get":
    ok(find(args[1]))
elif cmd == "kill":
    find(args[1])["state"] = "finished"
    save()
    ok({})
elif cmd == "rm":
    state["terminals"] = [t for t in terms if t["address"] != args[1]]
    save()
    ok({})
elif cmd == "read":
    find(args[-3] if args[-2] == "--lines" else args[1])
    print(state.get("screen", ""))
elif cmd == "keys":
    t = find(args[1])
    t["status"] = {"state": "idle"}
    state["screen"] = ""
    save()
    ok({})
elif cmd == "send":
    positional = [a for i, a in enumerate(args[1:], 1) if not a.startswith("--") and args[i - 1] != "--timeout"]
    t = find(positional[0])
    if (t.get("status") or {}).get("state") == "blocked":
        err("blocked", 1)
    if "--wait" not in args:
        ok({"started": None})
    ok(state.get("send_result", {"started": True, "wait": {"reason": "status", "status": "idle"}}))
elif cmd == "wait":
    t = find(args[1])
    r = state.get("wait_result", {"reason": "status", "status": "idle", "exit_code": None})
    print(json.dumps(r))
    sys.exit(r.get("exit_code") or 0)
elif cmd == "server":
    if state.get("server_down"):
        err("no_core", 1)
    ok({"version": "fake"})
else:
    ok({})
"""


class Case(unittest.TestCase):
    repo_kwargs: dict = {}
    fake_herdr = False  # True: a stand-in `herdr` on PATH instead of stubbed launcher functions

    def setUp(self):
        self.r = Repo(**self.repo_kwargs)
        self.addCleanup(self.r.close)
        if self.fake_herdr:
            self.install_fake_herdr()
        else:
            self.stub_herdr("call", no_herdr)
            self.stub_herdr("state", lambda name: "idle")
            self.stub_herdr("sidebar", lambda *a, **k: None)
            orig = h.Doctor.herdr_state
            h.Doctor.herdr_state = lambda doc: None  # no herdr server in tests
            self.addCleanup(setattr, h.Doctor, "herdr_state", orig)

    def stub(self, name, value):
        orig = getattr(h, name)
        setattr(h, name, value)
        self.addCleanup(setattr, h, name, orig)

    def stub_herdr(self, method, value):
        """Replace one method of the launcher's HERDR adapter for this test."""
        setattr(h.HERDR, method, value)
        self.addCleanup(vars(h.HERDR).pop, method, None)

    def setenv(self, name, value):
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))

    def install_fake_herdr(self):
        bindir = self.r.tmp / "bin"
        bindir.mkdir()
        exe = bindir / "herdr"
        exe.write_text(FAKE_HERDR)
        exe.chmod(0o755)
        self.state_path = self.r.tmp / "herdr-state.json"
        self.log_path = self.r.tmp / "herdr-log.jsonl"
        self.log_path.write_text("")
        self.state = {
            "repo": str(self.r.root),
            "new_worktree": str(self.r.tmp / "new-wt"),
            "worktrees": [{"path": str(self.r.wt), "branch": "feat", "open_workspace_id": "w1"}],
            "source": {"source_checkout_path": str(self.r.root), "source_workspace_id": "w0"},
        }
        self.save_state()
        self.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        self.setenv("FAKE_HERDR_STATE", str(self.state_path))
        self.setenv("FAKE_HERDR_LOG", str(self.log_path))
        self.setenv("HERDR_WORKSPACE_ID", None)
        self.stub("time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))  # no real waits

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def calls(self, *prefix) -> list:
        """Every argv the fake herdr received that starts with `prefix`."""
        lines = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [args for args in lines if args[: len(prefix)] == list(prefix)]

    def chdir(self, path):
        cwd = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, cwd)

    @staticmethod
    def output(fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(*args)
        return code, out.getvalue()


# ----------------------------------------------------------------------------- pure functions
class PureFunctions(unittest.TestCase):
    def test_section(self):
        text = "## A\n1\n## Requirements\n<!-- c -->\n- R1: SHALL x\n## B\n"
        self.assertEqual(h.Markdown.section(text, "a"), ["1"])
        self.assertEqual(h.Markdown.section(h.Markdown.without_comments(text), "requirements"), ["", "- R1: SHALL x"])

    def test_verdict_from_the_form(self):
        def verdict(text):
            return h.FeatureFiles.verdict_of(text, "reviewer")

        self.assertEqual(verdict(review_text(CHECKPOINTS, "")), "APPROVED", "nothing written")
        self.assertEqual(verdict(review_text(CHECKPOINTS, "\n  \n")), "APPROVED", "blank lines are nothing")
        self.assertEqual(verdict(review_text(CHECKPOINTS, "<!-- a comment -->")), "APPROVED")
        for finding in ("- R1: wrong", "* R1: wrong", "1. R1: wrong", "R1: wrong (src/m.py:1)", "none", "None."):
            self.assertEqual(verdict(review_text(CHECKPOINTS, finding)), "CHANGES_REQUESTED", finding)
        self.assertEqual(verdict(review_text(2, "- R1: wrong")), "CHANGES_REQUESTED", "a finding wins")
        self.assertEqual(verdict(review_text(CHECKPOINTS - 1, "")), "", "an unticked box decides nothing")
        self.assertEqual(verdict("# Review\n## Findings\n\n## Checked\n- [x] 1\n"), "", "boxes missing")
        self.assertEqual(verdict(""), "")
        decorated = review_text(CHECKPOINTS, "- R1: wrong").replace("## Findings", "## Findings (1)")
        self.assertEqual(verdict(decorated), "", "a decorated heading is not the section")
        self.assertEqual(verdict("# Review\n## Checked\n" + "- [x] 1\n" * CHECKPOINTS), "")

    def test_cites_only_the_trailing_group(self):
        self.assertEqual(h.Task("T1", "see R3 for context (R1)", False).cites, {"R1"})
        self.assertEqual(h.Task("T1", "do x (R1, R2)", False).cites, {"R1", "R2"})
        self.assertEqual(h.Task("T1", "do x", False).cites, set())

    def test_next_action_and_groups(self):
        gates = {"approve": "human", "merge": "auto"}
        self.assertEqual(h.next_action("spec", gates), "run spec_author")
        self.assertEqual(h.next_action("spec_review", gates), "run spec_reviewer")
        self.assertEqual(h.next_action("spec_ready", gates), "approve (human)")
        self.assertEqual(h.next_action("implementing", gates), "run implementer")
        self.assertEqual(h.next_action("review", gates), "run reviewer")
        self.assertEqual(h.next_action("reviewed", gates), "merge (auto)")
        self.assertTrue(h.needs_human("blocked", gates))
        self.assertTrue(h.needs_human("spec_ready", gates))
        self.assertFalse(h.needs_human("reviewed", gates), "an auto gate is the driver's move")
        self.assertTrue(h.needs_human("reviewed", {}), "a missing gate means human")
        self.assertFalse(h.needs_human("implementing", gates))

    def test_state_text(self):
        gates = {"approve": "human", "merge": "auto"}
        self.assertEqual(h.state_text("spec_ready", gates), "spec ready · needs you: approve")
        self.assertEqual(h.state_text("spec_review", gates), "spec review · spec_reviewer next")
        self.assertEqual(h.state_text("reviewed", gates), "reviewed · auto-merge")
        self.assertEqual(h.state_text("implementing", gates), "implementing · implementer next")
        self.assertEqual(h.state_text("implementing", gates, running="implementer"), "implementer · working")
        self.assertEqual(h.state_text("blocked", gates), "blocked · needs you")
        self.assertEqual(h.state_text("done", gates), "done")

    def test_effort_levels_per_cli(self):
        native = {
            "claude": ["low", "medium", "high", "xhigh", "max"],
            "codex": ["low", "medium", "high", "xhigh", "xhigh"],  # codex has nothing above xhigh
        }
        flag = {"claude": ["--effort", "{}"], "codex": ["-c", "model_reasoning_effort={}"]}
        for kind, values in native.items():
            for ours, theirs in zip(h.EFFORT_LEVELS, values):
                want = [part.format(theirs) for part in flag[kind]]
                self.assertEqual(h.CLIS[kind].effort_args(ours), want, (kind, ours))
        self.assertEqual(h.CLIS["claude"].effort_args(""), [])

    def test_permission_levels_per_cli(self):
        cases = {
            ("claude", "guarded"): ["--permission-mode", "auto"],
            ("claude", "full"): ["--dangerously-skip-permissions"],
            ("codex", "guarded"): ["--sandbox", "workspace-write", "--ask-for-approval", "never"],
            ("codex", "full"): ["--dangerously-bypass-approvals-and-sandbox"],
        }
        for (kind, level), flags in cases.items():
            self.assertEqual(agent({"harness": kind, "permissions": level}).cli_args(), ["--", *flags], (kind, level))
        self.assertNotIn(
            "--add-dir", h.CLIS["codex"].permissions["guarded"], "no write access to .git: the launcher commits"
        )

    def test_patterns_args_prompt(self):
        self.assertTrue(h.valid_pattern(".env"))
        self.assertTrue(h.valid_pattern("data/*.db"))
        for bad in ("/etc/passwd", "../x", "", ".", 3):
            self.assertFalse(h.valid_pattern(bad), bad)
        self.assertEqual(agent({"harness": "gemini"}).cli_args(), [], "nothing to pass: no `--`")
        self.assertEqual(
            agent({"harness": "gemini", "model": "m", "args": ["-x"]}).cli_args(), ["--", "--model", "m", "-x"]
        )
        self.assertEqual(
            agent({"harness": "claude", "effort": "extra"}).cli_args(),
            ["--", "--effort", "xhigh", "--dangerously-skip-permissions"],
        )
        self.assertEqual(
            agent(
                {"harness": "codex", "model": "g", "effort": "high", "permissions": "full", "args": ["-y"]}
            ).cli_args(),
            [
                "--",
                "--model",
                "g",
                "-c",
                "model_reasoning_effort=high",
                "--dangerously-bypass-approvals-and-sandbox",
                "-y",
            ],
            "model, effort, permissions, then args",
        )
        self.assertEqual(agent({"harness": "gemini", "effort": "high", "permissions": "full"}).cli_args(), [])
        self.assertEqual(
            h.RoleRun.prompt_for("reviewer", "s", " a\n b ", "main"),
            "Read harness/roles/reviewer.md and follow it for harness/features/s/ (base branch: main). a b",
        )
        self.assertEqual(
            h.RoleRun.prompt_for("spec_author", "s", "", "dev", bootstrap=True),
            "Read harness/roles/spec_author.md and follow it for harness/features/s/ (base branch: dev; no project "
            "code yet: this feature sets the project up).",
        )


# ----------------------------------------------------------------------------- config and worktree files
class ConfigTests(Case):
    def test_defaults_and_main_checkout(self):
        cfg = self.r.cfg()
        self.assertEqual(
            (cfg.gates, cfg.max_review_rounds, cfg.setup_cmd, cfg.copy_files),
            ({"approve": "human", "merge": "human"}, 3, "", (".env",)),
        )
        (self.r.wt / "harness" / "config.json").write_text(
            json.dumps({"base_branch": "main", "test_cmd": "false", "agents": AGENTS})
        )
        self.assertEqual(
            h.Config.load(self.r.wt).test_cmd, "true", "config comes from the main checkout, not the branch"
        )

    def test_rejects_bad_files(self):
        bad = [
            {"test_cmd": "true", "agents": AGENTS},
            {"base_branch": "main", "test_cmd": "true", "agents": {}},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "gates": {"approve": "maybe"}},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "copy_files": ["/x"]},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "max_review_rounds": -1},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "max_review_rounds": True},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "setup_cmd": 1},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "gates": "auto"},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "gates": ["auto"]},
            {"base_branch": "main", "test_cmd": "", "agents": AGENTS},
            {"base_branch": 5, "test_cmd": "true", "agents": AGENTS},
            {"base_branch": "main", "test_cmd": ["pytest"], "agents": AGENTS},
        ]
        for raw in bad:
            (self.r.root / "harness" / "config.json").write_text(json.dumps(raw))
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                h.Config.load(self.r.root)
        (self.r.root / "harness" / "config.json").write_text("{")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.Config.load(self.r.root)
        for key, value in (("max_role_minutes", -1), ("max_review_rounds", "3"), ("max_role_minutes", True)):
            self.r.write_cfg(**{key: value})
            with self.assertRaises(h.Die) as cm:
                h.Config.load(self.r.root)
            self.assertIn(f"{key} must be an integer >= 0", cm.exception.msg)

    def test_rejects_agents_outside_the_vocabulary(self):
        full = {"harness": "claude", "permissions": "full"}
        bad = {
            "effort 'turbo' is not ours": (
                {"reviewer": {**full, "effort": "turbo"}},
                "effort must be one of low, medium",
            ),
            "a provider word": ({"reviewer": {**full, "effort": "xhigh"}}, "here it is 'extra'"),
            "effort as a number": ({"reviewer": {**full, "effort": 3}}, "effort must be one of"),
            "unknown permissions": (
                {"reviewer": {"harness": "claude", "permissions": "yolo"}},
                "'yolo' is not one of them; a role agent runs unattended: use guarded or full",
            ),
            "the old ask level": ({"reviewer": {"harness": "claude", "permissions": "ask"}}, "use guarded or full"),
            "a level the CLI lacks": ({"reviewer": {"harness": "pi", "permissions": "guarded"}}, "it has: full"),
            "a role without permissions": ({"reviewer": {"harness": "claude"}}, "it is required"),
            "harness as a list": ({"reviewer": {**full, "harness": ["claude"]}}, "harness is required"),
            "model as a number": ({"reviewer": {**full, "model": 5}}, "model must be a string"),
            "args not strings": ({"reviewer": {**full, "args": [1]}}, "args must be a list of strings"),
        }
        for why, (agents, msg) in bad.items():
            self.r.write_cfg(agents={**AGENTS, **agents})
            with self.assertRaises(h.Die) as cm:
                h.Config.load(self.r.root)
            self.assertIn(msg, cm.exception.msg, why)
        self.r.write_cfg(agents={**AGENTS, "reviewer": {"harness": "gemini", "args": ["--yolo"]}})
        gemini = h.Config.load(self.r.root).agent("reviewer")
        self.assertEqual((gemini.permissions, gemini.cli_args()), ("", ["--", "--yolo"]), "not translated: optional")
        self.r.write_cfg(
            agents={**AGENTS, "reviewer": {"harness": "gemini", "permissions": "full", "args": ["--yolo"]}}
        )
        self.assertEqual(
            h.Config.load(self.r.root).agent("reviewer").harness, "gemini", "an untranslated CLI: flags in args"
        )

    def test_copy_ignored(self):
        root = self.r.root
        (root / ".env").write_text("S=1")
        (root / "data" / "sub").mkdir(parents=True)
        (root / "data" / "sub" / "db").write_text("db")
        os.symlink("sub/db", root / "data" / "link")
        dst = self.r.tmp / "dst"
        dst.mkdir()
        line = h.WorktreeSetup(root, dst, self.r.cfg()).copy_ignored([".env", "data", "data/*", "src/m.py", "nope"])
        self.assertIn("copied .env, data", line)
        self.assertIn("not in the main checkout: nope", line)
        self.assertIn("skipped, not gitignored: src/m.py", line)
        self.assertEqual((dst / ".env").read_text(), "S=1")
        self.assertEqual((dst / "data" / "sub" / "db").read_text(), "db")
        self.assertTrue((dst / "data" / "link").is_symlink())
        self.assertFalse((dst / "src").exists())
        self.assertIn(
            "copied .env, data",
            h.WorktreeSetup(root, dst, self.r.cfg()).copy_ignored([".env", "data"]),
            "a second run must not fail",
        )


# ----------------------------------------------------------------------------- git helpers
class GitHelpers(Case):
    def test_main_checkout_and_refs(self):
        self.assertEqual(h.Git(self.r.wt).main_checkout().resolve(), self.r.root.resolve())
        self.assertTrue(h.Git(self.r.root).is_main())
        self.assertFalse(h.Git(self.r.wt).is_main())
        self.assertFalse(h.Git(self.r.wt).ref_exists(REVIEW_START))
        self.r.ref()
        self.r.approve()
        self.assertTrue(h.Git(self.r.root).ref_exists(REVIEW_START), "refs are shared across worktrees")
        h.Git(self.r.wt).drop_refs("refs/harness/feat/")
        self.assertFalse(h.Git(self.r.wt).ref_exists(REVIEW_START))
        self.assertFalse(h.Git(self.r.wt).ref_exists(APPROVED))

    def test_features_everywhere(self):
        feats = h.Feature.all(self.r.root, "main")
        f = feats["feat"]
        self.assertEqual((f.slug, f.branch, f.state, f.live), ("feat", "feat", "spec", True))
        self.assertEqual(f.wt.resolve(), self.r.wt.resolve())
        self.assertEqual(h.Feature.find(self.r.wt, "feat", "main").dir, f.dir)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.Feature.find(self.r.root, "nope", "main")

    def test_merge_in(self):
        git = h.Git(self.r.wt)
        self.assertEqual(git.merge_in("main", "feat: merge main"), [], "nothing to merge")
        (self.r.root / "new.txt").write_text("x")
        self.r.commit("main: new")
        self.assertEqual(git.merge_in("main", "feat: merge main"), [])
        self.assertTrue((self.r.wt / "new.txt").exists())
        self.assertEqual(self.r.log()[-1], "feat: merge main")
        (self.r.root / "src" / "m.py").write_text("def f():\n    return 2\n")
        self.r.commit("main: 2")
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")
        self.r.commit("feat: 3", cwd=self.r.wt)
        self.assertEqual(git.merge_in("main", "feat: merge main"), ["src/m.py"])
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "", "the conflict is committed as it is")
        self.assertEqual(self.r.log()[-1], "feat: merge main, conflicts in src/m.py")
        self.assertIn("<<<<<<<", (self.r.wt / "src" / "m.py").read_text())
        self.assertEqual(git.conflict_markers("main"), ["src/m.py"])
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")
        self.assertEqual(git.conflict_markers("main"), [], "resolved in the working tree")
        self.assertEqual(git.merge_in("main", "feat: merge main"), [], "nothing left to merge")

    def test_a_conflict_git_cannot_mark_is_aborted(self):
        git = h.Git(self.r.wt)
        (self.r.root / "img.bin").write_bytes(b"\x00\x01main")
        self.r.git("rm", "-q", "src/m.py")
        self.r.commit("main: deletes m.py, adds a binary")
        (self.r.wt / "img.bin").write_bytes(b"\x00\x01feat")
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")
        self.r.commit("feat: edits m.py, adds a binary", cwd=self.r.wt)
        head = git.head()
        with self.assertRaises(h.Die) as cm:
            git.merge_in("main", "feat: merge main")
        self.assertIn("conflicts git cannot mark in the file: img.bin, src/m.py", cm.exception.msg)
        self.assertEqual(git.head(), head, "aborted: nothing committed")
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "", "the merge is aborted")

    def test_porcelain_and_changed_paths(self):
        (self.r.wt / "src" / "m.py").write_text("changed\n")
        (self.r.wt / "new dir").mkdir()
        (self.r.wt / "new dir" / "f.txt").write_text("")
        self.assertEqual(sorted(p for _, p in h.Git(self.r.wt).porcelain()), ["new dir/f.txt", "src/m.py"])
        self.assertEqual(
            h.Git(self.r.wt).changed_paths("main"),
            ["harness/features/feat/spec.md", "harness/features/feat/tasks.md", "new dir/f.txt", "src/m.py"],
        )
        self.r.git("mv", "harness/roles/reviewer.md", "harness/features/feat/x.md", cwd=self.r.wt)
        paths = [p for _, p in h.Git(self.r.wt).porcelain()]
        self.assertIn("harness/roles/reviewer.md", paths, "a rename shows under both names")
        self.assertIn("harness/features/feat/x.md", paths)
        self.r.git("commit", "-q", "-m", "feat: moved", cwd=self.r.wt)
        self.assertIn("harness/roles/reviewer.md", h.Git(self.r.wt).changed_paths("main"), "committed too")

    def test_review_rounds_count_changes_requested_rounds_not_commits(self):
        files = h.FeatureFiles(self.r.wt, "feat")
        self.assertEqual(files.review_rounds("reviewer", "main"), 0)
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: reviewer", cwd=self.r.wt)
        (self.r.fdir / "review.md").write_text((self.r.fdir / "review.md").read_text() + "- one more finding\n")
        self.r.commit("feat: reviewer, one more finding", cwd=self.r.wt)
        self.assertEqual(files.review_rounds("reviewer", "main"), 1, "two commits in one round are one round")
        files.clear_review("reviewer")
        self.assertEqual(files.review_rounds("reviewer", "main"), 1, "a cleared round still counts")
        self.assertEqual(self.r.log()[-1], "feat: clear review")
        self.r.review("APPROVED")
        self.r.commit("feat: reviewer", cwd=self.r.wt)
        files.clear_review("reviewer")
        self.assertEqual(files.review_rounds("reviewer", "main"), 1, "an APPROVED round does not count")
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: reviewer", cwd=self.r.wt)
        self.assertEqual(files.review_rounds("reviewer", "main"), 2, "the review on disk counts too")
        files.clear_review("reviewer")
        self.assertFalse((self.r.fdir / "review.md").exists())
        files.clear_review("reviewer")  # nothing to clear: no error, no commit
        self.assertEqual(self.r.log()[-1], "feat: clear review")
        self.r.review("CHANGES_REQUESTED", role="spec_reviewer")
        self.r.commit("feat: spec_reviewer", cwd=self.r.wt)
        self.assertEqual(files.review_rounds("spec_reviewer", "main"), 1, "each review counts its own rounds")
        self.assertEqual(files.review_rounds("reviewer", "main"), 2)
        files.clear_review("spec_reviewer")
        self.assertEqual(self.r.log()[-1], "feat: clear spec review")
        self.assertEqual(files.review_rounds("spec_reviewer", "main"), 1, "a cleared spec round still counts")

    def test_ask_and_answer(self):
        files = h.FeatureFiles(self.r.wt, "feat")
        self.assertEqual(files.questions(), [])
        files.ask("why?")
        self.assertEqual(self.r.state(), "blocked")
        self.assertEqual(len(files.questions()), 1)
        self.assertIn("launcher", files.questions()[0])
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "")
        self.assertEqual(self.r.log()[-1], "feat: blocked by the launcher")
        asked = files.answer()
        self.assertIn("why?", asked)
        self.assertEqual(files.questions(), [])
        self.assertNotEqual(self.r.state(), "blocked")
        self.assertEqual(self.r.log()[-1], "feat: questions answered")
        self.r.questions("")
        self.assertEqual(files.questions(), [], "a file with only its heading asks nothing")
        for q in ("1. Is 1 right?", "Is 1 right?", "* Is 1 right?", "none"):
            self.r.questions(q)
            self.assertEqual(files.questions(), [q], "what is written asks, however it is marked")

    def test_driver_lock(self):
        lock = h.Lock.driver(self.r.wt)
        self.assertEqual(lock.running(), 0)
        lock.path.write_text("999999999")
        self.assertEqual(lock.running(), 0, "a file left by a dead holder is no lock")
        holder = hold_lock(self, lock)
        self.assertEqual(h.Lock.driver(self.r.wt).running(), holder.pid)
        self.assertEqual(h.Lock.merge(self.r.root).running(), 0, "a driver's lock is not a merge")
        self.assertEqual(h.Lock.merge(self.r.root).path.name, "harness-merge.pid")

    def test_acquire_driver_lock(self):
        lock = h.Lock.driver(self.r.wt)
        self.assertEqual(lock.acquire(), 0)
        self.assertEqual(lock.path.read_text(), str(os.getpid()))
        self.assertEqual(h.Lock.driver(self.r.wt).running(), os.getpid())
        self.assertEqual(h.Lock.driver(self.r.wt).acquire(), os.getpid(), "a live holder keeps its lock")
        lock.release()
        self.assertEqual(h.Lock.driver(self.r.wt).running(), 0, "released: no driver, whatever the file says")
        holder = hold_lock(self, lock)
        self.assertEqual(h.Lock.driver(self.r.wt).acquire(), holder.pid, "a live driver keeps its lock")

    def test_a_second_driver_refuses_to_run(self):
        hold_lock(self, h.Lock.driver(self.r.wt))
        f = h.Feature.find(self.r.root, "feat", "main")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = h.Driver(self.r.root, self.r.cfg(), f, "", "").run()
        self.assertEqual(code, h.EXIT_ERROR)
        self.assertIn("is already running feat", err.getvalue())
        self.assertFalse(h.Lock.driver(self.r.wt).stamp.exists(), "it stops before announcing a start")


# ----------------------------------------------------------------------------- state and check rules
class CheckRules(Case):
    def test_a_new_feature_is_at_spec(self):
        self.assertEqual(self.r.state(), "spec")
        self.assertEqual(
            self.r.check(),
            [
                "feat: ## Design is empty",
                "feat: no active requirement line '- R<n>: ... SHALL ...' under ## Requirements",
                "feat: no task line '- [ ] T<n>: ... (R<n>)' in tasks.md",
            ],
            "the template's examples are comments: an unwritten feature has nothing, and nothing is the error",
        )
        text = (self.r.fdir / "spec.md").read_text() + (self.r.fdir / "tasks.md").read_text()
        self.assertNotIn("status:", text, "state is never written")

    def test_rule_2_sections(self):
        order = "feat: spec.md needs ## Intent, ## Requirements, ## Design, ## Verification, in that order"
        self.r.spec()
        self.r.tasks()
        self.assertEqual(self.r.check(), [], "the fixture has the four sections")
        text = (self.r.fdir / "spec.md").read_text()
        (self.r.fdir / "spec.md").write_text(text.replace("## Intent\ndo x\n", "## Intent\n<!-- later -->\n"))
        self.assertEqual(self.r.check(), ["feat: ## Intent is empty"])
        (self.r.fdir / "spec.md").write_text(text.replace("## Design\nsrc/m.py\n", ""))
        self.assertEqual([e[: len(order)] for e in self.r.check()], [order], "missing: the order error alone")
        swapped = text.replace("## Intent\ndo x\n", "## Design\nsrc/m.py\n", 1).replace(
            "## Design\nsrc/m.py\n\n## Verification", "## Intent\ndo x\n\n## Verification"
        )
        (self.r.fdir / "spec.md").write_text(swapped)
        self.assertEqual([e[: len(order)] for e in self.r.check()], [order], "out of order")
        (self.r.fdir / "spec.md").write_text(text.replace("## Design\n", "## Notes\nnone\n\n## Design\n"))
        self.assertEqual(self.r.check(), [], "an extra section is tolerated")

    def test_checkpoints_come_from_the_rules(self):
        self.assertTrue(CHECKPOINTS and SPEC_CHECKPOINTS)
        ff = h.FeatureFiles(self.r.wt, "feat")
        for role in h.REVIEWS:
            for i, text in enumerate(h.checkpoints(role), 1):
                self.assertTrue(text.startswith(f"{i}. "), text)
            ff.seed_review(role)
            text = ff.form(role).read_text()
            self.assertEqual(h.FeatureFiles.review_form(text), (0, len(h.checkpoints(role)), []))
            self.assertEqual(h.FeatureFiles.verdict_of(text.replace("- [ ]", "- [x]"), role), "APPROVED")
        self.assertEqual(ff.form("spec_reviewer").name, "spec_review.md")

    def test_rule_2(self):
        reqs = "- R1: WHEN x, THE SYSTEM SHALL y.\n- R1: WHEN x, THE SYSTEM SHALL z.\n- R2: returns 1."
        self.r.spec(reqs=reqs)
        self.r.tasks("- [ ] T1: test (R1, R2)")
        errs = self.r.check()
        self.assertIn("feat: duplicate requirement id R1", errs)
        self.assertIn("feat: R2 has no SHALL", errs)
        self.r.spec(reqs="- R1: withdrawn")
        self.assertIn("feat: no active requirement line '- R<n>: ... SHALL ...' under ## Requirements", self.r.check())

    def test_rule_2_ears(self):
        good = [
            "THE SYSTEM SHALL log every request.",
            "WHEN a user logs in, THE SYSTEM SHALL set a cookie.",
            "WHILE offline, THE SYSTEM SHALL queue writes.",
            "WHERE dark mode is enabled, THE SYSTEM SHALL use the dark palette.",
            "IF the token is expired, THEN THE SYSTEM SHALL return 401.",
            "WHILE offline, WHEN a write arrives, THE SYSTEM SHALL queue it.",
            "WHEN a, b, or c happens, THE API SHALL return 200 within 2 s.",
        ]
        bad = {
            "WHEN x, THE SYSTEM SHALL y and SHALL z.": "has 2 SHALLs; one per requirement",
            "IF x, THE SYSTEM SHALL y.": "follows no EARS pattern",
            "SHALL return 1.": "follows no EARS pattern",
            "The system SHALL return 1.": "follows no EARS pattern",
            "The system shall return 1.": "has no SHALL",
        }
        lines = [f"- R{i}: {t}" for i, t in enumerate([*good, *bad], 1)]
        self.r.spec(reqs="\n".join(lines))
        self.r.tasks(f"- [ ] T1: all ({', '.join(f'R{i}' for i in range(1, len(lines) + 1))})")
        errs = self.r.check()
        for i, (text, why) in enumerate(bad.items(), len(good) + 1):
            self.assertTrue(any(e.startswith(f"feat: R{i} {why}") for e in errs), (text, errs))
        self.assertEqual(len(errs), len(bad), errs)

    def test_fixture_requirements_are_ears(self):
        for text in (R1, "- R2: THE SYSTEM SHALL z."):
            self.assertTrue(h.EARS_RE.match(h.REQ_RE.match(text)[2]), text)

    def test_rule_3(self):
        self.r.spec(reqs=R1 + "\n- R2: THE SYSTEM SHALL z.")
        self.r.tasks("- [ ] T1: a (R1, R9)\n- [ ] T2: b")
        errs = self.r.check()
        self.assertIn("feat: T1 cites unknown R9", errs)
        self.assertIn("feat: T2 cites no requirement", errs)
        self.assertIn("feat: R2 is not covered by any task", errs)
        (self.r.fdir / "tasks.md").unlink()
        self.assertIn("feat: missing tasks.md", self.r.check())

    def test_rule_4_reads_the_verification_table(self):
        self.r.spec(rows="| R1 | (feat:R1) |")
        self.r.tasks()
        self.assertEqual(self.r.check(), ["feat: R1 has no test file in ## Verification"], "a row without a file")
        self.r.spec(rows="")
        self.assertEqual(self.r.check(), ["feat: R1 has no test file in ## Verification"], "no row")
        self.r.spec(rows="| R1 | `tests/test_a.py::test_x`, `tests/test_feat.py` (feat:R1) |")
        rows = h.FeatureFiles(self.r.wt, "feat").verification()
        self.assertEqual(rows, ({"R1": ["tests/test_a.py", "tests/test_feat.py"]}, []))
        self.assertEqual(self.r.check(), [], "the files need not exist before the implementer runs")
        self.assertEqual(self.r.state(), "spec_review")
        for path in ("tests/", "tests/*.py", "harness/features/feat/spec.md", "harness/x.py"):
            self.r.spec(rows=f"| R1 | `{path}` |")
            self.assertEqual(
                self.r.check(),
                [f"feat: R1 in ## Verification must name a test file, a relative path outside harness/, not {path}"],
                path,
            )

    def test_states_follow_the_evidence(self):
        self.r.spec()
        self.r.tasks()
        self.assertEqual(self.r.state(), "spec_review")
        self.r.questions()
        self.assertEqual(self.r.state(), "blocked", "a question for the human comes first")
        self.r.questions("")
        self.r.commit("feat: spec_author", cwd=self.r.wt)
        self.r.review("APPROVED", role="spec_reviewer")
        self.assertEqual(self.r.state(), "spec_review", "a spec review the launcher never started says nothing")
        self.r.ref(role="spec_reviewer")
        self.assertEqual(self.r.state(), "spec_ready")
        self.r.commit("feat: spec_reviewer", cwd=self.r.wt)
        self.r.approve()
        self.assertEqual(self.r.state(), "implementing")
        self.assertIn("feat: T1 is not ticked", self.r.check())
        self.r.implemented()
        self.assertEqual(self.r.state(), "review")
        self.assertEqual(self.r.check(), [])
        self.r.commit("feat: implementer", cwd=self.r.wt)
        self.r.review("APPROVED")
        self.assertEqual(self.r.state(), "review", "a review the launcher never started says nothing")
        self.r.ref()
        self.assertEqual(self.r.state(), "reviewed")
        self.r.commit("feat: reviewer", cwd=self.r.wt)
        self.assertEqual(self.r.state(), "reviewed", "committed: still of HEAD")
        self.r.review("CHANGES_REQUESTED")
        self.assertEqual(self.r.state(), "implementing")
        self.r.commit("feat: reviewer", cwd=self.r.wt)
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 1  # fixed\n")
        self.r.commit("feat: implementer", cwd=self.r.wt)
        self.assertEqual(self.r.state(), "review", "code moved on: the old review is not of HEAD")
        self.assertEqual(self.r.check(), [], "a stale review is not an error")
        self.assertEqual(h.Checker(self.r.root, "feat", "main").state(), "done", "on the base checkout")

    def test_a_worktree_off_its_branch_is_an_error(self):
        self.r.spec_ready()
        self.r.git("checkout", "-q", "--detach", cwd=self.r.wt)
        with self.assertRaises(h.Die) as cm:
            self.r.state()
        self.assertIn("is on 'detached', not on the branch 'feat'", cm.exception.msg)
        self.r.git("checkout", "-q", "-b", "other", cwd=self.r.wt)
        with self.assertRaises(h.Die) as cm:
            h.Feature.all(self.r.root, "main")
        self.assertIn("is on 'other'", cm.exception.msg)

    def test_the_checkpoints_must_exist(self):
        rules = self.r.tmp / "sdd.md"
        rules.write_text(
            "# Rules\n\n## 5. Spec reviewer checkpoints\n\nprose only\n\n## 6. Reviewer checkpoints\n\n1. x\n"
        )
        self.stub("RULES", rules)
        h.checkpoints.cache_clear()
        self.addCleanup(h.checkpoints.cache_clear)
        self.assertEqual(h.checkpoints("reviewer"), ("1. x",))
        with self.assertRaises(h.Die) as cm:
            h.checkpoints("spec_reviewer")
        self.assertIn("no numbered line under '## 5. Spec reviewer checkpoints': spec_review.md", cm.exception.msg)

    def test_rule_3_task_ids_unique(self):
        self.r.spec()
        self.r.tasks("- [ ] T1: a (R1)\n- [ ] T1: b (R1)")
        self.assertIn("feat: duplicate task id T1", self.r.check())

    def test_rule_4_one_row_per_requirement_a_relative_path(self):
        self.r.spec(rows="| R1 | `tests/test_feat.py` |\n| R1 | `tests/other.py` |")
        self.r.tasks()
        self.assertIn("feat: R1 has more than one row in ## Verification", self.r.check())
        for bad in ("/etc/hosts", "../x.py", "tests/"):
            self.r.spec(rows=f"| R1 | `{bad}` |")
            self.assertIn(
                f"feat: R1 in ## Verification must name a test file, a relative path outside harness/, not {bad}",
                self.r.check(),
            )

    def test_rule_5_complete(self):
        self.r.approved()
        self.r.tasks("- [ ] T1: test f (R1)")
        errs = self.r.check()
        self.assertIn(
            "feat: no test file named for R1 in ## Verification exists (tests/test_feat.py)", errs, "not written yet"
        )
        self.assertIn("feat: T1 is not ticked", errs)
        self.assertIn("feat: missing log.md", errs)
        self.r.implemented(with_test=False)
        (self.r.wt / "tests" / "test_feat.py").write_text("def test_f():\n    assert True\n")
        self.assertEqual(self.r.check(), [NO_TOKEN])
        self.r.implemented()
        self.assertEqual(self.r.check(), [])
        (self.r.wt / "src" / "m.py").write_text("<<<<<<< HEAD\ndef f():\n    return 1\n=======\n>>>>>>> main\n")
        self.assertEqual(self.r.check(), ["feat: conflict markers left in src/m.py"])
        self.assertEqual(self.r.state(), "implementing")

    def test_rule_5_matches_the_whole_token(self):
        self.r.approved()
        self.r.implemented()
        test = self.r.wt / "tests" / "test_feat.py"
        test.write_text("# feat:R10\n# xfeat:R1\n# my-feat:R1\n# feat:R1x\ndef test_f():\n    assert True\n")
        self.assertIn(NO_TOKEN, self.r.check(), "longer tokens do not cover R1")
        for line in ("# covers feat:R1", '"""(feat:R1)"""', "feat:R1"):
            test.write_text(line + "\ndef test_f():\n    assert True\n")
            self.assertNotIn(NO_TOKEN, self.r.check(), line)
        self.r.spec(rows="| R1 | `tests/test_other.py` |")
        (self.r.wt / "tests" / "test_other.py").write_text("x\n")
        elsewhere = NO_TOKEN.replace("tests/test_feat.py", "tests/test_other.py")
        self.assertIn(elsewhere, self.r.check(), "the token in another file is not enough")

    def test_rule_6_review_form(self):
        self.r.review_ready()
        self.r.ref()
        self.r.review("APPROVED")
        self.assertEqual((self.r.state(), self.r.check()), ("reviewed", []))
        self.r.review("APPROVED", ticked=CHECKPOINTS - 1)
        self.assertEqual(self.r.state(), "review")
        self.assertEqual(
            self.r.check(),
            [
                f"feat: review.md ticks {CHECKPOINTS - 1} of the {CHECKPOINTS} checkpoints and lists no finding: "
                "tick them all (APPROVED) or write what is wrong under ## Findings (CHANGES_REQUESTED)"
            ],
        )
        self.r.review("CHANGES_REQUESTED", ticked=3)
        self.assertEqual((self.r.state(), self.r.check()), ("implementing", []), "a finding is a verdict by itself")
        (self.r.fdir / "review.md").write_text("# Review\n\n## Findings\n\n## Checked\n- [x] 1\n")
        self.assertEqual(self.r.state(), "review", "fewer boxes than checkpoints decide nothing")
        self.assertEqual(
            self.r.check(), [f"feat: review.md lists 1 checkpoints, the form has {CHECKPOINTS}: keep the seeded boxes"]
        )
        (self.r.fdir / "review.md").write_text("# Review\n\n## Findings (2)\n- R1: wrong\n\n## Checked\n- [x] 1\n")
        self.assertEqual(self.r.state(), "review", "a decorated heading is not the section: no verdict")
        self.assertEqual(self.r.check(), ["feat: review.md needs a `## Findings` and a `## Checked` section (sdd §2)"])

    def test_states_follow_the_evidence_for_the_spec_review(self):
        self.r.spec_review_ready()
        self.assertEqual((self.r.state(), self.r.check()), ("spec_review", []))
        self.r.review("APPROVED", role="spec_reviewer")
        self.assertEqual(self.r.state(), "spec_review", "a spec review the launcher never started says nothing")
        self.r.ref(role="spec_reviewer")
        self.assertEqual(self.r.state(), "spec_ready")
        self.r.commit("feat: spec_reviewer", cwd=self.r.wt)
        self.assertEqual(self.r.state(), "spec_ready", "committed: still of HEAD")
        self.r.review("APPROVED", ticked=SPEC_CHECKPOINTS - 1, role="spec_reviewer")
        self.assertEqual(self.r.state(), "spec_review", "an unticked box decides nothing")
        self.assertEqual(
            self.r.check(),
            [
                f"feat: spec_review.md ticks {SPEC_CHECKPOINTS - 1} of the {SPEC_CHECKPOINTS} checkpoints and lists "
                "no finding: tick them all (APPROVED) or write what is wrong under ## Findings (CHANGES_REQUESTED)"
            ],
        )
        self.r.review("CHANGES_REQUESTED", role="spec_reviewer")
        self.assertEqual((self.r.state(), self.r.check()), ("spec", []), "a finding sends the spec back")
        self.r.commit("feat: spec_reviewer", cwd=self.r.wt)
        ff = h.FeatureFiles(self.r.wt, "feat")
        ff.ask("x")
        ff.answer()
        self.assertEqual(self.r.state(), "spec", "the launcher's questions do not age the spec review")
        self.r.spec(reqs="- R1: WHEN f is called, THE SYSTEM SHALL return 1 at once.")
        self.r.commit("feat: spec_author", cwd=self.r.wt)
        self.assertEqual(self.r.state(), "spec_review", "the spec moved on: the old review is not of HEAD")
        self.assertEqual(self.r.check(), [], "a stale spec review is not an error")
        self.r.tasks("- [ ] T1: test f (R9)")
        self.assertEqual(self.r.state(), "spec", "rules 2-4 come before the spec review")
        self.assertIn("feat: T1 cites unknown R9", self.r.check())

    def test_the_implementers_evidence_outranks_the_verdict(self):
        self.r.reviewed("APPROVED")
        self.assertEqual(self.r.state(), "reviewed")
        (self.r.wt / "tests" / "test_feat.py").unlink()
        self.assertEqual(self.r.state(), "implementing", "a missing test file: rule 5 before the verdict")
        self.r.git("checkout", "--", "tests/test_feat.py", cwd=self.r.wt)
        self.r.tasks("- [x] T1: test f (R9)")
        self.assertEqual(self.r.state(), "implementing", "a broken tasks.md: rule 3 before the verdict")
        self.assertIn("feat: T1 cites unknown R9", self.r.check())

    def test_the_launchers_questions_do_not_age_a_review(self):
        self.r.reviewed("CHANGES_REQUESTED")
        ff = h.FeatureFiles(self.r.wt, "feat")
        ff.ask("x")
        self.assertEqual(self.r.state(), "blocked")
        ff.answer()
        self.assertEqual(self.r.state(), "implementing", "the review of HEAD still says CHANGES_REQUESTED")

    def test_a_review_belongs_to_head(self):
        self.r.reviewed("APPROVED")
        self.assertEqual(self.r.state(), "reviewed")
        (self.r.root / ".gitignore").write_text(".env\ndata/\nmore\n")
        self.r.commit("main: more")
        self.assertEqual(h.Git(self.r.wt).merge_in("main", "feat: merge main"), [])
        self.assertEqual(self.r.state(), "reviewed", "the base branch merged in does not age the review")
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 1  # later\n")
        self.r.commit("feat: implementer", cwd=self.r.wt)
        self.assertEqual(self.r.state(), "review", "a code commit after the review does")
        self.assertEqual(self.r.check(), [])

    def test_main_checkout_is_done(self):
        self.r.review_ready()
        main_feat = self.r.root / "harness" / "features" / "feat"
        shutil.copytree(self.r.fdir, main_feat)
        shutil.copy(self.r.wt / "tests" / "test_feat.py", self.r.root / "tests" / "test_feat.py")
        self.r.commit("feat: merge")
        (self.r.root / "harness" / "harness.py").write_text("# local edit\n")
        self.assertEqual(h.Checker(self.r.root, "feat", "main").state(), "done")
        self.assertEqual(h.Checker(self.r.root, "feat", "main").errors(), [], "nothing to check on the main checkout")
        self.assertEqual(h.Checker(self.r.root, "nope", "main").errors(), ["nope: missing spec.md"])


# ----------------------------------------------------------------------------- a role run: commit and outcome
class RoleRuns(Case):
    def run_of(self, role):
        return h.RoleRun(self.r.cfg(), self.r.root, self.r.wt, role, "feat")

    def test_finish_commits_the_write_set_and_discards_the_rest(self):
        self.r.approved()
        run = self.run_of("implementer")
        run.start()
        self.r.implemented()
        self.r.questions("")
        (self.r.wt / "src" / "new.py").write_text("x = 1\n")
        (self.r.wt / "harness" / "harness.py").write_text("# hacked\n")
        (self.r.wt / "AGENTS.md").write_text("steer the next agent\n")
        (self.r.wt / "GEMINI.md").write_text("steer the next agent\n")
        (self.r.wt / "harness" / "docs" / "note.md").write_text("new\n")
        (self.r.wt / "src" / "CLAUDE.md").write_text("steer the next agent, nested\n")
        (self.r.wt / "src" / ".claude").mkdir()
        (self.r.wt / "src" / ".claude" / "settings.json").write_text("{}\n")
        (self.r.wt / "src" / "harness").mkdir()
        (self.r.wt / "src" / "harness" / "x.py").write_text("# the project's own 'harness' folder\n")
        self.r.spec(reqs="- R1: WHEN f is called, THE SYSTEM SHALL return 2.")
        (self.r.fdir / "review.md").write_text(review_text())
        discarded = run.finish()
        self.assertEqual(
            discarded,
            [
                "AGENTS.md",
                "GEMINI.md",
                "harness/docs/note.md",
                "harness/features/feat/review.md",
                "harness/features/feat/spec.md",
                "harness/harness.py",
                "src/.claude/settings.json",
                "src/CLAUDE.md",
            ],
        )
        self.assertEqual(self.r.log()[-1], "feat: implementer")
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "", "nothing left behind")
        committed = self.r.git("show", "--format=", "--name-only", "HEAD", cwd=self.r.wt).splitlines()
        self.assertEqual(
            sorted(committed),
            [
                "harness/features/feat/log.md",
                "harness/features/feat/questions.md",
                "harness/features/feat/tasks.md",
                "src/harness/x.py",
                "src/new.py",
                "tests/test_feat.py",
            ],
        )
        self.assertNotIn("return 2", (self.r.fdir / "spec.md").read_text(), "spec.md is frozen: restored")
        self.assertFalse((self.r.fdir / "review.md").exists(), "a verdict the implementer wrote is gone")
        self.assertFalse((self.r.wt / "harness" / "docs" / "note.md").exists())
        self.assertNotIn("hacked", (self.r.wt / "harness" / "harness.py").read_text())
        self.assertEqual(self.r.state(), "review")

    def test_finish_folds_the_agents_own_commits(self):
        self.r.approved()
        run = self.run_of("implementer")
        run.start()
        self.r.implemented()
        self.r.commit("feat: T1 by the agent", cwd=self.r.wt)
        (self.r.wt / "harness" / "harness.py").write_text("# hacked\n")
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 1  # polished\n")
        self.r.commit("feat: T2 by the agent, with a harness change", cwd=self.r.wt)
        self.assertEqual(run.finish(), ["harness/harness.py"])
        self.assertEqual(
            self.r.log(), ["feat: new feature", "feat: spec_author", "feat: spec_reviewer", "feat: implementer"]
        )
        self.assertNotIn("hacked", (self.r.wt / "harness" / "harness.py").read_text())
        self.assertIn("polished", (self.r.wt / "src" / "m.py").read_text())
        self.assertEqual(self.r.state(), "review")

    def test_finish_reviewer_keeps_only_review_md(self):
        self.r.review_ready()
        self.r.ref()
        run = self.run_of("reviewer")
        run.start()
        self.r.review("APPROVED")
        (self.r.wt / "src" / "m.py").write_text("# fixed by the reviewer\n")
        (self.r.wt / "notes.txt").write_text("n")
        self.r.git("mv", "harness/roles/reviewer.md", "harness/features/feat/x.md", cwd=self.r.wt)
        (self.r.wt / ".env").write_text("SECRET=1")  # gitignored: not the launcher's business
        discarded = run.finish()
        self.assertEqual(
            discarded,
            ["harness/features/feat/x.md", "harness/roles/reviewer.md", "notes.txt", "src/m.py"],
        )
        self.assertEqual(self.r.log()[-1], "feat: reviewer")
        self.assertEqual(
            self.r.git("show", "--format=", "--name-only", "HEAD", cwd=self.r.wt), "harness/features/feat/review.md"
        )
        self.assertEqual((self.r.wt / "src" / "m.py").read_text(), "def f():\n    return 1\n")
        self.assertTrue((self.r.wt / "harness" / "roles" / "reviewer.md").exists(), "the rename is undone")
        self.assertFalse((self.r.fdir / "x.md").exists())
        self.assertFalse((self.r.wt / "notes.txt").exists())
        self.assertTrue((self.r.wt / ".env").exists(), "ignored files stay")
        self.assertEqual(self.r.state(), "reviewed")

    def test_finish_with_nothing_to_commit(self):
        run = self.run_of("spec_author")
        run.start()
        self.assertEqual(run.finish(), [])
        self.assertEqual(self.r.log(), ["feat: new feature"])

    def test_outcomes(self):
        def outcome(role, note="", discarded=()):
            return self.run_of(role).outcome(note, list(discarded))

        self.assertEqual(outcome("spec_author", "agent_blocked").result, "NEEDS_ATTENTION")
        self.stub_herdr("state", lambda name: "blocked")
        self.assertEqual(outcome("spec_author").result, "NEEDS_ATTENTION")
        self.stub_herdr("state", lambda name: "idle")
        out = outcome("spec_author")
        self.assertEqual((out.result, out.note), ("NEEDS_ATTENTION", "check fails"))
        self.assertIn("feat: no active requirement line '- R<n>: ... SHALL ...' under ## Requirements", out.errors)
        self.r.spec()
        self.r.tasks()
        self.r.commit("feat: spec_author", cwd=self.r.wt)
        self.stub_herdr("state", lambda name: "blocked")
        self.assertEqual(outcome("spec_author").result, "DONE", "the files moved on: the agent's state says nothing")
        self.stub_herdr("state", lambda name: "idle")
        out = outcome("spec_author", discarded=["src/x.py"])
        self.assertEqual(
            (out.result, out.errors), ("DONE", ["feat: discarded, outside the spec_author's files: src/x.py"])
        )
        self.r.questions()
        self.assertEqual(outcome("spec_author").result, "DONE", "a question is a way to finish")
        self.r.questions("")
        out = outcome("spec_reviewer")
        self.assertEqual(
            (out.result, out.errors, out.note),
            ("NEEDS_ATTENTION", [], "left the feature at 'spec_review' and changed nothing that counts"),
        )
        self.r.ref(role="spec_reviewer")
        self.r.review("APPROVED", ticked=2, role="spec_reviewer")
        out = outcome("spec_reviewer")
        self.assertEqual(out.result, "NEEDS_ATTENTION")
        self.assertIn("spec_review.md ticks 2 of", out.errors[0])
        self.r.review("CHANGES_REQUESTED", role="spec_reviewer")
        self.assertEqual(outcome("spec_reviewer").result, "DONE")
        self.r.review("APPROVED", role="spec_reviewer")
        self.assertEqual(outcome("spec_reviewer").result, "DONE")
        self.r.commit("feat: spec_reviewer", cwd=self.r.wt)
        self.r.approve()
        out = outcome("implementer")
        self.assertEqual(out.result, "NEEDS_ATTENTION")
        self.assertIn("feat: T1 is not ticked", out.errors)
        self.r.implemented()
        self.r.commit("feat: implementer", cwd=self.r.wt)
        self.assertEqual(outcome("implementer").result, "DONE")
        out = outcome("reviewer")
        self.assertEqual(
            (out.result, out.errors, out.note),
            ("NEEDS_ATTENTION", [], "left the feature at 'review' and changed nothing that counts"),
        )
        self.r.ref()
        self.r.review("APPROVED", ticked=2)
        out = outcome("reviewer")
        self.assertEqual(out.result, "NEEDS_ATTENTION")
        self.assertIn("ticks 2 of", out.errors[0])
        self.r.review("CHANGES_REQUESTED")
        self.assertEqual(outcome("reviewer").result, "DONE")
        self.r.review("APPROVED")
        self.assertEqual(outcome("reviewer").result, "DONE")

    def test_outcome_review_cap(self):
        self.r.write_cfg(max_review_rounds=1)
        self.r.reviewed("CHANGES_REQUESTED")
        out = self.run_of("reviewer").outcome("", [])
        self.assertEqual((out.result, out.note), ("NEEDS_ATTENTION", "blocked after 1 review rounds"))
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn("max_review_rounds", (self.r.fdir / "questions.md").read_text())
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "")

    def test_outcome_spec_review_cap(self):
        self.r.write_cfg(max_review_rounds=1)
        self.r.spec_reviewed("CHANGES_REQUESTED")
        out = self.run_of("spec_reviewer").outcome("", [])
        self.assertEqual((out.result, out.note), ("NEEDS_ATTENTION", "blocked after 1 spec review rounds"))
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn("the findings are in spec_review.md", (self.r.fdir / "questions.md").read_text())

    def test_what_an_agent_writes_about_state_counts_for_nothing(self):
        run = self.run_of("spec_author")
        run.start()
        self.r.spec()
        self.r.tasks()
        text = (self.r.fdir / "spec.md").read_text()
        (self.r.fdir / "spec.md").write_text("---\nstatus: approved\n---\n" + text)
        self.r.review("APPROVED", role="spec_reviewer")
        self.r.ref(role="spec_reviewer")
        run.finish()
        self.assertEqual(self.r.state(), "spec_review", "the spec review the author wrote is discarded")
        self.r.ref(role="spec_reviewer")
        self.r.review("APPROVED", role="spec_reviewer")
        self.r.commit("feat: spec_reviewer", cwd=self.r.wt)
        self.assertEqual(self.r.state(), "spec_ready", "no approve ref: not approved")
        self.r.approve()
        run = self.run_of("implementer")
        run.start()
        self.r.implemented()
        self.r.review("APPROVED")
        self.r.ref()
        run.finish()
        self.assertEqual(self.r.state(), "review", "the verdict the implementer wrote is discarded")
        self.assertFalse((self.r.fdir / "review.md").exists())


# ----------------------------------------------------------------------------- the driver, with stubbed agents
class DriverTests(Case):
    def setUp(self):
        super().setUp()
        self.calls: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self.actions: dict[str, list] = {r: [] for r in h.ROLES}
        self.stub_herdr("run_role", self.fake_prompt)
        self.stub_herdr("wait", lambda name, ms: (True, ""))
        self.stub_herdr("notify", lambda pane, text: self.notes.append(text))
        self.closed: list[str] = []
        self.stub_herdr("close_role_tab", lambda wt, role: self.closed.append(role))
        self.merged = []
        self.stub("Driver", type("Driver", (h.Driver,), {"spawn_merge": lambda driver: self.fake_merge(driver)}))

    def fake_prompt(self, root, wt, role, slug, text, timeout_ms, acfg):
        self.calls.append((role, text))
        self.actions[role].pop(0)()
        return True, ""

    def fake_merge(self, driver):
        self.merged.append(driver.slug)
        driver.lock.release()
        raise h._Stop(h.EXIT_DONE)

    # role behaviours: they edit files and stop, as agents do; the launcher commits
    def spec_author(self, reqs=R1, tasks="- [ ] T1: test f (R1)"):
        self.r.spec(reqs=reqs)
        self.r.tasks(tasks)

    def implementer(self, with_test=True):
        self.r.implemented(with_test)

    def spec_reviewer(self, verdict="APPROVED", ticked=None):
        self.r.review(verdict, ticked=ticked, role="spec_reviewer")

    def reviewer(self, verdict="APPROVED", ticked=CHECKPOINTS):
        self.r.review(verdict, ticked=ticked)

    def fix(self):
        """An implementer addressing findings: some code changes (a run that changes nothing is not a fix)."""
        m = self.r.wt / "src" / "m.py"
        m.write_text(m.read_text() + "# fixed\n")

    def roles(self):
        return [role for role, _ in self.calls]

    def drive(self, msg="", notify="pane"):
        f = h.Feature.find(self.r.root, "feat", "main")
        return self.output(h.Driver(self.r.root, self.r.cfg(), f, msg, notify).run)

    def auto(self, rounds=3):
        self.r.write_cfg(gates={"approve": "auto", "merge": "auto"}, max_review_rounds=rounds)

    def test_auto_gates_run_to_the_merge(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "reviewer"])
        self.assertEqual(self.merged, ["feat"])
        self.assertEqual(
            self.r.log(),
            ["feat: new feature", "feat: spec_author", "feat: spec_reviewer", "feat: implementer", "feat: reviewer"],
        )
        self.assertTrue(h.Git(self.r.wt).ref_exists(APPROVED))
        self.assertEqual(self.r.state(), "reviewed")
        self.assertIn("run reviewer", out)
        self.assertEqual(h.Lock.driver(self.r.wt).running(), 0)

    def test_human_gate_stops_and_notifies(self):
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("harness: feat spec ready, needs you", self.notes[0])
        self.assertEqual(self.r.state(), "spec_ready")
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "", "the spec is committed")
        self.assertEqual(h.Lock.driver(self.r.wt).running(), 0)
        self.assertFalse(self.merged)

    def test_feedback_at_an_auto_gate_goes_to_the_judged_role_first(self):
        self.auto()
        self.r.spec_ready()
        reworded = "- R1: WHEN f is called, THE SYSTEM SHALL return 1 at once."
        self.actions["spec_author"] = [lambda: self.spec_author(reqs=reworded)]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive(msg="say at once")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "reviewer"], "reviewed again")
        self.assertIn("say at once", self.calls[0][1])
        self.assertEqual(self.merged, ["feat"])

    def test_feedback_at_an_auto_merge_gate_goes_to_the_implementer_first(self):
        self.auto()
        self.r.reviewed("APPROVED")
        self.actions["implementer"] = [self.fix]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive(msg="rename f to g")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["implementer", "reviewer"])
        self.assertIn("rename f to g", self.calls[0][1])
        self.assertEqual(self.merged, ["feat"])

    def test_a_run_that_changes_nothing_stops_for_the_human(self):
        self.r.reviewed("APPROVED")
        self.actions["implementer"] = [lambda: None]
        code, _ = self.drive(msg="rename f to g")
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(self.roles(), ["implementer"])
        self.assertIn(
            "implementer stopped (left the feature at 'reviewed' and changed nothing that counts)", self.notes[-1]
        )
        self.assertEqual(self.closed, [], "its tab stays, for the human to read why")
        self.assertEqual(self.r.state(), "reviewed")

    def test_config_is_reloaded_each_step(self):
        self.actions["spec_author"] = [lambda: (self.spec_author(), self.auto())]  # the human edits config.json
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.merged, ["feat"], "the gates set to auto mid-drive apply")

    def test_leftovers_are_committed_under_their_role(self):
        self.auto()
        self.r.approved()
        self.r.implemented()  # uncommitted: a run the launcher did not finish (max_role_minutes, a dead driver)
        self.actions["reviewer"] = [self.reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertIn("committing what an unfinished implementer run left", out)
        self.assertEqual(self.roles(), ["reviewer"])
        self.assertIn("feat: implementer", self.r.log())
        self.assertTrue((self.r.wt / "tests" / "test_feat.py").exists(), "nothing discarded")
        self.assertEqual(self.merged, ["feat"])

    def test_a_reviewers_leftover_is_its_own(self):
        self.auto()
        self.r.review_ready()
        self.r.ref()
        self.r.review("APPROVED")  # uncommitted
        code, _ = self.drive()
        self.assertEqual((code, self.roles(), self.merged), (h.EXIT_DONE, [], ["feat"]))
        self.assertEqual(self.r.log()[-1], "feat: reviewer")

    def test_a_driver_failure_prompts_the_orchestrator(self):
        self.r.review_ready()

        def boom(*a, **k):
            raise h.HerdrError("agent_not_started", "rev-feat-repo did not start")

        self.stub_herdr("run_role", boom)
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ERROR)
        self.assertIn("harness: feat driver failed: rev-feat-repo did not start", self.notes[-1])
        self.assertEqual(h.Lock.driver(self.r.wt).running(), 0)

        def bug(*a, **k):
            raise KeyError("root_pane")  # an unexpected herdr shape is a launcher bug, still reported

        self.stub_herdr("run_role", bug)
        with contextlib.redirect_stderr(io.StringIO()):  # the traceback
            code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ERROR)
        self.assertIn("harness: feat driver failed: KeyError: 'root_pane'", self.notes[-1])

    def test_message_at_the_gate_goes_to_the_judged_role(self):
        self.r.spec_ready()
        revised = R1 + "\n- R2: THE SYSTEM SHALL log it."
        self.actions["spec_author"] = [lambda: self.spec_author(reqs=revised, tasks="- [ ] T1: test f (R1, R2)")]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        code, _ = self.drive(msg="please add R2")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer"], "the revised spec is reviewed again")
        self.assertIn("please add R2", self.calls[0][1])
        self.assertEqual(self.r.log().count("feat: clear spec review"), 1)
        self.assertIn("spec ready, needs you", self.notes[-1], "the revised spec waits at the gate again")

    def test_failed_check_goes_back_then_on(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [lambda: self.implementer(with_test=False), self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "implementer", "reviewer"])
        self.assertIn("no test file named for R1 in ## Verification exists", self.calls[3][1])
        self.assertEqual(self.merged, ["feat"])

    def test_red_tests_block_after_fix_tries(self):
        self.auto()
        self.r.write_cfg(gates={"approve": "auto", "merge": "auto"}, test_cmd="false")
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer] * 5
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer"] + ["implementer"] * h.FIX_TRIES)
        self.assertIn("test_cmd fails with main merged in", self.calls[3][1])
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn(f"implementer ran {h.FIX_TRIES} times in a row, still at 'review'", self.notes[-1])
        self.assertIn("tests failed (exit 1: false)", (self.r.fdir / "questions.md").read_text())
        self.assertFalse(self.merged)

    def test_a_conflict_goes_to_the_implementer_as_files(self):
        self.auto()
        (self.r.root / "src" / "m.py").write_text("def f():\n    return 2\n")
        self.r.commit("main: 2")

        def implement_conflicting():
            self.implementer()
            (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")

        def resolve():
            (self.r.wt / "src" / "m.py").write_text("def f():\n    return 1\n")

        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [implement_conflicting, resolve]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "implementer", "reviewer"])
        conflict = "The base branch 'main' was merged in and conflicts in src/m.py: resolve them."
        self.assertTrue(self.calls[3][1].endswith(conflict), self.calls[3][1])
        self.assertTrue(
            self.calls[2][1].endswith("harness/features/feat/ (base branch: main)."), "a first run: the job line"
        )
        self.assertIn("feat: merge main, conflicts in src/m.py", self.r.log())
        self.assertEqual(self.merged, ["feat"])

    def test_an_unresolved_conflict_counts_as_a_retry(self):
        self.auto()
        (self.r.root / "src" / "m.py").write_text("def f():\n    return 2\n")
        self.r.commit("main: 2")

        def implement_conflicting():
            self.implementer()
            (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")

        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [implement_conflicting, lambda: None, lambda: None, lambda: None]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer"] + ["implementer"] * h.FIX_TRIES)
        self.assertEqual(self.r.state(), "blocked")

    def test_another_role_resets_the_retries(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer, self.fix, self.fix]
        self.actions["reviewer"] = [
            lambda: self.reviewer("CHANGES_REQUESTED"),
            lambda: self.reviewer("CHANGES_REQUESTED"),
            self.reviewer,
        ]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer"] + ["implementer", "reviewer"] * 3)
        self.assertEqual(self.merged, ["feat"])
        self.assertEqual(self.r.log().count("feat: clear review"), 2)

    def test_changes_requested_loops_then_blocks_at_the_cap(self):
        self.auto(rounds=2)
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer, self.fix]
        self.actions["reviewer"] = [
            lambda: self.reviewer("CHANGES_REQUESTED"),
            lambda: self.reviewer("CHANGES_REQUESTED"),
        ]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(
            self.roles(), ["spec_author", "spec_reviewer", "implementer", "reviewer", "implementer", "reviewer"]
        )
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn("blocked, needs you", self.notes[-1])
        self.assertFalse(self.merged)

    def test_spec_changes_requested_loops_then_blocks_at_the_cap(self):
        self.auto(rounds=2)

        def reword():
            self.assertIn("- R1: the error path", (self.r.fdir / "spec_review.md").read_text(), "findings on disk")
            self.spec_author(reqs="- R1: WHEN f is called, THE SYSTEM SHALL return 1 at once.")

        self.actions["spec_author"] = [self.spec_author, reword]
        self.actions["spec_reviewer"] = [lambda: self.spec_reviewer("CHANGES_REQUESTED")] * 2
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "spec_author", "spec_reviewer"])
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn("blocked, needs you", self.notes[-1])
        questions = (self.r.fdir / "questions.md").read_text()
        self.assertIn("2 spec review rounds ended in CHANGES_REQUESTED (max_review_rounds 2)", questions)
        self.assertIn("the findings are in spec_review.md", questions)
        self.assertEqual(self.r.log().count("feat: clear spec review"), 1)
        self.assertFalse(h.Git(self.r.wt).ref_exists(APPROVED))
        self.assertFalse(self.merged)

    def test_an_incomplete_review_goes_back_to_the_reviewer(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [lambda: self.reviewer(ticked=3), self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "reviewer", "reviewer"])
        self.assertIn(f"ticks 3 of the {CHECKPOINTS} checkpoints", self.calls[4][1])
        self.assertEqual(self.merged, ["feat"])

    def test_an_incomplete_spec_review_goes_back_to_the_spec_reviewer(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [lambda: self.spec_reviewer(ticked=3), self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "spec_reviewer", "implementer", "reviewer"])
        self.assertIn(f"spec_review.md ticks 3 of the {SPEC_CHECKPOINTS} checkpoints", self.calls[2][1])
        self.assertEqual(self.merged, ["feat"])

    def test_the_spec_review_form_is_seeded(self):
        self.auto()
        self.r.spec_review_ready()

        def tick_all():
            p = self.r.fdir / "spec_review.md"
            text = p.read_text()
            self.assertTrue(text.startswith("# Review\n\n## Findings\n\n## Checked\n- [ ] 1. "), text)
            self.assertEqual(text.count("- [ ]"), SPEC_CHECKPOINTS)
            self.assertTrue(h.Git(self.r.wt).ref_exists(SPEC_REVIEW_START), "the round is marked")
            p.write_text(text.replace("- [ ]", "- [x]"))

        self.actions["spec_reviewer"] = [tick_all]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual((code, self.merged), (h.EXIT_DONE, ["feat"]))
        self.assertEqual(self.roles(), ["spec_reviewer", "implementer", "reviewer"])
        self.assertEqual(self.r.log()[1:4], ["feat: spec_author", "feat: spec_reviewer", "feat: implementer"])

    def test_a_spec_reviewer_may_ask(self):
        self.auto()
        self.r.spec_review_ready()

        def ask():
            self.r.questions("- Is R1 meant to cover negative input?")

        self.actions["spec_reviewer"] = [ask, self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual((code, self.r.state()), (h.EXIT_DONE, "blocked"))
        self.assertEqual(self.r.log()[-1], "feat: spec_reviewer")
        code, _ = self.drive(msg="no, positive only")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_reviewer", "spec_reviewer", "implementer", "reviewer"])
        self.assertIn("Answer: no, positive only", self.calls[1][1])
        self.assertEqual(self.merged, ["feat"])

    def test_pre_approval_leftovers_go_to_their_role(self):
        self.auto()
        self.r.spec_review_ready()
        self.r.ref(role="spec_reviewer")
        self.r.review("APPROVED", role="spec_reviewer")  # uncommitted: an unfinished spec reviewer run
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertIn("committing what an unfinished spec_reviewer run left", out)
        self.assertEqual(self.roles(), ["implementer", "reviewer"])
        self.assertIn("feat: spec_reviewer", self.r.log())
        self.assertEqual(self.merged, ["feat"])

    def test_a_spec_authors_leftover_is_its_own(self):
        self.r.spec()  # uncommitted, no tasks yet: an unfinished spec author run
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertIn("committing what an unfinished spec_author run left", out)
        self.assertEqual(self.r.log()[1], "feat: spec_author")
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer"])
        self.assertIn("spec ready, needs you", self.notes[-1])

    def test_the_review_form_is_seeded(self):
        self.auto()
        self.r.review_ready()

        def tick_all():
            p = self.r.fdir / "review.md"
            text = p.read_text()
            self.assertTrue(text.startswith("# Review\n\n## Findings\n\n## Checked\n- [ ] 1. "), text)
            p.write_text(text.replace("- [ ]", "- [x]"))

        self.actions["reviewer"] = [tick_all]
        code, _ = self.drive()
        self.assertEqual((code, self.merged), (h.EXIT_DONE, ["feat"]))
        self.assertEqual(self.r.log()[-1], "feat: reviewer")

    def test_the_answer_goes_to_the_role_the_state_names(self):
        self.auto()
        self.r.review_ready()
        self.actions["reviewer"] = [lambda: self.reviewer(ticked=3)] * 3 + [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn(f"reviewer ran {h.FIX_TRIES} times in a row, still at 'review'", self.notes[-1])
        code, _ = self.drive(msg="tick them all")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["reviewer"] * 4, "the answer went to the reviewer, not the implementer")
        self.assertIn("Answer: tick them all", self.calls[3][1])
        self.assertEqual(self.merged, ["feat"])

    def test_a_reviewer_may_ask(self):
        self.auto()
        self.r.review_ready()
        self.actions["reviewer"] = [lambda: self.r.questions("- Is R1 meant to cover negative input?"), self.reviewer]
        code, _ = self.drive()
        self.assertEqual((code, self.r.state()), (h.EXIT_DONE, "blocked"))
        self.assertEqual(self.r.log()[-1], "feat: reviewer")
        self.assertIn("blocked, needs you: read", self.notes[-1])
        code, _ = self.drive(msg="no, positive only")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["reviewer", "reviewer"])
        self.assertIn("Answer: no, positive only", self.calls[1][1])
        self.assertEqual(self.merged, ["feat"])

    def test_the_human_message_survives_a_conflict(self):
        self.auto()
        (self.r.root / "src" / "m.py").write_text("def f():\n    return 2\n")
        self.r.commit("main: 2")
        self.r.approved()
        self.r.implemented()
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")
        self.r.commit("feat: implementer", cwd=self.r.wt)
        self.actions["implementer"] = [lambda: (self.r.wt / "src" / "m.py").write_text("def f():\n    return 1\n")]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive(msg="also log it")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["implementer", "reviewer"])
        self.assertIn("also log it", self.calls[0][1])
        self.assertIn("conflicts in src/m.py: resolve them", self.calls[0][1])
        self.assertEqual(self.merged, ["feat"])

    def test_the_implementer_gets_the_conflict_a_merge_left(self):
        self.auto()
        self.r.reviewed("APPROVED")
        (self.r.root / "tests" / "test_feat.py").write_text("# main's version\n")
        self.r.commit("main: edits the test")
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            h.land(self.r.root, self.r.wt, "feat", "main")
        self.assertEqual(self.r.state(), "implementing")
        self.actions["implementer"] = [self.implementer]  # writes the test file back, markers gone
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["implementer", "reviewer"])
        self.assertIn("conflicts in tests/test_feat.py: resolve them", self.calls[0][1])
        self.assertEqual(self.merged, ["feat"])

    def test_an_implementer_that_breaks_tasks_md_goes_back(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [
            lambda: (self.implementer(), self.r.tasks("- [x] T1: test f (R9)")),
            self.implementer,
        ]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "implementer", "reviewer"])
        self.assertIn("T1 cites unknown R9", self.calls[3][1])
        self.assertEqual(self.merged, ["feat"])

    def test_a_reviewers_edits_outside_review_md_are_discarded(self):
        self.auto()

        def review_and_fix():
            self.reviewer()
            (self.r.wt / "src" / "m.py").write_text("# the reviewer fixed it\n")

        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [review_and_fix]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.merged, ["feat"])
        self.assertEqual((self.r.wt / "src" / "m.py").read_text(), "def f():\n    return 1\n")
        self.assertIn("discarded, outside the reviewer's files: src/m.py", out)

    def test_a_spec_reviewers_edits_outside_its_form_are_discarded(self):
        self.auto()

        def review_and_edit():
            self.spec_reviewer()
            self.r.spec(reqs="- R1: WHEN f is called, THE SYSTEM SHALL return 2.")

        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [review_and_edit]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.merged, ["feat"])
        self.assertIn("SHALL return 1.", (self.r.fdir / "spec.md").read_text())
        self.assertIn("discarded, outside the spec_reviewer's files: harness/features/feat/spec.md", out)

    def test_a_question_blocks_and_the_answer_goes_to_the_asker(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [lambda: self.r.questions("- Is 1 right?")]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.r.state(), "blocked")
        self.assertIn("blocked, needs you: read", self.notes[-1])
        self.assertIn("questions.md", self.notes[-1])
        self.assertEqual(self.closed, ["spec_author", "spec_reviewer", "implementer"], "finished by asking: done")
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, _ = self.drive(msg="yes, 1")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(self.roles(), ["spec_author", "spec_reviewer", "implementer", "implementer", "reviewer"])
        self.assertIn("Is 1 right?", self.calls[3][1])
        self.assertIn("Answer: yes, 1", self.calls[3][1])
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").questions(), [])
        self.assertEqual(self.merged, ["feat"])

    def stuck_role(self, max_minutes, settle_after):
        """A spec author whose prompt never settles by itself; wait_agent settles after `settle_after` waits; each
        wait moves the clock 10 minutes."""
        self.r.write_cfg(max_role_minutes=max_minutes)
        clock, waits = [0.0], [0]

        def wait(name, ms):
            clock[0] += 600
            waits[0] += 1
            return waits[0] >= settle_after, ""

        def prompt(root, wt, role, slug, text, timeout_ms, acfg):
            self.fake_prompt(root, wt, role, slug, text, timeout_ms, acfg)
            return role != "spec_author", ""  # only the spec author is stuck

        self.stub_herdr("wait", wait)
        self.stub_herdr("run_role", prompt)
        self.stub("time", types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        return waits

    def test_a_role_past_max_role_minutes_stops_the_driver(self):
        waits = self.stuck_role(max_minutes=30, settle_after=99)
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertIn("needs you: spec_author still working after 30 min (max_role_minutes 30)", self.notes[-1])
        self.assertEqual(waits[0], 3)
        self.assertEqual(self.closed, [], "the agent is left running for the human to look at")

    def test_the_roles_tab_goes_when_the_run_is_judged(self):
        self.auto()
        self.actions["spec_author"] = [self.spec_author]
        self.actions["spec_reviewer"] = [self.spec_reviewer]
        self.actions["implementer"] = [lambda: self.implementer(with_test=False), self.implementer]
        self.actions["reviewer"] = [lambda: self.reviewer(ticked=2), self.reviewer]
        code, _ = self.drive()
        self.assertEqual((code, self.merged), (h.EXIT_DONE, ["feat"]))
        self.assertEqual(
            self.closed, ["spec_author", "spec_reviewer", "implementer", "implementer", "reviewer", "reviewer"]
        )

    def test_no_role_limit_waits_until_the_role_settles(self):
        waits = self.stuck_role(max_minutes=0, settle_after=20)
        code, _ = self.drive()  # human approve gate: stops at spec_ready once the spec author settles
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(waits[0], 20)
        self.assertIn("spec ready, needs you", self.notes[-1])


# ----------------------------------------------------------------------------- commands on the board
class Commands(Case):
    def setUp(self):
        super().setUp()
        self.chdir(self.r.root)

    def test_status_groups(self):
        code, out = self.output(h.cmd_status, Namespace())
        self.assertEqual(code, 0)
        self.assertIn("1 to run", out, "nothing runs: the orchestrator's move")
        self.assertIn("run spec_author", out)
        holder = hold_lock(self, h.Lock.driver(self.r.wt))
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("1 working", out)
        self.assertIn("(driver running)", out)
        holder.kill()
        holder.wait()
        self.r.spec_ready()
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("1 needs you", out)
        self.assertIn("approve (human)", out)
        self.r.questions()
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("blocked", out)
        self.assertIn("read questions.md", out)
        self.r.git("worktree", "remove", "--force", str(self.r.wt))
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("no features yet", out, "no folder on main, no worktree: nothing to show")
        self.r.git("merge", "-q", "--no-edit", "feat")
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("1 done", out)
        self.assertIn("  feat  done", out)

    def test_doctor_shows_the_setup_first(self):
        self.chdir(self.r.wt)  # from a worktree it still shows the main checkout's config
        agents = {
            **AGENTS,
            "implementer": {"harness": "codex", "effort": "max", "permissions": "guarded", "args": ["-x"]},
            "reviewer": {"harness": "gemini", "effort": "high", "permissions": "full"},
        }
        self.r.write_cfg(gates={"approve": "auto"}, agents=agents)
        _, out = self.output(h.cmd_doctor, Namespace(no_tests=True, models=False))
        self.assertTrue(out.startswith("harness/config.json\n"), "the setup comes first")
        self.assertIn("gates              approve: auto   merge: human", out)
        self.assertIn("implementer        codex   (default model)", out)
        self.assertIn("spec_reviewer      claude  (default model)", out)
        self.assertIn("effort       max (codex: xhigh)", out)
        self.assertIn("permissions  guarded: never asks, the CLI blocks risky actions; its sandbox", out)
        flags = "-c model_reasoning_effort=xhigh --sandbox workspace-write --ask-for-approval never -x"
        self.assertIn(f"flags        {flags}", out, "what the CLI is given, verbatim")
        self.assertIn("effort       high (not translated for gemini: put its flag in args)", out)
        self.assertIn("permissions  full (not translated for gemini: put its flags in args)", out)
        self.assertIn("flags        -\n", out, "nothing translated, no args")
        self.assertIn("orchestrator       the agent you started (not in this file)", out)
        self.assertNotIn("settings.local.json", out)
        self.assertIn("copy_files         .env", out)
        self.assertIn("ok    worktree", out)
        self.assertIn("feat (spec)", out)

    def start(self, launch_driver):
        """`start newf` from the main checkout, herdr creating the worktree at tmp/wt-new. The tools doctor looks for
        are stand-ins on PATH, so the suite does not depend on what this machine has installed."""
        bindir = self.r.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        for tool in ("herdr", "claude"):
            (bindir / tool).write_text("#!/bin/sh\nexit 0\n")
            (bindir / tool).chmod(0o755)
        self.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

        def create(root, slug, base):
            path = self.r.tmp / "wt-new"
            self.r.git("worktree", "add", "-q", "-b", slug, str(path), base)
            return {"worktree": {"path": str(path)}, "workspace": {"workspace_id": "w9"}}

        self.stub_herdr("create_worktree", create)
        self.stub_herdr("launch_driver", launch_driver)
        return self.output(h.cmd_start, Namespace(slug="newf", intent="do y"))

    def test_start_creates_the_feature_and_starts_its_driver(self):
        launched = []
        code, out = self.start(lambda root, wt, slug, msg, pane: launched.append((wt, slug, msg)) or "p7")
        self.assertEqual(code, 0)
        spec = self.r.tmp / "wt-new" / "harness" / "features" / "newf" / "spec.md"
        self.assertTrue(spec.exists())
        self.assertIn("# Newf\n\n## Intent\ndo y\n", spec.read_text())
        self.assertEqual(launched, [(self.r.tmp / "wt-new", "newf", "")])
        self.assertIn("doctor: no problem", out, "the setup is checked before every feature")
        self.assertIn("driver for newf: pane p7", out)
        self.assertFalse((self.r.root / ".claude").exists(), "nothing tool-specific is written")

    def test_start_refuses_on_a_doctor_problem(self):
        self.r.git("update-ref", "refs/harness/old/approved", "HEAD")  # a leftover doctor reports
        with self.assertRaises(h.Die) as cm:
            self.start(lambda *a: self.fail("the driver must not start"))
        msg = cm.exception.msg
        self.assertTrue(msg.startswith("harness/config.json\n"), "doctor's whole output, the setup first")
        self.assertIn("FAIL  stale refs/harness/old/approved", msg)
        self.assertIn("doctor finds 1 problem(s): fix them, then start again", msg)
        self.assertFalse((self.r.tmp / "wt-new").exists(), "nothing created")
        self.assertEqual(self.r.git("branch", "--list", "newf"), "")

    def test_slugs_and_the_command_line(self):
        for slug in ("ab\n", "Ab", "a", "-ab", "a" * 21):
            with self.assertRaises(h.Die):
                h.start_preconditions(self.r.root, self.r.cfg(), slug)
        h.start_preconditions(self.r.root, self.r.cfg(), "a" * 20)
        for role in h.ROLES:
            self.assertLessEqual(len(h.agent_name(role, "a" * 20, self.r.root)), 32, "herdr's name limit")
        a = h.parser().parse_args(["drive", "--notify", "p", "--", "feat", "--flag"])
        self.assertEqual((a.msg, a.notify, a.tab), ("--flag", "p", False), "a message may start with a dash")
        self.assertEqual(h.parser().parse_args(["drive", "feat"]).msg, "")
        self.assertEqual(h.parser().parse_args(["drive", "feat", "--msg-file", "m"]).msg_file, "m")
        self.assertNotIn("--msg-file", h.parser().format_help(), "the launcher's own channel, not a human's")

    def test_drive_reads_the_message_file_once(self):
        self.r.spec_ready()
        seen = []

        class FakeDriver:
            def __init__(self, root, cfg, f, msg, notify):
                seen.append((f.slug, msg, notify))

            def run(self):
                return 0

        self.stub("Driver", FakeDriver)
        path = self.r.tmp / "msg"
        path.write_text("two\nlines\n")
        code = h.cmd_drive(Namespace(slug="feat", msg="", tab=False, notify="p", msg_file=str(path)))
        self.assertEqual((code, seen), (0, [("feat", "two\nlines\n", "p")]))
        self.assertFalse(path.exists(), "read once")
        h.cmd_drive(Namespace(slug="feat", msg="typed", tab=False, notify="", msg_file=""))
        self.assertEqual(seen[-1], ("feat", "typed", ""))

    def test_an_auto_merge_runs_merge_detached(self):
        self.r.reviewed("APPROVED")
        spawned = []
        fake = {**vars(subprocess), "Popen": lambda *a, **k: spawned.append((a, k))}
        self.stub("subprocess", types.SimpleNamespace(**fake))
        f = h.Feature.find(self.r.root, "feat", "main")
        driver = h.Driver(self.r.root, self.r.cfg(), f, "", "wB:p2")
        with self.assertRaises(h._Stop) as cm, contextlib.redirect_stdout(io.StringIO()):
            driver.spawn_merge()
        self.assertEqual(cm.exception.code, h.EXIT_DONE)
        (argv,), kw = spawned[0]
        self.assertEqual(argv[1:], [h.__file__, "merge", "feat", "--notify", "wB:p2"], "merge reports its own outcome")
        self.assertEqual(Path(kw["cwd"]).resolve(), self.r.root.resolve(), "from the main checkout")
        self.assertTrue(kw["start_new_session"], "this pane goes with the worktree")
        self.assertTrue((self.r.root / ".git" / "harness-merge-feat.log").exists())

    def test_start_says_how_to_drive_when_the_driver_cannot_start(self):
        def fail(*args):
            raise h.HerdrError("driver_not_started", "no driver")

        with self.assertRaises(h.Die) as cm:
            self.start(fail)
        self.assertIn("start its driver: python3 harness/harness.py drive newf --tab", cm.exception.msg)
        self.assertTrue((self.r.tmp / "wt-new" / "harness" / "features" / "newf" / "spec.md").exists())

    def test_check_command_finds_the_worktree_and_says_the_state(self):
        self.r.spec_ready()
        code, out = self.output(h.cmd_check, Namespace(slug="feat", tests=False, no_tests=True))
        self.assertEqual(code, 0)
        self.assertIn(f"checking feat in {self.r.wt.resolve()}", out)
        self.assertIn("feat: spec_ready\n", out)
        (self.r.wt / "harness" / "features" / "stray").mkdir()  # no spec.md: not a feature
        self.chdir(self.r.wt)
        code, out = self.output(h.cmd_check, Namespace(slug=None, tests=False, no_tests=True))
        self.assertEqual((code, out), (0, "feat: spec_ready\nOK (feat)\n"), "every feature of this checkout")
        self.assertIn("OK (feat)", out)
        self.r.approve()
        code, out = self.output(h.cmd_check, Namespace(slug="feat", tests=False, no_tests=True))
        self.assertEqual(code, 1)
        self.assertIn("feat: implementing\n", out)
        self.assertIn("feat: T1 is not ticked", out)
        self.chdir(self.r.wt)
        self.r.implemented()
        self.r.write_cfg(test_cmd="false")
        code, out = self.output(h.cmd_check, Namespace(slug="feat", tests=False, no_tests=False))
        self.assertEqual(code, 1)
        self.assertIn("feat: review\n", out)
        self.assertIn("tests failed (exit 1: false)", out, "from review on, the tests run by default")

    def test_approve_needs_spec_ready_and_no_driver(self):
        self.stub_herdr("resume", lambda *a: 0)
        with self.assertRaises(h.Die) as cm, contextlib.redirect_stdout(io.StringIO()):
            h.cmd_approve(Namespace(slug="feat", undo=False))
        self.assertIn("'feat' is spec, expected spec_ready", cm.exception.msg)
        self.r.spec_ready()
        holder = hold_lock(self, h.Lock.driver(self.r.wt))
        with self.assertRaises(h.Die):
            h.cmd_approve(Namespace(slug="feat", undo=False))
        holder.kill()
        holder.wait()
        (self.r.fdir / "spec.md").write_text((self.r.fdir / "spec.md").read_text() + "\nmore\n")  # unfinished run
        with self.assertRaises(h.Die) as cm:
            h.cmd_approve(Namespace(slug="feat", undo=False))
        self.assertIn("uncommitted changes from an unfinished run", cm.exception.msg)
        self.assertFalse(h.Git(self.r.wt).ref_exists(APPROVED))
        self.r.git("checkout", "--", "harness/features/feat/spec.md", cwd=self.r.wt)
        code, out = self.output(h.cmd_approve, Namespace(slug="feat", undo=False))
        self.assertEqual(code, 0)
        self.assertIn("approved feat", out)
        self.assertEqual(self.r.state(), "implementing")
        self.assertEqual(self.r.log()[-1], "feat: spec_reviewer", "approval is a ref, not a commit")
        self.r.implemented()  # an implementer stopped mid-run (max_role_minutes): its files are on disk, uncommitted
        with self.assertRaises(h.Die) as cm:
            h.cmd_approve(Namespace(slug="feat", undo=True))
        self.assertIn("uncommitted changes from an unfinished run", cm.exception.msg, "undo would lose them")
        self.assertTrue(h.Git(self.r.wt).ref_exists(APPROVED))
        h.Git(self.r.wt).restore_rest()
        self.r.implemented()
        self.r.commit("feat: implementer", cwd=self.r.wt)  # the implementer's commits came after the spec review
        code, out = self.output(h.cmd_approve, Namespace(slug="feat", undo=True))
        self.assertEqual(code, 0)
        self.assertIn("approval of feat withdrawn (spec_ready)", out, "the spec was frozen: its review stands again")
        self.assertFalse(h.Git(self.r.wt).ref_exists(APPROVED))
        with self.assertRaises(h.Die) as cm:
            h.cmd_approve(Namespace(slug="feat", undo=True))
        self.assertIn("'feat' is not approved (spec_ready)", cm.exception.msg)

    def test_merge_reports_a_failure_to_the_notify_pane(self):
        notes = []
        self.stub_herdr("notify", lambda pane, text: notes.append(text))
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify="wB:p2"))
        self.assertEqual(ctx.exception.code, h.EXIT_ERROR)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("harness: feat merge failed: 'feat' is spec; need a review"), notes[0])
        self.assertIn("harness-merge-feat.log", notes[0])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertEqual(len(notes), 1, "nothing to notify without --notify")

    def test_merge_lands_and_cleans_up(self):
        self.stub_herdr("workspace_of", lambda wt: None)
        self.r.reviewed("APPROVED")
        (self.r.root / "scratch.txt").write_text("an untracked file on main is nobody's business\n")
        notes = []
        self.stub_herdr("notify", lambda pane, text: notes.append((pane, text)))
        code, out = self.output(h.cmd_merge, Namespace(slug="feat", notify="wB:p2"))
        self.assertEqual(code, 0)
        self.assertIn("merged feat into main", out)
        self.assertIn("worktree removed; branch deleted", out)
        self.assertEqual(notes[-1][0], "wB:p2")
        self.assertIn("harness: feat merged into main at", notes[-1][1])
        self.assertEqual(self.r.git("log", "-1", "--format=%s"), "feat: merge")
        self.assertTrue((self.r.root / "harness" / "features" / "feat" / "review.md").exists())
        self.assertTrue((self.r.root / "tests" / "test_feat.py").exists())
        self.assertEqual(h.Checker(self.r.root, "feat", "main").state(), "done")
        self.assertFalse(self.r.wt.exists())
        self.assertNotIn("feat", self.r.git("branch", "--format=%(refname:short)"))
        self.assertFalse(h.Git(self.r.root).ref_exists(REVIEW_START))
        self.assertFalse(h.Git(self.r.root).ref_exists(APPROVED))
        self.assertEqual(h.Lock.merge(self.r.root).running(), 0)

    def test_a_failed_merge_hands_the_feature_back(self):
        self.r.write_cfg(test_cmd="test ! -f RED")
        self.r.reviewed("APPROVED")
        (self.r.root / "RED").write_text("")
        self.r.commit("main: breaks the tests")
        with self.assertRaises(h.Die) as cm, contextlib.redirect_stdout(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertIn(
            "the check fails; not merging. Run `python3 harness/harness.py drive feat --tab`", cm.exception.msg
        )  # noqa: E501
        self.assertEqual(self.r.log()[-2:], ["feat: merge main", "feat: clear review"])
        self.assertEqual(self.r.state(), "review", "the review no longer stands for what would land")
        self.r.git("rm", "-q", "RED")
        self.r.commit("main: fixed")
        (self.r.root / "tests" / "test_feat.py").write_text("# main's version\n")
        self.r.commit("main: edits the test")
        self.r.ref()
        self.r.review("APPROVED")
        self.r.commit("feat: reviewer", cwd=self.r.wt)
        with self.assertRaises(h.Die) as cm, contextlib.redirect_stdout(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertIn("conflicts in tests/test_feat.py; not merging", cm.exception.msg)
        self.assertEqual(self.r.state(), "implementing")
        self.assertIn("feat: conflict markers left in tests/test_feat.py", self.r.check())

    def test_a_failed_worktree_removal_keeps_the_feature_mergeable(self):
        self.stub_herdr("workspace_of", lambda wt: None)
        self.r.reviewed("APPROVED")
        self.r.git("worktree", "lock", str(self.r.wt))
        code, out = self.output(h.cmd_merge, Namespace(slug="feat", notify=""))
        self.assertEqual(code, 0)
        self.assertIn("could not remove the worktree", out)
        self.assertTrue(h.Git(self.r.root).ref_exists(APPROVED), "the launcher's marks stay until the cleanup is done")
        self.assertEqual(self.r.state(), "reviewed")
        self.r.git("worktree", "unlock", str(self.r.wt))
        code, out = self.output(h.cmd_merge, Namespace(slug="feat", notify=""))
        self.assertEqual(code, 0)
        self.assertIn("worktree removed; branch deleted", out)

    def test_merge_refuses_a_second_merge_and_leaves_main_clean_on_failure(self):
        self.r.reviewed("APPROVED")
        holder = hold_lock(self, h.Lock.merge(self.r.root))
        with self.assertRaises(h.Die) as cm:
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertIn("another merge is running", cm.exception.msg)
        holder.kill()
        holder.wait()
        holder = hold_lock(self, h.Lock.driver(self.r.wt))
        with self.assertRaises(h.Die) as cm:
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertIn("is still running feat", cm.exception.msg, "no merge under a driver")
        holder.kill()
        holder.wait()
        head = self.r.git("rev-parse", "HEAD")
        orig = h.Git.ok

        def ok(git, *args):
            if args[0] == "merge" and "--no-ff" in args:
                git.proc("merge", "--no-commit", "--no-ff", args[-1], check=False)  # a merge left half done
                return False
            return orig(git, *args)

        self.stub("Git", type("Git", (h.Git,), {"ok": ok}))
        with self.assertRaises(h.Die) as cm, contextlib.redirect_stdout(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertIn("'main' is left as it was", cm.exception.msg)
        self.assertEqual(self.r.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.r.git("status", "--porcelain"), "", "the failed merge was aborted")
        self.assertTrue(self.r.wt.exists(), "nothing removed")


# ----------------------------------------------------------------------------- the herdr adapter, through a fake herdr
class HerdrAdapter(Case):
    fake_herdr = True
    ACFG = {"harness": "claude", "model": "m", "effort": "high", "permissions": "full"}

    def setUp(self):
        super().setUp()
        self.spec, self.srev, self.impl, self.rev = (h.agent_name(r, "feat", self.r.root) for r in h.ROLES)

    def test_each_run_starts_a_fresh_agent_in_the_roles_tab(self):
        settled, note = h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "do it", 1000, agent(self.ACFG))
        self.assertEqual((settled, note), (True, ""))
        self.assertEqual(self.calls("tab", "close"), [], "no earlier tab")
        self.assertEqual(
            self.calls("tab", "create"),
            [["tab", "create", "--workspace", "w1", "--cwd", str(self.r.wt), "--label", "implementer", "--no-focus"]],
        )
        self.assertEqual(
            self.calls("agent", "start"),
            [
                [
                    "agent",
                    "start",
                    self.impl,
                    "--kind",
                    "claude",
                    "--pane",
                    "w1:p2",
                    "--timeout",
                    "120000",
                    "--",
                    "--model",
                    "m",
                    "--effort",
                    "high",
                    "--dangerously-skip-permissions",
                ]
            ],
        )
        prompts = self.calls("agent", "prompt")
        self.assertEqual(prompts, [["agent", "prompt", self.impl, "do it", "--wait", "--timeout", "1000"]])
        # the next run of the same role, after a config change: its tab is closed, a new agent gets the new flags
        self.state["tabs"] = [{"tab_id": "w1:t2", "label": "implementer"}, {"tab_id": "w1:t3", "label": "reviewer"}]
        self.save_state()
        changed = agent({**self.ACFG, "model": "m2", "permissions": "guarded"})
        h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "again", 1000, changed)
        self.assertEqual(self.calls("tab", "close"), [["tab", "close", "w1:t2"]], "only the implementer's tab")
        self.assertEqual(len(self.calls("tab", "create")), 2)
        start = self.calls("agent", "start")[-1]
        self.assertEqual(
            start[start.index("--") :], ["--", "--model", "m2", "--effort", "high", "--permission-mode", "auto"]
        )
        self.assertEqual(self.calls("agent", "prompt")[-1][2:4], [self.impl, "again"])

    def test_startup_dialogs_are_answered(self):
        log = io.StringIO()  # the launcher says which dialog it answered: the driver's tab shows it
        redirect = contextlib.redirect_stdout(log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        trust = "Quick safety check: Is this a project you created or one you trust?\n❯ No, exit\n  Yes, I trust "
        trust += "this folder"  # Claude Code 2.1.317 in a fresh worktree, seen live
        self.state.update(start_status="blocked", screen=trust)  # Claude Code 2.1.317 in a fresh worktree, seen live
        self.save_state()
        settled, note = h.HERDR.run_role(self.r.root, self.r.wt, "reviewer", "feat", "go", 1000, agent(self.ACFG))
        self.assertEqual((settled, note), (True, ""))
        self.assertIn(f"{self.rev}: answered the 'trust this folder' dialog (down enter)", log.getvalue())
        self.assertEqual(self.calls("agent", "send-keys"), [["agent", "send-keys", self.rev, "down", "enter"]])
        self.assertEqual(self.calls("agent", "read")[0][3:5], ["--source", "visible"], "what is on screen now")
        waits = [c for c in self.calls("agent", "wait") if c[2] == self.rev]
        self.assertEqual(
            waits[0][3:7], ["--until", "idle", "--until", "done"], "after the keys: blocked is not settled"
        )
        self.state.update(screen="Something unexpected?", agents={})
        self.save_state()
        settled, note = h.HERDR.run_role(self.r.root, self.r.wt, "spec_author", "feat", "go", 1000, agent(self.ACFG))
        self.assertEqual((settled, note), (True, "blocked at startup"))
        self.state.update(screen="Do you trust this folder? (y/n)", agents={})
        self.save_state()
        codex = {"harness": "codex", "permissions": "full"}
        settled, note = h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "go", 1000, agent(codex))
        self.assertEqual(note, "blocked at startup", "a dialog is answered only for the CLI known to show it")
        self.assertEqual(len(self.calls("agent", "send-keys")), 1)
        self.state.update(
            screen="Folder access\n  Trust this folder? Codex can read, edit, and run files here", agents={}
        )
        self.save_state()
        settled, note = h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "go", 1000, agent(codex))
        self.assertEqual((settled, note), (True, ""))
        self.assertEqual(self.calls("agent", "send-keys")[-1], ["agent", "send-keys", self.impl, "enter"])
        self.state.update(
            screen="  Update available · 0.164.0 → 0.166.0\n› 1. Update now (runs `npm install`)", agents={}
        )
        self.save_state()
        settled, note = h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "go", 1000, agent(codex))
        self.assertEqual((settled, note), (True, ""))
        keys = self.calls("agent", "send-keys")[-1]
        self.assertEqual(keys, ["agent", "send-keys", self.impl, "down", "down", "enter"], "skip, never update")

    def test_prompt_outcomes(self):
        self.state["prompt_error"] = "timeout"
        self.save_state()
        self.assertEqual(
            h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "x", 5, agent(self.ACFG)), (False, "")
        )
        self.state["prompt_error"] = "agent_blocked"
        self.save_state()
        self.assertEqual(
            h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "x", 5, agent(self.ACFG)),
            (True, "agent_blocked"),
        )
        self.assertEqual(len(self.calls("agent", "start")), 2, "a fresh agent every run")
        self.state["prompt_error"] = "agent_prompt_stalled"  # the text sits in the input box, the agent idle
        self.save_state()
        with contextlib.redirect_stdout(io.StringIO()):
            out = h.HERDR.run_role(self.r.root, self.r.wt, "implementer", "feat", "x", 5, agent(self.ACFG))
        self.assertEqual(out, (True, ""), "Enter once, then wait for the agent")
        self.assertEqual(self.calls("agent", "send-keys")[-1], ["agent", "send-keys", self.impl, "enter"])
        self.assertEqual(self.calls("agent", "wait")[-1][:3], ["agent", "wait", self.impl])
        self.assertEqual(h.HERDR.state(self.impl), "idle")
        self.assertEqual(h.HERDR.state("nobody"), "gone")
        self.assertEqual(h.HERDR.wait("nobody", 5), (True, "agent_not_found"))
        self.state.update(agents={self.impl: {"status": "idle"}}, wait_error="timeout")
        self.save_state()
        self.assertEqual(h.HERDR.wait(self.impl, 5), (False, ""))
        self.setenv("PATH", str(self.r.tmp))  # no herdr on PATH at all
        with self.assertRaises(h.Die) as cm:
            h.HERDR.call("agent", "get", "x")
        self.assertIn("herdr not found in PATH", cm.exception.msg)

    def test_launch_driver_uses_the_driver_tab(self):
        stamp = h.Lock.driver(self.r.wt).stamp
        self.state["touch_on_pane_run"] = str(stamp)
        self.save_state()
        msg = "two\nlines of feedback, " + "long " * 300
        pane = h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", msg, "wB:p2")
        self.assertEqual(pane, "w1:p2")
        self.assertEqual(self.calls("tab", "create")[0][6:], ["--label", "driver", "--no-focus"])
        run = self.calls("pane", "run")[0]
        self.assertEqual(run[:3], ["pane", "run", "w1:p2"])
        msg_file = h.Lock.driver(self.r.wt).msg
        typed = f"{shlex.quote(h.__file__)} drive --notify wB:p2 --msg-file {shlex.quote(str(msg_file))} -- feat"
        self.assertIn(typed, run[3])
        self.assertEqual(msg_file.read_text(), msg, "whatever its size or shape: never typed into the shell")
        self.assertLess(len(run[3]), 1024, "a typed line must fit the terminal's line input")
        self.assertTrue(stamp.exists(), "the launch is confirmed by the driver's start stamp")
        # the tab exists: herdr lists its Sidebar pane first; the driver must go to the shell pane
        sidebar = {"pane_id": "w1:p8", "tab_id": "w1:t5", "label": "Sidebar"}
        self.state.update(
            tabs=[{"tab_id": "w1:t5", "label": "driver"}], panes=[sidebar, {"pane_id": "w1:p7", "tab_id": "w1:t5"}]
        )
        self.save_state()
        self.assertEqual(h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "", ""), "w1:p7")
        self.assertEqual(len(self.calls("tab", "create")), 1, "an existing driver tab is reused")
        self.assertTrue(self.calls("pane", "run")[-1][3].endswith("drive -- feat"))
        self.assertEqual(self.calls("pane", "run")[-1][2], "w1:p7", "never the Sidebar pane")
        self.state["panes"] = [sidebar, {"pane_id": "w1:p9", "tab_id": "w1:t5", "agent": "claude"}]
        self.save_state()
        self.assertEqual(h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "", ""), "w1:p2")
        self.assertEqual(len(self.calls("tab", "create")), 2, "never typed into an agent's pane: a new tab")

    def test_launch_driver_reports_a_driver_that_did_not_start(self):
        with self.assertRaises(h.HerdrError) as ctx:
            h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "", "")
        self.assertEqual(ctx.exception.code, "driver_not_started")
        self.assertIn("herdr pane read w1:p2", ctx.exception.message)
        self.assertEqual(len(self.calls("pane", "run")), 1)

    def test_worktree_create_nests_under_the_callers_workspace(self):
        self.setenv("HERDR_WORKSPACE_ID", "w0")
        res = h.HERDR.create_worktree(self.r.root, "newf", "main")
        self.assertEqual(res["worktree"]["path"], str(self.r.tmp / "new-wt"))
        create = self.calls("worktree", "create")[0]
        self.assertEqual(
            create[2:4], ["--workspace", "w0"], "the caller's workspace is on the main checkout: nest under it"
        )
        self.assertEqual(create[4:], ["--branch", "newf", "--base", "main", "--label", "newf", "--no-focus"])
        self.setenv("HERDR_WORKSPACE_ID", None)
        h.HERDR.create_worktree(self.r.root, "other", "main")
        self.assertEqual(self.calls("worktree", "create")[1][2:4], ["--cwd", str(self.r.root)])
        self.assertEqual(h.HERDR.open_workspace(self.r.root, self.r.wt), "w1")
        closed = self.r.tmp / "closed"  # a real worktree whose herdr workspace is closed: it gets reopened
        self.r.git("worktree", "add", "-q", "-b", "closed", str(closed))
        self.assertEqual(h.HERDR.open_workspace(self.r.root, closed), "w9")
        self.assertEqual(
            self.calls("worktree", "open")[0][2:],
            ["--cwd", str(self.r.root), "--path", str(closed), "--no-focus"],
        )

    def test_sidebar_notify_and_remove_worktree(self):
        h.HERDR.sidebar(self.r.wt, "spec ready · needs you: approve")
        self.assertEqual(
            self.calls("workspace", "report-metadata"),
            [
                [
                    "workspace",
                    "report-metadata",
                    "w1",
                    "--source",
                    "harness",
                    "--token",
                    "harness=spec ready · needs you: approve",
                ]
            ],
        )
        self.state["agents"] = {"wB:p2": {"status": "idle"}}
        self.save_state()
        h.HERDR.notify("wB:p2", "harness: feat is done")
        self.assertEqual(self.calls("agent", "prompt")[-1], ["agent", "prompt", "wB:p2", "harness: feat is done"])
        _, out = self.output(h.HERDR.notify, "wB:p3", "harness: feat is done")
        self.assertEqual(out, "", "a plain shell in the pane: nobody to prompt, nothing to say")
        self.r.git("merge", "-q", "--no-edit", "feat")  # the branch must be merged for `git branch -d`
        problem, _ = self.output(h.remove_worktree, self.r.root, self.r.wt, "feat")
        self.assertEqual(problem, "")
        self.assertEqual(self.calls("worktree", "remove"), [["worktree", "remove", "--workspace", "w1", "--force"]])
        self.assertFalse(self.r.wt.exists())
        self.assertNotIn("feat", self.r.git("branch", "--format=%(refname:short)"))

    def test_doctor_sees_herdr_leftovers(self):
        main = str(self.r.root)
        self.state["workspaces"] = [
            {
                "workspace_id": "w0",
                "label": "main",
                "worktree": {"checkout_path": main, "is_linked_worktree": False, "repo_root": main},
            },
            {
                "workspace_id": "w7",
                "label": "dup",
                "worktree": {"checkout_path": main, "is_linked_worktree": False, "repo_root": main},
            },
            {
                "workspace_id": "w8",
                "label": "gone",
                "worktree": {"checkout_path": str(self.r.tmp / "gone"), "is_linked_worktree": True, "repo_root": main},
            },
        ]
        self.save_state()
        self.stub("Doctor", type("Doctor", (h.Doctor,), {"herdr_cfg": self.r.tmp / "herdr.toml"}))  # not ~/.config
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.herdr_state)
        self.assertEqual(doc.problems, ["workspace w8 'gone' points at a removed worktree"])
        self.assertIn("herdr workspace close w8", out)
        self.assertIn("sidebar does not show feature states yet", "\n".join(doc.notes))
        self.state["workspaces"] = []
        self.save_state()
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.herdr_state)
        self.assertEqual(doc.problems, [])
        self.assertIn("ok    herdr server reachable", out)


class DoctorChecks(Case):
    def test_an_empty_project_is_not_a_red_baseline(self):
        self.r.git("rm", "-r", "-q", "src", "tests")
        self.r.commit("only the template")
        self.assertEqual(h.Git(self.r.root).project_paths(), set())
        self.r.write_cfg(test_cmd="false", setup_cmd="touch setup-ran")
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertEqual((out, doc.problems), ("", []))
        self.assertIn("no project code yet", doc.notes[0])
        dst = self.r.tmp / "new-wt"
        dst.mkdir()
        _, out = self.output(h.WorktreeSetup(self.r.root, dst, self.r.cfg()).run)
        self.assertIn("setup_cmd: skipped (no project code yet", out)
        self.assertFalse((dst / "setup-ran").exists())
        (self.r.root / "main.go").write_text("package main\n")  # any file outside the template's is project code
        self.r.commit("first code")
        self.assertEqual(h.Git(self.r.root).project_paths(), {"main.go"})
        _, out = self.output(h.WorktreeSetup(self.r.root, dst, self.r.cfg()).run)
        self.assertIn("setup_cmd: ok", out)
        self.assertTrue((dst / "setup-ran").exists())
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertIn("FAIL  test_cmd green on the main checkout (false)", out)

    def test_project_section_placeholders(self):
        agents_md = self.r.root / "AGENTS.md"
        doc = h.Doctor(self.r.root)
        doc.project_section()
        self.assertEqual(doc.notes, [], "no AGENTS.md: nothing to say")
        unfilled = (
            "## 1. Project\n- What it is: <one paragraph: what this software does and for whom>\n"
            "- Stack and layout: <language, framework; where code and tests live>\n- Conventions: <style, naming>\n\n"
        )
        shipped = f"# AGENTS.md\n\n{unfilled}## 2. Two lanes\n- Trivial change: <typo>: just do it.\n"
        agents_md.write_text(shipped)
        doc.project_section()
        self.assertEqual(len(doc.notes), 1)
        self.assertIn("AGENTS.md §1 is not filled in yet", doc.notes[0])
        filled = (
            "## 1. Project\n<!-- Filled in by the orchestrator with the human before the first feature. -->\n"
            "- What it is: a CLI that renames photos by date\n- Stack and layout: Rust, Vec<T> everywhere\n"
            "- Conventions: rustfmt; errors as Result<T, E>\n\n"
        )
        agents_md.write_text(shipped.replace(unfilled, filled))
        doc = h.Doctor(self.r.root)
        doc.project_section()
        self.assertEqual(doc.notes, [], "filled in; the HTML comment, generics and later sections do not count")

    def test_baseline_runs_test_cmd_unless_skipped(self):
        self.r.write_cfg(test_cmd="echo boom; false")
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertIn("FAIL  test_cmd green on the main checkout (echo boom; false)", out)
        self.assertIn("boom", out)
        self.assertIn("blamed on the first feature's implementer", out)
        doc = h.Doctor(self.r.root, tests=False)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertEqual((out, doc.problems), ("", []))
        self.assertIn("test_cmd not run (--no-tests)", doc.notes)
        self.r.write_cfg(test_cmd="true")
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertIn("ok    test_cmd green on the main checkout (true)", out)

    def test_git_leftovers(self):
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.git_leftovers, "main")
        self.assertIn("ok    worktree", out)
        self.assertEqual(doc.problems, [])
        self.r.git("worktree", "add", "-q", "-b", "stray", str(self.r.tmp / "stray"))
        self.r.approve()
        self.r.git("worktree", "remove", "--force", str(self.r.wt))
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.git_leftovers, "main")
        self.assertIn("has no feature folder", "\n".join(doc.problems))
        self.assertIn("branch 'feat' has no worktree (its feature is not merged)", "\n".join(doc.problems))
        self.assertIn("herdr worktree open", out)
        self.assertIn("stale refs/harness/feat/approved", "\n".join(doc.problems))

    def test_models_probe_each_cli_and_model_once(self):
        bindir, log = self.r.tmp / "bin", self.r.tmp / "probes.log"
        bindir.mkdir()
        fake = (
            f'#!/bin/sh\necho "$(basename "$0") $*" >> {log}\n'
            'case "$*" in *bad*) echo "unknown model" >&2; exit 1;; esac\necho OK\n'
        )
        for cli in ("claude", "codex"):
            (bindir / cli).write_text(fake)
            (bindir / cli).chmod(0o755)
        self.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        self.r.write_cfg(
            agents={
                "spec_author": {"harness": "claude", "model": "", "effort": "extra", "permissions": "full"},
                "spec_reviewer": {"harness": "claude", "model": "", "effort": "extra", "permissions": "full"},
                "implementer": {"harness": "gemini", "model": "x/y", "permissions": "full"},
                "reviewer": {"harness": "codex", "model": "bad", "permissions": "guarded"},
            }
        )
        doc = h.Doctor(self.r.root, models=True)
        _, out = self.output(doc.models_check, self.r.cfg())
        self.assertIn("ok    spec_author: claude answers with model (default model)", out)
        self.assertIn("FAIL  reviewer: codex answers with model bad", out)
        self.assertIn("unknown model", out)
        self.assertIn("a wrong id (agents.reviewer.model), not logged in, or no credits", out)
        self.assertIn("implementer: gemini has no probe here, its model is not checked", doc.notes)
        probes = log.read_text().splitlines()
        self.assertEqual(len(probes), 2, "one probe per (CLI, model) pair")
        self.assertIn("claude -p --effort xhigh Reply with the word OK.", probes, "the CLI's default model, its effort")
        self.assertIn("codex exec -m bad --skip-git-repo-check Reply with the word OK.", probes)
        doc = h.Doctor(self.r.root)  # without --models nothing is probed
        doc.herdr_state = lambda: None
        self.stub("probe_model", lambda *a: self.fail("probed without --models"))
        self.output(doc.run)


class HerdrAppAdapter(Case):
    """The desktop app's adapter against a stand-in `hc`, and a stand-in `herdr-app` for what needs the window."""

    fake_herdr = True
    ACFG = {"harness": "claude", "model": "m", "effort": "high", "permissions": "full"}

    def setUp(self):
        super().setUp()
        bindir = self.r.tmp / "bin"
        for name, text in (("hc", FAKE_HC), ("herdr-app", self.fake_app(0))):
            (bindir / name).write_text(text)
            (bindir / name).chmod(0o755)
        self.bindir = bindir
        self.state = {"terminals": []}
        self.save_state()
        self.app = h.HerdrApp()
        self.stub("HERDR", self.app)
        self.space = self.app.space_id(self.r.wt)
        self.name = h.agent_name("implementer", "feat", self.r.root)

    def fake_app(self, code: int) -> str:
        return f'#!/bin/sh\necho "herdr-app $*" >> "$FAKE_HERDR_LOG.app"\nexit {code}\n'

    def window(self, code: int):
        (self.bindir / "herdr-app").write_text(self.fake_app(code))

    def lines(self) -> list:
        """What the stand-in `herdr-app` was asked."""
        log = Path(f"{self.log_path}.app")
        return log.read_text().splitlines() if log.exists() else []

    def role(self, role="implementer", text="go", timeout=300_000):
        return self.app.run_role(self.r.root, self.r.wt, role, "feat", text, timeout, agent(self.ACFG))

    def test_runtime_is_the_innermost_one(self):
        self.assertIs(type(h.pick_runtime({"HERDR_ENV": "1"})), h.Herdr)
        self.assertIs(type(h.pick_runtime({"HC_TERMINAL_ID": "t1"})), h.HerdrApp)
        self.assertIs(type(h.pick_runtime({"HERDR_ENV": "1", "HC_TERMINAL_ID": "t1"})), h.Herdr)  # TUI inside the app

    def test_outside_both_runtimes_the_installed_one(self):
        self.assertIs(type(h.pick_runtime({})), h.Herdr)  # both on PATH here
        (self.bindir / "herdr").unlink()
        self.setenv("PATH", str(self.bindir))
        self.assertIs(type(h.pick_runtime({})), h.HerdrApp)

    def test_a_role_runs_as_a_labelled_terminal_with_its_flags_and_the_prompt(self):
        self.assertEqual(self.role(), (True, ""))
        t = self.state_now()["terminals"][0]
        self.assertEqual(t["name"], self.name)
        self.assertEqual(t["argv"][0], "claude")
        self.assertIn("--dangerously-skip-permissions", t["argv"])
        self.assertIn(f"herdr.space={self.space}", t["labels"])
        run = self.calls("run")[0]
        for flag in ("--bg", "--no-parent", "--no-notify"):  # outlives the driver; nothing typed into the driver
            self.assertIn(flag, run)
        self.assertEqual(run[run.index("--cwd") + 1], str(self.r.wt))
        self.assertTrue(self.calls("send", "--wait"))

    def state_now(self) -> dict:
        return json.loads(self.state_path.read_text())

    def test_each_run_replaces_the_roles_previous_terminal(self):
        self.role()
        first = self.state_now()["terminals"][0]["address"]
        self.role()
        self.assertEqual(self.calls("kill"), [["kill", first]])
        self.assertEqual(self.calls("rm"), [["rm", first]])
        self.assertEqual(len(self.state_now()["terminals"]), 1)

    def test_a_dialog_on_a_terminal_with_no_status_is_answered(self):
        self.state.update(start_status="none", screen="Do you trust this folder?")
        self.save_state()
        self.assertEqual(self.role(), (True, ""))
        self.assertEqual(self.calls("keys"), [["keys", self.name, "down", "enter"]])

    def test_an_unknown_dialog_stops_as_blocked(self):
        self.state.update(start_status="blocked", screen="Pick a theme")
        self.save_state()
        self.assertEqual(self.role(), (True, "blocked at startup"))
        self.assertFalse(self.calls("send"))

    def test_no_status_at_all_is_stopped_for_the_human(self):
        self.state.update(start_status="none")
        self.save_state()
        clock = iter(range(0, 100_000, 10))
        self.stub("time", types.SimpleNamespace(sleep=lambda s: None, time=lambda: next(clock)))
        self.assertEqual(self.role(), (True, "? at startup"))

    def test_a_turn_still_running_is_not_settled(self):
        self.state["send_result"] = {"started": True, "wait": {"reason": "timeout", "status": "working"}}
        self.save_state()
        self.assertEqual(self.role(), (False, ""))
        self.assertEqual(self.calls("send", "--wait")[0][3], "300")  # hc takes seconds

    def test_a_turn_ending_blocked_is_reported(self):
        self.state["send_result"] = {"started": True, "wait": {"reason": "status", "status": "blocked"}}
        self.save_state()
        self.assertEqual(self.role(), (True, "agent_blocked"))

    def test_a_prompt_that_did_not_start_gets_enter_once_when_idle(self):
        self.state["send_result"] = {"started": False}
        self.save_state()
        self.assertEqual(self.role(), (True, ""))
        self.assertEqual(self.calls("keys"), [["keys", self.name, "enter"]])

    def test_state_and_wait(self):
        self.role()
        self.assertEqual(self.app.state(self.name), "idle")
        self.assertEqual(self.app.state("nobody"), "gone")
        self.assertEqual(self.app.wait("nobody", 5000), (True, "not_found"))
        self.assertEqual(self.app.wait(self.name, 5000), (True, ""))
        self.state_set(wait_result={"reason": "timeout", "status": "working"})
        self.assertEqual(self.app.wait(self.name, 5000), (False, ""))
        self.state_set(wait_result={"reason": "exit", "exit_code": 1})  # hc passes on the exit code: not an error
        self.assertEqual(self.app.wait(self.name, 5000), (True, ""))
        self.app.call("kill", self.name)
        self.assertEqual(self.app.state(self.name), "gone")

    def state_set(self, **kw):
        state = self.state_now()
        state.update(kw)
        self.state_path.write_text(json.dumps(state))

    def test_screen_reads_the_visible_part_with_zero_lines(self):
        self.role()
        self.state_set(screen="hello")
        self.assertIn("hello", self.app.screen(self.name, source="visible"))
        self.assertEqual(self.calls("read")[-1][-2:], ["--lines", "0"])

    def test_the_worktree_goes_where_the_apps_own_mod_puts_it(self):
        home = self.r.tmp / "home"
        self.setenv("HOME", str(home))
        res = self.app.create_worktree(self.r.root, "newf", "main")
        wt = home / ".herdr" / "worktrees" / self.r.root.name / "newf"
        self.assertEqual(res["worktree"]["path"], str(wt))
        self.assertEqual(res["workspace"]["workspace_id"], self.app.space_id(wt))
        self.assertEqual(h.Git(wt).branch(), "newf")

    def test_a_workspace_is_open_while_a_terminal_carries_its_label(self):
        self.assertIsNone(self.app.workspace_of(self.r.wt))
        self.role()
        self.assertEqual(self.app.workspace_of(self.r.wt), self.space)

    def test_removing_a_worktree_ends_its_terminals_and_leaves_the_rest_to_git(self):
        self.role()
        self.assertFalse(self.app.remove_worktree(self.r.wt))
        self.assertEqual(self.state_now()["terminals"], [])

    def test_the_driver_runs_in_a_shell_of_the_driver_tab(self):
        self.state["start_status"] = "none"
        self.save_state()
        self.stub("subprocess", subprocess)
        lock = h.Lock.driver(self.r.wt)
        before_stamp = self.r.tmp / "stamp"
        pane = self.start_driver(lock, before_stamp)
        self.assertEqual(pane, "t1")
        t = self.state_now()["terminals"][0]
        self.assertEqual(t["argv"], [])  # a shell: it stays for the human once the driver stops
        sent = self.calls("send")[0]
        self.assertEqual(sent[1], "t1")
        self.assertIn("drive --notify t9 -- feat", sent[2])
        self.assertEqual(self.app.driver_tab_pane(self.space), "t1")  # the next `drive --tab` reuses it

    def start_driver(self, lock, _stamp):
        real = self.app.run_in_pane

        def run_and_stamp(pane, command):
            real(pane, command)
            lock.write_stamp()  # what the driver does first thing

        self.stub_herdr("run_in_pane", run_and_stamp)
        return self.app.launch_driver(self.r.root, self.r.wt, "feat", "", "t9")

    def test_notify_prompts_an_agent_but_never_a_shell(self):
        self.role()
        self.app.notify(self.name, "harness: feat is done")
        self.assertEqual(self.calls("send")[-1], ["send", self.name, "harness: feat is done"])
        sent = len(self.calls("send"))
        self.app.call("run", "--name", "shell")  # no argv: a shell reports no status
        self.app.notify("shell", "harness: feat is done")
        self.assertEqual(len(self.calls("send")), sent)
        self.app.notify("nobody", "harness: feat is done")  # gone: nobody to tell, no noise

    def test_notify_reports_an_agent_that_refuses(self):
        self.state.update(start_status="blocked", terminals=[])
        self.save_state()
        self.app.call("run", "--name", "busy", "--", "claude")
        _, out = self.output(self.app.notify, "busy", "harness: feat is done")
        self.assertIn("could not notify terminal busy: blocked", out)

    def test_the_spaces_name_carries_the_state_while_a_window_is_open(self):
        self.role()
        self.app.sidebar(self.r.wt, "spec ready · needs you: approve")
        name = f"{self.r.wt.name} · spec ready · needs you: approve"
        self.assertEqual(self.lines()[-1], f"herdr-app do space.rename --id {self.space} --name {name}")

    def test_no_window_is_no_problem(self):
        self.window(2)
        self.assertEqual(self.role(), (True, ""))
        self.app.sidebar(self.r.wt, "x")
        self.assertEqual(len([ln for ln in self.lines() if "tab.rename" in ln]), 1)  # no retry: nothing to wait for

    def test_no_herdr_app_binary_is_no_problem(self):
        (self.bindir / "herdr-app").unlink()
        self.setenv("PATH", str(self.bindir) + ":/usr/bin:/bin")
        self.assertEqual(self.role(), (True, ""))
        self.app.sidebar(self.r.wt, "x")

    def test_the_tab_name_waits_for_the_window_to_adopt_the_terminal(self):
        self.window(1)
        self.role()
        self.assertEqual(len([ln for ln in self.lines() if "tab.rename" in ln]), 4)

    def test_doctor_names_the_runtime_and_asks_for_no_sidebar_config(self):
        _, out = self.output(h.Doctor(self.r.root, tests=False).run)
        self.assertIn("hc in PATH (runtime: herdr app)", out)
        self.assertIn("herdr app server reachable", out)
        self.assertNotIn("sidebar", out)

    def test_doctor_says_when_the_core_does_not_answer(self):
        self.state["server_down"] = True
        self.save_state()
        doc = h.Doctor(self.r.root, tests=False)
        _, out = self.output(doc.run)
        self.assertIn("FAIL  herdr app server reachable (no_core", out)

    def test_the_hints_name_this_runtime(self):
        self.assertEqual(self.app.read_hint("a"), "hc read a --lines 120")
        self.assertEqual(self.app.pane_hint("t1"), "hc read t1 --lines 40")
        self.assertIn("git worktree add", self.app.reopen_hint(self.r.root, "b"))

    def test_merge_drops_hcs_process_scope(self):
        """`hc kill` ends every process carrying the terminal's HC_SCOPE: `merge` must outlive the terminal it ends."""
        self.setenv("HC_SCOPE", "scope")
        seen = {}

        def popen(cmd, **kw):
            seen.update(kw)
            return None

        self.stub("subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": popen}))
        driver = h.Driver.__new__(h.Driver)
        driver.wt, driver.slug, driver.notify_pane, driver.lock = self.r.wt, "feat", "", h.Lock.driver(self.r.wt)
        driver.log = lambda *a: None
        with self.assertRaises(h._Stop):
            driver.spawn_merge()
        self.assertNotIn("HC_SCOPE", seen["env"])
        self.assertTrue(seen["start_new_session"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *sys.argv[1:]])
