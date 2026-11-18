locals {
  labels = {
    application = "pyracast"
    managed_by  = "terraform"
  }
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "oslogin.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "application" {
  name                    = var.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "application" {
  name                     = var.name
  region                   = var.region
  network                  = google_compute_network.application.id
  ip_cidr_range            = "10.20.0.0/24"
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_address" "application" {
  name       = var.name
  region     = var.region
  depends_on = [google_project_service.required]
}

resource "google_compute_firewall" "https" {
  name    = "${var.name}-https"
  network = google_compute_network.application.id
  # Origin HTTPS accepts only Cloudflare proxy IPv4 ranges: https://www.cloudflare.com/ips-v4
  source_ranges = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
  ]
  target_service_accounts = [google_service_account.runtime.email]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "acme" {
  name                    = "${var.name}-acme"
  network                 = google_compute_network.application.id
  source_ranges           = ["0.0.0.0/0"]
  target_service_accounts = [google_service_account.runtime.email]

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
}

resource "google_compute_firewall" "iap_ssh" {
  name                    = "${var.name}-iap-ssh"
  network                 = google_compute_network.application.id
  source_ranges           = ["35.235.240.0/20"]
  target_service_accounts = [google_service_account.runtime.email]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_artifact_registry_repository" "application" {
  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  description   = "Verified Pyracast release images"
  labels        = local.labels
  depends_on    = [google_project_service.required]

  docker_config {
    immutable_tags = false
  }

  cleanup_policy_dry_run = var.artifact_cleanup_dry_run

  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "30d"
    }
  }

  cleanup_policies {
    id     = "keep-recent-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  cleanup_policies {
    id     = "keep-production"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["production"]
    }
  }
}

resource "google_secret_manager_secret" "application" {
  for_each  = toset(["runtime", "migration", "database"])
  secret_id = "${var.name}-${each.key}"
  labels    = local.labels

  replication {
    user_managed {
      replicas {
        location = coalesce(var.secret_replica_location, var.region)
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "backup" {
  name                        = "${var.project_id}-${var.name}-backups"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true
  }

  # Age counts from each generation's creation: a live backup becomes noncurrent at seven days
  # and a later lifecycle run deletes it, as it deletes a backup deleted or replaced earlier.
  lifecycle_rule {
    condition {
      age        = 7
      with_state = "ANY"
    }
    action {
      type = "Delete"
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_disk" "data" {
  name   = "${var.name}-data"
  type   = "pd-balanced"
  zone   = var.zone
  size   = var.data_disk_gb
  labels = local.labels

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_instance" "application" {
  name                = var.name
  zone                = var.zone
  machine_type        = var.machine_type
  deletion_protection = var.deletion_protection
  labels              = local.labels

  boot_disk {
    initialize_params {
      image = var.vm_image
      type  = "pd-standard"
      size  = 30
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "pyracast-data"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.application.id
    stack_type = "IPV4_ONLY"
    access_config {
      nat_ip = google_compute_address.application.address
    }
  }

  service_account {
    email  = google_service_account.runtime.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    startup-script = templatefile("${path.module}/templates/startup.sh.tftpl", {
      network_guard_base64 = filebase64("${path.module}/templates/network-guard.sh")
      host_config_base64 = base64encode(jsonencode({
        project_id              = var.project_id
        runtime_secret_id       = google_secret_manager_secret.application["runtime"].id
        runtime_secret_version  = "${google_secret_manager_secret.application["runtime"].id}/versions/${var.runtime_secret_version}"
        database_secret_version = "${google_secret_manager_secret.application["database"].id}/versions/${var.database_secret_version}"
        backup_bucket           = google_storage_bucket.backup.name
        domain                  = var.domain
        acme_email              = var.acme_email
        origin_pull_ca          = var.origin_pull_ca
      }))
    })
  }

  lifecycle {
    precondition {
      condition     = startswith(var.zone, "${var.region}-")
      error_message = "zone must be within region."
    }
  }

  depends_on = [google_project_iam_member.runtime_roles]
}
