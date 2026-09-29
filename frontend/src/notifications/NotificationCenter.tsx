/**
 * T-47：消息中心面板（通知 + 面试历史），原 App.tsx 的 JSX 第 1252~1375 行。
 *
 * 只负责渲染：数据与交互全部来自 `useNotificationCenter()` 返回的 center 对象。
 * 逐字保留了两条已有修复：
 *   · `h.report` 可能为 `null`（T-10 / FR-6.4）—— 原先直接读 `h.report.overall_score`，
 *     一条 report 为 NULL 的历史记录就会让整页白屏；
 *   · `id={`record-${h.id}`}` 是"点通知 → 跳到对应记录并高亮"的锚点。
 */

import type { NotificationCenterApi } from './useNotificationCenter';

export function NotificationCenter({ center }: { center: NotificationCenterApi }) {
  const {
    history, notifications, unreadCount, expandedHistoryId, setExpandedHistoryId,
    highlightId, historyListRef, infoTab, setInfoTab,
    markAllRead, deleteNotification, clearAllNotifications, handleNotificationClick,
  } = center;

  return (
    <div className="mb-4 border rounded bg-white shadow">
      <div className="flex border-b">
        <button
          onClick={() => setInfoTab('notifications')}
          className={`flex-1 py-2.5 text-sm font-medium transition ${infoTab === 'notifications' ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50' : 'text-gray-500 hover:text-gray-700'}`}
        >
          🔔 通知 {unreadCount > 0 && <span className="inline-flex items-center justify-center bg-red-500 text-white text-xs rounded-full w-5 h-5 ml-1">{unreadCount}</span>}
        </button>
        <button
          onClick={() => setInfoTab('history')}
          className={`flex-1 py-2.5 text-sm font-medium transition ${infoTab === 'history' ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50' : 'text-gray-500 hover:text-gray-700'}`}
        >
          📋 面试历史
        </button>
      </div>

      {infoTab === 'notifications' && (
        <div className="p-3">
          <div className="flex justify-between items-center mb-2">
            <span className="text-xs text-gray-400">即时提醒</span>
            <div className="flex gap-2 text-xs">
              {unreadCount > 0 && <button onClick={markAllRead} className="text-blue-500 underline">全部已读</button>}
              {notifications.length > 0 && <button onClick={clearAllNotifications} className="text-red-400 underline">清空全部</button>}
            </div>
          </div>
          {notifications.length === 0 && <p className="text-center py-6 text-gray-400 text-sm">暂无通知，一切安好 🎉</p>}
          <ul className="space-y-1 max-h-64 overflow-y-auto">
            {notifications.map((n) => {
              const iconMap: Record<string, string> = {
                interview_approved: '✅',
                interview_rejected: '❌',
                interview_pending: '⏳',
                new_comment: '💬',
                system: '🔔',
              };
              return (
                <li
                  key={n.id}
                  onClick={() => handleNotificationClick(n)}
                  className={`p-3 rounded-xl cursor-pointer flex items-start gap-3 transition-all duration-200 border ${n.is_read ? 'bg-white border-slate-100' : 'bg-primary-50/60 border-primary-100 font-medium'} hover:shadow-sm`}
                >
                  <span className="text-lg mt-0.5 shrink-0">{iconMap[n.type] || '🔔'}</span>
                  <div className="flex-1 min-w-0">
                    <p className={`text-sm leading-relaxed ${n.is_read ? 'text-slate-600' : 'text-slate-900'}`}>{n.message}</p>
                    <p className="text-xs text-slate-400 mt-1">{new Date(n.created_at).toLocaleString()}</p>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); deleteNotification(n.id); }}
                    className="text-slate-300 hover:text-red-500 text-sm shrink-0 p-1 hover:bg-red-50 rounded-lg transition-colors"
                    title="删除"
                  >✕</button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {infoTab === 'history' && (
        <div className="p-3">
          <span className="text-xs text-slate-400 font-medium">面试记录</span>
          {history.length === 0 && <p className="text-center py-6 text-slate-400 text-sm">暂无面试记录</p>}
          <ul ref={historyListRef} className="space-y-2 mt-2 max-h-80 overflow-y-auto scroll-smooth">
            {history.map((h) => (
              <li
                key={h.id}
                id={`record-${h.id}`}
                onClick={() => setExpandedHistoryId(expandedHistoryId === h.id ? null : h.id)}
                className={`rounded-xl p-3 cursor-pointer transition-all duration-300 border ${highlightId === h.id ? 'bg-amber-50 border-amber-400 ring-2 ring-amber-300 scale-[1.02] shadow-md' : 'bg-white border-slate-100 hover:border-slate-200 hover:shadow-sm'} ${expandedHistoryId === h.id ? 'ring-2 ring-primary-300 border-primary-200 shadow-md' : ''}`}
              >
                <div className="flex justify-between items-start mb-1.5">
                  <div className="flex items-center gap-2">
                    <span className="w-6 h-6 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-[10px] font-bold">AI</span>
                    <p className="font-semibold text-sm text-slate-800">{h.role}</p>
                  </div>
                  <span className="text-xs text-slate-400">{new Date(h.created_at).toLocaleDateString()}</span>
                </div>
                <div className="flex items-center gap-3 text-sm">
                  {/* T-10 / FR-6.4：report 可能为 null（后端已容错返回 null）。
                      修复前这里直接访问 h.report.overall_score，
                      一条 report 为 NULL 的历史记录就会让整个页面白屏。 */}
                  {h.report ? (
                    <span className={`font-bold text-sm ${h.report.overall_score >= 7 ? 'text-emerald-600' : h.report.overall_score >= 4 ? 'text-amber-600' : 'text-red-600'}`}>
                      得分 {h.report.overall_score}/10
                    </span>
                  ) : (
                    <span className="text-xs text-slate-400">该记录无评估报告</span>
                  )}
                  {h.status && (
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${h.status === 'approved' ? 'bg-emerald-100 text-emerald-700' : h.status === 'rejected' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}`}>
                      {h.status === 'approved' ? '已通过' : h.status === 'rejected' ? '未通过' : '待审核'}
                    </span>
                  )}
                  <span className="text-xs text-slate-400 ml-auto">{expandedHistoryId === h.id ? '收起 ▲' : '展开 ▼'}</span>
                </div>
                {expandedHistoryId === h.id && (
                  <div className="mt-3 pt-3 border-t border-slate-100 text-xs space-y-2">
                    {h.report ? (
                      <>
                        <div className="flex gap-4">
                          <span className="text-slate-500">表达能力 <span className="font-bold text-slate-800">{h.report.expression_score}/10</span></span>
                          <span className="text-slate-500">技术深度 <span className="font-bold text-slate-800">{h.report.technical_score}/10</span></span>
                          <span className="text-slate-500">逻辑思维 <span className="font-bold text-slate-800">{h.report.logic_score}/10</span></span>
                        </div>
                        {h.report.details && <p className="text-slate-600 bg-slate-50 p-3 rounded-xl border border-slate-100 leading-relaxed">📋 {h.report.details}</p>}
                        {h.report.suggestion && <p className="text-amber-700 bg-amber-50 p-3 rounded-xl border border-amber-200 leading-relaxed">💡 {h.report.suggestion}</p>}
                      </>
                    ) : (
                      <p className="text-slate-400">该记录没有评估报告（可能因生成失败或数据异常）。</p>
                    )}
                    {h.admin_comment && <p className="text-blue-700 bg-blue-50 p-3 rounded-xl border border-blue-200 leading-relaxed">✏ 管理员评语：{h.admin_comment}</p>}
                  </div>
                )}
                {expandedHistoryId !== h.id && h.admin_comment && (
                  <p className="text-xs text-slate-400 mt-1.5 truncate">评语：{h.admin_comment}</p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
