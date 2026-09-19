"""Shared release discovery under a verified, bounded read scope.

Hosted authority is acquired inside the reviewed job entry using its injected
GITHUB_TOKEN and fresh platform facts. A caller flag, receipt JSON or arbitrary
GH_TOKEN never grants this scope. It authorizes observation, not mutation.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .publication_remote import (GitHubReadError, GitHubReleaseReadbackError, _NoRedirect,
                                  read_github_response, strict_github_json)

REPOSITORY = "yanyuhanyue/AniMemo"
REPOSITORY_ID = 1327429673
OWNER_ID = 111261350
CONTROL_ID = 373784357
PREFIX = "repos/" + REPOSITORY
WINDOW_SECONDS = 60
_SEAL = object()
_CONTEXT = ("GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
            "GITHUB_JOB", "GITHUB_WORKFLOW_SHA", "GITHUB_WORKFLOW_REF", "GITHUB_REF", "GITHUB_EVENT_NAME",
            "GITHUB_ACTOR", "GITHUB_ACTOR_ID", "GITHUB_TRIGGERING_ACTOR", "GITHUB_ACTIONS")


def require(value, code="GITHUB_READ_AUTHORITY_UNVERIFIED"):
    if not value:
        raise ConnectionError(code)


def exact_id(value, expected=None):
    return type(value) is int and value>0 and (expected is None or value==expected)


def shape(value):
    return {dict:"object",list:"array",str:"string",bool:"boolean",int:"integer",float:"number",type(None):"null"}.get(type(value),"other")


def field_shape(value, field):
    present=isinstance(value,dict) and field in value
    item=value[field] if present else None
    return {"present":present,"type":shape(item) if present else "missing","boolean":item if type(item) is bool else None}


def page_links(response, page, endpoint):
    if response.link is None:
        return False
    relations={}
    for entry in response.link.split(","):
        match=re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*',entry)
        require(match is not None and match[2] not in relations,"GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")
        parsed=urllib.parse.urlsplit(match[1])
        try:query=urllib.parse.parse_qs(parsed.query,strict_parsing=True)
        except ValueError:raise ConnectionError("GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED") from None
        require(parsed.scheme=="https" and parsed.netloc=="api.github.com" and parsed.path=="/"+endpoint
                and not parsed.fragment and set(query)=={"per_page","page"} and query["per_page"]==["100"]
                and len(query["page"])==1 and re.fullmatch(r"[1-9][0-9]{0,2}",query["page"][0])
                and int(query["page"][0])<=100,"GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")
        number=int(query["page"][0]);relations[match[2]]=number
        require(match[2]!="next" or number==page+1,"GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")
    require(not (relations.get("last",page)>page and "next" not in relations)
            and relations.get("last",100)>=relations.get("next",page),"GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")
    return "next" in relations


class HostedGitHubReadClient:
    """GET-only transport owned by one reviewed running platform job."""
    def __init__(self, seal, token, context, *, opener=None, clock=time.monotonic):
        require(seal is _SEAL)
        self._token=token
        self._context=context
        self._opener=opener or urllib.request.build_opener(_NoRedirect())
        self._clock=clock
        self._born=clock()
        self._verified=False
        self._release_ids={CONTROL_ID}
        self._bootstrap_paths={}
        self.events=[]
        self.identity={}

    @classmethod
    def from_current_job(cls, *, audit_events=None):
        env=os.environ
        require(env.get("GITHUB_ACTIONS")=="true" and env.get("GITHUB_REPOSITORY")==REPOSITORY
                and env.get("GITHUB_REPOSITORY_ID")==str(REPOSITORY_ID)
                and env.get("GITHUB_ACTOR")=="yanyuhanyue" and env.get("GITHUB_ACTOR_ID")==str(OWNER_ID)
                and env.get("GITHUB_TRIGGERING_ACTOR")=="yanyuhanyue"
                and env.get("GITHUB_RUN_ATTEMPT")=="1")
        token=env.get("GITHUB_TOKEN")
        require(type(token) is str and bool(token))
        require(re.fullmatch(r"[1-9][0-9]*",env.get("GITHUB_RUN_ID","")))
        require(re.fullmatch(r"[0-9a-f]{40}",env.get("GITHUB_WORKFLOW_SHA","")))
        run_id=int(env["GITHUB_RUN_ID"])
        if env["GITHUB_JOB"]=="read-contract":
            workflow=".github/workflows/release-readback-check.yml";job_name="read-contract"
        elif env["GITHUB_JOB"]=="publish" and "/.github/workflows/release.yml@" in env["GITHUB_WORKFLOW_REF"]:
            workflow=".github/workflows/release.yml";job_name="publish-immutable-prerelease"
        elif env["GITHUB_JOB"]=="publish" and "/.github/workflows/promote-release.yml@" in env["GITHUB_WORKFLOW_REF"]:
            workflow=".github/workflows/promote-release.yml";job_name="promote-existing-rc-artifacts"
        else:
            raise ConnectionError("GITHUB_READ_JOB_UNREVIEWED")
        require(env["GITHUB_WORKFLOW_REF"]==REPOSITORY+"/"+workflow+"@"+env["GITHUB_REF"])
        root=Path(__file__).resolve().parents[1]
        local=subprocess.run(("git","rev-parse","HEAD"),cwd=root,capture_output=True,check=False,timeout=15)
        require(local.returncode==0)
        checkout=local.stdout.decode("ascii").strip()
        require(re.fullmatch(r"[0-9a-f]{40}",checkout))
        dirty=subprocess.run(("git","status","--porcelain","--untracked-files=no"),cwd=root,capture_output=True,check=False,timeout=15)
        require(dirty.returncode==0 and not dirty.stdout,"GITHUB_READ_CHECKOUT_DIRTY")
        client=cls(_SEAL,token,{key:env.get(key) for key in _CONTEXT})
        if audit_events is not None:
            require(type(audit_events) is list and not audit_events)
            client.events=audit_events
        run_path=PREFIX+f"/actions/runs/{run_id}"
        jobs_path=run_path+"/jobs?per_page=100&page=1"
        source_path=PREFIX+"/contents/"+workflow+"?ref="+env["GITHUB_WORKFLOW_SHA"]
        client._bootstrap_paths={run_path:"2026-03-10",jobs_path:"2026-03-10",source_path:"2026-03-10",
                                 PREFIX+"/git/ref/heads/main":"2026-03-10"}
        run=client._object(run_path)
        require(exact_id(run.get("id"),run_id) and exact_id(run.get("run_attempt"),1)
                and run.get("head_sha")==checkout and run.get("status")=="in_progress"
                and run.get("event")==env["GITHUB_EVENT_NAME"] and run.get("path")==workflow
                and exact_id(run.get("repository",{}).get("id"),REPOSITORY_ID)
                and exact_id(run.get("head_repository",{}).get("id"),REPOSITORY_ID)
                and exact_id(run.get("actor",{}).get("id"),OWNER_ID)
                and exact_id(run.get("triggering_actor",{}).get("id"),OWNER_ID))
        main=client._object(PREFIX+"/git/ref/heads/main")["object"]["sha"]
        if env["GITHUB_EVENT_NAME"]=="pull_request":
            require(workflow==".github/workflows/release-readback-check.yml")
            match=re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge",env["GITHUB_REF"])
            require(match is not None)
            pr_path=PREFIX+"/pulls/"+match[1];client._bootstrap_paths[pr_path]="2022-11-28"
            pr=client._object(pr_path)
            require(pr.get("state")=="open" and pr.get("draft") is False
                    and pr["head"]["sha"]==checkout and pr["base"]["sha"]==main
                    and exact_id(pr["head"]["repo"].get("id"),REPOSITORY_ID)
                    and exact_id(pr["base"]["repo"].get("id"),REPOSITORY_ID)
                    and pr["head"]["repo"].get("fork") is False and pr["base"]["repo"].get("fork") is False
                    and exact_id(pr["user"].get("id"),OWNER_ID))
            markers=re.findall(r"<!-- animemo-reviewed-head: ([0-9a-f]{40}) -->",pr.get("body") or "")
            require(markers==[checkout],"GITHUB_READ_REVIEW_BINDING_INVALID")
        else:
            require(env["GITHUB_EVENT_NAME"]=="workflow_dispatch" and env["GITHUB_REF"]=="refs/heads/main"
                    and checkout==main==env["GITHUB_WORKFLOW_SHA"])
        jobs=client._object(jobs_path)
        require(type(jobs.get("total_count")) is int and 0<jobs["total_count"]<=100
                and type(jobs.get("jobs")) is list and len(jobs["jobs"])==jobs["total_count"]
                and all(type(job) is dict for job in jobs["jobs"]))
        own=[job for job in jobs["jobs"] if job.get("name")==job_name]
        require(len(own)==1 and own[0].get("status")=="in_progress" and exact_id(own[0].get("id")))
        source=client._object(source_path)
        require(source.get("type")=="file" and source.get("encoding")=="base64")
        body=base64.b64decode(source["content"].replace("\n",""),validate=True)
        require(body==(root/workflow).read_bytes(),"GITHUB_READ_WORKFLOW_SOURCE_MISMATCH")
        client.identity={"repository_id":REPOSITORY_ID,"run_id":run_id,"job_id":own[0]["id"],
                         "job":job_name,"checkout_sha":checkout,"workflow_sha":env["GITHUB_WORKFLOW_SHA"],
                         "workflow":workflow,"event":env["GITHUB_EVENT_NAME"],"scope":"RELEASE_READ_ONLY",
                         "permission_source":"REVIEWED_NATIVE_JOB_ENTRY_PLUS_LIVE_PLATFORM_BINDING",
                         "effective_permissions":"NOT_EXPOSED_BY_THIS_API; independently collect runner setup log"}
        client._verified=True
        return client

    def require_scope(self):
        require(self._verified and self._clock()-self._born<900
                and all(os.environ.get(key)==value for key,value in self._context.items())
                and os.environ.get("GITHUB_TOKEN")==self._token,"GITHUB_READ_SCOPE_EXPIRED_OR_CHANGED")

    def _object(self,endpoint):
        response=self.request("GET",endpoint,None)
        require(response.status==200)
        value=strict_github_json(response.body)
        require(type(value) is dict)
        return value

    def _endpoint(self,method,endpoint,payload):
        require(method=="GET" and payload is None and type(endpoint) is str,"GITHUB_READ_ENDPOINT_DENIED")
        if endpoint in self._bootstrap_paths:
            return "PLATFORM_BINDING",self._bootstrap_paths[endpoint],None
        if endpoint==PREFIX:return "REPOSITORY","2026-03-10",None
        if endpoint==PREFIX+"/releases/latest":return "LATEST","2026-03-10",None
        if re.fullmatch(re.escape(PREFIX)+r"/releases/tags/v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:rc|beta)\.[1-9][0-9]*)?",endpoint):
            return "BY_TAG","2026-03-10",None
        match=re.fullmatch(re.escape(PREFIX)+r"/releases\?per_page=100&page=([1-9][0-9]{0,2})",endpoint)
        if match and int(match[1])<=100:return "LIST","2026-03-10",int(match[1])
        for number in self._release_ids:
            if endpoint==PREFIX+"/releases/"+str(number):return "CONTROL_ID" if number==CONTROL_ID else "UNIQUE_ID_READBACK","2026-03-10",None
            match=re.fullmatch(re.escape(PREFIX+"/releases/"+str(number))+r"/assets\?per_page=100&page=([1-9][0-9]{0,2})",endpoint)
            if match and int(match[1])<=100:return "ASSET_LIST","2026-03-10",int(match[1])
        raise ConnectionError("GITHUB_READ_ENDPOINT_DENIED")

    def request(self,method,endpoint,payload):
        kind,version,page=self._endpoint(method,endpoint,payload)
        if self._verified:self.require_scope()
        row={"ordinal":len(self.events)+1,"utc":datetime.now(timezone.utc).isoformat(),"endpoint_class":kind,
             "credential_role":"NATIVE_CURRENT_JOB_GITHUB_TOKEN","requested_version":version,"selected_version":None,
             "status":None,"request_id":None,"page":page,"body_bytes":None,"transport":"NO_RESPONSE"}
        self.events.append(row)
        request=urllib.request.Request("https://api.github.com/"+endpoint,method="GET",headers={
            "Authorization":"Bearer "+self._token,"Accept":"application/vnd.github+json",
            "X-GitHub-Api-Version":version,"User-Agent":"AniMemo-publication-transaction/1"})
        try:
            try:response=self._opener.open(request,timeout=45)
            except urllib.error.HTTPError as error:response=error
            with response:result=read_github_response(response)
        except GitHubReadError as error:
            self._headers(row,error.response);row["transport"]=error.reason
            raise ConnectionError("GITHUB_READ_BODY_UNVERIFIED") from None
        except (OSError, http.client.HTTPException):
            raise ConnectionError("GITHUB_READ_TRANSPORT_UNKNOWN") from None
        self._headers(row,result);row.update(body_bytes=len(result.body),transport="COMPLETE")
        require(result.selected_version==version,"GITHUB_READ_API_VERSION_MISMATCH")
        try:value=strict_github_json(result.body);row["json_type"]=shape(value)
        except (ValueError,UnicodeError):row["json_type"]="invalid";return result
        if kind=="REPOSITORY":
            row["permissions"]=field_shape(value,"permissions")
            row["push"]=field_shape(value.get("permissions") if type(value) is dict else None,"push")
        if kind in {"LIST","ASSET_LIST"}:row["item_count"]=len(value) if type(value) is list else None
        return result

    @staticmethod
    def _headers(row,response):
        row["status"]=response.status
        for attr,pattern in [("selected_version",r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),("request_id",r"[a-fA-F0-9:]{1,128}")]:
            value=getattr(response,attr);row[attr]=value if type(value) is str and re.fullmatch(pattern,value) else None
        for attr in ("rate_remaining","rate_reset","retry_after"):
            value=getattr(response,attr);row[attr]=int(value) if type(value) is str and re.fullmatch(r"[0-9]{1,16}",value) else None


class GitHubReleaseDiscovery:
    """One bounded phase snapshot shared by Draft, five assets and Publish."""
    def __init__(self,repository,tag,request,*,clock=time.monotonic):
        self.repository,self.tag,self.request=repository,tag,request
        owner=getattr(request,"__self__",None)
        self.client=owner if isinstance(owner,HostedGitHubReadClient) else None
        self.clock=clock
        self.invalidate()

    def invalidate(self):
        self._valid=False;self._value=None;self._until=0

    def _read(self,endpoint,stage,*,missing=False):
        try:response=self.request("GET",endpoint,None)
        except ConnectionError:raise GitHubReleaseReadbackError("GITHUB_RELEASE_"+stage+"_HTTP_UNVERIFIED") from None
        if missing and response.status==404:return None
        if response.status!=200:raise GitHubReleaseReadbackError("GITHUB_RELEASE_"+stage+"_HTTP_UNVERIFIED")
        try:value=strict_github_json(response.body)
        except (ValueError,UnicodeError):raise GitHubReleaseReadbackError("GITHUB_RELEASE_"+stage+"_JSON_UNVERIFIED") from None
        if type(value) is not dict:raise GitHubReleaseReadbackError("GITHUB_RELEASE_"+stage+"_JSON_UNVERIFIED")
        return value

    def _list(self,endpoint,*,assets=False):
        collected=[];ids=set()
        for page in range(1,101):
            try:response=self.request("GET",endpoint+f"?per_page=100&page={page}",None)
            except ConnectionError:raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_HTTP_UNVERIFIED") from None
            if response.status!=200:raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_HTTP_UNVERIFIED")
            try:value=strict_github_json(response.body)
            except (ValueError,UnicodeError):raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_JSON_UNVERIFIED") from None
            try:more=page_links(response,page,endpoint)
            except (ConnectionError,ValueError):raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED") from None
            if type(value) is not list or len(value)>100:raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_ITEM_SHAPE_UNVERIFIED")
            for item in value:
                if type(item) is not dict or not exact_id(item.get("id")) or item["id"] in ids or type(item.get("name" if assets else "tag_name")) is not str:
                    raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_ITEM_SHAPE_UNVERIFIED")
                ids.add(item["id"]);collected.append(item)
                if self.client:
                    if assets:
                        require(type(item.get("size")) is int and item["size"]>=0
                                and item.get("state")=="uploaded", "GITHUB_READ_ASSET_SHAPE_UNVERIFIED")
                    else:
                        require(type(item.get("draft")) is bool and type(item.get("updated_at")) is str
                                and "published_at" in item and (item["published_at"] is None
                                or type(item["published_at"]) is str), "GITHUB_READ_COLLECTION_SHAPE_UNVERIFIED")
            if self.client:
                self.client.events[-1].update(has_next=more,target_match_count=sum(i.get("tag_name")==self.tag for i in collected))
            if more and not value:raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")
            # A validated Link with no next explicitly closes a full last page.
            if not more and (len(value)<100 or response.link is not None):return collected
        raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")

    @staticmethod
    def _control(value):
        require(type(value) is dict and exact_id(value.get("id"),CONTROL_ID) and value.get("draft") is True
                and value.get("prerelease") is False and value.get("immutable") is False
                and "published_at" in value and value["published_at"] is None
                and type(value.get("tag_name")) is str and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+",value["tag_name"])
                and value.get("target_commitish")=="main" and value.get("assets")==[]
                and type(value.get("updated_at")) is str and type(value.get("body")) is str,
                "GITHUB_READ_CONTROL_UNVERIFIED")
        return {key:value[key] for key in ("id","tag_name","draft","prerelease","immutable","published_at","updated_at","target_commitish","body","assets")}

    def get(self,*,force=False):
        if self.client:self.client.require_scope()
        if self.client and not force and self._valid and self.clock()<self._until:return copy.deepcopy(self._value)
        self.invalidate();start=self.clock()
        root="repos/"+self.repository
        try:
            before=None
            if self.client:
                require(self.repository==REPOSITORY)
                repository=self._read(root,"REPOSITORY")
                require(exact_id(repository.get("id"),REPOSITORY_ID) and repository.get("full_name")==REPOSITORY
                        and type(repository.get("owner")) is dict
                        and exact_id(repository.get("owner",{}).get("id"),OWNER_ID)
                        and repository.get("owner",{}).get("login")=="yanyuhanyue")
                before=self._control(self._read(root+f"/releases/{CONTROL_ID}","CONTROL"))
            published=self._read(root+"/releases/tags/"+urllib.parse.quote(self.tag,safe=""),"BY_TAG",missing=True)
            if published is not None and not self.client:
                result=published
            else:
                items=self._list(root+"/releases")
                matches=[item for item in items if item["tag_name"]==self.tag]
                if len(matches)>1:raise GitHubReleaseReadbackError("GITHUB_RELEASE_LIST_ITEM_SHAPE_UNVERIFIED")
                if self.client:
                    controls=[v for v in items if v["id"]==CONTROL_ID]
                    require(len(controls)==1 and self._control(controls[0])==before)
                    second=self._list(root+"/releases")
                    # Compare parsed values, not order; they are observations,
                    # not a transaction snapshot guaranteed by GitHub.
                    def collection_identity(values):
                        return sorted([(v["id"],v["tag_name"],v.get("draft"),v.get("updated_at"),v.get("published_at")) for v in values],key=lambda v:v[0])
                    require(collection_identity(items)==collection_identity(second),"GITHUB_READ_COLLECTION_DRIFT")
                    controls_after=[v for v in second if v["id"]==CONTROL_ID]
                    require(len(controls_after)==1 and self._control(controls_after[0])==before,"GITHUB_READ_CONTROL_DRIFT")
                    require(self._control(self._read(root+f"/releases/{CONTROL_ID}","CONTROL"))==before,"GITHUB_READ_CONTROL_DRIFT")
                if not matches:
                    if published is not None:raise ConnectionError("GITHUB_READ_TARGET_DRIFT")
                    if not self.client:
                        repository=self._read(root,"REPOSITORY",missing=True)
                        permissions=repository.get("permissions") if repository else None
                        if type(permissions) is not dict or type(permissions.get("push")) is not bool:
                            raise GitHubReleaseReadbackError("GITHUB_RELEASE_PERMISSIONS_SHAPE_UNVERIFIED")
                        if permissions["push"] is not True:raise GitHubReleaseReadbackError("GITHUB_RELEASE_DRAFT_VISIBILITY_UNVERIFIED")
                    result=None
                else:
                    target=matches[0]
                    if self.client:self.client._release_ids.add(target["id"])
                    result=self._read(root+"/releases/"+str(target["id"]),"UNIQUE_ID_READBACK",missing=True)
                    if result is None or not exact_id(result.get("id"),target["id"]) or result.get("tag_name")!=self.tag:
                        raise GitHubReleaseReadbackError("GITHUB_RELEASE_UNIQUE_ID_READBACK_UNVERIFIED")
                    if self.client:
                        require(type(result.get("draft")) is bool and result.get("draft")==target.get("draft")
                                and result.get("updated_at")==target.get("updated_at"),"GITHUB_READ_TARGET_DRIFT")
                        require(published is None or published.get("id")==target["id"],"GITHUB_READ_TARGET_DRIFT")
                        require(published is not None or result["draft"] is True,"GITHUB_READ_TARGET_DRIFT")
                        result["assets"]=self._list(root+"/releases/"+str(target["id"])+"/assets",assets=True)
            require(self.clock()-start<WINDOW_SECONDS,"GITHUB_READ_WINDOW_EXPIRED")
            self._value=copy.deepcopy(result);self._until=start+WINDOW_SECONDS;self._valid=True
            return copy.deepcopy(result)
        except (ConnectionError,ValueError):
            self.invalidate()
            raise
