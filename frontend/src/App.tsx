/**
 * T-46 / T-47：`App.tsx` 从 2189 行降到"路由出口 + 页面装配"。
 *
 * 拆分后的分工（每个文件 ≤400 行，验收脚本逐条核对）：
 *   · 面试主流程  → `interview/useInterviewSession.ts`（状态+逻辑）+ `interview/InterviewRoom.tsx`
 *   · 评估报告    → `report/ReportView.tsx`
 *   · 消息中心    → `notifications/useNotificationCenter.ts` + `notifications/NotificationCenter.tsx`
 *   · 个人中心    → `profile/ProfilePanel.tsx`（自持资料/头像/改密状态）
 *   · 登录注册    → `auth/AuthModal.tsx`
 *   · 题库弹窗    → `interview/QuestionBankModal.tsx`
 *
 * 本文件只剩：顶部导航、未登录落地页、管理员提示卡、以及"哪一屏"的装配。
 * 认证状态仍唯一来自 `AuthContext`（T-44 边界）；管理员仍由 `<Navigate to="/admin">` 接管；
 * 面试超时闭环（T-42）与报告标注（T-43）行为不变 —— 它们各自的契约测试原样通过。
 *
 * ⚠️ 注意：`if (isAdmin) return <Navigate …>` 必须留在**所有 Hook 之后**。
 * 这正是 FR-11.3（条件 Hook）在 T-44 里修掉的坑，不能让它在 App 里复现。
 */

import { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useAuth } from './auth/AuthContext';
import { AuthModal } from './auth/AuthModal';
import { ToastHost } from './components/Toast';
import { InterviewRoom } from './interview/InterviewRoom';
import { QuestionBankModal } from './interview/QuestionBankModal';
import { useInterviewSession } from './interview/useInterviewSession';
import { NotificationCenter } from './notifications/NotificationCenter';
import { useNotificationCenter } from './notifications/useNotificationCenter';
import { ProfilePanel } from './profile/ProfilePanel';
import { ReportView } from './report/ReportView';

// T-45：管理员面板已拆到 src/admin/AdminPanel.tsx，由路由 /admin
// （src/admin/AdminPage.tsx）承载；本文件不内联任何管理后台实现。

