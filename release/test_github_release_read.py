"""Synthetic HTTP/platform fixtures; never evidence of live hosted authority."""
import base64
import copy
import http.client
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.github_release_read import (
    CONTROL_ID, PREFIX, REPOSITORY, HostedGitHubReadClient, GitHubReleaseDiscovery,
)
from release.publication_remote import (
    GitHubDraftAdapter, GitHubAssetAdapter, GitHubPublishAdapter, GitHubReadError,
    read_github_response, strict_github_json, CommandResult,
)
from release.publication_transaction import MutationIntent, ObservationClass
from release.readback_check import TARGET, observe_contract

ROOT=Path(__file__).parents[1]
SHA='a'*40
MAIN='b'*40
WORKFLOW='.github/workflows/release-readback-check.yml'


class Response(io.BytesIO):
    def __init__(self,value,status=200,*,raw=False,link=None):
        super().__init__(value if raw else json.dumps(value).encode())
        self.status=status
        self.headers={'X-GitHub-Api-Version-Selected':'2026-03-10','X-GitHub-Request-Id':'ABC:123'}
        if link is not None:self.headers['Link']=link


class HostedFixture:
    """HTTP requests enter the real scope factory, parser and seven observers."""
    def __init__(self):
        self.calls=[]
        self.env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPOSITORY,'GITHUB_REPOSITORY_ID':'1327429673',
                  'GITHUB_RUN_ID':'987','GITHUB_RUN_ATTEMPT':'1','GITHUB_JOB':'read-contract',
                  'GITHUB_WORKFLOW_SHA':'c'*40,'GITHUB_WORKFLOW_REF':REPOSITORY+'/'+WORKFLOW+'@refs/pull/263/merge',
                  'GITHUB_REF':'refs/pull/263/merge','GITHUB_EVENT_NAME':'pull_request',
                  'GITHUB_ACTOR':'yanyuhanyue','GITHUB_ACTOR_ID':'111261350','GITHUB_TRIGGERING_ACTOR':'yanyuhanyue',
                  'GITHUB_TOKEN':'SECRET_SENTINEL_NATIVE'}
        repo={'id':1327429673,'full_name':REPOSITORY,'fork':False,'owner':{'id':111261350,'login':'yanyuhanyue'},
              'permissions':{'push':False}}
        self.control={'id':CONTROL_ID,'tag_name':'v2.0.0','draft':True,'prerelease':False,'immutable':False,
                      'published_at':None,'updated_at':'2026-09-19T11:00:00Z','target_commitish':'main','body':'current notes','assets':[]}
        self.target={'id':999,'tag_name':TARGET,'name':TARGET,'draft':True,'prerelease':True,'immutable':False,
                     'body':'notes','updated_at':'2026-09-19T11:00:00Z','published_at':None,'assets':[]}
        self.data={
            PREFIX+'/actions/runs/987':{'id':987,'run_attempt':1,'head_sha':SHA,'status':'in_progress','event':'pull_request',
                'path':WORKFLOW,'repository':repo,'head_repository':repo,'actor':{'id':111261350},'triggering_actor':{'id':111261350}},
            PREFIX+'/git/ref/heads/main':{'object':{'sha':MAIN}},
            PREFIX+'/pulls/263':{'state':'open','draft':False,'head':{'sha':SHA,'repo':repo},'base':{'sha':MAIN,'repo':repo},
                'user':{'id':111261350},'body':'<!-- animemo-reviewed-head: '+SHA+' -->'},
            PREFIX+'/actions/runs/987/jobs?per_page=100&page=1':{'total_count':1,'jobs':[{'id':654,'name':'read-contract','status':'in_progress'}]},
            PREFIX+'/contents/'+WORKFLOW+'?ref='+'c'*40:{'type':'file','encoding':'base64','content':base64.b64encode((ROOT/WORKFLOW).read_bytes()).decode()},
            PREFIX:repo,
            PREFIX+'/releases/'+str(CONTROL_ID):self.control,
            PREFIX+'/releases/tags/'+TARGET:lambda:Response({},404),
            PREFIX+'/releases?per_page=100&page=1':[self.control],
            PREFIX+'/releases/999':self.target,
            PREFIX+'/releases/999/assets?per_page=100&page=1':[],
        }

    def open(self,request,timeout):
        assert request.get_method()=='GET' and request.data is None
        assert request.full_url.startswith('https://api.github.com/'+PREFIX)
        assert request.get_header('Authorization')=='Bearer SECRET_SENTINEL_NATIVE'
        self.calls.append(request)
        path=request.full_url.removeprefix('https://api.github.com/')
        value=self.data[path]
        if isinstance(value,Exception):raise value
        response=value() if callable(value) else Response(value)
        if path==PREFIX+'/pulls/263':response.headers['X-GitHub-Api-Version-Selected']='2022-11-28'
        return response

    def git(self,argv,**kwargs):
        if tuple(argv)==('git','rev-parse','HEAD'):return SimpleNamespace(returncode=0,stdout=(SHA+'\n').encode())
        if tuple(argv)==('git','status','--porcelain','--untracked-files=no'):return SimpleNamespace(returncode=0,stdout=b'')
        raise AssertionError('Unexpected subprocess (no writes allowed)')

    def enter(self,test):
        for patch in (mock.patch.dict(os.environ,self.env,clear=True),
                      mock.patch('urllib.request.build_opener',return_value=self),
                      mock.patch('subprocess.run',side_effect=self.git)):
            patch.start();test.addCleanup(patch.stop)
        return self


