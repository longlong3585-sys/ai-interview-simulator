/**
 * T-42 / T-43 契约测试（零依赖，`node:test` 运行，**不需要**起后端、不联网）。
 *
 * 分三层，缺一层都会留下"看起来改好了"的假象：
 *
 *   1. **纯逻辑**（`src/interview/timeout.ts`）：服务端时间 → 死线 → 倒计时 → 锁定判定。
 *      这层用固定时钟断言，毫秒级跑完 —— 超时逻辑靠手点页面验证是不可能的。
 *   2. **接线**（`src/App.tsx`）：真的用了上面这些函数，且**没有**第二份硬编码时长、
 *      没有退回逐秒自减、锁定态真的把输入区从 DOM 里摘掉。
 *   3. **服务端契约对齐**：前端判定的 `interview_timeout` / `timeout` 字面量，
 *      必须与后端路由、`EndedReason` 一致 —— 否则前端永远等不到那次 409。
 *
 * 运行：cd frontend && npm run test:node
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  TIMEOUT_CODE,
  TIMEOUT_REASON,
  TIMEOUT_REPORT_NOTE,
  formatCountdown,
  isTimeoutEnded,
  isTimeoutReport,
  isTimeoutResponse,
  parseServerDeadline,
  remainingSeconds,
  resolveDeadline,
  timeoutLockNotice,
} from '../src/interview/timeout.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const APP_TSX = path.join(REPO_ROOT, 'frontend', 'src', 'App.tsx');
const REPORT_VIEW = path.join(REPO_ROOT, 'frontend', 'src', 'report', 'ReportView.tsx');
const MODULE_TS = path.join(REPO_ROOT, 'frontend', 'src', 'interview', 'timeout.ts');
const INTERVIEW_PY = path.join(REPO_ROOT, 'backend', 'routers', 'interview.py');
const BASE_PY = path.join(REPO_ROOT, 'backend', 'services', 'stores', 'base.py');

const appSource = readFileSync(APP_TSX, 'utf8');
const reportViewSource = readFileSync(REPORT_VIEW, 'utf8');
const moduleSource = readFileSync(MODULE_TS, 'utf8');
const pySource = readFileSync(INTERVIEW_PY, 'utf8');
const baseSource = readFileSync(BASE_PY, 'utf8');

/**
 * T-46 / T-47 之后，"接线"不再集中在一个文件里（面谈主流程拆去了
 * `src/interview/*`，报告页拆去了 `src/report/ReportView.tsx`）。
 *
 * 因此这一层断言必须扫**整棵 src 树**：只盯 `App.tsx` 的话，
 * 任何人把逻辑挪到别的文件都会让护栏**静默失效** ——
 * 拆分时这里确实一次报出 9 条"假警报"（代码没问题，是护栏忘了一起搬）。
 * 只扫源码文件（`.ts` / `.tsx`），排除构建产物与测试。
 */
function allFrontendSource() {
  const files = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(ts|tsx)$/.test(entry.name)) files.push(full);
    }
  };
  walk(path.join(REPO_ROOT, 'frontend', 'src'));
  files.sort();
  return files.map((f) => readFileSync(f, 'utf8')).join('\n');
}

/**
 * 去掉注释后再做"代码形状"断言。
 *
 * 必须这么做：本次修复的注释里**原样引用**了被删掉的旧代码
 * （`setTimeLeft(15 * 60)`、`setTimeout(() => endInterview(), 1500)`）——
 * 那是最有价值的说明，却会让"不得再出现"的断言变成假警报。
 */
function codeOnly(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(/\r?\n/)
    .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\s\/\/.*$/, '')))
    .join('\n');
}

const appCode = codeOnly(appSource);
/** 全树源码（去注释）：T-46 / T-47 之后接线断言的口径。 */
const frontCode = codeOnly(allFrontendSource());

// ---------------------------------------------------------------------------
// 1. 纯逻辑：服务端时间 → 死线
// ---------------------------------------------------------------------------

