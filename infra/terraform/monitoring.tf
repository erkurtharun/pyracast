resource "google_monitoring_notification_channel" "operator" {
  display_name = "${var.name} operator email"
  type         = "email"
  labels = {
    email_address = var.acme_email
  }
  depends_on = [google_project_service.required]
}

resource "google_monitoring_uptime_check_config" "readiness" {
  display_name       = "${var.name} HTTPS readiness"
  timeout            = "10s"
  period             = "300s"
  selected_regions   = ["EUROPE", "USA", "ASIA_PACIFIC"]
  log_check_failures = true

  http_check {
    path           = "/health/ready/"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_value = 200
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.domain
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "readiness" {
  display_name          = "${var.name} HTTPS readiness failure"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator.name]
  user_labels           = local.labels

  conditions {
    display_name = "At least two checker locations fail for five minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\"",
        "metric.label.check_id=\"${google_monitoring_uptime_check_config.readiness.uptime_check_id}\"",
        "resource.type=\"uptime_url\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Pyracast HTTPS readiness has failed from at least two checker locations for five minutes. Inspect the VM, local PostgreSQL and the latest release. Check /var/lib/pyracast/release-blocked before restarting the application."
  }
}

locals {
  # Every operations event a log-match policy alerts on, each with its own policy. A key names an
  # existing policy (backup, runtime, host, certificate, secrets and origin_pull existed first):
  # renaming one replaces that policy.
  operations_alerts = {
    backup            = { event = "pyracast_backup_failed", description = "A database backup failed or the last verified one is older than 90 minutes." }
    backup_setting    = { event = "pyracast_backup_setting_invalid", description = "daily_backups in the host configuration is neither true nor false; an hourly backup run ends failed." }
    backup_daily_copy = { event = "pyracast_backup_daily_copy_failed", description = "The hourly backup succeeded, but its daily copy failed." }
    rehearsal         = { event = "pyracast_rehearsal_unhealthy", description = "The restore rehearsal failed or none passed within eight days." }
    runtime           = { event = "pyracast_runtime_unhealthy", description = "A container stays unhealthy after the watchdog's restart, PostgreSQL or RabbitMQ is unhealthy, or pyracast.service is stopped outside a release." }
    delivery          = { event = "pyracast_delivery_unhealthy", description = "The delivery health check failed; the line names the failing checks with their counts." }
    watchdog_step     = { event = "pyracast_watchdog_step_failed", description = "One watchdog check could not run; the line names the step." }
    release_blocked   = { event = "pyracast_release_blocked", description = "A release has kept the site in maintenance for more than an hour." }
    host              = { event = "pyracast_host_capacity", description = "The VM has less than 15 percent disk space or 10 percent available memory." }
    certificate       = { event = "pyracast_certificate_expiring", description = "The origin certificate has less than a quarter of its lifetime left, so its renewal is failing." }
    secrets           = { event = "pyracast_secrets_failed", description = "The runtime secret helper could not prepare a release's environment files; the line names the category." }
    origin_pull       = { event = "pyracast_origin_pull_unenforced", description = "Origin pulls are required, but port 443 no longer refuses a handshake without the edge's certificate." }
  }
}

resource "google_monitoring_alert_policy" "operations" {
  for_each              = local.operations_alerts
  display_name          = "${var.name} ${each.key} failure"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator.name]
  user_labels           = local.labels

  conditions {
    display_name = "${each.key} operational warning"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"gce_instance\"",
        "logName=\"projects/${var.project_id}/logs/pyracast-operations\"",
        "jsonPayload.event=\"${each.value.event}\"",
      ])
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = "${each.value.event}: ${each.value.description} What to check first and how to recover: the event's row under Alert events in DEPLOYMENT.md. A line with test set to true is a test alert."
  }
}

