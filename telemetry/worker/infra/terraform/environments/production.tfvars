# Production values for Alfred's hosted telemetry Worker.
#
# Do NOT commit a real Cloudflare API token or rate-limit salt. Pass them via:
#   export TF_VAR_cloudflare_api_token=<token>
#   export TF_VAR_rate_limit_salt=$(openssl rand -base64 48)
#   export TF_VAR_trusted_ingest_token=$(openssl rand -base64 48)

# Non-secret defaults live in variables.tf.
