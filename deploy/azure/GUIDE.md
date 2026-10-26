# Deploying to Azure, step by step

From an empty Azure subscription to Match Intelligence on its own HTTPS address, then the shirt-number model trained on Azure Machine Learning, then everything removed again. It takes about an hour, most of it waiting.

[README.md](README.md) explains what each piece does; [REPORT.md](REPORT.md) explains why steps 1 to 3 are here: a new subscription hits each of them.

Commands run from the repository's root unless they say otherwise. `SUB` is your subscription's id throughout.

## 0. What you need

- An Azure account, and the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), signed in: `az login`.
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.10 or newer.
- An SSH key: `~/.ssh/id_ed25519.pub` (`ssh-keygen -t ed25519` if you have none).
- `rsync`, `jq` and `curl`.

## 1. A pay-as-you-go subscription

A Free Trial subscription is disabled when its credit or its 30 days run out, and then everything fails as `ReadOnlyDisabledSubscription`. Upgrade it in the portal (**Subscriptions → your subscription → Upgrade**). Then check that Azure agrees. For a few minutes after an upgrade, some answers still say *Disabled*: wait until several in a row say `Enabled PayAsYouGo_2014-09-01`.

```bash
SUB=$(az account show --query id -o tsv)        # or the id of the subscription to use
for i in 1 2 3 4 5; do
  az rest --method get --url "https://management.azure.com/subscriptions/$SUB?api-version=2022-12-01" \
    --query "[state, subscriptionPolicies.quotaId]" -o tsv | paste -sd' '
done
```

## 2. Register the resource providers

A new subscription has none registered, and Azure won't show your VM quota until `Microsoft.Compute` is. Each one takes a minute or two:

```bash
for ns in CognitiveServices Compute Consumption ContainerRegistry DevTestLab Insights KeyVault \
          MachineLearningServices ManagedIdentity Network OperationalInsights Storage; do
  az provider register -n Microsoft.$ns --subscription $SUB --wait && echo "Microsoft.$ns registered"
done
```

## 3. Choose the region and sizes

The VM needs 4 vCPUs and 32 GB, and Azure ML's cluster 8 vCPUs. Whether a size works depends on two things, and a new subscription often fails one of them: the size must be **offered** to your subscription in that region, and you need **quota** for its family there. Azure ML keeps a quota of its own, separate from the VMs'.

```bash
REGION=swedencentral

# Offered? "ok", or why not (NotAvailableForSubscription: not in this region, for this subscription)
az vm list-skus -l $REGION --resource-type virtualMachines --subscription $SUB -o json |
  jq -r '.[] | select(.name|test("^Standard_(E4s_v5|E4as_v5|F8s_v2|F8as_v6|D8s_v5)$"))
         | "\(.name)\t\((.restrictions|map(.reasonCode)|join(",")) // "" | if .=="" then "ok" else . end)"'

# The VM's quota (vCPUs per family)
az vm list-usage -l $REGION --subscription $SUB -o json |
  jq -r '.[] | select(.name.localizedValue|test("ESv5|EASv5|Total Regional")) | "\(.limit)\t\(.name.localizedValue)"'

# Azure ML's quota (its own, per family)
az rest --method get --subscription $SUB \
  --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.MachineLearningServices/locations/$REGION/usages?api-version=2024-04-01" -o json |
  jq -r '.value[] | select(.name.value|test("Dedicated")) | select(.limit > 0) | "\(.limit)\t\(.name.localizedValue)"'

# Azure OpenAI: the model, and its tokens-a-minute quota (the deployment takes 100)
az cognitiveservices usage list -l $REGION --subscription $SUB --query "[?name.value=='OpenAI.GlobalStandard.gpt4.1-mini'].[limit]" -o tsv
```

For this project's subscription in October 2026, **Sweden Central** was the one that fits:
- `E4s_v5` is offered there, at $0.268 an hour;
- `D8s_v5` is offered for Azure ML's cluster, at $0.408 an hour;
- `gpt-4.1-mini` has 200k tokens a minute.

Both sizes still needed quota: 0 vCPUs of ESv5 for the VM, and Azure ML had quota only for families that aren't offered there. UK South, the default, offers no E4 size to a new subscription.

**If a family's quota is 0**, ask for more. It's free, and a small increase is usually approved within minutes:
- In the portal, open **Quotas → Compute**, filter by your region and the family (*Standard ESv5 Family vCPUs*), select it, then **New quota request**: 8 vCPUs leaves room to resize.
- For Azure ML, do the same under **Quotas → Azure Machine Learning**: the region, then *Standard DSv5 Family Cluster Dedicated vCPUs* (for `D8s_v5`), 8 vCPUs. Without it, everything but the cluster deploys, and `aml.sh` fails until it's granted.

## 4. Configure

```bash
cd deploy/azure
cp example.tfvars terraform.tfvars        # git-ignored: it never leaves your machine
```

Edit `terraform.tfvars`:

```hcl
subscription_id = "<SUB>"
location        = "swedencentral"         # the region from step 3
size            = "Standard_E4s_v5"       # a 4-vCPU, 32 GB size offered there, with quota
ml_cluster_size = "Standard_D8s_v5"       # an 8-vCPU size offered there, with Azure ML quota
budget          = 150                     # a month, in your billing currency: the VM alone is about 7 a day
budget_emails   = ["you@example.com"]     # besides the subscription's owners

# Once the repository is public: the VM clones it, and the deploy workflow can redeploy it
# repo_url    = "https://github.com/<owner>/<repo>"
# github_repo = "<owner>/<repo>"

# shutdown_time = "2300"                  # stop the VM every night (not while it's being judged)
```

