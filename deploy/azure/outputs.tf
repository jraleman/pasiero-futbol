output "fqdn" {
  value = local.fqdn
}

output "urls" {
  description = "What the judges open (once vm/deploy.sh has finished: terraform output -raw progress)."
  value = {
    dashboard     = "https://${local.fqdn}/"
    analysis_room = "https://${local.fqdn}:8443/analysis/"
    match_story   = "https://${local.fqdn}:8443/analysis/#story-card"
    inbox         = "https://${local.fqdn}:8443/mail/"
    grafana       = "https://${local.fqdn}/grafana/"
  }
}

output "judge_login" {
  description = "The dashboard (user judge) and the inbox; the analysis room's token is ingest-read-token."
  sensitive   = true
  value = {
    dashboard_user     = "judge"
    password           = random_password.secret["judge-password"].result
    analysis_room_name = "any name"
    analysis_token     = random_password.secret["ingest-read-token"].result
  }
}

output "connect" {
  value = "ssh p4n4@${azurerm_public_ip.vm.ip_address}"
}

output "progress" {
  description = "The first deploy's log (about 20 minutes: Docker, the images, the dashboard's build, Ollama's model)."
  value       = "ssh p4n4@${azurerm_public_ip.vm.ip_address} tail -f /var/log/match-intelligence.log"
}

output "redeploy" {
  description = "Pull the branch on the VM and deploy it again, without SSH."
  value       = "az vm run-command invoke -g ${azurerm_resource_group.main.name} -n ${azurerm_linux_virtual_machine.main.name} --command-id RunShellScript --scripts 'sudo -iu p4n4 bh-deploy'"
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "vm" {
  value = azurerm_linux_virtual_machine.main.name
}

output "key_vault" {
  value = azurerm_key_vault.main.name
}

output "openai" {
  description = "For ai/.env on your own machine, with a key (openai_keys = true)."
  value = {
    AZURE_OPENAI_ENDPOINT   = azurerm_cognitive_account.openai.endpoint
    AZURE_OPENAI_DEPLOYMENT = azurerm_cognitive_deployment.chat.name
  }
}

output "ml_workspace" {
  value = var.ml ? azurerm_machine_learning_workspace.main[0].name : null
}

output "github" {
  description = "The deploy workflow's repository variables (Settings → Secrets and variables → Actions → Variables)."
  value = var.github_repo == null ? null : {
    AZURE_CLIENT_ID       = azurerm_user_assigned_identity.github[0].client_id
    AZURE_TENANT_ID       = data.azurerm_client_config.current.tenant_id
    AZURE_SUBSCRIPTION_ID = data.azurerm_client_config.current.subscription_id
    AZURE_RESOURCE_GROUP  = azurerm_resource_group.main.name
    AZURE_VM              = azurerm_linux_virtual_machine.main.name
  }
}
