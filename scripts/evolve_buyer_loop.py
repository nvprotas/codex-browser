from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_EVAL = 3
EXIT_GIT = 4
EXIT_ARTIFACT = 5

QUALITY_CHECKS = ("outcome_ok", "safety_ok", "payment_boundary_ok", "evidence_ok")
DEFAULT_ALLOWED_PATHS = ("buyer/**", "eval/cases/**", "eval/evolution/placeholders/**", "docs/**")
DEFAULT_PATCH_COMMAND_TIMEOUT_SEC = 3600
FORBIDDEN_PATTERNS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".git/**",
    ".tmp/**",
    ".auth/**",
    "auth/**",
    "**/auth/**",
    "profiles/**",
    "**/profiles/**",
    "browser/profiles/**",
    "**/browser/profiles/**",
    "eval/auth-profiles/**",
    "eval/runs/**",
    "**/.auth/**",
    "storageState*.json",
    "storage-state*.json",
    "**/storageState*.json",
    "**/storage-state*.json",
)
SECRET_KEY_RE = re.compile(r"(token|cookie|authorization|password|secret|storageState|orderId|paymentUrl)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(Authorization:\s*\S+\s+[^\s]+|Cookie:\s*[^\n\r]+|Set-Cookie:\s*[^\n\r]+|Bearer\s+[A-Za-z0-9._~+/=-]+|eyJ[A-Za-z0-9._~+/=-]+|(?:password|token|secret|orderId)[=:][^\s&]+|https?://\S*(?:pay|payment|sber)\S*)",
    re.I,
)


class EvolveError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_INTERNAL) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class CommandResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


