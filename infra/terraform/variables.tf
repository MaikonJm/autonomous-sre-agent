variable "project" {
  description = "Prefixo dos recursos"
  type        = string
  default     = "autonomous-sre-agent"
}

variable "region" {
  description = "Região AWS (precisa ter o Bedrock Nova Micro)"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "2 vCPU / 4 GB: cabe k3s + Prometheus + app + agente. Tipos elegíveis ao Free plan."
  type        = string
  default     = "c7i-flex.large"

  validation {
    condition     = contains(["c7i-flex.large", "m7i-flex.large", "t3.medium"], var.instance_type)
    error_message = "Use c7i-flex.large (padrão), m7i-flex.large ou t3.medium: com menos de 4 GB o Prometheus não cabe."
  }
}

variable "k3s_version" {
  description = "Versão do k3s (fixa para o laboratório ser reproduzível)"
  type        = string
  default     = "v1.31.4+k3s1"
}

variable "repo_url" {
  description = "Repositório público clonado no nó no primeiro boot"
  type        = string
}

variable "auto_deploy" {
  description = "Se true, o primeiro boot já sobe Prometheus, app e agente (~10 min depois do apply)"
  type        = bool
  default     = true
}

variable "bedrock_model_id" {
  description = "Modelo usado pelo agente (via perfil de inferência us.)"
  type        = string
  default     = "amazon.nova-micro-v1:0"
}

variable "alert_email" {
  description = "E-mail para alertas de orçamento (vazio = sem budget)"
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Limite mensal do budget de alerta"
  type        = number
  default     = 10
}
