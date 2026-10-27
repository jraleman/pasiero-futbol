# Applies against mock providers: no Azure subscription needed.
#   terraform init -backend=false && terraform test

mock_provider "azurerm" {
  override_data {
    target = data.azurerm_client_config.current
    values = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      subscription_id = "00000000-0000-0000-0000-000000000002"
      object_id       = "00000000-0000-0000-0000-000000000003"
    }
  }
  # Mock values for the IDs the role assignments and secrets need in a valid form
  mock_resource "azurerm_key_vault" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv" }
  }
  mock_resource "azurerm_linux_virtual_machine" {
    defaults = {
      id       = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
      identity = { principal_id = "00000000-0000-0000-0000-0000000000aa" }
    }
  }
  mock_resource "azurerm_cognitive_account" {
    defaults = {
      id       = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai"
      endpoint = "https://oai-test.openai.azure.com/"
    }
  }
  mock_resource "azurerm_public_ip" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip", ip_address = "198.51.100.10" }
  }
  mock_resource "azurerm_resource_group" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg" }
  }
  mock_resource "azurerm_subnet" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/vm" }
  }
  mock_resource "azurerm_network_interface" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic" }
  }
  mock_resource "azurerm_network_security_group" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Network/networkSecurityGroups/nsg" }
  }
  mock_resource "azurerm_log_analytics_workspace" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/log" }
  }
  mock_resource "azurerm_application_insights" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Insights/components/appi" }
  }
  mock_resource "azurerm_storage_account" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st" }
  }
  mock_resource "azurerm_machine_learning_workspace" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.MachineLearningServices/workspaces/mlw" }
  }
  mock_resource "azurerm_user_assigned_identity" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id", principal_id = "00000000-0000-0000-0000-0000000000bb" }
  }
}

mock_provider "http" {
  override_data {
    target = data.http.my_ip
    values = { response_body = "203.0.113.7\n" }
  }
}

mock_provider "time" {
  mock_resource "time_static" {
    defaults = { rfc3339 = "2026-10-23T09:41:00Z" }
  }
}

variables {
  ssh_public_key_path = "tests/fixtures/id_test.pub"
}

run "defaults" {
  command = apply

  assert {
    condition     = azurerm_linux_virtual_machine.main.size == "Standard_E4s_v5" && azurerm_linux_virtual_machine.main.os_disk[0].disk_size_gb == 128 && azurerm_linux_virtual_machine.main.source_image_reference[0].sku == "server"
    error_message = "the nuc's host: Standard_E4s_v5, x86 Ubuntu, 128 GB"
  }
  assert {
    condition     = can(regex("^pasiero-futbol-[a-z0-9]{6}\\.uksouth\\.cloudapp\\.azure\\.com$", local.fqdn)) && azurerm_public_ip.vm.domain_name_label == local.label
    error_message = "address: ${local.fqdn}"
  }
  assert {
    condition = (length(azurerm_network_security_group.vm.security_rule) == 2 && anytrue([
      for r in azurerm_network_security_group.vm.security_rule : r.destination_port_range == "22" && r.source_address_prefix == "203.0.113.7/32"
      ]) && anytrue([
      for r in azurerm_network_security_group.vm.security_rule : toset(r.destination_port_ranges) == toset(["80", "443", "8443"])
    ]))
    error_message = "SSH from this machine only; the web on 80, 443 and 8443; nothing else"
  }
  assert {
    condition = alltrue([
      for line in ["FQDN=${local.fqdn}", "KEY_VAULT=${azurerm_key_vault.main.name}", "AZURE_OPENAI_ENDPOINT=https://oai-test.openai.azure.com/",
      "AZURE_OPENAI_DEPLOYMENT=gpt-4.1-mini", "GIT_REF=main", "pasiero-deploy"] : strcontains(local.cloud_init, line)
    ])
    error_message = "cloud-init should say where the VM is and what it uses, and run the deploy"
  }
  assert {
    condition = !anytrue([
      for k, p in random_password.secret : strcontains(local.cloud_init, p.result)
    ])
    error_message = "no secret may be in the VM's custom data (it's readable from the VM's metadata)"
  }
  assert {
    condition     = azurerm_key_vault.main.rbac_authorization_enabled && length(azurerm_key_vault_secret.main) == 10
    error_message = "the secrets in a Key Vault with RBAC"
  }
  assert {
    condition     = azurerm_role_assignment.kv_vm.role_definition_name == "Key Vault Secrets User" && azurerm_role_assignment.openai_vm.role_definition_name == "Cognitive Services OpenAI User"
    error_message = "the VM's identity reads the secrets and calls Azure OpenAI, nothing more"
  }
  assert {
    condition     = !azurerm_cognitive_account.openai.local_auth_enabled && azurerm_cognitive_deployment.chat.model[0].name == "gpt-4.1-mini" && azurerm_cognitive_deployment.chat.sku[0].name == "GlobalStandard"
    error_message = "Azure OpenAI without keys, gpt-4.1-mini on GlobalStandard"
  }
  assert {
    condition     = length(azurerm_machine_learning_workspace.main) == 1 && azurerm_machine_learning_compute_cluster.cpu[0].scale_settings[0].min_node_count == 0 && azurerm_machine_learning_compute_cluster.cpu[0].name == "cpu-cluster"
    error_message = "Azure ML with a cluster that scales to zero, named as job.yml expects"
  }
  assert {
    condition     = length(azurerm_dev_test_global_vm_shutdown_schedule.main) == 0 && length(azurerm_user_assigned_identity.github) == 0
    error_message = "no nightly shutdown and no GitHub identity unless asked"
  }
  assert {
    condition = (azurerm_consumption_budget_resource_group.main[0].amount == 100 && azurerm_consumption_budget_resource_group.main[0].time_grain == "Monthly"
      && azurerm_consumption_budget_resource_group.main[0].resource_group_id == azurerm_resource_group.main.id
    && azurerm_consumption_budget_resource_group.main[0].time_period[0].start_date == "2026-10-01T00:00:00Z")
    error_message = "a monthly budget of 100 on the resource group, from the 1st of the month it was made in"
  }
  assert {
    condition = (toset([for n in azurerm_consumption_budget_resource_group.main[0].notification : "${n.threshold_type} ${n.threshold}"])
      == toset(["Actual 50", "Actual 80", "Actual 100", "Forecasted 100"])
    && alltrue([for n in azurerm_consumption_budget_resource_group.main[0].notification : n.contact_roles == tolist(["Owner"])]))
    error_message = "the owners emailed at 50%, 80% and 100% spent, and on a forecast over 100%"
  }
}

