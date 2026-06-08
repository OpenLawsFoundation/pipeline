#!/usr/bin/env bash
# Register this container as a self-hosted GitHub Actions runner, then run it.
#
# Auth (provide ONE):
#   GH_PAT        - a (fine-grained or classic) PAT with permission to manage
#                   runners; the container fetches a fresh registration token on
#                   every start. RECOMMENDED — survives restarts.
#   RUNNER_TOKEN  - a pre-generated runner registration token (short-lived ~1h);
#                   handy for a one-shot run, no PAT needed.
#
# Target (provide GH_OWNER; add GH_REPO for a repo-level runner, omit for org):
#   GH_OWNER      - org or user, e.g. OpenLawsFoundation
#   GH_REPO       - e.g. pipeline   (omit => org-level runner)
#
# Optional:
#   RUNNER_LABELS - comma-separated labels the workflow targets (default residential-it)
#   RUNNER_NAME   - runner name (default olf-<host>-<pid>)
#   RUNNER_GROUP  - runner group (org runners only)
#   EPHEMERAL     - "1" (default) registers with --ephemeral (one job then exits,
#                   re-registers on next container start); "0" stays persistent.
set -euo pipefail

GH_OWNER="${GH_OWNER:?set GH_OWNER (e.g. OpenLawsFoundation)}"
RUNNER_LABELS="${RUNNER_LABELS:-residential-it}"
RUNNER_NAME="${RUNNER_NAME:-olf-$(hostname)-$$}"
EPHEMERAL="${EPHEMERAL:-1}"

if [[ -n "${GH_REPO:-}" ]]; then
  reg_api="https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/actions/runners/registration-token"
  reg_url="https://github.com/${GH_OWNER}/${GH_REPO}"
else
  reg_api="https://api.github.com/orgs/${GH_OWNER}/actions/runners/registration-token"
  reg_url="https://github.com/${GH_OWNER}"
fi

# Obtain a registration token (from the PAT) unless one was supplied directly.
if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  : "${GH_PAT:?provide GH_PAT or RUNNER_TOKEN}"
  RUNNER_TOKEN="$(curl -fsSL -X POST \
      -H "Authorization: Bearer ${GH_PAT}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "${reg_api}" | jq -r '.token')"
  if [[ -z "${RUNNER_TOKEN}" || "${RUNNER_TOKEN}" == "null" ]]; then
    echo "ERROR: could not get a registration token — check GH_PAT scopes (needs repo Administration: read/write, or org Self-hosted-runners for org-level)." >&2
    exit 1
  fi
fi

deregister() { ./config.sh remove --token "${RUNNER_TOKEN}" >/dev/null 2>&1 || true; }
trap 'deregister; exit 0' INT TERM
trap deregister EXIT

eph=(); [[ "${EPHEMERAL}" == "1" ]] && eph=(--ephemeral)
./config.sh \
  --url "${reg_url}" \
  --token "${RUNNER_TOKEN}" \
  --name "${RUNNER_NAME}" \
  --labels "${RUNNER_LABELS}" \
  ${RUNNER_GROUP:+--runnergroup "${RUNNER_GROUP}"} \
  --work _work --unattended --replace "${eph[@]}"

# run.sh handles its own signals; exec so SIGTERM reaches it.
exec ./run.sh
