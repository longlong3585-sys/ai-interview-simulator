/**
 * T-42 端到端验收（**由 `backend/scripts/verify_t42_manual.py` 调用**，
 * 也可手工对着一个已在运行的后端跑）。
 *
 * 与 `tests/interview-timeout.test.mjs` 的分工：
 *   * 那个文件是**离线契约测试**（纯逻辑 + 源码形状 + 后端字面量对齐）；
 *   * 这个脚本是**真刀真枪的端到端**：真的起会话、真的等时间走过去、
 *     真的收到 409 `interview_timeout`，并且**用的是前端自己的那份逻辑**
 *     （`src/interview/timeout.ts`）来做判定 ——
 *     而不是在脚本里重写一份"我以为前端会这么做"的实现。
 *
 * 因此它证明的是：**前端拿着服务端下发的真实数据，会判断出"该锁了"。**
 * 唯一不覆盖的是 React 的渲染结果（"输入区是否真的从 DOM 里消失"），
 * 那由离线契约测试的源码断言负责 —— 脚本不会假装自己点了浏览器。
 *
 * 用法：
 *   node tests/e2e-timeout-live.mjs --base-url http://127.0.0.1:8000 \
 *        --token <JWT> --duration 12
 *
 * 退出码：0 = 通过；1 = 有断言失败；2 = 参数/环境问题。
 */

import {
  TIMEOUT_REASON,
  formatCountdown,
  isTimeoutEnded,
  isTimeoutReport,
  isTimeoutResponse,
  parseServerDeadline,
  remainingSeconds,
  resolveDeadline,
  timeoutLockNotice,
} from '../src/interview/timeout.ts';

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ''), process.argv[i + 1]);
}

const BASE_URL = (args.get('base-url') || '').replace(/\/+$/, '');
const TOKEN = args.get('token') || '';
const DURATION = Number(args.get('duration') || 12);

