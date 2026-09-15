"""One protected execution of the original RC transaction, with live prechecks."""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .candidate import canonical_json_bytes
from .publication_remote import build_publication_runtime, canonical_identity
from .publication_transaction import DurablePublicationController, RemoteObservation
from .recovery_contract import (
    identity,
    instant,
    policy,
    require,
    utc,
    validate_bound_ledger,
    validate_execution,
)
from .recovery_remote import GuardedRecoveryJournal, file_digest


class RecoveryPlatform:
    def __init__(self, remote, repository: Path, environment, event, *, clock=None):
        self.remote, self.repository = remote, repository
        self.environment, self.event = dict(environment), event
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.previous_time = None
        self.p = policy()

    def now(self):
        current = self.clock()
        require(
            current.tzinfo is not None
            and (self.previous_time is None or current >= self.previous_time),
            "RECOVERY_CLOCK_ROLLBACK",
        )
        self.previous_time = current
        return current

    def git(self, *args):
        require(
            args
            in {
                ("rev-parse", "HEAD"),
                ("rev-parse", "HEAD^{tree}"),
                ("show", "-s", "--format=%P", "HEAD"),
                ("status", "--porcelain", "--untracked-files=all"),
            },
            "RECOVERY_GIT_READONLY_BOUNDARY",
        )
        result = subprocess.run(
            ("git", "-C", str(self.repository), *args),
            capture_output=True,
            timeout=30,
            check=False,
        )
        require(result.returncode == 0, "RECOVERY_GIT_READ_FAILED")
        return result.stdout.decode("utf-8", errors="strict").strip()

    def execution(self):
        remote, p = self.remote, self.p
        rid = self.environment.get("GITHUB_RUN_ID", "")
        require(
            rid.isascii() and rid.isdigit() and len(rid) < 24, "RECOVERY_RUN_ID_INVALID"
        )
        run = remote.get(f"{remote.base}/actions/runs/{rid}")
        workflow = remote.get(f"{remote.base}/actions/workflows/release-recovery.yml")
        pr = remote.merge_identity(p["sourcePr"], p["sourceBranch"])
        previous = p["previousTool"]
        previous_pr = remote.merge_identity(
            previous["sourcePr"], previous["sourceBranch"]
        )
        tool_commit = remote.get(f"{remote.base}/git/commits/{pr['merge_commit_sha']}")
        previous_commit = remote.get(f"{remote.base}/git/commits/{previous['sha']}")
        previous_reviewed = remote.get(
            f"{remote.base}/git/commits/{previous['reviewedHead']}"
        )
        parent = p["parentTool"]
        parent_facts = (
            remote.merge_identity(parent["sourcePr"], parent["sourceBranch"]),
            remote.get(f"{remote.base}/git/commits/{parent['sha']}"),
            remote.get(f"{remote.base}/git/commits/{parent['reviewedHead']}"),
        )
        diagnostic = p["diagnosticTool"]
        diagnostic_facts = (
            remote.merge_identity(diagnostic["sourcePr"], diagnostic["sourceBranch"]),
            remote.get(f"{remote.base}/git/commits/{diagnostic['sha']}"),
            remote.get(f"{remote.base}/git/commits/{diagnostic['reviewedHead']}"),
        )
        draft_read = p["draftReadTool"]
        draft_read_facts = (
            remote.merge_identity(draft_read["sourcePr"], draft_read["sourceBranch"]),
            remote.get(f"{remote.base}/git/commits/{draft_read['sha']}"),
            remote.get(f"{remote.base}/git/commits/{draft_read['reviewedHead']}"),
        )
        superseded_run = remote.get(
            f"{remote.base}/actions/runs/{p['supersededFailure']['runId']}"
        )
        repo = remote.get(remote.base)
        main = remote.get(f"{remote.base}/git/ref/heads/main")["object"]["sha"]
        reviewed = remote.get(f"{remote.base}/git/commits/{pr['head']['sha']}")
        require(
            reviewed.get("sha") == pr["head"]["sha"], "RECOVERY_REVIEWED_HEAD_INVALID"
        )
        runs = remote.listed(
            f"{remote.base}/actions/workflows/{workflow['id']}/runs?event=workflow_dispatch",
            "workflow_runs",
        )
        require(
            not self.git("status", "--porcelain", "--untracked-files=all"),
            "RECOVERY_WORKTREE_DIRTY",
        )
        claim = validate_execution(
            environment=self.environment,
            event=self.event,
            run=run,
            workflow=workflow,
            repository=repo,
            pull_request=pr,
            main_sha=main,
            checkout_sha=self.git("rev-parse", "HEAD"),
            checkout_tree=self.git("rev-parse", "HEAD^{tree}"),
            reviewed_tree=reviewed["tree"]["sha"],
            parent_sha=self.git("show", "-s", "--format=%P", "HEAD"),
            tool_commit=tool_commit,
            previous_pr=previous_pr,
            previous_commit=previous_commit,
            previous_reviewed=previous_reviewed,
            parent_facts=parent_facts,
            diagnostic_facts=diagnostic_facts,
            draft_read_facts=draft_read_facts,
            superseded_run=superseded_run,
            execution_runs=runs["workflow_runs"],
            now=self.now(),
        )
        self.validate_settings()
        return claim

    def validate_settings(self):
        r, p = self.remote, self.p
        immutable = r.get(f"{r.base}/immutable-releases", administration=True)
        protection = r.get(f"{r.base}/branches/main/protection", administration=True)
        require(immutable.get("enabled") is True, "RECOVERY_IMMUTABILITY_DISABLED")
        require(
            identity(protection) == p["protectionIdentity"],
            "RECOVERY_PROTECTION_CHANGED",
        )
        for path, expected in (
            (".github/workflows/release-drafter.yml", p["drafterWorkflowBlob"]),
            (".github/release-drafter.yml", p["drafterConfigBlob"]),
        ):
            observed = r.get(f"{r.base}/contents/{path}?ref=main")
            require(observed.get("sha") == expected, "RECOVERY_DRAFTER_CHANGED")
        ordinary = r.get(f"{r.base}/releases/{p['ordinaryDraft']}")
        require(
            ordinary.get("id") == p["ordinaryDraft"]
            and ordinary.get("draft") is True
            and ordinary.get("prerelease") is False
            and ordinary.get("published_at") is None
            and ordinary.get("assets") == [],
            "RECOVERY_ORDINARY_DRAFT_CHANGED",
        )

    def validate_active(self, claim):
        now = self.now()
        require(
            instant(claim["issuedAt"]) <= now < instant(claim["expiresAt"]),
            "RECOVERY_AUTHORIZATION_EXPIRED",
        )
        r = self.remote
        running = r.get(f"{r.base}/actions/runs/{claim['runId']}")
        require(
            running.get("id") == claim["runId"]
            and running.get("run_attempt") == 1
            and running.get("head_sha") == claim["toolSha"]
            and running.get("workflow_id") == claim["workflowId"]
            and running.get("status") == "in_progress"
            and running.get("conclusion") is None
            and running.get("actor", {}).get("id") == claim["operatorId"]
            and running.get("triggering_actor", {}).get("id") == claim["operatorId"],
            "RECOVERY_RUN_NO_LONGER_ACTIVE",
        )
        require(
            r.get(f"{r.base}/git/ref/heads/main")["object"]["sha"] == claim["toolSha"],
            "RECOVERY_TOOL_DRIFT",
        )
        require(
            self.git("rev-parse", "HEAD") == claim["toolSha"]
            and self.git("rev-parse", "HEAD^{tree}") == claim["toolTree"]
            and not self.git("status", "--porcelain", "--untracked-files=all"),
            "RECOVERY_TOOL_DRIFT",
        )
        for state in ("queued", "in_progress", "waiting", "requested", "pending"):
            runs = r.listed(f"{r.base}/actions/runs?status={state}", "workflow_runs")[
                "workflow_runs"
            ]
            conflicts = [
                run
                for run in runs
                if run["id"] != claim["runId"]
                and (
                    run.get("path", "").startswith(".github/workflows/release")
                    or run.get("path", "") == ".github/workflows/promote-release.yml"
                )
            ]
            require(not conflicts, "RECOVERY_COMPETING_EXECUTION")
        return now

    def initial_draft_reads(self):
        """Same native-token discovery/asset path used again by execute precheck."""
        self.validate_settings()
        draft = self.remote.draft()
        # A by-tag 200 is not evidence of a complete authenticated collection.
        releases = self.remote.listed(f"{self.remote.base}/releases")
        require(
            all(
                type(r.get("tag_name")) is str
                and type(r.get("id")) is int
                and r["id"] > 0
                for r in releases
            ),
            "RECOVERY_INITIAL_DISCOVERY_CONFLICT",
        )
        matches = [
            r for r in releases if r.get("tag_name") == self.p["subject"]["release_tag"]
        ]
        require(
            len(matches) == 1 and matches[0]["id"] == draft["id"],
            "RECOVERY_INITIAL_DISCOVERY_CONFLICT",
        )
        require(
            draft.get("draft") is True
            and draft.get("immutable") is False
            and draft.get("published_at") is None
            and draft.get("assets") == [],
            "RECOVERY_INITIAL_DRAFT_CHANGED",
        )
        return {
            "status": "FIXED_DRAFT_READS_VERIFIED",
            "fullInspectionPassed": False,
            "ordinaryDraftId": self.p["ordinaryDraft"],
            "transactionDraftId": draft["id"],
            "credentialRole": "GITHUB_TOKEN",
            "draftState": "DRAFT_EMPTY",
            "canonicalDiscovery": "COMPLETE_UNIQUE",
            "assetList": "EMPTY",
            "platformReportedJobPermissions": None,
            "platformReportedJobPermissionsStatus": "UNKNOWN",
            "remoteMutations": 0,
        }

    def registry(self):
        values = {}
        for step in self.p["steps"]:
            if not step["name"].startswith("registry-"):
                continue
            repository, tag = step["remoteKey"].removeprefix("ghcr.io/").rsplit(":", 1)
            query = urllib.parse.urlencode(
                {"service": "ghcr.io", "scope": "repository:" + repository + ":pull"}
            )
            with urllib.request.urlopen(
                "https://ghcr.io/token?" + query, timeout=45
            ) as response:
                token = json.load(response)["token"]
            request = urllib.request.Request(
                "https://ghcr.io/v2/" + repository + "/manifests/" + tag,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
                },
            )
            from .publication_remote import _NoRedirect

            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=45
            ) as response:
                raw = response.read(4 * 1024 * 1024)
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                require(
                    response.status == 200
                    and response.headers.get("Docker-Content-Digest")
                    == digest
                    == step["expectedIdentity"],
                    "RECOVERY_REGISTRY_CHANGED",
                )
                values[step["name"]] = digest
        return values

    def tag(self):
        r, p = self.remote, self.p
        ref = r.get(f"{r.base}/git/ref/tags/{p['subject']['release_tag']}")
        require(
            ref.get("object")
            == {
                "sha": p["tagObject"],
                "type": "tag",
                "url": f"https://api.github.com/{r.base}/git/tags/{p['tagObject']}",
            },
            "RECOVERY_TAG_CHANGED",
        )
        tag = r.get(f"{r.base}/git/tags/{p['tagObject']}")
        require(
            tag.get("tag") == p["subject"]["release_tag"]
            and tag.get("message") == p["subject"]["release_tag"] + "\n"
            and tag.get("object", {}).get("sha") == p["subject"]["sha"]
            and tag.get("object", {}).get("type") == "commit",
            "RECOVERY_TAG_CHANGED",
        )
        return {"object": p["tagObject"], "commit": p["subject"]["sha"]}