run "options" {
  command = apply
  variables {
    ml              = false
    github_repo     = "someone/pasiero-futbol"
    shutdown_time   = "2300"
    dns_label       = "pl-match"
    ssh_source_cidr = "198.51.100.0/24"
    repo_url        = "https://github.com/someone/pasiero-futbol"
    budget          = null
  }

  assert {
    condition     = length(azurerm_machine_learning_workspace.main) == 0 && output.ml_workspace == null
    error_message = "ml = false: no Azure ML"
  }
  assert {
    condition     = azurerm_federated_identity_credential.github[0].subject == "repo:someone/pasiero-futbol:ref:refs/heads/main" && azurerm_role_assignment.github_vm[0].scope == azurerm_linux_virtual_machine.main.id
    error_message = "the workflow on main may run commands on this VM only"
  }
  assert {
    condition     = azurerm_dev_test_global_vm_shutdown_schedule.main[0].daily_recurrence_time == "2300"
    error_message = "the nightly shutdown"
  }
  assert {
    condition     = local.fqdn == "pl-match.uksouth.cloudapp.azure.com" && local.ssh_source == "198.51.100.0/24"
    error_message = "dns_label and ssh_source_cidr"
  }
  assert {
    condition     = strcontains(local.cloud_init, "REPO_URL=https://github.com/someone/pasiero-futbol")
    error_message = "the VM clones repo_url"
  }
  assert {
    condition     = length(azurerm_consumption_budget_resource_group.main) == 0
    error_message = "budget = null: no budget"
  }
}

run "budget" {
  command = plan
  variables {
    budget        = 250
    budget_emails = ["coach@example.com"]
  }

  assert {
    condition = (azurerm_consumption_budget_resource_group.main[0].amount == 250 && alltrue([
      for n in azurerm_consumption_budget_resource_group.main[0].notification : n.contact_emails == tolist(["coach@example.com"])
    ]))
    error_message = "budget and budget_emails"
  }
}

run "bad_budget" {
  command = plan
  variables {
    budget = 0
  }
  expect_failures = [var.budget]
}

run "bad_shutdown_time" {
  command = plan
  variables {
    shutdown_time = "25:00"
  }
  expect_failures = [var.shutdown_time]
}

run "bad_name" {
  command = plan
  variables {
    name = "Match_Intelligence"
  }
  expect_failures = [var.name]
}
