# Azure OpenAI: the team of agents' model (ai/agent/team.py). Keys are off unless openai_keys:
# the VM signs in with its managed identity (DefaultAzureCredential), with the Cognitive
# Services OpenAI User role and nothing more.

resource "azurerm_cognitive_account" "openai" {
  name                  = "oai-${var.name}-${local.suffix}"
  resource_group_name   = azurerm_resource_group.main.name
  location              = azurerm_resource_group.main.location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "oai-${var.name}-${local.suffix}"
  local_auth_enabled    = var.openai_keys
  tags                  = local.tags
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.openai_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }

  sku {
    name     = var.openai_sku
    capacity = var.openai_capacity
  }

  # The model's version stays fixed until this says otherwise
  version_upgrade_option = "NoAutoUpgrade"
}

resource "azurerm_role_assignment" "openai_vm" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_linux_virtual_machine.main.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# You, in the portal's playground, and code run with az login (DefaultAzureCredential)
resource "azurerm_role_assignment" "openai_deployer" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = data.azurerm_client_config.current.object_id
}
