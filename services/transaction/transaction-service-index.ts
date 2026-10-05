import { Pool } from 'pg';
import pino from 'pino';

import { initTransactionAPI } from './transaction-service-api';
import { TransactionService } from './transaction-service-core';
import { LedgerClient } from './transaction-service-ledger-client';
import { outboundServiceToken } from './service-auth';
import { initRoyaltyAPI } from './royalty-service-api';
import { RoyaltyService } from './royalty-service-core';
import { RoyaltyLedgerClient } from './royalty-service-ledger-client';

const logger = pino();
const PORT = parseInt(process.env.PORT || '3000', 10);

async function main() {
  logger.info('Starting Transaction Service');

  // DB_HOST and friends when DB_HOST is set (existing setups unchanged);
  // otherwise DATABASE_URL (what docker-compose sets); otherwise defaults.
  const useUrl = !process.env.DB_HOST && !!process.env.DATABASE_URL;
  const pool = new Pool({
    ...(useUrl
      ? { connectionString: process.env.DATABASE_URL }
      : {
          host: process.env.DB_HOST || '127.0.0.1',
          port: parseInt(process.env.DB_PORT || '5432', 10),
          database: process.env.DB_NAME || 'archisynapse',
          user: process.env.DB_USER || 'postgres',
          password: process.env.DB_PASSWORD || 'postgres',
        }),
    max: 20,
  });

  try {
    const client = await pool.connect();
    await client.query('SELECT NOW()');
    client.release();
    logger.info('Connected to database');
  } catch (error) {
    logger.error(error, 'Failed to connect to database');
    process.exit(1);
  }

  // Surface a missing or unreadable ledger token at startup rather than on the
  // first payment. The clients still re-read it on every call (rotation).
  // An unreadable ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN_FILE throws here and
  // stops the service.
  if (!outboundServiceToken('ARCHISYNAPSE_TRANSACTION_TO_LEDGER')) {
    logger.warn(
      'No ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN(_FILE): ledger calls carry no service token ' +
        'and an enforcing ledger will refuse them'
    );
  }

  const ledgerClient = new LedgerClient(process.env.LEDGER_SERVICE_URL || 'http://127.0.0.1:3001');
  const transactionService = new TransactionService(pool, ledgerClient, {
    processorClearingAccountId: process.env.LEDGER_PROCESSOR_CLEARING_ACCOUNT_ID || '',
    merchantPayableAccountId: process.env.LEDGER_MERCHANT_PAYABLE_ACCOUNT_ID || '',
    platformFeeRevenueAccountId: process.env.LEDGER_PLATFORM_FEE_REVENUE_ACCOUNT_ID || '',
  });

  const royaltyLedgerClient = new RoyaltyLedgerClient(
    process.env.LEDGER_SERVICE_URL || 'http://127.0.0.1:3001'
  );
  const royaltyService = new RoyaltyService(pool, royaltyLedgerClient);

  const app = initTransactionAPI(transactionService);
  app.use(initRoyaltyAPI(royaltyService));
  const server = app.listen(PORT, () => {
    logger.info(`Transaction Service listening on http://127.0.0.1:${PORT}`);
  });

  const shutdown = async (signal: string) => {
    logger.info(`Received ${signal}, shutting down`);
    server.close(async () => {
      await pool.end();
      process.exit(0);
    });
    setTimeout(() => process.exit(1), 10000);
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch((error) => {
  logger.error(error, 'Fatal transaction service error');
  process.exit(1);
});
