"""Negative version occupancy, never publication or Candidate authority.

The protected source decision preserves a failed transaction. Live verification
reads its canonical append-only history; it does not append or unfreeze it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

KIND = "FROZEN_UNATTEMPTED_VERSION_OCCUPANCY"
REPOSITORY = "yanyuhanyue/AniMemo"
FIELDS = {
    "kind", "repository", "releaseTag", "reusable", "candidateSha", "candidateTreeSha",
    "qualificationRunId", "publishRunId", "operationId", "journalRef", "journalHead",
    "journalRevision", "journalState", "ledgerIdentity", "ledgerFileSha256", "ledgerFileBytes",
    "steps", "decision",
}
_SEAL = object()


class FrozenOccupancyError(ValueError):
    pass


def require(value, code="FROZEN_OCCUPANCY_INVALID"):
    if not value:
        raise FrozenOccupancyError(code)


def digest(value):
    return type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def commit(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def identity(value):
    return "sha256:" + hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


def projected_steps(ledger):
    return [{key: copy.deepcopy(step[key]) for key in
             ("name", "kind", "remoteKey", "expectedIdentity", "state", "attempts", "committed")}
            for step in ledger["steps"]]


def validate_record(value):
    require(type(value) is dict and set(value) == FIELDS)
    require(value["kind"] == KIND and value["repository"] == REPOSITORY and value["reusable"] is False)
    require(type(value["releaseTag"]) is str and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*",value["releaseTag"]))
    require(all(commit(value[k]) for k in ("candidateSha","candidateTreeSha","journalHead")))
    require(all(digest(value[k]) for k in ("operationId","ledgerIdentity","ledgerFileSha256")))
    require(value["journalRef"] == "refs/heads/publication-transactions/" + value["operationId"][7:])
    require(all(type(value[k]) is int and value[k]>0 for k in ("qualificationRunId","publishRunId","ledgerFileBytes")))
    require(type(value["journalRevision"]) is int and value["journalRevision"]>=1 and value["journalState"]=="FROZEN")
    steps=value["steps"]
    require(type(steps) is list and len(steps)==17)
    names=set()
    for step in steps:
        require(type(step) is dict and set(step)=={"name","kind","remoteKey","expectedIdentity","state","attempts","committed"})
        require(type(step["name"]) is str and step["name"] not in names)
        names.add(step["name"])
        require(type(step["attempts"]) is list and not step["attempts"] and step["committed"] is False)
        require(step["state"] in {"WAITING","FROZEN"} and digest(step["expectedIdentity"]))
        require(type(step["remoteKey"]) is str and step["remoteKey"] and type(step["kind"]) is str)
    require(sum(step["state"]=="FROZEN" for step in steps)==1)
    decision=value["decision"]
    require(type(decision) is dict and set(decision)=={"action","authorization","issuedAt","sourceFile","sourceSha256"})
    require(decision["action"]=="EXCLUDE_VERSION_PRESERVE_FROZEN_TRANSACTION")
    require(type(decision["authorization"]) is str and re.fullmatch(r"ANIMEMO_[A-Z0-9_]+",decision["authorization"]))
    require(type(decision["issuedAt"]) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",decision["issuedAt"]))
    require(type(decision["sourceFile"]) is str and "/" not in decision["sourceFile"] and "\\" not in decision["sourceFile"] and digest(decision["sourceSha256"]))
    return copy.deepcopy(value)


class VerifiedFrozenOccupancies:
    """Issued only by a canonical live read; accepted only for exclusion."""
    def __init__(self, seal, records):
        require(seal is _SEAL,"FROZEN_OCCUPANCY_LIVE_READ_REQUIRED")
        self._records=copy.deepcopy(records)
        self._identities=tuple(identity(record) for record in records)

    def require_records(self, records):
        require(tuple(identity(record) for record in records)==self._identities,"FROZEN_OCCUPANCY_LIVE_BINDING_MISMATCH")

    def predecessors(self, target):
        return [{"releaseTag":v["releaseTag"],"operationId":v["operationId"],"journalHead":v["journalHead"],
                 "ledgerIdentity":v["ledgerIdentity"],"state":"FROZEN","published":False,"recordIdentity":identity(v)}
                for v in self._records if v["releaseTag"].split("-rc.")[0]==target.split("-rc.")[0]]


def frozen_records(config):
    require(type(config) is dict and type(config.get("reservations")) is list
            and all(type(v) is dict for v in config["reservations"]))
    return [validate_record(v) for v in config["reservations"] if v.get("kind")==KIND]


def verify_live(config, repository_path, *, remote="origin", run_git=None, active_plan=None, source_tree=None,
                preflight_target=None):
    """Read all journal refs, preserve history, reject every unexplained freeze.

    Verification does not invoke a controller, append, claim, push or mutate.
    Completed journals remain protected; any other unapproved in-flight journal
    blocks a successor even when a different version would otherwise be free.
    """
    from .publication_transaction import GitRemoteAppendOnlyJournal, _run_git_command
    runner=run_git or _run_git_command
    records=frozen_records(config)
    path=Path(repository_path)
    def git(*args):
        result=runner(("git","-C",str(path),*args),90,None,None)
        require(result.returncode==0,"FROZEN_OCCUPANCY_REMOTE_UNKNOWN")
        return result.stdout
    origin=git("remote","get-url",remote).decode("utf-8").strip()
    require(origin in {"https://github.com/"+REPOSITORY+".git","https://github.com/"+REPOSITORY,
                       "git@github.com:"+REPOSITORY+".git"},"FROZEN_OCCUPANCY_WRONG_REPOSITORY")
    preflight_source = None
    if preflight_target is not None:
        require(active_plan is None and type(preflight_target) is str
                and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-(?:rc|beta)\.[1-9][0-9]*",preflight_target))
        preflight_source = {"sha":git("rev-parse","HEAD").decode("ascii").strip(),
                            "tree":git("rev-parse","HEAD^{tree}").decode("ascii").strip()}
        require(all(commit(v) for v in preflight_source.values()))
    raw=git("ls-remote","--refs",remote,"refs/heads/publication-transactions/*").decode("ascii")
    refs={}
    for line in raw.splitlines():
        head,sep,ref=line.partition("\t")
        require(sep and commit(head) and re.fullmatch(r"refs/heads/publication-transactions/[0-9a-f]{64}",ref) and ref not in refs)
        refs[ref]=head
    require(len(refs)<=1000,"FROZEN_OCCUPANCY_RESOURCE_LIMIT")
    approved={record["journalRef"]:record for record in records}
    require(len(approved)==len(records) and set(approved).issubset(refs),"FROZEN_OCCUPANCY_REF_MISSING")
    journal=GitRemoteAppendOnlyJournal(path,remote=remote,run_git=runner)
    for ref,head in sorted(refs.items()):
        operation="sha256:"+ref.rsplit("/",1)[1]
        ledger=journal.load(operation)
        require(ledger is not None,"FROZEN_OCCUPANCY_REMOTE_UNKNOWN")
        require(git("rev-parse","FETCH_HEAD").decode("ascii").strip()==head,"FROZEN_OCCUPANCY_HEAD_DRIFT")
        record=approved.get(ref)
        if record is None:
            same_preflight = (preflight_source is not None and ledger["repository"]==REPOSITORY
                              and ledger["source"]==preflight_source and ledger["target"]["tag"]==preflight_target
                              and ledger["channel"]==preflight_target.split("-")[1].split(".")[0]
                              and ledger["finalState"]=="ACTIVE")
            same_active = (active_plan is not None and ledger["planIdentity"]==active_plan["identity"]
                           and ledger["source"]=={"sha":active_plan["commit"],"tree":source_tree}
                           and ledger["target"]["tag"]==active_plan["tag"] and ledger["finalState"]!="FROZEN")
            require(ledger["finalState"]=="COMPLETE" or same_active or same_preflight,"UNRESOLVED_PUBLICATION_TRANSACTION")
            continue
        data=git("show",head+":ledger.json")
        require(head==record["journalHead"] and len(data)==record["ledgerFileBytes"]
                and "sha256:"+hashlib.sha256(data).hexdigest()==record["ledgerFileSha256"],"FROZEN_OCCUPANCY_BYTES_MISMATCH")
        require(ledger["repository"]==record["repository"] and ledger["source"]=={"sha":record["candidateSha"],"tree":record["candidateTreeSha"]}
                and ledger["target"]["tag"]==record["releaseTag"] and ledger["operationId"]==record["operationId"]
                and ledger["revision"]==record["journalRevision"] and ledger["finalState"]==record["journalState"]
                and ledger["ledgerIdentity"]==record["ledgerIdentity"] and projected_steps(ledger)==record["steps"],"FROZEN_OCCUPANCY_LEDGER_MISMATCH")
    # Do not accept observations assembled across a changing ref inventory.
    require(git("ls-remote","--refs",remote,"refs/heads/publication-transactions/*").decode("ascii")==raw,"FROZEN_OCCUPANCY_HEAD_DRIFT")
    return VerifiedFrozenOccupancies(_SEAL,records)


def reject_frozen_remote_keys(intents, records):
    """Even a different version cannot overwrite an occupied source tag/key."""
    occupied={step["remoteKey"] for record in records for step in record["steps"]}
    require(not any(intent.remote_key in occupied for intent in intents),
            "FROZEN_PUBLICATION_REMOTE_KEY_CONFLICT")


def reject_frozen_target(tag):
    config=json.loads(Path(__file__).with_name("publication-reservations.json").read_text(encoding="utf-8"))
    require(tag not in {v["releaseTag"] for v in frozen_records(config)},"FROZEN_VERSION_NOT_REUSABLE")


def declared_predecessors(tag):
    if type(tag) is not str or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*",tag):
        return []
    base, sequence=tag.rsplit("-rc.",1)
    config=json.loads(Path(__file__).with_name("publication-reservations.json").read_text(encoding="utf-8"))
    return [{"releaseTag":v["releaseTag"],"operationId":v["operationId"],"journalHead":v["journalHead"],
             "ledgerIdentity":v["ledgerIdentity"],"state":"FROZEN","published":False,"recordIdentity":identity(v)}
            for v in frozen_records(config) if v["releaseTag"].rsplit("-rc.",1)[0]==base
            and int(v["releaseTag"].rsplit("-rc.",1)[1])<int(sequence)]


def verify_publication_target(tag, bump, channel, repository_path):
    """Validate receipt selection against the same live resolver before publish."""
    from .contract import resolve_prerelease, validate_publication_reservations
    from .publication_transaction import _run_git_command
    reject_frozen_target(tag)
    config=json.loads(Path(__file__).with_name("publication-reservations.json").read_bytes())
    validate_publication_reservations(config)
    proof=verify_live(config, repository_path, preflight_target=tag)
    result=_run_git_command(("git","-C",str(repository_path),"tag","--list","v*"),30,None,None)
    require(result.returncode==0,"PUBLICATION_TAG_INVENTORY_UNKNOWN")
    # Its own existing tag belongs to an idempotent transaction readback; every
    # other occupied sequence still participates in canonical resolution.
    tags=[v for v in result.stdout.decode("ascii").splitlines() if v!=tag]
    selected=resolve_prerelease(tags=tags,bump=bump,channel=channel,
                               publication_reservations=config,frozen_observations=proof)
    require(selected["releaseTag"]==tag,"PUBLICATION_VERSION_RESOLUTION_MISMATCH")
    return selected


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--target",required=True)
    parser.add_argument("--bump",required=True,choices=("patch","minor","major"))
    parser.add_argument("--channel",required=True,choices=("rc","beta"))
    args=parser.parse_args()
    # Use the canonical imported class identity even for a -m invocation.
    from release.frozen_occupancy import verify_publication_target as check
    try:
        print(json.dumps(check(args.target,args.bump,args.channel,Path.cwd())))
    except Exception:
        raise SystemExit("PUBLICATION_VERSION_OR_FROZEN_INVENTORY_UNVERIFIED") from None
