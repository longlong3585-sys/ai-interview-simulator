/**
 * T-34 / Bug 4（P0）：API 基地址**默认是相对路径**（空串）。
 *
 * 修复前这里硬编码了一个**本机绝对地址**（`http` + 本机回环 + 8000 端口），带来三个
 * 必然的线上故障：
 *   ① **混合内容**：页面走 HTTPS 时，浏览器直接拦掉所有 `http://` 请求 —— 按钮全按不动；
 *   ② **跨域**：换成真实域名后，前端 origin 与那个本机地址不是同源，请求被 CORS 挡下；
 *   ③ **构建期固化的 host**：地址被 Vite 内联进产物，换环境必须重新构建前端。
 *
 * 现在：默认留空 → 所有请求走**当前页面的 origin**（`/api/...`），
 *   开发期由 Vite `server.proxy` 转发到后端（见 `vite.config.ts`），
 *   生产期由 Nginx / 后端同源托管（`docs/03-tasks.md` 的 T-53 / T-54）。
 * 只有"前后端确实不同源"时才需要填 `VITE_API_BASE_URL`（见 `.env.example`）。
 *
 * 注意：本文件连注释里都**不写那个旧地址的字面量** —— 否则未压缩的构建产物
 * （tsc 产物、`build.minify=false`）里依然 grep 得到它，验收标准就成了"靠压缩器兜底"。
 */

import { normalizeApiBaseUrl } from './utils/apiBaseUrl';

export const API_BASE_URL = normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL);
