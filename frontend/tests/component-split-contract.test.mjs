/**
 * T-46 / T-47 契约测试：**单文件 ≤400 行 + App.tsx 降为路由/页面装配**。
 *
 * 运行：cd frontend && npm run test:node      （零依赖、不联网、毫秒级）
 *
 * ── 为什么又做"源码结构"断言 ──────────────────────────────────────────────
 * T-46 / T-47 的验收标准（`docs/03-tasks.md` §阶段 4）不是某个函数返回什么，
 * 而是**代码形状**：
 *   · 单文件 ≤400 行；
 *   · `App.tsx` 降为"路由装配"。
 * 本机装不上 vitest / jsdom（`node_modules` 里没有），所以只能扫源码。
 * 同目录先例：route-guard（Hook 顺序）/ api-parity / dead-params / skip-words-parity。
 *
 * ── 每条断言都配"判别力自检" ──────────────────────────────────────────────
 * 只写"现状通过"的断言等于没有断言：把拆分前的形状喂进同一个判定函数，
 * 必须判为失败。因此 `isThinAssembly()` / `overBudget()` 都是可复用的纯函数，
 * 测试里既喂真实源码，也喂修复前的合成样本。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(HERE, '..', 'src');

const read = (rel) => readFileSync(path.join(SRC, rel), 'utf8');
const linesOf = (src) => src.split('\n').length;

/** 收集 src 下全部 .ts/.tsx（相对路径 → 源码）。 */
function collectSources() {
  const out = new Map();
  const walk = (dir, prefix) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), rel);
      else if (/\.(ts|tsx)$/.test(entry.name)) out.set(rel, readFileSync(path.join(dir, entry.name), 'utf8'));
    }
  };
  walk(SRC, '');
  return out;
}

const SOURCES = collectSources();

const APP = read('App.tsx');
const INTERVIEW_ROOM = read('interview/InterviewRoom.tsx');
const REPORT_VIEW = read('report/ReportView.tsx');
const SESSION = read('interview/useInterviewSession.ts');
const CHAT = read('interview/useInterviewChat.ts');
const TIMEOUT = read('interview/useInterviewTimeout.ts');
const NOTIF_CENTER = read('notifications/NotificationCenter.tsx');
const NOTIF_HOOK = read('notifications/useNotificationCenter.ts');
const PROFILE = read('profile/ProfilePanel.tsx');
const AUTH_MODAL = read('auth/AuthModal.tsx');
const QUESTION_BANK_MODAL = read('interview/QuestionBankModal.tsx');
const QUESTION_BANK_DATA = read('interview/questionBank.ts');

/** T-46 / T-47 的交付物清单（少一个即视为没拆完）。 */
const DELIVERABLES = {
  'interview/useInterviewTimeout.ts': TIMEOUT,
  'interview/useSpeech.ts': read('interview/useSpeech.ts'),
  'interview/useInterviewChat.ts': CHAT,
  'interview/useInterviewSession.ts': SESSION,
  'interview/InterviewRoom.tsx': INTERVIEW_ROOM,
  'interview/resumeUpload.ts': read('interview/resumeUpload.ts'),
  'interview/questionBank.ts': QUESTION_BANK_DATA,
  'interview/QuestionBankModal.tsx': QUESTION_BANK_MODAL,
  'report/ReportView.tsx': REPORT_VIEW,
  'notifications/useNotificationCenter.ts': NOTIF_HOOK,
  'notifications/NotificationCenter.tsx': NOTIF_CENTER,
  'profile/ProfilePanel.tsx': PROFILE,
  'auth/AuthModal.tsx': AUTH_MODAL,
};

// ---------------------------------------------------------------------------
// 判定函数（可复用 → 才能做判别力自检）
// ---------------------------------------------------------------------------

/** 行数预算：单文件 ≤400 行。返回超预算的文件名列表。 */
function overBudget(sources, budget = 400) {
  return [...sources.entries()]
    .filter(([, src]) => linesOf(src) > budget)
    .map(([name, src]) => `${name}(${linesOf(src)} 行)`);
}

/**
 * "App.tsx 是否已降为装配层"。
 *
 * 判据：它**不再自己实现任何一屏**，只 import 并组合各屏幕组件 + 持有面板开关。
 * 只要还残留面试主流程 / 报告 / 通知 / 资料 / 登录 / 题库的实现，就算没拆完。
 */
