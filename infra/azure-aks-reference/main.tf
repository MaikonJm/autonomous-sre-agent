terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.0" }
  }
  # State local de propósito: laboratório efêmero, criado e destruído na mesma sessão.
  # Em produção: backend "azurerm" com lock (ver projeto sre-platform-demo).
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

provider "aws" {
  region = var.aws_region
}

variable "subscription_id" {
  description = "az account show --query id -o tsv"
  type        = string
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "node_vm_size" {
  description = "2 vCPU / 8 GB. Cabe na quota de 4 vCPUs da conta Free."
  type        = string
  default     = "Standard_B2ms"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "bedrock_model_id" {
  type    = string
  default = "amazon.nova-micro-v1:0"
}

locals {
  name = "sreagent"
  tags = { project = "azure-sre-ai-agent", ephemeral = "true", owner = "maikon" }
}

resource "random_string" "suffix" {
  length  = 5
  special = false
  upper   = false
}

# ============================================================ Azure
resource "azurerm_resource_group" "this" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

resource "azurerm_container_registry" "this" {
  name                = "acr${local.name}${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Basic" # ~US$0,17/dia
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = "aks-${local.name}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = local.name
  sku_tier            = "Free" # control plane sem custo

  default_node_pool {
    name            = "system"
    vm_size         = var.node_vm_size
    node_count      = 1 # laboratório: 1 nó. Produção: >= 3 nós em 3 zonas
    os_disk_size_gb = 64
    upgrade_settings { max_surge = "10%" }
  }

  identity { type = "SystemAssigned" }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
  }

  # Emissor OIDC público: a AWS valida os tokens dos service accounts por ele
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  tags = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                            = azurerm_container_registry.this.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

# ============================================================ AWS (federação cross-cloud)
data "aws_caller_identity" "current" {}

data "tls_certificate" "aks_oidc" {
  url = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

# A AWS confia nos tokens emitidos pelo AKS (sem access key estática no cluster)
resource "aws_iam_openid_connect_provider" "aks" {
  url             = azurerm_kubernetes_cluster.this.oidc_issuer_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.aks_oidc.certificates[0].sha1_fingerprint]
  tags            = local.tags
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.aks.arn]
    }
    # Só o service account sre-agent/sre-agent DESTE cluster pode assumir a role.
    # A chave usa a URL como a AWS a armazena (evita o problema da barra final).
    condition {
      test     = "StringEquals"
      variable = "${aws_iam_openid_connect_provider.aks.url}:sub"
      values   = ["system:serviceaccount:sre-agent:sre-agent"]
    }
    condition {
      test     = "StringEquals"
      variable = "${aws_iam_openid_connect_provider.aks.url}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "agent" {
  name                 = "aks-sre-agent-bedrock"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
  tags                 = local.tags
}

# Menor privilégio: invocar UM modelo e nada mais
data "aws_iam_policy_document" "bedrock" {
  statement {
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.${var.bedrock_model_id}",
      "arn:aws:bedrock:*::foundation-model/${var.bedrock_model_id}",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "invoke-agent-model"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.bedrock.json
}

# ============================================================ outputs
output "resource_group" { value = azurerm_resource_group.this.name }
output "aks_name" { value = azurerm_kubernetes_cluster.this.name }
output "acr_login_server" { value = azurerm_container_registry.this.login_server }
output "acr_name" { value = azurerm_container_registry.this.name }
output "aws_role_arn" { value = aws_iam_role.agent.arn }
output "oidc_issuer_url" { value = azurerm_kubernetes_cluster.this.oidc_issuer_url }