test('服务端无时区的 deadline_at 必须按 UTC 解释（时区陷阱）', () => {
  // 后端 `interview_deadline()` 用 `datetime.utcnow()` 派生，下发的是
  // 形如 `2026-09-29T17:10:51` 的**无时区**串。JS 的 new Date() 会按本地时区
  // 解释它 —— 在 UTC+8 下，整条倒计时凭空多出 8 小时，UI 永远不会锁。
  assert.equal(
    parseServerDeadline('2026-09-29T17:10:51'),
    Date.parse('2026-09-29T17:10:51Z'),
  );

  const now = Date.parse('2026-09-29T17:09:51Z'); // 死线前 60 秒
  const resolved = resolveDeadline({ deadline_at: '2026-09-29T17:10:51' }, now);
  assert.equal(resolved.deadlineMs - now, 60_000, '剩余时间必须是 60 秒，而不是 60 秒 ± 时区偏移');
  assert.equal(resolved.source, 'session_deadline');
});

test('时区陷阱的判别力：本地时区非 UTC 时，解析结果必须区别于本地解释', () => {
  const naive = '2026-09-29T17:10:51';
  const localInterpretation = new Date(2026, 8, 29, 17, 10, 51).getTime();
  if (localInterpretation === Date.parse(`${naive}Z`)) {
    // 跑在 UTC 机器上 —— 此时两种解释相同，本用例无法提供判别力。
    // 这恰恰说明"这段 bug 在 CI（UTC）上永远看不见"，所以上面的断言
    // 必须写成与 UTC 解释相等，而不是"与本地解释相等"。
    return;
  }
  assert.notEqual(
    parseServerDeadline(naive),
    localInterpretation,
    '无时区串被当成了本地时间（UTC+8 下会多出 8 小时倒计时）',
  );
});

test('带时区标记的时间串按其偏移解释，不做任何"修正"', () => {
  assert.equal(parseServerDeadline('2026-09-29T17:10:51Z'), Date.parse('2026-09-29T17:10:51Z'));
  assert.equal(
    parseServerDeadline('2026-09-30T01:10:51+08:00'),
    Date.parse('2026-09-29T17:10:51Z'),
  );
  assert.equal(parseServerDeadline('2026-09-29T17:10:51.250'), Date.parse('2026-09-29T17:10:51.250Z'));
});

test('无法解析 / 缺失的时间一律返回 null（宁可不算，也不误杀）', () => {
  for (const bad of [null, undefined, '', '   ', 'not-a-date', 42, {}]) {
    assert.equal(parseServerDeadline(bad), null, `parseServerDeadline(${JSON.stringify(bad)}) 应为 null`);
  }
});

test('死线优先级：相对量 > 绝对时刻 > 兜底时长', () => {
  const now = Date.parse('2026-09-29T17:00:00Z');

  // ① 服务端给了 interview_remaining_seconds（相对量，免疫时钟偏差）
  const byRemaining = resolveDeadline(
    { interview_remaining_seconds: 120, deadline_at: '2026-09-29T23:00:00', duration_seconds: 900 },
    now,
  );
  assert.equal(byRemaining.source, 'session_remaining');
  assert.equal(byRemaining.deadlineMs, now + 120_000);

  // ② 只有绝对时刻
  const byDeadline = resolveDeadline({ deadline_at: '2026-09-29T17:15:00' }, now);
  assert.equal(byDeadline.source, 'session_deadline');
  assert.equal(byDeadline.deadlineMs, now + 900_000);

  // ③ 只有时长（config 下发的 duration_seconds，或调用方传入的兜底）
  const byDuration = resolveDeadline({ duration_seconds: 300 }, now);
  assert.equal(byDuration.source, 'duration');
  assert.equal(byDuration.deadlineMs, now + 300_000);

  const byFallback = resolveDeadline({}, now, 240);
  assert.equal(byFallback.source, 'duration');
  assert.equal(byFallback.deadlineMs, now + 240_000);

  // ④ 什么都没有 → 不自行编造时长（此时只靠服务端 409 兜底）
  const nothing = resolveDeadline({}, now);
  assert.equal(nothing.source, 'none');
  assert.equal(nothing.deadlineMs, null);
});

