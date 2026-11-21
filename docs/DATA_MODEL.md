# Data model

> Generated from the Django migrations; do not edit by hand. Regenerate with
> `uv run python scripts/generate_er_diagram.py`; its `--check` option fails when this file is stale.

Column types are PostgreSQL types with every character outside letters, digits and
underscores replaced by `_`. Keys: `PK` primary, `FK` foreign, `UK` unique. Column
comments mark nullable columns and name the referenced column of a foreign key.
Relationship labels name the foreign key column on the child table. Constraint and
index definitions are the DDL Django emits, with whitespace other than spaces and
non-printing characters shown as `\uXXXX` escapes. Implicit indexes on foreign key
and `db_index` columns are not listed.

Foreign keys from model fields are `DEFERRABLE INITIALLY DEFERRED`.
Composite foreign keys and triggers exist only in `RunSQL` migrations; they are read
from the migration files and listed with the constraints of their table as written
there, under their latest name, not drawn as relationships. Schema changes made by
migration Python code are not visible to this document, and `django_migrations` has
no model and is not shown.

## Overview

60 tables and 118 single-column foreign keys.

```mermaid
erDiagram
    accounts_membership
    accounts_tenant
    accounts_userlanguagepreference
    accounts_workspaceadminchange
    accounts_workspaceinvitation
    accounts_workspacememberremoval
    pyracast_authentication_cache
    regions_auditevent
    regions_region
    regions_regionlocationchangerequest
    connections_provideraccount
    connections_sitebinding
    forecasts_collectionjob
    forecasts_collectionschedule
    forecasts_dispatcherstatus
    forecasts_forecastresult
    forecasts_forecastsnapshot
    forecasts_historicalreferenceimport
    forecasts_referencecapacitynormalization
    forecasts_requestattempt
    plants_calibrationactivation
    plants_calibrationrun
    plants_forecastissuance
    plants_plant
    plants_plantconfiguration
    plants_plantevent
    plants_plantsubmission
    plants_productionobservation
    incidents_incidentrevision
    incidents_plantincident
    notifications_deliveryrecipient
    notifications_notificationdelivery
    notifications_notificationsettings
    notifications_outboundmail
    notifications_reminderrecipient
    notifications_reminderrecipienttie
    notifications_reminderschedule
    metering_epiascollectionjob
    metering_epiasconnection
    metering_epiasconnectionrevision
    metering_epiassnapshot
    change_requests_changerequest
    change_requests_incidentchange
    change_requests_productionchangeitem
    auth_group
    auth_group_permissions
    auth_permission
    auth_user
    auth_user_groups
    auth_user_user_permissions
    django_content_type
    django_session
    django_admin_log
    account_emailaddress
    account_emailconfirmation
    socialaccount_socialaccount
    socialaccount_socialapp
    socialaccount_socialtoken
    mfa_authenticator
    usersessions_usersession
    auth_user ||--o{ accounts_membership : "user_id"
    accounts_tenant ||--o{ accounts_membership : "tenant_id"
    auth_user ||--o| accounts_userlanguagepreference : "user_id"
    auth_user ||--o{ accounts_workspaceadminchange : "actor_id"
    auth_user ||--o{ accounts_workspaceadminchange : "administrator_id"
    auth_user |o--o{ accounts_workspaceadminchange : "previous_administrator_id"
    accounts_tenant ||--o{ accounts_workspaceadminchange : "tenant_id"
    auth_user ||--o{ accounts_workspaceinvitation : "created_by_id"
    accounts_tenant ||--o{ accounts_workspaceinvitation : "tenant_id"
    auth_user |o--o{ accounts_workspaceinvitation : "accepted_by_id"
    auth_user ||--o{ accounts_workspacememberremoval : "actor_id"
    accounts_tenant ||--o{ accounts_workspacememberremoval : "tenant_id"
    auth_user ||--o{ accounts_workspacememberremoval : "user_id"
    auth_user ||--o{ regions_auditevent : "actor_id"
    connections_provideraccount |o--o{ regions_auditevent : "provider_account_id"
    regions_region |o--o{ regions_auditevent : "region_id"
    connections_sitebinding |o--o{ regions_region : "approved_binding_id"
    accounts_tenant ||--o{ regions_region : "tenant_id"
    regions_region ||--o{ regions_regionlocationchangerequest : "region_id"
    auth_user ||--o{ regions_regionlocationchangerequest : "requested_by_id"
    auth_user |o--o{ regions_regionlocationchangerequest : "reviewed_by_id"
    auth_user |o--o{ connections_provideraccount : "authorized_by_id"
    connections_provideraccount ||--o{ connections_sitebinding : "provider_account_id"
    regions_region ||--o{ connections_sitebinding : "region_id"
    connections_sitebinding ||--o{ forecasts_collectionjob : "binding_id"
    forecasts_historicalreferenceimport |o--o{ forecasts_collectionjob : "reference_import_id"
    regions_region ||--o| forecasts_collectionschedule : "region_id"
    forecasts_forecastsnapshot ||--o| forecasts_forecastresult : "snapshot_id"
    connections_sitebinding ||--o{ forecasts_forecastsnapshot : "binding_id"
    forecasts_collectionjob ||--o| forecasts_forecastsnapshot : "job_id"
    connections_sitebinding ||--o{ forecasts_historicalreferenceimport : "binding_id"
    auth_user ||--o{ forecasts_historicalreferenceimport : "imported_by_id"
    auth_user ||--o{ forecasts_referencecapacitynormalization : "authorized_by_id"
    forecasts_forecastsnapshot ||--o| forecasts_referencecapacitynormalization : "snapshot_id"
    forecasts_forecastsnapshot ||--o{ forecasts_referencecapacitynormalization : "source_snapshot_id"
    connections_sitebinding ||--o{ forecasts_referencecapacitynormalization : "target_binding_id"
    connections_sitebinding |o--o{ forecasts_requestattempt : "binding_id"
    forecasts_collectionjob |o--o{ forecasts_requestattempt : "job_id"
    connections_provideraccount ||--o{ forecasts_requestattempt : "provider_account_id"
    auth_user |o--o{ plants_calibrationactivation : "actor_id"
    plants_calibrationrun |o--o{ plants_calibrationactivation : "calibration_id"
    plants_plant ||--o{ plants_calibrationactivation : "plant_id"
    plants_plantconfiguration ||--o{ plants_calibrationactivation : "configuration_id"
    connections_sitebinding ||--o{ plants_calibrationrun : "binding_id"
    plants_calibrationactivation |o--o{ plants_calibrationrun : "comparison_activation_id"
    auth_user ||--o{ plants_calibrationrun : "requested_by_id"
    plants_plant ||--o{ plants_calibrationrun : "plant_id"
    plants_plantconfiguration ||--o{ plants_calibrationrun : "configuration_id"
    plants_calibrationrun |o--o{ plants_forecastissuance : "calibration_id"
    forecasts_forecastsnapshot ||--o{ plants_forecastissuance : "snapshot_id"
    plants_plant ||--o{ plants_forecastissuance : "plant_id"
    plants_plantconfiguration ||--o{ plants_forecastissuance : "configuration_id"
    auth_user |o--o{ plants_plant : "automatic_calibration_enabled_by_id"
    regions_region ||--o{ plants_plant : "region_id"
    plants_plantconfiguration |o--o{ plants_plant : "current_configuration_id"
    connections_sitebinding ||--o{ plants_plantconfiguration : "binding_id"
    auth_user ||--o{ plants_plantconfiguration : "created_by_id"
    plants_plant ||--o{ plants_plantconfiguration : "plant_id"
    regions_region ||--o{ plants_plantconfiguration : "region_id"
    auth_user |o--o{ plants_plantevent : "actor_id"
    plants_plant ||--o{ plants_plantevent : "plant_id"
    plants_plant ||--o| plants_plantsubmission : "plant_id"
    auth_user ||--o{ plants_plantsubmission : "submitted_by_id"
    plants_plantconfiguration |o--o{ plants_plantsubmission : "base_configuration_id"
    plants_plantconfiguration ||--o{ plants_productionobservation : "configuration_id"
    plants_plant ||--o{ plants_productionobservation : "plant_id"
    auth_user ||--o{ plants_productionobservation : "recorded_by_id"
    plants_productionobservation |o--o| plants_productionobservation : "supersedes_id"
    auth_user ||--o{ incidents_incidentrevision : "actor_id"
    incidents_plantincident ||--o{ incidents_incidentrevision : "incident_id"
    auth_user ||--o{ incidents_plantincident : "created_by_id"
    plants_plantconfiguration ||--o{ incidents_plantincident : "declared_configuration_id"
    plants_plant ||--o{ incidents_plantincident : "plant_id"
    auth_user ||--o{ incidents_plantincident : "updated_by_id"
    notifications_notificationdelivery ||--o{ notifications_deliveryrecipient : "delivery_id"
    connections_sitebinding ||--o{ notifications_notificationdelivery : "binding_id"
    forecasts_forecastsnapshot |o--o{ notifications_notificationdelivery : "snapshot_id"
    notifications_reminderschedule ||--o{ notifications_notificationdelivery : "schedule_id"
    accounts_workspaceinvitation |o--o{ notifications_outboundmail : "invitation_id"
    notifications_reminderschedule ||--o{ notifications_reminderrecipient : "schedule_id"
    notifications_reminderrecipient ||--o{ notifications_reminderrecipienttie : "recipient_id"
    auth_user |o--o{ notifications_reminderschedule : "configured_by_id"
    regions_region ||--o{ notifications_reminderschedule : "region_id"
    plants_plantconfiguration ||--o{ metering_epiascollectionjob : "configuration_id"
    plants_plant ||--o{ metering_epiascollectionjob : "plant_id"
    metering_epiasconnection ||--o{ metering_epiascollectionjob : "connection_id"
    auth_user ||--o{ metering_epiasconnection : "connected_by_id"
    plants_plant ||--o| metering_epiasconnection : "plant_id"
    metering_epiasconnection ||--o{ metering_epiasconnectionrevision : "connection_id"
    plants_plantconfiguration ||--o{ metering_epiassnapshot : "configuration_id"
    metering_epiasconnection ||--o{ metering_epiassnapshot : "connection_id"
    metering_epiascollectionjob ||--o{ metering_epiassnapshot : "job_id"
    plants_plantconfiguration ||--o{ change_requests_changerequest : "configuration_id"
    plants_plant ||--o{ change_requests_changerequest : "plant_id"
    auth_user ||--o{ change_requests_changerequest : "proposed_by_id"
    auth_user |o--o{ change_requests_changerequest : "reviewed_by_id"
    incidents_incidentrevision |o--o{ change_requests_incidentchange : "applied_revision_id"
    incidents_plantincident |o--o{ change_requests_incidentchange : "incident_id"
    change_requests_changerequest ||--o| change_requests_incidentchange : "request_id"
    plants_productionobservation |o--o{ change_requests_productionchangeitem : "applied_observation_id"
    plants_productionobservation |o--o{ change_requests_productionchangeitem : "expected_head_id"
    change_requests_changerequest ||--o{ change_requests_productionchangeitem : "request_id"
    auth_group ||--o{ auth_group_permissions : "group_id"
    auth_permission ||--o{ auth_group_permissions : "permission_id"
    django_content_type ||--o{ auth_permission : "content_type_id"
    auth_user ||--o{ auth_user_groups : "user_id"
    auth_group ||--o{ auth_user_groups : "group_id"
    auth_user ||--o{ auth_user_user_permissions : "user_id"
    auth_permission ||--o{ auth_user_user_permissions : "permission_id"
    django_content_type |o--o{ django_admin_log : "content_type_id"
    auth_user ||--o{ django_admin_log : "user_id"
    auth_user ||--o{ account_emailaddress : "user_id"
    account_emailaddress ||--o{ account_emailconfirmation : "email_address_id"
    auth_user ||--o{ socialaccount_socialaccount : "user_id"
    socialaccount_socialaccount ||--o{ socialaccount_socialtoken : "account_id"
    socialaccount_socialapp |o--o{ socialaccount_socialtoken : "app_id"
    auth_user ||--o{ mfa_authenticator : "user_id"
    auth_user ||--o{ usersessions_usersession : "user_id"
```

