# Self-hosted runner (residential egress)

Normattiva firewalls cloud/datacenter IP ranges. GitHub-hosted runners live in
Azure, so they get a **TCP connect-timeout** to `api.normattiva.it` and the
pipeline can't fetch anything. This image runs the pipeline's workflows on a
self-hosted runner behind a **residential egress**, which Normattiva allows,
while GitHub Actions still orchestrates (dispatch, cron, logs, secrets).

> The egress is your network's job. Run this container on a host whose outbound
> traffic exits via the residential IP (residential line, residential-IP VPN, or
> your company gateway). The image does not route traffic itself.

## 1. Create the GitHub credential (PAT)

GitHub has no API to mint a PAT — create it once in the UI:

**Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate**

- **Repo-level runner** (just `OpenLawsFoundation/pipeline`):
  - Resource owner: `OpenLawsFoundation`, Repository access: only `pipeline`
  - Repository permissions → **Administration: Read and write**
- **Org-level runner** (every OLF repo can use it — recommended for "the company"):
  - Resource owner: `OpenLawsFoundation`
  - Organization permissions → **Self-hosted runners: Read and write**
  - (classic-PAT equivalent: scope `admin:org`)

Keep the PAT only in your secret store / `docker run -e`. **Never** commit it or
bake it into the image.

## 2. Build

```bash
docker build -t olf-runner ./runner          # add --build-arg RUNNER_ARCH=arm64 on arm
```

## 3. Run

Repo-level, persistent, auto-restarting:

```bash
docker run -d --name olf-runner --restart unless-stopped \
  -e GH_OWNER=OpenLawsFoundation -e GH_REPO=pipeline \
  -e GH_PAT="<your PAT>" \
  -e RUNNER_LABELS=residential-it \
  -e EPHEMERAL=0 \
  olf-runner
```

Org-level (drop `GH_REPO`, PAT needs the org runner permission):

```bash
docker run -d --name olf-runner --restart unless-stopped \
  -e GH_OWNER=OpenLawsFoundation \
  -e GH_PAT="<org PAT>" \
  -e RUNNER_LABELS=residential-it \
  olf-runner
```

One-shot with a registration token instead of a PAT (no PAT needed; token lasts ~1h):

```bash
docker run --rm \
  -e GH_OWNER=OpenLawsFoundation -e GH_REPO=pipeline \
  -e RUNNER_TOKEN="<registration token>" \
  -e RUNNER_LABELS=residential-it -e EPHEMERAL=1 \
  olf-runner
```

`EPHEMERAL=1` (default) registers a fresh runner per job (most secure);
`EPHEMERAL=0` keeps it online for the cron + repeated dispatches.

## 4. Run the pipeline on it

The workflows target a runner by label via their `runner` input
(`runs-on: ${{ inputs.runner }}`). Use the label you set above:

```bash
gh workflow run adapter-it-backfill.yml    -f runner=residential-it   # full backfill
gh workflow run adapter-it-incremental.yml -f runner=residential-it   # (if you add the input)
```

`ARCHIVE_WRITE_TOKEN` (already configured on the pipeline repo) is delivered to
the runner as a normal Actions secret, so commit+push to the archive works.

## Notes / limits

- **Throughput**: even from an allowed IP, Normattiva rate-limits the async
  *export* endpoint (HTTP 409 "bloccata") after a few dozen jobs. The runner
  fixes the *connectivity*, not the throttle. The backfill is chunked,
  idempotent and resumable, so it builds incrementally across runs — but the
  efficient way to backfill the whole corpus is Normattiva's **preconfezionata
  bulk collections** (a few large ZIPs vs 200k per-act exports), which sidesteps
  the export throttle. That bulk path is a separate adapter feature.
- Pin `RUNNER_VERSION` in the Dockerfile to keep builds reproducible; bump it
  when GitHub deprecates older agents.
