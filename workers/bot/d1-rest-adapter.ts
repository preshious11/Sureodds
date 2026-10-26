// D1 REST adapter: implements the same D1Database surface the Worker binding
// provides (prepare().bind().first()/.all()/.run()), backed by Cloudflare's
// D1 REST API instead of the in-process binding. Bot code imported from
// workers/src/ is unaware of the difference - env.DB is just an object with
// this shape (see workers/tests/*.test.mjs's own in-memory mock for the same
// contract, which this adapter is built to match exactly).
//
// Scope: only prepare/bind/first/all/run are used anywhere in the bot path
// (workers/src/db.ts, custom_picks.ts, ledger.ts - confirmed by audit, see
// docs/WORKER_DEPENDENCIES_INVENTORY.md). batch() and raw() are NOT used and
// are deliberately NOT implemented - calling them throws a clear error
// rather than silently doing the wrong thing.

interface D1Meta {
  changes?: number;
  last_row_id?: number;
  duration?: number;
  rows_read?: number;
  rows_written?: number;
  [key: string]: unknown;
}

interface D1RunResult {
  meta: D1Meta;
}

interface D1AllResult<T> {
  results: T[];
}

interface D1BoundStatement<T = unknown> {
  first<U = T>(): Promise<U | null>;
  all<U = T>(): Promise<D1AllResult<U>>;
  run(): Promise<D1RunResult>;
}

interface D1PreparedStatement<T = unknown> extends D1BoundStatement<T> {
  bind(...params: unknown[]): D1BoundStatement<T>;
}

export interface D1Database {
  prepare<T = unknown>(sql: string): D1PreparedStatement<T>;
}

interface CloudflareQueryResultEntry {
  results?: unknown[];
  success: boolean;
  meta?: D1Meta;
}

interface CloudflareQueryResponse {
  success: boolean;
  errors?: Array<{ code?: number; message: string }>;
  messages?: Array<{ code?: number; message: string }>;
  result?: CloudflareQueryResultEntry[];
}

const DEFAULT_TIMEOUT_MS = 15_000;
const MAX_RETRIES = 3;
const RETRY_BASE_DELAY_MS = 500;

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Minimal, synchronous-prepare D1 REST client. `prepare()` itself never
 * touches the network (matching the binding's own synchronous prepare) -
 * the HTTP call happens only when `.run()/.first()/.all()` is awaited.
 */
export class D1RestAdapter implements D1Database {
  private readonly endpoint: string;
  private readonly token: string;
  private readonly timeoutMs: number;

  constructor(accountId: string, databaseId: string, apiToken: string, timeoutMs = DEFAULT_TIMEOUT_MS) {
    if (!accountId || !databaseId || !apiToken) {
      throw new Error("D1RestAdapter requires accountId, databaseId and apiToken");
    }
    this.endpoint = `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database/${databaseId}/query`;
    this.token = apiToken;
    this.timeoutMs = timeoutMs;
  }

  prepare<T = unknown>(sql: string): D1PreparedStatement<T> {
    const execute = (params: unknown[]): Promise<CloudflareQueryResultEntry> => this.query(sql, params);

    const boundStatement = (params: unknown[]): D1BoundStatement<T> => ({
      async first<U = T>(): Promise<U | null> {
        const entry = await execute(params);
        const rows = (entry.results ?? []) as U[];
        return rows.length > 0 ? rows[0] : null;
      },
      async all<U = T>(): Promise<D1AllResult<U>> {
        const entry = await execute(params);
        return { results: (entry.results ?? []) as U[] };
      },
      async run(): Promise<D1RunResult> {
        const entry = await execute(params);
        return { meta: entry.meta ?? {} };
      },
    });

    return {
      bind(...params: unknown[]): D1BoundStatement<T> {
        return boundStatement(params);
      },
      ...boundStatement([]),
    };
  }

  /** Deliberately unsupported - see header comment. Throws rather than silently misbehaving. */
  batch(): never {
    throw new Error("D1RestAdapter.batch() is not implemented: no caller in the bot path uses it (see docs/WORKER_DEPENDENCIES_INVENTORY.md)");
  }

  /** Deliberately unsupported - see header comment. */
  raw(): never {
    throw new Error("D1RestAdapter.raw() is not implemented: no caller in the bot path uses it (see docs/WORKER_DEPENDENCIES_INVENTORY.md)");
  }

  private async query(sql: string, params: unknown[]): Promise<CloudflareQueryResultEntry> {
    let lastErr: unknown;
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        return await this.queryOnce(sql, params);
      } catch (err) {
        lastErr = err;
        if (err instanceof D1RestError && !err.retryable) throw err;
        if (attempt < MAX_RETRIES - 1) {
          await sleep(RETRY_BASE_DELAY_MS * 2 ** attempt);
        }
      }
    }
    throw lastErr;
  }

  private async queryOnce(sql: string, params: unknown[]): Promise<CloudflareQueryResultEntry> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await fetch(this.endpoint, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ sql, params }),
        signal: controller.signal,
      });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        throw new D1RestError(`D1 REST request timed out after ${this.timeoutMs}ms`, true);
      }
      throw new D1RestError(`D1 REST network error: ${err instanceof Error ? err.message : String(err)}`, true);
    } finally {
      clearTimeout(timeout);
    }

    if (response.status === 401) {
      throw new D1RestError("D1 REST: unauthorized (invalid or expired API token)", false);
    }
    if (response.status === 403) {
      throw new D1RestError("D1 REST: forbidden (API token missing D1 edit scope on this database)", false);
    }
    if (response.status === 429) {
      throw new D1RestError("D1 REST: rate limited", true);
    }
    if (response.status >= 500) {
      throw new D1RestError(`D1 REST: Cloudflare server error (HTTP ${response.status})`, true);
    }
    if (!response.ok) {
      const body = await safeReadText(response);
      throw new D1RestError(`D1 REST: HTTP ${response.status}: ${body}`, false);
    }

    let body: CloudflareQueryResponse;
    try {
      body = (await response.json()) as CloudflareQueryResponse;
    } catch {
      throw new D1RestError("D1 REST: response was not valid JSON", true);
    }

    if (!body.success) {
      const message = (body.errors ?? []).map((e) => e.message).join("; ") || "unknown D1 error";
      // WHY: throwIfQuota() in db.ts regex-matches the error message for
      // D1's free-tier quota wording - passed through verbatim so that
      // detection keeps working against the REST error text too.
      throw new D1RestError(message, false);
    }

    const entry = body.result?.[0];
    if (!entry) {
      throw new D1RestError("D1 REST: response had no result entry", false);
    }
    if (!entry.success) {
      throw new D1RestError("D1 REST: query reported failure with no error detail", false);
    }
    return entry;
  }
}

async function safeReadText(response: Response): Promise<string> {
  try {
    return (await response.text()).slice(0, 500);
  } catch {
    return "<unreadable body>";
  }
}

export class D1RestError extends Error {
  readonly retryable: boolean;
  constructor(message: string, retryable: boolean) {
    super(message);
    this.name = "D1RestError";
    this.retryable = retryable;
  }
}
