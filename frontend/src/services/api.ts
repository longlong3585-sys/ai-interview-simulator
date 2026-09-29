/**
 * T-36（api 层收敛 / ADR-016 前置）：**全库唯一的 HTTP 出口**。
 *
 * 收敛前：18 处裸 `fetch` 散在 5 个文件里，每个调用点各自拼 `${API_BASE_URL}`、
 * 各自塞 `Authorization`、各自判断 `res.ok`。后果是同一类缺陷要修 N 遍：
 *   · 401 处处不同 —— 有的登出、有的只 console.error、有的把 401 当"空数据"；
 *   · 令牌续期（响应头 `X-Refreshed-Token`）根本没有落点，无人读、无人存；
 *   · 新增端点极易漏带令牌，且类型系统完全不报错（都是字符串拼接）。
 *
 * 收敛后：**除本文件外，`src/` 下不允许出现任何 `fetch(`**（契约测试守死）。
 * 本文件统一承担四件事：
 *   ① URL 解析（相对基地址 → 当前 origin；绝对地址原样放行）；
 *   ② `Authorization: Bearer <token>` 注入（公开路径除外，见 `isPublicApiPath`）；
 *   ③ **401 → 统一登出**（经 `registerUnauthorizedHandler` 注入，见下）；
 *   ④ **`X-Refreshed-Token` → 集中刷新 localStorage 里的 token**。
 *
 * 为什么登出用"注入回调"而不是 `import { useAuth }`：
 * `auth/AuthContext.tsx` → `main.tsx` → …… 任何一处 import 本文件都会构成运行时环。
 * 因此本模块**不认识 React**，只暴露一个注册口；`main.tsx` 在挂载入口把
 * `signOut()` 注册进来。这样"401 统一登出"是**结构上**成立的，而不是靠调用点自觉。
 */

import { API_BASE_URL } from '../config';
import {
  ApiError,
  applyRefreshedToken,
  isAbsoluteExpiryResponse,
  isPublicApiPath,
  isUnauthorizedError,
  readRefreshedToken,
  readTokenExpiredReason,
} from './authResponse';
import type { TokenStorage } from './authResponse';

// 统一层对外**转发**认证响应侧的全部常量与判定入口（上层只需要认识 `services/api` 一个模块）。
// 用 `export *` 而不是逐项列举：`ApiError` / `isPublicApiPath` 等既被本文件 import 使用、
// 又要对外可见，逐项写会与上面的 import 撞名。
export * from './authResponse';

/** 令牌读取（与 `AuthContext` 的 `AUTH_STORAGE_KEYS` / `authResponse.AUTH_TOKEN_STORAGE_KEY` 同源）。 */
const getToken = (): string | null => {
  try {
    return localStorage.getItem('token');
  } catch {
    // 隐私模式 / 沙箱里 localStorage 可能直接抛 —— 降级为"无令牌"，
    // 不能让一次读取失败把整个应用打成白屏（与 AuthContext 同一取舍）。
    return null;
  }
};

/** 令牌变更的订阅者（`AuthContext` 的 `signIn` 用它同步内存态，T-50 才用得上）。 */
type TokenListener = (token: string) => void;
const tokenListeners = new Set<TokenListener>();

/** 订阅续期令牌；返回退订函数。 */
export function onTokenRefreshed(listener: TokenListener): () => void {
  tokenListeners.add(listener);
  return () => tokenListeners.delete(listener);
}

/**
 * `X-Refreshed-Token` 的**集中处理**（不变量 ②）。
 *
 * 刻意写成"依赖注入"形态（storage / listeners 由参数传入，默认取真实实现）：
 * 这样 `tests/api-convergence.test.mjs` 能在 Node 里给一个假的 storage
 * **真跑一遍续期路径**，而不是靠 grep 断言"看起来读过头了"。
 *
 * 顺序：先落存储、再通知订阅者。反过来会出现"订阅者拿到新令牌、存储却还是旧的"。
 * 写入失败（隐私模式 / 配额满）返回 false，且**不抛** —— 续期失败不该把一次正常请求打成异常。
 */
export function handleRefreshedToken(
  response: Response,
  storage: TokenStorage = localStorage,
  listeners: Iterable<TokenListener> = tokenListeners,
): boolean {
  const refreshed = readRefreshedToken(response);
  if (!refreshed) return false;
  const persisted = applyRefreshedToken(storage, refreshed);
  for (const listener of listeners) listener(refreshed);
  return persisted;
}

/** 401 时的统一登出入口。默认是"未接线"的空实现 —— 由 `main.tsx` 注册。 */
type UnauthorizedHandler = () => void;
let unauthorizedHandler: UnauthorizedHandler | null = null;

/**
 * 注册 401 统一处理（`main.tsx` 传入 `AuthContext` 的 `signOut`）。
 *
 * 之所以由外部注册：本模块不能 import React（会成环，见文件头）。
 * `main.tsx` 若不注册，契约测试会失败 —— "忘了接线"必须是一个**响亮的失败**，
 * 而不是一个静默的"401 后界面还装作已登录"。
 */
export function registerUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

/** 当前是否已接线（契约测试与排障用）。 */
export function hasUnauthorizedHandler(): boolean {
  return typeof unauthorizedHandler === 'function';
}

/** 触发统一登出。**幂等**：一次请求只登出一次，重复调用无副作用。 */
export function notifyUnauthorized(): void {
  if (unauthorizedHandler) unauthorizedHandler();
}

/** 相对基地址 + 相对路径的拼接（`API_BASE_URL` 默认空串 = 当前 origin）。 */
export function resolveApiUrl(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  if (API_BASE_URL && url.startsWith(API_BASE_URL)) return url;
  return `${API_BASE_URL}${url}`;
}

