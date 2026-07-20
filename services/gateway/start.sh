#!/bin/bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="$BASE_DIR/services/gateway"
FRAUD_DIR="$BASE_DIR/services/fraud"
ANALYTICS_DIR="$BASE_DIR/services/analytics"
TRANSACTION_DIR="$BASE_DIR/services/transaction"
LEDGER_DIR="$BASE_DIR/services/ledger"

POSTGRES_PORT="${POSTGRES_PORT:-55432}"
RUN_ID="${RUN_ID:-$(date +%s)}"
POSTGRES_CONTAINER="archisynapse-gateway-postgres-${POSTGRES_PORT}-${RUN_ID}"
DB_NAME="archisynapse"
DB_USER="postgres"
DB_PASSWORD="postgres"

FRAUD_PORT="${FRAUD_PORT:-8082}"
ANALYTICS_PORT="${ANALYTICS_PORT:-8081}"
TRANSACTION_PORT="${TRANSACTION_PORT:-3000}"
LEDGER_PORT="${LEDGER_PORT:-3001}"
GATEWAY_PORT="${GATEWAY_PORT:-9000}"

PIDS=()

cleanup() {
  echo
  echo "[cleanup] stopping local services"
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  docker stop "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts="${3:-60}"

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "[ready] $name -> $url"
      return 0
    fi
    sleep 1
  done

  echo "[error] $name did not become ready: $url" >&2
  return 1
}

port_in_use() {
  local port="$1"
  ss -ltn "( sport = :${port} )" | grep -q ":${port}"
}

backup_sqlite_if_present() {
  local path="$1"
  if [ -f "$path" ]; then
    mv "$path" "${path}.bak.$(date +%s)"
  fi
}

ensure_node_deps() {
  if [ ! -d "$BASE_DIR/node_modules" ]; then
    echo "[setup] installing node workspace dependencies"
    (cd "$BASE_DIR" && npm install)
  fi
}

start_postgres() {
  echo "[setup] starting postgres container on ${POSTGRES_PORT}"
  docker run -d \
    --name "$POSTGRES_CONTAINER" \
    -e POSTGRES_DB="$DB_NAME" \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -p "${POSTGRES_PORT}:5432" \
    postgres:16-alpine >/dev/null

  local postgres_ready=0
  for ((i=1; i<=60; i++)); do
    if docker exec "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" >/dev/null 2>&1; then
      postgres_ready=1
      break
    fi
    sleep 1
  done

  if [ "$postgres_ready" -ne 1 ]; then
    echo "[error] postgres did not become query-ready in time" >&2
    return 1
  fi

  echo "[setup] applying transaction schema"
  docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" < "$TRANSACTION_DIR/transaction-service-schema.sql"
  echo "[setup] applying ledger schema"
  docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" < "$LEDGER_DIR/ledger-service-schema.sql"
}

start_fraud() {
  echo "[start] fraud service on ${FRAUD_PORT}"
  backup_sqlite_if_present "$FRAUD_DIR/archisynapse_fraud.db"
  (
    cd "$FRAUD_DIR"
    ./.venv/bin/uvicorn archisynapse_fraud_mvp:app --host 127.0.0.1 --port "$FRAUD_PORT"
  ) &
  PIDS+=($!)
}

start_analytics() {
  echo "[start] analytics service on ${ANALYTICS_PORT}"
  backup_sqlite_if_present "$ANALYTICS_DIR/revenue_intelligence.db"
  (
    cd "$ANALYTICS_DIR"
    ./.venv/bin/uvicorn revenue_intelligence:app --host 127.0.0.1 --port "$ANALYTICS_PORT"
  ) &
  PIDS+=($!)
}

start_ledger() {
  echo "[start] ledger service on ${LEDGER_PORT}"
  (
    cd "$LEDGER_DIR"
    DB_HOST=127.0.0.1 \
    DB_PORT="$POSTGRES_PORT" \
    DB_NAME="$DB_NAME" \
    DB_USER="$DB_USER" \
    DB_PASSWORD="$DB_PASSWORD" \
    PORT="$LEDGER_PORT" \
    npx tsx ledger-service-index.ts
  ) &
  PIDS+=($!)
}

start_transaction() {
  echo "[start] transaction service on ${TRANSACTION_PORT}"
  (
    cd "$TRANSACTION_DIR"
    DB_HOST=127.0.0.1 \
    DB_PORT="$POSTGRES_PORT" \
    DB_NAME="$DB_NAME" \
    DB_USER="$DB_USER" \
    DB_PASSWORD="$DB_PASSWORD" \
    PORT="$TRANSACTION_PORT" \
    LEDGER_SERVICE_URL="http://127.0.0.1:${LEDGER_PORT}" \
    npx tsx transaction-service-index.ts
  ) &
  PIDS+=($!)
}

start_gateway() {
  echo "[start] gateway service on ${GATEWAY_PORT}"
  (
    cd "$GATEWAY_DIR"
    FRAUD_SERVICE_URL="http://127.0.0.1:${FRAUD_PORT}" \
    TRANSACTION_SERVICE_URL="http://127.0.0.1:${TRANSACTION_PORT}" \
    LEDGER_SERVICE_URL="http://127.0.0.1:${LEDGER_PORT}" \
    ANALYTICS_SERVICE_URL="http://127.0.0.1:${ANALYTICS_PORT}" \
    ./.venv/bin/uvicorn main:app --host 127.0.0.1 --port "$GATEWAY_PORT"
  ) &
  PIDS+=($!)
}

main() {
  echo "=============================================================="
  echo "ARCHISYNAPSE REVENUE ASSURANCE LOOP v1 STARTUP"
  echo "=============================================================="

  ensure_node_deps

  for port in "$FRAUD_PORT" "$ANALYTICS_PORT" "$TRANSACTION_PORT" "$LEDGER_PORT" "$GATEWAY_PORT"; do
    if port_in_use "$port"; then
      echo "[error] port ${port} is already in use; stop the stale listener before running start.sh" >&2
      exit 1
    fi
  done

  start_postgres
  start_fraud
  start_analytics
  start_ledger
  start_transaction
  start_gateway

  wait_for_http "fraud" "http://127.0.0.1:${FRAUD_PORT}/health"
  wait_for_http "analytics" "http://127.0.0.1:${ANALYTICS_PORT}/health"
  wait_for_http "ledger" "http://127.0.0.1:${LEDGER_PORT}/health"
  wait_for_http "transaction" "http://127.0.0.1:${TRANSACTION_PORT}/health"
  wait_for_http "gateway" "http://127.0.0.1:${GATEWAY_PORT}/health"

  echo "[ready] all five application services are up"
  echo "fraud=http://127.0.0.1:${FRAUD_PORT}"
  echo "analytics=http://127.0.0.1:${ANALYTICS_PORT}"
  echo "ledger=http://127.0.0.1:${LEDGER_PORT}"
  echo "transaction=http://127.0.0.1:${TRANSACTION_PORT}"
  echo "gateway=http://127.0.0.1:${GATEWAY_PORT}"

  wait
}

main "$@"
