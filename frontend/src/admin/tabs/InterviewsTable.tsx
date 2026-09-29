/**
 * T-45：管理后台「面试记录」页签。
 *
 * 从 `AdminPanel.tsx` 拆出 —— 目的不是"换个地方堆代码"，而是切掉三处真实耦合：
 *   ① `loading` / `successMsg` 原本被三个页签共用：切页签时上一个页签的
 *      "加载中"和"✅ 更新成功"会串到下一个页签上；
 *   ② `interviews` 的增删改只在这个页签里发生，却让整个面板重渲染；
 *   ③ 报告详情弹窗的数据只在表格里用得上。
 * 现在这些状态都归本组件所有；对外只依赖 `token`。
 */

import { useEffect, useState } from 'react';
import { authFetch } from '../../services/api';
import ReportDetailModal, { type InterviewDetail } from '../ReportDetailModal';

export default function InterviewsTable({ token }: { token: string | null }) {
  const [interviews, setInterviews] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [successMsg, setSuccessMsg] = useState('');
  const [detailInterview, setDetailInterview] = useState<InterviewDetail | null>(null);

  const fetchInterviews = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const res = await authFetch('/api/admin/interviews');
      const data = await res.json();
      setInterviews(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!token) return;
    fetchInterviews();
    // 令牌变化时重新拉取；fetchInterviews 每次渲染都是新函数，故不入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const updateInterview = async (id: number, status: string, comment: string) => {
    try {
      const res = await authFetch(`/api/admin/interviews/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, admin_comment: comment }),
      });
      if (res.ok) {
        setInterviews(list => list.map(i => i.id === id ? { ...i, status, admin_comment: comment } : i));
        setSuccessMsg('✅ 更新成功');
        setTimeout(() => setSuccessMsg(''), 2000);
      } else {
        const err = await res.json();
        alert(err.detail || '更新失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const deleteInterview = async (id: number) => {
    if (!confirm('确定要删除这条面试记录吗？删除后不可恢复。')) return;
    try {
      const res = await authFetch(`/api/admin/interviews/${id}`, { method: 'DELETE' });
      if (res.ok) {
        fetchInterviews();
      } else {
        const err = await res.json();
        alert(err.detail || '删除失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  return (
    <div>
      {loading && <p className="text-center py-4">加载中...</p>}
      {successMsg && <p className="text-center py-2 text-green-600 font-medium transition-opacity">{successMsg}</p>}

      {!loading && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-100">
                <th className="p-2 text-left">ID</th>
                <th className="p-2 text-left">用户</th>
                <th className="p-2 text-left">岗位</th>
                <th className="p-2 text-left">得分</th>
                <th className="p-2 text-left">状态</th>
                <th className="p-2 text-left">评语</th>
                <th className="p-2 text-left">创建时间</th>
                <th className="p-2 text-left">操作</th>
               </tr>
            </thead>
            <tbody>
              {interviews.map(i => (
                <tr key={i.id} className="border-b hover:bg-gray-50">
                  <td className="p-2">{i.id}</td>
                  <td className="p-2">{i.username}</td>
                  <td className="p-2">{i.role}</td>
                  <td className="p-2">
                    {i.report ? (
                      <span className={`font-bold ${i.report.overall_score >= 7 ? 'text-green-600' : i.report.overall_score >= 4 ? 'text-yellow-600' : 'text-red-600'}`}>
                        {i.report.overall_score}
                      </span>
                    ) : '-'}
                  </td>
                  <td className="p-2">
                    <span className={`px-2 py-1 rounded text-xs ${
                      i.status === 'approved' ? 'bg-green-100 text-green-700' :
                      i.status === 'rejected' ? 'bg-red-100 text-red-700' :
                      'bg-yellow-100 text-yellow-700'
                    }`}>
                      {i.status === 'approved' ? '已通过' : i.status === 'rejected' ? '已拒绝' : '待审核'}
                    </span>
                  </td>
                  <td className="p-2">
                    <input
                      type="text"
                      value={i.admin_comment || ''}
                      onChange={e => {
                        const value = e.target.value;
                        setInterviews(list => list.map(item => item.id === i.id ? { ...item, admin_comment: value } : item));
                      }}
                      className="border rounded px-1 py-0.5 text-sm w-32"
                      placeholder="评语"
                    />
                  </td>
                  <td className="p-2">{new Date(i.created_at).toLocaleString()}</td>
                  <td className="p-2">
                    <div className="flex gap-1">
                      <button
                        onClick={() => setDetailInterview(i)}
                        className="bg-purple-500 text-white px-2 py-1 rounded text-xs hover:bg-purple-600"
                      >
                        详情
                      </button>
                      <select value={i.status} onChange={e => {
                        const value = e.target.value;
                        setInterviews(list => list.map(item => item.id === i.id ? { ...item, status: value } : item));
                      }} className="border rounded px-2 py-1 text-xs">
                        <option value="pending">待审核</option>
                        <option value="approved">通过</option>
                        <option value="rejected">拒绝</option>
                      </select>
                      <button
                        onClick={() => updateInterview(i.id, i.status, i.admin_comment || '')}
                        className="bg-blue-500 text-white px-2 py-1 rounded text-xs"
                      >
                        更新
                      </button>
                      <button
                        onClick={() => deleteInterview(i.id)}
                        className="bg-red-400 text-white px-2 py-1 rounded text-xs hover:bg-red-600"
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {interviews.length === 0 && <p className="text-center py-4 text-gray-500">暂无面试记录</p>}
        </div>
      )}

      {detailInterview && (
        <ReportDetailModal detail={detailInterview} onClose={() => setDetailInterview(null)} />
      )}
    </div>
  );
}