class RecoveryPrecheck:
    def __init__(self, platform, claim, materials, backend, *, root: Path):
        self.platform, self.claim, self.materials, self.backend = (
            platform,
            claim,
            materials,
            backend,
        )
        self.root, self.current = root, None
        self.serial = 0
        self.guard = None

    def snapshot(self):
        p = policy()
        self.platform.validate_active(self.claim)
        self.platform.validate_settings()
        ledger = (
            self.guard.load(self.claim["operationId"])
            if self.guard
            else self.backend.load(self.claim["operationId"])
        )
        require(ledger is not None, "RECOVERY_EXISTING_TRANSACTION_MISSING")
        validate_bound_ledger(ledger)
        journal_head = (
            self.guard.head
            if self.guard
            else self.backend._remote_head(self.claim["operationId"])
        )
        root = Path(self.materials["assetRoot"])
        for name, item in {
            **p["plan"]["assets"],
            **p["plan"]["transport_assets"],
        }.items():
            require(
                file_digest(root / name) == (item["sha256"], item["size"]),
                "RECOVERY_LOCAL_ASSET_CHANGED",
            )
        require(
            file_digest(root / "release-notes.md")[0]
            == p["transaction"]["draft_body_sha256"],
            "RECOVERY_NOTES_CHANGED",
        )
        artifacts = self.platform.remote.listed(
            f"{self.platform.remote.base}/actions/runs/{p['subject']['qualification_run_id']}/artifacts",
            "artifacts",
        )
        for expected in p["qualificationArtifacts"].values():
            matches = [a for a in artifacts["artifacts"] if a["id"] == expected["id"]]
            require(
                len(matches) == 1
                and matches[0].get("expired") is False
                and all(matches[0][k] == v for k, v in expected.items()),
                "RECOVERY_QUALIFICATION_CHANGED",
            )
        draft = self.platform.remote.draft(published_allowed=True)
        draft_identity = {
            k: draft.get(k)
            for k in (
                "id",
                "tag_name",
                "target_commitish",
                "name",
                "body",
                "prerelease",
                "draft",
                "immutable",
            )
        }
        draft_identity["assets"] = [
            {k: item.get(k) for k in ("id", "name", "size", "digest", "state", "url")}
            for item in draft["assets"]
        ]
        for name, expected in (
            ("candidate-input.json", p["subject"]["candidate_input_sha256"]),
            ("verified-candidate.json", p["subject"]["verified_candidate_sha256"]),
            (
                "candidate-acceptance-receipt.json",
                p["subject"]["candidate_aggregate_sha256"],
            ),
        ):
            require(
                file_digest(root / name)[0] == expected,
                "RECOVERY_CANDIDATE_METADATA_CHANGED",
            )
        return {
            "subject": p["subject"],
            "materialBinding": self.materials["materialBinding"],
            "planDigest": p["transaction"]["plan_digest"],
            "journalIdentity": ledger["ledgerIdentity"],
            "journalHead": journal_head,
            "revision": ledger["revision"],
            "tag": self.platform.tag(),
            "registry": self.platform.registry(),
            "proofs": self.platform.remote.verify_proofs(root),
            "draft": draft_identity,
        }

    def refresh(self):
        started = self.platform.now()
        first = self.snapshot()
        second = self.snapshot()
        require(first == second, "RECOVERY_PRECHECK_SNAPSHOT_CHANGED")
        now = self.platform.now()
        expires = min(
            started + timedelta(seconds=policy()["precheckSeconds"]),
            instant(self.claim["expiresAt"]),
        )
        require(started <= now < expires, "RECOVERY_PRECHECK_EXPIRED")
        self.current = {
            "schema": "animemo.rc-recovery-preflight/v1",
            "claimBinding": self.claim["bindingDigest"],
            "toolSha": self.claim["toolSha"],
            "toolTree": self.claim["toolTree"],
            "observedAt": utc(started),
            "expiresAt": utc(expires),
            "snapshot": second,
        }
        self.current["identity"] = identity(self.current)
        self.serial += 1
        (self.root / f"precheck-{self.serial:03d}.json").write_bytes(
            canonical_json_bytes(self.current)
        )
        return self.current

    def final_send_check(self):
        # No network or material reads after this final check. Expiry here
        # stops the request; it cannot recursively refresh a pending write.
        now = self.platform.now()
        require(
            self.claim["mode"] == "execute"
            and instant(self.claim["issuedAt"])
            <= now
            < instant(self.claim["expiresAt"]),
            "RECOVERY_AUTHORIZATION_EXPIRED",
        )
        require(
            self.current is not None
            and self.current["claimBinding"] == self.claim["bindingDigest"]
            and instant(self.current["observedAt"])
            <= now
            < instant(self.current["expiresAt"]),
            "RECOVERY_PRECHECK_EXPIRED",
        )

    def before_write(self):
        now = self.platform.validate_active(self.claim)
        require(self.claim["mode"] == "execute", "RECOVERY_EXECUTION_NOT_AUTHORIZED")
        if self.current is None or now >= instant(self.current["expiresAt"]):
            self.refresh()
            now = self.platform.now()
        require(
            self.current["claimBinding"] == self.claim["bindingDigest"]
            and instant(self.current["observedAt"])
            <= now
            < instant(self.current["expiresAt"]),
            "RECOVERY_PRECHECK_EXPIRED",
        )
        # Live object identity is checked even while the bulk proof snapshot is
        # within its 900-second validity. Local bytes are rechecked by upload.
        draft = self.platform.remote.draft(published_allowed=True)
        tag = self.platform.tag()
        ledger = (
            self.guard.load(self.claim["operationId"])
            if self.guard
            else self.backend.load(self.claim["operationId"])
        )
        head = (
            self.guard.head
            if self.guard
            else self.backend._remote_head(self.claim["operationId"])
        )
        if self.guard is None:
            require(
                head == self.claim["initialHead"]
                and ledger["revision"] == self.claim["initialRevision"]
                and ledger["ledgerIdentity"] == self.claim["initialLedgerIdentity"],
                "RECOVERY_INITIAL_STATE_CHANGED",
            )
        finished = self.platform.now()
        require(
            now <= finished < instant(self.current["expiresAt"]),
            "RECOVERY_PRECHECK_EXPIRED",
        )
        observation = {
            "precheckIdentity": self.current["identity"],
            "claimBinding": self.claim["bindingDigest"],
            "observedAt": utc(finished),
            "journalHead": head,
            "revision": ledger["revision"],
            "journalIdentity": ledger["ledgerIdentity"],
            "draftId": draft["id"],
            "tag": tag,
        }
        with (self.root / "write-observations.jsonl").open("ab") as output:
            output.write(canonical_json_bytes(observation))


