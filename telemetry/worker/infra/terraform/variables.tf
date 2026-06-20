variable "cloudflare_api_token" {
  description = "Cloudflare API token. Required scopes: Account Workers Scripts Edit, Workers KV Storage Edit, Account Settings Read."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID. Pass via terraform.tfvars or TF_VAR_cloudflare_account_id."
  type        = string
}

variable "worker_name" {
  description = "Cloudflare Worker script name."
  type        = string
  default     = "alfred-proof-telemetry"
}

variable "allowed_origin" {
  description = "Browser origin allowed to read GET /stats."
  type        = string
  default     = "https://alfred.luminik.io"
}

variable "require_install_token" {
  description = "Require a per-install token issued by POST /register before POST /ingest accepts reports."
  type        = bool
  default     = true
}

variable "trusted_counts_only" {
  description = "When true, anonymous installs count as active installs but only trusted reporters can move PR/issue/file/line totals."
  type        = bool
  default     = true
}

variable "trusted_ingest_token" {
  description = "Private token accepted as X-Alfred-Trusted-Token for count-bearing hosted reports."
  type        = string
  sensitive   = true
}

variable "rate_limit_salt" {
  description = "Secret salt for keyed per-IP rate-limit buckets. Generate a long random value."
  type        = string
  sensitive   = true
}

variable "stats_cache_ttl_seconds" {
  description = "GET /stats cache TTL in seconds. Hosted default is 300 to keep KV list usage free-tier friendly; 0 disables the cache."
  type        = number
  default     = 300
}
