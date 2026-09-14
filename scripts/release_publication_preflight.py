"""Replay the publication read path with archived inputs and zero remote writes.

This development entry executes the actual YAML shell/CLI stages. GitHub GETs
read authenticated cached responses/ZIPs; no network adapter or publication
transaction is available. Its output is always NON_AUTHORITATIVE_TEST.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path

import yaml

from release.contract import rc_target_version

SOURCE = Path(__file__).resolve().parents[1]
WINDOWS_JQ_SHA256 = "23cb60a1354eed6bcc8d9b9735e8c7b388cd1fdcb75726b93bc299ef22dd9334"
WINDOWS_JQ_PATH = Path.home() / ".animemo/tools/jq-1.8.1-windows-amd64.exe"


def _bash_executable():
    """Select the supported host runtime; CLI inputs cannot select a program."""
    if os.name == "nt":
        return "C:/Program Files/Git/bin/bash.exe"
    return "/usr/bin/bash"


def _git_revision(revision, env):
    if revision != "HEAD" and re.fullmatch(r"[0-9a-f]{40}\^\{tree\}", revision) is None:
        raise ValueError("PREFLIGHT_GIT_REVISION_INVALID")
    executable = (
        "C:/Program Files/Git/cmd/git.exe" if os.name == "nt" else "/usr/bin/git"
    )
    return (
        subprocess.run(
            [executable, "rev-parse", "--verify", revision],
            cwd=SOURCE,
            env=env,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )


def _source_commit(env):
    return _git_revision("HEAD", env)


def _prepare_jq(tools):
    """Stage a trusted jq file without trusting any sibling executables."""
    if os.name == "nt":
        payload = WINDOWS_JQ_PATH.read_bytes()
        if hashlib.sha256(payload).hexdigest() != WINDOWS_JQ_SHA256:
            raise ValueError("WINDOWS_JQ_DIGEST_MISMATCH")
        target = tools / "jq.exe"
    else:
        payload = Path("/usr/bin/jq").read_bytes()
        target = tools / "jq"
    with target.open("xb") as stream:
        stream.write(payload)
    if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(payload).digest():
        raise ValueError("STAGED_JQ_DIGEST_MISMATCH")
    target.chmod(0o700)


def _replay_environment(tools):
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH", "GLOBIGNORE"}
        and not key.startswith("BASH_FUNC_")
    }
    paths = (
        [
            "C:/Program Files/Git/usr/bin",
            "C:/Program Files/Git/bin",
            "C:/Program Files/Git/cmd",
        ]
        if os.name == "nt"
        else ["/usr/bin", "/bin"]
    )
    env["PATH"] = os.pathsep.join([str(tools), *paths])
    return env


def _python_child(arguments):
    cli_reads = {
        "decode-candidate-acceptance-receipt",
        "resolve-version",
        "extract-qualification-artifact",
        "verify-prepublication-materials",
        "verify-prepublication-candidate",
        "verify-publish-candidate-input",
        "validate-freshness-run-metadata",
        "extract-metadata-freshness-artifact",
        "verify-metadata-freshness",
        "validate-deployment-contract",
        "validate-manifest",
        "build-portable",
        "plan-publication-files",
    }
    version_code = "import sys; from release.contract import rc_target_version; print(rc_target_version(sys.argv[1]))"
    allowed = (
        (
            len(arguments) >= 3
            and arguments[:2] == ["-m", "release.cli"]
            and arguments[2] in cli_reads
        )
        or (arguments[:3] == ["-m", "release.qualification_finalization", "phase-b"])
        or (
            arguments[:2] == ["-m", "scripts.release_authority"]
            and os.environ.get("OPERATION") in {"publish", "portable"}
        )
        or (
            len(arguments) >= 2
            and arguments[1] == "verify"
            and Path(arguments[0]).absolute()
            in {
                SOURCE / "scripts/platform_qualification.py",
                SOURCE / "scripts/release_notes_preflight.py",
            }
        )
        or (len(arguments) == 3 and arguments[:2] == ["-c", version_code])
    )
    if not allowed:
        print("PREFLIGHT_CHILD_NOT_READ_ONLY", file=sys.stderr)
        return 2
    # Only this explicitly invoked test process can inject the existing clock
    # seam. Production CLI and workflow semantics have no clock override.
    if arguments[:3] == ["-m", "release.cli", "verify-metadata-freshness"]:
        clock = os.environ.get("ANIMEMO_PREFLIGHT_TEST_CLOCK")
        if clock:
            from release import cli

            cli.verify_metadata_freshness_artifact = partial(
                cli.verify_metadata_freshness_artifact,
                verified_at=datetime.fromisoformat(clock.replace("Z", "+00:00")),
            )
            return cli.main(arguments[2:])
    if arguments[:1] == ["-c"]:
        return subprocess.run(
            [sys.executable, "-X", "utf8", *arguments], check=False
        ).returncode
    elif arguments[:1] == ["-m"]:
        sys.argv = arguments[1:]
        runpy.run_module(arguments[1], run_name="__main__", alter_sys=True)
    else:
        sys.argv = arguments
        runpy.run_path(arguments[0], run_name="__main__")
    return 0


def _read(path):
    from release.materials import reject_duplicate_json_keys

    return json.loads(path.read_bytes(), object_pairs_hook=reject_duplicate_json_keys)


def _save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _shell(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


class PreflightStageError(RuntimeError):
    def __init__(self, code, exit_code):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code if exit_code not in (None, 0) else 2


@contextmanager
def _stage_record(summary, output, name):
    record = {"name": name, "status": "RUNNING", "exit_code": None}
    summary["stages"].append(record)
    summary["status"] = "IN_PROGRESS"
    try:
        _save(output / "result.json", summary)
        yield record
        if record["exit_code"] != 0:
            raise PreflightStageError("STAGE_EXIT", record["exit_code"])
        record["status"] = "PASS"
        _save(output / "result.json", summary)
    except (
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        StopIteration,
        PreflightStageError,
    ) as error:
        if record["exit_code"] not in (None, 0):
            code = "STAGE_EXIT"
            if isinstance(error, OSError):
                summary.setdefault("secondary_failures", []).append(
                    "STAGE_DIAGNOSTIC_WRITE_FAILED"
                )
        elif isinstance(error, subprocess.TimeoutExpired):
            code = "STAGE_TIMEOUT"
        elif isinstance(error, OSError):
            code = "STAGE_IO_FAILURE"
        else:
            code = "STAGE_INPUT_INVALID"
        record.update(status="FAIL", code=code)
        summary["status"] = "FAIL"
        try:
            _save(output / "result.json", summary)
        except OSError:
            summary.setdefault("secondary_failures", []).append("RESULT_WRITE_FAILED")
        print(json.dumps(summary), file=sys.stderr)
        raise PreflightStageError(code, record["exit_code"]) from None


def replay(args):
    from release import candidate
    from release import metadata_freshness as mf

    qroot, froot = (
        args.qualification_directory.absolute(),
        args.freshness_directory.absolute(),
    )
    receipt_raw = args.candidate_receipt.read_bytes()
    receipt = candidate.validate_aggregate_receipt(_read(args.candidate_receipt))
    if (
        candidate.canonical_json_bytes(receipt) != receipt_raw
        or receipt["result"] != "PASS"
    ):
        raise ValueError("CANDIDATE_CANONICAL_PASS_REQUIRED")
    wire = candidate.encode_aggregate_receipt_b64url(receipt)
    if candidate.decode_aggregate_receipt_b64url(wire)[1] != receipt_raw:
        raise ValueError("CANDIDATE_WIRE_BYTE_MISMATCH")
    qrun, qjobs, qartifacts = (
        _read(qroot / name) for name in ("run.json", "jobs.json", "artifacts.json")
    )
    frun, fartifacts = (_read(froot / name) for name in ("run.json", "artifacts.json"))
    sha, tree, qid, fid = (
        receipt["source_sha"],
        receipt["source_tree"],
        receipt["qualification_run_id"],
        frun["id"],
    )
    selected = mf.validate_qualification_run_metadata(
        run_metadata=qrun,
        jobs_metadata=qjobs,
        artifacts_metadata=qartifacts,
        expected_run_id=qid,
        expected_sha=sha,
    )
    fselected = mf.validate_freshness_run_metadata(
        run_metadata=frun,
        artifacts_metadata=fartifacts,
        expected_run_id=fid,
        expected_sha=sha,
    )
    archives = [
        (qroot / "evidence.zip", qartifacts, selected["artifactId"]),
        (
            qroot / "controller-authority.zip",
            qartifacts,
            selected["controllerArtifactId"],
        ),
        (froot / "evidence.zip", fartifacts, fselected["artifactId"]),
    ]
    for path, listing, identifier in archives:
        item = next(a for a in listing["artifacts"] if a["id"] == identifier)
        with path.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != item["size_in_bytes"] or digest != item["digest"]:
            raise ValueError("CACHED_ARCHIVE_IDENTITY_MISMATCH")
    output = args.output_directory.absolute()
    output.mkdir(exist_ok=False)
    temp, workspace, tools = (
        output / name for name in ("runtime", "workspace", "tools")
    )
    for path in (temp, workspace, tools):
        path.mkdir()
    _prepare_jq(tools)
    document = yaml.safe_load(
        (SOURCE / ".github/workflows/release.yml").read_text("utf-8")
    )
    env = _replay_environment(tools)
    if _git_revision(f"{sha}^{{tree}}", env) != tree:
        raise ValueError("ARCHIVED_SOURCE_TREE_MISMATCH")
    env.update(
        {
            "PYTHONPATH": str(SOURCE),
            "PYTHONUTF8": "1",
            "RUNNER_TEMP": temp.as_posix(),
            "GITHUB_WORKSPACE": SOURCE.as_posix(),
            "GITHUB_OUTPUT": (temp / "outputs.txt").as_posix(),
            "GITHUB_ENV": (temp / "env.txt").as_posix(),
            "GITHUB_SHA": sha,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "yanyuhanyue/AniMemo",
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "QUALIFICATION_RUN_ID": str(qid),
            "FRESHNESS_RUN_ID": str(fid),
            "INTENDED_MAIN_SHA": sha,
            "CANDIDATE_SHA": sha,
            "CANDIDATE_VERSION": receipt["candidate_version"],
            "RELEASE_TAG": receipt["candidate_version"],
            "CHANNEL": "rc",
            "RELEASE_CHANNEL": "rc",
            "OPERATION": "publish",
            "VERSION_BUMP": "major",
            "TARGET_VERSION": rc_target_version(receipt["candidate_version"]),
            "RELEASE_GRAPH_CONTRACT": "animemo.release-gate.jobs/v2",
            "API_REPOSITORY": "ghcr.io/yanyuhanyue/animemo-api",
            "WEB_REPOSITORY": "ghcr.io/yanyuhanyue/animemo-web",
            "NEEDS_JSON": json.dumps(
                {
                    n: {"result": ("success" if n == "preflight" else "skipped")}
                    for n in (
                        "preflight",
                        "full-ci",
                        "full-release-gate",
                        "performance",
                        "platform-qualification",
                    )
                }
            ),
        }
    )
    # The baseline comes from the exact Q bytes, never a default host value.
    import zipfile

    with zipfile.ZipFile(qroot / "evidence.zip") as archive:
        qualification = json.loads(archive.read(f"release-qualification-{qid}.json"))
    env["UPGRADE_BASE_SHA"] = qualification["upgrade_base_sha"]
    if args.test_clock:
        datetime.fromisoformat(args.test_clock.replace("Z", "+00:00"))
        env["ANIMEMO_PREFLIGHT_TEST_CLOCK"] = args.test_clock
    else:
        env.pop("ANIMEMO_PREFLIGHT_TEST_CLOCK", None)
    substitutions = {
        "inputs.qualification_run_id": str(qid),
        "inputs.metadata_freshness_run_id": str(fid),
        "inputs.channel": "rc",
        "inputs.upgrade_base_sha": env["UPGRADE_BASE_SHA"],
        "inputs.candidate_acceptance_receipt_b64url": "$CANDIDATE_ACCEPTANCE_RECEIPT_B64URL",
        "needs.preflight.outputs.candidate_sha": sha,
        "needs.preflight.outputs.release_tag": receipt["candidate_version"],
        "needs.preflight.outputs.target_version": env["TARGET_VERSION"],
        "needs.metadata-freshness-authority.outputs.qualification_artifact_id": str(
            selected["artifactId"]
        ),
        "needs.metadata-freshness-authority.outputs.candidate_tree": tree,
        "needs.metadata-freshness-authority.outputs.candidate_acceptance_receipt_sha256": candidate.sha256_bytes(
            receipt_raw
        ),
        "needs.metadata-freshness-authority.outputs.candidate_version": receipt[
            "candidate_version"
        ],
    }
    routes = {
        f"repos/yanyuhanyue/AniMemo/actions/runs/{qid}": qroot / "run.json",
        f"repos/yanyuhanyue/AniMemo/actions/runs/{qid}/jobs?per_page=100": qroot
        / "jobs.json",
        f"repos/yanyuhanyue/AniMemo/actions/runs/{qid}/artifacts?per_page=100": qroot
        / "artifacts.json",
        f"repos/yanyuhanyue/AniMemo/actions/runs/{fid}": froot / "run.json",
        f"repos/yanyuhanyue/AniMemo/actions/runs/{fid}/artifacts?per_page=100": froot
        / "artifacts.json",
    }
    for path, listing, identifier in archives:
        item = next(a for a in listing["artifacts"] if a["id"] == identifier)
        routes[item["archive_download_url"].removeprefix("https://api.github.com/")] = (
            path
        )
    route_cases = "".join(
        f"  {_shell(url)}) cat {_shell(path.as_posix())} ;;\n"
        for url, path in routes.items()
    )
    prefix = f"""python() {{ {_shell(sys.executable)} -X utf8 {_shell(__file__)} --replay-python "$@"; }}