function App() {
  // T-44：认证状态（token / userId / role / username）**唯一真源**在 AuthContext，
  // 持久化与登出清理都由它保证。
  const { token, role: userRole, username: currentUsername, isAdmin, signOut } = useAuth();
  // T-44：管理员面板从"布尔量驱动的弹窗"改为 `/admin` 路由，跳转靠 router。
  const navigate = useNavigate();
  const notif = useNotificationCenter({ token, userRole });
  const [showProfile, setShowProfile] = useState(false);
  const [showQuestionBank, setShowQuestionBank] = useState(false);
  const [showAuthModal, setShowAuthModal] = useState(false);

  const session = useInterviewSession({
    token,
    signOut,
    // T-42：登出要把面试历史 / 通知 / 个人中心面板一起清干净 ——
    // 否则下一个账号登录后会继承上一个账号的面板内容。
    onSignedOut: () => {
      notif.clear();
      setShowProfile(false);
    },
    // 报告保存成功：刷新历史并切到「面试历史」页签（原 endInterview 的收尾）。
    onHistorySaved: () => notif.openHistory(),
  });

  // T-47：登录成功后的收尾。历史 / 通知 / 资料各自监听 token 变化自动重载，
  // 这里只补管理员那条"不要继承用户面板"的分支。
  const handleSignedIn = (payload: { token: string; role: string; username: string }) => {
    if (payload.role === 'admin') notif.setShowInfoPanel(false);
  };

  // T-44：管理员不再"弹面板"——登录后直接进入独立的管理后台路由。
  // 这一步原本藏在 App 里（`if (userRole === 'admin') setShowAdminPanel(true)`），
  // 现在由路由层接手：URL 即状态，刷新/前进后退都能回到同一屏。
  if (isAdmin) {
    return <Navigate to="/admin" replace />;
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-slate-50">
      {/* 顶部导航 */}
      <header className="sticky top-0 z-40 bg-white/80 backdrop-blur-xl border-b border-slate-200/60 shadow-sm">
        <div className="max-w-4xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center shadow-md shadow-primary-200">
              <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
            </div>
            <h1 className="text-lg font-bold bg-gradient-to-r from-primary-700 to-primary-500 bg-clip-text text-transparent">AI 智能面试模拟</h1>
          </div>
          <nav className="flex items-center gap-1">
            <button onClick={() => setShowQuestionBank(true)} className="btn-ghost text-sm flex items-center gap-1.5">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
              <span>题库</span>
            </button>
            {token ? (
              <>
                {userRole === 'admin' && (
                  <button onClick={() => navigate('/admin')} className="btn-ghost text-sm flex items-center gap-1.5 text-primary-600">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
                    <span>管理</span>
                  </button>
                )}
                {userRole !== 'admin' && (
                  <button onClick={() => { notif.setInfoTab('notifications'); notif.setShowInfoPanel(!notif.showInfoPanel); }} className="btn-ghost text-sm flex items-center gap-1.5 relative">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" /></svg>
                    <span>消息</span>
                    {notif.unreadCount > 0 && <span className="absolute -top-0.5 -right-0.5 bg-red-500 text-white text-[10px] font-bold rounded-full min-w-[18px] h-[18px] flex items-center justify-center px-1">{notif.unreadCount > 99 ? '99+' : notif.unreadCount}</span>}
                  </button>
                )}
                <button onClick={() => setShowProfile(true)} className="btn-ghost text-sm flex items-center gap-1.5">
                  <div className="w-6 h-6 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-xs font-semibold">
                    {currentUsername?.charAt(0)?.toUpperCase() || '?'}
                  </div>
                  <span>{currentUsername || '个人'}</span>
                </button>
                <button onClick={session.logout} className="btn-ghost text-sm flex items-center gap-1.5 text-red-500 hover:text-red-600 hover:bg-red-50">
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
                  <span>退出</span>
                </button>
              </>
            ) : (
              <button onClick={() => setShowAuthModal(true)} className="btn-primary text-sm px-4 py-2">
                登录 / 注册
              </button>
            )}
          </nav>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-6 animate-fade-in">

      {/* 消息中心 - 通知 + 面试历史 */}
      {notif.showInfoPanel && token && <NotificationCenter center={notif} />}

      {/* 个人中心弹窗（资料 / 头像 / 改密） */}
      <ProfilePanel
        open={showProfile}
        token={token}
        username={currentUsername}
        onClose={() => setShowProfile(false)}
        onLogout={session.logout}
      />

      {/* 未登录提示 */}
      {!token && (
        <div className="animate-slide-up">
          <div className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-primary-600 via-primary-700 to-indigo-800 p-8 md:p-12 mb-8 shadow-float">
            <div className="absolute inset-0 bg-[url('data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjAiIGhlaWdodD0iNjAiIHZpZXdCb3g9IjAgMCA2MCA2MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48ZyBmaWxsPSJub25lIiBmaWxsLXJ1bGU9ImV2ZW5vZGQiPjxnIGZpbGw9IiNmZmYiIGZpbGwtb3BhY2l0eT0iMC4wNSI+PGNpcmNsZSBjeD0iMzAiIGN5PSIzMCIgcj0iMiIvPjwvZz48L2c+PC9zdmc+')] opacity-50"></div>
            <div className="relative z-10 flex flex-col md:flex-row items-center gap-6">
              <div className="flex-1 text-white">
                <span className="badge bg-white/20 text-white border-white/30 mb-3">AI-Powered</span>
                <h2 className="text-2xl md:text-3xl font-extrabold mb-3 leading-tight">准备好迎接下一次面试了吗？</h2>
                <p className="text-primary-100 text-sm md:text-base leading-relaxed max-w-lg">
                  上传简历，AI 面试官将自动生成针对性问题，模拟真实面试场景，并提供专业评估报告。
                </p>
                <div className="flex gap-3 mt-5">
                  <button onClick={() => setShowAuthModal(true)} className="bg-white text-primary-700 font-semibold px-5 py-2.5 rounded-xl shadow-md hover:shadow-lg hover:scale-[1.02] active:scale-[0.98] transition-all duration-200 text-sm">
                    免费开始 →
                  </button>
                  <button onClick={() => setShowQuestionBank(true)} className="bg-white/10 text-white border border-white/20 font-semibold px-5 py-2.5 rounded-xl hover:bg-white/20 transition-all duration-200 text-sm">
                    浏览题库
                  </button>
                </div>
              </div>
              <div className="hidden md:flex items-center justify-center">
                <div className="w-44 h-44 rounded-3xl bg-white/10 backdrop-blur-xl border border-white/20 flex items-center justify-center animate-float shadow-2xl">
                  <svg className="w-20 h-20 text-white/80" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>
                </div>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            {[
              { icon: '📄', title: '简历智能解析', desc: '支持 PDF/DOCX 格式，自动提取关键信息' },
              { icon: '🤖', title: 'AI 面试模拟', desc: 'DeepSeek 驱动的真实面试场景对话' },
              { icon: '📊', title: '专业评估报告', desc: '多维度评分 + 改进建议，助力成长' },
            ].map((f, i) => (
              <div key={i} className="card text-center animate-slide-up" style={{ animationDelay: `${i * 0.1}s` }}>
                <div className="text-3xl mb-3">{f.icon}</div>
                <h3 className="font-semibold text-slate-800 mb-1">{f.title}</h3>
                <p className="text-xs text-slate-500 leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 管理员提示卡 */}
      {token && userRole === 'admin' && (
        <div className="card border-primary-200 bg-gradient-to-r from-primary-50 to-indigo-50 mb-6 animate-slide-up">
          <div className="flex items-start gap-4">
            <div className="w-10 h-10 rounded-xl bg-primary-100 flex items-center justify-center text-xl shrink-0">🛡️</div>
            <div className="flex-1">
              <h3 className="font-bold text-primary-800 mb-1">管理员模式</h3>
              <p className="text-sm text-primary-600">您当前处于管理员账户，不能进行面试。请使用管理后台管理用户和面试记录。</p>
            </div>
            <button onClick={() => navigate('/admin')} className="btn-primary text-sm px-4 py-2 shrink-0">
              打开管理后台
            </button>
          </div>
        </div>
      )}

      {/* 登录后的功能区域：报告 / 面试主流程 */}
      {token && userRole !== 'admin' && (
        <>
          {session.report ? (
            <ReportView report={session.report} onRestart={session.startNewInterview} />
          ) : (
            <InterviewRoom session={session} />
          )}
        </>
      )}

      {/* 题库弹窗 */}
      <QuestionBankModal open={showQuestionBank} onClose={() => setShowQuestionBank(false)} />

      {/* 登录/注册弹窗 */}
      <AuthModal
        open={showAuthModal}
        onClose={() => setShowAuthModal(false)}
        onSignedIn={handleSignedIn}
      />
      </main>
      {/* T-42 / FR-4.12：超时锁定等"必须被看见"的反馈统一走 Toast（禁用 alert）。 */}
      <ToastHost toast={session.toast} onClose={() => session.setToast(null)} />
    </div>
  );
}

export default App;
