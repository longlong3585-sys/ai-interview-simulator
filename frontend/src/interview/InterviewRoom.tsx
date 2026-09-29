/**
 * T-46：面试主流程的视图层（原 App.tsx 的 JSX 第 1763~2060 行）。
 *
 * 这里**只有渲染**：状态与行为全部来自 `useInterviewSession()` 返回的 session 对象。
 * 拆分时逐字保留了三类关键界面事实（它们各有契约测试守着）：
 *   · `data-testid="interview-countdown"` + `data-countdown-source`（T-42 倒计时）；
 *   · `data-testid="interview-locked"`：超时后输入区**整块销毁**（不是置灰），
 *     只留"出报告"与"重开一场"两条出路；
 *   · 进度点 / 问题清单 / 聊天区 / 输入区原样保留。
 */

import { formatCountdown } from './timeout';
import type { InterviewSession } from './useInterviewSession';

export function InterviewRoom({ session }: { session: InterviewSession }) {
  const {
    uploading, hasResume, resumeFileName, resumeDragOver, setResumeDragOver,
    handleResumeDrop, handleResumeFile, resumeError, resumePreview, questions,
    startInterview, loading, role, setRole, interviewStarted, enableSpeech, setEnableSpeech,
    currentQuestionIndex, totalQuestions, questionStatus, timeLeft, deadlineSource,
    interviewLocked, lockNotice, chatContainerRef, messages, input, setInput, sendMessage,
    speechSupported, startListening, isListening, interviewFinished, skipQuestion,
    endInterview, endInterviewRef, resetInterviewFlow,
  } = session;

  return (
    <>
      {/* 简历上传 */}
      <div className="card mb-5 animate-slide-up">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-8 h-8 rounded-lg bg-primary-100 flex items-center justify-center text-primary-600 shrink-0">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
          </div>
          <div>
            <h3 className="font-semibold text-sm text-slate-800">上传简历</h3>
            <p className="text-xs text-slate-400">支持 PDF / DOCX，最大 5MB</p>
          </div>
        </div>
        <div
          className={`relative border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-300 ${resumeDragOver ? 'border-primary-400 bg-primary-50/50 scale-[1.01]' : hasResume ? 'border-emerald-400 bg-emerald-50/30' : 'border-slate-200 hover:border-primary-300 hover:bg-slate-50/50'}`}
          onDragOver={e => { e.preventDefault(); setResumeDragOver(true); }}
          onDragLeave={() => setResumeDragOver(false)}
          onDrop={handleResumeDrop}
          onClick={() => document.getElementById('resume-file-input')?.click()}
        >
          {uploading ? (
            <div className="flex flex-col items-center gap-3 text-primary-600">
              <div className="w-10 h-10 rounded-full border-2 border-primary-200 border-t-primary-600 animate-spin" />
              <span className="text-sm font-medium">正在解析简历...</span>
            </div>
          ) : hasResume ? (
            <div className="flex flex-col items-center gap-2">
              <div className="w-10 h-10 rounded-full bg-emerald-100 flex items-center justify-center">
                <svg className="w-5 h-5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
              </div>
              <span className="font-semibold text-emerald-700">{resumeFileName}</span>
              <span className="text-xs text-slate-400">点击或拖拽更换文件</span>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <div className="w-10 h-10 rounded-full bg-slate-100 flex items-center justify-center">
                <svg className="w-5 h-5 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
              </div>
              <span className="font-medium text-slate-600">点击选择文件或拖拽到此处</span>
              <span className="text-xs text-slate-400">支持 PDF、DOCX 格式</span>
            </div>
          )}
        </div>
        <input
          id="resume-file-input"
          type="file"
          accept=".pdf,.docx"
          className="hidden"
          onChange={e => { const file = e.target.files?.[0]; if (file) handleResumeFile(file); }}
          disabled={uploading}
        />
        {resumeError && (
          <div className="mt-3 p-3 bg-red-50 border border-red-200 rounded-xl flex items-center gap-2 text-red-600 text-sm">
            <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
            <span>{resumeError}</span>
          </div>
        )}
        {resumePreview && (
          <details className="mt-3 group">
            <summary className="text-sm text-primary-600 cursor-pointer hover:text-primary-800 font-medium flex items-center gap-1">
              <svg className="w-4 h-4 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
              简历预览（前 500 字）
            </summary>
            <p className="text-xs text-slate-600 mt-2 bg-slate-50 p-3 rounded-xl border border-slate-200 whitespace-pre-wrap max-h-32 overflow-y-auto scrollbar-thin leading-relaxed">{resumePreview}</p>
          </details>
        )}
        {questions.length > 0 && (
          <div className="mt-4 p-4 bg-primary-50/50 rounded-xl border border-primary-100">
            <p className="font-semibold text-sm text-primary-800 mb-2 flex items-center gap-1.5">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
              面试问题 共 {questions.length} 题
            </p>
            <ol className="space-y-1.5">
              {questions.map((q, idx) => (
                <li key={idx} className="flex items-start gap-2 text-sm text-slate-700">
                  <span className="w-5 h-5 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center text-[10px] font-bold shrink-0 mt-0.5">{idx + 1}</span>
                  <span>{q}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
        {!hasResume && !uploading && resumeError && (
          <button onClick={() => document.getElementById('resume-file-input')?.click()} className="mt-3 text-sm text-primary-600 font-medium hover:text-primary-800 flex items-center gap-1">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
            重试上传
          </button>
        )}
        {hasResume && !interviewStarted && messages.length === 0 && (
          <button onClick={startInterview} disabled={loading} className="btn-primary w-full mt-4 py-3 text-sm">
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <div className="w-4 h-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                正在准备面试...
              </span>
            ) : (
              <span className="flex items-center justify-center gap-2">
                开始面试
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5m0 0l-5 5m5-5H6" /></svg>
              </span>
            )}
          </button>
        )}
      </div>

      {/* 控制面板 */}
      <div className="flex flex-wrap gap-3 items-center mb-4">
        <div className="flex items-center gap-2">
          <label className="text-xs font-semibold text-slate-500 uppercase tracking-wider">岗位</label>
          <select
            className={`text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-primary-400 transition-all ${interviewStarted ? 'bg-slate-50 text-slate-400 cursor-not-allowed' : ''}`}
            value={role}
            onChange={e => {
              if (interviewStarted) {
                alert('面试进行中，请先结束当前面试再切换岗位。');
                return;
              }
              setRole(e.target.value);
            }}
            disabled={interviewStarted}
          >
            <option>后端开发</option><option>前端开发</option><option>全栈开发</option>
            <option>算法工程师</option><option>移动开发</option><option>测试开发</option>
            <option>运维开发</option><option>数据工程</option><option>机器学习工程师</option>
            <option>嵌入式开发</option>
          </select>
        </div>
        <label className="flex items-center gap-2 text-sm cursor-pointer group">
          <div className={`w-9 h-5 rounded-full relative transition-colors duration-200 ${enableSpeech ? 'bg-primary-500' : 'bg-slate-300'}`}>
            <div className={`w-4 h-4 rounded-full bg-white absolute top-0.5 shadow-sm transition-transform duration-200 ${enableSpeech ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
          </div>
          <span className="text-xs text-slate-500 group-hover:text-slate-700">AI 语音</span>
          <input type="checkbox" checked={enableSpeech} onChange={e => setEnableSpeech(e.target.checked)} className="hidden" />
        </label>
      </div>

      {interviewStarted && (
        <div className="card mb-4 animate-fade-in">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span className="text-xs text-slate-400 font-medium">
                进度 <span className="text-primary-600 font-bold text-sm">{currentQuestionIndex}/{totalQuestions}</span>
              </span>
              <div className="flex gap-1.5">
                {questionStatus.map((s, i) => (
                  <div
                    key={i}
                    className={`w-2.5 h-2.5 rounded-full transition-all duration-300 ${
                      s === 'answered' ? 'bg-emerald-500 shadow-sm shadow-emerald-200' :
                      s === 'skipped' ? 'bg-amber-400' :
                      i === currentQuestionIndex ? 'bg-primary-500 animate-pulse-soft shadow-sm shadow-primary-200' :
                      'bg-slate-200'
                    }`}
                    title={`Q${i+1}: ${s === 'answered' ? '已回答' : s === 'skipped' ? '已跳过' : '待回答'}`}
                  />
                ))}
              </div>
              {questions.length > 0 && (
                <details className="group">
                  <summary className="text-xs text-slate-400 cursor-pointer hover:text-primary-600 font-medium">问题清单</summary>
                  <div className="absolute mt-2 bg-white border border-slate-200 rounded-xl shadow-lg p-3 z-10 min-w-[200px]">
                    <ol className="text-xs space-y-1">
                      {questions.map((q, i) => (
                        <li key={i} className={questionStatus[i] === 'answered' ? 'text-emerald-600' : questionStatus[i] === 'skipped' ? 'text-amber-500 line-through' : 'text-slate-500'}>
                          {i + 1}. {q}
                        </li>
                      ))}
                    </ol>
                  </div>
                </details>
              )}
            </div>
            {timeLeft !== null && (
              <div
                data-testid="interview-countdown"
                data-countdown-source={deadlineSource}
                className={`flex items-center gap-1.5 text-sm font-mono font-bold px-3 py-1.5 rounded-xl ${
                interviewLocked ? 'bg-red-100 text-red-700 border border-red-300' :
                timeLeft <= 60 ? 'bg-red-50 text-red-600 border border-red-200 animate-pulse' :
                timeLeft <= 180 ? 'bg-amber-50 text-amber-700 border border-amber-200' :
                'bg-primary-50 text-primary-700 border border-primary-200'
              }`}>
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                {formatCountdown(timeLeft)}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 聊天区域 */}
      <div ref={chatContainerRef} className="bg-slate-50 rounded-2xl p-5 h-[420px] overflow-y-auto mb-4 scrollbar-thin border border-slate-200/60 shadow-inner">
        {messages.map((msg, idx) => (
          <div key={idx} className={`mb-3 flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'} animate-slide-up`} style={{ animationDelay: '0ms' }}>
            {msg.role === 'assistant' && (
              <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-xs font-bold shrink-0 mr-2 mt-1 shadow-sm">
                AI
              </div>
            )}
            <div className={`max-w-[75%] ${msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-ai'}`}>
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{msg.content}</p>
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start mb-3">
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-xs font-bold shrink-0 mr-2 mt-1 shadow-sm">
              AI
            </div>
            <div className="chat-bubble-ai max-w-[75%]">
              <div className="flex gap-1.5">
                <div className="w-2 h-2 rounded-full bg-slate-300 animate-bounce" style={{ animationDelay: '0ms' }} />
                <div className="w-2 h-2 rounded-full bg-slate-300 animate-bounce" style={{ animationDelay: '150ms' }} />
                <div className="w-2 h-2 rounded-full bg-slate-300 animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 输入区域 —— T-42 / FR-4.12：超时锁定后**整块销毁**（不是置灰）。
          置灰仍留着一个可聚焦的控件，且"看起来还能再答一句"，
          与服务端到点后必然 409 的事实相矛盾。这里直接把输入区、
          跳过、发送一起从 DOM 里摘掉，只留下两条出路：
          出报告（超时口径）或重开一场。 */}
      {interviewLocked ? (
        <div
          data-testid="interview-locked"
          className="rounded-2xl border-2 border-red-200 bg-red-50/70 p-5 animate-slide-up"
        >
          <div className="flex items-start gap-3">
            <div className="w-10 h-10 rounded-xl bg-red-100 text-red-600 flex items-center justify-center shrink-0 text-lg">⏰</div>
            <div className="flex-1">
              <h4 className="font-bold text-red-800 text-sm">
                {lockNotice?.title || '本场面试已超时自动结束'}
              </h4>
              <p className="text-sm text-red-700 mt-1 leading-relaxed">
                {lockNotice?.message}
              </p>
              {lockNotice?.hint && (
                <p className="text-xs text-red-600/90 mt-2 leading-relaxed">{lockNotice.hint}</p>
              )}
              <div className="flex flex-wrap gap-2 mt-4">
                <button
                  onClick={() => endInterviewRef.current()}
                  disabled={loading}
                  data-testid="timeout-generate-report"
                  className="btn-primary text-sm px-4 h-10 flex items-center gap-1.5"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 17v-6h13M9 17H4a1 1 0 01-1-1V5a1 1 0 011-1h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V16" /></svg>
                  生成报告（按超时口径评分）
                </button>
                <button
                  onClick={resetInterviewFlow}
                  data-testid="timeout-restart"
                  className="btn-secondary text-sm px-4 h-10 flex items-center gap-1.5"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                  重新开始（需重新上传简历）
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : (
      <div className="flex gap-2 items-end">
        <textarea
          className="flex-1 input-field resize-none min-h-[48px] max-h-24 text-sm"
          rows={2}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
          placeholder={interviewStarted ? `第 ${currentQuestionIndex + 1}/${totalQuestions} 题 - 输入回答...` : hasResume ? "请先点击「开始面试」" : "请先上传简历"}
          disabled={!interviewStarted || interviewFinished}
        />
        {speechSupported && (
          <button onClick={startListening} disabled={isListening || !interviewStarted} className={`w-11 h-11 rounded-xl flex items-center justify-center shrink-0 transition-all duration-200 ${isListening ? 'bg-red-500 text-white shadow-lg shadow-red-200 animate-pulse' : 'bg-slate-100 text-slate-500 hover:bg-slate-200'}`} title="语音输入">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" /></svg>
          </button>
        )}
        {interviewStarted && !interviewFinished && (
          <button onClick={skipQuestion} disabled={loading} className="btn-secondary text-sm px-4 h-11 shrink-0 flex items-center gap-1.5" title="跳过此题">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" /></svg>
            跳过
          </button>
        )}
        <button onClick={sendMessage} disabled={loading || !interviewStarted || interviewFinished} className="btn-primary h-11 px-5 shrink-0 flex items-center gap-1.5 text-sm">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" /></svg>
          发送
        </button>
        <button onClick={endInterview} disabled={loading || messages.length === 0} className="btn-danger h-11 px-4 shrink-0 flex items-center gap-1.5 text-sm">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          结束
        </button>
      </div>
      )}
    </>
  );
}
