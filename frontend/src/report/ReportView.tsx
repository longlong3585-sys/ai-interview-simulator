/**
 * T-47：面试评估报告视图（原 App.tsx 的 JSX 第 1636~1760 行）。
 *
 * 保留 T-43 / FR-4.5 的硬约束：**超时结束的报告必须明说自己是怎么结束的**。
 * 没有 `data-testid="report-timeout-note"` 那一段，用户看到的是一份和正常完成
 * 一模一样的评分 ——「只答了 2 题却拿了 6 分」会被当成真实水平，
 * 而事实是"没答的部分根本没评分"（T-27 的口径）。
 */

import { isTimeoutReport, TIMEOUT_REPORT_NOTE } from '../interview/timeout';

export function ReportView({ report, onRestart }: { report: any; onRestart: () => void }) {
  const exportTxt = () => {
    const content = `面试评估报告\n\n` +
      `综合得分：${report.overall_score}/10\n` +
      `表达能力：${report.expression_score}/10\n` +
      `技术深度：${report.technical_score}/10\n` +
      `逻辑思维：${report.logic_score}/10\n` +
      `已回答：${report.answered_count || 0}/${report.total_questions} 个问题\n\n` +
      (isTimeoutReport(report) ? `【${TIMEOUT_REPORT_NOTE}】\n\n` : '') +
      `总结：${report.details}\n\n` +
      `改进建议：${report.suggestion}`;
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `面试报告_${new Date().toLocaleDateString()}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="animate-scale-in">
      <div className="card p-0 overflow-hidden shadow-float">
        <div className="bg-gradient-to-r from-primary-600 to-indigo-700 px-6 py-5 text-white">
          <div className="flex justify-between items-center mb-1">
            <h3 className="font-bold text-lg">面试评估报告</h3>
            <button
              onClick={exportTxt}
              className="text-white/80 hover:text-white text-xs font-medium flex items-center gap-1 bg-white/10 rounded-lg px-3 py-1.5 hover:bg-white/20 transition-colors">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
              导出 TXT
            </button>
          </div>
          <p className="text-primary-100 text-xs">AI 面试官根据对话内容生成的详细评估</p>
        </div>
        <div className="p-6">
          {/* T-43 / FR-4.5：超时结束的报告必须**明说自己是怎么结束的**。
              没有这一行，用户看到的是一份和正常完成一模一样的评分 ——
              「只答了 2 题却拿了 6 分」会被当成真实水平，
              而事实是"没答的部分根本没评分"（T-27 的口径）。 */}
          {isTimeoutReport(report) && (
            <div
              data-testid="report-timeout-note"
              className="mb-5 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 flex items-start gap-2.5"
            >
              <span className="text-lg leading-none mt-0.5">⏰</span>
              <div>
                <p className="text-sm font-bold text-amber-900">{TIMEOUT_REPORT_NOTE}</p>
                <p className="text-xs text-amber-800 mt-1 leading-relaxed">
                  本场面试在倒计时归零时由<strong>服务端</strong>自动结束：
                  已作答的题目按正常口径评分，<strong>未及作答的题目不计入扣分</strong>。
                  想要完整评估，请重新开始一场面试。
                </p>
              </div>
            </div>
          )}
          <div className="flex items-center justify-center mb-6">
            <div className={`w-28 h-28 rounded-full flex items-center justify-center border-4 ${report.overall_score >= 7 ? 'border-emerald-300 bg-emerald-50' : report.overall_score >= 4 ? 'border-amber-300 bg-amber-50' : 'border-red-300 bg-red-50'}`}>
              <div className="text-center">
                <div className={`text-3xl font-extrabold ${report.overall_score >= 7 ? 'text-emerald-600' : report.overall_score >= 4 ? 'text-amber-600' : 'text-red-600'}`}>
                  {report.overall_score}
                </div>
                <div className="text-xs text-slate-400 font-medium">/ 10</div>
              </div>
            </div>
          </div>
          <div className="space-y-3 mb-6">
            {[
              { label: '表达能力', score: report.expression_score },
              { label: '技术深度', score: report.technical_score },
              { label: '逻辑思维', score: report.logic_score },
            ].map(item => {
              const pct = (item.score / 10) * 100;
              const barColor = item.score >= 7 ? 'bg-gradient-to-r from-emerald-500 to-emerald-400' : item.score >= 4 ? 'bg-gradient-to-r from-amber-500 to-amber-400' : 'bg-gradient-to-r from-red-500 to-red-400';
              return (
                <div key={item.label} className="flex items-center gap-3">
                  <span className="text-xs text-slate-500 font-medium w-16 text-right">{item.label}</span>
                  <div className="flex-1 h-3 bg-slate-100 rounded-full overflow-hidden">
                    <div className={`h-full ${barColor} rounded-full transition-all duration-700 ease-out`} style={{ width: `${pct}%` }} />
                  </div>
                  <span className={`text-sm font-bold w-10 ${item.score >= 7 ? 'text-emerald-600' : item.score >= 4 ? 'text-amber-600' : 'text-red-600'}`}>
                    {item.score}/10
                  </span>
                </div>
              );
            })}
          </div>

          {report.total_questions !== undefined && (
            <p className="text-center text-xs text-slate-400 mb-4">
              回答了 <span className="font-semibold text-primary-600">{report.answered_count || 0}</span> / {report.total_questions} 个问题
              {isTimeoutReport(report) && (
                <span className="text-amber-600">（超时未及作答的题目不计入扣分）</span>
              )}
            </p>
          )}
          <div className="space-y-3">
            <div className="bg-slate-50 rounded-xl p-4">
              <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">总结</h4>
              <p className="text-sm text-slate-700 leading-relaxed">{report.details}</p>
            </div>
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
              <h4 className="text-xs font-semibold text-amber-700 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
                改进建议
              </h4>
              <p className="text-sm text-amber-800 leading-relaxed">{report.suggestion}</p>
            </div>
          </div>
          <button onClick={onRestart} className="btn-primary w-full mt-5 py-3 text-sm flex items-center justify-center gap-2">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
            开始新面试（需重新上传简历）
          </button>
        </div>
      </div>
    </div>
  );
}