## accounts (`pyracast.accounts`)

```mermaid
erDiagram
    accounts_membership {
        bigint id PK
        varchar_6 role
        integer user_id FK "references auth_user.id"
        bigint tenant_id FK "references accounts_tenant.id"
    }
    accounts_tenant {
        bigint id PK
        varchar_200 name
    }
    accounts_userlanguagepreference {
        bigint id PK
        varchar_2 language
        integer user_id FK, UK "references auth_user.id"
    }
    accounts_workspaceadminchange {
        bigint id PK
        timestamp_with_time_zone created_at
        integer actor_id FK "references auth_user.id"
        integer administrator_id FK "references auth_user.id"
        integer previous_administrator_id FK "nullable; references auth_user.id"
        bigint tenant_id FK "references accounts_tenant.id"
    }
    accounts_workspaceinvitation {
        bigint id PK
        varchar_254 email
        varchar_64 token_hash UK "nullable"
        timestamp_with_time_zone created_at
        timestamp_with_time_zone expires_at
        timestamp_with_time_zone accepted_at "nullable"
        varchar_9 delivery_status
        integer created_by_id FK "references auth_user.id"
        bigint tenant_id FK "references accounts_tenant.id"
        varchar_6 role
        timestamp_with_time_zone revoked_at "nullable"
        varchar_21 revoked_reason
        integer accepted_by_id FK "nullable; references auth_user.id"
    }
    accounts_workspacememberremoval {
        bigint id PK
        timestamp_with_time_zone created_at
        integer actor_id FK "references auth_user.id"
        bigint tenant_id FK "references accounts_tenant.id"
        integer user_id FK "references auth_user.id"
    }
    pyracast_authentication_cache {
        varchar_255 cache_key PK
        jsonb value
        timestamp_with_time_zone expires "nullable"
    }
    auth_user ||--o{ accounts_membership : "user_id"
    accounts_tenant ||--o{ accounts_membership : "tenant_id"
    auth_user ||--o| accounts_userlanguagepreference : "user_id"
    auth_user ||--o{ accounts_workspaceadminchange : "actor_id"
    auth_user ||--o{ accounts_workspaceadminchange : "administrator_id"
    auth_user |o--o{ accounts_workspaceadminchange : "previous_administrator_id"
    accounts_tenant ||--o{ accounts_workspaceadminchange : "tenant_id"
    auth_user ||--o{ accounts_workspaceinvitation : "created_by_id"
    accounts_tenant ||--o{ accounts_workspaceinvitation : "tenant_id"
    auth_user |o--o{ accounts_workspaceinvitation : "accepted_by_id"
    auth_user ||--o{ accounts_workspacememberremoval : "actor_id"
    accounts_tenant ||--o{ accounts_workspacememberremoval : "tenant_id"
    auth_user ||--o{ accounts_workspacememberremoval : "user_id"
```

### Constraints and indexes

#### `accounts_membership`

- `membership_role_chk`: `CHECK ("role" IN ('admin', 'viewer'))`
- `membership_tenant_administrator_uniq`: `UNIQUE INDEX ("tenant_id") WHERE "role" = 'admin'`
- `membership_tenant_user_uniq`: `UNIQUE ("tenant_id", "user_id")`

#### `accounts_tenant`

- `tenant_name_chk`: `CHECK (NOT ("name"::text ~  E'^\\s*$'))`

#### `accounts_userlanguagepreference`

- `userlanguagepreference_language_chk`: `CHECK ("language" IN ('en', 'tr'))`

#### `accounts_workspaceadminchange`

- `workspaceadminchange_administrator_chk`: `CHECK (NOT ("administrator_id" = ("previous_administrator_id") AND "previous_administrator_id" IS NOT NULL))`

#### `accounts_workspaceinvitation`

- `workspaceinvitation_accepted_by_chk`: `CHECK (("accepted_by_id" IS NULL OR "accepted_at" IS NOT NULL))`
- `workspaceinvitation_delivery_status_chk`: `CHECK ("delivery_status" IN ('pending', 'sent', 'failed', 'uncertain'))`
- `workspaceinvitation_revoked_at_chk`: `CHECK ((("revoked_at" IS NULL AND "revoked_reason" = '') OR ("accepted_at" IS NULL AND "revoked_at" IS NOT NULL)))`
- `workspaceinvitation_revoked_reason_chk`: `CHECK ("revoked_reason" IN ('', 'cancelled', 'superseded', 'member_removed', 'administrator_changed'))`
- `workspaceinvitation_role_chk`: `CHECK ("role" IN ('admin', 'viewer'))`
- `workspaceinvitation_tenant_administrator_uniq`: `UNIQUE INDEX ("tenant_id") WHERE ("accepted_at" IS NULL AND "revoked_at" IS NULL AND "role" = 'admin')`
- `workspaceinvitation_tenant_email_uniq`: `UNIQUE INDEX ("tenant_id", "email") WHERE ("accepted_at" IS NULL AND "revoked_at" IS NULL)`

## regions (`pyracast.regions`)

```mermaid
erDiagram
    regions_auditevent {
        bigint id PK
        varchar_40 action
        integer revision
        timestamp_with_time_zone created_at
        integer actor_id FK "references auth_user.id"
        bigint provider_account_id FK "nullable; references connections_provideraccount.id"
        bigint region_id FK "nullable; references regions_region.id"
        jsonb details
    }
    regions_region {
        bigint id PK
        varchar_200 name
        double_precision latitude
        double_precision longitude
        varchar_100 timezone
        integer revision
        varchar_20 status
        timestamp_with_time_zone created_at
        timestamp_with_time_zone deleted_at "nullable"
        bigint approved_binding_id FK "nullable; references connections_sitebinding.id"
        bigint tenant_id FK "references accounts_tenant.id"
        timestamp_with_time_zone purge_started_at "nullable"
        timestamp_with_time_zone purge_warning_sent_at "nullable"
        bigint_array purge_plant_ids "nullable"
        text_array purge_bindings "nullable"
    }
    regions_regionlocationchangerequest {
        bigint id PK
        integer base_revision
        double_precision latitude
        double_precision longitude
        varchar_100 timezone
        varchar_1000 reason
        varchar_20 status
        timestamp_with_time_zone created_at
        timestamp_with_time_zone reviewed_at "nullable"
        bigint region_id FK "references regions_region.id"
        integer requested_by_id FK "references auth_user.id"
        integer reviewed_by_id FK "nullable; references auth_user.id"
        varchar_500 decision_reason
    }
    auth_user ||--o{ regions_auditevent : "actor_id"
    connections_provideraccount |o--o{ regions_auditevent : "provider_account_id"
    regions_region |o--o{ regions_auditevent : "region_id"
    connections_sitebinding |o--o{ regions_region : "approved_binding_id"
    accounts_tenant ||--o{ regions_region : "tenant_id"
    regions_region ||--o{ regions_regionlocationchangerequest : "region_id"
    auth_user ||--o{ regions_regionlocationchangerequest : "requested_by_id"
    auth_user |o--o{ regions_regionlocationchangerequest : "reviewed_by_id"
```

### Constraints and indexes

#### `regions_auditevent`

- `auditevent_action_chk`: `CHECK ("action" IN ('region_created', 'region_renamed', 'location_change_requested', 'location_change_approved', 'location_change_rejected', 'location_change_withdrawn', 'location_change_rebased', 'binding_configured', 'connection_validated', 'connection_validation_failed', 'region_approved', 'region_rejected', 'region_suspended', 'region_resubmitted', 'region_trashed', 'region_restored', 'collection_schedule_updated', 'reminder_schedule_updated', 'historical_reference_imported', 'reference_capacity_normalized', 'provider_account_created', 'provider_secret_rotated', 'provider_cooldown_cleared'))`
- `auditevent_details_chk`: `CHECK (jsonb_typeof("details") = 'object')`
- `auditevent_target_chk`: `CHECK ((("provider_account_id" IS NULL AND "region_id" IS NOT NULL) OR ("provider_account_id" IS NOT NULL AND "region_id" IS NULL)))`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

#### `regions_region`

- `region_approved_binding_fk`: `FOREIGN KEY (approved_binding_id, id) REFERENCES connections_sitebinding (id, region_id) DEFERRABLE INITIALLY DEFERRED`
- `region_latitude_chk`: `CHECK (("latitude" >=  -90.0 AND "latitude" <= 90.0))`
- `region_longitude_chk`: `CHECK (("longitude" >=  -180.0 AND "longitude" <= 180.0))`
- `region_purge_found_chk`: `CHECK ((("purge_bindings" IS NULL AND "purge_plant_ids" IS NULL AND "purge_started_at" IS NULL) OR ("purge_bindings" IS NOT NULL AND "purge_plant_ids" IS NOT NULL AND "purge_started_at" IS NOT NULL)))`
- `region_status_approved_binding_chk`: `CHECK ((NOT ("status" = 'approved') OR "approved_binding_id" IS NOT NULL))`
- `region_status_chk`: `CHECK ("status" IN ('pending', 'approved', 'suspended', 'rejected'))`
- `region_tenant_location_uniq`: `UNIQUE INDEX ("tenant_id", (ROUND(("latitude")::numeric, 6)), (ROUND(("longitude")::numeric, 6))) WHERE "deleted_at" IS NULL`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

#### `regions_regionlocationchangerequest`

- `regionlocationchangerequest_latitude_chk`: `CHECK (("latitude" >=  -90.0 AND "latitude" <= 90.0))`
- `regionlocationchangerequest_longitude_chk`: `CHECK (("longitude" >=  -180.0 AND "longitude" <= 180.0))`
- `regionlocationchangerequest_region_uniq`: `UNIQUE INDEX ("region_id") WHERE "status" = 'pending'`
- `regionlocationchangerequest_review_chk`: `CHECK ((("decision_reason" = '' AND "reviewed_at" IS NULL AND "reviewed_by_id" IS NULL AND "status" = 'pending') OR ("decision_reason" = '' AND "reviewed_at" IS NOT NULL AND "reviewed_by_id" IS NULL AND "status" = 'withdrawn') OR ("reviewed_at" IS NOT NULL AND "reviewed_by_id" IS NOT NULL AND "status" IN ('approved', 'rejected'))))`
- `regionlocationchangerequest_status_chk`: `CHECK ("status" IN ('pending', 'approved', 'rejected', 'withdrawn'))`
- column check (name generated by PostgreSQL): `CHECK ("base_revision" >= 0)`

## connections (`pyracast.connections`)