/**
 * 出站处理：注入令牌、去掉会让 multipart 边界丢失的 Content-Type。
 *
 * `body instanceof FormData` 时必须删掉 `Content-Type` —— 否则浏览器拿不到
 * `boundary=`，后端会直接 400（原实现只在 `authFetch` 里做了这件事，
 * 于是每个新写的上传点都要记得手写一遍）。
 */
function prepareRequest(url: string, options: RequestInit = {}): RequestInit {
  const headers: Record<string, string> = { ...(options.headers as Record<string, string> | undefined) };
  if (!isPublicApiPath(url)) {
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
  }
  if (options.body instanceof FormData) delete headers['Content-Type'];
  return { ...options, headers };
}

/**
 * 入站处理：**先续期、后判 401**。
 *
 * 顺序很关键：T-50 的滑动续期会在"令牌刚过期但仍在绝对上限内"时回 401 之外的成功码
 * 并带上新令牌；若先判 401 再读头，重启的会话会被当成过期会话登出。
 */
function observeResponse(response: Response): Response {
  handleRefreshedToken(response);
  return response;
}

/** 统一出口：所有请求（含公开请求）都必须经过这里。 */
export async function request(url: string, options: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(resolveApiUrl(url), prepareRequest(url, options));
  } catch (err) {
    // 网络层失败也要有类型（原来各调用点只能 catch 一个裸 Error 再猜是不是权限问题）。
    throw new ApiError(0, url, `Network Error: ${(err as Error)?.message ?? String(err)}`);
  }
  observeResponse(response);
  if (response.status === 401) {
    // T-50 / ADR-016 R-9：**绝对上限已到**是一种特殊的 401 ——
    // 服务端会话与面试进度都还在，此时统一登出会把用户"能恢复的进度"变成"必须先登录"。
    // 因此：先提示（抛一个带标记的错误让上层展示文案），**不调用** notifyUnauthorized()，
    // 本地会话照旧保留到用户自己重新登录。
    if (isAbsoluteExpiryResponse(response)) {
      throw new ApiError(
        401,
        url,
        'SessionAbsoluteExpired',
        readTokenExpiredReason(response),
      );
    }
    // 其余 401（令牌无效 / 被吊销 / 账号被禁用）语义不变：**先登出，再抛**。
    // 抛出的错误保留 `message === 'Unauthorized'`，
    // 因为 `useInterviewChat` / `resumeUpload` 里既有的判定依赖这个字符串。
    notifyUnauthorized();
    throw new ApiError(401, url, 'Unauthorized');
  }
  return response;
}

/** 该 `Response` 是否代表"令牌已失效"（不变量 ① 的判定入口，供上层按需复用）。 */
export function isUnauthorizedResponse(response: Response): boolean {
  return isUnauthorizedError({ status: response.status });
}

/**
 * 带令牌的请求（兼容既有调用点：`authFetch('/api/x')` 与 `authFetch(\`${API_BASE_URL}/api/x\`)` 都成立）。
 * 现在的令牌注入与 401 处理都在 `request()` 里，因此这里只是语义别名。
 */
export async function authFetch(url: string, options: RequestInit = {}): Promise<Response> {
  return request(url, options);
}

/** 公开请求（登录/注册/验证码/题库）：不带令牌，**401 不触发登出**。 */
export async function publicFetch(url: string, options: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(resolveApiUrl(url), prepareRequest(url, options));
  } catch (err) {
    throw new ApiError(0, url, `Network Error: ${(err as Error)?.message ?? String(err)}`);
  }
  return observeResponse(response);
}

/** JSON 请求体：公开请求与已认证请求共用同一套 Content-Type 规则。 */
function jsonInit(method: string, body: unknown, options?: RequestInit): RequestInit {
  const headers: Record<string, string> = { ...(options?.headers as Record<string, string> | undefined) };
  const isForm = body instanceof FormData;
  const isUrlEncoded = body instanceof URLSearchParams;
  if (body !== undefined && body !== null && !isForm && !isUrlEncoded) {
    headers['Content-Type'] = 'application/json';
  }
  let encoded: BodyInit | undefined;
  if (body === undefined || body === null) encoded = undefined;
  else if (isForm || isUrlEncoded || typeof body === 'string') encoded = body as BodyInit;
  else encoded = JSON.stringify(body);
  return { ...options, method, headers, body: encoded };
}

export async function apiGet(url: string, options?: RequestInit): Promise<Response> {
  return request(url, { ...options, method: 'GET' });
}

export async function apiPost(url: string, body?: unknown, options?: RequestInit): Promise<Response> {
  return request(url, jsonInit('POST', body, options));
}

export async function apiPut(url: string, body?: unknown, options?: RequestInit): Promise<Response> {
  return request(url, jsonInit('PUT', body, options));
}

export async function apiPatch(url: string, body?: unknown, options?: RequestInit): Promise<Response> {
  return request(url, jsonInit('PATCH', body, options));
}

export async function apiDelete(url: string, options?: RequestInit): Promise<Response> {
  return request(url, { ...options, method: 'DELETE' });
}

/** 公开版 JSON 请求（登录 / 注册 / 验证码 / 题库）。 */
export async function publicPost(url: string, body?: unknown, options?: RequestInit): Promise<Response> {
  return publicFetch(url, jsonInit('POST', body, options));
}

/** 公开版 GET。 */
export async function publicGet(url: string, options?: RequestInit): Promise<Response> {
  return publicFetch(url, { ...options, method: 'GET' });
}
