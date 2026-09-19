"""Offline real-history replay; all mutation endpoints remain local fakes."""
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.contract import ReleaseContractError, resolve_prerelease, validate_publication_reservations
from release.frozen_occupancy import (FrozenOccupancyError, KIND, VerifiedFrozenOccupancies,
    frozen_records, reject_frozen_remote_keys, reject_frozen_target, verify_live)
from release.publication import PublicationError, build_publication_plan, validate_publication_plan
from release.publication_transaction import GitCommandResult, PublicationTransactionError, MutationIntent

ROOT=Path(__file__).parents[1]


def config():return json.loads((ROOT/'release/publication-reservations.json').read_bytes())


class FrozenGitFixture:
    def __init__(self):
        self.fixture=json.loads((ROOT/'release/fixtures/frozen-rc2-history.json').read_bytes())
        self.outputs={tuple(row['arguments']):base64.b64decode(row['stdout_base64']) for row in self.fixture['commands']}
        self.head=self.fixture['head'];self.ref=self.fixture['ref'];self.calls=[]
        self.inventory=(self.head+'\t'+self.ref+'\n').encode()

    def __call__(self,argv,timeout,input_bytes,environment):
        self.assert_read(argv,input_bytes,environment)
        args=tuple(argv[3:]);self.calls.append(args)
        if args==('remote','get-url','origin'):out=b'https://github.com/yanyuhanyue/AniMemo.git\n'
        elif args[:2]==('ls-remote','--refs'):out=self.inventory
        elif args==('fetch','--no-tags','origin',self.ref):out=b''
        elif args==('rev-parse','FETCH_HEAD'):out=(self.head+'\n').encode()
        else:out=self.outputs[args]
        return GitCommandResult(0,out,b'')

    @staticmethod
    def assert_read(argv,input_bytes,environment):
        assert argv[:2]==('git','-C') and input_bytes is None and environment is None
        assert argv[3] in {'remote','ls-remote','fetch','rev-parse','rev-list','ls-tree','show'}


def verified_config():
    value=config();proof=verify_live(value,ROOT,run_git=FrozenGitFixture())
    return value,proof


