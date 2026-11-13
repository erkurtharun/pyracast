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
    "servicenetworking.googleapis.com",
    "serviceusage.googleapis.com",
    "sqladmin.googleapis.com",
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
}

resource "google_compute_global_address" "sql_private_range" {
  name          = "${var.name}-sql-private"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.application.id
}

resource "google_service_networking_connection" "sql" {
  network                 = google_compute_network.application.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.sql_private_range.name]
}

resource "google_compute_address" "application" {
  name       = var.name
  region     = var.region
  depends_on = [google_project_service.required]
}

resource "google_compute_firewall" "https" {
  name                    = "${var.name}-web"
  network                 = google_compute_network.application.id
  source_ranges           = ["0.0.0.0/0"]
  target_service_accounts = [google_service_account.runtime.email]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
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
    immutable_tags = true
  }
}

resource "google_secret_manager_secret" "application" {
  for_each  = toset(["runtime", "migration"])
  secret_id = "${var.name}-${each.key}"
  labels    = local.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.required]
}

resource "google_sql_database_instance" "application" {
  name                = var.name
  region              = var.region
  database_version    = "POSTGRES_18"
  deletion_protection = var.deletion_protection

  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.database_tier
    availability_type           = var.database_ha ? "REGIONAL" : "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 20
    disk_autoresize             = true
    deletion_protection_enabled = var.deletion_protection
    connector_enforcement       = "REQUIRED"
    user_labels                 = local.labels

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.application.id
      enable_private_path_for_google_cloud_services = false
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "01:00"
      location                       = var.region
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 7
      }
    }

    maintenance_window {
      day          = 7
      hour         = 2
      update_track = "stable"
    }
  }

  depends_on = [google_service_networking_connection.sql]
}

resource "google_sql_database" "application" {
  name     = "pyracast"
  instance = google_sql_database_instance.application.name

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_resource_policy" "data_snapshot" {
  name   = "${var.name}-data-daily"
  region = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "03:00"
      }
    }

    retention_policy {
      max_retention_days    = 7
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }

    snapshot_properties {
      labels            = local.labels
      storage_locations = [var.region]
    }
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

resource "google_compute_disk_resource_policy_attachment" "data_snapshot" {
  name = google_compute_resource_policy.data_snapshot.name
  disk = google_compute_disk.data.name
  zone = var.zone
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
      type  = "pd-balanced"
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
        project_id                = var.project_id
        runtime_secret_id         = google_secret_manager_secret.application["runtime"].id
        runtime_secret_version    = "${google_secret_manager_secret.application["runtime"].id}/versions/${var.runtime_secret_version}"
        cloud_sql_connection_name = google_sql_database_instance.application.connection_name
        domain                    = var.domain
        acme_email                = var.acme_email
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
