# GitHub Actions' deploy workflow (.github/workflows/deploy.yml): an identity it signs in as
# with OIDC (no secret stored in GitHub), allowed only to run vm/deploy.sh on this VM.

resource "azurerm_user_assigned_identity" "github" {
  count               = var.github_repo == null ? 0 : 1
  name                = "id-${var.name}-github"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "github" {
  count                     = var.github_repo == null ? 0 : 1
  name                      = "github-main"
  user_assigned_identity_id = azurerm_user_assigned_identity.github[0].id
  issuer                    = "https://token.actions.githubusercontent.com"
  audience                  = ["api://AzureADTokenExchange"]
  subject                   = "repo:${var.github_repo}:ref:refs/heads/${var.git_ref}"
}

# Run commands on this VM (and nothing else)
resource "azurerm_role_assignment" "github_vm" {
  count                = var.github_repo == null ? 0 : 1
  scope                = azurerm_linux_virtual_machine.main.id
  role_definition_name = "Virtual Machine Contributor"
  principal_id         = azurerm_user_assigned_identity.github[0].principal_id
  principal_type       = "ServicePrincipal"
}
