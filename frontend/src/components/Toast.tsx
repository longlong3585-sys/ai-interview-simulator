import { useEffect, useRef } from 'react';

/**
 * T-42 所需的最小 Toast —— **只为**"超时锁定"这类必须被看见的反馈而存在。
 *
 * 边界（刻意）：这不是 T-37 的"全局错误呈现"。T-37 要做的是把全库 24 处
 * 裸 `fetch` 的失败统一收敛到一个 Provider，并清掉所有 `alert(`；
 * 本文件只是一个受控的展示组件，谁有消息谁渲染，不注册全局监听、
 * 不接管请求错误。等 T-37 落地时，它可以被直接搬进全局 Provider 复用。
 */

export type ToastTone = 'info' | 'warn' | 'error';

export interface ToastMessage {
  /** 每次新消息换一个 id，用来重置自动关闭计时（同文案再来一次也要重新计时）。 */
  id: number;
  text: string;
  tone?: ToastTone;
  /** 补充说明（例如服务端下发的 `hint`）。 */
  detail?: string;
  /** 为 true 时不由计时器关闭 —— 用于"必须被用户确认"的关键反馈。 */
  persistent?: boolean;
}

const TONE_CLASSES: Record<ToastTone, string> = {
  info: 'border-primary-200 bg-white text-slate-800',
  warn: 'border-amber-300 bg-amber-50 text-amber-900',
  error: 'border-red-300 bg-red-50 text-red-900',
};

export function ToastHost({
  toast,
  onClose,
  durationMs = 8000,
}: {
  toast: ToastMessage | null;
  onClose: () => void;
  durationMs?: number;
}) {
  // onClose 每次渲染都是新函数；用 ref 兜住它，避免"父组件一重渲染就重置计时"。
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!toast || toast.persistent) return;
    const timer = setTimeout(() => onCloseRef.current(), durationMs);
    return () => clearTimeout(timer);
  }, [toast, durationMs]);

  if (!toast) return null;

  const tone = toast.tone || 'info';
  return (
    <div
      data-testid="toast"
      data-toast-tone={tone}
      role="alert"
      aria-live="assertive"
      className={`fixed bottom-6 right-6 z-[60] w-[22rem] max-w-[90vw] rounded-2xl border shadow-float p-4 animate-slide-up ${TONE_CLASSES[tone]}`}
    >
      <div className="flex items-start gap-2.5">
        <span className="text-lg leading-none mt-0.5">{tone === 'error' ? '⏰' : tone === 'warn' ? '⚠️' : 'ℹ️'}</span>
        <div className="flex-1">
          <p className="text-sm font-semibold leading-relaxed">{toast.text}</p>
          {toast.detail && <p className="text-xs mt-1.5 leading-relaxed opacity-80">{toast.detail}</p>}
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭提示"
          className="text-current opacity-50 hover:opacity-100 transition-opacity shrink-0"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  );
}
