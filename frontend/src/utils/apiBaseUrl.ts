/**
 * T-34 / Bug 4（P0）：API 基地址的**归一化**。
 *
 * 为什么是一个独立的纯函数：`config.ts` 读的是 `import.meta.env`，在 Node 里是 `undefined`
 * 一 import 就抛；把这个判断剥出来，`tests/api-base-url.test.mjs` 才能直接 import 并断言行为
 * （与 T-49 的 `questionTags` 同一手法）。
 *
 * 规则：
 *   · 未配置 / 空串 / 全空白 → `''` —— **相对路径**，请求打到当前页面的 origin；
 *   · 去掉末尾斜杠 —— 调用方统一按 `API_BASE_URL + '/api/...'` 拼接，多一个斜杠会变成
 *     `//api/...`（某些反代/CDN 会把 `//` 当成协议相对 URL 或直接 404）。
 */

export function normalizeApiBaseUrl(raw: string | undefined | null): string {
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim();
  if (!trimmed) return '';
  return trimmed.replace(/\/+$/, '');
}
