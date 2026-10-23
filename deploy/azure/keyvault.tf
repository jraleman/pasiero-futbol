# The stack's secrets: generated here, kept in Key Vault, read by the VM at deploy time with
# its managed identity (vm/deploy.sh). Nothing in the repository or the VM's image.

locals {
  # Key Vault name → what it is. Tokens are hex (they go in URLs and headers as they are).
  secrets = {
    "ingest-read-token"  = "the staff's token: the analysis room's login (INGEST_READ_TOKEN)"
    "site-token"         = "the match device's token (SITE_TOKEN)"
    "notify-token"       = "the ingest service → n8n (NOTIFY_TOKEN)"
    "influxdb-password"  = "InfluxDB's admin"
    "influxdb-token"     = "InfluxDB's API token"
    "grafana-password"   = "Grafana's admin"
    "n8n-owner-password" = "n8n's owner"
    "n8n-encryption-key" = "n8n's credentials store"
    "judge-password"     = "the judges' login: the dashboard's judge user and the inbox"
    "admin-password"     = "the dashboard's admin user"
  }
}

resource "azurerm_key_vault" "main" {
  name                       = "kv-mi-${local.suffix}"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7
  purge_protection_enabled   = false
  tags                       = local.tags
}

# Whoever applies writes the secrets; the VM reads them
resource "azurerm_role_assignment" "kv_deployer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "kv_vm" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_linux_virtual_machine.main.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# A new role assignment takes a minute or so to reach Key Vault
resource "time_sleep" "kv_roles" {
  depends_on      = [azurerm_role_assignment.kv_deployer]
  create_duration = "60s"
}

resource "random_password" "secret" {
  for_each = local.secrets
  length   = 32
  special  = false
}

resource "azurerm_key_vault_secret" "main" {
  for_each     = local.secrets
  name         = each.key
  value        = random_password.secret[each.key].result
  key_vault_id = azurerm_key_vault.main.id
  content_type = each.value
  tags         = local.tags
  depends_on   = [time_sleep.kv_roles]
}
