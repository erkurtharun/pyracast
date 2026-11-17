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

## Workspace and collaboration

| Flow | Covers |
| --- | --- |
| [workspace-switching](workspace-switching.md) | Workspace list, inbox counters and switching |
| [member-invitation](member-invitation.md) | Invite a member by email, list and cancel pending invitations |
| [invitation-acceptance](invitation-acceptance.md) | Accept a workspace invitation |
| [member-removal](member-removal.md) | List workspace members and remove a viewer |
| [change-request-submission](change-request-submission.md) | Viewer proposes a production or outage change and follows its outcome |
| [change-request-review](change-request-review.md) | Administrator reviews the change-request inbox |

## Plant onboarding and approval

| Flow | Covers |
| --- | --- |
| [plant-portfolio](plant-portfolio.md) | Browse the plant portfolio, open a plant and move between its tabs |
| [site-onboarding](site-onboarding.md) | Onboard a site: location plus plant submitted for approval |
| [provider-connection-setup](provider-connection-setup.md) | Configure and validate a plant's provider account and site binding |
| [plant-approval](plant-approval.md) | Approve, reject or suspend a plant |

## Forecast collection

| Flow | Covers |
| --- | --- |
| [collection-schedule](collection-schedule.md) | Configure forecast collection times for a site |
| [forecast-collection-scheduling](forecast-collection-scheduling.md) | Forecast collection scheduling and publishing of due jobs |
| [forecast-collection-execution](forecast-collection-execution.md) | Run a collection job: fetch the provider, store the snapshot and publish the result |
| [provider-quota-and-backoff](provider-quota-and-backoff.md) | Provider quota ledger, forecast budget, cooldown and credential blocks |
| [historical-reference-import](historical-reference-import.md) | Import a historical provider export as reference data |
| [reference-capacity-normalization](reference-capacity-normalization.md) | Rescale reference snapshots to another binding capacity |

## Forecast reading

| Flow | Covers |
| --- | --- |
| [forecast-view](forecast-view.md) | View a plant's generation forecast |
| [irradiance-forecast-view](irradiance-forecast-view.md) | View the regional irradiance forecast and weather reference |
| [forecast-performance](forecast-performance.md) | Compare measured production with forecasts |
| [csv-export](csv-export.md) | Download displayed data as CSV |

## Production and metering

| Flow | Covers |
| --- | --- |
| [production-history](production-history.md) | View a production day and correct intervals |
| [production-csv-import](production-csv-import.md) | Import production from a CSV file |
| [epias-connection](epias-connection.md) | Connect or disconnect a plant's EPIAS reported-production feed |
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
| [reminder-delivery](reminder-delivery.md) | Reminder delivery |

## Configuration and lifecycle

| Flow | Covers |
| --- | --- |
| [plant-configuration-revision](plant-configuration-revision.md) | Edit a plant's name and technical configuration |
| [location-change](location-change.md) | Request a site location change and review it |
| [plant-trash-and-restore](plant-trash-and-restore.md) | Move a site or plant to the recycle bin and restore it |
| [trash-retention-purge](trash-retention-purge.md) | Purge trashed sites and plants after 30 days |

## Platform administration

| Flow | Covers |
| --- | --- |
| [platform-admin-bootstrap](platform-admin-bootstrap.md) | Create the first platform administrator and provision TOTP by CLI |
| [admin-console-access](admin-console-access.md) | Platform console access: MFA gate, SPA hand-off, shared shell and logout |
| [admin-overview-and-operations](admin-overview-and-operations.md) | Console overview, plant list and operations health |
| [admin-record-browser](admin-record-browser.md) | Browse platform records in the stock Django admin |
| [workspace-admin-reassignment](workspace-admin-reassignment.md) | List workspaces and reassign the workspace administrator |
| [platform-settings](platform-settings.md) | Set the global recipients-per-plant limit |
| [provider-credential-recovery](provider-credential-recovery.md) | Rotate a provider API key or clear a provider cooldown |

## Operations and delivery

| Flow | Covers |
| --- | --- |
| [dispatcher-cycle](dispatcher-cycle.md) | Dispatcher scheduling and publishing cycle |
| [worker-runtime](worker-runtime.md) | Worker processes, RabbitMQ broker and graceful shutdown |
| [delivery-health-check](delivery-health-check.md) | Collection and delivery health report |
| [host-supervision-and-alerting](host-supervision-and-alerting.md) | Host watchdog, health probes and alerts |
| [backup-and-restore-rehearsal](backup-and-restore-rehearsal.md) | Hourly and pre-release database backups and restore rehearsal |
| [database-reset-and-restore](database-reset-and-restore.md) | Reset the production database or restore it from a verified archive |
| [database-migration](database-migration.md) | Database service, role bootstrap and locked schema migrations |
| [release-pipeline](release-pipeline.md) | CI image build, verification and production release |
| [infrastructure-provisioning](infrastructure-provisioning.md) | Provision the Google Cloud environment with Terraform |
| [secret-provisioning](secret-provisioning.md) | Generate, upload and materialise production secrets |
| [encryption-key-rotation](encryption-key-rotation.md) | Rotate the application key and re-encrypt stored secrets |
| [gmail-sender-authorization](gmail-sender-authorization.md) | Authorise the send-only Gmail sender |
| [edge-gateway-and-tls](edge-gateway-and-tls.md) | Edge gateway, TLS and the API process entry |
| [local-dev-stack](local-dev-stack.md) | Start, stop and inspect the local development stack |
| [developer-quality-gate](developer-quality-gate.md) | Run the backend and frontend quality gate locally and in CI |
| [dependency-and-code-scanning](dependency-and-code-scanning.md) | Scan code, dependencies and images |
| [offline-irradiance-calibration](offline-irradiance-calibration.md) | Calibrate the irradiance inverse model offline from exported files |
| [fixture-demo](fixture-demo.md) | Create a synthetic demo plant locally |

## Cross-cutting mechanisms

| Flow | Covers |
| --- | --- |
| [tenant-authorization](tenant-authorization.md) | Tenant, role and platform-administrator authorization |
| [session-and-csrf](session-and-csrf.md) | Session lifetime, request serialisation, CSRF and session revocation |
| [rate-limiting](rate-limiting.md) | Rate limiting and the database-backed authentication cache |
| [audit-trail](audit-trail.md) | Audit trail: region, plant, security and administration events |
| [api-error-contract](api-error-contract.md) | API response contract: errors, conflicts and pagination |
| [api-schema-and-client-types](api-schema-and-client-types.md) | OpenAPI schema and generated client types |
| [provider-detail-redaction](provider-detail-redaction.md) | Hide provider details from customer API responses |
| [i18n-and-language](i18n-and-language.md) | Language selection, persistence and rendering |
| [security-headers-and-proxy-trust](security-headers-and-proxy-trust.md) | Security headers, CSP and trusted proxy chain |
| [email-transport](email-transport.md) | Email transport: SMTP and Gmail backends |
| [account-mail-notifications](account-mail-notifications.md) | Account mails: codes, security notifications and anti-enumeration notices |
| [frontend-data-layer](frontend-data-layer.md) | Frontend shared layer: HTTP clients, hooks, polling, forms and UI primitives |
