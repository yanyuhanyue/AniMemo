#!/usr/bin/env bash
set -euo pipefail

test "${RUNNER_OS:-}" = "Linux"
test "${RUNNER_ARCH:-}" = "X64"
[[ "${GH_CLI_VERSION:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
[[ "${GH_CLI_LINUX_AMD64_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
test -n "${RUNNER_TEMP:-}"
test -n "${GITHUB_PATH:-}"

archive="$RUNNER_TEMP/gh_${GH_CLI_VERSION}_linux_amd64.tar.gz"
install_root="$RUNNER_TEMP/animemo-pinned-gh-${GH_CLI_VERSION}"
cli_dir="$install_root/gh_${GH_CLI_VERSION}_linux_amd64/bin"

umask 077
mkdir -p "$install_root"
curl \
  --fail \
  --silent \
  --show-error \
  --location \
  --proto '=https' \
  --tlsv1.2 \
  --retry 3 \
  --retry-delay 1 \
  --retry-all-errors \
  "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_amd64.tar.gz" \
  --output "$archive"

printf '%s  %s\n' "$GH_CLI_LINUX_AMD64_SHA256" "$archive" \
  | sha256sum --check --strict
tar \
  --extract \
  --gzip \
  --file "$archive" \
  --directory "$install_root" \
  --no-same-owner

test -x "$cli_dir/gh"
actual_version="$("$cli_dir/gh" --version \
  | sed -nE 's/^gh version ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' \
  | head -n 1)"
test "$actual_version" = "$GH_CLI_VERSION"
printf '%s\n' "$cli_dir" >> "$GITHUB_PATH"
