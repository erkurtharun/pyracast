variable "project_id" {
  description = "Existing Google Cloud project with billing enabled."
  type        = string
}

variable "region" {
  description = "Region shared by the VM, backup bucket and registry."
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "VM and persistent disk zone, within region."
  type        = string
  default     = "europe-west1-b"
}

variable "secret_replica_location" {
  description = "Immutable Secret Manager replica location; null uses region. Preserve the original location when moving an existing deployment."
  type        = string
  default     = null
}

variable "name" {
  description = "Resource name prefix."
  type        = string
  default     = "pyracast"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.name))
    error_message = "Use 3-20 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "domain" {
  description = "Public DNS hostname pointing to the VM IPv4 address; no scheme or path."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$", var.domain))
    error_message = "Provide a lowercase DNS hostname, without a scheme, path or port."
  }
}

variable "acme_email" {
  description = "Operator email for ACME certificate issuance."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.acme_email))
    error_message = "Provide a valid operator email address."
  }
}

variable "machine_type" {
  description = "VM capacity for PostgreSQL, frontend, API, broker and independent workers."
  type        = string
  default     = "e2-medium"
}

variable "artifact_cleanup_dry_run" {
  description = "Preview registry deletions until live production retention tags are verified."
  type        = bool
  default     = true
}

variable "vm_image" {
  description = "Ubuntu 24.04 amd64 source image. An image self-link can pin an approved OS image."
  type        = string
  default     = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
}

variable "data_disk_gb" {
  description = "Persistent PostgreSQL, RabbitMQ and gateway certificate storage, independent of the boot disk."
  type        = number
  default     = 30

  validation {
    condition     = var.data_disk_gb >= 10
    error_message = "The persistent data disk must be at least 10 GB."
  }
}

variable "backup_retention_days" {
  description = "Days each backup generation cannot be permanently deleted, by an unlocked retention policy; an overwrite or a delete still makes the live object noncurrent. No extra storage cost: the lifecycle keeps every generation seven days anyway. 0 sets none."
  type        = number
  default     = 0

  validation {
    condition     = var.backup_retention_days >= 0 && var.backup_retention_days <= 7 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "Use whole days from 0 to 7."
  }
}

variable "backup_daily_tier" {
  description = "Keep the first hourly backup of each UTC day for 30 days under daily/ (storage cost). Change the published seven-day backup statement first."
  type        = bool
  default     = false
}

variable "data_disk_snapshot_days" {
  description = "Days a daily snapshot of the data disk is kept (storage cost); 0 takes none. At most seven, the published backup lifetime."
  type        = number
  default     = 0

  validation {
    condition     = var.data_disk_snapshot_days >= 0 && var.data_disk_snapshot_days <= 7 && floor(var.data_disk_snapshot_days) == var.data_disk_snapshot_days
    error_message = "Use whole days from 0 to 7."
  }
}

variable "deletion_protection" {
  description = "Protect the VM against accidental deletion."
  type        = bool
  default     = true
}

variable "runtime_secret_version" {
  description = "Numeric Secret Manager version activated on application startup. Add this version before the first release."
  type        = number
  default     = 1

  validation {
    condition     = var.runtime_secret_version >= 1 && floor(var.runtime_secret_version) == var.runtime_secret_version
    error_message = "Pin a positive integer secret version; latest is not a release contract."
  }
}

variable "operator_members" {
  description = "IAM members allowed to administer the VM through IAP and OS Login (for example user:name@example.com)."
  type        = set(string)
  default     = []
}

variable "database_secret_version" {
  description = "Numeric Secret Manager version containing the PostgreSQL owner password, read only on the host."
  type        = number
  default     = 1

  validation {
    condition     = var.database_secret_version >= 1 && floor(var.database_secret_version) == var.database_secret_version
    error_message = "Pin a positive integer database secret version."
  }
}

variable "migration_secret_version" {
  description = "Numeric Secret Manager version of the migration secret that releases read."
  type        = number
  default     = 1

  validation {
    condition     = var.migration_secret_version >= 1 && floor(var.migration_secret_version) == var.migration_secret_version
    error_message = "Pin a positive integer migration secret version."
  }
}

variable "origin_pull_ca" {
  description = "PEM certificates of the authority that issued the edge's origin-pull client certificate; empty accepts any edge handshake."
  type        = string
  default     = ""

  validation {
    condition     = var.origin_pull_ca == "" || (startswith(var.origin_pull_ca, "-----BEGIN CERTIFICATE-----") && !strcontains(var.origin_pull_ca, "PRIVATE KEY"))
    error_message = "Provide only the PEM certificates of the origin-pull authority, never a key."
  }
}

variable "github_repository" {
  description = "Repository name used to bind the exact release workflow. Numeric IDs are the identity boundary."
  type        = string
  default     = "erkurtharun/pyracast"
}

variable "github_repository_id" {
  description = "Immutable GitHub repository ID."
  type        = string
  default     = "1451399734"
}

variable "github_owner_id" {
  description = "Immutable GitHub owner ID."
  type        = string
  default     = "49256548"
}

variable "github_environment" {
  description = "GitHub environment used by the trusted release job."
  type        = string
  default     = "production"
}

variable "github_release_workflow" {
  description = "Workflow path permitted to impersonate the deployment service account."
  type        = string
  default     = ".github/workflows/release.yml"
}
