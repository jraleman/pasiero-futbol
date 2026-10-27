# Azure Machine Learning: the workspace and the CPU cluster that train the shirt-number model
# on synthetic players (ai/vision/shirts/, job.yml). The cluster scales to zero between jobs,
# so it costs nothing while idle. The workspace keeps its own key vault (Azure ML manages it);
# it makes its image registry itself on the first job.

resource "azurerm_log_analytics_workspace" "ml" {
  count               = var.ml ? 1 : 0
  name                = "log-${var.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

resource "azurerm_application_insights" "ml" {
  count               = var.ml ? 1 : 0
  name                = "appi-${var.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  workspace_id        = azurerm_log_analytics_workspace.ml[0].id
  application_type    = "web"
  tags                = local.tags
}

resource "azurerm_key_vault" "ml" {
  count                      = var.ml ? 1 : 0
  name                       = "kv-ml-${local.suffix}"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  soft_delete_retention_days = 7
  purge_protection_enabled   = false
  tags                       = local.tags
}

resource "azurerm_storage_account" "ml" {
  count                           = var.ml ? 1 : 0
  name                            = "stml${local.suffix}"
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_machine_learning_workspace" "main" {
  count                   = var.ml ? 1 : 0
  name                    = "mlw-${var.name}"
  resource_group_name     = azurerm_resource_group.main.name
  location                = azurerm_resource_group.main.location
  application_insights_id = azurerm_application_insights.ml[0].id
  key_vault_id            = azurerm_key_vault.ml[0].id
  storage_account_id      = azurerm_storage_account.ml[0].id
  # The job's environment image is built on the cluster
  image_build_compute_name = "cpu-cluster"
  description              = "Pasiero Futbol: the shirt-number model"
  tags                     = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_machine_learning_compute_cluster" "cpu" {
  count                         = var.ml ? 1 : 0
  name                          = "cpu-cluster"
  machine_learning_workspace_id = azurerm_machine_learning_workspace.main[0].id
  location                      = azurerm_resource_group.main.location
  vm_size                       = var.ml_cluster_size
  vm_priority                   = "Dedicated"
  tags                          = local.tags

  scale_settings {
    min_node_count                       = 0
    max_node_count                       = var.ml_max_nodes
    scale_down_nodes_after_idle_duration = "PT10M"
  }

  identity {
    type = "SystemAssigned"
  }
}
