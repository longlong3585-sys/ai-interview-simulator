/**
 * T-45：管理员面板（T-45 从 `App.tsx` 拆出，原 `AdminPanelContent`，App.tsx 21~456 行）。
 *
 * 拆分动机：`App.tsx` 当时已 2527 行，一个 436 行的管理后台和面谈主流程挤在同一个文件里。
 *
 * 本次同时收掉 T-44 点名的 **FR-11.3 条件 Hook**：
 *   修复前第一行就是
 *     `if (!token) return <div>请先登录</div>;`
 *   —— 它挡在 7 个 `useState` **之前**。只要令牌从"有"变"无"（登出、401 被清），
 *   本次渲染就会少调用 7 个 Hook，React 直接抛
 *   "Rendered fewer hooks than expected"。
 *   现在：Hook **全部无条件调用**，早退只发生在 **JSX 出口**（见文件末尾 `if (!token)`），
 *   顺序恒定 —— 令牌由有到无只是"渲染另一棵树"，不再抛错。
 *
 * 三个页签已各自独立成组件（`tabs/`）：
 *   拆完面板本体后本文件仍有 419 行，越过 T-45~T-47 的「单文件 ≤400 行」预算；
 *   而三个页签原本共用 `loading` / `successMsg`，切页签会把上一个页签的
 *   "加载中…"/"✅ 更新成功"串到下一个页签上 —— 拆开同时修掉了这个串扰。
 *   本文件因此只剩：**守卫 + 页签壳**（约 80 行）。
 *
 * 顺带（T-36 口径）：管理员 API 调用一律走 `authFetch`（在各页签组件内），
 * 组件不再自己从 props 拼 `Authorization` 头。
 */

import { useState } from 'react';
import StatsDashboard from './tabs/StatsDashboard';
import UsersTable from './tabs/UsersTable';
import InterviewsTable from './tabs/InterviewsTable';

export interface AdminPanelProps {
  /**
   * 当前令牌。仍作为 prop 传入口（而不是组件内部 `useAuth()`）是刻意的：
   * 让"令牌缺失"成为一个**可被测试直接构造**的输入，
   * 契约测试据此断言"由有到无不抛错"。路由层（RequireAdmin）负责保证它非空。
   */
  token: string | null;
  /** 关闭按钮回调；不传则不渲染关闭按钮（作为整页路由时用不到）。 */
  onClose?: () => void;
}

type AdminTab = 'users' | 'interviews' | 'stats';

const TABS: Array<{ key: AdminTab; label: string }> = [
  { key: 'stats', label: '📊 仪表盘' },
  { key: 'users', label: '用户管理' },
  { key: 'interviews', label: '面试记录' },
];

export default function AdminPanel({ token, onClose }: AdminPanelProps) {
  // ---------------------------------------------------------------------------
  // T-44 / FR-11.3：Hook **无条件**调用，任何 return 都必须排在其后。
  // 这里只留一个页签状态 —— 数据与 loading 都下沉到各页签组件里了。
  // ---------------------------------------------------------------------------
  const [activeTab, setActiveTab] = useState<AdminTab>('stats');

  // ---------------------------------------------------------------------------
  // T-44 / FR-11.3：早退只允许出现在这里 —— 所有 Hook 之后。
  // 令牌由有变无时：Hook 数量不变，只是渲染成"请先登录"这一棵树。
  // ---------------------------------------------------------------------------
  if (!token) {
    return <div className="p-4 text-center text-gray-500">请先登录</div>;
  }

  return (
    <div>
      <div className="flex gap-2 mb-4 border-b">
        {TABS.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 ${activeTab === tab.key ? 'border-b-2 border-green-500 font-bold' : 'text-gray-500'}`}
          >
            {tab.label}
          </button>
        ))}
        {onClose && (
          <button onClick={onClose} className="ml-auto px-3 py-2 text-gray-500 hover:text-gray-700" aria-label="关闭管理后台">✕</button>
        )}
      </div>

      {activeTab === 'stats' && <StatsDashboard token={token} />}
      {activeTab === 'users' && <UsersTable token={token} />}
      {activeTab === 'interviews' && <InterviewsTable token={token} />}
    </div>
  );
}
