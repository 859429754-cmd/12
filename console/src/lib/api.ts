export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
  retries?: number;
  retryDelayMs?: number;
}

export class ApiError extends Error {
  readonly status: number;
  readonly path: string;
  readonly body: unknown;

  constructor(path: string, status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.path = path;
    this.status = status;
    this.body = body;
  }
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, ms));

export async function api<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { timeoutMs = 8000, retries = 1, retryDelayMs = 500, ...requestOptions } = options;
  let lastError: unknown;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        ...requestOptions,
        signal: controller.signal,
        headers: { "Content-Type": "application/json", ...(requestOptions.headers || {}) },
      });
      const text = await response.text();
      const body = parseBody(text);
      if (!response.ok) {
        throw new ApiError(path, response.status, extractMessage(body, response.status), body);
      }
      return body as T;
    } catch (error) {
      lastError = normalizeFetchError(path, error);
      if (!shouldRetry(error) || attempt >= retries) break;
      await sleep(retryDelayMs * (attempt + 1));
    } finally {
      window.clearTimeout(timer);
    }
  }

  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

function parseBody(text: string): unknown {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

function extractMessage(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const item = body as Record<string, unknown>;
    if (item.detail === "console_auth_not_configured") {
      return "控制台账号未配置，已拒绝访问。请先在 .env.runtime 配置管理员账号密码。";
    }
    if (item.detail === "auth_required") {
      return "请先登录 AI 量化控制台账号。";
    }
    if (item.detail === "permission_denied") {
      return "当前账号没有执行该操作的权限。";
    }
    return String(item.detail || item.message || `HTTP ${status}`);
  }
  return `HTTP ${status}`;
}

function normalizeFetchError(path: string, error: unknown): Error {
  if (error instanceof ApiError) return error;
  if (error instanceof DOMException && error.name === "AbortError") {
    return new ApiError(path, 0, `Request timed out: ${path}`, { timeout: true });
  }
  if (error instanceof TypeError) {
    return new ApiError(path, 0, `Cannot reach backend: ${path}. Check whether the Python API is running.`, {
      network: true,
    });
  }
  return error instanceof Error ? error : new Error(String(error));
}

function shouldRetry(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status === 0 || error.status === 429 || error.status >= 500;
  }
  return error instanceof TypeError || (error instanceof DOMException && error.name === "AbortError");
}
