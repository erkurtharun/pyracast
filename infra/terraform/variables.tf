variable "project_id" {
  description = "Existing Google Cloud project with billing enabled."
  type        = string
}

variable "region" {
  description = "Region shared by the VM, private database, registry and secrets."
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "VM and persistent disk zone, within region."
  type        = string
  default     = "europe-west1-b"
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
  description = "Operator email for Caddy certificate issuance."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.acme_email))
    error_message = "Provide a valid operator email address."
  }
}

variable "machine_type" {
  description = "VM capacity for the frontend, API, broker and independent workers."
  type        = string
  default     = "e2-standard-2"
}

variable "vm_image" {
  description = "Ubuntu 24.04 amd64 source image. An image self-link can pin an approved OS image."
  type        = string
  default     = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
}

variable "data_disk_gb" {
  description = "Persistent RabbitMQ and Caddy storage, independent of the boot disk."
  type        = number
  default     = 30

  validation {
    condition     = var.data_disk_gb >= 10
    error_message = "The persistent data disk must be at least 10 GB."
  }
}

variable "database_tier" {
  description = "Cloud SQL Enterprise machine type; choose after measuring load."
  type        = string
  default     = "db-custom-1-3840"
}

variable "database_ha" {
  description = "Use regional Cloud SQL HA. The application VM remains a single availability zone."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the VM and Cloud SQL instance against accidental deletion."
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
