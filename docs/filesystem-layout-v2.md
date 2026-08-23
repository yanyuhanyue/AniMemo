# AniMemo Filesystem Layout v2

This contract specializes the deterministic filesystem portion of
`instance-scoped-deployment-contract-v2.md`. It does not modify Filesystem
Layout v1 or authorize migration from that layout.

The only production mapping is:

| Resource | Template |
| --- | --- |
| application | `/opt/animemo-instances/<name>` |
| data | `/data/animemo-instances/<name>` |
| updater program | `/opt/animemo-updater` |
| updater state | `/var/lib/animemo-updater/instances/<name>` |
| updater runtime | `/run/animemo-updater/<name>` |
| managed config | `/data/animemo-instances/<name>/config/animemo.json` |
| backups | `/data/animemo-instances/<name>/backups` |
| locator | `/var/lib/animemo-updater/instances/<name>/instance.json` |
| socket | `/run/animemo-updater/<name>/updater.sock` |

`<name>` is a validated `InstanceName`, never a path segment supplied directly.
All sensitive files are regular, single-link, no-follow, mode-checked,
`0600`, atomically replaced, and fsynced with their parent. Instance-owned roots
reject symlinks, junctions, foreign content, and ownership conflicts. There is
no arbitrary root override, legacy fallback, scanning, or automatic adoption.
