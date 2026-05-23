# Alfred AWS Route 53 zone reference

Captured 2026-05-23 after the alfred subdomain delegation landed.

## Hosted zones

| Domain | Zone ID | Purpose |
|---|---|---|
| `luminik.io` | `Z0711232ISS6HC06WOLL` | Parent zone (existing). Holds the `alfred` NS delegation record. |
| `alfred.luminik.io` | `Z06176411ZA2HK17JOUEJ` | New zone owned by Alfred. Holds the apex A/AAAA records pointing to GitHub Pages, plus any future TXT (GSC, GA-verification, ACM). |

## Nameservers on the new zone

```
ns-681.awsdns-21.net
ns-1423.awsdns-49.org
ns-391.awsdns-48.com
ns-1942.awsdns-50.co.uk
```

## Apex records on the new zone

| Type | Name | Values |
|---|---|---|
| `A` | `alfred.luminik.io.` | `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153` (GitHub Pages apex IPs) |
| `AAAA` | `alfred.luminik.io.` | `2606:50c0:8000::153`, `2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153` |

## How to add records here later

For GSC verification or GA4 DNS verification, the record goes in the new zone, not the parent:

```sh
aws route53 change-resource-record-sets \
  --hosted-zone-id Z06176411ZA2HK17JOUEJ \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "alfred.luminik.io",
        "Type": "TXT",
        "TTL": 300,
        "ResourceRecords": [{"Value": "\"google-site-verification=YOUR_TOKEN\""}]
      }
    }]
  }' --profile luminik
```

## Rollback

If anything goes wrong, restore the original parent-zone CNAME and delete the new zone:

```sh
# 1. Re-add the CNAME on the parent
aws route53 change-resource-record-sets --hosted-zone-id Z0711232ISS6HC06WOLL --change-batch '{
  "Changes": [
    {"Action": "DELETE", "ResourceRecordSet": {"Name":"alfred.luminik.io","Type":"NS","TTL":172800,"ResourceRecords":[{"Value":"ns-681.awsdns-21.net"},{"Value":"ns-1423.awsdns-49.org"},{"Value":"ns-391.awsdns-48.com"},{"Value":"ns-1942.awsdns-50.co.uk"}]}},
    {"Action": "UPSERT", "ResourceRecordSet": {"Name":"alfred.luminik.io","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"luminik-io.github.io"}]}}
  ]
}' --profile luminik

# 2. Empty + delete the new zone
aws route53 list-resource-record-sets --hosted-zone-id Z06176411ZA2HK17JOUEJ --profile luminik
# ...DELETE the A and AAAA records first, then:
aws route53 delete-hosted-zone --id Z06176411ZA2HK17JOUEJ --profile luminik
```
