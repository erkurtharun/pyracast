output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "zone" {
  value = var.zone
}

output "instance_name" {
  value = google_compute_instance.application.name
}

output "instance_ip" {
  value = google_compute_address.application.address
}

output "runtime_service_account_email" {
  value = google_service_account.runtime.email
}

output "deploy_service_account_email" {
  value = google_service_account.deploy.email
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "artifact_registry_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.application.repository_id}"
}

output "backup_bucket" {
  value = google_storage_bucket.backup.name
}

output "database_secret_id" {
  value = google_secret_manager_secret.application["database"].id
}

output "runtime_secret_id" {
  value = google_secret_manager_secret.application["runtime"].id
}

output "migration_secret_id" {
  value = google_secret_manager_secret.application["migration"].id
}

output "uptime_check_id" {
  value = google_monitoring_uptime_check_config.readiness.uptime_check_id
}

output "operator_notification_channel" {
  description = "Confirm email notification delivery in Cloud Monitoring before relying on alerts."
  value       = google_monitoring_notification_channel.operator.name
}

output "github_environment_variables" {
  description = "Nonsecret GitHub production environment variables. Enable deployment only after provisioning and smoke checks."
  value = {
    GCP_PROJECT_ID               = var.project_id
    GCP_ZONE                     = var.zone
    GCP_INSTANCE                 = google_compute_instance.application.name
    GCP_WIF_PROVIDER             = google_iam_workload_identity_pool_provider.github.name
    GCP_DEPLOY_SERVICE_ACCOUNT   = google_service_account.deploy.email
    GCP_ARTIFACT_REPOSITORY      = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.application.repository_id}"
    GCP_MIGRATION_SECRET         = google_secret_manager_secret.application["migration"].secret_id
    GCP_MIGRATION_SECRET_VERSION = tostring(var.migration_secret_version)
    PYRACAST_PUBLIC_ORIGIN       = "https://${var.domain}"
  }
}
