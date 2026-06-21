output "worker_name" {
  description = "Worker script name."
  value       = cloudflare_worker.telemetry.name
}

output "kv_namespace_id" {
  description = "Production KV namespace ID."
  value       = cloudflare_workers_kv_namespace.telemetry.id
}

output "stats_path" {
  description = "Path exposed by the Worker for public aggregate stats."
  value       = "/stats"
}

output "ingest_path" {
  description = "Path used by Alfred installs to report anonymous aggregate counts."
  value       = "/ingest"
}
