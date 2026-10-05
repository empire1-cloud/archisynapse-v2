import { Pool, PoolClient } from 'pg';

/**
 * Tenant isolation for PostgreSQL row-level security (migrations 007+).
 *
 * "enforce" (default): tenant-scoped work runs as the restricted role
 * `archisynapse_tenant` with `archisynapse.tenant_id` set, so a query that
 * forgets its organization filter still only sees that organization's rows.
 * "off": the previous behaviour (owner connection).
 *
 * The role and setting are session-level so existing statement-by-statement
 * (autocommit) behaviour is unchanged. close() always ends any transaction the
 * caller left open, resets the role and setting, and destroys the connection
 * instead of returning it to the pool if that reset fails. A tenant role can
 * therefore never leak to the next user of a pooled connection.
 */
export const TENANT_ROLE = 'archisynapse_tenant';

export function tenantIsolationFromEnv(env: NodeJS.ProcessEnv = process.env): boolean {
  const value = (env.ARCHISYNAPSE_TENANT_ISOLATION ?? 'enforce').trim().toLowerCase();
  if (value !== 'enforce' && value !== 'off') {
    throw new Error("ARCHISYNAPSE_TENANT_ISOLATION must be 'enforce' or 'off'");
  }
  return value === 'enforce';
}

export interface TenantSession {
  client: PoolClient;
  close(): Promise<void>;
}

export async function openTenantSession(
  pool: Pool,
  organizationId: string,
  enforce: boolean
): Promise<TenantSession> {
  if (!organizationId) {
    throw new Error('organizationId is required for a tenant-scoped query');
  }
  const client = await pool.connect();
  try {
    if (enforce) {
      await client.query(`SET ROLE ${TENANT_ROLE}`);
      await client.query("SELECT set_config('archisynapse.tenant_id', $1, false)", [
        organizationId,
      ]);
    }
  } catch (error) {
    client.release(error as Error);
    throw error;
  }

  let closed = false;
  return {
    client,
    async close() {
      if (closed) return;
      closed = true;
      let resetError: Error | undefined;
      try {
        // ROLLBACK first: a caller that returned early inside BEGIN must not
        // hand an open transaction (or a role set inside it) back to the pool.
        await client.query('ROLLBACK');
        if (enforce) {
          await client.query('RESET ROLE');
          await client.query("SELECT set_config('archisynapse.tenant_id', '', false)");
        }
      } catch (error) {
        resetError = error as Error;
      }
      client.release(resetError);
    },
  };
}

export async function tenantQuery(
  pool: Pool,
  organizationId: string,
  enforce: boolean,
  text: string,
  params: unknown[] = []
) {
  const session = await openTenantSession(pool, organizationId, enforce);
  try {
    return await session.client.query(text, params);
  } finally {
    await session.close();
  }
}