resource "google_logging_metric" "watchdog_heartbeat" {
  name = "${var.name}-watchdog-heartbeat"

  filter = join(" AND ", [
    "resource.type=\"gce_instance\"",
    "logName=\"projects/${var.project_id}/logs/pyracast-operations\"",
    "jsonPayload.event=\"pyracast_watchdog_heartbeat\"",
    "NOT jsonPayload.test=true",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "watchdog_heartbeat" {
  display_name          = "${var.name} watchdog heartbeat missing"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator.name]
  user_labels           = local.labels

  conditions {
    display_name = "No watchdog heartbeat for fifteen minutes"
    condition_absent {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.watchdog_heartbeat.name}\"",
        "resource.type=\"gce_instance\"",
      ])
      duration = "900s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = "The host watchdog has reported nothing for fifteen minutes: the VM is down or pyracast-watchdog.timer stopped. Inspect systemctl status pyracast-watchdog.timer and journalctl -u pyracast-watchdog.service."
  }
}

# One line per watchdog run that restarted containers which then recovered. Counted in total:
# `services` is a list, and a label holds one value, so per service would miss a run that
# restarted two.
resource "google_logging_metric" "runtime_restarts" {
  name = "${var.name}-runtime-restarts"

  filter = join(" AND ", [
    "resource.type=\"gce_instance\"",
    "logName=\"projects/${var.project_id}/logs/pyracast-operations\"",
    "jsonPayload.event=\"pyracast_runtime_restarted\"",
    "NOT jsonPayload.test=true",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "runtime_restarts" {
  display_name          = "${var.name} repeated container restarts"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator.name]
  user_labels           = local.labels

  # The window is the named period plus the minute the condition must hold (duration).
  conditions {
    display_name = "Three watchdog restarts within one hour"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.runtime_restarts.name}\"",
        "resource.type=\"gce_instance\"",
      ])
      comparison              = "COMPARISON_GT"
      threshold_value         = 2
      duration                = "60s"
      evaluation_missing_data = "EVALUATION_MISSING_DATA_INACTIVE"

      aggregations {
        alignment_period     = "3660s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  # An empty window closes the incident (evaluation_missing_data); this backs that up, never
  # sooner than the longest window could still hold the lines that opened it.
  alert_strategy {
    auto_close = "3660s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = "pyracast_runtime_restarted: the watchdog restarted containers in at least three runs within one hour and they recovered each time, so one keeps crashing. Test alerts never count. What to check first and how to recover: the event's row under Alert events in DEPLOYMENT.md."
  }
}

locals {
  # gcplogs writes each container line as jsonPayload.message and the container's name, which
  # Docker sets and no line can change, as jsonPayload.container.name: Compose names it
  # /<project>-<service>-<n>, or /<project>-<service>-run-<id> for `compose run`. Only the
  # services that write security lines count. Their records start one line each with
  # "<date> <time,ms> <level> <logger> " (pyracast.log_lines; every further line is indented),
  # so the anchored pattern matches only a record of the pyracast.security logger.
  security_log_filter = join(" AND ", [
    "resource.type=\"gce_instance\"",
    "logName=\"projects/${var.project_id}/logs/gcplogs-docker-driver\"",
    "jsonPayload.container.name=~\"^/pyracast-(api|worker|notifications|dispatcher|migrate)-([0-9]+|run-[0-9a-f]+)$\"",
    "jsonPayload.message=~\"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} [A-Z]+ pyracast[.]security \"",
  ])
}

# Security lines are kept 365 days here; the default bucket keeps its own copy for 30 days.
resource "google_logging_project_bucket_config" "security" {
  project        = var.project_id
  location       = var.region
  bucket_id      = "${var.name}-security"
  description    = "pyracast.security lines, kept 365 days."
  retention_days = 365

  depends_on = [google_project_service.required]
}

# A sink into a log bucket of its own project is authorized without a service account or grant.
resource "google_logging_project_sink" "security" {
  name        = "${var.name}-security"
  description = "Routes the pyracast.security lines to their 365-day bucket."
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/${google_logging_project_bucket_config.security.location}/buckets/${google_logging_project_bucket_config.security.bucket_id}"
  filter      = local.security_log_filter
}

# Counts by event the same pyracast.security lines the security sink keeps.
resource "google_logging_metric" "security_events" {
  name = "${var.name}-security-events"

  filter = local.security_log_filter

  label_extractors = {
    event = "REGEXP_EXTRACT(jsonPayload.message, \"\\\"event\\\": \\\"([a-z_]+)\\\"\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"

    labels {
      key        = "event"
      value_type = "STRING"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "security" {
  for_each = {
    sign_in_lockouts       = { signal = "login_locked", threshold = 20, description = "More than 20 sign-ins were refused by the failure limits within ten minutes. Inspect the pyracast.security lines for the client addresses and subjects involved." }
    second_factor_lockouts = { signal = "second_factor_locked", threshold = 5, description = "More than 5 second-factor checks were refused by the failure limits within ten minutes. Inspect the pyracast.security lines for the accounts involved." }
    authorization_refusals = { signal = "authorization_denied", threshold = 30, description = "Signed-in accounts were refused more than 30 times within ten minutes. Inspect the pyracast.security lines for the accounts, views and workspaces involved." }
    csrf_refusals          = { signal = "csrf_refused", threshold = 10, description = "Signed-in requests failed the CSRF check more than 10 times within ten minutes. Inspect the pyracast.security lines and the client addresses involved." }
    invitation_limits      = { signal = "invitation_limited", threshold = 10, description = "Invitation budgets refused more than 10 invitations within ten minutes. Inspect the pyracast.security lines for the senders and workspaces involved." }
    proxy_headers          = { signal = "invalid_proxy_headers", threshold = 0, description = "A request reached the API with proxy headers it does not trust. Inspect the gateway configuration and the peer address in the pyracast.security line." }
    admin_factor           = { signal = "admin_mfa_provisioned", threshold = 0, description = "An operator provisioned a platform administrator's second factor. Confirm the change was planned." }
  }
  display_name          = "${var.name} security ${each.key}"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator.name]
  user_labels           = local.labels

  # The window is the named period plus the minute the condition must hold (duration).
  conditions {
    display_name = "${each.value.signal} above ${each.value.threshold} in ten minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.security_events.name}\"",
        "metric.label.event=\"${each.value.signal}\"",
        "resource.type=\"gce_instance\"",
      ])
      comparison              = "COMPARISON_GT"
      threshold_value         = each.value.threshold
      duration                = "60s"
      evaluation_missing_data = "EVALUATION_MISSING_DATA_INACTIVE"

      aggregations {
        alignment_period     = "660s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  # An empty window closes the incident (evaluation_missing_data); this backs that up, never
  # sooner than the longest window could still hold the lines that opened it.
  alert_strategy {
    auto_close = "3660s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = each.value.description
  }
}

# SetIamPolicy also carries changes of the project's owners and of its audit configuration.
resource "google_logging_metric" "access_changes" {
  name = "${var.name}-access-changes"

  filter = join(" AND ", [
    "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\"",
    "protoPayload.serviceName=~\"^(cloudresourcemanager|iam)[.]googleapis[.]com$\"",
    "protoPayload.methodName=~\"^(SetIamPolicy|google[.]iam[.]admin[.]v1[.](Create|Update|Delete|Undelete)Role)$\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "access_changes" {
  display_name          = "${var.name} project access changed"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator.name]
  user_labels           = local.labels

  # The window is the named period plus the minute the condition must hold (duration).
  conditions {
    display_name = "An access change within ten minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.access_changes.name}\"",
        "resource.type=\"global\"",
      ])
      comparison              = "COMPARISON_GT"
      threshold_value         = 0
      duration                = "60s"
      evaluation_missing_data = "EVALUATION_MISSING_DATA_INACTIVE"

      aggregations {
        alignment_period     = "660s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "3660s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = "The project's IAM policy (members, owners or audit configuration) or one of its custom roles changed. A change made by a reviewed Terraform apply is expected; look into any other. What to check first and how to recover: the Project access changed row under Other alerts in DEPLOYMENT.md."
  }
}
