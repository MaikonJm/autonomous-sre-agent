# ============================================================ Cadeia de suprimentos de software
# Build no GitHub Actions -> ECR -> assinatura com AWS Signer (Notation, certificado gerenciado)
# -> o nó VERIFICA a assinatura antes de fazer deploy. Imagem sem assinatura válida não roda.
# Equivalente no Azure: ACR + Notation com certificado no Key Vault + Ratify/Azure Policy no AKS.

variable "github_repository" {
  description = "Repositório (owner/nome) autorizado a publicar e assinar imagens"
  type        = string
  default     = "MaikonJm/autonomous-sre-agent"
}

variable "create_github_oidc_provider" {
  description = "false se a conta já tem o provider OIDC do GitHub (só pode existir um por conta)"
  type        = bool
  default     = false
}

locals {
  apps = toset(["orders-api", "sre-agent"])
}

# ---------- Registry ----------
resource "aws_ecr_repository" "app" {
  for_each             = local.apps
  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "IMMUTABLE" # uma tag nunca aponta para outra imagem depois de publicada
  force_delete         = true        # laboratório: destroy apaga as imagens junto

  image_scanning_configuration {
    scan_on_push = true # scan de CVEs do próprio ECR a cada push
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Manter só as 10 imagens mais recentes (custo)"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---------- Assinatura: o AWS Signer gerencia chave, certificado e PKI ----------
resource "aws_signer_signing_profile" "images" {
  name        = replace(var.project, "-", "_") # nomes de signing profile não aceitam hífen
  platform_id = "Notation-OCI-SHA384-ECDSA"
}

# ---------- Identidade do pipeline: GitHub Actions via OIDC, sem chave da AWS no GitHub ----------
resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_github_oidc_provider ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  github_oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "release_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Só a branch main DESTE repositório publica e assina
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "release" {
  name                 = "${var.project}-github-release"
  assume_role_policy   = data.aws_iam_policy_document.release_trust.json
  max_session_duration = 3600
}

# Exceção aceita: ecr:GetAuthorizationToken e signer:GetRevocationStatus não aceitam restrição por
# recurso na AWS (só "*"). Todas as outras ações estão limitadas aos repositórios e ao signing profile.
#trivy:ignore:AVD-AWS-0057
data "aws_iam_policy_document" "release" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
      "ecr:DescribeImages",
    ]
    resources = [for r in aws_ecr_repository.app : r.arn]
  }
  statement {
    sid       = "SignWithProfile"
    actions   = ["signer:SignPayload", "signer:GetSigningProfile"]
    resources = [aws_signer_signing_profile.images.arn]
  }
  statement {
    sid       = "CheckRevocation"
    actions   = ["signer:GetRevocationStatus"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "release" {
  name   = "publish-and-sign"
  role   = aws_iam_role.release.id
  policy = data.aws_iam_policy_document.release.json
}

# ---------- O nó: só LÊ imagens e verifica assinaturas (não publica nem assina) ----------
# Exceção aceita: ecr:GetAuthorizationToken e signer:GetRevocationStatus não aceitam restrição por
# recurso na AWS (só "*"). Todas as outras ações estão limitadas aos repositórios e ao signing profile.
#trivy:ignore:AVD-AWS-0057
data "aws_iam_policy_document" "node_supply_chain" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages", "ecr:BatchCheckLayerAvailability"]
    resources = [for r in aws_ecr_repository.app : r.arn]
  }
  statement {
    sid       = "VerifySignatures"
    actions   = ["signer:GetRevocationStatus"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadLabConfig"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/*"]
  }
}

resource "aws_iam_role_policy" "node_supply_chain" {
  name   = "pull-and-verify-images"
  role   = aws_iam_role.node.id
  policy = data.aws_iam_policy_document.node_supply_chain.json
}

# ---------- Configuração que o nó lê (sem recriar a EC2) ----------
resource "aws_ssm_parameter" "ecr_registry" {
  name  = "/${var.project}/ecr-registry"
  type  = "String"
  value = split("/", aws_ecr_repository.app["orders-api"].repository_url)[0]
}

resource "aws_ssm_parameter" "signing_profile_arn" {
  name  = "/${var.project}/signing-profile-arn"
  type  = "String"
  value = aws_signer_signing_profile.images.arn
}

output "github_release_role_arn" {
  description = "Variável AWS_RELEASE_ROLE_ARN do repositório no GitHub"
  value       = aws_iam_role.release.arn
}

output "signing_profile_arn" {
  description = "Variável SIGNING_PROFILE_ARN do repositório no GitHub"
  value       = aws_signer_signing_profile.images.arn
}

output "ecr_registry" {
  value = aws_ssm_parameter.ecr_registry.value
}
