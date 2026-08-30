# GitHub Actions public provenance fixture

This directory contains a public, secret-free GitHub Actions Sigstore bundle
for the AniMemo `v1.1.0-rc.18` API image. The bundle was acquired anonymously
from GitHub's public attestation service for:

- repository: `yanyuhanyue/AniMemo`
- workflow: `.github/workflows/release.yml@refs/heads/main`
- source commit: `1e699c456110266398f868e43fa2e69b2d704d24`
- Actions run: `33286221425`, attempt `1`
- subject: `ghcr.io/yanyuhanyue/animemo-api`
- subject digest: `sha256:2588108838c23c9b7e29d70d3a897109bf93b5c52cc4bcf949d5434e51496459`

The bundle was acquired with `gh attestation download` without a GitHub token.
The trusted root is the Sigstore Public Good target from the official
`sigstore/root-signing` repository at immutable commit
`7992f478a9ff9306cc6f83cb99dfa28dfb0b88cc`. Tests invoke the production
verifier directly and do not consult the network or environment variables.

Committed fixture identities:

- bundle: `sha256:6f174db7894200a118bc971d86462a0098bdd7766c49f695d1066a8e29d28922`
- trusted root: `sha256:6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c755fc3517c9e6bc0b66`
- untrusted-chain negative root: `sha256:503c669ede6b4416c39de5d48d5964141970d9881fc941370faac0f75789fecf`
