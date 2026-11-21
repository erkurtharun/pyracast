# Provision the Google Cloud environment with Terraform

An operator runs `terraform plan` and `apply` in `infra/terraform` (normally from Cloud Shell) against a private GCS state bucket created by hand. The result is one shielded Ubuntu VM with a static IPv4 address and a separate data disk, network and firewalls, Artifact Registry, three empty Secret Manager secrets, a backup bucket, service accounts and IAM, GitHub Workload Identity Federation and Cloud Monitoring alerts; the VM startup script then prepares Docker, the data disk, `/etc/pyracast/host.json`, `pyracast.service` and the Docker drop-in, and each release installs the backup, rehearsal and watchdog units from `deploy/systemd/`. The operator copies the `github_environment_variables` output into the GitHub `production` environment and the `instance_ip` output into a Cloudflare A record by hand.

## Entry points

- Operator commands `terraform -chdir=infra/terraform init -backend-config=...`, `plan -out=production.tfplan`, `apply production.tfplan`, `output -json github_environment_variables` (procedure in `docs/DEPLOYMENT.md`, "Create the infrastructure").
- Inputs: `infra/terraform/variables.tf` (validated variables), `terraform.tfvars.example`, `backend.hcl.example` (partial `backend "gcs" {}` in `versions.tf`). The local `terraform.tfvars`, `backend.hcl`, `*.tfplan` and `.terraform/` are git-ignored.
- Resources: `main.tf` (APIs, network, firewalls, registry, secrets, bucket, disk, optional disk snapshots, VM), `iam.tf` (service accounts, roles, audit logs, WIF, optional image-rescan identity), `monitoring.tf` (email channel, uptime check, readiness alert, six operations alerts, a watchdog heartbeat alert, seven security alerts on the `security-events` log metric), `outputs.tf`.
- VM trigger: Compute Engine runs `metadata.startup-script` (`templates/startup.sh.tftpl`, with `templates/network-guard.sh` embedded) on every boot.
- CI: `.github/workflows/ci.yml`, job "Deployment configuration and containers", steps "Validate Terraform without cloud credentials or backend access" and "Verify the boot script on a stubbed host without cloud access" (`deploy/test-boot.sh`). Dependabot (`.github/dependabot.yml`, `terraform` ecosystem, weekly) proposes provider pin changes in `versions.tf` and `.terraform.lock.hcl`.
- No web screen, Django view or database table is involved.

## Sequence

```mermaid
sequenceDiagram
    actor Op as Operator
    participant TF as Terraform
    participant St as "State bucket"
    participant G as "GCP APIs"
    participant VM as "VM startup script"
    participant Pkg as "Ubuntu and Docker apt repositories"
    participant SD as systemd
    participant GH as "GitHub production env"
    Op->>TF: "init with backend bucket and prefix"
    TF->>St: "configure the gcs backend"
    Op->>TF: "plan -out, then apply the saved plan"
    TF->>St: "lock and read state"
    alt variable validation or zone precondition fails
        TF-->>Op: "plan error, nothing changed"
    else plan would destroy a prevent_destroy resource
        TF-->>Op: "plan error (secrets, backup bucket, data disk)"
    end
    TF->>G: "enable 13 APIs, VPC, subnet with flow logs, static IP, two service accounts"
    TF->>G: "firewalls targeting the runtime account, registry, 3 secrets without versions, versioned bucket, data disk"
    TF->>G: "IAM on project, registry, secrets and bucket, audit logs, WIF pool and provider"
    TF->>G: "VM with startup-script metadata and host.json payload"
    TF->>G: "per-instance SSH IAM, email channel, uptime check, alert policies and log metrics"
    TF->>St: "write state, release lock"
    TF-->>Op: "outputs incl. github_environment_variables"
    Op->>GH: "set the environment variables by hand"
    G->>VM: "boot, run startup script (every boot)"
    alt not Ubuntu 24.04 amd64, data disk missing after 90 s, non-ext4, unformatted but not empty, or foreign fstab entry
        VM-->>SD: "script exits non-zero, later steps skipped"
    end
    VM->>VM: "write host.json, network guard, daemon.json, Docker drop-in and pyracast.service when they differ"
    VM->>VM: "mkfs.ext4 only if no signature and a zeroed first MiB, mount at /var/lib/pyracast with nofail, release lock 0600"
    opt pinned Docker packages missing and no release holds the lock
        VM->>Pkg: "apt-get update, Docker key checked by fingerprint, install pinned Docker and Compose"
    end
    VM->>SD: "daemon-reload after a change, enable docker and pyracast.service"
    VM->>SD: "install the backup, rehearsal and watchdog units from current when a release exists"
    opt Docker configuration changed and no release holds the lock
        VM->>SD: "restart docker.service"
    end
    SD->>SD: "network guard runs before and after dockerd starts"
    opt "/opt/pyracast/current/deploy/compose.yaml exists"
        VM->>SD: "start pyracast.service"
    end
    G-->>Op: "readiness alert email until a release is reachable through Cloudflare"
```

