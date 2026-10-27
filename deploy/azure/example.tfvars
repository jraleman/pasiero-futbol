# Copy to terraform.tfvars (git-ignored) and change what you need: terraform apply reads it.
subscription_id = "00000000-0000-0000-0000-000000000000" # az account show --query id -o tsv

# The VM clones and deploys this (public) repository; without it, deploy/azure/vm/push.sh
# copies your working tree instead
# repo_url    = "https://github.com/<you>/pasiero-futbol"
# github_repo = "<you>/pasiero-futbol"                       # the deploy workflow's identity (OIDC)

# location      = "uksouth"
# shutdown_time = "2300"                                # stop the VM every night (not while judging)
# ml            = false                                 # no Azure ML workspace
# budget        = 100                                   # a month, in your billing currency (null: none)
# budget_emails = ["you@example.com"]                   # besides the subscription's owners
