# Grupo de operadores do laboratório com MENOR PRIVILÉGIO.
# Aplicado uma vez (ainda com admin); depois a AdministratorAccess é removida do usuário.
#
#   cd infra/operator-iam && terraform init && terraform apply
#   Console: IAM > Users > maikon-cli > Permissions > remover AdministratorAccess
#   Teste: terraform -chdir=../terraform plan  (precisa continuar funcionando)
#
# Além do escopo, a policy carrega GUARDRAILS DE CUSTO e SEGURANÇA que valem mesmo se
# o Terraform principal for alterado por engano:
#   - proibido abrir porta de entrada em Security Group
#   - proibido criar NAT Gateway
#   - EC2 só nos tipos aprovados e só em us-east-1
#   - anexar policy gerenciada a role só se for a do SSM (evita escalar para admin)
#
# Limitação conhecida: iam:PutRolePolicy nas roles do projeto ainda permitiria uma policy
# inline ampla. Em produção: permissions boundary obrigatório nas roles criadas pelo pipeline.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = var.project, managed_by = "terraform" }
  }
}

variable "project" {
  type    = string
  default = "autonomous-sre-agent"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "operator_user_name" {
  description = "Usuário IAM que opera o laboratório pelo terminal"
  type        = string
  default     = "maikon-cli"
}

variable "allowed_instance_types" {
  type    = list(string)
  default = ["c7i-flex.large", "m7i-flex.large", "t3.medium"]
}

data "aws_caller_identity" "current" {}

locals {
  acct   = data.aws_caller_identity.current.account_id
  region = var.region
  p      = var.project
}

# Exceção aceita (wildcards): leituras Describe/List e ações sem restrição por recurso precisam de "*";
# toda escrita é limitada por região, prefixo do projeto ou tag, e as negações abaixo vencem tudo.
#trivy:ignore:AVD-AWS-0057
data "aws_iam_policy_document" "operator" {
  # ---------- leitura (o Terraform precisa consultar o estado atual de tudo) ----------
  statement {
    sid = "ReadOnlyDiscovery"
    actions = [
      "ec2:Describe*", "iam:Get*", "iam:List*", "ecr:Describe*", "ecr:List*", "ecr:GetAuthorizationToken",
      "signer:Get*", "signer:List*", "ssm:Describe*", "ssm:Get*", "ssm:List*", "budgets:View*", "budgets:Describe*",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }

  # ---------- EC2 e rede: tudo, mas só na região do laboratório ----------
  statement {
    sid       = "Ec2InLabRegion"
    actions   = ["ec2:*"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }

  # ---------- IAM: só as roles e instance profiles do projeto ----------
  statement {
    sid     = "IamProjectRoles"
    actions = ["iam:*"]
    resources = [
      "arn:aws:iam::${local.acct}:role/${local.p}-*",
      "arn:aws:iam::${local.acct}:instance-profile/${local.p}-*",
      "arn:aws:iam::${local.acct}:oidc-provider/token.actions.githubusercontent.com",
    ]
  }
  statement {
    sid       = "PassNodeRoleToEc2Only"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${local.acct}:role/${local.p}-node"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  # ---------- registry, assinatura, configuração e custo do projeto ----------
  statement {
    sid       = "EcrProjectRepos"
    actions   = ["ecr:*"]
    resources = ["arn:aws:ecr:${local.region}:${local.acct}:repository/${local.p}/*"]
  }
  statement {
    sid       = "SignerProjectProfile"
    actions   = ["signer:*"]
    resources = ["arn:aws:signer:${local.region}:${local.acct}:/signing-profiles/${replace(local.p, "-", "_")}*"]
  }
  statement {
    sid       = "SsmProjectParameters"
    actions   = ["ssm:*"]
    resources = ["arn:aws:ssm:${local.region}:${local.acct}:parameter/${local.p}/*"]
  }
  statement {
    sid       = "BudgetsProject"
    actions   = ["budgets:*"]
    resources = ["arn:aws:budgets::${local.acct}:budget/${local.p}-*"]
  }

  # ---------- operação: comandos e sessões SSM só nas instâncias do projeto ----------
  statement {
    sid       = "SsmCommandsOnProjectInstances"
    actions   = ["ssm:SendCommand", "ssm:StartSession"]
    resources = ["arn:aws:ec2:${local.region}:${local.acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/project"
      values   = [local.p]
    }
  }
  statement {
    sid     = "SsmDocuments"
    actions = ["ssm:SendCommand", "ssm:StartSession"]
    resources = [
      "arn:aws:ssm:${local.region}::document/AWS-RunShellScript",
      "arn:aws:ssm:${local.region}::document/AWS-StartPortForwardingSession",
      "arn:aws:ssm:${local.region}:${local.acct}:document/SSM-SessionManagerRunShell",
    ]
  }
  statement {
    sid       = "SsmOwnSessions"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession"]
    resources = ["arn:aws:ssm:${local.region}:${local.acct}:session/$${aws:username}-*"]
  }
  statement {
    sid     = "BedrockAgentModel"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${local.region}:${local.acct}:inference-profile/us.amazon.nova-micro-v1:0",
      "arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0",
    ]
  }

  # ---------- GUARDRAILS: negações explícitas vencem qualquer permissão ----------
  statement {
    sid       = "DenyInboundPorts"
    effect    = "Deny"
    actions   = ["ec2:AuthorizeSecurityGroupIngress"]
    resources = ["*"]
  }
  statement {
    sid       = "DenyNatGateway"
    effect    = "Deny"
    actions   = ["ec2:CreateNatGateway"]
    resources = ["*"]
  }
  statement {
    sid       = "DenyUnapprovedInstanceTypes"
    effect    = "Deny"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:*:*:instance/*"]
    condition {
      test     = "StringNotEquals"
      variable = "ec2:InstanceType"
      values   = var.allowed_instance_types
    }
  }
  statement {
    sid       = "DenyManagedPolicyEscalation"
    effect    = "Deny"
    actions   = ["iam:AttachRolePolicy"]
    resources = ["*"]
    condition {
      test     = "ArnNotEquals"
      variable = "iam:PolicyARN"
      values   = ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    }
  }
}

resource "aws_iam_policy" "operator" {
  name        = "${local.p}-operator"
  description = "Operar o laboratorio ${local.p} com menor privilegio"
  policy      = data.aws_iam_policy_document.operator.json
}

resource "aws_iam_group" "operators" {
  name = "${local.p}-operators"
}

resource "aws_iam_group_policy_attachment" "operators" {
  group      = aws_iam_group.operators.name
  policy_arn = aws_iam_policy.operator.arn
}

resource "aws_iam_user_group_membership" "operator" {
  user   = var.operator_user_name
  groups = [aws_iam_group.operators.name]
}

output "next_step" {
  value = "Agora remova a AdministratorAccess do usuário ${var.operator_user_name} no console e rode terraform plan na stack principal para validar."
}