class HostedReadTests(unittest.TestCase):
    def setUp(self):self.fx=HostedFixture().enter(self)

    def client(self):return HostedGitHubReadClient.from_current_job()

    def draft(self,client):
        adapter=GitHubDraftAdapter(repository=REPOSITORY,tag=TARGET,title=TARGET,body=b'notes',prerelease=True,
                                  request=client.request)
        return adapter,MutationIntent('release-draft','GITHUB_RELEASE_DRAFT','key',adapter.identity)

    def test_native_false_scope_reaches_seven_real_observers_and_shared_cache(self):
        client=self.client()
        with mock.patch('release.publication_transaction.GitRemoteAppendOnlyJournal.append',side_effect=AssertionError('write')):
            result=observe_contract(client)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(len(result['observers']),7)
        self.assertEqual(len(self.fx.calls),11)  # five bindings + six shared discovery reads
        self.assertIs(next(e for e in client.events if e['endpoint_class']=='REPOSITORY')['push']['boolean'],False)
        self.assertNotIn('SECRET_SENTINEL',json.dumps(result))

    def test_arbitrary_token_or_flag_cannot_acquire_scope(self):
        for key,value in [('GITHUB_TOKEN',''),('GITHUB_ACTIONS','false'),('GITHUB_ACTOR_ID','9'),
                          ('GITHUB_RUN_ATTEMPT','2'),('GITHUB_REPOSITORY_ID','9'),('GITHUB_JOB','other')]:
            with self.subTest(key=key),mock.patch.dict(os.environ,{key:value}),self.assertRaises((ConnectionError,KeyError)):
                self.client()
        with self.assertRaises(ConnectionError):HostedGitHubReadClient(True,'token',{})

    def test_platform_binding_mismatches_fail_before_observation(self):
        cases=[('/actions/runs/987','head_sha',MAIN),('/actions/runs/987','run_attempt',True),
               ('/actions/runs/987','status','completed'),('/actions/runs/987','repository',{'id':True}),
               ('/pulls/263','draft',True),('/pulls/263','body',''),
               ('/actions/runs/987/jobs?per_page=100&page=1','total_count',2),
               ('/contents/'+WORKFLOW+'?ref='+'c'*40,'content',base64.b64encode(b'wrong source').decode())]
        for path,key,value in cases:
            original=self.fx.data[PREFIX+path]
            with self.subTest(path=path,key=key):
                self.fx.data[PREFIX+path]=original|{key:value}
                with self.assertRaises((ConnectionError,ValueError,KeyError,TypeError,AttributeError)):self.client()
                self.fx.data[PREFIX+path]=original

    def test_missing_and_limited_control_never_absent(self):
        original=copy.deepcopy(self.fx.data)
        cases=[(PREFIX,{'id':999}),
               (PREFIX+'/releases/'+str(CONTROL_ID),lambda:Response({},404)),
               (PREFIX+'/releases/'+str(CONTROL_ID),self.fx.control|{'draft':False}),
               (PREFIX+'/releases?per_page=100&page=1',[])]
        for path,value in cases:
            self.fx.data=original|{path:value}
            adapter,intent=self.draft(self.client())
            self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)

    def test_every_http_stage_failure_is_unknown_and_redacted(self):
        paths=[PREFIX,PREFIX+'/releases/'+str(CONTROL_ID),PREFIX+'/releases/tags/'+TARGET,
               PREFIX+'/releases?per_page=100&page=1']
        for path in paths:
            original=self.fx.data[path]
            for failure in [401,403,429,500,503,302,OSError('SECRET_SENTINEL_EXCEPTION')]:
                with self.subTest(path=path,failure=type(failure).__name__):
                    self.fx.data[path]=(lambda s=failure:Response({'message':'SECRET_SENTINEL_BODY'},s)) if type(failure) is int else failure
                    client=self.client();adapter,intent=self.draft(client)
                    self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)
                    self.assertNotIn('SECRET_SENTINEL',json.dumps(client.events))
            self.fx.data[path]=original

    def test_malformed_json_and_collection_types(self):
        path=PREFIX+'/releases?per_page=100&page=1'
        cases=[lambda:Response(b'[{"id":1,"id":2}]',raw=True),lambda:Response(b'NaN',raw=True),{},[None],
               [self.fx.control,self.fx.control],[self.fx.control|{'id':True}],
               [self.fx.control|{'draft':1}],[self.fx.control|{'updated_at':None}]]
        for value in cases:
            self.fx.data[path]=value;adapter,intent=self.draft(self.client())
            self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)

    def test_unique_id_and_assets_verified_once_for_all_observers(self):
        self.fx.data[PREFIX+'/releases?per_page=100&page=1']=[self.fx.control,self.fx.target]
        client=self.client();discovery=GitHubReleaseDiscovery(REPOSITORY,TARGET,client.request)
        self.assertEqual(discovery.get()['id'],999)
        calls=len(self.fx.calls)
        for _ in range(7):self.assertEqual(discovery.get()['id'],999)
        self.assertEqual(len(self.fx.calls),calls)
        for target in [self.fx.target|{'id':True},self.fx.target|{'tag_name':'other'},self.fx.target|{'draft':False},
                       self.fx.target|{'updated_at':'changed'},lambda:Response({},404)]:
            self.fx.data[PREFIX+'/releases/999']=target
            adapter,intent=self.draft(client)
            self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)

    def test_duplicate_target_and_unsafe_pagination(self):
        path=PREFIX+'/releases?per_page=100&page=1'
        for value in [[self.fx.control,self.fx.target,self.fx.target|{'id':998}],
                      lambda:Response([self.fx.control],link='<https://evil.invalid/x>; rel="next"'),
                      lambda:Response([self.fx.control],link='<https://api.github.com/'+PREFIX+'/releases?per_page=100&page=3>; rel="next"'),
                      lambda:Response([],link='<https://api.github.com/'+PREFIX+'/releases?per_page=100&page=2>; rel="next"')]:
            self.fx.data[path]=value;adapter,intent=self.draft(self.client())
            self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)

    def test_full_terminal_page_and_no_link_probe(self):
        rows=[self.fx.control]+[self.fx.control|{'id':n,'tag_name':'v1.0.'+str(n),'draft':False} for n in range(1,100)]
        path=PREFIX+'/releases?per_page=100&page=1'
        for link in ['<https://api.github.com/'+path+'>; rel="last"',None]:
            self.fx.data[path]=lambda l=link:Response(rows,link=l)
            self.fx.data[PREFIX+'/releases?per_page=100&page=2']=[]
            adapter,intent=self.draft(self.client())
            self.assertIs(adapter.observe(intent).classification,ObservationClass.ABSENT)

    def test_control_and_collection_drift_rejected_but_new_window_baseline_allowed(self):
        control_path=PREFIX+'/releases/'+str(CONTROL_ID)
        calls=[]
        def changed():
            calls.append(1)
            return Response(self.fx.control if len(calls)==1 else self.fx.control|{'body':'new'})
        self.fx.data[control_path]=changed
        adapter,intent=self.draft(self.client());self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)
        self.fx.control['updated_at']='2026-09-20T01:02:03Z'
        self.fx.data[control_path]=self.fx.control
        adapter,intent=self.draft(self.client());self.assertIs(adapter.observe(intent).classification,ObservationClass.ABSENT)
        calls.clear()
        def changed_list():
            calls.append(1)
            return Response([self.fx.control] if len(calls)==1 else [self.fx.control,self.fx.target])
        self.fx.data[PREFIX+'/releases?per_page=100&page=1']=changed_list
        adapter,intent=self.draft(self.client());self.assertIs(adapter.observe(intent).classification,ObservationClass.UNKNOWN)

    def test_scope_cache_expiry_identity_and_mutation_invalidation(self):
        client=self.client();clock=[0]
        discovery=GitHubReleaseDiscovery(REPOSITORY,TARGET,client.request,clock=lambda:clock[0])
        self.assertIsNone(discovery.get());calls=len(self.fx.calls)
        clock[0]=61;discovery.get();self.assertEqual(len(self.fx.calls),calls+6)
        with mock.patch.dict(os.environ,{'GITHUB_TOKEN':'other'}),self.assertRaises(ConnectionError):discovery.get()
        discovery.invalidate();self.assertFalse(discovery._valid)
        adapter=GitHubDraftAdapter(repository=REPOSITORY,tag=TARGET,title=TARGET,body=b'notes',prerelease=True,
                                  request=client.request,discovery=discovery)
        intent=MutationIntent('release-draft','GITHUB_RELEASE_DRAFT','key',adapter.identity)
        # A conflict appearing after cached absence cannot result in POST.
        self.fx.data[PREFIX+'/releases?per_page=100&page=1']=[self.fx.control,self.fx.target|{'body':'conflict'}]
        self.fx.data[PREFIX+'/releases/999']=self.fx.target|{'body':'conflict'}
        adapter.mutate(intent)
        self.assertFalse(discovery._valid)
        self.assertTrue(all(c.get_method()=='GET' for c in self.fx.calls))

    def test_get_allowlist_denies_writes_foreign_and_unobserved_ids(self):
        client=self.client();count=len(self.fx.calls)
        for method,path,payload in [('POST',PREFIX,None),('PATCH',PREFIX,{}),('DELETE',PREFIX,None),
                                    ('GET','https://api.github.com/'+PREFIX,None),('GET',PREFIX+'/releases/999',None),
                                    ('GET',PREFIX+'/../secrets',None),('GET',PREFIX,{}),('GET',PREFIX+'/releases?per_page=100&page=101',None)]:
            with self.assertRaises(ConnectionError):client.request(method,path,payload)
        self.assertEqual(count,len(self.fx.calls))

    def test_entry_only_writes_safe_exclusive_local_result(self):
        from release import readback_check
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'private'
            with (mock.patch.object(readback_check,'OUTPUT_ROOT',root),mock.patch('builtins.print'),
                  mock.patch.object(readback_check,'datetime',SimpleNamespace(now=lambda _:readback_check.START))):
                self.assertEqual(readback_check.main(),0)
                data=(root/'result.json').read_bytes()
                self.assertNotIn(b'SECRET_SENTINEL',data)
                with self.assertRaises(FileExistsError):readback_check.main()
                self.assertEqual((root/'result.json').read_bytes(),data)

    def test_actual_workflow_bootstrap_requires_reviewed_same_repository_event(self):
        import yaml
        workflow=yaml.load((ROOT/WORKFLOW).read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
        code=workflow['jobs']['read-contract']['steps'][0]['run'].split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        class FixedTime(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,9,19,12,0,0,tzinfo=timezone.utc)
        event={'action':'ready_for_review','pull_request':copy.deepcopy(self.fx.data[PREFIX+'/pulls/263'])}
        event['pull_request'].update(number=263)
        event['pull_request']['base']['ref']='main'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'event.json';output=Path(directory)/'output'
            for change in ['valid','duplicate-marker','fork','wrong-action']:
                value=copy.deepcopy(event)
                if change=='duplicate-marker':value['pull_request']['body']*=2
                if change=='fork':value['pull_request']['head']['repo']['fork']=True
                if change=='wrong-action':value['action']='synchronize'
                path.write_text(json.dumps(value),encoding='utf-8')
                with mock.patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(path),'GITHUB_OUTPUT':str(output)}),mock.patch('datetime.datetime',FixedTime):
                    if change=='valid':exec(compile(code,WORKFLOW,'exec'),{})
                    else:
                        with self.assertRaises(SystemExit):exec(compile(code,WORKFLOW,'exec'),{})
            self.assertEqual(output.read_text(),'checkout_sha='+SHA+'\n')


