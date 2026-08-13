# AniMemo v1.0 DeepSec Final RC Audit

Status: **PASS WITH ONE NON-BLOCKING DEFERRED MEDIUM**

Audited application candidate: `306878de039cba6a706ce11c03ac9dab97448917`
Completed: 2026-08-14, Asia/Shanghai

## Scope and source handling

The scan input was created with `git archive 306878de039cba6a706ce11c03ac9dab97448917`. It contained 721 tracked entries and 721 extracted files. Untracked files, the working `.env`, databases, backups, SSH material, production credentials, and runtime state were not included.

Raw JSON is intentionally retained outside the repository at:

```text
C:\Users\admin\AppData\Local\Temp\animemo-final-rc-deepsec-306878d-20260814-005500
```

The security result applies to the frozen application candidate, not to the later report-only readiness commit.

## Tool and commands

DeepSec package version: `0.2.0`.

`deepsec --version` is not implemented by this CLI. The installed package version was recorded from the actual environment, and the supported interface was confirmed with:

```text
deepsec shield --help
deepsec shield scan --help
deepsec shield supply-chain --help
```

Equivalent scan commands used the actual `0.2.0` interface:

```text
deepsec shield scan <tracked-source> --layer l1 --format json --output <temp> --include-tests
deepsec shield scan <tracked-source> --layer l2 --format json --output <temp> --include-tests
deepsec shield scan <four-changed-files> --layer l3 --remote-l3 --format json --output <temp> --include-tests
deepsec shield supply-chain check <tracked-source> --format json --output <temp>
```

The full-repository local L3 attempt exceeded its execution window. Remote full-repository L3 had already timed out twice and was not retried under the two-attempt rule. A focused final-SHA L3 scan covered every file changed by the final code fix:

- `backend/performance/seed.py`
- `backend/journal/test_performance_baseline.py`
- `backend/plugin_host/management/commands/sync_official_plugins.py`
- `backend/plugin_host/tests/test_official_sync.py`

## Finding summary

| Layer | Raw findings | Raw severity | Manual result |
| --- | ---: | --- | --- |
| L1 | 60 | 54 Critical, 6 High | 60 false positive / not applicable |
| L2 | 7 | 3 High, 4 Medium | 6 false positive / not applicable, 1 deferred Medium |
| Focused L3 | 10 | 5 Medium, 5 Low | 10 false positive / not applicable |
| Supply chain | 0 | none | PASS |

Confirmed Critical: **0**.

Confirmed High: **0**.

False positive or not applicable: **76**.

Deferred: **1 Medium**.

## L1 triage

The 54 `hardcoded_secret` findings are synthetic CI/test values, masked samples, generated setup credentials, test CSRF values, or fixed non-production fixture credentials. No production secret was present in the tracked archive.

The three `ai_pattern_error` findings are not exploitable code-generation or SQL-injection defects:

- Updater thread names and redacted error text do not execute input.
- The SQLite-only `json_each` table name is obtained from Django model metadata, not a request value.

The three `insecure_config` findings are not runtime vulnerabilities:

- `check_official_plugin_immutability.py` evaluates a tightly validated literal tuple extracted from repository-owned source; it does not evaluate caller-provided Python.
- The two `yaml.load()` calls are test-only workflow parsers using `yaml.BaseLoader`, which constructs scalar/container data and does not instantiate arbitrary Python objects.

Disposition: all 60 L1 findings are **FALSE POSITIVE** or **NOT APPLICABLE**.

## L2 triage

| Finding group | Count | Disposition | Rationale |
| --- | ---: | --- | --- |
| Updater command injection | 2 | FALSE POSITIVE | Fixed executable/argv vectors, `list(argv)`, `shell=False`, and production RPC does not accept arbitrary commands. |
| Resource sampler command injection | 1 | NOT APPLICABLE | Isolated CI harness constructs fixed Docker/PostgreSQL/Redis argv arrays; the callable is an injected test seam, not a product request surface. |
| Core Bangumi SSRF | 1 | FALSE POSITIVE | URL construction is constrained by `_fixed_url()` to fixed Bangumi API/OAuth bases. |
| Official importer Bangumi SSRF | 2 | FALSE POSITIVE | Fixed `https://api.bgm.tv/v0/subjects/` paths with `int(subject_id)` interpolation. |
| Plugin Host arbitrary URL | 1 | DEFERRED MEDIUM | `PluginContext.request_json()` accepts an arbitrary URL in trusted in-process backend plugin code. This is consistent with the explicit v1.0 trusted-publisher model, but it is not a network sandbox. |

The deferred network boundary must be closed before enabling untrusted backend publishers, a public third-party backend marketplace, worker/container isolation, or Runtime v3. The expected remediation is a Host-owned outbound network broker with scheme/host policy and explicit capability declarations. It is not a confirmed v1.0 authorization bypass because backend plugin execution already requires trusted superuser review/publish and runs with in-process authority.

## Focused L3 triage

All ten final-delta findings were reviewed against execution boundaries and tests:

- Test passwords and the performance integration secret exist only in disposable fixtures.
- Performance identity tokens are deliberately emitted to a protected workflow artifact boundary; production execution is blocked by the load harness target denylist and isolated workflow contract.
- Prefix cleanup and fixture approval records are confined to the explicitly disposable performance database contract.
- `force_authenticate` is a query-count unit test; the real isolated performance workflow separately uses issued JWTs.
- Official sync is an operator management command with package validation, immutable content checks, concurrency handling, and fail-closed conflict tests. The reported actor and diagnostics do not create a remote authorization surface.
- Manifest console text, paths, and package hashes are operator diagnostics, not secrets.

Disposition: all ten are **FALSE POSITIVE** or **NOT APPLICABLE**. No code change was justified by these findings.

## Supply chain and Spear

DeepSec supply-chain check returned zero findings for the tracked source dependency inputs.

Spear: **NOT RUN BY POLICY**. No scan targeted `animemo.cc`, the VPS, localhost bypasses, private networks, Cloudflare, R2, or the real Bangumi application.

## Final security decision

DeepSec L1: **PASS** after triage.

DeepSec L2: **PASS**, with one non-blocking deferred Medium.

DeepSec L3: **FAIL for the required full-repository final-SHA scope**; the final changed-file delta passed.

DeepSec Supply Chain: **PASS**.
Release-blocking confirmed Critical/High: **0**.

This result does not override the overall Final RC readiness verdict. Recovery, concurrency/long-task isolation, Release Producer parity, and Pre-Merge authority have separate unresolved evidence requirements.
