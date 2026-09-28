/**
 * Vitest + React Testing Library 配置（T-03）
 *
 * ⚠️ 当前状态（2026-09-28）：本机 **npm registry 不可达**（与 PyPI 同一故障：
 *    DNS/TCP 通但 TLS 握手被重置），因此 vitest / @testing-library/react /
 *    jsdom 均**无法安装**，本文件与 tests/*.test.tsx 目前处于"待激活"状态。
 *
 * 本文件刻意放在 `src/` **之外** —— `tsconfig.app.json` 的 include 是 ["src"]，
 * 若把这些文件放进 src，`npm run build`（tsc -b）会因缺少 vitest 类型而失败。
 *
 * 激活方式（网络恢复后执行一次即可，无需改动任何测试代码）：
 *   cd frontend
 *   npm install -D vitest @testing-library/react @testing-library/dom jsdom
 *   npm run test:run
 *
 * 当前可运行的测试是零依赖的契约测试（node:test）：
 *   npm run test:node
 */

import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    // 组件测试需要 DOM；jsdom 是安装清单中的依赖
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    // 只收 .ts/.tsx；.mjs 契约测试由 node:test 运行（`npm run test:node`）
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    // 与 npm run build 的产物目录保持一致
    exclude: ['node_modules/**', 'dist/**'],
  },
});
