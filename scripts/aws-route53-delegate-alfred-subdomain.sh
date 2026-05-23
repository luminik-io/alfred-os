#!/usr/bin/env bash
#
# Delegate alfred.luminik.io to its own Route 53 hosted zone.
#
# Current DNS state (verified 2026-05-23 via dig):
#   - luminik.io is hosted on AWS Route 53 (ns-{186,547,1079,1765}.awsdns-*)
#   - alfred.luminik.io resolves via a CNAME (or record at the parent zone)
#     to luminik-io.github.io, then to GitHub Pages IPs:
#       185.199.108.153, .109.153, .110.153, .111.153
#
# Goal:
#   - Carve alfred.luminik.io out as its own hosted zone so we can manage
#     its DNS (GA verification, GSC verification, future CDN, etc.) without
#     touching the parent luminik.io zone.
#
# Plan (idempotent where possible):
#   1. Create a new public hosted zone for alfred.luminik.io.
#   2. Read back its 4 NS records.
#   3. Inside the new zone, add A and AAAA records at the apex pointing at
#      GitHub Pages' published IPs. RFC-clean (apex can't be a CNAME).
#   4. In the parent luminik.io zone, REMOVE any existing alfred.luminik.io
#      record (CNAME or A set) and ADD an NS record set pointing to the new
#      zone's nameservers. This is the actual delegation.
#   5. Verify by re-running dig and checking NS authority moves.
#
# This script PRINTS the commands it would run. To actually execute, pass
# --execute. Safe default is dry-run because zone changes propagate fast
# and are awkward to roll back if a record is wrong.
#
# Prereqs:
#   - aws sso login (run this first; the script will check)
#   - jq installed
#   - permission to write to both zones
#
# Usage:
#   ./aws-route53-delegate-alfred-subdomain.sh             # dry-run, prints plan
#   ./aws-route53-delegate-alfred-subdomain.sh --execute   # actually runs it

set -euo pipefail

EXECUTE=0
if [[ "${1:-}" == "--execute" ]]; then
  EXECUTE=1
fi

PARENT_DOMAIN="luminik.io"
SUB_DOMAIN="alfred.luminik.io"

# GitHub Pages apex IPs (published, stable). Source:
#   https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#configuring-an-apex-domain
GH_A_IPS=(185.199.108.153 185.199.109.153 185.199.110.153 185.199.111.153)
GH_AAAA_IPS=(
  "2606:50c0:8000::153"
  "2606:50c0:8001::153"
  "2606:50c0:8002::153"
  "2606:50c0:8003::153"
)

run() {
  if [[ $EXECUTE -eq 1 ]]; then
    echo "+ $*"
    "$@"
  else
    echo "[dry-run] $*"
  fi
}

echo "==> Step 0: check auth"
aws sts get-caller-identity > /dev/null 2>&1 || {
  echo "ERROR: AWS auth is missing or expired. Run: aws sso login"
  exit 1
}
echo "  caller: $(aws sts get-caller-identity --query Arn --output text)"

echo
echo "==> Step 1: locate parent zone for $PARENT_DOMAIN"
PARENT_ZONE_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name "$PARENT_DOMAIN." \
  --max-items 1 \
  --query 'HostedZones[?Name==`'"$PARENT_DOMAIN"'.`].Id' \
  --output text | sed 's|/hostedzone/||')

if [[ -z "$PARENT_ZONE_ID" ]]; then
  echo "ERROR: parent hosted zone for $PARENT_DOMAIN not found in this account."
  exit 1
fi
echo "  parent zone id: $PARENT_ZONE_ID"

echo
echo "==> Step 2: check whether $SUB_DOMAIN zone already exists"
EXISTING_SUB_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name "$SUB_DOMAIN." \
  --query 'HostedZones[?Name==`'"$SUB_DOMAIN"'.`].Id' \
  --output text | sed 's|/hostedzone/||' || true)

if [[ -n "$EXISTING_SUB_ID" ]]; then
  echo "  found existing $SUB_DOMAIN zone: $EXISTING_SUB_ID. Reusing."
  SUB_ZONE_ID=$EXISTING_SUB_ID
else
  echo "  no existing $SUB_DOMAIN zone. Creating."
  CALLER_REF="alfred-delegate-$(date +%Y%m%d%H%M%S)"
  if [[ $EXECUTE -eq 1 ]]; then
    SUB_ZONE_ID=$(aws route53 create-hosted-zone \
      --name "$SUB_DOMAIN" \
      --caller-reference "$CALLER_REF" \
      --hosted-zone-config Comment="Carve alfred.luminik.io out of parent for independent DNS",PrivateZone=false \
      --query 'HostedZone.Id' --output text | sed 's|/hostedzone/||')
    echo "  created zone: $SUB_ZONE_ID"
  else
    echo "[dry-run] aws route53 create-hosted-zone --name $SUB_DOMAIN --caller-reference $CALLER_REF ..."
    SUB_ZONE_ID="<NEW-ZONE-ID>"
  fi