```mermaid
erDiagram
    connections_provideraccount {
        bigint id PK
        varchar_30 provider
        varchar_200 external_account_id
        varchar_2048 encrypted_api_key
        integer daily_quota
        boolean enabled
        integer credential_revision
        timestamp_with_time_zone request_blocked_until "nullable"
        integer authentication_failed_revision "nullable"
        varchar_500 authorization_reference
        timestamp_with_time_zone authorized_at "nullable"
        integer authorized_by_id FK "nullable; references auth_user.id"
    }
    connections_sitebinding {
        bigint id PK
        integer region_revision
        varchar_200 site_id
        double_precision capacity_kw
        double_precision dc_capacity_kw "nullable"
        varchar_2 power_basis
        boolean capacity_semantics_verified
        double_precision azimuth_degrees
        double_precision losses_percent
        date installation_date "nullable"
        varchar_500 settings_verification_reference
        double_precision latitude
        double_precision longitude
        varchar_100 timezone
        timestamp_with_time_zone validated_at "nullable"
        integer validated_credential_revision "nullable"
        varchar_20 health
        timestamp_with_time_zone created_at
        bigint provider_account_id FK "references connections_provideraccount.id"
        bigint region_id FK "references regions_region.id"
    }
    auth_user |o--o{ connections_provideraccount : "authorized_by_id"
    connections_provideraccount ||--o{ connections_sitebinding : "provider_account_id"
    regions_region ||--o{ connections_sitebinding : "region_id"
```

### Constraints and indexes

#### `connections_provideraccount`

- `provideraccount_authentication_failed_revision_chk`: `CHECK (("authentication_failed_revision" IS NULL OR ("authentication_failed_revision" >= 1 AND "authentication_failed_revision" <= ("credential_revision"))))`
- `provideraccount_credential_revision_chk`: `CHECK ("credential_revision" >= 1)`
- `provideraccount_daily_quota_chk`: `CHECK (("daily_quota" >= 1 AND "daily_quota" <= 10))`
- `provideraccount_encrypted_api_key_chk`: `CHECK ((("encrypted_api_key" = '' AND "provider" = 'fixture') OR "encrypted_api_key"::text LIKE 'encrypted:v1:%'))`
- `provideraccount_provider_chk`: `CHECK ("provider" IN ('fixture', 'solcast'))`
- `provideraccount_provider_external_account_id_uniq`: `UNIQUE INDEX ((CASEFOLD(BTRIM("provider", ' \u0009\u000a\u000d\u000b\u000c\u001c\u001d\u001e\u001f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000') COLLATE "pg_unicode_fast")), (CASEFOLD(BTRIM("external_account_id", ' \u0009\u000a\u000d\u000b\u000c\u001c\u001d\u001e\u001f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000') COLLATE "pg_unicode_fast")))`
- column check (name generated by PostgreSQL): `CHECK ("authentication_failed_revision" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("credential_revision" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("daily_quota" >= 0)`

#### `connections_sitebinding`

- `sitebinding_azimuth_degrees_chk`: `CHECK (("azimuth_degrees" >= 0.0 AND "azimuth_degrees" < 360.0))`
- `sitebinding_capacity_kw_finite_chk`: `CHECK ("capacity_kw" < 'Infinity'::float8)`
- `sitebinding_capacity_kw_positive_chk`: `CHECK ("capacity_kw" > 0.0)`
- `sitebinding_dc_capacity_kw_chk`: `CHECK (("dc_capacity_kw" IS NULL OR ("dc_capacity_kw" > 0.0 AND "dc_capacity_kw" < 'Infinity'::float8)))`
- `sitebinding_health_chk`: `CHECK ("health" IN ('unknown', 'healthy', 'error'))`
- `sitebinding_id_provider_account_uniq`: `UNIQUE ("id", "provider_account_id")`
- `sitebinding_id_region_uniq`: `UNIQUE ("id", "region_id")`
- `sitebinding_latitude_chk`: `CHECK (("latitude" >=  -90.0 AND "latitude" <= 90.0))`
- `sitebinding_longitude_chk`: `CHECK (("longitude" >=  -180.0 AND "longitude" <= 180.0))`
- `sitebinding_losses_percent_chk`: `CHECK (("losses_percent" >= 0.0 AND "losses_percent" < 100.0))`
- `sitebinding_power_basis_chk`: `CHECK ("power_basis" IN ('ac', 'dc'))`
- `sitebinding_region_region_revision_uniq`: `UNIQUE ("region_id", "region_revision")`
- `sitebinding_validated_chk`: `CHECK ((("validated_at" IS NULL AND "validated_credential_revision" IS NULL) OR ("validated_at" IS NOT NULL AND "validated_credential_revision" >= 1 AND "validated_credential_revision" IS NOT NULL)))`
- column check (name generated by PostgreSQL): `CHECK ("region_revision" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("validated_credential_revision" >= 0)`

## forecasts (`pyracast.forecasts`)

```mermaid
erDiagram
    forecasts_collectionjob {
        bigint id PK
        varchar_30 endpoint
        date target_date
        varchar_20 status
        timestamp_with_time_zone available_at
        timestamp_with_time_zone retry_until "nullable"
        uuid lease_token "nullable"
        timestamp_with_time_zone lease_expires_at "nullable"
        integer attempts
        varchar_40 last_error
        timestamp_with_time_zone created_at
        bigint binding_id FK "references connections_sitebinding.id"
        bigint reference_import_id FK "nullable; references forecasts_historicalreferenceimport.id"
        timestamp_with_time_zone scheduled_for
    }
    forecasts_collectionschedule {
        bigint id PK
        boolean enabled
        jsonb times
        timestamp_with_time_zone effective_from
        bigint region_id FK, UK "references regions_region.id"
    }
    forecasts_dispatcherstatus {
        varchar_20 phase PK
        timestamp_with_time_zone attempted_at
        timestamp_with_time_zone succeeded_at "nullable"
        varchar_100 last_error
        integer duration_ms "nullable"
        integer published "nullable"
    }
    forecasts_forecastresult {
        bigint id PK
        jsonb payload
        jsonb daily_payloads
        timestamp_with_time_zone published_at
        bigint snapshot_id FK, UK "references forecasts_forecastsnapshot.id"
    }
    forecasts_forecastsnapshot {
        bigint id PK
        jsonb raw_payload
        jsonb normalized_intervals
        timestamp_with_time_zone collected_at
        timestamp_with_time_zone issued_at "nullable"
        varchar_20 source_kind
        timestamp_with_time_zone coverage_start "nullable"
        timestamp_with_time_zone coverage_end "nullable"
        timestamp_with_time_zone available_at "nullable"
        jsonb weather_payload "nullable"
        varchar_64 weather_error
        bigint binding_id FK "references connections_sitebinding.id"
        bigint job_id FK, UK "references forecasts_collectionjob.id"
    }
    forecasts_historicalreferenceimport {
        bigint id PK
        varchar_255 source_name
        jsonb source_settings
        integer interval_count
        bigint binding_id FK "references connections_sitebinding.id"
        integer imported_by_id FK "references auth_user.id"
        varchar_64 content_sha256
        timestamp_with_time_zone created_at
    }
    forecasts_referencecapacitynormalization {
        bigint id PK
        double_precision source_capacity_kw
        double_precision target_capacity_kw
        double_precision factor
        varchar_1000 authorization_reference
        timestamp_with_time_zone created_at
        integer authorized_by_id FK "references auth_user.id"
        bigint snapshot_id FK, UK "references forecasts_forecastsnapshot.id"
        bigint source_snapshot_id FK "references forecasts_forecastsnapshot.id"
        bigint target_binding_id FK "references connections_sitebinding.id"
    }
    forecasts_requestattempt {
        bigint id PK
        date utc_day
        timestamp_with_time_zone created_at
        varchar_20 purpose
        varchar_20 outcome
        uuid correlation_id UK
        bigint binding_id FK "nullable; references connections_sitebinding.id"
        bigint job_id FK "nullable; references forecasts_collectionjob.id"
        bigint provider_account_id FK "references connections_provideraccount.id"
        smallint http_status "nullable"
    }
    connections_sitebinding ||--o{ forecasts_collectionjob : "binding_id"
    forecasts_historicalreferenceimport |o--o{ forecasts_collectionjob : "reference_import_id"
    regions_region ||--o| forecasts_collectionschedule : "region_id"
    forecasts_forecastsnapshot ||--o| forecasts_forecastresult : "snapshot_id"
    connections_sitebinding ||--o{ forecasts_forecastsnapshot : "binding_id"
    forecasts_collectionjob ||--o| forecasts_forecastsnapshot : "job_id"
    connections_sitebinding ||--o{ forecasts_historicalreferenceimport : "binding_id"
    auth_user ||--o{ forecasts_historicalreferenceimport : "imported_by_id"
    auth_user ||--o{ forecasts_referencecapacitynormalization : "authorized_by_id"
    forecasts_forecastsnapshot ||--o| forecasts_referencecapacitynormalization : "snapshot_id"
    forecasts_forecastsnapshot ||--o{ forecasts_referencecapacitynormalization : "source_snapshot_id"
    connections_sitebinding ||--o{ forecasts_referencecapacitynormalization : "target_binding_id"
    connections_sitebinding |o--o{ forecasts_requestattempt : "binding_id"
    forecasts_collectionjob |o--o{ forecasts_requestattempt : "job_id"
    connections_provideraccount ||--o{ forecasts_requestattempt : "provider_account_id"
```

### Constraints and indexes

#### `forecasts_collectionjob`

- `collectionjob_binding_endpoint_scheduled_for_uniq`: `UNIQUE INDEX ("binding_id", "endpoint", "scheduled_for") WHERE "endpoint" IN ('forecasts', 'estimated_actuals')`
- `collectionjob_due_idx`: `INDEX ("available_at", "id") WHERE "status" IN ('pending', 'retry', 'running')`
- `collectionjob_endpoint_chk`: `CHECK ("endpoint" IN ('forecasts', 'estimated_actuals', 'historical_import', 'scaled_forecasts', 'scaled_estimated_actuals'))`
- `collectionjob_id_binding_uniq`: `UNIQUE ("id", "binding_id")`
- `collectionjob_import_status_chk`: `CHECK (("reference_import_id" IS NULL OR "status" = 'succeeded'))`
- `collectionjob_last_error_chk`: `CHECK ("last_error" IN ('', 'history_window_closed', 'attempts_exhausted', 'forecast_superseded', 'binding_not_current', 'schedule_disabled', 'slot_not_configured', 'schedule_changed', 'history_incomplete', 'history_already_archived', 'provider_authentication_failed', 'provider_allowance_exhausted', 'provider_access_denied', 'provider_site_not_found', 'provider_throttled', 'provider_server_error', 'provider_unreachable', 'provider_invalid_response', 'provider_configuration_invalid', 'account_unavailable', 'history_budget_reserved', 'quota_exhausted', 'provider_failure', 'provider_unexpected_failure', 'invalid_source_kind', 'calculation_failure', 'lock_contention', 'publication_failure'))`
- `collectionjob_lease_chk`: `CHECK ((("lease_expires_at" IS NOT NULL AND "lease_token" IS NOT NULL AND "status" IN ('running')) OR ("lease_expires_at" IS NULL AND "lease_token" IS NULL AND NOT ("status" IN ('running')))))`
- `collectionjob_reference_import_binding_fk`: `FOREIGN KEY (reference_import_id, binding_id) REFERENCES forecasts_historicalreferenceimport (id, binding_id) DEFERRABLE INITIALLY DEFERRED`
- `collectionjob_reference_import_chk`: `CHECK ((("endpoint" = 'historical_import' AND "reference_import_id" IS NOT NULL) OR (NOT ("endpoint" = 'historical_import') AND "reference_import_id" IS NULL)))`
- `collectionjob_reference_import_target_date_uniq`: `UNIQUE ("reference_import_id", "target_date")`
- `collectionjob_status_chk`: `CHECK ("status" IN ('pending', 'running', 'retry', 'succeeded', 'failed', 'stale'))`
- column check (name generated by PostgreSQL): `CHECK ("attempts" >= 0)`

