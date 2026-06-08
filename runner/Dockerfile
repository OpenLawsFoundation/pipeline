# Self-hosted GitHub Actions runner for the OLF pipeline.
#
# WHY THIS EXISTS: Normattiva firewalls cloud/datacenter IP ranges at the network
# layer (GitHub-hosted runners live in Azure and get a TCP connect-timeout). This
# runner is meant to run behind a *residential* egress so it can reach Normattiva,
# while GitHub Actions still orchestrates the job (cron, logs, dispatch).
#
# The egress itself (residential VPN/gateway) is provided by your host/network —
# this image only runs the runner agent. The GitHub credential is passed at
# RUNTIME and is never baked into the image.
#
#   docker build -t olf-runner ./runner
#   docker run -d --name olf-runner --restart unless-stopped \
#     -e GH_OWNER=OpenLawsFoundation -e GH_REPO=pipeline \
#     -e GH_PAT="<fine-grained PAT>" -e RUNNER_LABELS=residential-it \
#     olf-runner
#
# For an arm64 host, build with: --build-arg RUNNER_ARCH=arm64

FROM ubuntu:24.04

ARG RUNNER_VERSION=2.334.0
ARG RUNNER_ARCH=x64
ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_MANUALLY_TRAP_SIG=1 \
    ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT=1

# Runner + workflow prerequisites: git/curl/jq for registration, libicu for the
# runner agent, python for the pipeline (setup-python will fetch 3.12 if needed).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq git sudo tar gzip unzip lsb-release \
        libicu74 python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner

WORKDIR /home/runner/actions-runner

RUN curl -fsSL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz \
    && ./bin/installdependencies.sh

COPY entrypoint.sh /home/runner/entrypoint.sh
RUN chmod +x /home/runner/entrypoint.sh \
    && chown -R runner:runner /home/runner

USER runner
ENTRYPOINT ["/home/runner/entrypoint.sh"]
