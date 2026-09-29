/**
 * T-36 / T-40：把 `AuthContext` 与统一的 HTTP 出口**双向接线**的桥。
 *
 * 两个方向（都是"只能在这里接"的环，不是随手一写的胶水）：
 *
 *   1. **出**：`services/api.ts` 必须在任何请求拿到 `401` 时统一登出，
 *      但它**不能** import `AuthContext`（`AuthContext` → `main.tsx` → `App` → hook →
 *      api.ts 会构成运行时环）。因此 api.ts 只暴露 `registerUnauthorizedHandler()`，
 *      由本组件把 `signOut` 注册进去。
 *
 *   2. **入**：T-36 在续期响应头处留了 `onTokenRefreshed()` 订阅口，
 *      但那时只写了 `localStorage`。T-40 起由 `applyRefreshedToken()` 把新令牌
 *      **回灌内存态** —— 于是"当前页面立刻用新令牌"不再需要等一次刷新。
 *
 * 另外顺带处理**跨标签页**：另一个标签页登录/登出会派发 `storage` 事件，
 * 这里把它转成 `restoreFromStorage()`，让本页跟着切换身份。
 *
 * 接线失败的表现：契约测试直接失败（`main.tsx` 必须挂载本组件）。刻意让它"响亮地失败"，
 * 而不是静默退化成一个 401 后界面仍装作已登录、续期只写盘不生效的应用。
 */

import { useEffect } from 'react';
import { useAuth } from './AuthContext';
import {
  AUTH_TOKEN_STORAGE_KEY,
  registerUnauthorizedHandler,
  onTokenRefreshed,
} from '../services/api';

export function AuthBridge() {
  const { signOut, applyRefreshedToken, restoreFromStorage } = useAuth();

  useEffect(() => {
    registerUnauthorizedHandler(signOut);
    // 卸载时摘掉：避免热更新后留着指向旧渲染的 `signOut` 闭包。
    return () => registerUnauthorizedHandler(null);
  }, [signOut]);

  useEffect(() => {
    // T-40：续期令牌回灌内存态（T-36 留的口子在这里兑现）。
    return onTokenRefreshed(applyRefreshedToken);
  }, [applyRefreshedToken]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      // `key === null` 表示整片存储被清空（`localStorage.clear()`），也要跟着还原。
      if (event.key !== null && event.key !== AUTH_TOKEN_STORAGE_KEY) return;
      restoreFromStorage();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [restoreFromStorage]);

  return null;
}
