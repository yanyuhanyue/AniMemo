#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

template=/etc/nginx/animemo/default.conf.template
target=/etc/nginx/conf.d/default.conf
temporary="${target}.tmp"
placeholder=__ANIMEMO_TRUSTED_EDGE_PROXY_CIDR__

if [ ! -x /sbin/ip ]; then
  echo "AniMemo Web IPv4 route authority is unavailable." >&2
  exit 1
fi

gateway="$(/sbin/ip -4 route show default | /usr/bin/awk '
  $1 == "default" && $2 == "via" {
    count += 1
    value = $3
  }
  END {
    if (count != 1) {
      exit 1
    }
    print value
  }
')" || {
  echo "AniMemo Web could not determine one exact IPv4 edge proxy." >&2
  exit 1
}

if ! printf '%s\n' "$gateway" | /usr/bin/awk -F. '
  NF != 4 { exit 1 }
  {
    for (index = 1; index <= 4; index += 1) {
      if ($index !~ /^[0-9]+$/ || $index < 0 || $index > 255) {
        exit 1
      }
    }
    if ($1 == 0 || $1 == 127 || $1 >= 224 || ($1 == 169 && $2 == 254)) {
      exit 1
    }
  }
'; then
  echo "AniMemo Web edge proxy identity is invalid." >&2
  exit 1
fi

if [ "$(/bin/grep -o "$placeholder" "$template" | /usr/bin/wc -l)" -ne 2 ]; then
  echo "AniMemo Web proxy template contract is invalid." >&2
  exit 1
fi

/bin/sed "s|$placeholder|${gateway}/32|g" "$template" > "$temporary"
if /bin/grep -q "$placeholder" "$temporary"; then
  echo "AniMemo Web proxy template rendering is incomplete." >&2
  exit 1
fi
/bin/chmod 0444 "$temporary"
/bin/mv -f "$temporary" "$target"

exec /docker-entrypoint.sh "$@"