test('服务端说"剩余 0 秒"必须立刻锁，绝不能回落到兜底时长再送 15 分钟', () => {
  const now = Date.parse('2026-09-29T17:00:00Z');
  const resolved = resolveDeadline(
    { interview_remaining_seconds: 0, duration_seconds: 900 },
    now,
  );
  assert.equal(resolved.source, 'session_remaining', '0 是有效值，不能被当成"没给"');
  assert.equal(resolved.deadlineMs, now);
  assert.equal(remainingSeconds(resolved.deadlineMs, now), 0, '剩余 0 秒就必须触发锁定');
});

test('倒计时按死线重算（不是逐秒自减），归零后不为负', () => {
  const deadline = Date.parse('2026-09-29T17:15:00Z');
  assert.equal(remainingSeconds(deadline, deadline - 900_000), 900);
  assert.equal(remainingSeconds(deadline, deadline - 1_500), 2, '不足 1 秒向上取整，避免提前显示 00:00');
  assert.equal(remainingSeconds(deadline, deadline), 0);
  assert.equal(remainingSeconds(deadline, deadline + 60_000), 0, '过期后不得出现负数');
  assert.equal(remainingSeconds(null, deadline), null);
});

test('倒计时展示为 MM:SS，且不显示负数', () => {
  assert.equal(formatCountdown(900), '15:00');
  assert.equal(formatCountdown(59), '00:59');
  assert.equal(formatCountdown(0), '00:00');
  assert.equal(formatCountdown(-5), '00:00');
  assert.equal(formatCountdown(null), '--:--');
});

// ---------------------------------------------------------------------------
// 2. 纯逻辑：超时判定与文案
// ---------------------------------------------------------------------------

test('只有 409 + interview_timeout 才算超时（判别力）', () => {
  assert.equal(
    isTimeoutResponse(409, { code: 'interview_timeout', ended_reason: 'timeout', detail: 'x' }),
    true,
  );
  // 只有 ended_reason 也要认（服务端两种标识都下发）
  assert.equal(isTimeoutResponse(409, { ended_reason: 'timeout' }), true);

  // 同样是 409，但这两种**不是**超时：锁了 UI 会把用户卡在一场还能继续的面试外面
  assert.equal(isTimeoutResponse(409, { code: 'no_active_session' }), false, 'no_active_session 不是超时');
  assert.equal(isTimeoutResponse(409, { code: 'version_conflict' }), false, '并发冲突不是超时');
  // 其它状态码一律不认
  assert.equal(isTimeoutResponse(200, { code: 'interview_timeout' }), false);
  assert.equal(isTimeoutResponse(500, { code: 'interview_timeout' }), false);
  assert.equal(isTimeoutResponse(409, null), false);
  assert.equal(isTimeoutResponse(409, 'boom'), false);
});

test('last_ended 与报告体的超时口径判定', () => {
  assert.equal(isTimeoutEnded({ status: 'abandoned', ended_reason: 'timeout' }), true);
  assert.equal(isTimeoutEnded({ status: 'finished', ended_reason: 'completed' }), false);
  assert.equal(isTimeoutEnded(null), false);

  assert.equal(isTimeoutReport({ ended_reason: 'timeout', overall_score: 6 }), true);
  assert.equal(isTimeoutReport({ ended_reason: 'completed' }), false);
  assert.equal(isTimeoutReport({ ended_reason: 'manual' }), false);
  assert.equal(isTimeoutReport(null), false);
});

test('锁定文案优先用服务端的 detail，缺失时才退回默认文案', () => {
  const fromServer = timeoutLockNotice({
    detail: '本场面试已超时自动结束，请开始一场新的面试',
    hint: '面试时长上限为 15 分钟；已答部分仍可生成报告',
  });
  assert.equal(fromServer.message, '本场面试已超时自动结束，请开始一场新的面试');
  assert.match(fromServer.hint, /已答部分仍可生成报告/);

  const fallback = timeoutLockNotice(null);
  assert.match(fallback.message, /超时/);
  assert.match(fallback.hint, /不计入扣分/);
});

