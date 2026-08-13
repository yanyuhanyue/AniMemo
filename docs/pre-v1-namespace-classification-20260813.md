# AniMemo Pre-v1 Namespace Classification

Snapshot: 2026-08-13 (Asia/Shanghai).

This inventory classifies tracked legacy identifiers after the Barrier B pre-v1
namespace normalization. Current AniMemo-owned runtime, browser, package,
deployment, database, release, updater, and CI identities use `ANIMEMO_*`,
`animemo_*`, or `animemo-*` as appropriate.

## Allowed legacy references

| Classification | Location | Reason |
| --- | --- | --- |
| `IMMUTABLE_MIGRATION_HISTORY` | `backend/site_config/migrations/0006_animemo_domain_defaults.py` and its targeted test | The old `re-anime.cc`, `media.re-anime.cc`, and `img.re-anime.cc` values are source keys that existing rows must recognize and migrate to `media.animemo.cc`. |
| `SECURITY_DENYLIST` | `src/lib/webAuthAdapter.js` and `tests/browser-namespace.test.mjs` | The obsolete `anime_journal_access` and `anime_journal_refresh` browser keys are removal-only. AniMemo never reads or accepts their values. |
| `SECURITY_DENYLIST` | `scripts/perf/load_harness.py` and its targeted test | `re-anime.cc`, the canonical AniMemo hosts, and the production IP remain forbidden load-test targets. |
| `SECURITY_DENYLIST` | `backend/config/settings.py` and its production-settings test | The obsolete local-only secret string remains in the unsafe-secret set so production refuses it. |
| `HISTORICAL_AUDIT_FACT` | dated audit and production-acceptance planning documents | These files describe the repository or infrastructure state at the time of the audit and are not current runtime guidance. |
| `IMMUTABLE_MIGRATION_HISTORY` | migrations `0001` through `0005` | Historical schema/data operations are not rewritten for naming aesthetics. |
| `THIRD_PARTY` | dependency lock metadata, generated dependency comments, and external provider/product names | Third-party identities are outside the AniMemo machine namespace. |

## Result

- `ACTIVE_OWNED_NAMESPACE`: none remaining.
- `UNKNOWN`: none remaining.
- `LEGACY_REDIRECT_REFERENCE`: no repository runtime default; the live legacy
  `re-anime.cc` vhost is an externally managed temporary redirect and was not
  mutated by Barrier B.

Repository deployment templates are canonical for `animemo.cc`,
`www.animemo.cc`, and `media.animemo.cc`. They do not claim to prove or modify
the live server.
