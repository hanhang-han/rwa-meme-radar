import { API_BASE } from './client.js';

export class DeveloperApiError extends Error {
  constructor(status, message) {
    super(message || `HTTP ${status}`);
    this.name = 'DeveloperApiError';
    this.status = status;
  }
}

export async function developerRequest(path, { method = 'GET', body, csrfToken, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    const response = await fetch(`${API_BASE}developer/${path.replace(/^\//, '')}`, {
      method, headers, credentials: 'include', signal: controller.signal,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = typeof payload?.detail === 'string' ? payload.detail : null;
      throw new DeveloperApiError(response.status, detail);
    }
    return payload;
  } catch (error) {
    if (controller.signal.aborted) throw new DeveloperApiError(0, 'request-timeout');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const registerDeveloper = (email, inviteCode, password) =>
  developerRequest('register', { method: 'POST', body: { email, inviteCode, password } });
export const loginDeveloper = (email, password) =>
  developerRequest('login', { method: 'POST', body: { email, password } });
export const getDeveloperSession = () => developerRequest('me');
export const logoutDeveloper = (csrfToken) => developerRequest('logout', { method: 'POST', csrfToken });
export const listDeveloperKeys = () => developerRequest('keys');
export const createDeveloperKey = (name, csrfToken) => developerRequest('keys', { method: 'POST', body: { name }, csrfToken });
export const revokeDeveloperKey = (id, csrfToken) => developerRequest(`keys/${encodeURIComponent(id)}`, { method: 'DELETE', csrfToken });
export const getDeveloperUsage = () => developerRequest('usage');
