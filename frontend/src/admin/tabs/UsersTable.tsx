/**
 * T-45：管理后台「用户管理」页签。
 *
 * 从 `AdminPanel.tsx` 拆出；`users` / `loading` 归本组件，
 * 因此"禁用/删除/重置密码"引发的刷新只重渲染这张表。
 */

import { useEffect, useState } from 'react';
import { authFetch } from '../../services/api';
import { API_BASE_URL } from '../../config';

export default function UsersTable({ token }: { token: string | null }) {
  const [users, setUsers] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchUsers = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const res = await authFetch(`${API_BASE_URL}/api/admin/users`);
      const data = await res.json();
      setUsers(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!token) return;
    fetchUsers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const resetPassword = async (userId: number, newPassword: string) => {
    try {
      const res = await authFetch(`${API_BASE_URL}/api/admin/users/${userId}/reset_password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ new_password: newPassword }).toString(),
      });
      if (res.ok) {
        alert('密码重置成功');
      } else {
        const err = await res.json();
        alert(err.detail || '重置失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const toggleActive = async (userId: number) => {
    try {
      const res = await authFetch(`${API_BASE_URL}/api/admin/users/${userId}/toggle_active`, {
        method: 'PATCH',
      });
      if (res.ok) {
        fetchUsers();
      } else {
        const err = await res.json();
        alert(err.detail || '操作失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const deleteUser = async (userId: number) => {
    try {
      const res = await authFetch(`${API_BASE_URL}/api/admin/users/${userId}`, { method: 'DELETE' });
      if (res.ok) {
        fetchUsers();
      } else {
        const err = await res.json();
        alert(err.detail || '删除失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  if (loading) return <p className="text-center py-4">加载中...</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-gray-100">
            <th className="p-2 text-left">ID</th>
            <th className="p-2 text-left">用户名</th>
            <th className="p-2 text-left">角色</th>
            <th className="p-2 text-left">邮箱</th>
            <th className="p-2 text-left">状态</th>
            <th className="p-2 text-left">创建时间</th>
            <th className="p-2 text-left">操作</th>
           </tr>
        </thead>
        <tbody>
          {users.map(u => (
            <tr key={u.id} className="border-b">
              <td className="p-2">{u.id}</td>
              <td className="p-2">{u.username}</td>
              <td className="p-2">
                <span className={`px-2 py-1 rounded text-xs ${u.role === 'admin' ? 'bg-red-100 text-red-700' : 'bg-blue-100 text-blue-700'}`}>{u.role}</span>
              </td>
              <td className="p-2">{u.email || '-'}</td>
              <td className="p-2">
                <span className={`px-2 py-1 rounded text-xs ${u.is_active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-700'}`}>{u.is_active ? '活跃' : '禁用'}</span>
              </td>
              <td className="p-2">{new Date(u.created_at).toLocaleString()}</td>
              <td className="p-2">
                <div className="flex gap-1 flex-wrap">
                  <button
                    onClick={() => {
                      const newPwd = prompt('请输入新密码（至少8位）');
                      if (newPwd && newPwd.length >= 8) {
                        resetPassword(u.id, newPwd);
                      } else if (newPwd) {
                        alert('密码长度至少8位');
                      }
                    }}
                    className="bg-blue-500 text-white px-2 py-1 rounded text-xs"
                  >
                    重置密码
                  </button>
                  {u.role !== 'admin' && (
                    <>
                      <button
                        onClick={() => toggleActive(u.id)}
                        className={`px-2 py-1 rounded text-xs ${u.is_active ? 'bg-yellow-500 text-white' : 'bg-green-500 text-white'}`}
                      >
                        {u.is_active ? '禁用' : '启用'}
                      </button>
                      <button
                        onClick={() => {
                          if (confirm(`确定删除用户 ${u.username}？`)) {
                            deleteUser(u.id);
                          }
                        }}
                        className="bg-red-500 text-white px-2 py-1 rounded text-xs"
                      >
                        删除
                      </button>
                    </>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {users.length === 0 && <p className="text-center py-4 text-gray-500">暂无用户</p>}
    </div>
  );
}
