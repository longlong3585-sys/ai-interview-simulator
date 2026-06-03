import { API_BASE_URL } from '../config';

const getToken = () => localStorage.getItem('token');

function resolveUrl(url: string): string {
  if (url.startsWith('http://') || url.startsWith('https://')) {
    return url;
  }
  return `${API_BASE_URL}${url}`;
}

export async function authFetch(url: string, options: RequestInit = {}) {
  const currentToken = getToken();
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> || {}),
    'Authorization': `Bearer ${currentToken}`
  };
  if (options.body instanceof FormData) {
    delete headers['Content-Type'];
  }
  const response = await fetch(resolveUrl(url), { ...options, headers });
  if (response.status === 401) {
    throw new Error('Unauthorized');
  }
  return response;
}

export async function apiGet(url: string, options?: RequestInit) {
  return authFetch(url, { ...options, method: 'GET' });
}

export async function apiPost(url: string, body?: any, options?: RequestInit) {
  const headers: Record<string, string> = {};
  if (!(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }
  return authFetch(url, {
    ...options,
    method: 'POST',
    headers: { ...headers, ...(options?.headers as Record<string, string> || {}) },
    body: body instanceof FormData ? body : (body ? JSON.stringify(body) : undefined),
  });
}

export async function apiPatch(url: string, body?: any, options?: RequestInit) {
  return authFetch(url, {
    ...options,
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(options?.headers as Record<string, string> || {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
}

export async function apiDelete(url: string, options?: RequestInit) {
  return authFetch(url, { ...options, method: 'DELETE' });
}

export async function publicFetch(url: string, options?: RequestInit) {
  const response = await fetch(resolveUrl(url), options);
  return response;
}
