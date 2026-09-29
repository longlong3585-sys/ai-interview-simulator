/**
 * T-45：管理后台「仪表盘」页签（原 `AdminPanelContent` 的 stats 分支）。
 *
 * 拆出后 `AdminPanel.tsx` 只剩"页签壳 + 路由出口"，
 * 三个页签各自持有自己的数据与 loading，互不干扰。
 */

import { useEffect, useState } from 'react';
import { authFetch } from '../../services/api';
import { API_BASE_URL } from '../../config';

export default function StatsDashboard({ token }: { token: string | null }) {
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    (async () => {
      setLoading(true);
      try {
        const res = await authFetch(`${API_BASE_URL}/api/admin/stats`);
        const data = await res.json();
        if (alive) setStats(data);
      } catch (err) {
        console.error(err);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    // 卸载后丢弃迟到的响应，避免"切走页签后仍 setState"
    return () => {
      alive = false;
    };
  }, [token]);

  if (loading) return <p className="text-center py-4">加载中...</p>;
  if (!stats) return <p className="text-center py-4 text-gray-500">暂无统计数据</p>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-blue-50 p-4 rounded shadow">
          <h3 className="text-sm text-gray-500">总用户数</h3>
          <p className="text-2xl font-bold">{stats.total_users}</p>
        </div>
        <div className="bg-green-50 p-4 rounded shadow">
          <h3 className="text-sm text-gray-500">总面试次数</h3>
          <p className="text-2xl font-bold">{stats.total_interviews}</p>
        </div>
        <div className="bg-yellow-50 p-4 rounded shadow">
          <h3 className="text-sm text-gray-500">平均综合得分</h3>
          <p className="text-2xl font-bold">{stats.avg_overall_score}</p>
        </div>
        <div className="bg-purple-50 p-4 rounded shadow">
          <h3 className="text-sm text-gray-500">通过率</h3>
          <p className="text-2xl font-bold">{stats.pass_rate}%</p>
        </div>
      </div>
      <div className="bg-gray-50 p-4 rounded shadow">
        <h3 className="font-bold mb-2">近7天面试趋势</h3>
        <div className="flex items-end space-x-2 h-40">
          {stats.daily_interviews?.map((day: any) => (
            <div key={day.date} className="flex flex-col items-center flex-1">
              <div className="bg-blue-500 w-full rounded-t" style={{ height: `${Math.max(day.count * 20, 10)}px` }}></div>
              <span className="text-xs mt-1">{day.date.slice(5)}</span>
            </div>
          )) || <div className="text-center text-gray-500 w-full">暂无数据</div>}
        </div>
      </div>
    </div>
  );
}
