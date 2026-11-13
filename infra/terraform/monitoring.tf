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
  period             = "60s"
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
    content   = "Pyracast HTTPS readiness has failed from at least two checker locations for five minutes. Inspect the VM, Cloud SQL connectivity and the latest release. Check /var/lib/pyracast/release-blocked before restarting the application."
  }
}