## Data

No PostgreSQL table is read or written. The stores that carry this flow's state:

| Table | R/W | What |
| --- | --- | --- |
| Terraform state (GCS bucket created by hand, prefix from `-backend-config`) | R/W | Resource IDs and the rendered startup script; no secret values |
| Local `terraform.tfvars`, `production.tfplan`, `.terraform/` (git-ignored) | R/W | Inputs, the saved plan that `apply` executes, provider cache |
| Secret Manager `<name>-runtime`, `-migration`, `-database` | W (secret resources only) | Created without versions; versions are added by hand in flow `secret-provisioning` |
| Instance metadata `startup-script` | W | Rendered template with base64 `host.json` and network guard |
| `/etc/pyracast/host.json` (0600) | W (each boot, when it differs) | The keys of `deploy/host_contract.py`: `project_id` and `runtime_secret_id` (validated only), pinned runtime and database secret versions, `backup_bucket`, `domain`, `acme_email`, `origin_pull_ca`, and `daily_backups` only with the daily backup tier |
| `/etc/fstab`, `/var/lib/pyracast/{postgres,rabbitmq,gateway,release.lock}`, `/opt/pyracast/releases` | W | Data disk mount `nodev,nosuid,nofail`; `/var/lib/pyracast` root-owned 0755; data directories (UID 70, 999, 10001); the release lock 0600 |
| `/etc/docker/daemon.json`, Docker apt source and key, `/usr/local/sbin/pyracast-network-guard` | W (when they differ) | `local` log driver, iptables backend, IPv6 off, `live-restore`; the guard script |
| `/etc/systemd/system/pyracast.service`, `docker.service.d/pyracast-data.conf` | W (when they differ) | Application unit, Docker mount and guard hooks |
| `/etc/systemd/system/pyracast-{backup,rehearsal,watchdog}.{service,timer}` | W (each release and boot) | Copied from `deploy/systemd/` by `deploy/install-units.sh` |
| iptables chains `DOCKER-USER`, `PYRACAST_METADATA` | W (each Docker and application start) | Drop rule for `169.254.169.254` |
| GitHub `production` environment variables | W (by hand) | `GCP_PROJECT_ID`, `GCP_ZONE`, `GCP_INSTANCE`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_ARTIFACT_REPOSITORY`, `GCP_MIGRATION_SECRET` (the short secret ID), `GCP_MIGRATION_SECRET_VERSION` and `PYRACAST_PUBLIC_ORIGIN` from the output; a release treats the last two as optional (`latest` version, public probe skipped with a warning) |

## Authorization

- Operator: needs project-level rights to create every resource and access to the state bucket; nothing in the repository encodes or checks this.
- Runtime service account (`iam.tf`, VM identity, scope `cloud-platform` so IAM is the only limit): `logging.logWriter` (project), registry reader, `secretAccessor` on runtime and database secrets only, bucket `objectCreator` and `objectViewer` (no delete).
- Deploy service account: registry writer plus custom role with `artifactregistry.tags.delete`, `secretAccessor` on the migration secret only, custom discovery role, and per-instance `compute.osAdminLogin`, IAP tunnel (condition port 22) and `iam.serviceAccountUser` on the runtime account.
- `operator_members`: same per-instance SSH set as the deploy account (root through OS Login) plus a discovery role.
- GitHub: WIF provider `attribute_condition` requires the repository and owner numeric IDs, `refs/heads/main`, the exact `<repository>/.github/workflows/release.yml@refs/heads/main` workflow and environment `production`; `github_deploy` binds the deploy account to the repository ID. `GCP_DEPLOY_ENABLED` is a repository variable outside the output. With `image_rescan_reader`, a separate pool, provider and service account (registry reader only) serve the `image-rescan.yml` workflow alone; the output `image_rescan_setting` carries them.
- Network: 443 only from the hard-coded Cloudflare IPv4 list, 80 from anywhere (ACME), 22 only from the IAP range; all target the runtime service account; no egress rule. The subnet records flow logs (10-minute aggregation, sampling 0.5, all metadata). OS Login on, project SSH keys blocked, serial port off.
- Audit: Cloud Storage `DATA_READ` and `DATA_WRITE` and Secret Manager `DATA_READ` access logs are enabled project-wide.
- Containers: `pyracast-network-guard` drops forwarded traffic to `169.254.169.254` so only host processes can use the VM identity.

## Failure paths

| Condition | Result |
| --- | --- |
| `name`, `domain`, `acme_email`, `data_disk_gb < 10`, a secret version that is not a positive integer, a retention or snapshot period outside 0 to 7 whole days, a non-numeric GitHub id, or an `origin_pull_ca` that is not certificates | Plan error with the variable's `error_message` |
| `zone` not inside `region` | Plan error "zone must be within region." |
| Change would destroy a secret, the backup bucket or the data disk | Plan error from `prevent_destroy` |
| `init` with another prefix than the one holding the state (`backend.hcl.example` and `DEPLOYMENT.md` use `production`) | Empty state: the plan proposes creating everything and apply fails on existing names (standard Terraform behavior, not run) |
| VM replacement or delete while `deletion_protection = true` | Apply error |
| `machine_type` change (resize) on a running VM | Apply fails: `allow_stopping_for_update` is not set (error text present in the pinned provider; not run) |
| Host is not Ubuntu 24.04 amd64 | "This boot script supports Ubuntu 24.04 on amd64 only; nothing was changed.", exit 1 |
| Data disk not visible within 90 s | Startup script exits; first boot leaves no units |
| Disk has a non-ext4 filesystem, or no filesystem but a signature or a nonzero first MiB | "The application data disk must use ext4." or "... is not empty; inspect it before continuing.", no format, exit 1 |
| `/var/lib/pyracast` already mapped to another device in fstab | "A different data disk is already configured; inspect fstab before continuing.", exit 1 |
| apt, Docker key or repository failure, a key without the published fingerprint, or a pinned version the apt repository does not carry | Script stops at the failing line ("Pyracast boot configuration stopped at line N; every step after it is unchanged."); previously enabled units still start at boot with the old files |
| The pinned packages or the Docker configuration need a change while a release holds the lock | "... while the release lock was held; run this script again.", exit 1; a pending marker keeps the Docker restart due |
| Network guard exits non-zero | `docker.service` fails in `ExecStartPre`, so `pyracast.service` (`Requires=`) does not start: fail closed |
| `/var/lib/pyracast/release-blocked` present | `pyracast.service` is skipped by `ConditionPathExists`; the hourly backup skips its run (warning `pyracast_backup_skipped`) and the watchdog checks capacity only; no alert |
| Pinned runtime or database secret version missing or unreadable | `runtime_secrets.py` "Secret preparation failed (<category>): ..." and the `pyracast_secrets_failed` alert; the first release aborts in `install-release.sh` before maintenance, later `pyracast.service` fails in `ExecStartPre` |
| Readiness unreachable (no release yet, DNS-only record, outage) | Uptime alert email after two or more checker locations fail for 5 minutes (inferred from the configuration) |
| A `GCP_*` variable empty in GitHub, a malformed `GCP_ARTIFACT_REPOSITORY`, `GCP_INSTANCE`, `GCP_ZONE` or `GCP_MIGRATION_SECRET_VERSION` | Release step "Validate deployment configuration" exits 1 before cloud authentication. Other values (project, WIF provider, service account, migration secret) are only checked for emptiness and fail later at authentication or Secret Manager access |
| OIDC token from another branch, workflow, environment or repository, or after a repository rename | Token exchange refused, release cannot authenticate |
| `fmt`, `init -lockfile=readonly` or `validate` fails in CI, or the boot script test fails | CI job fails, blocking release authorization |

## Rules and invariants

- Terraform never holds secret values: secrets are created without versions, and `host.json` carries only version resource names pinned to positive integers (`runtime_secret_version`, `database_secret_version`). The migration secret is outside `host.json`: Terraform's `migration_secret_version` only feeds the `GCP_MIGRATION_SECRET_VERSION` output, and an empty variable in GitHub makes the release read `latest`.
- The startup script runs on every boot and changes the host only where what it would write differs. A metadata change is applied in place without recreating the VM, but takes effect only at the next boot or a manual rerun. `host.json` keys are typed by `deploy/host_contract.py`; a release refuses an unknown, ill-typed or missing key.
- Docker restarts only for its own changed configuration, never while a release holds the lock, and a pending restart stays due until it ran. The restart propagates to the `Requires=`-bound `pyracast.service` (Compose stop, then up) even though `daemon.json` sets `live-restore` (from unit semantics; not observed on a host). The script never restarts `pyracast.service`; the unit restarts a failed start itself (`Restart=on-failure`, 30 s apart, at most 5 starts in 1800 s).
- The startup script formats the data disk only when it has no filesystem, no signature and a zeroed first MiB. The `device_name` from `local.data_device_name` in `main.tf` and `/dev/disk/by-id/google-pyracast-data` in the script must stay in step.
- Only `pyracast.service` carries `ConditionPathExists=!/var/lib/pyracast/release-blocked`; it starts only when `/opt/pyracast/current/deploy/compose.yaml` exists, runs with `UMask=0077`, and its stop takes `/var/lib/pyracast/release.lock`. The backup, rehearsal and watchdog units come from the release, not from the boot script, and run only when their script exists under `current`; the backup and the watchdog check the marker themselves.
- The network guard runs before and after Docker starts and before the application starts, and after the packages it needs on the first boot and whenever the guard changed (marker `/etc/pyracast/network-guard-pending`), so the metadata drop rule is present whenever containers run. It relies on the `iptables` firewall backend set in `daemon.json`.
- Docker, containerd and Compose are exact apt versions for Ubuntu 24.04 `noble` amd64, installed only when missing and held with `apt-mark hold`; the repository key must carry its published fingerprint. `vm_image` defaults to an unpinned image family. Terraform `= 1.17.2` and provider `= 8.11.0` are exact, with a committed lock file.
- Registry cleanup starts in dry-run (`artifact_cleanup_dry_run`): delete versions older than 30 days, but keep the 20 newest and all `production*` tags. Tags are mutable.
- Backup bucket: versioning on and one `Delete` rule at age 7 days for every generation, so no backup outlives seven days; soft delete disabled; the runtime account cannot delete. Off by default: `backup_retention_days` (unlocked retention policy), `backup_daily_tier` (`daily/` objects kept 30 days, the 7-day rule limited to `postgres/`, `daily_backups` in `host.json`) and `data_disk_snapshot_days` (daily disk snapshots).
- Log-based alerts fire on `pyracast_backup_failed`, `pyracast_runtime_unhealthy`, `pyracast_host_capacity`, `pyracast_certificate_expiring`, `pyracast_secrets_failed` and `pyracast_origin_pull_unenforced` in log `pyracast-operations`, at most one notification per hour, auto-closed after 24 hours; a missing watchdog heartbeat for fifteen minutes and seven thresholds on the security lines the `api` service logs in blocking mode alert too. The single email channel uses `acme_email`.
- APIs are never disabled on destroy (`disable_on_destroy = false`).

## Tests

- `.github/workflows/ci.yml`: `terraform fmt -check -recursive`, `init -backend=false -input=false -lockfile=readonly`, `validate`, `deploy/test-boot.sh` (the rendered boot script on a stubbed host: first, unchanged, changed and upgraded boots, restarts and package changes under the release lock, the guard retry, a refused keyring, used disk and host) and `systemd-analyze verify` of the release units. No plan against a real project.
- `tests/test_boot_script.py` reads the template as text (placeholders, ordering, fingerprint, release lock, restart rules, start limit). `tests/test_host_contract.py`: what Terraform writes into `host.json` equals the contract, and every key a host helper reads is a setting.
- `tests/test_operations_contracts.py` reads `infra/terraform` as text: every alert policy event in `monitoring.tf` is reported by a host script and every `ERROR` event has a policy; the HTTPS firewall ranges equal the IPv4 ranges the gateway trusts; the `pyracast.service` stop timeout covers every Compose stop grace period; paid backup protection is off by default; the bucket keeps every generation seven days; audit and flow logs are on.
- Adjacent pytest (consumers only): `tests/test_deployment_secrets.py`, `tests/test_deployment_backup.py` (`deploy/operations_log.py` `report`), `tests/test_deployment_watchdog.py`, `tests/test_deployment_helpers.py` (host helpers parse as Python 3.12, the Ubuntu 24.04 `python3`). `deploy/test-release.sh` uses a hand-written `host.json` without `origin_pull_ca` and `daily_backups`.
- Gaps: no Playwright test touches `infra/terraform` and nothing plans it; `templates/network-guard.sh` is stubbed in the boot test and neither template is shellchecked; `docs/DEPLOYMENT.md` ("Verification boundary") states that `deploy/smoke.sh` does not prove GCP IAM, the metadata firewall rules, ACME or public DNS.