Leave `openai_keys` off. The VM signs in to Azure OpenAI with its managed identity; turn keys on only to point your own machine at the same deployment ([README.md](README.md), *On your own machine*).

## 5. Create it

```bash
terraform init
terraform plan -out tfplan                # read it: 45 to add, all in rg-match-intelligence
terraform apply tfplan                    # about 10 minutes
rm tfplan
```

This creates:
- the resource group;
- the network, with SSH open to your address only;
- the VM, with its managed identity;
- Key Vault and its ten secrets;
- Azure OpenAI and the `gpt-4.1-mini` deployment;
- the Azure ML workspace and its cluster;
- the budget;
- the GitHub identity, if `github_repo` is set.

On first boot the VM installs Docker, uv and p4n4-emu.

If `apply` stops with a quota or `SkuNotAvailable` error, go back to step 3 for that size and run `apply` again: it carries on from where it stopped.

## 6. Deploy the stack

**With `repo_url`:** the VM clones the repository and deploys itself. Skip to following the log.

**Without it** (the repository not public yet), copy your working tree to the VM once it's up, about 3 minutes after `apply`:

```bash
deploy/azure/vm/push.sh                   # rsync to the VM, then bh-deploy there
```

Follow the deploy log. The first deploy takes about 20 minutes: images, the dashboard's Flutter build, and Ollama's 4.6 GB fallback model.

```bash
terraform -chdir=deploy/azure output -raw progress | sh     # Ctrl-C once it says "Up" and lists the addresses
```

## 7. Check it

```bash
terraform -chdir=deploy/azure output urls
terraform -chdir=deploy/azure output -json judge_login
```

| Open | Expect |
|---|---|
| `https://<fqdn>/` | A valid certificate (Let's Encrypt). The browser's login (`judge`), then the dashboard in the Premier League theme; sign in as `judge` again |
| The dashboard's **Cameras** | The five loops playing, with the players' boxes; `curl -I https://<fqdn>/cameras/tv.mp4` without the login: `401` |
| `https://<fqdn>:8443/analysis/` | The analysis room after any name and `analysis_token`: a live match at the Amex, *Demo data* in the header |
| `https://<fqdn>/grafana/` | The *Match analysis* dashboard, behind the browser's login only (Grafana's viewer is anonymous) |
| `https://<fqdn>:8443/mail/` | The inbox, as `judge`: the half-time and match reports arrive as the match plays |
| The dashboard's **Assistant** | An answer that streams the team's tool calls. The agent's log should say Azure OpenAI, not Ollama (below) |

```bash
ip=$(terraform -chdir=deploy/azure output -raw connect | sed 's/^ssh //')
ssh $ip 'docker logs p4n4-ollama 2>&1 | grep "team:"'      # "team: orchestrator, … on Azure OpenAI gpt-4.1-mini at https://oai-…"
ssh $ip 'docker stats --no-stream'                          # every container within the nuc profile's limits
```

If the team log says *Azure OpenAI isn't set*, the VM's `.env` didn't get the endpoint: run `bh-deploy` again (step 9). If answers come from Ollama, the identity's role may still be propagating (up to 10 minutes after `apply`). Look for the reason in `docker logs p4n4-ollama`.

## 8. Train the shirt-number model on Azure ML

```bash
EPOCHS=2 PER_EPOCH=2000 ai/vision/shirts/aml.sh     # first, a few minutes, to try the setup
ai/vision/shirts/aml.sh                            # the full run: about 2½ hours, about $1
```

`aml.sh`:
1. submits the job and prints its Azure ML Studio link;
2. streams its log;
3. registers the model as `shirt-numbers`;
4. downloads it to `ai/vision/models/shirt_numbers.onnx`.

The first job also builds the training environment's image, which takes about 15 minutes more, and creates the workspace's container registry. Studio shows each epoch's read, misread and invented rates.

To check it against the vision-language model on real footage, see [ai/vision/shirts/README.md](../../ai/vision/shirts/README.md), *Is it better? On footage*.

## 9. Redeploy after a change

Any one of these:

```bash
deploy/azure/vm/push.sh                                     # your working tree, before pushing
terraform -chdir=deploy/azure output -raw redeploy | sh     # pulls the branch on the VM, no SSH
```

Once the repository is public, with `github_repo` set and `terraform output github`'s values added as the repository's Actions *variables*, every push to `main` that passes CI redeploys by itself.

## 10. Save money, then take it all down

```bash
az vm deallocate -g rg-match-intelligence -n vm-match-intelligence   # stopped: only the disk and IP, about 1 a day
az vm start      -g rg-match-intelligence -n vm-match-intelligence   # back in about 2 minutes, same address

cd deploy/azure && terraform destroy      # everything above, about 10 minutes
```

`destroy` purges Key Vault and Azure OpenAI, so their names can be used again right away. The resource providers stay registered (they cost nothing). Afterwards, check that nothing is left: `az group show -n rg-match-intelligence` should say it doesn't exist.
