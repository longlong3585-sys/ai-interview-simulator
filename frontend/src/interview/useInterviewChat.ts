/**
 * T-46：把「面试问答」的两条写路径（`sendMessage` / `skipQuestion`）从 App.tsx 拆出来。
 *
 * 保留 T-42 / Bug 3A 的两条不变量，它们是本次拆分**不能碰**的行为：
 *   ① 锁定后写入口全部关闭（输入区在锁定态已被销毁，这里是第二道防线，
 *      挡住"锁定瞬间已在途"的提交）；
 *   ② 服务端判定超时（409 + `interview_timeout`）时，要把乐观插入的用户气泡**撤回**
 *      —— 这一轮并没有被记进会话，否则用户以为"这句答上了"，而报告里根本没有它。
 *
 * 面试状态（messages / input / loading / 进度）由 `useInterviewSession` 持有，
 * 本 hook 只提供行为，避免 Hook 之间循环依赖。
 */

import { useEffect, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from 'react';
import { authFetch } from '../services/api';
import { nextToastId } from '../components/toastSeq';
import type { ToastMessage } from '../components/Toast';
import { isTimeoutResponse } from './timeout';

export interface ChatMessage {
  role: string;
  content: string;
}

export interface InterviewChatApi {
  /** T-13 / FR-4.10：跳过词由后端下发（单一来源），本 hook 只做本地 UI 标记。 */
  skipWords: string[];
  setSkipWords: (words: string[]) => void;
  chatContainerRef: RefObject<HTMLDivElement | null>;
  sendMessage: () => Promise<void>;
  skipQuestion: () => Promise<void>;
}

interface UseInterviewChatOptions {
  token: string | null;
  role: string;
  input: string;
  setInput: (value: string) => void;
  loading: boolean;
  setLoading: (value: boolean) => void;
  messages: ChatMessage[];
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  hasResume: boolean;
  resumeFullText: string;
  interviewStarted: boolean;
  interviewFinished: boolean;
  setInterviewFinished: (value: boolean) => void;
  currentQuestionIndex: number;
  setCurrentQuestionIndex: (value: number) => void;
  setQuestionStatus: Dispatch<SetStateAction<string[]>>;
  /** T-42：锁定后所有写路径关闭。 */
  interviewLocked: boolean;
  enableSpeech: boolean;
  speakText: (text: string) => void;
  lockByServerTimeout: (body?: unknown) => void;
  setToast: (toast: ToastMessage | null) => void;
  /**
   * T-42 / Bug 3A：定时器与 setTimeout 必须拿到**最新**的 endInterview。
   * 原实现在 `sendMessage` 里 `setTimeout(() => endInterview(), 1500)` ——
   * 那个闭包捕获的是**本次渲染之前**的 `messages`，于是"最后一轮问答"既不在
   * 报告入参里，`messages.length === 0` 的判断也可能用旧值命中。
   */
  endInterviewRef: RefObject<() => Promise<void>>;
  /** 令牌失效（`Unauthorized`）时的登出入口，同样走 ref 拿最新实现。 */
  logoutRef: RefObject<() => void>;
}

export function useInterviewChat(options: UseInterviewChatOptions): InterviewChatApi {
  const {
    token, role, input, setInput, loading, setLoading,
    messages, setMessages, hasResume, resumeFullText,
    interviewStarted, interviewFinished, setInterviewFinished,
    currentQuestionIndex, setCurrentQuestionIndex, setQuestionStatus,
    interviewLocked, enableSpeech, speakText, lockByServerTimeout, setToast,
    endInterviewRef, logoutRef,
  } = options;

  // T-13 / FR-4.10：跳过词由后端 GET /api/interview/config 下发（单一来源）
  const [skipWords, setSkipWords] = useState<string[]>([]);
  const chatContainerRef = useRef<HTMLDivElement>(null);

  // 自动滚动
  useEffect(() => {
    if (chatContainerRef.current) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
    }
  }, [messages]);

  // 发送消息
  const sendMessage = async () => {
    if (!input.trim() || loading || !hasResume || !token) return;
    // T-42：锁定后写入口全部关闭（输入区在锁定态已被销毁，这里是第二道防线，
    // 挡住"锁定瞬间已在途"的提交）。
    if (interviewLocked || interviewFinished) return;

    const lowerInput = input.trim().toLowerCase();
    // T-13 / FR-4.10：跳过词不再硬编码，改由后端 GET /api/interview/config 下发
    // （详见 loadInterviewConfig）。此处只做本地 UI 状态标记；
    // **权威判定仍在后端**（_is_skip_message），因此即使配置尚未加载完成，
    // 面试流程本身也不受影响，仅本地题目状态点可能显示为 answered 而非 skipped。
    const isSkip = skipWords.some(w => lowerInput.includes(w));

    const userMessage = { role: 'user', content: input };
    setMessages(prev => [...prev, userMessage]);
    setInput('');
    setLoading(true);
    try {
      // T-11 / FR-4.9：移除死参数
      //   user_id —— 后端自 T-04 起一律从 JWT 取身份，不再读该字段
      //   action  —— 后端从未读取过（原先在下方传 'start'）
      const bodyObj: any = {
        message: input,
        role: role,
      };
      if (!interviewStarted && messages.length === 0 && resumeFullText) {
        bodyObj.resume_context = resumeFullText;
      }
      const res = await authFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(bodyObj)
      });
      const data = await res.json();

      // T-42 / FR-4.12：服务端判定超时（409 + interview_timeout）。
      // 这一轮**没有**被记进会话，所以要把乐观插入的用户气泡撤回 ——
      // 否则用户会以为"这句答上了"，而报告里根本没有它。
      // 注意必须按 `code` 精确区分：`no_active_session` / `version_conflict`
      // 同样是 409，却不是超时（见 interview/timeout.ts 的判别力注释）。
      if (isTimeoutResponse(res.status, data)) {
        setMessages(prev => prev.slice(0, -1));
        lockByServerTimeout(data);
        return;
      }
      if (!res.ok) {
        setMessages(prev => prev.slice(0, -1));
        setToast({
          id: nextToastId(),
          tone: 'warn',
          text: typeof data?.detail === 'string' ? data.detail : '发送失败，请稍后重试',
        });
        return;
      }

      const aiContent = data.reply || '';
      const aiMessage = { role: 'assistant', content: aiContent };
      setMessages(prev => [...prev, aiMessage]);

      if (data.current_index !== undefined) {
        setCurrentQuestionIndex(data.current_index);
        if (!data.finished && isSkip) {
          setQuestionStatus(prev => {
            const cp = [...prev];
            if (cp[currentQuestionIndex]) cp[currentQuestionIndex] = 'skipped';
            return cp;
          });
        } else if (!data.finished) {
          setQuestionStatus(prev => {
            const cp = [...prev];
            if (cp[currentQuestionIndex]) cp[currentQuestionIndex] = 'answered';
            return cp;
          });
        }
      }

      if (data.finished) {
        setInterviewFinished(true);
        // T-42 / Bug 3A：走 ref 拿**最新**的 endInterview。
        // 直接写 `endInterview()` 会捕获本次渲染之前的 messages，
        // 最后一条回答就进不了报告入参（也不会出现在保存的历史里）。
        setTimeout(() => endInterviewRef.current(), 1500);
      }

      if (enableSpeech && aiContent) speakText(aiContent);
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logoutRef.current();
        alert('登录已过期，请重新登录');
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: '网络错误，请稍后重试' }]);
      }
    } finally {
      setLoading(false);
    }
  };

  const skipQuestion = async () => {
    if (loading || !interviewStarted || interviewFinished || interviewLocked) return;
    setLoading(true);
    try {
      const res = await authFetch('/api/skip_question', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      });
      const data = await res.json();
      // T-42：跳过是另一条写路径，同样会被服务端兜底拦下（T-28 P10）。
      if (isTimeoutResponse(res.status, data)) {
        lockByServerTimeout(data);
        return;
      }
      if (!res.ok) {
        setToast({
          id: nextToastId(),
          tone: 'warn',
          text: typeof data?.detail === 'string' ? data.detail : '跳过失败，请稍后重试',
        });
        return;
      }
      const aiMessage = { role: 'assistant', content: data.reply };
      setMessages(prev => [...prev, { role: 'user', content: '[跳过此题]' }, aiMessage]);
      if (data.current_index !== undefined) {
        setCurrentQuestionIndex(data.current_index);
        setQuestionStatus(prev => {
          const cp = [...prev];
          if (cp[data.current_index - 1]) cp[data.current_index - 1] = 'skipped';
          return cp;
        });
      }
      if (data.finished) {
        setInterviewFinished(true);
        setTimeout(() => endInterviewRef.current(), 1500);
      }
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logoutRef.current();
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: '跳过失败，请手动输入"跳过"重试' }]);
      }
    } finally {
      setLoading(false);
    }
  };

  return { skipWords, setSkipWords, chatContainerRef, sendMessage, skipQuestion };
}
