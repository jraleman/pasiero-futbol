terraform {
  required_version = ">= 1.10"

  # Local state by default: it holds the generated secrets (as Key Vault does), so keep it
  # out of git (.gitignore) and off shared disks. For a team, an azurerm backend instead.

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  # Only the providers this uses, registered on first apply (a new subscription has none)
  resource_provider_registrations = "none"
  resource_providers_to_register = [
    "Microsoft.CognitiveServices",
    "Microsoft.Compute",
    "Microsoft.ContainerRegistry",
    "Microsoft.DevTestLab",
    "Microsoft.Insights",
    "Microsoft.KeyVault",
    "Microsoft.MachineLearningServices",
    "Microsoft.ManagedIdentity",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
    "Microsoft.Storage",
  ]

  features {
    key_vault {
      # destroy, then apply again, reuses the names
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
    cognitive_account {
      purge_soft_delete_on_destroy = true
    }
    resource_group {
      # Azure ML adds resources of its own (the image registry, alert rules)
      prevent_deletion_if_contains_resources = false
    }
  }
}