test('T-43 标注文案逐字（FR-4.5）', () => {
  assert.equal(TIMEOUT_REPORT_NOTE, '因超时自动结束，仅基于已答部分评分');
});

test('纯逻辑层本身不得再藏一份时长常量', () => {
  assert.doesNotMatch(moduleSource, /15\s*\*\s*60/, 'timeout.ts 里出现了 15 分钟的硬编码');
  assert.doesNotMatch(moduleSource, /\b900\b/, 'timeout.ts 里出现了 900 秒的硬编码');
});

// ---------------------------------------------------------------------------
// 3. 接线：App.tsx 真的用上了这些函数，且没有第二份实现
// ---------------------------------------------------------------------------

/** 找 `<textarea` 是否落在"非锁定"分支里。 */
function textareaIsOutsideLockedBranch(source) {
  const lockedIdx = source.indexOf('data-testid="interview-locked"');
  assert.notEqual(lockedIdx, -1, '前端源码缺少锁定面板（data-testid="interview-locked"）');
  const elseIdx = source.indexOf(') : (', lockedIdx);
  const textareaIdx = source.indexOf('<textarea', lockedIdx);
  assert.notEqual(elseIdx, -1, '锁定面板后面没有找到三元表达式的 else 分支');
  assert.notEqual(textareaIdx, -1, '锁定面板后面没有找到输入框');
  return elseIdx < textareaIdx;
}