const IMPLEMENTATION_MARKERS = [
  /const sendMessage\s*=/,
  /const startInterview\s*=/,
  /const endInterview\s*=/,
  /const handleResumeFile\s*=/,
  /const loadHistory\s*=/,
  /const handleAuth\s*=/,
  /const loadCaptcha\s*=/,
  /const handleChangePassword\s*=/,
  /const markAllRead\s*=/,
  /data-testid="interview-locked"/,
  /data-testid="report-timeout-note"/,
  /data-testid="interview-countdown"/,
];

function assemblyViolations(appSource) {
  return IMPLEMENTATION_MARKERS.filter((re) => re.test(appSource)).map((re) => re.source);
}

function requiredAssemblyPieces(appSource) {
  return ['<InterviewRoom', '<ReportView', '<NotificationCenter', '<ProfilePanel', '<AuthModal', '<QuestionBankModal']
    .filter((needle) => !appSource.includes(needle));
}

// ---------------------------------------------------------------------------
// 1. 交付物 + 行数预算
// ---------------------------------------------------------------------------

test('T-46/T-47：全部目标文件都存在且非空（防止路径写错导致假通过）', () => {
  for (const [name, src] of Object.entries(DELIVERABLES)) {
    assert.ok(existsSync(path.join(SRC, name)), `${name} 不存在`);
    assert.ok(src.length > 200, `${name} 内容过短（${src.length} 字节）`);
  }
  assert.ok(Object.keys(DELIVERABLES).length >= 13, '交付物清单被误删');
});

test('T-45~T-47：全树单文件都在 ≤400 行预算内', () => {
  const over = overBudget(SOURCES);
  assert.deepEqual(over, [], `以下文件超出 ≤400 行预算：${over.join('、')}`);
  assert.ok(SOURCES.size >= 25, `只扫到 ${SOURCES.size} 个源文件，收集器可能失效`);
});

test('判别力：行数预算判定必须能抓住超预算文件（否则预算是恒真断言）', () => {
  const huge = new Map([['synthetic/TooBig.tsx', 'x\n'.repeat(401)]]);
  assert.deepEqual(overBudget(huge), ['synthetic/TooBig.tsx(402 行)']);
  assert.deepEqual(overBudget(new Map([['synthetic/Ok.tsx', 'x\n'.repeat(50)]])), []);
});

// ---------------------------------------------------------------------------
// 2. T-46 ①：App.tsx 降为装配层
// ---------------------------------------------------------------------------

test('T-46/T-47：App.tsx 已降为路由/页面装配（≤400 行且不再实现任何一屏）', () => {
  assert.ok(linesOf(APP) <= 400, `App.tsx 仍有 ${linesOf(APP)} 行（T-45 结束时尚有 2189 行）`);
  const violations = assemblyViolations(APP);
  assert.deepEqual(violations, [], `App.tsx 里仍残留界面/逻辑实现：${violations.join('、')}`);
  const missing = requiredAssemblyPieces(APP);
  assert.deepEqual(missing, [], `App.tsx 缺少装配：${missing.join('、')}`);
});

test('判别力：拆分前的 App.tsx 形状必须被判为"不是装配层"', () => {
  // 修复前 App.tsx 的真实特征（2189 行里既有实现又有内联 JSX）
  const before = `
    function App() {
      const sendMessage = async () => {};
      const startInterview = async () => {};
      const endInterview = async () => {};
      return <div data-testid="interview-locked"><textarea data-testid="interview-countdown" /></div>;
    }
  `;
  assert.notDeepEqual(assemblyViolations(before), [], '判别力失效：修复前的写法被判成了装配层');
  assert.ok(linesOf('x\n'.repeat(2189)) > 400, '行数断言本身应能识别 2189 行');
});

// ---------------------------------------------------------------------------
// 3. 单一所有者：状态与行为不允许"两边都有一份"
// ---------------------------------------------------------------------------

test('T-46：面试状态只有一处声明（不会拆出"两份真源"）', () => {
  const owners = (needle) =>
    [...SOURCES.entries()].filter(([, src]) => src.includes(needle)).map(([name]) => name);

  for (const [label, needle] of [
    ['useState(false) 的锁定态', 'const [interviewLocked, setInterviewLocked]'],
    ['倒计时状态', 'const [timeLeft, setTimeLeft]'],
    ['结束面试的最新回调 ref', 'const endInterviewRef = useRef'],
  ]) {
    const found = owners(needle);
    assert.equal(found.length, 1, `${label} 出现在 ${found.length} 个文件里：${found.join('、')}`);
  }
  // 两条写路径只允许有一份实现
  for (const needle of ['const sendMessage = async () => {', 'const skipQuestion = async () => {']) {
    const found = owners(needle);
    assert.equal(found.length, 1, `「${needle}」在 ${found.length} 个文件里各写了一份：${found.join('、')}`);
  }
});

