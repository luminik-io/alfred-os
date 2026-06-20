# Authenticated via TF_VAR_cloudflare_api_token. The existing Wrangler OAuth
# token works locally; CI should use a scoped Cloudflare API token.
provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
