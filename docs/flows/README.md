# Flow map

One page per flow: entry points, a sequence diagram, tables touched, authorization, failure paths,
non-obvious rules and the tests that pin it. Each page was traced from the code and checked by an
independent reviewer. Read only the flow you are about to change; the table schema is in
[DATA_MODEL.md](../DATA_MODEL.md).

When a change alters a flow, update its page in the same commit. A new flow gets a page with the same
sections and a row here.

## Identity and access

| Flow | Covers |
| --- | --- |
| [public-site](public-site.md) | Browse the public marketing and legal site |
| [company-signup](company-signup.md) | Company signup and email verification |
| [sign-in-and-sign-out](sign-in-and-sign-out.md) | Sign in and sign out |
| [password-reset](password-reset.md) | Reset a forgotten password by emailed code |
| [password-change](password-change.md) | Change or set the account password while signed in |
| [social-sign-in](social-sign-in.md) | Sign in or sign up with Google or Microsoft |
| [linked-accounts](linked-accounts.md) | Connect or disconnect a social account |
| [two-factor-management](two-factor-management.md) | Two-factor management |
| [step-up-reauthentication](step-up-reauthentication.md) | Reauthenticate before a sensitive account change |
| [account-email-change](account-email-change.md) | Change the account email and verify it by code |
| [app-entry-routing](app-entry-routing.md) | Open the app: session bootstrap and routing |
| [installed-app](installed-app.md) | Install and run the app: manifest, worker and releases |

## Workspace and collaboration

| Flow | Covers |
| --- | --- |
| [workspace-switching](workspace-switching.md) | Workspace list, inbox counters and switching |
| [member-invitation](member-invitation.md) | Invite a member by email, list, resend and cancel invitations |
| [invitation-acceptance](invitation-acceptance.md) | Accept a workspace invitation |
| [member-removal](member-removal.md) | List workspace members and remove a viewer |
| [change-request-submission](change-request-submission.md) | Viewer proposes a production or outage change and follows its outcome |
| [change-request-review](change-request-review.md) | Administrator reviews the change-request inbox |

## Plant onboarding and approval

| Flow | Covers |
| --- | --- |
| [plant-portfolio](plant-portfolio.md) | Browse the plant portfolio, open a plant and move between its tabs |
| [site-onboarding](site-onboarding.md) | Onboard a site: location plus plant submitted for approval, by the workspace administrator or in the console |
| [provider-connection-setup](provider-connection-setup.md) | Configure and validate a plant's provider account and site binding |
| [plant-approval](plant-approval.md) | Approve, reject or suspend a plant, and resubmit a rejected one |

## Forecast collection

| Flow | Covers |
| --- | --- |
| [collection-schedule](collection-schedule.md) | Configure forecast collection times for a site and read its collection status |
| [forecast-collection-scheduling](forecast-collection-scheduling.md) | Forecast collection scheduling and publishing of due jobs |
| [forecast-collection-execution](forecast-collection-execution.md) | Run a collection job: fetch the provider, store the snapshot and publish the result |
| [provider-quota-and-backoff](provider-quota-and-backoff.md) | Provider quota ledger, forecast budget, cooldown and credential blocks |
| [historical-reference-import](historical-reference-import.md) | Import a historical provider export as reference data |
| [reference-capacity-normalization](reference-capacity-normalization.md) | Rescale reference snapshots to another binding capacity |
| [provider-snapshot-restore](provider-snapshot-restore.md) | Restore live provider snapshots from a reset export |

## Forecast reading

| Flow | Covers |
| --- | --- |
| [forecast-view](forecast-view.md) | View a plant's generation forecast |
| [irradiance-forecast-view](irradiance-forecast-view.md) | View the regional irradiance forecast |
| [forecast-performance](forecast-performance.md) | Compare measured production with forecasts |
| [csv-export](csv-export.md) | Download displayed data as CSV |

## Production and metering

| Flow | Covers |
| --- | --- |
| [production-history](production-history.md) | View a production day and correct intervals |
| [production-csv-import](production-csv-import.md) | Import production from a CSV file |
| [epias-connection](epias-connection.md) | Connect or disconnect a plant's EPIAS reported-production feed in the admin console, and read its status |
| [epias-collection](epias-collection.md) | Collect EPIAS reported production and settlement injection |

## Calibration

| Flow | Covers |
| --- | --- |
| [calibration-run](calibration-run.md) | Prepare or request a calibration run and fit it in a worker |
| [calibration-activation](calibration-activation.md) | Review calibration runs, activate one or reset to baseline |
| [calibration-automation](calibration-automation.md) | Automatic calibration: toggle, daily check and promotion |

## Outages

| Flow | Covers |
| --- | --- |
| [outage-management](outage-management.md) | Report, edit, resolve and cancel plant outages |

## Notifications

| Flow | Covers |
| --- | --- |
| [reminder-configuration](reminder-configuration.md) | Configure forecast email reminders for a site |
| [reminder-delivery](reminder-delivery.md) | Schedule, prepare and send forecast reminder emails |

## Configuration and lifecycle

