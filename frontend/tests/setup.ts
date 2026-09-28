/**
 * Vitest 全局 setup（T-03）
 *
 * 目前只做一件事：每个用例后卸载已渲染的组件，避免 DOM 在用例间泄漏。
 *
 * 注意：未引入 `@testing-library/jest-dom`（本机无法安装）。
 * 若将来安装，可在此处加 `import '@testing-library/jest-dom/vitest';`
 * 以获得 toBeInTheDocument() 等断言。
 */

import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});