fi

echo
echo "==> Step 3: read new zone NS records"
if [[ $EXECUTE -eq 1 && "$SUB_ZONE_ID" != "<NEW-ZONE-ID>" ]]; then
  NS_RECORDS=$(aws route53 get-hosted-zone --id "$SUB_ZONE_ID" --query 'DelegationSet.NameServers' --output json)
  echo "  nameservers: $NS_RECORDS"
else
  NS_RECORDS='["ns-XXXX.awsdns-XX.com","ns-XXXX.awsdns-XX.net","ns-XXXX.awsdns-XX.org","ns-XXXX.awsdns-XX.co.uk"]'
fi

echo
echo "==> Step 4: add A + AAAA records at the apex of $SUB_DOMAIN"
A_BATCH=$(cat <<EOF
{
  "Comment": "Point alfred.luminik.io apex at GitHub Pages",
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "$SUB_DOMAIN",
        "Type": "A",
        "TTL": 300,
        "ResourceRecords": [
$(printf '          {"Value":"%s"},\n' "${GH_A_IPS[@]}" | sed '$ s/,$//')
        ]
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "$SUB_DOMAIN",
        "Type": "AAAA",
        "TTL": 300,
        "ResourceRecords": [
$(printf '          {"Value":"%s"},\n' "${GH_AAAA_IPS[@]}" | sed '$ s/,$//')
        ]
      }
    }
  ]
}
EOF
)
if [[ $EXECUTE -eq 1 && "$SUB_ZONE_ID" != "<NEW-ZONE-ID>" ]]; then
  echo "$A_BATCH" > /tmp/alfred-a-batch.json
  aws route53 change-resource-record-sets --hosted-zone-id "$SUB_ZONE_ID" --change-batch file:///tmp/alfred-a-batch.json
  echo "  A + AAAA records applied"
else
  echo "[dry-run] would UPSERT 4 A and 4 AAAA records for $SUB_DOMAIN in $SUB_ZONE_ID"
fi

echo
echo "==> Step 5: delegate from parent zone"
echo "  In parent zone $PARENT_ZONE_ID:"
echo "    - DELETE any existing $SUB_DOMAIN record (CNAME / A / AAAA)"
echo "    - ADD an NS record set pointing to the new zone's nameservers"

if [[ $EXECUTE -eq 1 ]]; then
  echo "  Reading existing $SUB_DOMAIN records on parent..."
  EXISTING_PARENT_RECORDS=$(aws route53 list-resource-record-sets \
    --hosted-zone-id "$PARENT_ZONE_ID" \
    --query "ResourceRecordSets[?Name=='$SUB_DOMAIN.']" \
    --output json)
  echo "$EXISTING_PARENT_RECORDS" | jq .

  # Build DELETE + UPSERT in one change batch.
  DELETES=$(echo "$EXISTING_PARENT_RECORDS" | jq '[.[] | {"Action":"DELETE","ResourceRecordSet": .}]')
  NS_VALUES=$(echo "$NS_RECORDS" | jq '[.[] | {Value: .}]')

  PARENT_BATCH=$(jq -n \
    --argjson deletes "$DELETES" \
    --argjson ns "$NS_VALUES" \
    --arg name "$SUB_DOMAIN" \
    '{Comment: "Delegate alfred.luminik.io to its own zone", Changes: ($deletes + [{Action:"UPSERT", ResourceRecordSet:{Name:$name, Type:"NS", TTL:172800, ResourceRecords:$ns}}])}')

  echo "$PARENT_BATCH" > /tmp/alfred-parent-batch.json
  aws route53 change-resource-record-sets --hosted-zone-id "$PARENT_ZONE_ID" --change-batch file:///tmp/alfred-parent-batch.json
  echo "  Delegation applied. NS authority moved from $PARENT_ZONE_ID to $SUB_ZONE_ID."
else
  echo "[dry-run] would delete existing records and UPSERT NS record set"
fi

echo
echo "==> Step 6: verify (run manually after a minute)"
cat <<'VERIFY'
  dig +trace alfred.luminik.io NS
  dig alfred.luminik.io A
  curl -I https://alfred.luminik.io/
VERIFY

echo
if [[ $EXECUTE -eq 1 ]]; then
  echo "Done. Note: DNS propagation typically takes 1-15 minutes."
  echo "If GitHub Pages flags the custom domain, re-save it in repo settings to refresh."
else
  echo "Dry-run complete. To execute, re-run with --execute."
fi
