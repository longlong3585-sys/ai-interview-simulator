/**
 * T-46：把「超时强制闭环」（T-42 / FR-4.12）从 2189 行的 `App.tsx` 里拆成独立状态机。
 *
 * 原实现（docs/29 §1）有三处硬伤，全部保留在本文件的注释里，改动一律不还原：
 *   ① 时长在前端硬编码（`setTimeLeft(15 * 60)`）—— 服务端改了时长，两边各说各话；
 *   ② 倒计时用 `prev - 1` 逐秒自减 —— 标签页被挂起/节流后比真实时间慢；
 *   ③ 归零只调 `endInterview`，**没有任何锁定状态** —— 输入区还在，用户还能继续答。
 *
 * 现在：时长唯一来源是服务端，倒计时按**死线**重算，到点即锁定（唯一终态入口）。
 *
 * 拆分口径（避免 Hook 之间循环依赖）：
 *   本 hook 只负责「服务端时间事实 → 倒计时 → 锁死 UI」这条链；
 *   锁定瞬间要清空的输入框 / 加载态由 `useInterviewSession` 以 setter 注入
 *   （`setInput` / `setLoading` 是 React 的稳定 setter，闭包捕获它们没有陈旧值问题）。
 */

import { useEffect, useRef, useState } from 'react';
import { authFetch } from '../services/api';
import { nextToastId } from '../components/toastSeq';
import type { ToastMessage } from '../components/Toast';
import {
  isTimeoutEnded,
  remainingSeconds,
  resolveDeadline,
  timeoutLockNotice,
  type DeadlineSource,
  type SessionTimeFacts,
  type TimeoutLockNotice,
} from './timeout';

export interface InterviewTimeoutApi {
  timeLeft: number | null;
  deadlineSource: DeadlineSource;
  interviewLocked: boolean;
  lockNotice: TimeoutLockNotice | null;
  toast: ToastMessage | null;
  setToast: (toast: ToastMessage | null) => void;
  /** config 下发的服务端时长（秒）；`null` 表示服务端没给，不自行编造。 */
  interviewDurationSeconds: number | null;
  /** 把服务端下发的 `duration_seconds` 落进状态 + 镜像 ref。 */
  applyDurationSeconds: (seconds: number | null) => void;
  stopTimer: () => void;
  /** 到点 / 服务端说超时：把界面锁死的唯一入口。 */
  lockByServerTimeout: (body?: unknown) => void;
  armInterviewDeadline: (
    facts: SessionTimeFacts | null,
    fallbackDurationSeconds?: number | null,
  ) => void;
  syncDeadlineFromServer: () => Promise<'continuing' | 'timeout' | 'unknown'>;
  /** 只清倒计时显示（结束面试时先停表）：不停锁定态、不作废世代号。 */
  clearCountdown: () => void;
  /** 换简历 / 重开一场 / 报告已出 / 登出：倒计时与锁定态整体清掉，并作废在途同步。 */
  resetTimeoutState: () => void;
}

interface UseInterviewTimeoutOptions {
  token: string | null;
  interviewStarted: boolean;
  setInput: (value: string) => void;
  setLoading: (value: boolean) => void;
}