| Flow | Covers |
| --- | --- |
| [plant-configuration-revision](plant-configuration-revision.md) | Edit a plant's name and technical configuration |
| [location-change](location-change.md) | Request a site location change, withdraw or review it, and correct a location from the console |
| [plant-trash-and-restore](plant-trash-and-restore.md) | Move a site to the recycle bin and restore it |
| [trash-retention-purge](trash-retention-purge.md) | Purge trashed sites after 30 days |

## Platform administration

| Flow | Covers |
| --- | --- |
| [platform-admin-bootstrap](platform-admin-bootstrap.md) | Create a platform administrator and reset an authenticator by CLI |
| [account-erasure](account-erasure.md) | Erase an account by anonymization from the CLI |
| [admin-console-access](admin-console-access.md) | Platform console access: MFA gate, SPA hand-off, confirmation, shared shell and logout |
| [admin-overview-and-operations](admin-overview-and-operations.md) | Console overview, plant list and operations health |
| [admin-record-browser](admin-record-browser.md) | Browse platform records in the read-only console lists |
| [admin-workspace-management](admin-workspace-management.md) | Create and manage workspaces from the console: creation, rename, invitations, member removal |
| [workspace-admin-reassignment](workspace-admin-reassignment.md) | Reassign the workspace administrator from the console: impact list, transfer, automation shutdown, invitation revocation and role mail |
| [platform-settings](platform-settings.md) | Set the global recipients-per-plant limit and review the plants it pauses |
| [provider-credential-recovery](provider-credential-recovery.md) | Provider accounts console: replace an API key with automatic revalidation, or clear a provider delay |

## Operations and delivery

| Flow | Covers |
| --- | --- |
| [dispatcher-cycle](dispatcher-cycle.md) | Dispatcher scheduling and publishing cycle |
| [worker-runtime](worker-runtime.md) | Worker processes, RabbitMQ broker and graceful shutdown |
| [delivery-health-check](delivery-health-check.md) | Collection, delivery, outbound mail, calibration and broker health report |
| [host-supervision-and-alerting](host-supervision-and-alerting.md) | Host watchdog, health probes and alerts |
| [backup-and-restore-rehearsal](backup-and-restore-rehearsal.md) | Hourly and pre-release database backups and the weekly restore rehearsal |
| [database-reset-and-restore](database-reset-and-restore.md) | Reset the production database or restore it from a verified archive, with its Solcast export |
| [database-migration](database-migration.md) | Database service, role convergence and locked schema migrations |
| [release-pipeline](release-pipeline.md) | CI image build and scan, release authorization (including the early release of a proven squash merge) and the production release |
| [infrastructure-provisioning](infrastructure-provisioning.md) | Provision the Google Cloud environment with Terraform |
| [secret-provisioning](secret-provisioning.md) | Generate, upload and materialise production secrets |
| [encryption-key-rotation](encryption-key-rotation.md) | Rotate the application key, check and re-encrypt stored secrets |
| [gmail-sender-authorization](gmail-sender-authorization.md) | Authorise the send-only Gmail sender |
| [edge-gateway-and-tls](edge-gateway-and-tls.md) | Edge gateway, TLS, per-client limits, maintenance notice and the API process entry |
| [local-dev-stack](local-dev-stack.md) | Start, stop and inspect the local development stack |
| [developer-quality-gate](developer-quality-gate.md) | Run the backend and frontend quality gate locally and in CI |
| [dependency-and-code-scanning](dependency-and-code-scanning.md) | Scan code, dependencies and images, including the daily image rescan |
| [offline-irradiance-calibration](offline-irradiance-calibration.md) | Calibrate the irradiance inverse model offline from exported files |
| [fixture-demo](fixture-demo.md) | Create or resume a synthetic demo plant and its first forecast locally |

## Cross-cutting mechanisms

| Flow | Covers |
| --- | --- |
| [tenant-authorization](tenant-authorization.md) | Route access policy, tenant roles and platform-administrator authorization |
| [session-and-csrf](session-and-csrf.md) | Session lifetime, request serialisation, session tag, CSRF, signed-in devices and session revocation |
| [rate-limiting](rate-limiting.md) | Rate limiting: allauth limits in the database-backed authentication cache, route throttles, invitation budgets and gateway client limits |
| [audit-trail](audit-trail.md) | Audit trail: region, plant, ledger and security-line events |
| [api-error-contract](api-error-contract.md) | API response contract: error body and codes, conflicts and pagination |
| [api-schema-and-client-types](api-schema-and-client-types.md) | OpenAPI schema and generated client types (build-time only, no schema route) |
| [provider-detail-redaction](provider-detail-redaction.md) | Hide provider details from customer API responses |
| [i18n-and-language](i18n-and-language.md) | Language selection, persistence and rendering |
| [security-headers-and-proxy-trust](security-headers-and-proxy-trust.md) | Security headers, CSP and trusted proxy chain |
| [email-transport](email-transport.md) | Email transport: SMTP and Gmail backends, the outbound mail outbox and retries |
| [account-mail-notifications](account-mail-notifications.md) | Account mails: stored intents, codes, security notifications and anti-enumeration notices |
| [frontend-data-layer](frontend-data-layer.md) | Frontend shared layer: HTTP client, queries, polling, forms and UI primitives |