class FrozenOccupancyTests(unittest.TestCase):
    def test_real_two_snapshots_through_canonical_loader_and_resolver(self):
        fx=FrozenGitFixture();value=config();proof=verify_live(value,ROOT,run_git=fx)
        self.assertEqual(sum(args[0]=='ls-tree' for args in fx.calls),2)
        result=resolve_prerelease(tags=['v1.0.0','v1.1.0','v2.0.0-rc.1'],bump='major',channel='rc',
                                  publication_reservations=value,frozen_observations=proof)
        self.assertEqual(result,{'targetVersion':'v2.0.0','releaseTag':'v2.0.0-rc.3','sequence':3})
        record=frozen_records(value)[0]
        self.assertEqual(record['kind'],KIND);self.assertFalse(record['reusable'])
        self.assertTrue(all(not s['attempts'] and s['committed'] is False for s in record['steps']))

    def test_forged_proof_wrong_binding_and_legacy_signature_not_relaxed(self):
        value,proof=verified_config()
        for false_proof in [None,True,{'verified':True}]:
            with self.assertRaises(ReleaseContractError):
                resolve_prerelease(tags=['v1.1.0'],bump='major',channel='rc',publication_reservations=value,frozen_observations=false_proof)
        with self.assertRaises(FrozenOccupancyError):VerifiedFrozenOccupancies(True,frozen_records(value))
        changed=copy.deepcopy(value);changed['reservations'][-1]['decision']['authorization']='ANIMEMO_DIFFERENT'
        with self.assertRaises(FrozenOccupancyError):proof.require_records(frozen_records(changed))
        changed=copy.deepcopy(value);changed['reservations'][0]['attestationVerified']=False
        with self.assertRaises((ReleaseContractError,FrozenOccupancyError)):validate_publication_reservations(changed)

    def test_head_bytes_attempts_and_linear_history_tampering_rejected(self):
        for key,replacement in [('journalHead','a'*40),('ledgerFileSha256','sha256:'+'a'*64),
                                ('ledgerIdentity','sha256:'+'a'*64),('qualificationRunId',True),('journalRevision',2)]:
            value=config();value['reservations'][-1][key]=replacement
            with self.subTest(key=key),self.assertRaises((FrozenOccupancyError,PublicationTransactionError)):
                verify_live(value,ROOT,run_git=FrozenGitFixture())
        value=config();value['reservations'][-1]['steps'][0]['attempts']=[{}]
        with self.assertRaises(FrozenOccupancyError):verify_live(value,ROOT,run_git=FrozenGitFixture())
        fx=FrozenGitFixture();key=('rev-list','--reverse','--parents',fx.head)
        fx.outputs[key]=fx.outputs[key].splitlines()[-1]+b' '+b'a'*40+b'\n'
        with self.assertRaises(PublicationTransactionError):verify_live(config(),ROOT,run_git=fx)
        fx=FrozenGitFixture();key=('show',fx.head+':ledger.json');fx.outputs[key]=fx.outputs[key].replace(b'"FROZEN"',b'"ACTIVE"',1)
        with self.assertRaises(PublicationTransactionError):verify_live(config(),ROOT,run_git=fx)

    def test_unapproved_freeze_and_remote_key_collision_remain_blocking(self):
        value=config();value['reservations']=[v for v in value['reservations'] if v.get('kind')!=KIND]
        with self.assertRaisesRegex(FrozenOccupancyError,'UNRESOLVED_PUBLICATION_TRANSACTION'):
            verify_live(value,ROOT,run_git=FrozenGitFixture())
        record=frozen_records(config())[0];step=record['steps'][0]
        intent=MutationIntent('new-version',step['kind'],step['remoteKey'],step['expectedIdentity'])
        with self.assertRaisesRegex(FrozenOccupancyError,'REMOTE_KEY_CONFLICT'):reject_frozen_remote_keys([intent],[record])
        with self.assertRaisesRegex(FrozenOccupancyError,'NOT_REUSABLE'):reject_frozen_target('v2.0.0-rc.2')

    def test_new_plan_explicitly_binds_predecessor_but_cannot_reuse_frozen_target(self):
        kwargs=dict(repository='yanyuhanyue/AniMemo',channel='rc',tag='v2.0.0-rc.3',commit='a'*40,
                    qualification_identity='sha256:'+'a'*64,release_notes_identity='sha256:'+'b'*64,
                    release_notes_markdown_sha256='sha256:'+'c'*64,
                    assets={name:{'sha256':'sha256:'+'d'*64,'size':1} for name in
                            ('checksums.txt','deployment-contract.json','installer-materials.tar','release-manifest.json')},
                    api_digest='sha256:'+'e'*64,web_digest='sha256:'+'f'*64)
        plan=build_publication_plan(**kwargs)
        self.assertEqual(plan,validate_publication_plan(plan))
        predecessor=plan['predecessor_frozen_occupancies'][0]
        self.assertEqual(predecessor['releaseTag'],'v2.0.0-rc.2');self.assertEqual(predecessor['state'],'FROZEN')
        self.assertFalse(predecessor['published'])
        del plan['predecessor_frozen_occupancies']
        with self.assertRaises(PublicationError):validate_publication_plan(plan)
        with self.assertRaises(PublicationError):build_publication_plan(**(kwargs|{'tag':'v2.0.0-rc.2'}))

    def test_actual_cli_handler_uses_canonical_live_replay_and_wire_version(self):
        from release.cli import main
        fx=FrozenGitFixture()
        with tempfile.TemporaryDirectory() as directory:
            tags=Path(directory)/'tags.txt';tags.write_text('v1.1.0\nv2.0.0-rc.1\n')
            output=Path(directory)/'github-output'
            with mock.patch('release.publication_transaction._run_git_command',side_effect=fx),mock.patch('builtins.print') as printed:
                result=main(['resolve-version','--tags-file',str(tags),'--publication-reservations-file',str(ROOT/'release/publication-reservations.json'),
                             '--bump','major','--channel','rc','--github-output',str(output)])
            self.assertEqual(result,0)
            self.assertIn('release_tag=v2.0.0-rc.3',output.read_text())


if __name__=='__main__':unittest.main()