#### `forecasts_collectionschedule`

- `collectionschedule_times_chk`: `CHECK ((jsonb_typeof("times") = 'array' AND NOT (jsonb_path_exists("times", 'strict $[*] ? (@.type() != "string" || !(@ like_regex "^([01][0-9]|2[0-3]):[0-5][0-9]$"))'::jsonpath, '{}', true))))`

#### `forecasts_dispatcherstatus`

- `dispatcherstatus_phase_chk`: `CHECK ("phase" IN ('collection', 'notifications', 'calibration', 'retention', 'metering'))`
- column check (name generated by PostgreSQL): `CHECK ("duration_ms" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("published" >= 0)`

#### `forecasts_forecastsnapshot`

- `forecastsnapshot_coverage_chk`: `CHECK ((("coverage_end" IS NULL AND "coverage_start" IS NULL) OR ("coverage_end" > ("coverage_start") AND "coverage_end" IS NOT NULL AND "coverage_start" IS NOT NULL)))`
- `forecastsnapshot_id_binding_uniq`: `UNIQUE ("id", "binding_id")`
- `forecastsnapshot_job_binding_fk`: `FOREIGN KEY (job_id, binding_id) REFERENCES forecasts_collectionjob (id, binding_id) DEFERRABLE INITIALLY DEFERRED`
- `forecastsnapshot_latest_idx`: `INDEX ("binding_id", "source_kind", "collected_at" DESC, "available_at" DESC, "id" DESC)`
- `forecastsnapshot_publication_guard`: `TRIGGER BEFORE UPDATE FOR EACH ROW EXECUTE FUNCTION guard_forecastsnapshot_publication()`
- `forecastsnapshot_source_kind_chk`: `CHECK ("source_kind" IN ('forecast', 'estimated_actuals'))`
- `forecastsnapshot_weather_error_chk`: `CHECK ("weather_error" IN ('', 'invalid_weather_number', 'invalid_weather_coordinates', 'invalid_weather_schema', 'invalid_weather_timezone', 'invalid_weather_units', 'empty_weather_response', 'invalid_weather_interval', 'invalid_weather_window', 'unsafe_weather_response', 'invalid_weather_collection_time', 'weather_request_failed', 'weather_acquisition_failed', 'weather_acquisition_interrupted'))`

#### `forecasts_historicalreferenceimport`

- `historicalreferenceimport_binding_content_sha256_uniq`: `UNIQUE ("binding_id", "content_sha256")`
- `historicalreferenceimport_id_binding_uniq`: `UNIQUE ("id", "binding_id")`
- `historicalreferenceimport_immutable`: `TRIGGER BEFORE UPDATE FOR EACH ROW EXECUTE FUNCTION refuse_historicalreferenceimport_update()`
- column check (name generated by PostgreSQL): `CHECK ("interval_count" >= 0)`

#### `forecasts_referencecapacitynormalization`

- `referencecapacitynormalization_factor_finite_chk`: `CHECK (("factor" > 0.0 AND "factor" < 'Infinity'::float8))`
- `referencecapacitynormalization_snapshot_target_binding_fk`: `FOREIGN KEY (snapshot_id, target_binding_id) REFERENCES forecasts_forecastsnapshot (id, binding_id) DEFERRABLE INITIALLY DEFERRED`
- `referencecapacitynormalization_source_capacity_kw_chk`: `CHECK (("source_capacity_kw" > 0.0 AND "source_capacity_kw" < 'Infinity'::float8))`
- `referencecapacitynormalization_source_snapshot_chk`: `CHECK (NOT ("snapshot_id" = ("source_snapshot_id")))`
- `referencecapacitynormalization_source_target_uniq`: `UNIQUE ("source_snapshot_id", "target_binding_id")`
- `referencecapacitynormalization_target_capacity_kw_chk`: `CHECK (("target_capacity_kw" > 0.0 AND "target_capacity_kw" < 'Infinity'::float8))`
- `referencecapacitynormalization_target_over_source_chk`: `CHECK ("factor" = ("target_capacity_kw" / "source_capacity_kw"))`

#### `forecasts_requestattempt`

- `requestattempt_account_day_idx`: `INDEX ("provider_account_id", "utc_day")`
- `requestattempt_binding_provider_account_fk`: `FOREIGN KEY (binding_id, provider_account_id) REFERENCES connections_sitebinding (id, provider_account_id) DEFERRABLE INITIALLY DEFERRED`
- `requestattempt_http_status_chk`: `CHECK (("http_status" IS NULL OR ("http_status" >= 100 AND "http_status" <= 599)))`
- `requestattempt_job_binding_chk`: `CHECK (("job_id" IS NULL OR "binding_id" IS NOT NULL))`
- `requestattempt_job_binding_fk`: `FOREIGN KEY (job_id, binding_id) REFERENCES forecasts_collectionjob (id, binding_id) DEFERRABLE INITIALLY DEFERRED`
- `requestattempt_outcome_chk`: `CHECK ("outcome" IN ('uncertain', 'succeeded', 'failed'))`
- `requestattempt_purpose_chk`: `CHECK ("purpose" IN ('forecast', 'estimated_actuals', 'validation', 'inspect_settings', 'inspect_forecast'))`
- column check (name generated by PostgreSQL): `CHECK ("http_status" >= 0)`

## plants (`pyracast.plants`)

```mermaid
erDiagram
    plants_calibrationactivation {
        bigint id PK
        timestamp_with_time_zone created_at
        integer actor_id FK "nullable; references auth_user.id"
        bigint calibration_id FK "nullable; references plants_calibrationrun.id"
        bigint plant_id FK "references plants_plant.id"
        bigint configuration_id FK "references plants_plantconfiguration.id"
    }
    plants_calibrationrun {
        bigint id PK
        date window_start
        date window_end
        timestamp_with_time_zone cutoff_at
        bigint observation_id_cutoff "nullable"
        jsonb production_snapshot_ids
        varchar_80 measurement_selection_policy
        jsonb reference_snapshot_ids "nullable"
        jsonb incident_revision_ids
        timestamp_with_time_zone created_at
        timestamp_with_time_zone completed_at "nullable"
        varchar_20 status
        varchar_200 assessment
        double_precision factor "nullable"
        jsonb result
        jsonb dataset_manifest
        varchar_64 dataset_sha256
        uuid lease_token "nullable"
        timestamp_with_time_zone lease_expires_at "nullable"
        integer attempts
        varchar_200 last_error
        bigint binding_id FK "references connections_sitebinding.id"
        bigint comparison_activation_id FK "nullable; references plants_calibrationactivation.id"
        integer requested_by_id FK "references auth_user.id"
        bigint plant_id FK "references plants_plant.id"
        bigint configuration_id FK "references plants_plantconfiguration.id"
        varchar_64 input_sha256
    }
    plants_forecastissuance {
        bigint id PK
        date target_date
        timestamp_with_time_zone published_at
        timestamp_with_time_zone cutoff_at
        varchar_100 model_version
        double_precision factor
        jsonb payloads
        jsonb outage_scenarios
        jsonb native_intervals
        bigint calibration_id FK "nullable; references plants_calibrationrun.id"
        bigint snapshot_id FK "references forecasts_forecastsnapshot.id"
        bigint plant_id FK "references plants_plant.id"
        bigint configuration_id FK "references plants_plantconfiguration.id"
    }
    plants_plant {
        bigint id PK
        varchar_200 name
        timestamp_with_time_zone created_at
        boolean automatic_calibration_enabled
        timestamp_with_time_zone next_calibration_check_at "nullable"
        integer automatic_calibration_enabled_by_id FK "nullable; references auth_user.id"
        bigint region_id FK "references regions_region.id"
        bigint current_configuration_id FK "nullable; references plants_plantconfiguration.id"
        timestamp_with_time_zone calibration_preparation_due_at "nullable"
        integer parameters_revision
    }
    plants_plantconfiguration {
        bigint id PK
        integer revision
        double_precision dc_capacity_mw
        double_precision inverter_capacity_mw "nullable"
        double_precision export_limit_mw "nullable"
        varchar_10 mounting_type
        varchar_20 measurement_basis
        timestamp_with_time_zone created_at
        bigint binding_id FK "references connections_sitebinding.id"
        integer created_by_id FK "references auth_user.id"
        bigint plant_id FK "references plants_plant.id"
        bigint region_id FK "references regions_region.id"
        double_precision surface_tilt_degrees "nullable"
        double_precision surface_azimuth_degrees "nullable"
    }
    plants_plantevent {
        bigint id PK
        varchar_50 action
        jsonb details
        timestamp_with_time_zone created_at
        integer actor_id FK "nullable; references auth_user.id"
        bigint plant_id FK "references plants_plant.id"
    }
    plants_plantsubmission {
        bigint id PK
        double_precision dc_capacity_mw
        double_precision inverter_capacity_mw "nullable"
        double_precision export_limit_mw "nullable"
        varchar_20 measurement_basis
        varchar_10 mounting_type
        timestamp_with_time_zone created_at
        bigint plant_id FK, UK "references plants_plant.id"
        integer submitted_by_id FK "references auth_user.id"
        double_precision surface_tilt_degrees "nullable"
        double_precision surface_azimuth_degrees "nullable"
        timestamp_with_time_zone updated_at
        bigint base_configuration_id FK "nullable; references plants_plantconfiguration.id"
    }
    plants_productionobservation {
        bigint id PK
        timestamp_with_time_zone starts_at
        timestamp_with_time_zone ends_at
        double_precision energy_mwh
        varchar_20 quality
        varchar_200 source
        boolean is_current
        boolean is_reverted
        bigint configuration_id FK "references plants_plantconfiguration.id"
        bigint plant_id FK "references plants_plant.id"
        integer recorded_by_id FK "references auth_user.id"
        bigint supersedes_id FK, UK "nullable; references plants_productionobservation.id"
        timestamp_with_time_zone created_at
    }
    auth_user |o--o{ plants_calibrationactivation : "actor_id"
    plants_calibrationrun |o--o{ plants_calibrationactivation : "calibration_id"
    plants_plant ||--o{ plants_calibrationactivation : "plant_id"
    plants_plantconfiguration ||--o{ plants_calibrationactivation : "configuration_id"
    connections_sitebinding ||--o{ plants_calibrationrun : "binding_id"
    plants_calibrationactivation |o--o{ plants_calibrationrun : "comparison_activation_id"
    auth_user ||--o{ plants_calibrationrun : "requested_by_id"
    plants_plant ||--o{ plants_calibrationrun : "plant_id"
    plants_plantconfiguration ||--o{ plants_calibrationrun : "configuration_id"
    plants_calibrationrun |o--o{ plants_forecastissuance : "calibration_id"
    forecasts_forecastsnapshot ||--o{ plants_forecastissuance : "snapshot_id"
    plants_plant ||--o{ plants_forecastissuance : "plant_id"
    plants_plantconfiguration ||--o{ plants_forecastissuance : "configuration_id"
    auth_user |o--o{ plants_plant : "automatic_calibration_enabled_by_id"
    regions_region ||--o{ plants_plant : "region_id"
    plants_plantconfiguration |o--o{ plants_plant : "current_configuration_id"
    connections_sitebinding ||--o{ plants_plantconfiguration : "binding_id"
    auth_user ||--o{ plants_plantconfiguration : "created_by_id"
    plants_plant ||--o{ plants_plantconfiguration : "plant_id"
    regions_region ||--o{ plants_plantconfiguration : "region_id"
    auth_user |o--o{ plants_plantevent : "actor_id"
    plants_plant ||--o{ plants_plantevent : "plant_id"
    plants_plant ||--o| plants_plantsubmission : "plant_id"
    auth_user ||--o{ plants_plantsubmission : "submitted_by_id"
    plants_plantconfiguration |o--o{ plants_plantsubmission : "base_configuration_id"
    plants_plantconfiguration ||--o{ plants_productionobservation : "configuration_id"
    plants_plant ||--o{ plants_productionobservation : "plant_id"
    auth_user ||--o{ plants_productionobservation : "recorded_by_id"
    plants_productionobservation |o--o| plants_productionobservation : "supersedes_id"
```