test('前端源码不再硬编码面试时长，也不再逐秒自减', () => {
  assert.doesNotMatch(frontCode, /setTimeLeft\(\s*15\s*\*\s*60\s*\)/, '仍有 setTimeLeft(15 * 60)');
  assert.doesNotMatch(frontCode, /return prev - 1/, '倒计时退回逐秒自减（挂起/节流后会比真实时间慢）');
  assert.match(frontCode, /formatCountdown\(/, '未使用 formatCountdown 渲染倒计时');
  assert.match(frontCode, /remainingSeconds\(/, '未按死线重算剩余时间');
});

test('前端源码用服务端下发的 duration_seconds 与权威死线武装倒计时', () => {
  assert.match(frontCode, /duration_seconds/, '未读取服务端下发的 duration_seconds');
  assert.match(frontCode, /armInterviewDeadline\(/, '未用服务端时间武装倒计时');
  assert.match(frontCode, /\/api\/interview\/session/, '未向会话读接口同步权威死线');
  assert.match(frontCode, /syncDeadlineFromServer/, '缺少服务端死线同步入口');
  assert.match(frontCode, /visibilitychange/, '标签页挂起恢复后没有重新校正倒计时');
});

test('前端源码在每一条写路径上识别服务端超时并锁定', () => {
  const hits = frontCode.match(/isTimeoutResponse\(/g) || [];
  assert.ok(hits.length >= 2, `isTimeoutResponse 只出现 ${hits.length} 次（chat 与 skip_question 两条写路径都要拦）`);
  assert.match(frontCode, /lockByServerTimeout\(/, '超时后没有锁定入口');
  assert.match(frontCode, /persistent: true/, '超时 Toast 被设成会自动消失（关键反馈不应被错过）');
  assert.match(frontCode, /ToastHost/, '未挂载 Toast');
  assert.match(frontCode, /isTimeoutEnded\(/, '刷新后发现超时时没有识别 last_ended');
});

test('T-42：超时锁定后输入区被销毁，而不是置灰', () => {
  assert.ok(
    textareaIsOutsideLockedBranch(frontCode),
    '输入框不在"非锁定"分支里 —— 锁定态下输入区仍然存在于 DOM 中',
  );
  assert.match(frontCode, /interviewLocked \? \(/, '输入区没有被锁定态分支包裹');
  assert.match(frontCode, /sendMessage[\s\S]{0,600}?interviewLocked/, 'sendMessage 未在锁定态提前返回');
});

test('这条"输入区在 else 分支"的检查有判别力（防止规则写错导致永远通过）', () => {
  const bad = '<div data-testid="interview-locked">locked</div><textarea />';
  assert.throws(() => textareaIsOutsideLockedBranch(bad), /else 分支/);
});

test('T-42 / Bug 3A：跨渲染的回调走 ref，不再闭包捕获旧 messages', () => {
  assert.match(frontCode, /endInterviewRef\.current = endInterview/, '没有把最新 endInterview 写进 ref');
  const viaRef = frontCode.match(/setTimeout\(\(\) => endInterviewRef\.current\(\)/g) || [];
  assert.ok(viaRef.length >= 2, `只有 ${viaRef.length} 处通过 ref 调用 endInterview（chat 与 skip 各一处）`);
  assert.doesNotMatch(
    frontCode,
    /setTimeout\(\(\) => endInterview\(\)/,
    '仍有 setTimeout 直接调用 endInterview（闭包捕获旧的 messages）',
  );
});

test('T-42：超时后允许零作答直接出报告（服务端 T-27 会给「未及作答」口径）', () => {
  assert.match(
    frontCode,
    /messages\.length === 0 && !(?:timeout\.)?interviewLocked/,
    '零作答的超时会话被前端拦住，用户拿不到报告',
  );
});

test('T-43：超时报告页显示标注文案，非超时报告不显示', () => {
  // T-47：报告页已拆到 src/report/ReportView.tsx，因此这一条**定点**扫那个文件：
  // 用整树 indexOf 会比较"哪个文件恰好排在前面"，属于假证据。
  assert.match(reportViewSource, /data-testid="report-timeout-note"/, '报告页缺少超时标注');
  assert.match(reportViewSource, /isTimeoutReport\(report\)/, '标注没有按 ended_reason 判定（会误伤正常报告）');
  assert.match(reportViewSource, /TIMEOUT_REPORT_NOTE/, '标注未使用统一的文案常量');
  assert.match(
    reportViewSource,
    /isTimeoutReport\(report\)\s*&&\s*\(/,
    '标注没有条件渲染 —— 正常完成的报告也会被贴上"超时"标签',
  );
});

test('T-47：App.tsx 只做装配 —— 报告屏与面试屏分别由 <ReportView> / <InterviewRoom> 承载', () => {
  assert.match(appCode, /<ReportView report=\{session\.report\}/, 'App.tsx 未装配报告视图');
  assert.match(appCode, /<InterviewRoom session=\{session\} \/>/, 'App.tsx 未装配面试视图');
  assert.doesNotMatch(appCode, /data-testid="report-timeout-note"/, '报告页 JSX 仍内联在 App.tsx');
  assert.doesNotMatch(appCode, /data-testid="interview-locked"/, '超时锁定面板 JSX 仍内联在 App.tsx');
});

// ---------------------------------------------------------------------------
// 4. 服务端契约对齐（前端等的那个 409 必须真的存在）
// ---------------------------------------------------------------------------

test('前端识别的 code / ended_reason 与后端一致', () => {
  assert.match(pySource, new RegExp(`"code":\\s*"${TIMEOUT_CODE}"`), '后端 409 未下发 interview_timeout');
  assert.match(baseSource, new RegExp(`TIMEOUT\\s*=\\s*"${TIMEOUT_REASON}"`), 'EndedReason.TIMEOUT 与前端不一致');
  assert.match(pySource, /result\["ended_reason"\]\s*=\s*ended_reason/, '报告体未回填 ended_reason');
});

test('后端确实下发前端倒计时所依赖的字段', () => {
  for (const field of ['duration_seconds', 'deadline_at', 'interview_remaining_seconds']) {
    assert.ok(pySource.includes(`"${field}"`), `后端未下发 ${field}`);
  }
});
