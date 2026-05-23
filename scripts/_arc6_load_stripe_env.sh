#!/bin/bash
# Loads Stripe Live secret + 6 old Price IDs into env vars from SSM.
# No values are echoed; no disk writes. `set -u` so missing fetches fail loud.
# Must be `source`d, not executed, so the exports propagate to the caller.
set -u
# AWS creds must already be exported by caller.
for p in stripe_secret_key stripe_price_individual stripe_price_individual_annual \
         stripe_price_team_monthly stripe_price_team_annual \
         stripe_price_company_monthly stripe_price_company_annual \
         stripe_price_intro_fee; do
  v=$(python3 -c "import boto3,os; print(boto3.client('ssm',region_name=os.environ['AWS_DEFAULT_REGION']).get_parameter(Name='/luciel/production/${p}',WithDecryption=True)['Parameter']['Value'])")
  export "ARC6_$(echo $p | tr '[:lower:]' '[:upper:]')=$v"
done
# Sanity: confirm STRIPE_SECRET_KEY starts with sk_live_ (no value echo).
case "${ARC6_STRIPE_SECRET_KEY:-}" in
  sk_live_*) echo "STRIPE secret: LIVE mode confirmed (prefix sk_live_)" ;;
  sk_test_*) echo "STRIPE secret: TEST mode (prefix sk_test_) — UNEXPECTED, abort"; return 1 2>/dev/null || exit 1 ;;
  *)         echo "STRIPE secret: UNRECOGNIZED prefix — abort"; return 1 2>/dev/null || exit 1 ;;
esac
echo "Stripe Price slots loaded: 6 recurring + 1 intro-fee"
