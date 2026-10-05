#!/bin/sh
# Run on every `docker compose up` by the `service-tokens` service.
#
# Creates one random token per internal call path, once, and keeps it in the
# `service_tokens_store` volume (mounted only into this setup service) so
# restarts reuse it. Then (re)writes, into one volume per service, ONLY what
# that service needs:
#
#   gateway      gateway-to-transaction.token   (raw, to call transaction)
#                gateway-to-ledger.token        (raw, read-only on the ledger)
#   transaction  transaction-to-ledger.token    (raw, to write to the ledger)
#                inbound.tokens                 (SHA-256 of the gateway's token)
#   ledger       inbound.tokens                 (SHA-256 of the gateway's and
#                                                the transaction service's tokens)
#
# So the gateway never holds the ledger write token, and a receiving service
# never holds a usable copy of its callers' tokens. No manual step and no
# secret in .env or source control.
#
# For production, do not use this: issue the tokens from a secret manager and
# set ARCHISYNAPSE_*_TO_*_TOKEN(_FILE) and ARCHISYNAPSE_INBOUND_SERVICE_TOKENS(_FILE).
set -eu

store="${SERVICE_TOKEN_STORE:-/service-tokens/store}"
out_gateway="${SERVICE_TOKEN_GATEWAY_DIR:-/service-tokens/gateway}"
out_transaction="${SERVICE_TOKEN_TRANSACTION_DIR:-/service-tokens/transaction}"
out_ledger="${SERVICE_TOKEN_LEDGER_DIR:-/service-tokens/ledger}"
mkdir -p "$store" "$out_gateway" "$out_transaction" "$out_ledger"
umask 077
chmod 700 "$store"
umask 022

make_token() {
  path="$store/$1.token"
  if [ ! -s "$path" ]; then
    # 32 random bytes as 64 hex characters.
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$path.tmp"
    chmod 600 "$path.tmp"
    mv "$path.tmp" "$path"
    echo "created $1 token"
  else
    echo "kept existing $1 token"
  fi
}

digest() {
  printf '%s' "$(cat "$store/$1.token")" | sha256sum | cut -d' ' -f1
}

# Write atomically: a service reading during an update sees old or new, never half.
put() {
  cat > "$1.tmp"
  chmod 644 "$1.tmp"
  mv "$1.tmp" "$1"
}

make_token gateway-to-transaction
make_token gateway-to-ledger
make_token transaction-to-ledger

cat "$store/gateway-to-transaction.token" | put "$out_gateway/gateway-to-transaction.token"
cat "$store/gateway-to-ledger.token" | put "$out_gateway/gateway-to-ledger.token"

cat "$store/transaction-to-ledger.token" | put "$out_transaction/transaction-to-ledger.token"
printf 'gateway:write:sha256:%s\n' "$(digest gateway-to-transaction)" | put "$out_transaction/inbound.tokens"

printf 'gateway:read:sha256:%s\ntransaction:write:sha256:%s\n' \
  "$(digest gateway-to-ledger)" "$(digest transaction-to-ledger)" | put "$out_ledger/inbound.tokens"

echo "service tokens ready"
