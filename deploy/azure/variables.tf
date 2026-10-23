variable "subscription_id" {
  description = "The subscription. Default: ARM_SUBSCRIPTION_ID."
  type        = string
  default     = null
}

variable "name" {
  description = "Prefix of every resource's name."
  type        = string
  default     = "match-intelligence"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "name: 3 to 25 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "location" {
  description = "Region: it must offer the VM size, the Azure OpenAI model and Azure ML."
  type        = string
  default     = "uksouth"
}

# ── The VM ──────────────────────────────────────────────────────────────────────

variable "size" {
  description = "VM size. Standard_E4s_v5 is p4n4-emu's nuc host: 4 vCPUs, 32 GB, of which the stack gets a NUC's 4 cores and 14 GB."
  type        = string
  default     = "Standard_E4s_v5"
}

variable "os_disk_gb" {
  description = "OS disk (Premium SSD): the images, Ollama's model (4.6 GB) and the matches."
  type        = number
  default     = 128
}

variable "ssh_public_key_path" {
  description = "Public key for the VM's p4n4 user."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_source_cidr" {
  description = "Where SSH may come from. Default: this machine's public IPv4 address, /32."
  type        = string
  default     = null
}

variable "dns_label" {
  description = "The address's first label: <label>.<location>.cloudapp.azure.com. Default: name plus a random suffix."
  type        = string
  default     = null
}

variable "repo_url" {
  description = "The repository the VM clones and deploys (public). Null: copy it with vm/push.sh instead."
  type        = string
  default     = null
}

variable "git_ref" {
  description = "Branch the VM deploys."
  type        = string
  default     = "main"
}

variable "p4n4_refs" {
  description = "Branches or tags of p4n4-emu, p4n4-lib and p4n4-dashboard on the VM, and the p4n4-api image."
  type = object({
    emu       = optional(string, "main")
    lib       = optional(string, "main")
    dashboard = optional(string, "main")
    api       = optional(string, "0.3.0")
  })
  default = {}
}

variable "shutdown_time" {
  description = "Stop (deallocate) the VM every day at this time, HHMM, to save money; null keeps it up (judging)."
  type        = string
  default     = null

  validation {
    condition     = var.shutdown_time == null || can(regex("^([01][0-9]|2[0-3])[0-5][0-9]$", var.shutdown_time))
    error_message = "shutdown_time is HHMM, e.g. 2300."
  }
}

variable "shutdown_timezone" {
  description = "Windows time zone name for shutdown_time."
  type        = string
  default     = "GMT Standard Time"
}

variable "budget" {
  description = "Monthly budget for the resource group, in the billing currency: emails at 50%, 80% and 100% spent, and on a forecast over 100%. Null: no budget."
  type        = number
  default     = 100

  validation {
    condition     = var.budget == null || try(var.budget > 0, false)
    error_message = "budget is an amount above 0, or null."
  }
}

variable "budget_emails" {
  description = "Who gets the budget's emails, besides the subscription's owners."
  type        = list(string)
  default     = []
}

# ── Azure OpenAI ────────────────────────────────────────────────────────────────

variable "openai_model" {
  description = "The agents' model: a chat model with tool calling (az cognitiveservices model list -l <location>)."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "openai_model_version" {
  type    = string
  default = "2025-04-14"
}

variable "openai_sku" {
  description = "Deployment type: GlobalStandard (pay per token, any region's capacity) or Standard (in region)."
  type        = string
  default     = "GlobalStandard"
}

variable "openai_capacity" {
  description = "Thousands of tokens a minute. A recap is about 6k tokens; the team's answer 10–20k."
  type        = number
  default     = 100
}

variable "openai_keys" {
  description = "Also allow keys (az cognitiveservices account keys list), for the stack on your own machine: its agent container has no Azure CLI to sign in with. The VM never uses one."
  type        = bool
  default     = false
}

# ── Azure Machine Learning ──────────────────────────────────────────────────────

variable "ml" {
  description = "The Azure ML workspace and CPU cluster that train the shirt-number model."
  type        = bool
  default     = true
}

variable "ml_cluster_size" {
  description = "The training cluster's nodes: CPUs (a new subscription's quota is often 10 cores a family); it scales to zero between jobs."
  type        = string
  default     = "Standard_F8s_v2"
}

variable "ml_max_nodes" {
  type    = number
  default = 1
}

# ── GitHub Actions ──────────────────────────────────────────────────────────────

variable "github_repo" {
  description = "owner/name: the deploy workflow on its main branch may redeploy the VM (OIDC, no secret). Null: no workflow identity."
  type        = string
  default     = null
}