git() {{
  [[ "$1" == rev-parse || ( "$1" == tag && "$2" == --list ) ]] || return 92
  for arg in "$@"; do case "$arg" in -d|--delete|-f|--force) return 92 ;; esac; done
  if [[ "$1" == rev-parse && "$2" == 'HEAD^{{commit}}' ]]; then printf '%s\\n' {_shell(sha)};
  elif [[ "$1" == rev-parse && "$2" == 'HEAD^{{tree}}' ]]; then printf '%s\\n' {_shell(tree)};
  else command git -C {_shell(SOURCE.as_posix())} "$@"; fi
}}
gh() {{ [[ "$1" == api && $# == 2 ]] || return 93; case "$2" in
{route_cases}  *) return 94 ;; esac; }}
curl() {{
  local url destination
  while (( $# )); do
    case "$1" in
      https://api.github.com/*) url="${{1#https://api.github.com/}}"; shift ;;
      -o) destination="$2"; shift 2 ;;
      -H) shift 2 ;;
      --fail|--location|--silent|--show-error) shift ;;
      *) return 95 ;;
    esac
  done
  [[ -n "$url" && -n "$destination" ]] || return 96
  gh api "$url" > "$destination"
}}
source {_shell((SOURCE / "scripts/release-input-diagnostics.sh").as_posix())}
"""
    if args.windows_file_mode_adapter:
        if os.name != "nt":
            raise ValueError("WINDOWS_ADAPTER_ONLY")
        adapter = """install() {
  local directory=false args=()
  while (( $# )); do
    case "$1" in -d) directory=true; shift ;; -m) shift 2 ;; --) shift ;; *) args+=("$1"); shift ;; esac
  done
  if "$directory"; then mkdir -p -- "${args[@]}"; else cp -- "${args[@]}"; fi
}
"""
        prefix = adapter + prefix
        (tools / "install").write_text(
            "#!/usr/bin/env bash\n" + adapter + 'install "$@"\n', encoding="utf-8"
        )
    stages = [
        ("preflight", "Resolve deterministic pre-release version"),
        ("release-authority", "Download and verify Phase A qualification evidence"),
        ("release-authority", "Enforce Phase B Release Producer authority"),
        (
            "release-authority",
            "Stage the validated release input for Phase B production",
        ),
        (
            "metadata-freshness-authority",
            "Authenticate exact trusted freshness producer and artifact",
        ),
        (
            "metadata-freshness-authority",
            "Stage exact freshness input for the mutation job",
        ),
        (
            "publish",
            "Verify and stage exact qualified prepublication materials before mutation",
        ),
        (
            "publish",
            "Verify metadata freshness TTL immediately before external publication",
        ),
        ("publish", "Validate the exact Candidate-accepted release assets"),
        ("publish", "Assemble the portable transport from accepted OCI layouts"),
        ("publish", "Generate the closed publication plan without mutation"),
    ]
    source_commit = _source_commit(env)
    summary = {
        "context": "NON_AUTHORITATIVE_TEST",
        "execution_source_commit": source_commit,
        "execution_source_kind": "LOCAL_CHECKOUT_INCLUDING_UNCOMMITTED_CHANGES",
        "material_source_sha": sha,
        "material_source_tree": tree,
        "qualification_run_id": qid,
        "freshness_run_id": fid,
        "test_clock": args.test_clock,
        "windows_file_mode_adapter": args.windows_file_mode_adapter,
        "remote_mutations": 0,
        "stages": [],
    }
    for index, (job, name) in enumerate(stages):
        with _stage_record(summary, output, name) as record:
            step = next(
                s for s in document["jobs"][job]["steps"] if s.get("name") == name
            )
            code = step["run"]
            for key, value in substitutions.items():
                code = code.replace("${{ " + key + " }}", value)
            if "${{" in code:
                raise ValueError("UNBOUND_WORKFLOW_INPUT")
            code = re.sub(
                r"python (scripts/[A-Za-z0-9_./-]+)",
                lambda m: "python " + _shell((SOURCE / m[1]).as_posix()),
                code,
            )
            code = code.replace(
                "source scripts/release-input-diagnostics.sh",
                "source "
                + _shell((SOURCE / "scripts/release-input-diagnostics.sh").as_posix()),
            )
            script = output / f"{index:02}.sh"
            script.write_text("set -euo pipefail\n" + prefix + code, encoding="utf-8")
            stage_env = env.copy()
            if index in (0, 1, 4):
                stage_env["CANDIDATE_ACCEPTANCE_RECEIPT_B64URL"] = wire
            if index in (1, 4):
                stage_env["GH_TOKEN"] = "NON_AUTHORITATIVE_TEST"
            result = subprocess.run(
                [_bash_executable(), "--noprofile", "--norc", "--", str(script)],
                cwd=workspace,
                env=stage_env,
                capture_output=True,
                timeout=900,
                check=False,
            )
            record["exit_code"] = result.returncode
            (output / f"{index:02}.stdout").write_bytes(result.stdout)
            (output / f"{index:02}.stderr").write_bytes(result.stderr)
            if result.returncode:
                raise PreflightStageError("STAGE_EXIT", result.returncode)
            if (temp / "env.txt").exists():
                for line in (temp / "env.txt").read_text("utf-8").splitlines():
                    key, sep, value = line.partition("=")
                    if sep:
                        env[key] = value
            if "ANIMEMO_ACCEPTED_API_DIGEST" in env:
                env["API_DIGEST"] = env["ANIMEMO_ACCEPTED_API_DIGEST"]
                env["WEB_DIGEST"] = env["ANIMEMO_ACCEPTED_WEB_DIGEST"]
        print(json.dumps(record), flush=True)
    summary["status"] = "PASS"
    _save(output / "result.json", summary)
    return 0


def main():
    if sys.argv[1:2] == ["--replay-python"]:
        return _python_child(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "qualification-directory",
        "freshness-directory",
        "candidate-receipt",
        "output-directory",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--test-clock",
        help="Explicit NON_AUTHORITATIVE_TEST clock; no receipt bytes change",
    )
    parser.add_argument("--windows-file-mode-adapter", action="store_true")
    args = parser.parse_args()
    try:
        return replay(args)
    except PreflightStageError as error:
        return error.exit_code
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        StopIteration,
        subprocess.SubprocessError,
    ) as error:
        code = getattr(error, "code", "PREFLIGHT_INPUT_INVALID")
        if (
            not isinstance(code, str)
            or re.fullmatch("[A-Z][A-Z0-9_]{0,95}", code) is None
        ):
            code = "PREFLIGHT_INPUT_INVALID"
        result = {
            "context": "NON_AUTHORITATIVE_TEST",
            "status": "NOT_RUN",
            "code": code,
            "remote_mutations": 0,
        }
        if not args.output_directory.exists():
            args.output_directory.mkdir(parents=True)
            _save(args.output_directory / "result.json", result)
        print(json.dumps(result), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
