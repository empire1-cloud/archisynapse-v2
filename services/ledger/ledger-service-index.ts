import { Pool } from 'pg';
import { createLogger } from 'pino';
import { initLedgerAPI } from './ledger-service-api';
import { LedgerService } from './ledger-service-core';

const logger = createLogger();

/**
 * Archisynapse Ledger Service
 * 
 * A production-grade double-entry bookkeeping engine for the Archisynapse payments platform.
 * 
 * Features:
 * - Immutable ledger (insert-only, no updates)
 * - Strict double-entry enforcement (debits = credits)
 * - Transaction atomicity and isolation
 * - Idempotency support (safe retries)
 * - Comprehensive audit logging
 * - Trial balance and reconciliation endpoints
 * 
 * Environment variables:
 * - DB_HOST: PostgreSQL host (default: localhost)
 * - DB_PORT: PostgreSQL port (default: 5432)
 * - DB_NAME: Database name (default: archisynapse)
 * - DB_USER: Database user (default: postgres)
 * - DB_PASSWORD: Database password
 * - PORT: HTTP port (default: 3001)
 * - NODE_ENV: Environment (development/production)
 */

const PORT = parseInt(process.env.PORT || '3001', 10);
const NODE_ENV = process.env.NODE_ENV || 'development';

async function main() {
  logger.info(`Starting Ledger Service (${NODE_ENV})`);

  // Initialize database pool
  const pool = new Pool({
    host: process.env.DB_HOST || 'localhost',
    port: parseInt(process.env.DB_PORT || '5432', 10),
    database: process.env.DB_NAME || 'archisynapse',
    user: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || '',
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
  });

  // Health check: connect to database
  try {
    const client = await pool.connect();
    const result = await client.query('SELECT NOW()');
    client.release();
    logger.info(`Connected to database at ${result.rows[0].now}`);
  } catch (err) {
    logger.error(err, 'Failed to connect to database');
    process.exit(1);
  }

  // Initialize ledger service
  const ledgerService = new LedgerService(pool);

  // Initialize Express API
  const app = initLedgerAPI(ledgerService);

  // Start HTTP server
  const server = app.listen(PORT, () => {
    logger.info(
      `Ledger Service listening on http://localhost:${PORT}`,
      {
        nodeEnv: NODE_ENV,
        dbHost: process.env.DB_HOST || 'localhost',
      }
    );
  });

  // Graceful shutdown
  const shutdown = async (signal: string) => {
    logger.info(`Received ${signal}, shutting down gracefully...`);
    server.close(async () => {
      await pool.end();
      logger.info('Database connection closed');
      process.exit(0);
    });

    // Force shutdown after 10 seconds
    setTimeout(() => {
      logger.error('Forced shutdown after 10 seconds');
      process.exit(1);
    }, 10000);
  };

  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

main().catch((err) => {
  logger.error(err, 'Fatal error');
  process.exit(1);
});
