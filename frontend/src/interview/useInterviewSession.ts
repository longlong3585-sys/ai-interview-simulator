/**
 * T-46：面试主流程的**组合根**（原 `App.tsx` 26~1121 行的状态与逻辑）。
 *
 *   useInterviewTimeout ── 服务端死线 → 倒计时 → 锁死 UI（T-42 / FR-4.12）
 *   useSpeech           ── 语音识别 / 合成
 *   useInterviewChat    ── sendMessage / skipQuestion 两条写路径
 *   本文件              ── 共享状态 + 开始/结束/复位 + 简历上传的状态迁移 + 登出
 *
 * 状态为什么集中在这里：`lockInterview` 要清输入框、`sendMessage` 要读简历全文与进度、
 * `endInterview` 要清简历与进度 —— 三个所有者互相需要。把共享状态放在组合根、
 * 把行为放进子 hook，就不需要 hook 之间互相 import 对方的状态。
 */

import { useEffect, useRef, useState, type DragEvent } from 'react';
import { authFetch } from '../services/api';
import { nextToastId } from '../components/toastSeq';
import { useSpeech } from './useSpeech';
import { useInterviewTimeout } from './useInterviewTimeout';
import { useInterviewChat, type ChatMessage } from './useInterviewChat';
import { uploadResumeFile, validateResumeFile } from './resumeUpload';

export interface InterviewSessionOptions {
  token: string | null;
  /** T-44：认证三件套 + 持久化清理由 AuthContext 承担。 */
  signOut: () => void;
  /** 登出后清理 App 级状态（面试历史 / 通知 / 个人中心面板）。 */
  onSignedOut: () => void;
  /** 报告已保存：刷新面试历史并切到「面试历史」页签。 */
  onHistorySaved: () => void;
}