test('T-47：每个页面级组件只有一份实现，且 App 只 import 不重写', () => {
  for (const [label, file, marker] of [
    ['报告页', 'report/ReportView.tsx', 'export function ReportView'],
    ['消息中心', 'notifications/NotificationCenter.tsx', 'export function NotificationCenter'],
    ['个人中心', 'profile/ProfilePanel.tsx', 'export function ProfilePanel'],
    ['登录注册', 'auth/AuthModal.tsx', 'export function AuthModal'],
    ['题库弹窗', 'interview/QuestionBankModal.tsx', 'export function QuestionBankModal'],
    ['面试主流程', 'interview/InterviewRoom.tsx', 'export function InterviewRoom'],
  ]) {
    assert.ok(SOURCES.get(file).includes(marker), `${label} 未在 ${file} 里导出组件`);
    const dup = [...SOURCES.entries()].filter(([, src]) => src.includes(marker)).map(([n]) => n);
    assert.deepEqual(dup, [file], `${label} 的实现出现在多个文件：${dup.join('、')}`);
  }
});

test('T-46/T-47：每个 API 调用只有一个归属文件（拆分不产生重复请求）', () => {
  // 必须去注释：注释里会引用别的接口名（例如 timeout.ts 解释 409 时提到 /api/chat），
  // 那属于文档，不是"第二处调用"。
  const codeOnly = (src) =>
    src
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .split(/\r?\n/)
      .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\s\/\/.*$/, '')))
      .join('\n');
  const code = new Map([...SOURCES.entries()].map(([name, src]) => [name, codeOnly(src)]));
  const callers = (endpoint) =>
    [...code.entries()].filter(([, src]) => src.includes(endpoint)).map(([name]) => name).sort();

  assert.deepEqual(callers('/api/chat'), ['interview/useInterviewChat.ts']);
  assert.deepEqual(callers('/api/skip_question'), ['interview/useInterviewChat.ts']);
  assert.deepEqual(callers('/api/generate_report'), ['interview/useInterviewSession.ts']);
  assert.deepEqual(callers('/api/start_interview'), ['interview/useInterviewSession.ts']);
  assert.deepEqual(callers('/api/resume/upload'), ['interview/resumeUpload.ts']);
  assert.deepEqual(callers('/api/interview/config'), ['interview/useInterviewSession.ts']);
  assert.deepEqual(callers('/api/change_password'), ['profile/ProfilePanel.tsx']);
  assert.deepEqual(callers('/api/login'), ['auth/AuthModal.tsx']);
  assert.deepEqual(callers('/api/notifications/read_all'), ['notifications/useNotificationCenter.ts']);
  assert.deepEqual(callers('/api/history_item'), ['notifications/useNotificationCenter.ts']);
  // 题库：数据层一个入口（原先那个从未被引用的 `src/QuestionBank.tsx` 已在 T-48 删除）
  assert.deepEqual(callers('/api/question_bank'), ['interview/questionBank.ts'], '题库取数归属不唯一');
  assert.ok(!code.get('App.tsx').includes('/api/question_bank'), 'App.tsx 里仍内联题库取数');
});

// ---------------------------------------------------------------------------
// 4. 拆分不许制造循环依赖
// ---------------------------------------------------------------------------

test('T-46/T-47：依赖方向单向 —— 没有任何模块 import App.tsx', () => {
  const offenders = [...SOURCES.entries()]
    .filter(([name]) => name !== 'main.tsx')
    .filter(([, src]) => /from '\.\.?\/App(\.tsx)?'/.test(src) || /from '\.\/App(\.tsx)?'/.test(src))
    .map(([name]) => name);
  assert.deepEqual(offenders, [], `这些文件反向 import 了 App.tsx：${offenders.join('、')}`);
  // 判别力：main.tsx 是唯一合法的 App 引入方
  assert.match(SOURCES.get('main.tsx'), /import App from '\.\/App\.tsx'/);
});

test('T-46/T-47：视图层不自己发请求（数据只从 hook/props 来）', () => {
  const viewFiles = {
    'interview/InterviewRoom.tsx': INTERVIEW_ROOM,
    'report/ReportView.tsx': REPORT_VIEW,
    'notifications/NotificationCenter.tsx': NOTIF_CENTER,
  };
  for (const [name, src] of Object.entries(viewFiles)) {
    assert.ok(!/await\s+fetch\(/.test(src), `${name} 里出现了裸 fetch（视图层应只渲染）`);
    assert.ok(!/await\s+authFetch\(/.test(src), `${name} 里出现了 authFetch（视图层应只渲染）`);
  }
});