### Constraints and indexes

#### `plants_calibrationactivation`

- `calibrationactivation_calibration_configuration_fk`: `FOREIGN KEY (calibration_id, configuration_id) REFERENCES plants_calibrationrun (id, configuration_id) DEFERRABLE INITIALLY DEFERRED`
- `calibrationactivation_configuration_plant_fk`: `FOREIGN KEY (configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `calibrationactivation_id_configuration_uniq`: `UNIQUE ("id", "configuration_id")`

#### `plants_calibrationrun`

- `calibrationrun_assessment_chk`: `CHECK ("assessment" IN ('', 'insufficient', 'provisional', 'evaluated'))`
- `calibrationrun_comparison_activation_configuration_fk`: `FOREIGN KEY (comparison_activation_id, configuration_id) REFERENCES plants_calibrationactivation (id, configuration_id) DEFERRABLE INITIALLY DEFERRED`
- `calibrationrun_completed_at_chk`: `CHECK (("status" IN ('pending', 'running') OR "completed_at" IS NOT NULL))`
- `calibrationrun_configuration_binding_fk`: `FOREIGN KEY (configuration_id, binding_id) REFERENCES plants_plantconfiguration (id, binding_id) DEFERRABLE INITIALLY DEFERRED`
- `calibrationrun_configuration_plant_fk`: `FOREIGN KEY (configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `calibrationrun_factor_chk`: `CHECK (("factor" IS NULL OR ("factor" > 0.0 AND "factor" < 'Infinity'::float8)))`
- `calibrationrun_id_configuration_uniq`: `UNIQUE ("id", "configuration_id")`
- `calibrationrun_last_error_chk`: `CHECK ("last_error" IN ('', 'attempt_limit', 'invalid_cutoff', 'invalid_calibration_data'))`
- `calibrationrun_lease_chk`: `CHECK ((("lease_expires_at" IS NOT NULL AND "lease_token" IS NOT NULL AND "status" IN ('running')) OR ("lease_expires_at" IS NULL AND "lease_token" IS NULL AND NOT ("status" IN ('running')))))`
- `calibrationrun_plant_uniq`: `UNIQUE INDEX ("plant_id") WHERE "status" IN ('pending', 'running')`
- `calibrationrun_status_chk`: `CHECK ("status" IN ('pending', 'running', 'ready', 'insufficient', 'rejected', 'failed', 'superseded'))`
- `calibrationrun_window_chk`: `CHECK ("window_end" >= ("window_start"))`
- column check (name generated by PostgreSQL): `CHECK ("attempts" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("observation_id_cutoff" >= 0)`

#### `plants_forecastissuance`

- `forecastissuance_calibration_configuration_fk`: `FOREIGN KEY (calibration_id, configuration_id) REFERENCES plants_calibrationrun (id, configuration_id) DEFERRABLE INITIALLY DEFERRED`
- `forecastissuance_calibration_factor_chk`: `CHECK (("calibration_id" IS NOT NULL OR "factor" = 1.0))`
- `forecastissuance_configuration_plant_fk`: `FOREIGN KEY (configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `forecastissuance_factor_chk`: `CHECK (("factor" > 0.0 AND "factor" < 'Infinity'::float8))`
- `forecastissuance_plant_day_idx`: `INDEX ("plant_id", "target_date")`
- `forecastissuance_plant_snapshot_target_date_uniq`: `UNIQUE ("plant_id", "snapshot_id", "target_date")`
- `forecastissuance_published_at_chk`: `CHECK ("published_at" < ("cutoff_at"))`
- `immutable_forecast_issuance`: `TRIGGER BEFORE INSERT OR UPDATE FOR EACH ROW EXECUTE FUNCTION guard_forecast_issuance()`

#### `plants_plant`

- `plant_automation_chk`: `CHECK ((("automatic_calibration_enabled" AND "automatic_calibration_enabled_by_id" IS NOT NULL AND "next_calibration_check_at" IS NOT NULL) OR (NOT "automatic_calibration_enabled" AND "automatic_calibration_enabled_by_id" IS NULL AND "next_calibration_check_at" IS NULL)))`
- `plant_current_configuration_fk`: `FOREIGN KEY (current_configuration_id, id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `plant_id_region_uniq`: `UNIQUE ("id", "region_id")`
- `plant_region_uniq`: `UNIQUE ("region_id")`
- column check (name generated by PostgreSQL): `CHECK ("parameters_revision" >= 0)`

#### `plants_plantconfiguration`

- `plantconfiguration_angles_chk`: `CHECK (("mounting_type" = 'fixed' OR ("surface_azimuth_degrees" IS NULL AND "surface_tilt_degrees" IS NULL)))`
- `plantconfiguration_binding_region_fk`: `FOREIGN KEY (binding_id, region_id) REFERENCES connections_sitebinding (id, region_id) DEFERRABLE INITIALLY DEFERRED`
- `plantconfiguration_dc_capacity_mw_chk`: `CHECK (("dc_capacity_mw" > 0.0 AND "dc_capacity_mw" < 'Infinity'::float8))`
- `plantconfiguration_export_limit_mw_chk`: `CHECK (("export_limit_mw" IS NULL OR ("export_limit_mw" > 0.0 AND "export_limit_mw" < 'Infinity'::float8)))`
- `plantconfiguration_id_binding_uniq`: `UNIQUE ("id", "binding_id")`
- `plantconfiguration_id_plant_uniq`: `UNIQUE ("id", "plant_id")`
- `plantconfiguration_inverter_capacity_mw_chk`: `CHECK (("inverter_capacity_mw" IS NULL OR ("inverter_capacity_mw" > 0.0 AND "inverter_capacity_mw" < 'Infinity'::float8)))`
- `plantconfiguration_measurement_basis_chk`: `CHECK ("measurement_basis" IN ('net_export', 'ac_generation'))`
- `plantconfiguration_mounting_type_chk`: `CHECK ("mounting_type" IN ('unknown', 'fixed', 'tracking'))`
- `plantconfiguration_plant_region_fk`: `FOREIGN KEY (plant_id, region_id) REFERENCES plants_plant (id, region_id) DEFERRABLE INITIALLY DEFERRED`
- `plantconfiguration_plant_revision_uniq`: `UNIQUE ("plant_id", "revision")`
- `plantconfiguration_revision_chk`: `CHECK ("revision" >= 1)`
- `plantconfiguration_surface_azimuth_degrees_chk`: `CHECK (("surface_azimuth_degrees" IS NULL OR ("surface_azimuth_degrees" >= 0.0 AND "surface_azimuth_degrees" < 360.0)))`
- `plantconfiguration_surface_tilt_degrees_chk`: `CHECK (("surface_tilt_degrees" IS NULL OR ("surface_tilt_degrees" >= 0.0 AND "surface_tilt_degrees" <= 90.0)))`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

#### `plants_plantevent`

- `plantevent_action_chk`: `CHECK ("action" IN ('submission_saved', 'configuration_created', 'plant_renamed', 'observations_imported', 'production_changed', 'epias_connected', 'epias_disconnected', 'calibration_requested', 'calibration_scheduled', 'calibration_completed', 'calibration_activated', 'calibration_automatically_activated', 'calibration_reset', 'calibration_automation_enabled', 'calibration_automation_disabled', 'calibration_automatic_skipped'))`

#### `plants_plantsubmission`

- `plantsubmission_angles_chk`: `CHECK (("mounting_type" = 'fixed' OR ("surface_azimuth_degrees" IS NULL AND "surface_tilt_degrees" IS NULL)))`
- `plantsubmission_base_configuration_plant_fk`: `FOREIGN KEY (base_configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `plantsubmission_dc_capacity_mw_chk`: `CHECK (("dc_capacity_mw" > 0.0 AND "dc_capacity_mw" < 'Infinity'::float8))`
- `plantsubmission_export_limit_mw_chk`: `CHECK (("export_limit_mw" IS NULL OR ("export_limit_mw" > 0.0 AND "export_limit_mw" < 'Infinity'::float8)))`
- `plantsubmission_inverter_capacity_mw_chk`: `CHECK (("inverter_capacity_mw" IS NULL OR ("inverter_capacity_mw" > 0.0 AND "inverter_capacity_mw" < 'Infinity'::float8)))`
- `plantsubmission_measurement_basis_chk`: `CHECK ("measurement_basis" IN ('net_export', 'ac_generation'))`
- `plantsubmission_mounting_type_chk`: `CHECK ("mounting_type" IN ('unknown', 'fixed', 'tracking'))`
- `plantsubmission_surface_azimuth_degrees_chk`: `CHECK (("surface_azimuth_degrees" IS NULL OR ("surface_azimuth_degrees" >= 0.0 AND "surface_azimuth_degrees" < 360.0)))`
- `plantsubmission_surface_tilt_degrees_chk`: `CHECK (("surface_tilt_degrees" IS NULL OR ("surface_tilt_degrees" >= 0.0 AND "surface_tilt_degrees" <= 90.0)))`

#### `plants_productionobservation`

- `productionobservation_configuration_plant_fk`: `FOREIGN KEY (configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `productionobservation_ends_at_chk`: `CHECK (("ends_at" = ("starts_at" + '0:30:00'::interval) OR "ends_at" = ("starts_at" + '1:00:00'::interval)))`
- `productionobservation_energy_mwh_chk`: `CHECK (("energy_mwh" >= 0.0 AND "energy_mwh" < 'Infinity'::float8))`
- `productionobservation_is_reverted_chk`: `CHECK ((NOT "is_reverted" OR "supersedes_id" IS NOT NULL))`
- `productionobservation_plant_interval_excl`: `EXCLUDE USING GIST ("plant_id" WITH =, (TSTZRANGE("starts_at", "ends_at", '[)')) WITH &&) WHERE (("is_current" AND NOT "is_reverted"))`
- `productionobservation_plant_starts_at_ends_at_uniq`: `UNIQUE INDEX ("plant_id", "starts_at", "ends_at") WHERE "is_current"`
- `productionobservation_quality_chk`: `CHECK ("quality" IN ('valid', 'outage', 'curtailed', 'invalid'))`
- `productionobservation_revision_guard`: `TRIGGER BEFORE INSERT OR UPDATE FOR EACH ROW EXECUTE FUNCTION guard_productionobservation_revision()`

## incidents (`pyracast.incidents`)

```mermaid
erDiagram
    incidents_incidentrevision {
        bigint id PK
        integer revision
        jsonb snapshot
        integer actor_id FK "references auth_user.id"
        bigint incident_id FK "references incidents_plantincident.id"
        timestamp_with_time_zone created_at
    }
    incidents_plantincident {
        bigint id PK
        varchar_20 kind
        varchar_200 title
        varchar_2000 notes
        timestamp_with_time_zone starts_at
        timestamp_with_time_zone ends_at "nullable"
        double_precision available_dc_mw "nullable"
        double_precision available_inverter_mw "nullable"
        double_precision available_export_mw "nullable"
        timestamp_with_time_zone cancelled_at "nullable"
        integer revision
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
        integer created_by_id FK "references auth_user.id"
        bigint declared_configuration_id FK "references plants_plantconfiguration.id"
        bigint plant_id FK "references plants_plant.id"
        integer updated_by_id FK "references auth_user.id"
    }
    auth_user ||--o{ incidents_incidentrevision : "actor_id"
    incidents_plantincident ||--o{ incidents_incidentrevision : "incident_id"
    auth_user ||--o{ incidents_plantincident : "created_by_id"
    plants_plantconfiguration ||--o{ incidents_plantincident : "declared_configuration_id"
    plants_plant ||--o{ incidents_plantincident : "plant_id"
    auth_user ||--o{ incidents_plantincident : "updated_by_id"
```

### Constraints and indexes

#### `incidents_incidentrevision`

- `incidentrevision_incident_revision_uniq`: `UNIQUE ("incident_id", "revision")`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

#### `incidents_plantincident`

- `plantincident_available_dc_mw_chk`: `CHECK (("available_dc_mw" IS NULL OR ("available_dc_mw" >= 0.0 AND "available_dc_mw" < 'Infinity'::float8)))`
- `plantincident_available_export_mw_chk`: `CHECK (("available_export_mw" IS NULL OR ("available_export_mw" >= 0.0 AND "available_export_mw" < 'Infinity'::float8)))`
- `plantincident_available_inverter_mw_chk`: `CHECK (("available_inverter_mw" IS NULL OR ("available_inverter_mw" >= 0.0 AND "available_inverter_mw" < 'Infinity'::float8)))`
- `plantincident_declared_configuration_plant_fk`: `FOREIGN KEY (declared_configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `plantincident_ends_at_chk`: `CHECK (("ends_at" IS NULL OR "ends_at" > ("starts_at")))`
- `plantincident_kind_chk`: `CHECK ("kind" IN ('inverter', 'panels', 'grid', 'other', 'multiple'))`
- `plantincident_plant_interval_excl`: `EXCLUDE USING GIST ("plant_id" WITH =, (TSTZRANGE("starts_at", "ends_at", '[)')) WITH &&) WHERE ("cancelled_at" IS NULL)`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

## notifications (`pyracast.notifications`)

```mermaid
erDiagram
    notifications_deliveryrecipient {
        bigint id PK
        text address
        text key
        boolean revoked
        bigint delivery_id FK "references notifications_notificationdelivery.id"
    }
    notifications_notificationdelivery {
        bigint id PK
        timestamp_with_time_zone scheduled_for
        varchar_20 status
        timestamp_with_time_zone available_at
        integer attempts
        uuid lease_token "nullable"
        timestamp_with_time_zone lease_expires_at "nullable"
        varchar_2 language
        timestamp_with_time_zone preparation_deadline
        jsonb prepared_message "nullable"
        timestamp_with_time_zone transmit_started_at "nullable"
        timestamp_with_time_zone sent_at "nullable"
        varchar_40 last_error
        uuid message_token UK
        bigint binding_id FK "references connections_sitebinding.id"
        bigint snapshot_id FK "nullable; references forecasts_forecastsnapshot.id"
        bigint schedule_id FK "references notifications_reminderschedule.id"
        boolean forecast_complete "nullable"
        varchar_14 forecast_kind
        timestamp_with_time_zone prepared_at "nullable"
    }
    notifications_notificationsettings {
        smallint id PK
        smallint max_recipients
    }
    notifications_outboundmail {
        bigint id PK
        varchar_48 kind
        varchar_320 recipient
        varchar_2 language
        boolean owner_language
        jsonb context
        text sealed_code
        varchar_9 status
        timestamp_with_time_zone created_at
        timestamp_with_time_zone available_at
        timestamp_with_time_zone expires_at
        timestamp_with_time_zone published_at "nullable"
        smallint attempts
        uuid lease_token "nullable"
        timestamp_with_time_zone lease_expires_at "nullable"
        timestamp_with_time_zone transmit_started_at "nullable"
        timestamp_with_time_zone finished_at "nullable"
        varchar_30 last_error
        bigint invitation_id FK "nullable; references accounts_workspaceinvitation.id"
    }
    notifications_reminderrecipient {
        bigint id PK
        text address
        text key
        bigint schedule_id FK "references notifications_reminderschedule.id"
    }
    notifications_reminderrecipienttie {
        bigint id PK
        bigint membership_id
        bigint recipient_id FK "references notifications_reminderrecipient.id"
    }
    notifications_reminderschedule {
        bigint id PK
        boolean enabled
        boolean include_workspace_members
        jsonb times
        timestamp_with_time_zone effective_from
        varchar_2 language
        integer configured_by_id FK "nullable; references auth_user.id"
        bigint region_id FK "references regions_region.id"
        integer revision
    }
    notifications_notificationdelivery ||--o{ notifications_deliveryrecipient : "delivery_id"
    connections_sitebinding ||--o{ notifications_notificationdelivery : "binding_id"
    forecasts_forecastsnapshot |o--o{ notifications_notificationdelivery : "snapshot_id"
    notifications_reminderschedule ||--o{ notifications_notificationdelivery : "schedule_id"
    accounts_workspaceinvitation |o--o{ notifications_outboundmail : "invitation_id"
    notifications_reminderschedule ||--o{ notifications_reminderrecipient : "schedule_id"
    notifications_reminderrecipient ||--o{ notifications_reminderrecipienttie : "recipient_id"
    auth_user |o--o{ notifications_reminderschedule : "configured_by_id"
    regions_region ||--o{ notifications_reminderschedule : "region_id"
```

### Constraints and indexes

#### `notifications_deliveryrecipient`

- `deliveryrecipient_delivery_key_uniq`: `UNIQUE ("delivery_id", "key")`

#### `notifications_notificationdelivery`

- `notificationdelivery_due_idx`: `INDEX ("available_at", "id") WHERE "status" IN ('pending', 'retry', 'sending')`
- `notificationdelivery_forecast_kind_chk`: `CHECK ("forecast_kind" IN ('', 'current', 'saved_complete', 'saved_partial', 'unavailable'))`
- `notificationdelivery_language_chk`: `CHECK ("language" IN ('en', 'tr'))`
- `notificationdelivery_last_error_chk`: `CHECK ("last_error" IN ('', 'missed_schedule', 'delivery_deadline_exceeded', 'cancelled_by_settings_change', 'schedule_or_authority_changed', 'no_recipients', 'invalid_recipient', 'recipient_limit_exceeded', 'definition_trashed', 'interrupted_send', 'interrupted_connection', 'mail_not_sent', 'mail_configuration_invalid', 'mail_credentials_rejected', 'mail_authorization_revoked', 'mail_rate_limited', 'mail_rejected', 'mail_sender_forbidden', 'mail_preparation_failed', 'mail_outcome_uncertain'))`
- `notificationdelivery_lease_chk`: `CHECK ((("lease_expires_at" IS NOT NULL AND "lease_token" IS NOT NULL AND "status" IN ('sending', 'uncertain')) OR ("lease_expires_at" IS NULL AND "lease_token" IS NULL AND NOT ("status" IN ('sending')))))`
- `notificationdelivery_prepared_at_chk`: `CHECK ((("forecast_complete" IS NULL AND "forecast_kind" = '' AND "prepared_at" IS NULL) OR (NOT ("forecast_kind" = '') AND "prepared_at" IS NOT NULL)))`
- `notificationdelivery_prepared_format_chk`: `CHECK (("prepared_message" -> 'format') IN ('"xlsx"'::jsonb, '"plain"'::jsonb))`
- `notificationdelivery_prepared_message_chk`: `CHECK ((NOT ("status" = 'sending') OR "prepared_message" IS NOT NULL))`
- `notificationdelivery_schedule_scheduled_for_uniq`: `UNIQUE ("schedule_id", "scheduled_for")`
- `notificationdelivery_status_chk`: `CHECK ("status" IN ('pending', 'retry', 'sending', 'sent', 'failed', 'uncertain', 'skipped', 'cancelled'))`
- column check (name generated by PostgreSQL): `CHECK ("attempts" >= 0)`

#### `notifications_notificationsettings`

- `notificationsettings_id_chk`: `CHECK ("id" = 1)`
- `notificationsettings_max_recipients_chk`: `CHECK ("max_recipients" >= 1)`
- column check (name generated by PostgreSQL): `CHECK ("id" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("max_recipients" >= 0)`

#### `notifications_outboundmail`

- `outboundmail_context_chk`: `CHECK ((jsonb_typeof("context") = 'object' AND NOT (jsonb_path_exists("context", 'strict $.keyvalue() ? (@.value.type() != "string" || !(@.key like_regex "^(from_email|to_email|deleted_email|provider|timestamp|ip|user_agent|subject_id)$"))'::jsonpath, '{}', true))))`
- `outboundmail_due_idx`: `INDEX ("available_at", "id") WHERE "status" IN ('pending', 'retry', 'sending')`
- `outboundmail_invitation_chk`: `CHECK ((("invitation_id" IS NOT NULL AND "kind" = 'workspace_invitation') OR (NOT ("kind" = 'workspace_invitation') AND "invitation_id" IS NULL)))`
- `outboundmail_kind_chk`: `CHECK ("kind" IN ('workspace_invitation', 'account/email/email_confirmation_signup', 'account/email/email_confirmation', 'account/email/password_reset_code', 'account/email/login_code', 'account/email/password_changed', 'account/email/password_set', 'account/email/password_reset', 'account/email/email_changed', 'account/email/email_confirm', 'account/email/email_deleted', 'mfa/email/totp_activated', 'mfa/email/totp_deactivated', 'account/email/second_factor_reset', 'mfa/email/recovery_codes_generated', 'socialaccount/email/account_connected', 'socialaccount/email/account_disconnected', 'account/email/unknown_account', 'account/email/account_already_exists', 'account/email/login_locked', 'plant_approved', 'plant_rejected', 'plant_suspended', 'workspace_admin_assigned', 'workspace_admin_released', 'plant_purge_warning'))`
- `outboundmail_language_chk`: `CHECK ("language" IN ('en', 'tr'))`
- `outboundmail_last_error_chk`: `CHECK ("last_error" IN ('', 'expired', 'budget_exhausted', 'invitation_unavailable', 'no_account', 'subject_unavailable', 'recipient_unavailable', 'interrupted_send', 'interrupted_connection', 'mail_not_sent', 'mail_configuration_invalid', 'mail_credentials_rejected', 'mail_authorization_revoked', 'mail_rate_limited', 'mail_rejected', 'mail_sender_forbidden', 'mail_preparation_failed', 'mail_outcome_uncertain'))`
- `outboundmail_lease_chk`: `CHECK ((("lease_expires_at" IS NOT NULL AND "lease_token" IS NOT NULL AND "status" IN ('sending', 'uncertain')) OR ("lease_expires_at" IS NULL AND "lease_token" IS NULL AND NOT ("status" IN ('sending')))))`
- `outboundmail_sealed_code_chk`: `CHECK (("sealed_code" = '' OR ("kind" IN ('account/email/email_confirmation_signup', 'account/email/email_confirmation', 'account/email/password_reset_code', 'account/email/login_code') AND "status" IN ('pending', 'retry', 'sending'))))`
- `outboundmail_status_chk`: `CHECK ("status" IN ('pending', 'retry', 'sending', 'sent', 'failed', 'uncertain', 'skipped'))`
- column check (name generated by PostgreSQL): `CHECK ("attempts" >= 0)`

#### `notifications_reminderrecipient`

- `reminderrecipient_schedule_key_uniq`: `UNIQUE ("schedule_id", "key")`

#### `notifications_reminderrecipienttie`

- `reminderrecipienttie_recipient_membership_uniq`: `UNIQUE ("recipient_id", "membership_id")`
- column check (name generated by PostgreSQL): `CHECK ("membership_id" >= 0)`

#### `notifications_reminderschedule`

- `reminderschedule_language_chk`: `CHECK ("language" IN ('en', 'tr'))`
- `reminderschedule_region_uniq`: `UNIQUE ("region_id")`
- `reminderschedule_times_chk`: `CHECK ((jsonb_typeof("times") = 'array' AND NOT (jsonb_path_exists("times", 'strict $[*] ? (@.type() != "string" || !(@ like_regex "^([01][0-9]|2[0-3]):[0-5][0-9]$"))'::jsonpath, '{}', true))))`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

## metering (`pyracast.metering`)

```mermaid
erDiagram
    metering_epiascollectionjob {
        bigint id PK
        integer connection_revision
        jsonb provider_plant
        date period_end_day "nullable"
        varchar_30 source_kind
        timestamp_with_time_zone scheduled_for
        varchar_20 status
        uuid lease_token "nullable"
        timestamp_with_time_zone lease_expires_at "nullable"
        smallint attempts
        timestamp_with_time_zone retry_until "nullable"
        varchar_40 last_error
        timestamp_with_time_zone created_at
        bigint configuration_id FK "references plants_plantconfiguration.id"
        bigint plant_id FK "references plants_plant.id"
        bigint connection_id FK "references metering_epiasconnection.id"
        date target_date
        timestamp_with_time_zone available_at
    }
    metering_epiasconnection {
        bigint id PK
        bigint provider_plant_id
        bigint settlement_plant_id "nullable"
        timestamp_with_time_zone settlement_checked_at "nullable"
        varchar_100 eic
        varchar_300 name
        boolean enabled
        integer revision
        timestamp_with_time_zone created_at
        timestamp_with_time_zone updated_at
        integer connected_by_id FK "references auth_user.id"
        bigint plant_id FK, UK "references plants_plant.id"
    }
    metering_epiasconnectionrevision {
        bigint id PK
        integer revision
        bigint provider_plant_id
        varchar_100 eic
        timestamp_with_time_zone connected_at
        bigint connection_id FK "references metering_epiasconnection.id"
    }
    metering_epiassnapshot {
        bigint id PK
        smallint attempt
        varchar_30 source_kind
        jsonb provider_plant
        varchar_200 endpoint
        varchar_40 normalization_policy
        timestamp_with_time_zone collected_at
        timestamp_with_time_zone available_at
        jsonb raw_payload
        jsonb readings
        varchar_64 content_sha256
        boolean complete
        bigint configuration_id FK "references plants_plantconfiguration.id"
        bigint connection_id FK "references metering_epiasconnection.id"
        bigint job_id FK "references metering_epiascollectionjob.id"
        date target_date
    }
    plants_plantconfiguration ||--o{ metering_epiascollectionjob : "configuration_id"
    plants_plant ||--o{ metering_epiascollectionjob : "plant_id"
    metering_epiasconnection ||--o{ metering_epiascollectionjob : "connection_id"
    auth_user ||--o{ metering_epiasconnection : "connected_by_id"
    plants_plant ||--o| metering_epiasconnection : "plant_id"
    metering_epiasconnection ||--o{ metering_epiasconnectionrevision : "connection_id"
    plants_plantconfiguration ||--o{ metering_epiassnapshot : "configuration_id"
    metering_epiasconnection ||--o{ metering_epiassnapshot : "connection_id"
    metering_epiascollectionjob ||--o{ metering_epiassnapshot : "job_id"
```

### Constraints and indexes

#### `metering_epiascollectionjob`

- `epiascollectionjob_configuration_plant_fk`: `FOREIGN KEY (configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `epiascollectionjob_connection_plant_fk`: `FOREIGN KEY (connection_id, plant_id) REFERENCES metering_epiasconnection (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `epiascollectionjob_connection_revision_chk`: `CHECK ("connection_revision" >= 1)`
- `epiascollectionjob_due_idx`: `INDEX ("status", "available_at")`
- `epiascollectionjob_id_connection_configuration_uniq`: `UNIQUE ("id", "connection_id", "configuration_id")`
- `epiascollectionjob_last_error_chk`: `CHECK ("last_error" IN ('', 'retry_window_expired', 'attempts_exhausted', 'epias_provider_failure', 'epias_credentials_missing', 'epias_credentials_rejected', 'unpublished_data', 'partial_data'))`
- `epiascollectionjob_lease_chk`: `CHECK ((("lease_expires_at" IS NOT NULL AND "lease_token" IS NOT NULL AND "status" IN ('running')) OR ("lease_expires_at" IS NULL AND "lease_token" IS NULL AND NOT ("status" IN ('running')))))`
- `epiascollectionjob_period_end_day_chk`: `CHECK (("period_end_day" IS NULL OR "period_end_day" >= ("target_date")))`
- `epiascollectionjob_slot_uniq`: `UNIQUE ("connection_id", "connection_revision", "configuration_id", "source_kind", "target_date", "scheduled_for")`
- `epiascollectionjob_source_kind_chk`: `CHECK ("source_kind" IN ('realtime_generation', 'settlement_injection'))`
- `epiascollectionjob_status_chk`: `CHECK ("status" IN ('pending', 'running', 'succeeded', 'failed', 'superseded', 'awaiting_publication'))`
- column check (name generated by PostgreSQL): `CHECK ("attempts" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("connection_revision" >= 0)`

#### `metering_epiasconnection`

- `epiasconnection_id_plant_uniq`: `UNIQUE ("id", "plant_id")`
- `epiasconnection_revision_chk`: `CHECK ("revision" >= 1)`
- column check (name generated by PostgreSQL): `CHECK ("provider_plant_id" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("settlement_plant_id" >= 0)`

#### `metering_epiasconnectionrevision`

- `epiasconnectionrevision_connection_revision_uniq`: `UNIQUE ("connection_id", "revision")`
- `epiasconnectionrevision_eic_chk`: `CHECK (NOT ("eic" = ''))`
- `epiasconnectionrevision_provider_plant_id_chk`: `CHECK ("provider_plant_id" >= 1)`
- `epiasconnectionrevision_revision_chk`: `CHECK ("revision" >= 1)`
- column check (name generated by PostgreSQL): `CHECK ("provider_plant_id" >= 0)`
- column check (name generated by PostgreSQL): `CHECK ("revision" >= 0)`

#### `metering_epiassnapshot`

- `epiassnapshot_attempt_chk`: `CHECK ("attempt" >= 1)`
- `epiassnapshot_connection_idx`: `INDEX ("connection_id", "target_date", "available_at")`
- `epiassnapshot_immutable`: `TRIGGER BEFORE UPDATE FOR EACH ROW EXECUTE FUNCTION refuse_epiassnapshot_update()`
- `epiassnapshot_job_attempt_target_date_uniq`: `UNIQUE ("job_id", "attempt", "target_date")`
- `epiassnapshot_job_connection_configuration_fk`: `FOREIGN KEY (job_id, connection_id, configuration_id) REFERENCES metering_epiascollectionjob (id, connection_id, configuration_id) DEFERRABLE INITIALLY DEFERRED`
- `epiassnapshot_source_kind_chk`: `CHECK ("source_kind" IN ('realtime_generation', 'settlement_injection'))`
- column check (name generated by PostgreSQL): `CHECK ("attempt" >= 0)`

## change_requests (`pyracast.change_requests`)

```mermaid
erDiagram
    change_requests_changerequest {
        bigint id PK
        varchar_20 kind
        varchar_20 status
        bigint proposer_membership_id
        uuid idempotency_key
        varchar_500 reason
        varchar_64 payload_sha256
        timestamp_with_time_zone closed_at "nullable"
        varchar_500 decision_reason
        timestamp_with_time_zone outcome_seen_at "nullable"
        jsonb reviewed_context
        bigint configuration_id FK "references plants_plantconfiguration.id"
        bigint plant_id FK "references plants_plant.id"
        integer proposed_by_id FK "references auth_user.id"
        integer reviewed_by_id FK "nullable; references auth_user.id"
        timestamp_with_time_zone created_at
    }
    change_requests_incidentchange {
        bigint id PK
        varchar_10 action
        integer expected_revision "nullable"
        varchar_20 kind
        varchar_200 title
        varchar_2000 notes
        timestamp_with_time_zone starts_at "nullable"
        timestamp_with_time_zone ends_at "nullable"
        double_precision available_dc_mw "nullable"
        double_precision available_inverter_mw "nullable"
        double_precision available_export_mw "nullable"
        bigint applied_revision_id FK "nullable; references incidents_incidentrevision.id"
        bigint incident_id FK "nullable; references incidents_plantincident.id"
        bigint request_id FK, UK "references change_requests_changerequest.id"
    }
    change_requests_productionchangeitem {
        bigint id PK
        timestamp_with_time_zone starts_at
        timestamp_with_time_zone ends_at
        varchar_10 action
        double_precision energy_mwh
        varchar_20 quality
        varchar_200 source
        double_precision baseline_energy_mwh "nullable"
        varchar_40 baseline_source_kind
        bigint applied_observation_id FK "nullable; references plants_productionobservation.id"
        bigint expected_head_id FK "nullable; references plants_productionobservation.id"
        bigint request_id FK "references change_requests_changerequest.id"
    }
    plants_plantconfiguration ||--o{ change_requests_changerequest : "configuration_id"
    plants_plant ||--o{ change_requests_changerequest : "plant_id"
    auth_user ||--o{ change_requests_changerequest : "proposed_by_id"
    auth_user |o--o{ change_requests_changerequest : "reviewed_by_id"
    incidents_incidentrevision |o--o{ change_requests_incidentchange : "applied_revision_id"
    incidents_plantincident |o--o{ change_requests_incidentchange : "incident_id"
    change_requests_changerequest ||--o| change_requests_incidentchange : "request_id"
    plants_productionobservation |o--o{ change_requests_productionchangeitem : "applied_observation_id"
    plants_productionobservation |o--o{ change_requests_productionchangeitem : "expected_head_id"
    change_requests_changerequest ||--o{ change_requests_productionchangeitem : "request_id"
```

### Constraints and indexes

#### `change_requests_changerequest`

- `changerequest_configuration_plant_fk`: `FOREIGN KEY (configuration_id, plant_id) REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED`
- `changerequest_decision_reason_chk`: `CHECK ((NOT ("status" = 'rejected') OR NOT ("decision_reason" = '')))`
- `changerequest_idempotency_key_uniq`: `UNIQUE ("proposer_membership_id", "idempotency_key")`
- `changerequest_kind_chk`: `CHECK ("kind" IN ('production', 'incident'))`
- `changerequest_plant_status_idx`: `INDEX ("plant_id", "status", "created_at")`
- `changerequest_review_chk`: `CHECK ((("closed_at" IS NULL AND "decision_reason" = '' AND "outcome_seen_at" IS NULL AND "reviewed_by_id" IS NULL AND "status" = 'pending') OR ("closed_at" IS NOT NULL AND "outcome_seen_at" IS NULL AND "reviewed_by_id" IS NULL AND "status" = 'withdrawn') OR ("closed_at" IS NOT NULL AND "reviewed_by_id" IS NOT NULL AND "status" IN ('approved', 'rejected'))))`
- `changerequest_status_chk`: `CHECK ("status" IN ('pending', 'approved', 'rejected', 'withdrawn'))`
- column check (name generated by PostgreSQL): `CHECK ("proposer_membership_id" >= 0)`

#### `change_requests_incidentchange`

- `incidentchange_action_chk`: `CHECK ("action" IN ('create', 'update', 'resolve', 'cancel'))`
- `incidentchange_available_dc_mw_chk`: `CHECK (("available_dc_mw" IS NULL OR ("available_dc_mw" >= 0.0 AND "available_dc_mw" < 'Infinity'::float8)))`
- `incidentchange_available_export_mw_chk`: `CHECK (("available_export_mw" IS NULL OR ("available_export_mw" >= 0.0 AND "available_export_mw" < 'Infinity'::float8)))`
- `incidentchange_available_inverter_mw_chk`: `CHECK (("available_inverter_mw" IS NULL OR ("available_inverter_mw" >= 0.0 AND "available_inverter_mw" < 'Infinity'::float8)))`
- `incidentchange_ends_at_chk`: `CHECK (("starts_at" IS NULL OR "ends_at" IS NULL OR "ends_at" > ("starts_at")))`
- `incidentchange_kind_chk`: `CHECK ("kind" IN ('', 'inverter', 'panels', 'grid', 'other', 'multiple'))`
- `incidentchange_resolution_chk`: `CHECK ((NOT ("action" = 'resolve') OR "ends_at" IS NOT NULL))`
- `incidentchange_target_chk`: `CHECK ((("action" = 'create' AND "expected_revision" IS NULL AND "incident_id" IS NULL) OR ("action" IN ('update', 'resolve', 'cancel') AND "expected_revision" >= 1 AND "expected_revision" IS NOT NULL AND "incident_id" IS NOT NULL)))`
- `incidentchange_values_chk`: `CHECK (("action" IN ('resolve', 'cancel') OR ("action" IN ('create', 'update') AND "kind" IN ('inverter', 'panels', 'grid', 'other', 'multiple') AND "starts_at" IS NOT NULL AND NOT ("title" = ''))))`
- column check (name generated by PostgreSQL): `CHECK ("expected_revision" >= 0)`

#### `change_requests_productionchangeitem`

- `productionchangeitem_action_chk`: `CHECK ("action" IN ('set', 'revert'))`
- `productionchangeitem_baseline_energy_mwh_chk`: `CHECK (("baseline_energy_mwh" IS NULL OR ("baseline_energy_mwh" >= 0.0 AND "baseline_energy_mwh" < 'Infinity'::float8)))`
- `productionchangeitem_baseline_source_kind_chk`: `CHECK ("baseline_source_kind" IN ('', 'manual', 'realtime_generation', 'settlement_injection'))`
- `productionchangeitem_ends_at_chk`: `CHECK ("ends_at" > ("starts_at"))`
- `productionchangeitem_energy_mwh_chk`: `CHECK (("energy_mwh" >= 0.0 AND "energy_mwh" < 'Infinity'::float8))`
- `productionchangeitem_quality_chk`: `CHECK ("quality" IN ('valid', 'outage', 'curtailed', 'invalid'))`
- `productionchangeitem_request_starts_at_ends_at_uniq`: `UNIQUE ("request_id", "starts_at", "ends_at")`

## auth (`django.contrib.auth`)

```mermaid
erDiagram
    auth_group {
        integer id PK
        varchar_150 name UK
    }
    auth_group_permissions {
        bigint id PK
        integer group_id FK "references auth_group.id"
        integer permission_id FK "references auth_permission.id"
    }
    auth_permission {
        integer id PK
        varchar_255 name
        integer content_type_id FK "references django_content_type.id"
        varchar_100 codename
    }
    auth_user {
        integer id PK
        varchar_128 password
        timestamp_with_time_zone last_login "nullable"
        boolean is_superuser
        varchar_150 username UK
        varchar_150 first_name
        varchar_150 last_name
        varchar_254 email
        boolean is_staff
        boolean is_active
        timestamp_with_time_zone date_joined
    }
    auth_user_groups {
        bigint id PK
        integer user_id FK "references auth_user.id"
        integer group_id FK "references auth_group.id"
    }
    auth_user_user_permissions {
        bigint id PK
        integer user_id FK "references auth_user.id"
        integer permission_id FK "references auth_permission.id"
    }
    auth_group ||--o{ auth_group_permissions : "group_id"
    auth_permission ||--o{ auth_group_permissions : "permission_id"
    django_content_type ||--o{ auth_permission : "content_type_id"
    auth_user ||--o{ auth_user_groups : "user_id"
    auth_group ||--o{ auth_user_groups : "group_id"
    auth_user ||--o{ auth_user_user_permissions : "user_id"
    auth_permission ||--o{ auth_user_user_permissions : "permission_id"
```

### Constraints and indexes

#### `auth_group_permissions`

- `unique_together` (name generated by Django): `UNIQUE ("group_id", "permission_id")`

#### `auth_permission`

- `unique_together` (name generated by Django): `UNIQUE ("content_type_id", "codename")`

#### `auth_user_groups`

- `unique_together` (name generated by Django): `UNIQUE ("user_id", "group_id")`

#### `auth_user_user_permissions`

- `unique_together` (name generated by Django): `UNIQUE ("user_id", "permission_id")`

## contenttypes (`django.contrib.contenttypes`)

```mermaid
erDiagram
    django_content_type {
        integer id PK
        varchar_100 app_label
        varchar_100 model
    }
```

### Constraints and indexes

#### `django_content_type`

- `unique_together` (name generated by Django): `UNIQUE ("app_label", "model")`

## sessions (`django.contrib.sessions`)

```mermaid
erDiagram
    django_session {
        varchar_40 session_key PK
        text session_data
        timestamp_with_time_zone expire_date
    }
```

### Constraints and indexes

None beyond primary keys, foreign keys and unique columns.

## admin (`django.contrib.admin`)

```mermaid
erDiagram
    django_admin_log {
        integer id PK
        timestamp_with_time_zone action_time
        text object_id "nullable"
        varchar_200 object_repr
        smallint action_flag
        text change_message
        integer content_type_id FK "nullable; references django_content_type.id"
        integer user_id FK "references auth_user.id"
    }
    django_content_type |o--o{ django_admin_log : "content_type_id"
    auth_user ||--o{ django_admin_log : "user_id"
```

### Constraints and indexes

#### `django_admin_log`

- column check (name generated by PostgreSQL): `CHECK ("action_flag" >= 0)`

## account (`allauth.account`)

```mermaid
erDiagram
    account_emailaddress {
        integer id PK
        varchar_254 email
        boolean verified
        boolean primary
        integer user_id FK "references auth_user.id"
    }
    account_emailconfirmation {
        integer id PK
        timestamp_with_time_zone created
        timestamp_with_time_zone sent "nullable"
        varchar_64 key UK
        integer email_address_id FK "references account_emailaddress.id"
    }
    auth_user ||--o{ account_emailaddress : "user_id"
    account_emailaddress ||--o{ account_emailconfirmation : "email_address_id"
```

### Constraints and indexes

#### `account_emailaddress`

- `unique_primary_email`: `UNIQUE INDEX ("user_id", "primary") WHERE "primary"`
- `unique_verified_email`: `UNIQUE INDEX ("email") WHERE "verified"`
- `unique_together` (name generated by Django): `UNIQUE ("user_id", "email")`

## socialaccount (`allauth.socialaccount`)

```mermaid
erDiagram
    socialaccount_socialaccount {
        integer id PK
        varchar_200 provider
        varchar_191 uid
        timestamp_with_time_zone last_login
        timestamp_with_time_zone date_joined
        jsonb extra_data
        integer user_id FK "references auth_user.id"
    }
    socialaccount_socialapp {
        integer id PK
        varchar_30 provider
        varchar_40 name
        varchar_191 client_id
        varchar_191 secret
        varchar_191 key
        varchar_200 provider_id
        jsonb settings
    }
    socialaccount_socialtoken {
        integer id PK
        text token
        text token_secret
        timestamp_with_time_zone expires_at "nullable"
        integer account_id FK "references socialaccount_socialaccount.id"
        integer app_id FK "nullable; references socialaccount_socialapp.id"
    }
    auth_user ||--o{ socialaccount_socialaccount : "user_id"
    socialaccount_socialaccount ||--o{ socialaccount_socialtoken : "account_id"
    socialaccount_socialapp |o--o{ socialaccount_socialtoken : "app_id"
```

### Constraints and indexes

#### `socialaccount_socialaccount`

- `unique_together` (name generated by Django): `UNIQUE ("provider", "uid")`

#### `socialaccount_socialtoken`

- `unique_together` (name generated by Django): `UNIQUE ("app_id", "account_id")`

## mfa (`allauth.mfa`)

```mermaid
erDiagram
    mfa_authenticator {
        bigint id PK
        varchar_20 type
        jsonb data
        integer user_id FK "references auth_user.id"
        timestamp_with_time_zone created_at
        timestamp_with_time_zone last_used_at "nullable"
    }
    auth_user ||--o{ mfa_authenticator : "user_id"
```

### Constraints and indexes

#### `mfa_authenticator`

- `unique_authenticator_type`: `UNIQUE INDEX ("user_id", "type") WHERE "type" IN ('totp', 'recovery_codes')`

## usersessions (`allauth.usersessions`)

```mermaid
erDiagram
    usersessions_usersession {
        bigint id PK
        timestamp_with_time_zone created_at
        inet ip
        timestamp_with_time_zone last_seen_at
        varchar_40 session_key UK
        varchar_200 user_agent
        jsonb data
        integer user_id FK "references auth_user.id"
    }
    auth_user ||--o{ usersessions_usersession : "user_id"
```

### Constraints and indexes

None beyond primary keys, foreign keys and unique columns.
