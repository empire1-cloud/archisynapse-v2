import { createHash, timingSafeEqual } from 'crypto';
import { readFileSync } from 'fs';
import type { NextFunction, Request, RequestHandler, Response } from 'express';

/**
 * Service-to-service authentication for internal Archisynapse services.
 *
 * Before this, the ledger and transaction services trusted any request that
 * carried an X-Organization-ID header. Anything that could reach the port
 * could act as any merchant. Now a caller must also present a service token
 * (`Authorization: Bearer <token>`) issued to it by name. Only then is the
 * X-Organization-ID header read.
 *
 * Configuration (identical in both services; this file is duplicated in
 * services/ledger and services/transaction, like tenant-session.ts):
 *
 *   ARCHISYNAPSE_SERVICE_AUTH            enforce (default) | off
 *   ARCHISYNAPSE_INBOUND_SERVICE_TOKENS  entries "caller:scope:token"
 *   ARCHISYNAPSE_INBOUND_SERVICE_TOKENS_FILE  path to a file of the same
 *                                        entries (one per line or comma separated)
 *
 *   scope = write  any method
 *   scope = read   GET and HEAD only (the gateway may read the ledger but
 *                  must never post to it)
 *
 *   token may be given as "sha256:<64 hex>" (the SHA-256 of the token) so the
 *   receiving service never holds a usable copy of its callers' tokens.
 *
 * Several entries per caller are allowed, so a token can be rotated without
 * downtime: add the new one, move callers over, remove the old one.
 *
 * Fails closed: in enforce mode the service refuses to start with no valid
 * entries, and every request without a matching token is rejected with 401
 * (403 for a read-only caller attempting a write). Tokens shorter than 32
 * characters are refused. Tokens are compared in constant time and never
 * logged.
 */

export type ServiceScope = 'read' | 'write';

export interface ServiceCredential {
  caller: string;
  scope: ServiceScope;
  digest: Buffer;
}

export interface ServiceAuthConfig {
  enforce: boolean;
  credentials: ServiceCredential[];
}

export const MIN_SERVICE_TOKEN_LENGTH = 32;
const OPEN_PATHS = new Set(['/health', '/ready']);
const READ_METHODS = new Set(['GET', 'HEAD']);
const CALLER_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

function digest(token: string): Buffer {
  return createHash('sha256').update(token, 'utf8').digest();
}

export function parseServiceTokens(spec: string): ServiceCredential[] {
  const credentials: ServiceCredential[] = [];
  for (const raw of spec.split(/[\n,]/)) {
    const entry = raw.trim();
    if (!entry || entry.startsWith('#')) {
      continue;
    }
    const first = entry.indexOf(':');
    const second = first < 0 ? -1 : entry.indexOf(':', first + 1);
    if (first < 0 || second < 0) {
      throw new Error('service token entries must look like caller:scope:token');
    }
    const caller = entry.slice(0, first);
    const scope = entry.slice(first + 1, second);
    const token = entry.slice(second + 1);
    if (!CALLER_PATTERN.test(caller)) {
      throw new Error(`invalid service caller name: ${JSON.stringify(caller)}`);
    }
    if (scope !== 'read' && scope !== 'write') {
      throw new Error(`service token scope for ${caller} must be read or write`);
    }
    if (token.startsWith('sha256:')) {
      const hex = token.slice('sha256:'.length);
      if (!/^[0-9a-f]{64}$/i.test(hex)) {
        throw new Error(`service token digest for ${caller} must be sha256:<64 hex characters>`);
      }
      credentials.push({ caller, scope, digest: Buffer.from(hex, 'hex') });
      continue;
    }
    if (token.length < MIN_SERVICE_TOKEN_LENGTH) {
      throw new Error(
        `service token for ${caller} must be at least ${MIN_SERVICE_TOKEN_LENGTH} characters`
      );
    }
    credentials.push({ caller, scope, digest: digest(token) });
  }
  return credentials;
}

export function serviceAuthFromEnv(
  env: NodeJS.ProcessEnv = process.env,
  readFile: (path: string) => string = (path) => readFileSync(path, 'utf8')
): ServiceAuthConfig {
  const mode = (env.ARCHISYNAPSE_SERVICE_AUTH ?? 'enforce').trim().toLowerCase();
  if (mode !== 'enforce' && mode !== 'off') {
    throw new Error("ARCHISYNAPSE_SERVICE_AUTH must be 'enforce' or 'off'");
  }
  if (mode === 'off') {
    return { enforce: false, credentials: [] };
  }
  const specs: string[] = [];
  if (env.ARCHISYNAPSE_INBOUND_SERVICE_TOKENS) {
    specs.push(env.ARCHISYNAPSE_INBOUND_SERVICE_TOKENS);
  }
  if (env.ARCHISYNAPSE_INBOUND_SERVICE_TOKENS_FILE) {
    specs.push(readFile(env.ARCHISYNAPSE_INBOUND_SERVICE_TOKENS_FILE));
  }
  const credentials = specs.flatMap(parseServiceTokens);
  if (credentials.length === 0) {
    throw new Error(
      'ARCHISYNAPSE_SERVICE_AUTH=enforce needs ARCHISYNAPSE_INBOUND_SERVICE_TOKENS ' +
        'or ARCHISYNAPSE_INBOUND_SERVICE_TOKENS_FILE (refusing to start without them)'
    );
  }
  return { enforce: true, credentials };
}

function bearerToken(req: Request): string | null {
  const header = req.headers.authorization;
  if (typeof header !== 'string') {
    return null;
  }
  const match = /^Bearer\s+(\S+)\s*$/i.exec(header);
  return match ? match[1] : null;
}

export function matchServiceCredential(
  credentials: ServiceCredential[],
  token: string
): ServiceCredential | null {
  const candidate = digest(token);
  let found: ServiceCredential | null = null;
  // Compare against every entry so timing does not reveal which one matched.
  for (const credential of credentials) {
    if (timingSafeEqual(candidate, credential.digest) && found === null) {
      found = credential;
    }
  }
  return found;
}

export function createServiceAuthMiddleware(config: ServiceAuthConfig): RequestHandler {
  return (req: Request, res: Response, next: NextFunction) => {
    if (!config.enforce || OPEN_PATHS.has(req.path)) {
      return next();
    }
    const token = bearerToken(req);
    const credential = token ? matchServiceCredential(config.credentials, token) : null;
    if (!credential) {
      return res.status(401).json({ error: 'Service authentication required' });
    }
    if (credential.scope === 'read' && !READ_METHODS.has(req.method)) {
      return res
        .status(403)
        .json({ error: `Service caller ${credential.caller} is read-only here` });
    }
    (req as any).serviceCaller = credential.caller;
    next();
  };
}

/**
 * Outbound helper for calls this service makes to another internal service.
 * Reads <PREFIX>_TOKEN or <PREFIX>_TOKEN_FILE (the file is read on every call,
 * so a rotated token takes effect without a restart). Returns no header when
 * neither is set, so the receiving service (if enforcing) rejects the call:
 * fail closed.
 */
export function outboundServiceToken(
  prefix: string,
  env: NodeJS.ProcessEnv = process.env,
  readFile: (path: string) => string = (path) => readFileSync(path, 'utf8')
): string | null {
  const direct = env[`${prefix}_TOKEN`];
  if (direct && direct.trim()) {
    return direct.trim();
  }
  const file = env[`${prefix}_TOKEN_FILE`];
  if (file && file.trim()) {
    const value = readFile(file.trim()).trim();
    return value || null;
  }
  return null;
}

export function serviceAuthHeaders(token: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}
