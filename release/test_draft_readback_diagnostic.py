"""No-network tests of the actual HTTP boundary and release observers."""
import copy
import http.client
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.draft_readback_diagnostic import (
    ASSETS, CONTROL, CONTROL_FIELDS, MAX_BODY, PREFIX, REPOSITORY, TAG, ReadOnlyProbe, run_probe,
)
from release.publication_remote import GitHubAssetAdapter, GitHubDraftAdapter, GitHubPublishAdapter
from release.publication_transaction import MutationIntent, ObservationClass


class Response(io.BytesIO):
    def __init__(self, value, status=200, *, link=None, raw=False, interrupted=False):
        super().__init__(value if raw else json.dumps(value).encode())
        self.status, self.interrupted = status, interrupted
        self.headers = {"X-GitHub-Request-Id": "ABC:123", "X-GitHub-Api-Version-Selected": "2026-03-10",
                        "X-RateLimit-Remaining": "49", "X-RateLimit-Reset": "1789813000", "Retry-After": "60"}
        if link is not None:
            self.headers["Link"] = link

    def read(self, size=-1):
        if self.interrupted:
            raise http.client.IncompleteRead(b"never-print-secret")
        return super().read(size)


class Opener:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        assert request.get_method() == "GET" and request.data is None
        assert request.full_url.startswith("https://api.github.com/" + PREFIX)
        assert request.get_header("X-github-api-version") == "2026-03-10"
        endpoint = request.full_url.removeprefix("https://api.github.com/")
        spec = self.responses[endpoint]
        if isinstance(spec, Exception):
            raise spec
        return spec()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.notes = Path(__file__).with_name("diagnostic-rc2-notes.txt").read_bytes()
        self.target = {"id": 999, "tag_name": TAG, "name": TAG, "body": self.notes.decode(),
                       "draft": True, "prerelease": True, "assets": []}
        self.control = copy.deepcopy(CONTROL_FIELDS)
        self.responses = {
            PREFIX + "/releases/tags/" + TAG: lambda: Response({}, 404),
            PREFIX + "/releases?per_page=100&page=1": lambda: Response([self.control]),
            PREFIX: lambda: Response({"id": 1327429673, "full_name": REPOSITORY, "permissions": {"push": True}}),
            PREFIX + "/releases/" + str(CONTROL): lambda: Response(self.control),
            PREFIX + "/releases/999": lambda: Response(self.target),
            PREFIX + "/releases/999/assets?per_page=100&page=1": lambda: Response([]),
        }

    def probe(self):
        self.opener = Opener(self.responses)
        return ReadOnlyProbe("SECRET_SENTINEL_credential", opener=self.opener)

    def observe(self, kind="draft", name=None):
        probe = self.probe()
        common = dict(repository=REPOSITORY, tag=TAG, request=probe)
        if kind == "draft":
            adapter = GitHubDraftAdapter(**common, title=TAG, body=self.notes, prerelease=True)
            intent = MutationIntent("release-draft", "GITHUB_RELEASE_DRAFT", "key", adapter.identity)
        elif kind == "asset":
            size, digest = ASSETS[name]
            adapter = GitHubAssetAdapter(**common, path=Path(name), expected_size=size, expected_digest="sha256:"+digest,
                                         run=lambda *_: self.fail("subprocess invoked"))
            intent = MutationIntent("release-asset-01", "GITHUB_RELEASE_ASSET", "key", "sha256:"+digest)
        else:
            adapter = GitHubPublishAdapter(**common, prerelease=True,
                expected_assets={n:{"size":s,"sha256":"sha256:"+d} for n,(s,d) in ASSETS.items()})
            intent = MutationIntent("release-publish", "GITHUB_RELEASE_PUBLISH", "key", adapter.identity)
        with mock.patch.dict(os.environ, {}, clear=True):
            return adapter.observe(intent), probe

    def test_original_absent_and_published_matching_behavior(self):
        result, probe = self.observe()
        self.assertIs(result.classification, ObservationClass.ABSENT)
        self.assertTrue(probe.collection_complete)
        self.responses[PREFIX + "/releases/tags/" + TAG] = lambda: Response(self.target | {"draft": False})
        result, _ = self.observe()
        self.assertIs(result.classification, ObservationClass.SAME)
        self.assertEqual(len(self.opener.calls), 1)

    def test_permissions_missing_null_types_and_false_stay_distinct(self):
        cases = [{}, {"permissions":None}, {"permissions":[]}, {"permissions":"bad"},
                 {"permissions":{}}, *({"permissions":{"push":v}} for v in [None, False, "true", 1, 0])]
        projections=[]
        for value in cases:
            with self.subTest(value=value):
                self.responses[PREFIX] = lambda v=value: Response(v)
                result, probe = self.observe()
                self.assertIs(result.classification, ObservationClass.UNKNOWN)
                self.assertIn(result.diagnostic_code, {"GITHUB_RELEASE_PERMISSIONS_SHAPE_UNVERIFIED", "GITHUB_RELEASE_DRAFT_VISIBILITY_UNVERIFIED"})
                projections.append(probe.events[-1])
        self.assertEqual(projections[0]["permissions"]["type"], "missing")
        self.assertEqual(projections[1]["permissions"]["type"], "null")
        self.assertEqual(projections[4]["push"]["type"], "missing")
        self.assertIs(projections[6]["push"]["boolean"], False)
        self.assertEqual(projections[8]["push"]["type"], "integer")

    def test_all_http_stages_and_transport_failures(self):
        for endpoint, stage in [(PREFIX+"/releases/tags/"+TAG, "BY_TAG"),
                                (PREFIX+"/releases?per_page=100&page=1", "LIST"), (PREFIX,"REPOSITORY")]:
            original=self.responses[endpoint]
            for status in [401,403,429,500,503]:
                with self.subTest(endpoint=endpoint,status=status):
                    self.responses[endpoint]=lambda s=status: Response({"message":"SECRET_SENTINEL_body"},s)
                    result,probe=self.observe()
                    self.assertIs(result.classification,ObservationClass.UNKNOWN)
                    self.assertEqual(result.diagnostic_code,"GITHUB_RELEASE_"+stage+"_HTTP_UNVERIFIED")
                    self.assertEqual(probe.events[-1]["status"],status)
            for failure in [OSError("SECRET_SENTINEL_exception"), lambda: Response({},interrupted=True)]:
                self.responses[endpoint]=failure
                result,probe=self.observe()
                self.assertIs(result.classification,ObservationClass.UNKNOWN)
                self.assertNotIn("SECRET_SENTINEL",json.dumps(probe.events))
                self.assertIn(probe.events[-1]["transport"], {"BODY_INTERRUPTED","NO_RESPONSE"})
            self.responses[endpoint]=original

    def test_json_item_and_pagination_failures(self):
        endpoint=PREFIX+"/releases?per_page=100&page=1"
        for factory in [lambda:Response(b'not json SECRET_SENTINEL',raw=True),lambda:Response({}),
                        lambda:Response([None]),lambda:Response([{"id":True,"tag_name":"x"}]),
                        lambda:Response([self.target,self.target]),lambda:Response([self.target,self.target|{"id":998}]),
                        lambda:Response([{"id":n,"tag_name":"x"} for n in range(1,102)])]:
            self.responses[endpoint]=factory
            result,probe=self.observe()
            self.assertIs(result.classification,ObservationClass.UNKNOWN)
            self.assertNotIn("SECRET_SENTINEL",json.dumps(probe.events))
        for link in ['<https://evil.test/SECRET_SENTINEL>; rel="next"',
                     '<https://api.github.com/'+PREFIX+'/releases?per_page=100&page=2>; rel="last"',
                     '<https://api.github.com/'+PREFIX+'/releases?per_page=100&page=3>; rel="next"', 'garbage']:
            self.responses[endpoint]=lambda l=link:Response([],link=l)
            result,probe=self.observe()
            self.assertIs(result.classification,ObservationClass.UNKNOWN)
            self.assertEqual(result.diagnostic_code,"GITHUB_RELEASE_LIST_PAGE_LINK_UNVERIFIED")
            self.assertIsNone(probe.target_id)
        self.responses[endpoint]=lambda:Response([self.target],link='<https://api.github.com/'+PREFIX+'/releases?per_page=100&page=2>; rel="next"')
        self.responses[PREFIX+'/releases?per_page=100&page=2']=lambda:Response({},403)
        result,_=self.observe()
        self.assertIs(result.classification,ObservationClass.UNKNOWN)

    def test_object_json_stage_codes(self):
        for endpoint,stage in [(PREFIX+'/releases/tags/'+TAG,'BY_TAG'),(PREFIX,'REPOSITORY')]:
            original=self.responses[endpoint]
            for factory in [lambda:Response(b'bad json',raw=True),lambda:Response([]),lambda:Response(None)]:
                self.responses[endpoint]=factory
                result,_=self.observe()
                self.assertIs(result.classification,ObservationClass.UNKNOWN)
                self.assertEqual(result.diagnostic_code,'GITHUB_RELEASE_'+stage+'_JSON_UNVERIFIED')
            self.responses[endpoint]=original

    def test_unique_readback_disappearance_and_identity_drift(self):
        self.responses[PREFIX+'/releases?per_page=100&page=1']=lambda:Response([self.target])
        for factory in [lambda:Response({},404),lambda:Response(self.target|{"id":998}),
                        lambda:Response(self.target|{"tag_name":"other"}),lambda:Response([]),
                        lambda:Response(b'bad',raw=True)]:
            self.responses[PREFIX+'/releases/999']=factory
            result,_=self.observe()
            self.assertIs(result.classification,ObservationClass.UNKNOWN)
            self.assertEqual(result.diagnostic_code,"GITHUB_RELEASE_UNIQUE_ID_READBACK_UNVERIFIED")

    def test_seven_observers_cross_same_transport_and_propagate_codes(self):
        self.responses[PREFIX]=lambda:Response({})
        for kind,name in [("draft",None),*(("asset",name) for name in ASSETS),("publish",None)]:
            with self.subTest(kind=kind,name=name):
                result,probe=self.observe(kind,name)
                self.assertIs(result.classification,ObservationClass.UNKNOWN)
                self.assertEqual(result.diagnostic_code,"GITHUB_RELEASE_PERMISSIONS_SHAPE_UNVERIFIED")
                self.assertEqual([r['endpoint_class'] for r in probe.events],["BY_TAG","LIST","REPOSITORY"])

    def test_get_allowlist_rejects_before_sending(self):
        probe=self.probe()
        for method,path,payload in [("POST",PREFIX,None),("PATCH",PREFIX,{}),("DELETE",PREFIX,None),
                                    ("GET","https://api.github.com/"+PREFIX,None),
                                    ("GET","https://evil.test/",None),("GET",PREFIX+'/../secrets',None),
                                    ("GET",PREFIX+'/releases/999',None),("GET",PREFIX+'/git/refs',None),
                                    ("GET",PREFIX,{"data":"x"})]:
            with self.assertRaisesRegex(ConnectionError,"DIAGNOSTIC_ENDPOINT_DENIED"):
                probe(method,path,payload)
        self.assertEqual(self.opener.calls,[])
        self.assertEqual(probe.events,[])

    def test_bounded_body_and_no_redirect(self):
        self.responses[PREFIX]=lambda:Response(b'x'*(MAX_BODY+1),raw=True)
        result,probe=self.observe()
        self.assertIs(result.classification,ObservationClass.UNKNOWN)
        self.assertEqual(probe.events[-1]['body_bytes'],MAX_BODY+1)
        self.assertEqual(probe.events[-1]['transport'],'BODY_LIMIT_EXCEEDED')
        self.responses[PREFIX]=lambda:Response({},302)
        result,probe=self.observe()
        self.assertIs(result.classification,ObservationClass.UNKNOWN)
        self.assertEqual(len(self.opener.calls),3)
        from release.publication_remote import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None,None,302,None,None,'https://evil.test'))

    def test_real_http_response_valid_json_prefix_is_not_complete(self):
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"permissions":{"push":true}}')
        def truncated():
            response=http.client.HTTPResponse(Socket())
            response.begin()
            return response
        self.responses[PREFIX]=truncated
        result,probe=self.observe()
        self.assertIs(result.classification,ObservationClass.UNKNOWN)
        self.assertEqual(probe.events[-1]['transport'],'BODY_INTERRUPTED')
        self.assertEqual(probe.events[-1]['status'],200)
        def oversized():
            response=Response({})
            response.headers['Content-Length']=str(MAX_BODY+2)
            return response
        self.responses[PREFIX]=oversized
        result,probe=self.observe()
        self.assertIs(result.classification,ObservationClass.UNKNOWN)
        self.assertEqual(probe.events[-1]['transport'],'BODY_LIMIT_EXCEEDED')

    def test_main_entry_cannot_reach_commands_or_journal(self):
        from release.draft_readback_diagnostic import main
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'animemo-draft-diagnostic';root.mkdir()
            (root/'identity.json').write_text(json.dumps({'checkout_sha':'a'*40}))
            opener=Opener(self.responses)
            with (mock.patch.dict(os.environ,{'GH_TOKEN':'SECRET_SENTINEL','RUNNER_TEMP':'/untrusted/ignored','CHECKOUT_SHA':'a'*40},clear=True),
                  mock.patch('release.draft_readback_diagnostic.OUTPUT_ROOT',root),
                  mock.patch('urllib.request.build_opener',return_value=opener),
                  mock.patch('subprocess.run',side_effect=AssertionError('command/git push denied')),
                  mock.patch('release.publication_transaction.GitRemoteAppendOnlyJournal.append',side_effect=AssertionError('journal denied')),
                  mock.patch('release.publication_transaction.DurablePublicationController.__init__',side_effect=AssertionError('publication/claim denied')),
                  mock.patch('builtins.print')):
                main()
                self.assertNotIn('GH_TOKEN',os.environ)
            output=(root/'diagnostic.json').read_text()
            self.assertNotIn('SECRET_SENTINEL',output)
            self.assertEqual(json.loads(output)['original_classifier']['classification'],'ABSENT')

    def test_output_directory_and_identity_symlinks_are_rejected(self):
        from release.draft_readback_diagnostic import main
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'output';root.mkdir()
            identity=root/'identity.json';identity.write_text(json.dumps({'checkout_sha':'a'*40}))
            with (mock.patch.dict(os.environ,{'GH_TOKEN':'sentinel','CHECKOUT_SHA':'a'*40},clear=True),
                  mock.patch('release.draft_readback_diagnostic.OUTPUT_ROOT',root),
                  mock.patch.object(type(root),'is_symlink',return_value=True),
                  mock.patch('urllib.request.build_opener') as opener,
                  self.assertRaisesRegex(RuntimeError,'DIAGNOSTIC_OUTPUT_DIRECTORY_INVALID')):
                main()
            opener.assert_not_called()
            with (mock.patch.dict(os.environ,{'GH_TOKEN':'sentinel','CHECKOUT_SHA':'a'*40},clear=True),
                  mock.patch('release.draft_readback_diagnostic.OUTPUT_ROOT',root),
                  mock.patch.object(type(root),'is_symlink',side_effect=[False,True]),
                  mock.patch('urllib.request.build_opener') as opener,
                  self.assertRaisesRegex(RuntimeError,'DIAGNOSTIC_IDENTITY_FILE_INVALID')):
                main()
            opener.assert_not_called()

    def test_existing_diagnostic_file_cannot_be_overwritten(self):
        from release.draft_readback_diagnostic import main
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'output';root.mkdir()
            (root/'identity.json').write_text(json.dumps({'checkout_sha':'a'*40}))
            output=root/'diagnostic.json';output.write_text('preserve-existing')
            with (mock.patch.dict(os.environ,{'GH_TOKEN':'sentinel','CHECKOUT_SHA':'a'*40},clear=True),
                  mock.patch('release.draft_readback_diagnostic.OUTPUT_ROOT',root),
                  mock.patch('urllib.request.build_opener',return_value=Opener(self.responses)),
                  self.assertRaises(FileExistsError)):
                main()
            self.assertEqual(output.read_text(),'preserve-existing')

    def test_positive_control_does_not_override_original_failure(self):
        self.responses[PREFIX]=lambda:Response({'id':1327429673,'full_name':REPOSITORY,'permissions':{'push':False}})
        result=run_probe(self.probe())
        self.assertEqual(result['original_classifier']['classification'],'UNKNOWN')
        self.assertEqual(result['control_by_id'],'VISIBLE')
        self.assertTrue(result['control_in_list'])
        self.assertEqual(result['alternative_read_contract']['classification'],'ABSENT')
        self.assertEqual(result['GET_count'],8)
        self.assertNotIn('SECRET_SENTINEL',json.dumps(result))

    def test_witness_wrong_repository_id_limited_visibility_and_control_drift(self):
        repository={'id':1327429673,'full_name':REPOSITORY,'permissions':{'push':False}}
        self.responses[PREFIX]=lambda:Response(repository)
        baseline=dict(self.responses)
        cases=[(PREFIX,lambda:Response(repository|{'id':999})),
               (PREFIX,lambda:Response(repository|{'id':1327429673.0})),
               (PREFIX,lambda:Response(repository|{'id':'1327429673'})),
               (PREFIX,lambda:Response(repository|{'full_name':'wrong/repo'})),
               (PREFIX+'/releases/'+str(CONTROL),lambda:Response(self.control|{'id':999})),
               (PREFIX+'/releases/'+str(CONTROL),lambda:Response(self.control|{'updated_at':'2026-09-19T10:00:00Z'})),
               (PREFIX+'/releases/'+str(CONTROL),lambda:Response({},404)),
               (PREFIX+'/releases?per_page=100&page=1',lambda:Response([])),
               (PREFIX+'/releases?per_page=100&page=1',lambda:Response([self.control],link='invalid'))]
        for endpoint,response in cases:
            self.responses=baseline|{endpoint:response}
            result=run_probe(self.probe())
            self.assertEqual(result['alternative_read_contract']['classification'],'UNKNOWN')
        self.responses=baseline
        calls=[]
        def changing_control():
            calls.append(1)
            return Response(self.control if len(calls)==1 else self.control|{'draft':False})
        self.responses[PREFIX+'/releases/'+str(CONTROL)]=changing_control
        result=run_probe(self.probe())
        self.assertEqual(result['alternative_read_contract']['classification'],'UNKNOWN')
        self.responses=baseline
        calls=[]
        def changing_collection():
            calls.append(1)
            return Response([self.control] if len(calls)==1 else [self.control,{'id':999,'tag_name':'other','draft':False,'updated_at':'2026-09-19T10:00:00Z'}])
        self.responses[PREFIX+'/releases?per_page=100&page=1']=changing_collection
        result=run_probe(self.probe())
        self.assertEqual(result['alternative_read_contract']['classification'],'UNKNOWN')

    def test_metadata_headers_are_projected_not_echoed(self):
        def malicious():
            response=Response({"permissions":{"push":"SECRET_SENTINEL"},"temp_clone_token":"SECRET_SENTINEL"})
            response.headers.update({'X-GitHub-Request-Id':'SECRET_SENTINEL','X-RateLimit-Remaining':'SECRET_SENTINEL'})
            return response
        self.responses[PREFIX]=malicious
        _,probe=self.observe()
        self.assertNotIn('SECRET_SENTINEL',json.dumps(probe.events))
        self.assertIsNone(probe.events[-1]['request_id'])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        workflow=Path(__file__).parents[1]/'.github/workflows/rc2-draft-readback-diagnostic.yml'
        text=workflow.read_text(encoding='utf-8')
        code=text.split("          python3 -I - <<'PY'\n",1)[1].split('\n          PY',1)[0]
        self.namespace={'__name__':'bootstrap_under_test'}
        exec(compile('\n'.join(line[10:] for line in code.splitlines()),str(workflow),'exec'),self.namespace)
        self.sha='a'*40
        repo={'id':1327429673,'full_name':REPOSITORY,'fork':False,'owner':{'id':111261350,'login':'yanyuhanyue'}}
        self.pr={'number':999,'user':{'login':'yanyuhanyue','id':111261350,'type':'User'},'state':'open','draft':False,
                 'body':'<!-- animemo-reviewed-head: '+self.sha+' -->',
                 'base':{'ref':'main','sha':self.namespace['M'],'repo':copy.deepcopy(repo)},
                 'head':{'ref':self.namespace['BRANCH'],'sha':self.sha,'repo':copy.deepcopy(repo)}}

    def test_review_binding_and_drift_rejected(self):
        check=self.namespace['check_pr']
        check(self.pr,self.sha)
        paths=[('user','login'),('user','id'),('user','type'),('head','sha'),('base','sha'),
               ('head','ref'),('base','ref'),('head','repo','full_name'),('head','repo','id'),
               ('head','repo','fork'),('base','repo','fork'),('head','repo','owner','id')]
        for path in paths:
            pr=copy.deepcopy(self.pr);obj=pr
            for key in path[:-1]:obj=obj[key]
            obj[path[-1]]='different'
            with self.subTest(path=path),self.assertRaises(ValueError):check(pr,self.sha)
        for body in ['',self.pr['body']*2,'<!-- animemo-reviewed-head: '+'b'*40+' -->']:
            with self.assertRaises(ValueError):check(self.pr|{'body':body},self.sha)

    def test_workflow_has_no_unapproved_entry_or_mutating_program(self):
        text=(Path(__file__).parents[1]/'.github/workflows/rc2-draft-readback-diagnostic.yml').read_text()
        self.assertIn('types: [ready_for_review]',text)
        self.assertIn('permissions: {}',text)
        self.assertIn('persist-credentials: false',text)
        for forbidden in ['pull_request_target:', 'workflow_dispatch:', 'ADMIN_READ', 'id-token:', 'scripts/release_publication', 'git push']:
            self.assertNotIn(forbidden,text)

    def test_bootstrap_live_pr_run_and_workflow_binding(self):
        ns=self.namespace
        with tempfile.TemporaryDirectory() as directory:
            ns['OUTPUT_ROOT']=Path(directory)/'animemo-draft-diagnostic'
            event_path=Path(directory)/'event.json'
            event_path.write_text(json.dumps({'action':'ready_for_review','sender':{'id':111261350},'pull_request':self.pr}))
            environment={'GITHUB_EVENT_NAME':'pull_request','GITHUB_REPOSITORY':REPOSITORY,'GITHUB_REPOSITORY_ID':'1327429673',
                'GITHUB_ACTOR':'yanyuhanyue','GITHUB_ACTOR_ID':'111261350','GITHUB_TRIGGERING_ACTOR':'yanyuhanyue',
                'GITHUB_RUN_ATTEMPT':'1','GITHUB_RUN_ID':'123','GITHUB_EVENT_PATH':str(event_path),
                'GITHUB_REF':'refs/pull/999/merge','GITHUB_SHA':'b'*40,'GITHUB_WORKFLOW_SHA':'b'*40,
                'GITHUB_WORKFLOW_REF':REPOSITORY+'/'+ns['WORKFLOW']+'@refs/pull/999/merge',
                'GH_TOKEN':'SECRET_SENTINEL','RUNNER_TEMP':directory,'GITHUB_OUTPUT':str(Path(directory)/'output')}
            run={'id':123,'run_attempt':1,'event':'pull_request','head_sha':self.sha,'path':ns['WORKFLOW'],
                 'repository':{'id':1327429673},'head_repository':{'id':1327429673},'actor':{'id':111261350},
                 'triggering_actor':{'id':111261350},'created_at':'2026-09-19T10:20:00Z','workflow_id':456}
            responses={'pulls/999':self.pr,'git/ref/heads/main':{'object':{'sha':ns['M']}},
                       'actions/runs/123':run,'actions/workflows/456':{'id':456,'path':ns['WORKFLOW']}}
            calls=[]
            def open_request(request,timeout):
                self.assertEqual(request.get_method(),'GET')
                path=request.full_url.removeprefix('https://api.github.com/'+PREFIX+'/')
                self.assertIn(path,responses)
                calls.append(path)
                return Response(responses[path])
            opener=mock.Mock();opener.open.side_effect=open_request
            with mock.patch.dict(os.environ,environment,clear=True),mock.patch('urllib.request.build_opener',return_value=opener):
                ns['main']()
            identity=json.loads((Path(directory)/'animemo-draft-diagnostic/identity.json').read_text())
            self.assertEqual(identity['checkout_sha'],self.sha)
            self.assertEqual(identity['GET_count'],4)
            self.assertNotIn('SECRET_SENTINEL',json.dumps(identity))
            for key,value in [('head_sha','c'*40),('run_attempt',2),('event','workflow_dispatch'),('path','other.yml')]:
                responses['actions/runs/123']=run|{key:value}
                with mock.patch.dict(os.environ,environment,clear=True),mock.patch('urllib.request.build_opener',return_value=opener),self.assertRaises(ValueError):
                    ns['main']()
            for key,value in [('GITHUB_RUN_ATTEMPT','2'),('GITHUB_ACTOR','dependabot[bot]'),('GITHUB_EVENT_NAME','pull_request_target')]:
                previous=len(calls)
                with mock.patch.dict(os.environ,environment|{key:value},clear=True),mock.patch('urllib.request.build_opener',return_value=opener),self.assertRaises(ValueError):
                    ns['main']()
                self.assertEqual(len(calls),previous)


if __name__ == '__main__':
    unittest.main()
