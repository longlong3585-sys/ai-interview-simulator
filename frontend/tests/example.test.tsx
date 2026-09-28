/**
 * T-03 骨架示例：React 组件渲染测试（待激活）
 *
 * 本文件演示"组件测试怎么写"，尚未运行（vitest / RTL 未能安装，见 vitest.config.ts 头部）。
 * 网络恢复后 `npm run test:run` 即可执行。
 *
 * 刻意**不**依赖 App.tsx —— 它需要路由、localStorage 与 CSS，
 * 属于 T-40/T-44 完成前端重构后才能稳定测试的目标。
 * 这里用一个内联组件验证 harness 本身是通的。
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

function Greeting({ name }: { name: string }) {
  return <p>你好，{name}</p>;
}

describe('测试骨架自检', () => {
  it('能渲染组件并查询文本', () => {
    render(<Greeting name="面试官" />);
    expect(screen.getByText('你好，面试官')).toBeTruthy();
  });

  it('jsdom 环境可用（localStorage 存在）', () => {
    // 前端多处依赖 localStorage（token/role/username），
    // 若环境配置错误，这里会立刻暴露，而不是等到组件测试才失败。
    expect(typeof window.localStorage).toBe('object');
    window.localStorage.setItem('__probe__', '1');
    expect(window.localStorage.getItem('__probe__')).toBe('1');
    window.localStorage.removeItem('__probe__');
  });
});