class HTTPFramingTests(unittest.TestCase):
    @staticmethod
    def response(raw):
        class Socket:
            def makefile(self,*args):return io.BytesIO(raw)
        response=http.client.HTTPResponse(Socket());response.begin();return response

    def test_real_chunked_eof_length_conflicts_and_budget(self):
        for raw in [b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n{}',
                    b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n2\r\n{}\r\n0\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}XX0\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\n{}\r\n0\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\n\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\nX-Trailer: value\r\n',
                    b'HTTP/1.1 200 OK\r\nLink: <a>; rel="last"\r\nLink: <b>; rel="next"\r\n\r\n{}',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n9\r\n{}']:
            with self.subTest(raw=raw[:65]),self.assertRaises(GitHubReadError):read_github_response(self.response(raw))
        for raw in [b'HTTP/1.1 200 OK\r\n\r\n{}',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\nX-Trailer: value\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n']:
            self.assertEqual(read_github_response(self.response(raw)).body,b'{}')
        with self.assertRaises(GitHubReadError):read_github_response(Response(b'{}x',raw=True),2)
        with self.assertRaises(GitHubReadError):read_github_response(Response(b'x'*(4*1024*1024+1),raw=True))

    def test_duplicate_keys_and_nonfinite_rejected(self):
        for raw in [b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":Infinity}']:
            with self.assertRaises(ValueError):strict_github_json(raw)


if __name__=='__main__':unittest.main()