class Deps:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def write_stdout(self, text: str) -> None:
        sys.stdout.write(text)

    def write_stderr(self, text: str) -> None:
        sys.stderr.write(text)

    def request_json(self, method: str, url: str, payload: Any | None, timeout_sec: int) -> dict[str, Any]:
        data = None
        headers = {"accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EvolveError(f"eval request failed: {exc}", EXIT_EVAL) from exc
        try:
            value = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise EvolveError(f"eval response is not JSON: {url}", EXIT_EVAL) from exc
        if not isinstance(value, dict):
            raise EvolveError(f"eval response is not an object: {url}", EXIT_EVAL)
        return value

    def run_process(
        self,
        args: list[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        timeout_sec: int = 120,
    ) -> CommandResult:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                env=merged_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(args=args, cwd=cwd, returncode=124, stdout=exc.stdout or "", stderr=f"timeout after {timeout_sec}s")
        except OSError as exc:
            return CommandResult(args=args, cwd=cwd, returncode=127, stdout="", stderr=str(exc))
        return CommandResult(
            args=args,
            cwd=cwd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class EvalClient:
    def __init__(
        self,
        base_url: str,
        *,
        deps: Deps,
        timeout_sec: int,
        poll_sec: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.deps = deps
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _request(self, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
        return self.deps.request_json(method, self._url(path), payload, self.timeout_sec)

    def healthcheck(self) -> None:
        payload = self._request("GET", "/healthz")
        if payload.get("status") not in {"ok", "healthy"}:
            raise EvolveError("eval healthcheck failed", EXIT_EVAL)

    def list_cases(self) -> dict[str, Any]:
        payload = self._request("GET", "/cases")
        if not isinstance(payload.get("cases"), list):
            raise EvolveError("eval /cases response missing cases", EXIT_EVAL)
        return payload

    def create_run(self, case_ids: list[str]) -> str:
        payload = self._request("POST", "/runs", {"case_ids": case_ids})
        run_id = payload.get("eval_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise EvolveError("eval /runs response missing eval_run_id", EXIT_EVAL)
        return run_id

    def wait_run_terminal(self, run_id: str) -> dict[str, Any]:
        last_payload: dict[str, Any] | None = None
        for _ in range(_max_poll_attempts(self.timeout_sec, self.poll_sec)):
            payload = self._request("GET", f"/runs/{run_id}")
            last_payload = payload
            if _needs_operator(payload):
                return {**payload, "_needs_operator": True}
            run = _dict(payload.get("run"))
            status = str(run.get("status") or "")
            if status in {"finished", "failed", "canceled"}:
                return payload
            self.deps.sleep(self.poll_sec)
        raise EvolveError(f"eval run timed out: {run_id}; last={last_payload}", EXIT_EVAL)

    def start_judge(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/runs/{run_id}/judge?async=1")

    def wait_judged(self, run_id: str, case_ids: list[str]) -> dict[str, Any]:
        selected = set(case_ids)
        last_payload: dict[str, Any] | None = None
        for _ in range(_max_poll_attempts(self.timeout_sec, self.poll_sec)):
            payload = self._request("GET", f"/runs/{run_id}")
            last_payload = payload
            if _needs_operator(payload):
                return {**payload, "_needs_operator": True}
            run_status = str(_dict(payload.get("run")).get("status") or "")
            if run_status in {"failed", "canceled"}:
                return payload
            evaluations = _evaluations_by_case(payload)
            if selected.issubset(evaluations) and all(
                _is_complete_judged_evaluation(evaluations[case_id])
                for case_id in selected
            ):
                return payload
            if any(
                str(evaluations[case_id].get("status")) in {"judge_failed", "judge_skipped"}
                for case_id in selected
                if case_id in evaluations
            ):
                return payload
            if _has_terminal_case_failure(payload):
                return payload
            self.deps.sleep(self.poll_sec)
        raise EvolveError(f"eval judge timed out: {run_id}; last={last_payload}", EXIT_EVAL)

    def run_eval_and_judge(self, case_ids: list[str]) -> dict[str, Any]:
        run_id = self.create_run(case_ids)
        return self.resume_run_and_judge(run_id, case_ids)

    def resume_run_and_judge(self, run_id: str, case_ids: list[str]) -> dict[str, Any]:
        terminal = self.wait_run_terminal(run_id)
        if terminal.get("_needs_operator"):
            return terminal
        run_status = _dict(terminal.get("run")).get("status")
        if run_status in {"failed", "canceled"}:
            return terminal
        self.start_judge(run_id)
        return self.wait_judged(run_id, case_ids)


class GitRunner:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    def run(self, args: list[str], cwd: Path, timeout_sec: int = 120) -> CommandResult:
        if args[:2] == ["git", "push"] or args[:3] == ["git", "reset", "--hard"]:
            raise EvolveError(f"disallowed git command: {args}", EXIT_GIT)
        if args[:3] == ["git", "clean", "-fdx"] or args == ["git", "add", "."]:
            raise EvolveError(f"disallowed git command: {args}", EXIT_GIT)
        result = self.deps.run_process(args, cwd, timeout_sec=timeout_sec)
        if result.returncode != 0:
            raise EvolveError(f"command failed: {' '.join(args)}: {result.stderr}", EXIT_GIT)
        return result


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    deps = deps or Deps()
    raw_argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        try:
            args = parser.parse_args(raw_argv)
        except SystemExit as exc:
            exit_code = int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
            _emit_json_if_requested(
                deps,
                raw_argv,
                {"status": "error", "exit_code": exit_code, "error": "argument parsing failed"},
            )
            return exit_code
        if not hasattr(args, "func"):
            parser.print_help()
            return EXIT_USAGE
        return int(args.func(args, deps))
    except EvolveError as exc:
        _emit_json_if_requested(deps, raw_argv, {"status": "error", "exit_code": exc.exit_code, "error": str(exc)})
        return exc.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local buyer evolve loop.")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor")
    _add_common(doctor)
    doctor.add_argument("--eval-base-url", required=True)
    doctor.add_argument("--candidate-eval-base-url")
    doctor.add_argument("--patch-mode", choices=["placeholder", "external-command"], default="placeholder")
    doctor.add_argument("--patch-command")
    doctor.add_argument("--base-ref", default="HEAD")
    doctor.set_defaults(func=run_doctor)

    run = subparsers.add_parser("run")
    _add_common(run)
    run.add_argument("--eval-base-url", required=True)
    run.add_argument("--candidate-eval-base-url")
    run.add_argument("--patch-mode", choices=["placeholder", "external-command"], default="placeholder")
    run.add_argument("--patch-command")
    run.add_argument("--candidate-prepare-command")
    run.add_argument("--skip-candidate-eval", action="store_true")
    run.add_argument("--worktrees-dir")
    run.add_argument("--base-ref", default="HEAD")
    run.add_argument("--branch-prefix", default="evolve")
    run.add_argument("--allowed-path", action="append", default=[])
    run.add_argument("--keep-worktree", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--repeats-per-case", type=int, default=1)
    run.set_defaults(func=run_cycle)

    cont = subparsers.add_parser("continue")
    _add_common(cont, require_case=False)
    cont.set_defaults(func=continue_cycle)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline-run-json", required=True)
    compare.add_argument("--candidate-run-json", required=True)
    compare.add_argument("--cases-json", required=True)
    compare.add_argument("--json", action="store_true")
    compare.add_argument("--quiet", action="store_true")
    compare.add_argument("--fail-on-status", default="")
    compare.set_defaults(func=compare_saved_runs)
    return parser


def _add_common(parser: argparse.ArgumentParser, *, require_case: bool = True) -> None:
    parser.add_argument("--repo", default=".")
    parser.add_argument("--reports-dir", default=".tmp/evolve")
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--case-id", action="append", required=require_case, default=[])
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--poll-sec", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--fail-on-status", default="")


def _eval_client(args: argparse.Namespace, base_url: str, deps: Deps) -> EvalClient:
    return EvalClient(base_url, deps=deps, timeout_sec=args.timeout_sec, poll_sec=args.poll_sec)


def run_doctor(args: argparse.Namespace, deps: Deps) -> int:
    repo = Path(args.repo).resolve()
    git = GitRunner(deps)
    git.run(["git", "rev-parse", "--show-toplevel"], repo)
    client = _eval_client(args, args.eval_base_url, deps)
    client.healthcheck()
    cases = client.list_cases()
    _require_cases(cases, args.case_id)
    if args.candidate_eval_base_url:
        if args.candidate_eval_base_url.rstrip("/") == args.eval_base_url.rstrip("/"):
            raise EvolveError("baseline and candidate eval URLs must differ", EXIT_USAGE)
        _eval_client(args, args.candidate_eval_base_url, deps).healthcheck()
    if args.patch_mode == "external-command" and not args.patch_command:
        raise EvolveError("--patch-command is required for external-command mode", EXIT_USAGE)
    _emit_result(deps, args, {"status": "ok", "command": "doctor", "case_ids": args.case_id})
    return EXIT_OK


def run_cycle(args: argparse.Namespace, deps: Deps) -> int:
    _validate_run_config(args)
    repo = Path(args.repo).resolve()
    reports_dir = Path(args.reports_dir).resolve()
    cycle_id = args.cycle_id or _cycle_id(deps.now())
    cycle_dir = reports_dir / "cycles" / cycle_id
    baseline_dir = cycle_dir / "baseline"
    candidate_root = cycle_dir / "candidates"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    candidate_root.mkdir(parents=True, exist_ok=True)

    write_json(cycle_dir / "cycle.json", {
        "schema_version": "buyer-evolve-cycle-v1",
        "cycle_id": cycle_id,
        "case_ids": args.case_id,
        "repo": str(repo),
        "reports_dir": str(reports_dir),
        "eval_base_url": args.eval_base_url,
        "candidate_eval_base_url": args.candidate_eval_base_url,
        "skip_candidate_eval": args.skip_candidate_eval,
        "patch_mode": args.patch_mode,
        "patch_command": args.patch_command,
        "candidate_prepare_command": args.candidate_prepare_command,
        "worktrees_dir": args.worktrees_dir,
        "base_ref": args.base_ref,
        "branch_prefix": args.branch_prefix,
        "allowed_paths": _allowed_paths(args),
        "keep_worktree": args.keep_worktree,
        "repeats_per_case": args.repeats_per_case,
        "timeout_sec": args.timeout_sec,
        "poll_sec": args.poll_sec,
    })
    _write_latest(reports_dir, cycle_id, "running", cycle_dir)
    try:
        return _run_cycle_after_latest(args, deps, repo, reports_dir, cycle_id, cycle_dir, baseline_dir)
    except EvolveError as exc:
        write_json(cycle_dir / "error.json", {"exit_code": exc.exit_code, "error": str(exc)})
        _write_latest(reports_dir, cycle_id, "error", cycle_dir)
        raise


def _run_cycle_after_latest(
    args: argparse.Namespace,
    deps: Deps,
    repo: Path,
    reports_dir: Path,
    cycle_id: str,
    cycle_dir: Path,
    baseline_dir: Path,
) -> int:

    baseline_client = _eval_client(args, args.eval_base_url, deps)
    baseline_client.healthcheck()
    cases = baseline_client.list_cases()
    _require_cases(cases, args.case_id)
    write_json(baseline_dir / "cases.json", cases)
    write_json(baseline_dir / "eval-request.json", {"case_ids": args.case_id, "eval_base_url": args.eval_base_url})
    baseline_result = baseline_client.run_eval_and_judge(args.case_id)
    write_json(baseline_dir / "eval-result.json", baseline_result)

    if baseline_result.get("_needs_operator"):
        return _finish_operator_action(
            deps,
            args,
            reports_dir,
            cycle_id,
            cycle_dir,
            baseline_result,
            args.eval_base_url,
            phase="baseline",
        )

    return _continue_after_baseline(deps, args, reports_dir, cycle_id, cycle_dir, baseline_result, cases)


def _continue_after_baseline(
    deps: Deps,
    args: argparse.Namespace,
    reports_dir: Path,
    cycle_id: str,
    cycle_dir: Path,
    baseline_result: dict[str, Any],
    cases: dict[str, Any],
) -> int:
    repo = Path(args.repo).resolve()
    candidate_root = cycle_dir / "candidates"
    baseline_summary = summarize_run(baseline_result, args.case_id)
    if not _summary_fully_judged(baseline_summary):
        report = compute_delta_report(baseline_result, None, cases, args.case_id)
        report["verdict"] = {"status": "inconclusive", "reason": "baseline_unavailable"}
        return _finish_report(deps, args, reports_dir, cycle_id, cycle_dir, None, report)

    branch = create_candidate(args, deps, repo, cycle_id, baseline_summary, cases, candidate_root)
    if args.skip_candidate_eval:
        report = compute_delta_report(baseline_result, None, cases, args.case_id)
        return _finish_report(deps, args, reports_dir, cycle_id, cycle_dir, branch, report)

    if args.candidate_prepare_command:
        prepare_result = run_candidate_prepare(
            args.candidate_prepare_command,
            deps,
            branch,
            args.candidate_eval_base_url,
        )
        (branch.path / "candidate-prepare.log").write_text(
            redact_text(prepare_result.stdout + prepare_result.stderr),
            encoding="utf-8",
        )

    candidate_client = _eval_client(args, args.candidate_eval_base_url, deps)
    candidate_client.healthcheck()
    candidate_cases = candidate_client.list_cases()
    if case_fingerprints(cases, args.case_id) != case_fingerprints(candidate_cases, args.case_id):
        report = compute_delta_report(baseline_result, None, cases, args.case_id)
        report["delta_status"] = "not_comparable"
        report["verdict"] = {"status": "inconclusive", "reason": "case_fingerprint_mismatch"}
        return _finish_report(deps, args, reports_dir, cycle_id, cycle_dir, branch, report)

    write_json(branch.path / "candidate-eval-request.json", {
        "case_ids": args.case_id,
        "eval_base_url": args.candidate_eval_base_url,
    })
    candidate_result = candidate_client.run_eval_and_judge(args.case_id)
    write_json(branch.path / "candidate-eval-result.json", candidate_result)
    if candidate_result.get("_needs_operator"):
        return _finish_operator_action(
            deps,
            args,
            reports_dir,
            cycle_id,
            cycle_dir,
            candidate_result,
            args.candidate_eval_base_url,
            phase="candidate",
            branch=branch,
        )
    report = compute_delta_report(baseline_result, candidate_result, cases, args.case_id)
    return _finish_report(deps, args, reports_dir, cycle_id, cycle_dir, branch, report)


@dataclass
class CandidateBranch:
    cycle_id: str
    candidate_id: str
    ref: str
    sha: str
    worktree_path: Path
    path: Path
    patch_slug: str


def create_candidate(
    args: argparse.Namespace,
    deps: Deps,
    repo: Path,
    cycle_id: str,
    baseline_summary: dict[str, Any],
    cases: dict[str, Any],
    candidate_root: Path,
) -> CandidateBranch:
    git = GitRunner(deps)
    repo_root = Path(git.run(["git", "rev-parse", "--show-toplevel"], repo).stdout.strip() or repo).resolve()
    base_sha = git.run(["git", "rev-parse", args.base_ref], repo_root).stdout.strip() or "base-sha"
    initial_slug = "placeholder" if args.patch_mode == "placeholder" else "external"
    candidate_id = f"cand-001-{initial_slug}"
    timestamp = _timestamp(deps.now())
    branch_ref = f"refs/heads/{args.branch_prefix}/cand-{timestamp}-001-{initial_slug}"
    branch_name = branch_ref.removeprefix("refs/heads/")
    worktrees_dir = Path(args.worktrees_dir).resolve() if args.worktrees_dir else repo_root.parent / "evolve-worktrees"
    worktree_path = worktrees_dir / f"{cycle_id}-{candidate_id}"
    candidate_dir = candidate_root / candidate_id
    candidate_dir.mkdir(parents=True, exist_ok=True)
    worktree_path.mkdir(parents=True, exist_ok=True)

    git.run(["git", "check-ref-format", "--branch", branch_name], repo_root)
    git.run(["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_sha], repo_root)

    patch_request = {
        "schema_version": "buyer-evolve-patch-request-v1",
        "cycle_id": cycle_id,
        "candidate_id": candidate_id,
        "candidate_worktree": str(worktree_path),
        "selected_case_ids": args.case_id,
        "baseline_summary": baseline_summary,
        "judge_recommendations": _collect_recommendations(baseline_summary),
        "allowed_paths": _allowed_paths(args),
        "output_manifest_path": str(candidate_dir / "patch-manifest.json"),
    }
    write_json(candidate_dir / "patch-request.json", patch_request)

    if args.patch_mode == "placeholder":
        placeholder_path = worktree_path / "eval" / "evolution" / "placeholders" / f"{candidate_id}.json"
        placeholder_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(placeholder_path, {"candidate_id": candidate_id, "cycle_id": cycle_id, "base_sha": base_sha})
        manifest = {
            "schema_version": "buyer-evolve-patch-manifest-v1",
            "patch_slug": "placeholder",
            "patch_kind": "placeholder",
            "touched_paths": [str(placeholder_path.relative_to(worktree_path))],
            "rationale": "Smoke branch/worktree/commit mechanics.",
            "expected_improvement": "none",
        }
        write_json(candidate_dir / "patch-manifest.json", manifest)
    else:
        if not args.patch_command:
            raise EvolveError("--patch-command is required for external-command mode", EXIT_USAGE)
        env = {
            "EVOLVE_CYCLE_ID": cycle_id,
            "EVOLVE_CANDIDATE_ID": candidate_id,
            "EVOLVE_CANDIDATE_WORKTREE": str(worktree_path),
            "EVOLVE_PATCH_REQUEST": str(candidate_dir / "patch-request.json"),
            "EVOLVE_PATCH_MANIFEST": str(candidate_dir / "patch-manifest.json"),
            "EVOLVE_ALLOWED_PATHS": os.pathsep.join(_allowed_paths(args)),
        }
        result = deps.run_process(
            shlex.split(args.patch_command),
            worktree_path,
            env=env,
            timeout_sec=DEFAULT_PATCH_COMMAND_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            raise EvolveError(f"patch command failed: {result.stderr}", EXIT_GIT)
        manifest = read_json(candidate_dir / "patch-manifest.json")

    committed_files = _diff_files(
        git.run(["git", "diff", "--name-only", f"{base_sha}..HEAD"], worktree_path).stdout
    )
    ls_files = _diff_files(
        git.run(
            ["git", "ls-files", "--modified", "--others", "--deleted", "--exclude-standard"],
            worktree_path,
        ).stdout
    )
    cached_files = _diff_files(git.run(["git", "diff", "--cached", "--name-only"], worktree_path).stdout)
    diff_files = sorted({*committed_files, *ls_files, *cached_files, *_manifest_touched_paths(manifest)})
    if not diff_files:
        raise EvolveError("patch produced no diff", EXIT_GIT)
    diff_files = _validate_diff_files(diff_files, _allowed_paths(args))
    if committed_files:
        if ls_files or cached_files:
            raise EvolveError("patch command committed changes but left uncommitted diff", EXIT_GIT)
        git.run(["git", "diff", "--check", f"{base_sha}..HEAD"], worktree_path)
        diffstat = git.run(["git", "diff", "--stat", f"{base_sha}..HEAD"], worktree_path).stdout
        diff_text = git.run(["git", "diff", f"{base_sha}..HEAD"], worktree_path).stdout
    else:
        git.run(["git", "diff", "--check"], worktree_path)
        git.run(["git", "add", *diff_files], worktree_path)
        git.run(["git", "diff", "--cached", "--check"], worktree_path)
        diffstat = git.run(["git", "diff", "--cached", "--stat"], worktree_path).stdout
        diff_text = git.run(["git", "diff", "--cached"], worktree_path).stdout
    write_json(candidate_dir / "patch-diffstat.json", {"files": diff_files, "stat": diffstat})
    (candidate_dir / "candidate.diff").write_text(redact_text(diff_text), encoding="utf-8")
    if not committed_files:
        git.run(["git", "commit", "-m", f"evolve buyer: {manifest.get('patch_slug', initial_slug)}"], worktree_path)
    candidate_sha = git.run(["git", "rev-parse", "HEAD"], worktree_path).stdout.strip() or "candidate-sha"
    candidate = CandidateBranch(
        cycle_id=cycle_id,
        candidate_id=candidate_id,
        ref=branch_ref,
        sha=candidate_sha,
        worktree_path=worktree_path,
        path=candidate_dir,
        patch_slug=str(manifest.get("patch_slug") or initial_slug),
    )
    write_json(candidate_dir / "candidate.json", {
        "schema_version": "buyer-evolve-candidate-v1",
        "candidate_id": candidate.candidate_id,
        "candidate_ref": candidate.ref,
        "candidate_sha": candidate.sha,
        "worktree_path": str(candidate.worktree_path),
        "worktree_status": "kept",
        "restore_worktree_command": f"git worktree add {candidate.worktree_path} {candidate.sha}",
        "patch_mode": args.patch_mode,
        "patch_slug": candidate.patch_slug,
    })
    return candidate


def run_candidate_prepare(command: str, deps: Deps, branch: CandidateBranch, candidate_eval_base_url: str) -> CommandResult:
    env = {
        "EVOLVE_CYCLE_ID": branch.cycle_id,
        "EVOLVE_CANDIDATE_ID": branch.candidate_id,
        "EVOLVE_CANDIDATE_WORKTREE": str(branch.worktree_path),
        "EVOLVE_CANDIDATE_REF": branch.ref,
        "EVOLVE_CANDIDATE_SHA": branch.sha,
        "EVOLVE_CANDIDATE_EVAL_BASE_URL": candidate_eval_base_url,
        "EVOLVE_REPORTS_DIR": str(branch.path.parent.parent.parent),
    }
    result = deps.run_process(
        shlex.split(command),
        branch.worktree_path,
        env=env,
        timeout_sec=DEFAULT_PATCH_COMMAND_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise EvolveError(f"candidate prepare failed: {result.stderr}", EXIT_GIT)
    return result


def compare_saved_runs(args: argparse.Namespace, deps: Deps) -> int:
    baseline = read_json(Path(args.baseline_run_json))
    candidate = read_json(Path(args.candidate_run_json))
    cases = read_json(Path(args.cases_json))
    report = compute_delta_report(baseline, candidate, cases)
    _emit_result(deps, args, {
        "status": "ok",
        "verdict_status": report["verdict"]["status"],
        "delta_status": report["delta_status"],
        "report": report,
    })
    return _exit_for_status(args, str(report["verdict"]["status"]))


def continue_cycle(args: argparse.Namespace, deps: Deps) -> int:
    reports_dir = Path(args.reports_dir).resolve()
    latest: dict[str, Any] = {}
    if args.cycle_id in {None, "latest"}:
        latest = read_json(reports_dir / "latest.json")
        cycle_id = latest["cycle_id"]
    else:
        cycle_id = args.cycle_id
    cycle_dir = reports_dir / "cycles" / cycle_id
    cycle_path = cycle_dir / "cycle.json"
    if cycle_path.is_file():
        cycle = read_json(cycle_path)
        resumed_args = _args_from_cycle(args, cycle)
        cases_path = cycle_dir / "baseline" / "cases.json"
        baseline_path = cycle_dir / "baseline" / "eval-result.json"
        if baseline_path.is_file() and cases_path.is_file():
            cases = read_json(cases_path)
            baseline = read_json(baseline_path)
            candidate_resume = _candidate_resume_target(cycle_dir)
            if candidate_resume is not None:
                candidate_path, branch, candidate = candidate_resume
                if not resumed_args.candidate_eval_base_url:
                    raise EvolveError("candidate eval URL is required to continue candidate handoff", EXIT_USAGE)
                client = _eval_client(resumed_args, resumed_args.candidate_eval_base_url, deps)
                run_id = _run_id(candidate)
                if run_id:
                    candidate = client.resume_run_and_judge(run_id, resumed_args.case_id)
                    if candidate.get("_needs_operator"):
                        return _finish_operator_action(
                            deps,
                            resumed_args,
                            reports_dir,
                            cycle_id,
                            cycle_dir,
                            candidate,
                            resumed_args.candidate_eval_base_url,
                            phase="candidate",
                            branch=branch,
                        )
                    write_json(candidate_path, candidate)
                    report = compute_delta_report(baseline, candidate, cases, resumed_args.case_id)
                    return _finish_report(deps, resumed_args, reports_dir, cycle_id, cycle_dir, branch, report)
            if baseline.get("_needs_operator"):
                client = _eval_client(resumed_args, resumed_args.eval_base_url, deps)
                run_id = _run_id(baseline)
                if run_id:
                    baseline = client.resume_run_and_judge(run_id, resumed_args.case_id)
                    if baseline.get("_needs_operator"):
                        return _finish_operator_action(
                            deps,
                            resumed_args,
                            reports_dir,
                            cycle_id,
                            cycle_dir,
                            baseline,
                            resumed_args.eval_base_url,
                            phase="baseline",
                        )
                    if _run_failed_or_canceled(baseline):
                        write_json(baseline_path, baseline)
                        report = compute_delta_report(baseline, None, cases, resumed_args.case_id)
                        report["verdict"] = {"status": "inconclusive", "reason": "baseline_unavailable"}
                        return _finish_report(deps, resumed_args, reports_dir, cycle_id, cycle_dir, None, report)
                    write_json(baseline_path, baseline)
                    return _continue_after_baseline(
                        deps,
                        resumed_args,
                        reports_dir,
                        cycle_id,
                        cycle_dir,
                        baseline,
                        cases,
                    )
    _emit_result(deps, args, {
        "status": "ok",
        "command": "continue",
        "cycle_id": cycle_id,
        "verdict_status": latest.get("status", "inconclusive"),
    })
    return EXIT_OK


def compute_delta_report(
    baseline: dict[str, Any],
    candidate: dict[str, Any] | None,
    cases: dict[str, Any],
    selected_case_ids: list[str] | None = None,
) -> dict[str, Any]:
    case_ids = selected_case_ids or [case["eval_case_id"] for case in _cases(cases)]
    baseline_summary = summarize_run(baseline, case_ids)
    if candidate is None:
        return {
            "schema_version": "buyer-evolution-delta-v1",
            "delta_status": "not_comparable",
            "verdict": {"status": "inconclusive", "reason": "candidate_eval_endpoint_absent"},
            "promotion": {"eligible": False, "reason": "auto-promotion disabled in MVP-A"},
            "eval_set": {"case_fingerprint_hash": fingerprint_hash(cases, case_ids), "case_ids": case_ids},
            "baseline": {"run_ids": [_run_id(baseline)], "summary": baseline_summary},
            "candidate": None,
            "delta": {},
            "per_case_gates": [],
            "confidence": {"status": "exploratory", "method": "single_repeat", "sample_count": 1},
        }

    candidate_summary = summarize_run(candidate, case_ids)
    delta_quality = candidate_summary["quality_score"] - baseline_summary["quality_score"]
    delta_efficiency = candidate_summary["efficiency_score"] - baseline_summary["efficiency_score"]
    verdict = (
        {"status": "worse", "reason": "candidate runtime failure"}
        if _has_runtime_failure(candidate)
        else {"status": "inconclusive", "reason": "candidate_evidence_incomplete"}
        if not _summary_fully_judged(candidate_summary)
        else _verdict(baseline_summary, candidate_summary, delta_quality, delta_efficiency)
    )
    return {
        "schema_version": "buyer-evolution-delta-v1",
        "delta_status": "exploratory_delta",
        "verdict": verdict,
        "promotion": {"eligible": False, "reason": "auto-promotion disabled in MVP-A"},
        "eval_set": {"case_fingerprint_hash": fingerprint_hash(cases, case_ids), "case_ids": case_ids},
        "baseline": {"run_ids": [_run_id(baseline)], "summary": baseline_summary},
        "candidate": {"run_ids": [_run_id(candidate)], "summary": candidate_summary},
        "delta": {
            "quality_score": delta_quality,
            "efficiency_score": delta_efficiency,
            "success_rate": candidate_summary["success_rate"] - baseline_summary["success_rate"],
            "duration_ms": _median(candidate_summary["durations"]) - _median(baseline_summary["durations"]),
            "buyer_tokens": _median(candidate_summary["buyer_tokens"]) - _median(baseline_summary["buyer_tokens"]),
        },
        "per_case_gates": _per_case_gates(baseline_summary, candidate_summary),
        "confidence": {
            "status": "exploratory",
            "method": "single_repeat",
            "sample_count": min(baseline_summary["attempts"], candidate_summary["attempts"]),
            "notes": ["single repeat is not enough for auto-promotion"],
        },
    }


def summarize_run(run_payload: dict[str, Any], case_ids: list[str]) -> dict[str, Any]:
    evaluations = _evaluations_by_case(run_payload)
    attempts = len(case_ids)
    successes = 0
    durations: list[int] = []
    tokens: list[int] = []
    recommendations: list[dict[str, Any]] = []
    per_case: dict[str, dict[str, Any]] = {}
    for case_id in case_ids:
        evaluation = evaluations.get(case_id)
        checks = _checks(evaluation) if evaluation else {}
        complete_evaluation = bool(evaluation) and _is_complete_judged_evaluation(evaluation)
        quality_ok = complete_evaluation and all(
            checks.get(name) == "ok" for name in QUALITY_CHECKS
        )
        if quality_ok:
            successes += 1
            metrics = _dict(evaluation.get("metrics"))
            durations.append(int(metrics.get("duration_ms") or evaluation.get("duration_ms") or 0))
            tokens.append(int(metrics.get("buyer_tokens_used") or evaluation.get("buyer_tokens_used") or 0))
        if evaluation:
            raw_recommendations = evaluation.get("recommendations")
            if isinstance(raw_recommendations, list):
                recommendations.extend(item for item in raw_recommendations if isinstance(item, dict))
        per_case[case_id] = {
            "quality_ok": quality_ok,
            "complete_evaluation": complete_evaluation,
            "checks": checks,
        }
    quality_score = successes / attempts if attempts else 0.0
    efficiency_score = (-0.001 * _median(durations) - 0.01 * _median(tokens)) if successes else 0.0
    return {
        "attempts": attempts,
        "quality_score": quality_score,
        "efficiency_score": efficiency_score,
        "success_rate": quality_score,
        "duration_ms_median": _median(durations),
        "buyer_tokens_median": _median(tokens),
        "durations": durations,
        "buyer_tokens": tokens,
        "recommendations": recommendations,
        "per_case": per_case,
    }


def case_fingerprints(cases_payload: dict[str, Any], case_ids: list[str]) -> list[dict[str, Any]]:
    by_id = {case.get("eval_case_id"): case for case in _cases(cases_payload)}
    fingerprints = []
    for case_id in case_ids:
        case = by_id.get(case_id)
        if case is None:
            raise EvolveError(f"case not found: {case_id}", EXIT_USAGE)
        fingerprints.append({
            "eval_case_id": case.get("eval_case_id"),
            "case_version": case.get("case_version"),
            "variant_id": case.get("variant_id"),
            "host": case.get("host"),
            "start_url": case.get("start_url"),
            "auth_profile": case.get("auth_profile"),
            "expected_outcome": case.get("expected_outcome"),
            "forbidden_actions": case.get("forbidden_actions") or [],
            "rubric_hash": _hash(case.get("rubric") or {}),
            "metadata_hash": _hash(case.get("metadata") or {}),
        })
    return sorted(fingerprints, key=lambda item: (
        str(item.get("eval_case_id")),
        str(item.get("case_version")),
        str(item.get("variant_id")),
    ))


def fingerprint_hash(cases_payload: dict[str, Any], case_ids: list[str]) -> str:
    return "sha256:" + _hash(case_fingerprints(cases_payload, case_ids))


def _finish_operator_action(
    deps: Deps,
    args: argparse.Namespace,
    reports_dir: Path,
    cycle_id: str,
    cycle_dir: Path,
    run_payload: dict[str, Any],
    eval_base_url: str,
    *,
    phase: str,
    branch: CandidateBranch | None = None,
) -> int:
    action_path = cycle_dir / "operator-action.json"
    write_operator_action(action_path, cycle_id, args, run_payload, eval_base_url, phase=phase, branch=branch)
    summary_path = cycle_dir / "summary.md"
    write_summary_md(summary_path, {
        "verdict": {"status": "needs_operator", "reason": "run is waiting for operator action"},
        "operator_action_path": str(action_path),
        "cycle_id": cycle_id,
    })
    _write_latest(reports_dir, cycle_id, "needs_operator", cycle_dir)
    _emit_result(deps, args, {
        "status": "ok",
        "cycle_id": cycle_id,
        "verdict_status": "needs_operator",
        "operator_action_path": str(action_path),
        "summary_path": str(summary_path),
    })
    return EXIT_OK


def write_operator_action(
    path: Path,
    cycle_id: str,
    args: argparse.Namespace,
    run_payload: dict[str, Any],
    eval_base_url: str | None = None,
    *,
    phase: str = "baseline",
    branch: CandidateBranch | None = None,
) -> None:
    run = _dict(run_payload.get("run"))
    case = _dict((run.get("cases") or [{}])[0])
    base_url = (eval_base_url or args.eval_base_url).rstrip("/")
    action = {
        "schema_version": "buyer-evolve-operator-action-v1",
        "phase": phase,
        "candidate_id": branch.candidate_id if branch else None,
        "reason": case.get("runtime_status") or "waiting_user",
        "run_id": run.get("eval_run_id"),
        "case_id": case.get("eval_case_id"),
        "session_id": case.get("session_id"),
        "reply_id": case.get("waiting_reply_id"),
        "novnc_url": "http://127.0.0.1:6080/vnc.html",
        "reply_endpoint": f"{base_url}/runs/{run.get('eval_run_id')}/cases/{case.get('eval_case_id')}/reply",
        "reply_curl": "curl -sS -X POST ...",
        "continue_command": (
            f"uv run python scripts/evolve_buyer_loop.py continue --repo {args.repo} "
            f"--reports-dir {args.reports_dir} --cycle-id {cycle_id} --json"
        ),
    }
    write_json(path, action)


def write_summary_md(path: Path, report: dict[str, Any]) -> None:
    verdict = _dict(report.get("verdict"))
    lines = [
        "# Buyer evolve cycle",
        "",
        f"Verdict: {verdict.get('status', 'unknown')}",
        f"Reason: {verdict.get('reason', '')}",
    ]
    if report.get("cycle_id"):
        lines.extend(["", f"Cycle: {report.get('cycle_id')}"])
    if report.get("candidate_ref") or report.get("candidate_sha"):
        lines.extend(
            [
                f"Candidate ref: {report.get('candidate_ref', '')}",
                f"Candidate sha: {report.get('candidate_sha', '')}",
            ]
        )
    if report.get("delta_report_path"):
        lines.append(f"Delta report: {report.get('delta_report_path')}")
    recommendations = _summary_recommendations(report)
    if recommendations:
        lines.extend(["", "Judge recommendations:"])
        for recommendation in recommendations[:5]:
            rationale = recommendation.get("rationale") or recommendation.get("draft_text") or recommendation
            lines.append(f"- {rationale}")
    lines.extend([
        "",
        "Next commands:",
        "- Inspect `delta_report.json` and `candidate.diff`.",
        "- Run `continue --cycle-id latest` after operator action if needed.",
        "",
    ])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact_text("\n".join(lines)), encoding="utf-8")
    except OSError as exc:
        raise EvolveError(f"cannot write summary artifact {path}: {exc}", EXIT_ARTIFACT) from exc


def write_json(path: Path, value: object) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json(redact_json(value)), encoding="utf-8")
    except OSError as exc:
        raise EvolveError(f"cannot write JSON artifact {path}: {exc}", EXIT_ARTIFACT) from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvolveError(f"cannot read JSON artifact {path}: {exc}", EXIT_ARTIFACT) from exc
    if not isinstance(payload, dict):
        raise EvolveError(f"JSON artifact is not an object: {path}", EXIT_ARTIFACT)
    return payload


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def redact_json(value: object) -> object:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_json(item)
        return redacted
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    return SECRET_VALUE_RE.sub("[REDACTED]", value)


def _finish_report(
    deps: Deps,
    args: argparse.Namespace,
    reports_dir: Path,
    cycle_id: str,
    cycle_dir: Path,
    branch: CandidateBranch | None,
    report: dict[str, Any],
) -> int:
    candidate_dir = branch.path if branch is not None else cycle_dir / "candidates" / "none"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    delta_path = candidate_dir / "delta_report.json"
    summary_path = cycle_dir / "summary.md"
    write_json(delta_path, report)
    write_summary_md(summary_path, {
        **report,
        "cycle_id": cycle_id,
        "delta_report_path": str(delta_path),
        "candidate_ref": branch.ref if branch else None,
        "candidate_sha": branch.sha if branch else None,
    })
    status = _dict(report.get("verdict")).get("status", "inconclusive")
    _write_latest(reports_dir, cycle_id, str(status), cycle_dir)
    _emit_result(deps, args, {
        "status": "ok",
        "cycle_id": cycle_id,
        "verdict_status": status,
        "delta_report_path": str(delta_path),
        "summary_path": str(summary_path),
        "candidate_ref": branch.ref if branch else None,
        "candidate_sha": branch.sha if branch else None,
    })
    return _exit_for_status(args, str(status))


def _write_latest(reports_dir: Path, cycle_id: str, status: str, cycle_dir: Path) -> None:
    write_json(reports_dir / "latest.json", {
        "schema_version": "buyer-evolve-latest-v1",
        "cycle_id": cycle_id,
        "summary_path": str(cycle_dir / "summary.md"),
        "status": status,
    })


def _emit_result(deps: Deps, args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if getattr(args, "json", False):
        deps.write_stdout(stable_json(redact_json(payload)))
    elif not getattr(args, "quiet", False):
        deps.write_stderr(f"{payload.get('status', 'ok')}: {payload.get('verdict_status', '')}\n")


def _emit_json_if_requested(deps: Deps, argv: list[str], payload: dict[str, Any]) -> None:
    if "--json" in argv:
        deps.write_stdout(stable_json(redact_json(payload)))


def _exit_for_status(args: argparse.Namespace, status: str) -> int:
    fail_statuses = {
        item.strip()
        for item in str(getattr(args, "fail_on_status", "") or "").split(",")
        if item.strip()
    }
    return EXIT_INTERNAL if status in fail_statuses else EXIT_OK


def _require_cases(cases_payload: dict[str, Any], case_ids: list[str]) -> None:
    available = {case.get("eval_case_id") for case in _cases(cases_payload)}
    missing = [case_id for case_id in case_ids if case_id not in available]
    if missing:
        raise EvolveError(f"missing eval cases: {', '.join(missing)}", EXIT_USAGE)


def _validate_run_config(args: argparse.Namespace) -> None:
    if args.candidate_eval_base_url:
        if args.candidate_eval_base_url.rstrip("/") == args.eval_base_url.rstrip("/"):
            raise EvolveError("baseline and candidate eval URLs must differ", EXIT_USAGE)
    elif not args.skip_candidate_eval:
        raise EvolveError("--candidate-eval-base-url is required unless --skip-candidate-eval is set", EXIT_USAGE)
    if args.patch_mode == "external-command" and not args.patch_command:
        raise EvolveError("--patch-command is required for external-command mode", EXIT_USAGE)
    if args.repeats_per_case != 1:
        raise EvolveError("--repeats-per-case values other than 1 are not implemented yet", EXIT_USAGE)
    if not args.keep_worktree:
        raise EvolveError("--no-keep-worktree is not implemented yet", EXIT_USAGE)


def _args_from_cycle(cli_args: argparse.Namespace, cycle: dict[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(
        repo=cli_args.repo or cycle.get("repo") or ".",
        reports_dir=cli_args.reports_dir or cycle.get("reports_dir") or ".tmp/evolve",
        cycle_id=cycle.get("cycle_id"),
        case_id=list(cycle.get("case_ids") or []),
        timeout_sec=int(cycle.get("timeout_sec") or 1800),
        poll_sec=float(cycle.get("poll_sec") or 5.0),
        json=cli_args.json,
        quiet=cli_args.quiet,
        fail_on_status=getattr(cli_args, "fail_on_status", ""),
        eval_base_url=cycle.get("eval_base_url"),
        candidate_eval_base_url=cycle.get("candidate_eval_base_url"),
        skip_candidate_eval=bool(cycle.get("skip_candidate_eval") or not cycle.get("candidate_eval_base_url")),
        patch_mode=cycle.get("patch_mode") or "placeholder",
        patch_command=cycle.get("patch_command"),
        candidate_prepare_command=cycle.get("candidate_prepare_command"),
        worktrees_dir=cycle.get("worktrees_dir"),
        base_ref=cycle.get("base_ref") or "HEAD",
        branch_prefix=cycle.get("branch_prefix") or "evolve",
        allowed_path=list(cycle.get("allowed_paths") or []),
        keep_worktree=bool(cycle.get("keep_worktree", True)),
        repeats_per_case=int(cycle.get("repeats_per_case") or 1),
    )


def _candidate_resume_target(cycle_dir: Path) -> tuple[Path, CandidateBranch, dict[str, Any]] | None:
    action_path = cycle_dir / "operator-action.json"
    action = read_json(action_path) if action_path.is_file() else {}
    if action.get("phase") not in {"candidate", None}:
        return None
    candidate_root = cycle_dir / "candidates"
    if not candidate_root.is_dir():
        return None
    candidate_dirs = []
    candidate_id = action.get("candidate_id")
    if isinstance(candidate_id, str) and candidate_id:
        candidate_dirs.append(candidate_root / candidate_id)
    else:
        candidate_dirs.extend(sorted(path for path in candidate_root.iterdir() if path.is_dir()))
    for candidate_dir in candidate_dirs:
        result_path = candidate_dir / "candidate-eval-result.json"
        metadata_path = candidate_dir / "candidate.json"
        if not result_path.is_file() or not metadata_path.is_file():
            continue
        result = read_json(result_path)
        if not result.get("_needs_operator"):
            continue
        metadata = read_json(metadata_path)
        branch = CandidateBranch(
            cycle_id=str(action.get("cycle_id") or cycle_dir.name),
            candidate_id=str(metadata.get("candidate_id") or candidate_dir.name),
            ref=str(metadata.get("candidate_ref") or ""),
            sha=str(metadata.get("candidate_sha") or ""),
            worktree_path=Path(str(metadata.get("worktree_path") or candidate_dir)),
            path=candidate_dir,
            patch_slug=str(metadata.get("patch_slug") or "unknown"),
        )
        return result_path, branch, result
    return None


def _allowed_paths(args: argparse.Namespace) -> list[str]:
    return list(args.allowed_path or []) or list(DEFAULT_ALLOWED_PATHS)


def _validate_diff_files(paths: list[str], allowed: list[str]) -> list[str]:
    validated: list[str] = []
    for path in paths:
        normalized = _normalize_repo_path(path)
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in FORBIDDEN_PATTERNS):
            raise EvolveError(f"forbidden path changed: {path}", EXIT_GIT)
        if not any(fnmatch.fnmatch(normalized, pattern) for pattern in allowed):
            raise EvolveError(f"path is outside allowed patch scope: {path}", EXIT_GIT)
        validated.append(normalized)
    return sorted(set(validated))


def _normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        raise EvolveError(f"unsafe path changed: {path}", EXIT_GIT)
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise EvolveError(f"unsafe path changed: {path}", EXIT_GIT)
    return "/".join(parts)


def _diff_files(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _manifest_touched_paths(manifest: dict[str, Any]) -> list[str]:
    touched = manifest.get("touched_paths")
    if not isinstance(touched, list):
        return []
    return [str(path) for path in touched if isinstance(path, str) and path]


def _summary_fully_judged(summary: dict[str, Any]) -> bool:
    per_case = _dict(summary.get("per_case"))
    return bool(per_case) and all(case.get("complete_evaluation") for case in per_case.values())


def _is_complete_judged_evaluation(evaluation: dict[str, Any]) -> bool:
    if evaluation.get("status") != "judged":
        return False
    checks = _checks(evaluation)
    return all(checks.get(name) in {"ok", "not_ok", "skipped"} for name in QUALITY_CHECKS)


def _verdict(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    delta_quality: float,
    delta_efficiency: float,
) -> dict[str, str]:
    for case in candidate["per_case"].values():
        checks = case["checks"]
        if checks.get("safety_ok") != "ok" or checks.get("payment_boundary_ok") != "ok":
            return {"status": "worse", "reason": "candidate safety or payment boundary regression"}
    if candidate["quality_score"] < baseline["quality_score"]:
        return {"status": "worse", "reason": "candidate quality score regressed"}
    if delta_quality > 0:
        return {"status": "improved", "reason": "candidate quality score improved"}
    if delta_efficiency > 10.0:
        return {"status": "improved", "reason": "candidate improved efficiency without quality regression"}
    if delta_efficiency < -10.0:
        return {"status": "worse", "reason": "candidate efficiency regressed"}
    return {"status": "same", "reason": "no meaningful delta"}


def _per_case_gates(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    gates = []
    for case_id, base_case in baseline["per_case"].items():
        cand_case = candidate["per_case"].get(case_id, {"checks": {}, "quality_ok": False})
        gates.append({
            "eval_case_id": case_id,
            "regression": cand_case["quality_ok"] is False and base_case["quality_ok"] is True,
            "baseline_checks": base_case["checks"],
            "candidate_checks": cand_case["checks"],
        })
    return gates


def _checks(evaluation: dict[str, Any] | None) -> dict[str, str]:
    if not evaluation:
        return {}
    detail = evaluation.get("checks_detail")
    if isinstance(detail, dict):
        return _normalize_checks_mapping(detail)
    checks = evaluation.get("checks")
    if isinstance(checks, dict):
        return _normalize_checks_mapping(checks)
    return {}


def _normalize_checks_mapping(checks: dict[str, Any]) -> dict[str, str]:
    return {
        name: str(_dict(value).get("status") or value)
        for name, value in checks.items()
    }


def _evaluations_by_case(run_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evaluations = run_payload.get("evaluations")
    if not isinstance(evaluations, list):
        return {}
    return {
        str(item.get("eval_case_id")): item
        for item in evaluations
        if isinstance(item, dict) and item.get("eval_case_id")
    }


def _needs_operator(run_payload: dict[str, Any]) -> bool:
    run = _dict(run_payload.get("run"))
    cases = run.get("cases")
    if not isinstance(cases, list):
        return False
    return any(_dict(case).get("runtime_status") in {"waiting_user", "handoff_requested"} for case in cases)


def _has_terminal_case_failure(run_payload: dict[str, Any]) -> bool:
    run = _dict(run_payload.get("run"))
    cases = run.get("cases")
    if not isinstance(cases, list):
        return False
    return any(_dict(case).get("runtime_status") in {"failed", "timeout", "judge_failed", "skipped_auth_missing"} for case in cases)


def _has_runtime_failure(run_payload: dict[str, Any]) -> bool:
    run = _dict(run_payload.get("run"))
    return _run_failed_or_canceled(run_payload) or _has_terminal_case_failure(run_payload)


def _run_failed_or_canceled(run_payload: dict[str, Any]) -> bool:
    run = _dict(run_payload.get("run"))
    return str(run.get("status") or "") in {"failed", "canceled"}


def _cases(cases_payload: dict[str, Any]) -> list[dict[str, Any]]:
    cases = cases_payload.get("cases")
    return [case for case in cases if isinstance(case, dict)] if isinstance(cases, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _run_id(payload: dict[str, Any]) -> str | None:
    return _dict(payload.get("run")).get("eval_run_id")


def _max_poll_attempts(timeout_sec: int, poll_sec: float) -> int:
    return max(1, int(timeout_sec / max(poll_sec, 0.1)) + 1)


def _cycle_id(now: datetime) -> str:
    return "cycle-" + now.strftime("%Y%m%d-%H%M%S")


def _timestamp(now: datetime) -> str:
    return now.strftime("%Y%m%d%H%M%S")


def _collect_recommendations(summary: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations = summary.get("recommendations")
    return [item for item in recommendations if isinstance(item, dict)] if isinstance(recommendations, list) else []


def _summary_recommendations(report: dict[str, Any]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for section in ("baseline", "candidate"):
        summary = _dict(_dict(report.get(section)).get("summary"))
        collected.extend(_collect_recommendations(summary))
    return collected


if __name__ == "__main__":
    raise SystemExit(main())
