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

resource "google_monitoring_alert_policy" "operations" {
  for_each = {
    backup      = { event = "pyracast_backup_failed", description = "Database backup failed; inspect pyracast-backup.service and verify the latest private-bucket backup before releasing." }
    runtime     = { event = "pyracast_runtime_unhealthy", description = "A required container remains unhealthy after supervision; inspect workers and release-blocked before restarting." }
    host        = { event = "pyracast_host_capacity", description = "The VM has less than 15 percent disk space or 10 percent available memory; inspect capacity before jobs are interrupted." }
    certificate = { event = "pyracast_certificate_expiring", description = "The origin certificate has less than a quarter of its lifetime left, so its renewal is failing; inspect the gateway logs and port 80 reachability for the ACME challenge." }
  }
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
    content   = each.value.description
  }
}

resource "google_logging_metric" "watchdog_heartbeat" {
  name = "${var.name}-watchdog-heartbeat"

  filter = join(" AND ", [
    "resource.type=\"gce_instance\"",
    "logName=\"projects/${var.project_id}/logs/pyracast-operations\"",
    "jsonPayload.event=\"pyracast_watchdog_heartbeat\"",
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

# Counts the pyracast.security lines the API ships through the gcplogs driver, by event.
resource "google_logging_metric" "security_events" {
  name = "${var.name}-security-events"

  filter = join(" AND ", [
    "resource.type=\"gce_instance\"",
    "logName=\"projects/${var.project_id}/logs/gcplogs-docker-driver\"",
    "jsonPayload.message:\" pyracast.security \"",
  ])

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

  conditions {
    display_name = "${each.value.signal} above ${each.value.threshold} in ten minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.security_events.name}\"",
        "metric.label.event=\"${each.value.signal}\"",
        "resource.type=\"gce_instance\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
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
    content   = each.value.description
  }
}
