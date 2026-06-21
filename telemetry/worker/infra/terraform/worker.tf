resource "cloudflare_workers_kv_namespace" "telemetry" {
  account_id = var.cloudflare_account_id
  title      = "${var.worker_name}-kv"
}

resource "cloudflare_worker" "telemetry" {
  account_id = var.cloudflare_account_id
  name       = var.worker_name

  observability = {
    enabled = true
  }
}

resource "cloudflare_worker_version" "telemetry" {
  account_id         = var.cloudflare_account_id
  worker_id          = cloudflare_worker.telemetry.id
  compatibility_date = "2024-11-01"
  main_module        = "worker.js"

  modules = [
    {
      name         = "worker.js"
      content_type = "application/javascript+module"
      content_file = "../../src/worker.js"
    }
  ]

  bindings = [
    {
      type         = "kv_namespace"
      name         = "TELEMETRY"
      namespace_id = cloudflare_workers_kv_namespace.telemetry.id
    },
    {
      type = "plain_text"
      name = "ALLOWED_ORIGIN"
      text = var.allowed_origin
    },
    {
      type = "plain_text"
      name = "REQUIRE_INSTALL_TOKEN"
      text = var.require_install_token ? "1" : "0"
    },
    {
      type = "plain_text"
      name = "TRUSTED_COUNTS_ONLY"
      text = var.trusted_counts_only ? "1" : "0"
    },
    {
      type = "plain_text"
      name = "STATS_CACHE_TTL_SECONDS"
      text = tostring(var.stats_cache_ttl_seconds)
    },
    {
      type = "secret_text"
      name = "RATE_LIMIT_SALT"
      text = var.rate_limit_salt
    },
    {
      type = "secret_text"
      name = "TRUSTED_INGEST_TOKEN"
      text = var.trusted_ingest_token
    }
  ]
}

resource "cloudflare_workers_deployment" "telemetry" {
  account_id  = var.cloudflare_account_id
  script_name = cloudflare_worker.telemetry.name
  strategy    = "percentage"

  versions = [
    {
      percentage = 100
      version_id = cloudflare_worker_version.telemetry.id
    }
  ]
}
