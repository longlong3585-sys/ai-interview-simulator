/**
 * T-36（api 层收敛）：**跨请求的两条认证规则** + 公开路径口径，全部收在这一个零依赖纯模块里。
 *
 * 为什么单独一个文件（与 T-34 的 `utils/apiBaseUrl.ts` 同一手法）：
 * `services/api.ts` 依赖 `import.meta.env`（经 `config.ts`）与全局 `fetch`，
 * 在 Node 里 import 就抛；把"读哪个响应头""哪些路径算公开"这类**判断**剥出来，
 * 契约测试就能在 Node 里直接 import 并断言真实行为 —— 而不是靠 grep 猜。
 *
 * 本文件约束：**不得 import 任何东西**（包括类型 import 之外的相对路径），
 * 否则 `node --test` 会因为解析 `.ts` 依赖链失败。契约测试有断言守着这条。
 *
 * 覆盖的两条不变量：
 *   ① `401` → 由 `api.ts` 统一登出（本文件给出 `ApiError` 与 `isUnauthorizedError` 判定）；
 *   ② `X-Refreshed-Token` 响应头 → 由 `api.ts` 集中读取并刷新 localStorage 里的 token。
 */

/** 后端续期时回传新令牌用的响应头（T-50 / ADR-016 的服务端侧会开始下发）。 */
export const REFRESHED_TOKEN_HEADER = 'X-Refreshed-Token';

// 显式写 `.ts`：`node --test` 直接 import 本文件时，ESM 解析器要求给全扩展名
// （tsconfig 已开 `allowImportingTsExtensions` + `noEmit`，Vite/tsc 都接受）。
import { AUTH_TOKEN_STORAGE_KEY } from '../auth/authStorage.ts';

/** 令牌在 localStorage 里的键。**唯一来源是 `auth/authStorage.ts`**（T-40 的持久化清单），
 *  这里转出以免出现两份写法。依赖方向 `services/authResponse` → `auth/authStorage`，不成环。 */
export { AUTH_TOKEN_STORAGE_KEY };

/** 无需 `Authorization` 的公开端点前缀（登录/注册/验证码/题库）。 */
export const PUBLIC_API_PATH_PREFIXES: readonly string[] = [
  '/api/captcha',
  '/api/login',
  '/api/register',
  '/api/question_bank',
];

/**
 * 该 URL 是否属于"公开端点"（不带令牌、401 也不触发登出）。
 *
 * 之所以按**前缀**判定：题库与验证码将来可能带子路径（`/api/question_bank/1`），
 * 而登录/注册是精确端点，多一个 `/api/login_xxx` 若被误判也会有测试兜住。
 */
export function isPublicApiPath(url: string): boolean {
  if (typeof url !== 'string') return false;
  // 只看路径部分：调用方可能传绝对地址或带查询串。
  const withoutOrigin = url.replace(/^https?:\/\/[^/]*/i, '');
  const pathOnly = withoutOrigin.split('?')[0].split('#')[0];
  return PUBLIC_API_PATH_PREFIXES.some((p) => pathOnly === p || pathOnly.startsWith(`${p}/`));
}

/**
 * 从响应里取续期令牌。**没有任何响应头时返回 `null`** —— 绝不能把
 * `headers.get()` 返回的 `null` 当成令牌写进 localStorage（那会立刻把用户登出）。
 */
export function readRefreshedToken(response: { headers?: { get(name: string): string | null } | null } | null | undefined): string | null {
  const raw = response?.headers?.get(REFRESHED_TOKEN_HEADER);
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  return trimmed ? trimmed : null;
}

/** 可注入的存储接口（测试里塞一个假的，生产用 localStorage）。 */
export interface TokenStorage {
  setItem(key: string, value: string): void;
}

/**
 * 把续期令牌写回存储。返回是否真的写入 —— 调用方据此决定要不要通知别处刷新状态。
 * 写入失败（隐私模式 / 配额满）不抛出：令牌已随本次请求的响应到达，
 * 为了"续期"把一次正常请求打成异常是本末倒置。
 */
export function applyRefreshedToken(storage: TokenStorage, token: string | null): boolean {
  if (typeof token !== 'string' || !token) return false;
  try {
    storage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
    return true;
  } catch {
    return false;
  }
}

/**
 * 认证/网络失败的统一错误类型。
 *
 * `message` 保持 `'Unauthorized'`：`useInterviewChat` / `resumeUpload` 里既有的
 * `err.message === 'Unauthorized'` 判定必须继续成立（T-36 只收敛出口，不改语义）。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly url: string;

  constructor(status: number, url: string, message?: string) {
    super(message ?? (status === 401 ? 'Unauthorized' : `HTTP ${status}`));
    this.name = 'ApiError';
    this.status = status;
    this.url = url;
  }
}

/** 401 判定（不依赖跨模块 `instanceof`：打包后多份类定义会让 instanceof 失效）。 */
export function isUnauthorizedError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false;
  const anyErr = err as { status?: unknown; message?: unknown };
  if (anyErr.status === 401) return true;
  return anyErr.message === 'Unauthorized';
}
