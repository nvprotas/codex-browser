from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tools import propose_buyer_patch


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def base_env(tmp_path: Path) -> dict[str, str]:
    worktree = tmp_path / "worktree"
    artifact_dir = tmp_path / "artifacts"
    worktree.mkdir()
    request_path = artifact_dir / "patch-request.json"
    manifest_path = artifact_dir / "patch-manifest.json"
    write_json(
        request_path,
        {
            "schema_version": "buyer-evolve-patch-request-v1",
            "cycle_id": "cycle-1",
            "candidate_id": "cand-001-external",
            "candidate_worktree": str(worktree),
            "selected_case_ids": ["litres_purchase_book_001"],
            "judge_recommendations": [
                {
                    "priority": "high",
                    "rationale": "Проверять способ оплаты до покупки.",
                    "draft_text": "Не нажимать покупку при сохраненной карте.",
                }
            ],
            "allowed_paths": ["docs/buyer-agent/instructions/*.md", "buyer/app/**/*.py"],
            "output_manifest_path": str(manifest_path),
        },
    )
    return {
        "EVOLVE_CANDIDATE_WORKTREE": str(worktree),
        "EVOLVE_PATCH_REQUEST": str(request_path),
        "EVOLVE_PATCH_MANIFEST": str(manifest_path),
        "EVOLVE_ALLOWED_PATHS": "docs/buyer-agent/instructions/*.md:buyer/app/**/*.py",
        "EVOLVE_CODEX_BIN": "fake-codex",
        "EVOLVE_CODEX_TIMEOUT_SEC": "30",
    }


def test_propose_buyer_patch_runs_codex_with_eval_context_and_writes_manifest_from_worktree_diff(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    calls: list[dict[str, Any]] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        cwd = kwargs["cwd"]
        prompt = kwargs.get("input", "")
        calls.append({"cmd": cmd, "cwd": cwd, "prompt": prompt})
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "base-sha\n", "")
        if cmd == ["git", "diff", "--name-only", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "docs/buyer-agent/instructions/litres.md\nbuyer/app/prompt_builder.py\n",
                "",
            )
        if cmd == ["git", "diff", "--name-only", "--cached", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["fake-codex", "exec"]:
            assert "EVOLVE_PATCH_REQUEST" in prompt
            assert "EVOLVE_PATCH_MANIFEST" not in prompt
            assert "EVOLVE_PATCH_MANIFEST" not in kwargs["env"]
            assert "Проверять способ оплаты до покупки." in prompt
            assert "уменьшать число agent/browser шагов" in prompt
            assert "Если baseline уже quality_ok/success_rate=1.0" in prompt
            assert "Не оптимизируй скорость ценой риска" in prompt
            assert "Не запускай `git add`, `git commit`" in prompt
            assert "Оставь изменения в рабочем дереве" in prompt
            return subprocess.CompletedProcess(cmd, 0, '{"type":"task_complete"}\n', "diagnostic\n")
        raise AssertionError(f"unexpected command: {cmd}")

    code = propose_buyer_patch.main(env=env, runner=runner)

    assert code == 0
    artifact_dir = Path(env["EVOLVE_PATCH_MANIFEST"]).parent
    assert (artifact_dir / "codex-exec.stdout.jsonl").read_text(encoding="utf-8") == '{"type":"task_complete"}\n'
    assert (artifact_dir / "codex-exec.stderr.log").read_text(encoding="utf-8") == "diagnostic\n"
    assert "Ты вложенный Codex patch-worker" in (artifact_dir / "codex-exec.prompt.txt").read_text(encoding="utf-8")
    manifest = json.loads(Path(env["EVOLVE_PATCH_MANIFEST"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "buyer-evolve-patch-manifest-v1"
    assert manifest["patch_slug"] == "litres-purchase-book-001-patch"
    assert manifest["patch_kind"] == "mixed"
    assert manifest["touched_paths"] == [
        "buyer/app/prompt_builder.py",
        "docs/buyer-agent/instructions/litres.md",
    ]
    assert manifest["rationale"] == "Проверять способ оплаты до покупки."
    assert calls[0]["cmd"] == ["git", "rev-parse", "HEAD"]
    assert calls[1]["cmd"][:2] == ["fake-codex", "exec"]
    assert calls[1]["cwd"] == Path(env["EVOLVE_CANDIDATE_WORKTREE"])
    assert calls[2]["cmd"] == ["git", "rev-parse", "HEAD"]
    assert calls[3]["cmd"] == ["git", "diff", "--name-only", "HEAD"]
    assert calls[4]["cmd"] == ["git", "diff", "--name-only", "--cached", "HEAD"]
    assert calls[5]["cmd"] == ["git", "ls-files", "--others", "--exclude-standard"]


def test_propose_buyer_patch_still_accepts_already_committed_patch(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    head = "base-sha"

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal head
        cmd = args[0]
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, f"{head}\n", "")
        if cmd[:2] == ["fake-codex", "exec"]:
            head = "candidate-sha"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "diff-tree"]:
            return subprocess.CompletedProcess(cmd, 0, "docs/buyer-agent/instructions/litres.md\n", "")
        if cmd[:3] == ["git", "log", "-1"]:
            return subprocess.CompletedProcess(cmd, 0, "evolve buyer: litres-payment-guard\n", "")
        raise AssertionError(f"unexpected command: {cmd}")

    code = propose_buyer_patch.main(env=env, runner=runner)

    assert code == 0
    manifest = json.loads(Path(env["EVOLVE_PATCH_MANIFEST"]).read_text(encoding="utf-8"))
    assert manifest["patch_slug"] == "litres-payment-guard"
    assert manifest["touched_paths"] == ["docs/buyer-agent/instructions/litres.md"]


def test_propose_buyer_patch_defaults_codex_timeout_to_one_hour(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env.pop("EVOLVE_CODEX_TIMEOUT_SEC")
    timeouts: list[int | None] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        timeouts.append(kwargs.get("timeout"))
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "base-sha\n", "")
        if cmd[:2] == ["fake-codex", "exec"]:
            raise subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=kwargs.get("timeout"),
                output='{"type":"partial"}\n',
                stderr="still running\n",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    code = propose_buyer_patch.main(env=env, runner=runner)

    assert code == 1
    assert timeouts[1] == 3600
    artifact_dir = Path(env["EVOLVE_PATCH_MANIFEST"]).parent
    assert (artifact_dir / "codex-exec.stdout.jsonl").read_text(encoding="utf-8") == '{"type":"partial"}\n'
    assert (artifact_dir / "codex-exec.stderr.log").read_text(encoding="utf-8") == "still running\n"


def test_propose_buyer_patch_fails_when_codex_does_not_change_files(tmp_path: Path) -> None:
    env = base_env(tmp_path)

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "base-sha\n", "")
        if cmd[:2] == ["fake-codex", "exec"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["git", "diff", "--name-only"] or cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    code = propose_buyer_patch.main(env=env, runner=runner, head_reader=lambda _path: "base-sha")

    assert code == 2
