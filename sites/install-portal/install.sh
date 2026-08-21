#!/bin/sh
set -eu

printf '%s\n' \
  'REMOTE_BOOTSTRAP_EXECUTION_DISABLED' \
  'AniMemo-controlled scripts must not execute before GitHub Release authority verification.' \
  'Use the verified Stage-0 instructions shown at https://install.animemo.cc/.' >&2
exit 78
