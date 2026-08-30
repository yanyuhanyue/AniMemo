#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

template=/etc/nginx/animemo/default.conf.template
target=/etc/nginx/conf.d/default.conf
temporary="${target}.tmp"
placeholder=__ANIMEMO_TRUSTED_EDGE_PROXY_CIDR__

gateway="$(/usr/bin/awk -f /usr/local/libexec/animemo/resolve-edge-gateway.awk \
  /proc/net/route)" || {
  echo "AniMemo Web could not determine one exact IPv4 edge proxy." >&2
  exit 1
}

if ! printf '%s\n' "$gateway" | /usr/bin/awk -F. '
  NF != 4 { exit 1 }
  {
    for (octet = 1; octet <= 4; octet += 1) {
      if ($octet !~ /^[0-9]+$/ || $octet < 0 || $octet > 255) {
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
