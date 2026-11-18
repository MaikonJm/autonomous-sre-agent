terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # State local: laboratório de uma pessoa. Em equipe: backend "s3" com lock
  # (use_lockfile = true) e versionamento no bucket.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = var.project, managed_by = "terraform", owner = "maikon" }
  }
}

data "aws_caller_identity" "current" {}

# Zonas que oferecem o tipo de instância escolhido (c7i-flex não existe em todas)
data "aws_ec2_instance_type_offerings" "this" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

# Ubuntu 24.04 LTS oficial da Canonical (parâmetro público do SSM)
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

# ============================================================ Rede
# VPC própria e mínima: 1 subnet pública, sem NAT Gateway (que custaria ~US$ 32/mês).
resource "aws_vpc" "this" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.project }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.project }
}

# Exceção aceita (Semgrep/Trivy): IP público sem NAT Gateway. Nenhuma porta de entrada aberta (SG sem ingress).
resource "aws_subnet" "public" { # nosemgrep: terraform.aws.security.aws-subnet-has-public-ip-address.aws-subnet-has-public-ip-address
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = sort(data.aws_ec2_instance_type_offerings.this.locations)[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.project}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Nenhuma porta de entrada aberta: acesso só via SSM Session Manager.
# Saída liberada: pacotes, imagens de container, APIs da AWS (Bedrock, SSM).
resource "aws_security_group" "node" {
  name        = "${var.project}-node"
  description = "k3s node: no inbound, SSM only"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.project}-node" }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.node.id
  description       = "Outbound to packages, images and AWS APIs"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ============================================================ Identidade do nó
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.project}-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

# Session Manager em vez de SSH: sem chave, sem porta 22, com log de sessão
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Menor privilégio: invocar UM modelo do Bedrock e nada mais.
# O perfil de inferência "us." pode rotear a chamada para qualquer região dos EUA.
data "aws_iam_policy_document" "bedrock" {
  statement {
    sid     = "InvokeAgentModel"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.${var.bedrock_model_id}",
      "arn:aws:bedrock:*::foundation-model/${var.bedrock_model_id}",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "invoke-agent-model"
  role   = aws_iam_role.node.id
  policy = data.aws_iam_policy_document.bedrock.json
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.project}-node"
  role = aws_iam_role.node.name
}

# ============================================================ O nó (k3s + todo o laboratório)
resource "aws_instance" "node" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.node.id]
  iam_instance_profile   = aws_iam_instance_profile.node.name

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    k3s_version = var.k3s_version
    repo_url    = var.repo_url
    auto_deploy = var.auto_deploy
  })
  user_data_replace_on_change = true

  # IMDSv2 obrigatório; hop limit 2 para os pods alcançarem a credencial do nó
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "${var.project}-k3s" }

  lifecycle {
    ignore_changes = [ami] # AMI nova não recria o laboratório de surpresa
  }
}

# ============================================================ Guarda de custo
resource "aws_budgets_budget" "lab" {
  count        = var.alert_email == "" ? 0 : 1
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