class RecoveryAdapter:
    def __init__(self, engine, intent):
        self.engine, self.intent = engine, intent

    def observe(self, intent):
        e = self.engine
        require(intent == self.intent, "RECOVERY_INTENT_CHANGED")
        if intent.name in policy()["readOnlyCommittedSteps"]:
            require(e.precheck.current is not None, "RECOVERY_PRECHECK_REQUIRED")
            return RemoteObservation.same(intent.expected_identity)
        release = e.remote.draft(published_allowed=True)
        if intent.name == "release-draft":
            actual = canonical_identity(
                {
                    "tag": release["tag_name"],
                    "title": release["name"],
                    "bodyDigest": "sha256:"
                    + hashlib.sha256(release["body"].encode()).hexdigest(),
                    "prerelease": release["prerelease"],
                }
            )
            return RemoteObservation.same(actual)
        if intent.name.startswith("release-asset-"):
            name = intent.remote_key.split(":asset:", 1)[1]
            return (
                RemoteObservation.same(intent.expected_identity)
                if any(a["name"] == name for a in release["assets"])
                else RemoteObservation.absent()
            )
        require(intent.name == "release-publish", "RECOVERY_STEP_INVALID")
        return (
            RemoteObservation.same(intent.expected_identity)
            if release["draft"] is False
            and release["immutable"] is True
            and len(release["assets"]) == 5
            else RemoteObservation.absent()
        )

    def mutate(self, intent):
        e = self.engine
        require(e.claim["mode"] == "execute", "RECOVERY_EXECUTION_NOT_AUTHORIZED")
        require(
            intent == self.intent
            and intent.name in policy()["remainingSteps"]
            and intent.name != "release-draft",
            "RECOVERY_MUTATION_FORBIDDEN",
        )
        e.precheck.before_write()
        ledger = e.guard.load(intent_scope := e.claim["operationId"])
        require(ledger["operationId"] == intent_scope, "RECOVERY_SCOPE_INVALID")
        step = next(s for s in ledger["steps"] if s["name"] == intent.name)
        require(
            step["state"] == "REQUEST_STARTED"
            and len(step["attempts"]) == 1
            and intent.name not in e.attempted,
            "RECOVERY_WRITE_ALREADY_ATTEMPTED",
        )
        e.attempted.add(intent.name)
        if intent.name == "release-publish":
            return e.remote.publish()
        name = intent.remote_key.split(":asset:", 1)[1]
        return e.remote.upload(Path(e.materials["assetRoot"]) / name)