if (!BASE_URL || !TOKEN || !Number.isFinite(DURATION)) {
  console.error('用法：node tests/e2e-timeout-live.mjs --base-url <url> --token <jwt> --duration <秒>');
  process.exit(2);
}
if (DURATION < 3) {
  console.error(`--duration 太小（${DURATION}）：倒计时与等待会有 1~2 秒的抖动，请给 >= 3 秒。`);
  process.exit(2);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const failures = [];

function say(msg = '') {
  console.log(msg);
}
function pass(msg) {
  say(`  [PASS] ${msg}`);
}
function fail(msg) {
  failures.push(msg);
  say(`  [FAIL] ${msg}`);
}
function section(title) {
  say('');
  say('-'.repeat(72));
  say(title);
  say('-'.repeat(72));
}
/** 断言 + 证据行，避免"通过了但看不出通过了什么"。 */
function check(cond, msg, evidence) {
  if (cond) pass(evidence ? `${msg} —— ${evidence}` : msg);
  else fail(evidence ? `${msg} —— 实际：${evidence}` : msg);
  return !!cond;
}

async function request(method, apiPath, { json, form, token = TOKEN } = {}) {
  const headers = {};
  let body;
  if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    body = new URLSearchParams(form).toString();
  } else if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${BASE_URL}${apiPath}`, { method, headers, body });
  let payload;
  const text = await res.text();
  try {
    payload = JSON.parse(text);
  } catch {
    payload = text;
  }
  return { status: res.status, body: payload };
}

async function main() {
  section(`A  服务端是面试时长的唯一来源（GET /api/interview/config）`);
  const cfg = await request('GET', '/api/interview/config');
  check(cfg.status === 200, 'config 返回 200', `HTTP ${cfg.status}`);
  check(
    cfg.body && cfg.body.duration_seconds === DURATION,
    `下发 duration_seconds = ${DURATION}（本次服务端启动时设定的值）`,
    `duration_seconds=${cfg.body && cfg.body.duration_seconds}`,
  );

  section('B  开始一场面试（POST /api/start_interview）');
  const questions = [
    '请介绍一下你最近负责的一个后端服务，它的瓶颈在哪？',
    '你如何定位线上偶发的超时问题？',
    '解释一下你理解的幂等设计。',
  ];
  const started = await request('POST', '/api/start_interview', {
    form: {
      role: '后端开发',
      resume_text: '（T-42 验收用的占位简历：三年后端开发经验。）',
      questions_json: JSON.stringify(questions),
    },
  });
  check(started.status === 200, 'start_interview 返回 200', `HTTP ${started.status}`);
  const firstSessionId = started.body && started.body.session_id;
  check(!!firstSessionId, '拿到 session_id', String(firstSessionId));

  section('C  倒计时以**服务端死线**为准（含时区交叉校验）');
  const sessionRes = await request('GET', '/api/interview/session');
  const session = sessionRes.body && sessionRes.body.session;
  check(sessionRes.status === 200 && !!session, '会话读接口返回 session', `HTTP ${sessionRes.status}`);
  if (!session) return;

  const now = Date.now();
  const resolved = resolveDeadline(session, now);
  check(resolved.source !== 'none', '前端能从服务端字段算出死线', `source=${resolved.source}`);
  const left = remainingSeconds(resolved.deadlineMs, now);
  say(`  服务端字段: deadline_at=${session.deadline_at} interview_remaining_seconds=${session.interview_remaining_seconds} duration_seconds=${session.duration_seconds}`);
  say(`  前端算出的倒计时: ${formatCountdown(left)}（剩余 ${left} 秒）`);
  check(
    left !== null && left > 0 && left <= DURATION + 2,
    `倒计时不超过服务端设定的时长（无时区偏移）`,
    `剩余 ${left} 秒，上限 ${DURATION + 2} 秒`,
  );

  // 时区陷阱的真数据交叉校验：绝对时刻（无时区串，按 UTC 解释）与
  // 服务端给的相对量必须在 3 秒内一致。若 deadline_at 被当成本地时间，
  // 在 UTC+8 上这里会差出 8 小时 —— 而这正是"UI 永远不锁"的根因。
  const byAbsolute = parseServerDeadline(session.deadline_at);
  const byRelative = now + Number(session.interview_remaining_seconds) * 1000;
  if (byAbsolute === null || !Number.isFinite(Number(session.interview_remaining_seconds))) {
    fail('服务端未同时下发 deadline_at 与 interview_remaining_seconds，无法做时区交叉校验');
  } else {
    const skew = Math.abs(byAbsolute - byRelative);
    check(skew <= 3000, 'deadline_at 与 interview_remaining_seconds 互相印证（按 UTC 解释）', `偏差 ${skew} 毫秒`);
  }

  section('D  不误伤：没到点照常能答题');
  const early = await request('POST', '/api/chat', {
    json: { message: '我负责过一个订单服务，主要瓶颈是库存扣减的行锁竞争。', role: '后端开发' },
  });
  check(early.status === 200, '到点前的 /api/chat 返回 200', `HTTP ${early.status}`);
  check(
    !isTimeoutResponse(early.status, early.body),
    '正常响应不会被误判成超时（前端不会错误锁定）',
    `isTimeoutResponse=${isTimeoutResponse(early.status, early.body)}`,
  );

  section(`E  等真实时间走过去（不做任何伪造，等待到死线 + 1.5 秒）`);
  const deadlineAt = resolved.deadlineMs;
  let sawZero = false;
  let zeroAt = null;
  let lastShown = null;
  while (Date.now() < deadlineAt + 1500) {
    const nowTick = Date.now();
    const remain = remainingSeconds(deadlineAt, nowTick);
    const shown = formatCountdown(remain);
    if (shown !== lastShown) {
      say(`  ${new Date(nowTick).toISOString().slice(11, 19)}  ${shown}`);
      lastShown = shown;
    }
    if (remain === 0 && !sawZero) {
      sawZero = true;
      zeroAt = nowTick;
    }
    await sleep(150);
  }
  check(sawZero, '倒计时确实走到了 00:00（前端据此锁定 UI）', zeroAt ? `首次归零时刻与死线相差 ${zeroAt - deadlineAt} 毫秒` : '从未归零');
  check(
    sawZero && Math.abs(zeroAt - deadlineAt) <= 2000,
    '归零时刻与服务端死线一致（不是本地硬编码的时长）',
    sawZero ? `偏差 ${zeroAt - deadlineAt} 毫秒` : '未归零',
  );

  section('F  到点后写路径一律 409 interview_timeout → 前端锁定');
  const late = await request('POST', '/api/chat', {
    json: { message: '那我再补充一点：我们后来引入了 Redis 预扣。', role: '后端开发' },
  });
  const locked = isTimeoutResponse(late.status, late.body);
  check(late.status === 409, '到点后的 /api/chat 返回 409', `HTTP ${late.status}`);
  check(locked, '前端会判定为"超时"并锁定 UI', JSON.stringify({ code: late.body && late.body.code, ended_reason: late.body && late.body.ended_reason }));
  const notice = timeoutLockNotice(late.body);
  check(
    notice.message === (late.body && late.body.detail),
    '锁定 Toast/面板文案直接来自服务端 detail（用户看到的是准确原因）',
    notice.message,
  );
  check(
    (late.body && late.body.ended_reason) === TIMEOUT_REASON,
    '服务端同时下发 ended_reason=timeout（T-43 标注依赖它）',
    String(late.body && late.body.ended_reason),
  );
  if (late.status !== 409) {
    say('  ⚠️ 服务端没有拦住超时后的答题 —— 前端锁定可被绕过（T-28 不成立）。');
  }

  // 第二次请求：会话已经在第一次请求里被结算，此时"没有进行中的会话"
  // 才是准确描述（docs/28 §4）。关键是它**不能再被当成超时**去重复锁定/提示。
  const second = await request('POST', '/api/chat', {
    json: { message: '还在吗？', role: '后端开发' },
  });
  check(
    second.status === 409 && !isTimeoutResponse(second.status, second.body),
    '紧接着的第二次请求是 no_active_session（不是超时），前端不会重复锁',
    `HTTP ${second.status} code=${second.body && second.body.code}`,
  );

  section('G  刷新页面也能说清"因超时已自动结束"（last_ended）');
  const afterRes = await request('GET', '/api/interview/session');
  const after = afterRes.body || {};
  check(after.session === null, '超时后 session 为 null', `session=${JSON.stringify(after.session)}`);
  check(
    isTimeoutEnded(after.last_ended),
    'last_ended.ended_reason=timeout —— 前端据此锁定并说明原因',
    JSON.stringify(after.last_ended && { status: after.last_ended.status, ended_reason: after.last_ended.ended_reason }),
  );

  section('H  超时报告口径（T-43 标注的数据来源）');
  const reportRes = await request('POST', '/api/generate_report', { json: {} });
  const report = reportRes.body || {};
  check(reportRes.status === 200, '超时后仍能生成报告', `HTTP ${reportRes.status}`);
  check(
    isTimeoutReport(report),
    '报告体带 ended_reason=timeout —— 报告页据此显示「因超时自动结束…」标注',
    `ended_reason=${report.ended_reason}`,
  );
  check(
    typeof report.answered_count === 'number',
    '报告带服务端裁决的 answered_count（未及作答不计入扣分）',
    `answered_count=${report.answered_count} total_questions=${report.total_questions}`,
  );

  section('I  超时后立刻可以重开（ADR-022R：不得把用户锁死在门外）');
  const restarted = await request('POST', '/api/start_interview', {
    form: { role: '后端开发', resume_text: '', questions_json: JSON.stringify(questions) },
  });
  check(restarted.status === 200, '超时后立刻能开新面试', `HTTP ${restarted.status}`);
  const newSessionId = restarted.body && restarted.body.session_id;
  check(
    !!newSessionId && newSessionId !== firstSessionId,
    '拿到的是一个新 session_id（旧会话确实已结算）',
    `${firstSessionId} -> ${newSessionId}`,
  );

  // 收尾：把刚开的这场也结束掉。
  // 不这么做，验收跑完会**留下一场 active 会话**：后面任何一次
  // `POST /api/generate_report`（脚本的兜底取证、或人工复查）都会评到
  // 这一场新面试，而不是我们刚验证过的那场超时会话 —— 证据就串了。
  const abandoned = await request('POST', '/api/interview/abandon', { json: {} });
  check(abandoned.status === 200, '收尾：把新开的那场主动结束（不留 active 会话给后续取证添乱）', `HTTP ${abandoned.status}`);
}

try {
  await main();
} catch (err) {
  fail(`脚本异常：${err && err.stack ? err.stack : err}`);
}

say('');
say('='.repeat(72));
if (failures.length === 0) {
  say('✅ T-42 端到端验收通过：倒计时来自服务端死线（无时区偏移）、');
  say('   到点后 /api/chat 返回 409 interview_timeout、前端据此判定需要锁定；');
  say('   last_ended 与报告体都带 timeout 口径（T-43 标注有据可依）。');
  process.exit(0);
}
say(`❌ T-42 端到端验收未通过：${failures.length} 项失败`);
for (const f of failures) say(`   - ${f}`);
process.exit(1);
