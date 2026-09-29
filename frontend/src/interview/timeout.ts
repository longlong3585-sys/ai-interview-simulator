/**
 * T-42 / FR-4.12：**超时强制闭环**的纯逻辑层。
 *
 * 为什么单独抽一个模块：超时闭环是"时间"驱动的行为，出错时最难复现
 * （要么等一刻钟，要么改系统时钟）。把"服务端下发的时间 → 倒计时 → 到点锁定"
 * 这条链上的判定全部写成**无副作用、无 React 依赖**的纯函数，
 * 就能用 `node --test` 在毫秒级、可重复地断言，而不是靠手点页面。
 *
 * 契约来源（T-28 后端，见 docs/28-manual-verification.md）：
 *   * `GET /api/interview/config` → `duration_seconds`（时长的唯一来源）
 *   * `GET /api/interview/session` 的 `session` → `deadline_at` /
 *     `interview_remaining_seconds` / `duration_seconds`
 *   * 到点后的写路径 → **409** + `code="interview_timeout"` +
 *     `ended_reason="timeout"` + `detail`（字符串，直接渲染）+ `hint`
 *   * `GET /api/interview/session` → `session: null` + `last_ended.ended_reason`
 *   * `POST /api/generate_report` → 报告体带 `ended_reason`
 *
 * 本模块**不**硬编码 15 分钟：时长只来自服务端。
 */

/** 服务端在响应里下发的时间事实（会话摘要 / config，字段均可缺省）。 */
export interface SessionTimeFacts {
  duration_seconds?: number | null;
  deadline_at?: string | null;
  interview_remaining_seconds?: number | null;
}

/** 死线是从哪一路信息算出来的 —— 只用于排查与断言，不参与业务判定。 */
export type DeadlineSource =
  | 'session_remaining'
  | 'session_deadline'
  | 'duration'
  | 'none';

export interface ResolvedDeadline {
  /** epoch 毫秒；`null` 表示服务端没有给出任何可用的时间信息。 */
  deadlineMs: number | null;
  source: DeadlineSource;
}

/** 服务端"因超时结束"的机器可读标识（与 `routers/interview.py` 一致）。 */
export const TIMEOUT_CODE = 'interview_timeout';
/** 与 `services/stores/base.py` 的 `EndedReason.TIMEOUT` 一致。 */
export const TIMEOUT_REASON = 'timeout';

/** T-43 / FR-4.5：超时报告页必须出现的标注文案（逐字，别改）。 */
export const TIMEOUT_REPORT_NOTE = '因超时自动结束，仅基于已答部分评分';

export const TIMEOUT_LOCK_TITLE = '本场面试已超时自动结束';
export const TIMEOUT_LOCK_FALLBACK_MESSAGE =
  '本场面试已超时：时长归零后由服务端自动结束，无法继续答题。';
export const TIMEOUT_LOCK_HINT =
  '已作答的部分仍可生成报告（按超时口径评分，未及作答的题目不计入扣分）；' +
  '也可以立刻开始一场新的面试。';

export interface TimeoutLockNotice {
  title: string;
  message: string;
  hint?: string;
}

/**
 * 后端 `deadline_at` 是 `datetime.utcnow()` 派生的**无时区** ISO 串
 * （形如 `2026-09-29T17:10:51`）。
 *
 * ⚠️ 这是本任务最容易踩的坑：JS 的 `new Date('2026-09-29T17:10:51')`
 * 会按**浏览器本地时区**解释它。在 UTC+8 下，同一串会被当成"8 小时以后"，
 * 于是倒计时凭空多出 8 小时、UI 永远不会锁 —— 而这种 bug 在 UTC 机器上
 * 跑测试是**完全看不见**的。因此无时区标记时一律补 `Z` 按 UTC 解释。
 */
const NAIVE_DATETIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?$/;

/** 解析服务端时间串 → epoch 毫秒；无法解析时返回 `null`（宁可不算，也不误杀）。 */
export function parseServerDeadline(value?: string | null): number | null {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text) return null;
  const normalized = NAIVE_DATETIME.test(text) ? `${text.replace(' ', 'T')}Z` : text;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : null;
}

function asPositiveNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value >= 0 ? value : null;
}