export function useInterviewSession({
  token,
  signOut,
  onSignedOut,
  onHistorySaved,
}: InterviewSessionOptions) {
  // —— 一、面试共享状态 ——
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [role, setRole] = useState('后端开发');
  const [questions, setQuestions] = useState<string[]>([]);
  const [report, setReport] = useState<any>(null);
  // 简历
  const [uploading, setUploading] = useState(false);
  const [hasResume, setHasResume] = useState(false);
  const [resumeFullText, setResumeFullText] = useState('');
  const [resumePreview, setResumePreview] = useState('');
  const [resumeFileName, setResumeFileName] = useState('');
  const [resumeError, setResumeError] = useState('');
  const [resumeDragOver, setResumeDragOver] = useState(false);
  // 面试进度
  const [interviewStarted, setInterviewStarted] = useState(false);
  const [currentQuestionIndex, setCurrentQuestionIndex] = useState(0);
  const [questionStatus, setQuestionStatus] = useState<string[]>([]);
  const [totalQuestions, setTotalQuestions] = useState(0);
  const [interviewFinished, setInterviewFinished] = useState(false);
  /** 语音识别 / 401 登出这类"跨渲染存活"的回调入口（见下）。 */
  const sendMessageRef = useRef<() => void>(() => {});
  const endInterviewRef = useRef<() => Promise<void>>(async () => {});
  const logoutRef = useRef<() => void>(() => {});

  // —— 二、子 hook：时间闭环 / 语音 / 问答 ——
  const timeout = useInterviewTimeout({ token, interviewStarted, setInput, setLoading });
  const speech = useSpeech({
    onTranscript: (text) => {
      setInput(text);
      // 语音输入自动提交：走 ref 拿**最新**的 sendMessage（修复前那个闭包里的
      // input 还是空串，sendMessage 第一行就 return 了，见 useSpeech.ts 注释）。
      setTimeout(() => sendMessageRef.current(), 100);
    },
  });
  const chat = useInterviewChat({
    token, role, input, setInput, loading, setLoading,
    messages, setMessages, hasResume, resumeFullText,
    interviewStarted, interviewFinished, setInterviewFinished,
    currentQuestionIndex, setCurrentQuestionIndex, setQuestionStatus,
    interviewLocked: timeout.interviewLocked,
    enableSpeech: speech.enableSpeech, speakText: speech.speakText,
    lockByServerTimeout: timeout.lockByServerTimeout, setToast: timeout.setToast,
    endInterviewRef, logoutRef,
  });

  useEffect(() => {
    sendMessageRef.current = chat.sendMessage;
  });

  // —— 三、简历上传（状态迁移在这里，纯函数在 resumeUpload.ts）——
  const handleResumeFile = async (file: File) => {
    const validationError = validateResumeFile(file);
    if (validationError) {
      setResumeError(validationError);
      return;
    }
    setResumeFileName(file.name);
    setUploading(true);
    try {
      const result = await uploadResumeFile(file);
      if (result.status === 'ok') {
        const data = result.data;
        setQuestions(data.questions);
        setHasResume(true);
        setResumePreview(data.preview);
        setInterviewStarted(false);
        setCurrentQuestionIndex(0);
        setQuestionStatus([]);
        setTotalQuestions(0);
        setInterviewFinished(false);
        // 换简历 = 作废上一场的倒计时与锁定态（服务端锁由 T-28 释放）。
        timeout.resetTimeoutState();
        if (data.full_text) setResumeFullText(data.full_text);
        setResumeError('');
      } else if (result.status === 'unauthorized') {
        logoutRef.current();
        setResumeError('登录已过期，请重新登录');
      } else {
        setResumeError(result.message);
        setHasResume(false);
      }
    } finally {
      setUploading(false);
    }
  };

  const handleResumeDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setResumeDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) handleResumeFile(file);
  };

  /** 语音输入开始时**清空输入框**（原 `startListening` 第一行就是 `setInput('')`）。 */
  const startListening = () => {
    setInput('');
    speech.startListening();
  };

  // —— 四、开始 / 结束 / 复位 ——
  const startInterview = async () => {
    if (questions.length === 0) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        role: role,
        resume_text: resumeFullText,
        questions_json: JSON.stringify(questions),
      });
      const res = await authFetch('/api/start_interview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params.toString()
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setQuestions(data.questions);
        setTotalQuestions(data.total);
        setCurrentQuestionIndex(data.current_index);
        setQuestionStatus(new Array(data.total).fill('pending'));
        setInterviewStarted(true);
        setInterviewFinished(false);
        setMessages([
          { role: 'assistant', content: data.greeting }
        ]);
        // 新一代面试：让任何在途的旧同步响应失效，再武装本场的倒计时。
        timeout.resetTimeoutState();
        // T-42 / FR-4.12：时长以**服务端**为唯一来源。原实现是 `setTimeLeft(15 * 60)`
        // + 逐秒自减，服务端一改时长两边就各说各话。`start_interview` 的响应不带死线，
        // 因此先用 config 下发的 `duration_seconds` 武装，紧接着向会话读接口要权威死线。
        timeout.armInterviewDeadline(data, timeout.interviewDurationSeconds);
        void timeout.syncDeadlineFromServer();
      } else {
        setResumeError(data.detail || '开始面试失败');
      }
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logoutRef.current();
        setResumeError('登录已过期，请重新登录');
      } else {
        setResumeError('无法连接服务器');
      }
    } finally {
      setLoading(false);
    }
  };

  const endInterview = async () => {
    timeout.clearCountdown();
    if (messages.length === 0 && !timeout.interviewLocked) {
      // 零对话且不是超时锁定 —— 服务端确实没有可评的对象（T-26 起报告
      // 以服务端会话为准，前端已无从判断内容，只能拦住明显无意义的请求）。
      timeout.setToast({ id: nextToastId(), tone: 'warn', text: '还没有任何对话，无法生成报告' });
      return;
    }
    setLoading(true);
    const currentRole = role;
    try {
      const res = await authFetch('/api/generate_report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // T-26 / Bug 3A：**不再发送 messages**（也不再发送任何 body）——后端以服务端
        // 会话为唯一事实来源，客户端无法再删改聊天记录来影响评分。
      });
      const data = await res.json();
      // T-42 / T-43：报告可能来自一场**超时**的面试（含零作答，T-27 会补上
      // 「未及作答，无法评分」）。锁定态随之解除 —— 用户已经拿到结果。
      setReport(data);
      setMessages([]);
      setHasResume(false);
      setInterviewStarted(false);
      setCurrentQuestionIndex(0);
      setQuestionStatus([]);
      setTotalQuestions(0);
      setInterviewFinished(false);
      setQuestions([]);
      setResumeFullText('');
      setResumePreview('');
      setResumeFileName('');
      setInput('');
      timeout.resetTimeoutState();

      if (data && data.expression_score !== undefined) {
        try {
          const saveRes = await authFetch('/api/save_interview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: currentRole, messages, report: data })
          });
          if (!saveRes.ok) {
            console.error('保存面试记录失败，HTTP', saveRes.status);
          } else {
            onHistorySaved();
          }
        } catch (saveErr) {
          console.error('保存面试记录异常', saveErr);
        }
      }
    } catch (err: any) {
      console.error(err);
      timeout.setToast({ id: nextToastId(), tone: 'error', text: '生成报告失败，请稍后重试' });
    } finally {
      setLoading(false);
    }
  };

  /**
   * T-42 / Bug 3A：把**每次渲染的最新** endInterview 存进 ref。
   * 定时器 / setTimeout / visibilitychange 这类"跨渲染存活"的回调一律
   * 通过 `endInterviewRef.current()` 调用，避免闭包捕获旧的 `messages`
   * （原 Bug：最后一轮问答进不了报告，或被误判成"还没有任何对话"）。
   */
  useEffect(() => {
    endInterviewRef.current = endInterview;
  });

  /** 报告页的"开始新面试"：清报告与简历预览，让用户重新上传。 */
  const startNewInterview = () => {
    setReport(null);
    setInterviewStarted(false);
    setCurrentQuestionIndex(0);
    setQuestionStatus([]);
    setTotalQuestions(0);
    setInterviewFinished(false);
    setResumePreview('');
    setResumeFileName('');
    setResumeError('');
    timeout.resetTimeoutState();
  };

  /** 超时锁定面板上"重新开始"用到：把面试相关状态整体复位（服务端锁已由 T-28 释放）。 */
  const resetInterviewFlow = () => {
    timeout.resetTimeoutState();
    setInterviewStarted(false);
    setInterviewFinished(false);
    setMessages([]);
    setQuestions([]);
    setQuestionStatus([]);
    setTotalQuestions(0);
    setCurrentQuestionIndex(0);
    setInput('');
    setHasResume(false);
    setResumePreview('');
    setResumeFileName('');
    setResumeFullText('');
  };

  // —— 五、服务端面试常量（跳过词 + 时长，单一来源）——
  // T-13 / FR-4.10：面试相关常量的唯一来源是后端。
  // 原先前端把 23 个跳过词硬编码在 sendMessage 里，与后端各存一份，
  // 任一侧改动都会造成"本地判定"与"服务端判定"分歧且无任何报错。
  const loadInterviewConfig = async () => {
    try {
      // T-36：令牌交给统一层注入（`tok` 参数已不再需要）。
      const res = await authFetch('/api/interview/config');
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data.skip_words)) chat.setSkipWords(data.skip_words);
        // T-42 / FR-4.12：面试时长的唯一来源是服务端。原先这里只取了
        // skip_words，时长仍由前端硬编码 15 分钟 —— 服务端一改时长，
        // 前端倒计时与"到点锁定"就整体失准。
        const duration = typeof data.duration_seconds === 'number' ? data.duration_seconds : null;
        timeout.applyDurationSeconds(duration);
      }
    } catch (err) {
      console.error('面试配置加载失败', err);
    }
  };

  useEffect(() => {
    if (token) void loadInterviewConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // —— 六、登出 + 挂载时的令牌校验 ——
  const logout = () => {
    // T-44：认证三件套 + 持久化清理收敛进 AuthContext
    signOut();
    // T-42：登出必须把超时锁定态一起清干净 ——
    // 否则下一个账号登录后会继承"上一场的锁定面板 + 超时 Toast"。
    timeout.resetTimeoutState();
    timeout.setToast(null);
    setMessages([]);
    setReport(null);
    setHasResume(false);
    setInterviewStarted(false);
    setCurrentQuestionIndex(0);
    setQuestionStatus([]);
    setTotalQuestions(0);
    setInterviewFinished(false);
    setQuestions([]);
    setResumeFullText('');
    setResumePreview('');
    setResumeFileName('');
    setResumeError('');
    onSignedOut();
  };

  useEffect(() => {
    logoutRef.current = logout;
  });

  useEffect(() => {
    const validateToken = async () => {
      const savedToken = localStorage.getItem('token');
      if (savedToken) {
        try {
          // T-36：令牌失效由统一层统一登出并**抛出** `Unauthorized`；
          // 这里保留原语义 —— 校验失败一律走本 hook 的 `logout()` 收尾
          // （除了清认证态，还要复位面试进度、超时锁定态）。
          await authFetch('/api/user/profile');
        } catch {
          logout();
        }
      }
    };
    validateToken();
    // 只在挂载时校验一次（与原实现一致）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    // 状态
    messages, input, setInput, loading, role, setRole, questions, report,
    uploading, hasResume, resumeFullText, resumePreview, resumeFileName,
    resumeError, resumeDragOver, setResumeDragOver,
    interviewStarted, currentQuestionIndex, questionStatus, totalQuestions, interviewFinished,
    // 子 hook
    ...timeout, ...speech, ...chat,
    // 行为（startListening 放在 spread 之后：用组合根版本覆写 useSpeech 的同名实现，
    // 以便"开始识别时清空输入框"这一条原行为不丢）
    startListening,
    handleResumeFile, handleResumeDrop, startInterview, endInterview,
    startNewInterview, resetInterviewFlow, logout, endInterviewRef,
  };
}

export type InterviewSession = ReturnType<typeof useInterviewSession>;