export function useInterviewTimeout({
  token,
  interviewStarted,
  setInput,
  setLoading,
}: UseInterviewTimeoutOptions): InterviewTimeoutApi {
  const [timeLeft, setTimeLeft] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // ------------------------------------------------------------------------
  // T-42 / FR-4.12：超时强制闭环的状态。
  // ------------------------------------------------------------------------
  const [interviewDurationSeconds, setInterviewDurationSeconds] = useState<number | null>(null);
  const [deadlineSource, setDeadlineSource] = useState<DeadlineSource>('none');
  const [interviewLocked, setInterviewLocked] = useState(false);
  const [lockNotice, setLockNotice] = useState<TimeoutLockNotice | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);
  /** 服务端死线的本地镜像（epoch 毫秒）；定时器与事件监听都读它，避免闭包读到旧值。 */
  const deadlineRef = useRef<number | null>(null);
  /** config 下发时长的镜像：供**不重新渲染**的回调（visibilitychange）取用。 */
  const durationRef = useRef<number | null>(null);
  /**
   * 面试"世代号"：每开始 / 锁定 / 结束 / 复位一场面试就 +1。
   * 用来丢弃**过期的异步响应**（见 `syncDeadlineFromServer`）——
   * 一个回来得太晚的会话同步，会把已经结束的面试重新装回倒计时。
   */
  const interviewGenerationRef = useRef(0);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  // ==========================================================================
  // T-42 / FR-4.12：超时强制闭环（服务端死线 → 倒计时 → 到点锁死 UI）
  // ==========================================================================

  const stopTimer = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  /**
   * 锁定 UI。这是超时闭环的**唯一终态入口**：锁定后
   * 输入区会被整块销毁（不是置灰）、所有写入口关闭，并弹出一条
   * 不可错过的 Toast（FR-4.12 第 ③ 条"明确反馈"）。
   */
  const lockInterview = (notice: TimeoutLockNotice) => {
    stopTimer();
    deadlineRef.current = null;
    interviewGenerationRef.current += 1;
    setTimeLeft(0);
    setInterviewLocked(true);
    setLockNotice(notice);
    setInput('');
    setLoading(false);
    setToast({
      id: nextToastId(),
      tone: 'error',
      text: notice.title,
      detail: notice.message,
      // 超时反馈必须被看见：不自动消失，由用户确认（服务端 `detail` 也一并展示）。
      persistent: true,
    });
  };

  /** 到点（或服务端判定超时）后把界面锁死。 */
  const lockByServerTimeout = (body?: unknown) => {
    lockInterview(timeoutLockNotice(body));
  };

  /** 定时器 tick：**每次都按死线重算**，不做逐秒自减（见 timeout.ts 注释）。 */
  const tickInterviewCountdown = () => {
    const deadline = deadlineRef.current;
    if (deadline === null) return;
    const remaining = remainingSeconds(deadline, Date.now());
    if (remaining === null) return;
    setTimeLeft(remaining);
    if (remaining <= 0) {
      // 本地到点即锁定：FR-4.12 要求前端"立刻打断主流程"，
      // 不能等下一次请求返回 409 才告诉用户（那中间还能继续输入）。
      lockInterview(timeoutLockNotice(null));
    }
  };

  const startTimer = () => {
    stopTimer();
    timerRef.current = setInterval(tickInterviewCountdown, 1000);
  };

  /**
   * 用服务端下发的时间事实**重新武装**倒计时。
   *
   * `facts` 可以是 `GET /api/interview/session` 的 `session`，
   * 也可以是 `POST /api/start_interview` 的响应（它不带死线，
   * 此时退回 config 下发的 `duration_seconds` —— 仍以服务端为准）。
   */
  const armInterviewDeadline = (
    facts: SessionTimeFacts | null,
    fallbackDurationSeconds: number | null = null,
  ) => {
    const resolved = resolveDeadline(
      facts,
      Date.now(),
      fallbackDurationSeconds ?? durationRef.current,
    );
    deadlineRef.current = resolved.deadlineMs;
    setDeadlineSource(resolved.source);
    if (resolved.deadlineMs === null) {
      // 服务端一个时间字段都没给：不自行编造时长，只保留服务端 409 兜底。
      stopTimer();
      setTimeLeft(null);
      return;
    }
    setTimeLeft(remainingSeconds(resolved.deadlineMs, Date.now()));
    startTimer();
  };

  /**
   * 向服务端要一次**权威死线**，并顺手让 T-28 的惰性兜底跑一遍。
   *
   * 触发时机：面试开始后、标签页重新可见时（挂起恢复）、刷新后。
   * 超时后 `session` 必为 `null`，此时靠 `last_ended.ended_reason=timeout`
   * 才能给出"因超时已自动结束"，而不是误报"你没有在面试"。
   */
  const syncDeadlineFromServer = async (): Promise<'continuing' | 'timeout' | 'unknown'> => {
    // 世代号：这一次同步发出后，如果面试已经被结束/复位/登出（世代号 +1），
    // 那么它的响应就是**过期**的，必须丢弃。
    // 不加这道闸会出现真实的错乱：用户在报告页上看到一条"面试已超时"的
    // Toast，因为一个几百毫秒前发出、回来时面试早已结束的同步请求
    // 又把倒计时（甚至锁定态）重新装了上去。
    const generation = interviewGenerationRef.current;
    try {
      const res = await authFetch('/api/interview/session', { method: 'GET' });
      if (!res.ok) return 'unknown';
      const data = await res.json();
      if (generation !== interviewGenerationRef.current) return 'unknown';
      if (data && data.session) {
        armInterviewDeadline(data.session as SessionTimeFacts, null);
        return 'continuing';
      }
      if (isTimeoutEnded(data && data.last_ended)) {
        // 刷新/回到前台时才发现超时：`session` 已是 null，唯一能说明
        // "发生过什么"的就是 `last_ended`。没有这一步，用户只会看到
        // "你没有任何面试"，以为进度丢了（docs/28 §1 的 ③）。
        lockByServerTimeout({
          detail: '上一场面试已因超时自动结束（服务端在本次同步时确认），无法继续答题。',
        });
        return 'timeout';
      }
      return 'unknown';
    } catch (err) {
      console.error('面试会话同步失败', err);
      return 'unknown';
    }
  };

  /**
   * 只清倒计时（结束面试的第一步）：停表 + 清死线镜像 + 清显示值。
   * 不动锁定态与世代号 —— 那两件事归 `resetTimeoutState`。
   */
  const clearCountdown = () => {
    stopTimer();
    deadlineRef.current = null;
    setTimeLeft(null);
  };

  /**
   * 换简历 / 重开一场 / 报告已出 / 登出：倒计时与锁定态整体清掉
   * （服务端锁由 T-28 释放），并 +1 世代号作废在途的会话同步。
   */
  const resetTimeoutState = () => {
    clearCountdown();
    interviewGenerationRef.current += 1;
    setInterviewLocked(false);
    setLockNotice(null);
  };

  const applyDurationSeconds = (seconds: number | null) => {
    durationRef.current = seconds;
    setInterviewDurationSeconds(seconds);
  };

  /**
   * 标签页重新可见时按服务端死线**重新校正**一次倒计时。
   *
   * 场景：手机锁屏 / 切走标签页几分钟。后台 setInterval 会被浏览器节流，
   * 回到前台时本地剩余时间可能已经不可信；顺便让服务端跑一次超时兜底 ——
   * "挂起再恢复"正是 Bug 3B 里被手工绕过的那条路径。
   */
  useEffect(() => {
    if (!interviewStarted || interviewLocked) return;
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void syncDeadlineFromServer();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interviewStarted, interviewLocked, token]);

  return {
    timeLeft,
    deadlineSource,
    interviewLocked,
    lockNotice,
    toast,
    setToast,
    interviewDurationSeconds,
    applyDurationSeconds,
    stopTimer,
    lockByServerTimeout,
    armInterviewDeadline,
    syncDeadlineFromServer,
    clearCountdown,
    resetTimeoutState,
  };
}
