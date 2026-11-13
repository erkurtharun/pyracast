resource "google_service_account" "runtime" {
  account_id   = "${var.name}-runtime"
  display_name = "Pyracast VM runtime"
  depends_on   = [google_project_service.required]
}

resource "google_service_account" "deploy" {
  account_id   = "${var.name}-deploy"
  display_name = "Pyracast verified release deployment"
  depends_on   = [google_project_service.required]
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = toset(["roles/logging.logWriter"])
  project  = var.project_id
  role     = each.value
  member   = google_service_account.runtime.member
}

resource "google_artifact_registry_repository_iam_member" "runtime_read" {
  location   = google_artifact_registry_repository.application.location
  repository = google_artifact_registry_repository.application.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.runtime.member
}

resource "google_artifact_registry_repository_iam_member" "deploy_write" {
  location   = google_artifact_registry_repository.application.location
  repository = google_artifact_registry_repository.application.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deploy.member
}

resource "google_secret_manager_secret_iam_member" "runtime_read" {
  for_each  = toset(["runtime", "database"])
  secret_id = google_secret_manager_secret.application[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

resource "google_secret_manager_secret_iam_member" "migration_read" {
  secret_id = google_secret_manager_secret.application["migration"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.deploy.member
}

resource "google_project_iam_custom_role" "deploy_discovery" {
  role_id     = "${replace(var.name, "-", "_")}_deploy_discovery"
  title       = "Pyracast deployment discovery"
  description = "Discover the deployment target for IAP SSH."
  permissions = [
    "compute.instances.get",
    "compute.instances.list",
    "compute.projects.get",
  ]
  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "deploy_discovery" {
  project = var.project_id
  role    = google_project_iam_custom_role.deploy_discovery.name
  member  = google_service_account.deploy.member
}

resource "google_storage_bucket_iam_member" "runtime_backup" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])
  bucket   = google_storage_bucket.backup.name
  role     = each.value
  member   = google_service_account.runtime.member
}

locals {
  ssh_members = merge(
    { deploy = google_service_account.deploy.member },
    { for member in var.operator_members : member => member },
  )
}

resource "google_compute_instance_iam_member" "ssh_admin" {
  for_each      = local.ssh_members
  instance_name = google_compute_instance.application.name
  zone          = var.zone
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_iap_tunnel_instance_iam_member" "ssh_tunnel" {
  for_each = local.ssh_members
  instance = google_compute_instance.application.name
  zone     = var.zone
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title      = "ssh-only"
    expression = "destination.port == 22"
  }
}

resource "google_service_account_iam_member" "ssh_runtime_identity" {
  for_each           = local.ssh_members
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

resource "google_project_iam_custom_role" "operator_discovery" {
  role_id     = "${replace(var.name, "-", "_")}_ssh_discovery"
  title       = "Pyracast SSH target discovery"
  permissions = ["compute.instances.get", "compute.instances.list", "compute.projects.get"]
  depends_on  = [google_project_service.required]
}

resource "google_project_iam_member" "operator_discovery" {
  for_each = var.operator_members
  project  = var.project_id
  role     = google_project_iam_custom_role.operator_discovery.name
  member   = each.value
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.name}-github"
  display_name              = "Pyracast GitHub releases"
  depends_on                = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
  }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_owner_id}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref == '${var.github_repository}/${var.github_release_workflow}@refs/heads/main'",
    "assertion.sub == 'repo:${var.github_repository}:environment:${var.github_environment}'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_deploy" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}
