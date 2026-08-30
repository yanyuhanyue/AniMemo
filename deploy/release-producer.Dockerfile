FROM python:3.12.10-bookworm@sha256:91487c05f87617b9d1165274c07a9b0040dfef21a855b5b418a0ebc6aa26c5de AS python-runtime

FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36

ARG SOURCE_DATE_EPOCH

COPY --from=python-runtime /usr/local/ /usr/local/

ADD --checksum=sha256:a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112 \
    https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_linux_amd64.tar.gz \
    /tmp/gh.tar.gz
ADD --checksum=sha256:5c16d8ddb971cb1d5e6ed8b1e743da8224414eeba2c2762d8f1a61b2f095699e \
    https://github.com/google/go-containerregistry/releases/download/v0.21.9/go-containerregistry_Linux_x86_64.tar.gz \
    /tmp/crane.tar.gz
ADD --checksum=sha256:020468de7539ce70ef1bceaf7cde2e8c4f2ca6c3afb84642aabc5c97d9fc2a0d \
    https://github.com/jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64 \
    /usr/local/bin/jq

RUN set -eux; \
    case "$SOURCE_DATE_EPOCH" in ''|*[!0-9]*|0) exit 1;; esac; \
    tar -xzf /tmp/gh.tar.gz -C /usr/local/bin --strip-components=2 gh_2.97.0_linux_amd64/bin/gh; \
    tar -xzf /tmp/crane.tar.gz -C /usr/local/bin crane; \
    chmod 0755 /usr/local/bin/gh /usr/local/bin/crane /usr/local/bin/jq; \
    rm -f /tmp/gh.tar.gz /tmp/crane.tar.gz; \
    test "$(python -c 'import platform; print(platform.python_version())')" = 3.12.10; \
    test "$(go version | awk '{print $3}')" = go1.26.6; \
    test "$(gh --version | awk 'NR == 1 {print $3}')" = 2.97.0; \
    test "$(crane version)" = 0.21.9; \
    test "$(jq --version)" = jq-1.8.1

COPY release/requirements.lock /opt/animemo-locks/release.requirements.lock
COPY deploy/release-producer.Dockerfile /opt/animemo-locks/release-producer.Dockerfile
COPY scripts/release-producer-entrypoint.sh /usr/local/bin/release-producer-entrypoint
RUN python -m pip install --disable-pip-version-check --no-cache-dir \
    --require-hashes -r /opt/animemo-locks/release.requirements.lock && \
    chmod 0755 /usr/local/bin/release-producer-entrypoint

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/release-producer-entrypoint"]