class ExistingRCRecovery:
    def __init__(self, *, platform, backend, materials, root: Path):
        self.platform, self.remote, self.backend = platform, platform.remote, backend
        self.materials, self.root = materials, root
        self.claim = platform.execution()
        self.precheck = RecoveryPrecheck(
            platform, self.claim, materials, backend, root=root
        )
        self.attempted = set()
        self.guard = None
        if self.claim["mode"] == "execute":
            self.remote.before_send = self.precheck.final_send_check
            self.backend.before_push = self.precheck.final_send_check

    def run(self):
        self.precheck.refresh()
        if self.claim["mode"] == "inspect":
            return {
                "status": "INSPECTED",
                "context": self.claim,
                "remoteMutations": 0,
                "precheck": self.precheck.current,
            }
        self.guard = GuardedRecoveryJournal(
            self.backend,
            self.claim,
            head_reader=lambda: self.backend._remote_head(self.claim["operationId"]),
            clock=self.platform.now,
            before_write=self.precheck.before_write,
        )
        self.guard.claim_once()
        self.precheck.guard = self.guard
        # Relative asset locators intentionally preserve the original plan's
        # attestation intent identities. cwd is the owned recovery workspace.
        runtime = build_publication_runtime(
            self.materials["plan"],
            source_tree=policy()["subject"]["tree"],
            asset_root=Path("release-output"),
            candidate_root=Path(self.materials["candidateRoot"]),
            repository_path=self.platform.repository,
        )
        adapters = {i.name: RecoveryAdapter(self, i) for i in runtime.intents}
        controller = DurablePublicationController.open(
            self.materials["plan"],
            source_tree=policy()["subject"]["tree"],
            intents=runtime.intents,
            journal=self.guard,
            adapters=adapters,
        )
        controller.preflight_all()
        for name in policy()["remainingSteps"]:
            ledger = controller.advance(name)
            require(
                next(s for s in ledger["steps"] if s["name"] == name)["committed"],
                "RECOVERY_STEP_NOT_COMMITTED",
            )
        final = controller.finalize()
        (
            Path(self.materials["assetRoot"]) / "publication-transaction-ledger.json"
        ).write_bytes(canonical_json_bytes(final))
        return {
            "schema": "animemo.existing-rc-recovery-execution/v2",
            "status": "TRANSACTION_COMPLETE",
            "claim": self.claim,
            "originalPublishRun": policy()["originalPublishRun"],
            "initialJournalHead": self.claim["initialHead"],
            "finalJournalHead": self.guard.head,
            "finalLedgerIdentity": final["ledgerIdentity"],
            "finalRevision": final["revision"],
            "journalAppendCount": self.guard.append_count,
            "writeRequests": self.remote.write_requests,
            "originalDraftPostCount": 0,
            "runtimeRebuildCount": 0,
            "completedAt": utc(self.platform.now()),
        }