/**
 * 把服务端下发的时间事实换算成**本地死线**（epoch 毫秒）。
 *
 * 优先级（从最可信到最不可信）：
 *   1. `interview_remaining_seconds` —— 服务端算好的**相对量**，
 *      免疫浏览器时钟偏差与时区问题，是首选；
 *   2. `deadline_at` —— 服务端的绝对时刻（按上面的 UTC 规则解释）；
 *   3. `duration_seconds`（本次会话的，或 config 下发的兜底）——
 *      以"现在"为起点近似：服务端的 `created_at` 就在这一刻附近。
 *
 * 三者都没有 → `{ deadlineMs: null }`：前端**不**自行编造时长，
 * 此时只能依赖服务端在写路径上的 409 兜底（T-28）。
 */
export function resolveDeadline(
  facts: SessionTimeFacts | null | undefined,
  nowMs: number,
  fallbackDurationSeconds: number | null = null,
): ResolvedDeadline {
  const safe: SessionTimeFacts = facts || {};

  const remaining = asPositiveNumber(safe.interview_remaining_seconds);
  if (remaining !== null) {
    return { deadlineMs: nowMs + remaining * 1000, source: 'session_remaining' };
  }

  const parsed = parseServerDeadline(safe.deadline_at);
  if (parsed !== null) {
    return { deadlineMs: parsed, source: 'session_deadline' };
  }

  const duration =
    asPositiveNumber(safe.duration_seconds) ?? asPositiveNumber(fallbackDurationSeconds);
  if (duration !== null && duration > 0) {
    return { deadlineMs: nowMs + duration * 1000, source: 'duration' };
  }

  return { deadlineMs: null, source: 'none' };
}

/**
 * 剩余秒数（向上取整、不为负）。
 *
 * 每个 tick 都用 `Date.now()` 重算，而不是 `prev - 1`：标签页被挂起、
 * 系统休眠、浏览器对后台 setInterval 的节流，都会让"逐次自减"比真实时间慢，
 * 用户会看到"还剩 2 分钟"时服务端其实早已判定超时。
 */
export function remainingSeconds(deadlineMs: number | null, nowMs: number): number | null {
  if (deadlineMs === null || !Number.isFinite(deadlineMs)) return null;
  return Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000));
}

/** 倒计时显示（`MM:SS`；未知时给 `--:--`，绝不显示负数）。 */
export function formatCountdown(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '--:--';
  const safe = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(safe / 60)).padStart(2, '0');
  const ss = String(safe % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

/**
 * 服务端是否**判定本场超时**（写路径的 409）。
 *
 * 判别力是这里的重点：`no_active_session` / `version_conflict` 同样是 409，
 * 但它们是"你根本没在面试""并发写冲突"，把这两种也锁 UI 会把用户
 * 卡在一场其实还能继续的面试外面，因此必须按 `code` 精确区分。
 */
export function isTimeoutResponse(status: number, body: unknown): boolean {
  if (status !== 409 || !body || typeof body !== 'object') return false;
  const payload = body as Record<string, unknown>;
  return payload.code === TIMEOUT_CODE || payload.ended_reason === TIMEOUT_REASON;
}

/** `GET /api/interview/session` 的 `last_ended` 是否是一场"因超时结束"的面试。 */
export function isTimeoutEnded(ended: unknown): boolean {
  if (!ended || typeof ended !== 'object') return false;
  return (ended as Record<string, unknown>).ended_reason === TIMEOUT_REASON;
}

/** T-43 / FR-4.5：报告是否属于"超时结束"口径 → 决定是否显示标注文案。 */
export function isTimeoutReport(report: unknown): boolean {
  if (!report || typeof report !== 'object') return false;
  return (report as Record<string, unknown>).ended_reason === TIMEOUT_REASON;
}

/** 锁定时给用户看的文案：服务端 `detail` 优先（它最贴近当前事实）。 */
export function timeoutLockNotice(body?: unknown): TimeoutLockNotice {
  const payload = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
  const detail = typeof payload.detail === 'string' ? payload.detail.trim() : '';
  const hint = typeof payload.hint === 'string' ? payload.hint.trim() : '';
  return {
    title: TIMEOUT_LOCK_TITLE,
    message: detail || TIMEOUT_LOCK_FALLBACK_MESSAGE,
    hint: hint || TIMEOUT_LOCK_HINT,
  };
}
