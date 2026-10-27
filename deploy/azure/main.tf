# Pasiero Futbol on Azure: the whole stack on one VM, under p4n4-emu's nuc profile (the
# club's device), behind HTTPS; its secrets in Key Vault; the agents on Azure OpenAI by the
# VM's managed identity; the shirt-number model trained on Azure ML. README.md.

locals {
  suffix = random_string.suffix.result
  label  = coalesce(var.dns_label, "${var.name}-${local.suffix}")
  fqdn   = "${local.label}.${var.location}.cloudapp.azure.com"
  tags   = { app = "pasiero-futbol", data = "synthetic" }

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    site = {
      FQDN                    = local.fqdn
      KEY_VAULT               = azurerm_key_vault.main.name
      AZURE_OPENAI_ENDPOINT   = azurerm_cognitive_account.openai.endpoint
      AZURE_OPENAI_DEPLOYMENT = azurerm_cognitive_deployment.chat.name
      REPO_URL                = var.repo_url == null ? "" : var.repo_url
      GIT_REF                 = var.git_ref
      DASHBOARD_REF           = var.p4n4_refs.dashboard
      P4N4_API_VERSION        = var.p4n4_refs.api
    }
    refs = var.p4n4_refs
  })

  # The lookup only exists without ssh_source_cidr, so only that branch may read it
  ssh_source = var.ssh_source_cidr != null ? var.ssh_source_cidr : "${chomp(data.http.my_ip[0].response_body)}/32"
}

data "azurerm_client_config" "current" {}

data "http" "my_ip" {
  count = var.ssh_source_cidr == null ? 1 : 0
  url   = "https://ipv4.icanhazip.com"
}

# Globally unique names (Key Vault, storage, the Azure OpenAI subdomain, the address)
resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${var.name}"
  location = var.location
  tags     = local.tags
}

# ── Network: the web on 80, 443 and 8443; SSH from one address ─────────────────

resource "azurerm_virtual_network" "main" {
  name                = "vnet-${var.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  address_space       = ["10.42.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "vm" {
  name                 = "vm"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.42.0.0/24"]
}

resource "azurerm_public_ip" "vm" {
  name                = "pip-${var.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"
  allocation_method   = "Static"
  domain_name_label   = local.label
  tags                = local.tags
}

resource "azurerm_network_security_group" "vm" {
  name                = "nsg-${var.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags

  security_rule {
    name                       = "ssh-from-me"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = local.ssh_source
    destination_address_prefix = "*"
  }

  # Caddy: 80 for Let's Encrypt and the redirect, 443 the dashboard, 8443 the analysis room.
  # Everything else the stack publishes (the agent, n8n, InfluxDB, MQTT…) stays unreachable.
  security_rule {
    name                       = "web"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443", "8443"]
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_interface" "vm" {
  name                = "nic-${var.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = azurerm_subnet.vm.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.vm.id
  }
}

resource "azurerm_network_interface_security_group_association" "vm" {
  network_interface_id      = azurerm_network_interface.vm.id
  network_security_group_id = azurerm_network_security_group.vm.id
}

# ── The VM ──────────────────────────────────────────────────────────────────────

resource "azurerm_linux_virtual_machine" "main" {
  name                  = "vm-${var.name}"
  resource_group_name   = azurerm_resource_group.main.name
  location              = azurerm_resource_group.main.location
  size                  = var.size
  network_interface_ids = [azurerm_network_interface.vm.id]
  tags                  = local.tags

  # uid 1000: the stacks' DATA_UID / DATA_GID default
  admin_username = "p4n4"
  admin_ssh_key {
    username   = "p4n4"
    public_key = file(pathexpand(var.ssh_public_key_path))
  }

  custom_data = base64encode(local.cloud_init)

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = var.os_disk_gb
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  # Key Vault's secrets and Azure OpenAI, without a key on the disk
  identity {
    type = "SystemAssigned"
  }

  # A new cloud-init only matters to a new VM: redeploys go through vm/deploy.sh
  lifecycle {
    ignore_changes = [custom_data]
  }
}

resource "azurerm_dev_test_global_vm_shutdown_schedule" "main" {
  count                 = var.shutdown_time == null ? 0 : 1
  virtual_machine_id    = azurerm_linux_virtual_machine.main.id
  location              = azurerm_resource_group.main.location
  enabled               = true
  daily_recurrence_time = var.shutdown_time
  timezone              = var.shutdown_timezone
  tags                  = local.tags

  notification_settings {
    enabled = false
  }
}
