# A monthly budget on the resource group: everything here, Azure ML's own registry included.
# Pay-as-you-go has no spending limit, so it emails before the bill surprises anyone: at 50%,
# 80% and 100% of what's been spent, and when the month's forecast passes 100%. It only
# warns; nothing is stopped. The amount is in the subscription's billing currency.

# The month the budget starts in, fixed at the first apply (a budget starts on the 1st)
resource "time_static" "budget" {
  count = var.budget == null ? 0 : 1
}

resource "azurerm_consumption_budget_resource_group" "main" {
  count             = var.budget == null ? 0 : 1
  name              = "budget-${var.name}"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.budget
  time_grain        = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", time_static.budget[0].rfc3339)
  }

  dynamic "notification" {
    for_each = { "50" = "Actual", "80" = "Actual", "100" = "Actual", "forecast" = "Forecasted" }
    content {
      operator       = "GreaterThan"
      threshold      = notification.key == "forecast" ? 100 : tonumber(notification.key)
      threshold_type = notification.value
      contact_roles  = ["Owner"]
      contact_emails = var.budget_emails
    }
  }
}
