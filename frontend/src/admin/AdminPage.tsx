/**
 * T-44 / T-45：管理后台的**路由页**。
 *
 * 它只是"路由出口 + 布局"的装配件：
 *   · 权限判断交给 `<RequireAdmin>`（路由层守卫，见 src/auth/RequireAuth.tsx）；
 *   · 面板本体在 src/admin/AdminPanel.tsx（T-45 从 App.tsx 拆出）。
 * 因此本文件**不含**任何业务状态 —— 这正是 T-45~T-47 想要的形状：
 * `App.tsx` 逐步降为路由装配，页面各归其位。
 */

import { useNavigate } from 'react-router-dom';
import AdminPanel from './AdminPanel';
import { RequireAdmin } from '../auth/RequireAuth';
import { useAuth } from '../auth/AuthContext';

export default function AdminPage() {
  const { token } = useAuth();
  const navigate = useNavigate();

  return (
    <RequireAdmin>
      <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-slate-50">
        <main className="max-w-6xl mx-auto px-4 py-8 animate-fade-in">
          <div className="bg-white rounded-2xl shadow-float p-6">
            <div className="flex justify-between items-center mb-4">
              <h1 className="text-2xl font-bold">⚙️ 管理后台</h1>
              <button onClick={() => navigate('/')} className="btn-ghost text-sm px-3 py-2">
                返回面试页
              </button>
            </div>
            <AdminPanel token={token} />
          </div>
        </main>
      </div>
    </RequireAdmin>
  );
}
