/**
 * T-44 / T-45 契约测试：路由化、路由层守卫、以及**条件 Hook** 的回归护栏。
 *
 * 运行：cd frontend && npm run test:node      （零依赖、不联网、毫秒级）
 *
 * ── 为什么是"源码结构"断言 ────────────────────────────────────────────────
 * T-44 的两条验收标准都不是"某个函数返回值对不对"，而是**代码形状**：
 *
 *   ① `AdminPanelContent` 不再在 `useState` 前 `return`；
 *   ② 令牌由有到无不抛错。
 *
 * ② 的直接原因是 ① —— React 的 Hook 顺序契约：同一组件两次渲染调用的 Hook 个数
 * 必须一致。修复前的 `AdminPanelContent` 第一行就是
 *     `if (!token) return <div>请先登录</div>;`
 * 它挡在 7 个 `useState` 之前，于是"有令牌 → 无令牌"这一次渲染只调用 0 个 Hook，
 * React 抛 "Rendered fewer hooks than expected"。
 *
 * 本机装不上 vitest / jsdom（`node_modules` 里没有），所以这里用
 * **一个能识别字符串与模板串的花括号扫描器**把组件体切出来，
 * 断言"早退的位置必须晚于所有 Hook 调用" —— 这正是崩与不崩的分界线。
 * 同目录另有同口径的先例（api-parity / dead-params / skip-words-parity 都做源码契约）。
 *
 * 另有一条**判别力自检**：把修复前的写法喂给同一个断言函数，必须判为失败。
 * 否则"永远通过"的断言等于没有断言。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(HERE, '..', 'src');

const read = (...p) => readFileSync(path.join(SRC, ...p), 'utf8');

const APP = read('App.tsx');
const MAIN = read('main.tsx');
const ADMIN_PANEL = read('admin', 'AdminPanel.tsx');
const REPORT_MODAL = read('admin', 'ReportDetailModal.tsx');
const ADMIN_PAGE = read('admin', 'AdminPage.tsx');
const TABS_STATS = read('admin', 'tabs', 'StatsDashboard.tsx');
const TABS_USERS = read('admin', 'tabs', 'UsersTable.tsx');
const TABS_INTERVIEWS = read('admin', 'tabs', 'InterviewsTable.tsx');
const AUTH_CONTEXT = read('auth', 'AuthContext.tsx');
const REQUIRE_AUTH = read('auth', 'RequireAuth.tsx');

/** 每个源文件都存在的哨兵：防止读错文件导致"假通过"。 */
const FILES = {
  'App.tsx': APP,
  'main.tsx': MAIN,
  'admin/AdminPanel.tsx': ADMIN_PANEL,
  'admin/ReportDetailModal.tsx': REPORT_MODAL,
  'admin/AdminPage.tsx': ADMIN_PAGE,
  'admin/tabs/StatsDashboard.tsx': TABS_STATS,
  'admin/tabs/UsersTable.tsx': TABS_USERS,
  'admin/tabs/InterviewsTable.tsx': TABS_INTERVIEWS,
  'auth/AuthContext.tsx': AUTH_CONTEXT,
  'auth/RequireAuth.tsx': REQUIRE_AUTH,
};

test('T-44/T-45 的全部目标文件都存在且非空（防止路径写错导致假通过）', () => {
  assert.ok(Object.keys(FILES).length >= 8, `只登记了 ${Object.keys(FILES).length} 个文件`);
  for (const [name, src] of Object.entries(FILES)) {
    assert.ok(src.length > 200, `${name} 内容过短（${src.length} 字节），路径或编码可能有问题`);
  }
});

// ---------------------------------------------------------------------------
// 花括号扫描器
// ---------------------------------------------------------------------------

/**
 * 从 `from` 处（该处应为 `open` 字符）找到配对的 `close` 字符，返回其下标。
 * 会跳过：行注释、块注释、单/双引号字符串、模板串（含 `${}` 嵌套）。
 * 默认配对 `{}`；传 `'('` / `')'` 可用于跳过参数表。
 */
function matchBrace(src, from, open = '{', close = '}') {
  assert.equal(
    src[from],
    open,
    `matchBrace 应从 ${open} 开始，实际是 ${JSON.stringify(src[from])}`
  );
  let depth = 0;
  let i = from;
  while (i < src.length) {
    const c = src[i];
    const two = src.slice(i, i + 2);
    if (two === '//') {
      const nl = src.indexOf('\n', i);
      i = nl === -1 ? src.length : nl + 1;
      continue;
    }
    if (two === '/*') {
      const end = src.indexOf('*/', i + 2);
      i = end === -1 ? src.length : end + 2;
      continue;
    }
    if (c === "'" || c === '"') {
      i++;
      while (i < src.length && src[i] !== c) {
        if (src[i] === '\\') i++;
        i++;
      }
      i++;
      continue;
    }
    if (c === '`') {
      i++;
      let tplDepth = 0;
      while (i < src.length) {
        if (src[i] === '\\') {
          i += 2;
          continue;
        }
        if (src[i] === '`' && tplDepth === 0) break;
        if (src.slice(i, i + 2) === '${') {
          tplDepth++;
          i += 2;
          continue;
        }
        if (src[i] === '}' && tplDepth > 0) {
          tplDepth--;
          i++;
          continue;
        }
        i++;
      }
      i++;
      continue;
    }
    if (c === open) depth++;
    else if (c === close) {
      depth--;
      if (depth === 0) return i;
    }
    i++;
  }
  throw new Error(`${open}${close} 未配对 —— 扫描器需要更新`);
}

/** 切出 `function NAME(` 或 `export default function NAME(` 的函数体。 */
function functionBody(src, name) {
  const re = new RegExp(`(?:export\\s+default\\s+)?function\\s+${name}\\s*[<(]`);
  const m = re.exec(src);
  assert.ok(m, `找不到函数 ${name}`);
  const paren = src.indexOf('(', m.index);
  // 关键：参数表里可能就有花括号（`{ token }: Props`），所以必须先整段跳过参数表，
  // 再去找函数体的 `{`。否则会把解构参数当成函数体，切出 16 个字符。
  const paramsEnd = matchBrace(src, paren, '(', ')');
  const open = src.indexOf('{', paramsEnd);
  return src.slice(open + 1, matchBrace(src, open));
}

const HOOK_RE = /\buse(?:State|Effect|Ref|Memo|Callback|Context)\s*(?:<[^>]*>)?\s*\(/g;

/**
 * 去掉注释行，避免注释里的 "return"/"useState" 被当成代码。
 * （只删整行注释与块注释行 —— 本仓库的注释都是整行写的。）
 */
function stripComments(src) {
  return src
    .split('\n')
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .join('\n');
}

/**
 * 找出 body 中所有匹配项，并标注它所在的花括号深度（函数体本身算 0 层）。
 * 跳过字符串/模板串/注释，所以 `{` 出现在字符串里不会被误计。
 */
function findAtDepth(body, re) {
  const depthAt = new Array(body.length).fill(0);
  let depth = 0;
  let i = 0;
  while (i < body.length) {
    const c = body[i];
    const two = body.slice(i, i + 2);
    depthAt[i] = depth;
    if (two === '//') {
      const nl = body.indexOf('\n', i);
      const end = nl === -1 ? body.length : nl;
      for (let k = i; k < end; k++) depthAt[k] = depth;
      i = end;
      continue;
    }
    if (two === '/*') {
      const end = body.indexOf('*/', i + 2);
      const stop = end === -1 ? body.length : end + 2;
      for (let k = i; k < stop; k++) depthAt[k] = depth;
      i = stop;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') {
      const quote = c;
      let k = i + 1;
      while (k < body.length) {
        if (body[k] === '\\') {
          k += 2;
          continue;
        }
        if (body[k] === quote) break;
        k++;
      }
      for (let p = i; p <= Math.min(k, body.length - 1); p++) depthAt[p] = depth;
      i = k + 1;
      continue;
    }
    if (c === '{') {
      depth++;
      depthAt[i] = depth;
    } else if (c === '}') {
      depth--;
      depthAt[i] = depth;
    } else {
      depthAt[i] = depth;
    }
    i++;
  }

  const out = [];
  const rx = new RegExp(re.source, re.flags);
  let m;
  while ((m = rx.exec(body)) !== null) out.push({ index: m.index, text: m[0], depth: depthAt[m.index] });
  return out;
}

/**
 * 审查一个组件体：Hook 调用必须全部早于"早退"（non-final `return`）。
 * 返回 `{ hooks, firstEarlyReturn }` —— 后者为 null 表示没有早退。
 *
 * 深度口径（`functionBody` 切出的是**花括号内部**，所以组件体的顶层是第 0 层）：
 *   · 组件自己的 Hook / `const` / `return <JSX>`   → 第 0 层
 *   · `if (!token) { return … }` 的早退           → 第 1 层（if 块）
 *   · `onClick={async () => { … return; }}`、
 *     `useEffect(() => { return () => … })` 这类**回调里的 return** → 第 2 层以上
 * 因此"组件级 return"取 `depth <= 1`，回调里的 return 会被挡在外面
 * （它们与 Hook 个数无关，不该参与"早退"判定）。
 */
function auditHookOrder(rawBody) {
  const body = stripComments(rawBody);
  const hooks = findAtDepth(body, HOOK_RE).filter((h) => h.depth === 0);
  const topReturns = findAtDepth(body, /\breturn\b/g).filter((r) => r.depth <= 1);

  const finalReturn = topReturns.length ? topReturns[topReturns.length - 1] : null;
  if (!finalReturn) return { hooks, firstEarlyReturn: null, finalReturn: null };

  const earlyReturns = topReturns.filter((r) => r.index !== finalReturn.index);
  const firstEarlyReturn = earlyReturns.length ? earlyReturns[0].index : null;
  return { hooks, firstEarlyReturn, finalReturn: finalReturn.index };
}

test('扫描器自检：能识别字符串/模板串里的花括号，不会把它们当成组件体边界', () => {
  const sample = 'function X() { const a = "${ not a brace }"; const b = `x ${ { y: 1 }.y }`; return a; }';
  const open = sample.indexOf('{');
  const close = matchBrace(sample, open);
  assert.equal(sample.slice(open, close + 1).endsWith('}'), true);
  assert.ok(close > sample.indexOf('return a;'), '闭合括号必须在 return 之后');
});

// ---------------------------------------------------------------------------
// T-44 ①：条件 Hook（FR-11.3）
// ---------------------------------------------------------------------------

test('AdminPanel 的 Hook 全部早于任何早退（FR-11.3 条件 Hook 已修）', () => {
  const body = functionBody(ADMIN_PANEL, 'AdminPanel');
  const { hooks, firstEarlyReturn } = auditHookOrder(body);

  // 页签下沉到 tabs/ 之后，面板壳只剩一个 useState；用 ≥1 而非 ≥7，
  // 这样"再拆一层"不会让本断言失效，但"return 跑到 Hook 前面"依然会被抓住。
  assert.ok(hooks.length >= 1, `只找到 ${hooks.length} 个 Hook，扫描器或组件结构异常`);
  assert.notEqual(
    firstEarlyReturn,
    null,
    '未找到任何早退 —— AdminPanel 应当保留 `if (!token) return 请先登录` 这一分支'
  );

  const lateHooks = hooks.filter((h) => h.index > firstEarlyReturn);
  assert.deepEqual(
    lateHooks.map((h) => h.text),
    [],
    '这些 Hook 出现在早退之后：React 会在"令牌从有到无"时抛 ' +
      '"Rendered fewer hooks than expected"。请把 return 移到所有 Hook 之后。'
  );
});

test('判别力：把修复前的写法（return 在 useState 之前）喂进去必须判为失败', () => {
  const broken = `
    function AdminPanelContent({ token }: { token: string | null }) {
      if (!token) return <div>请先登录</div>;
      const [activeTab, setActiveTab] = useState('stats');
      const [users, setUsers] = useState<any[]>([]);
      const [loading, setLoading] = useState(false);
      const [successMsg, setSuccessMsg] = useState('');
      const [detailInterview, setDetailInterview] = useState<any>(null);
      return <div>{users.length}{activeTab}{loading}{successMsg}{detailInterview}</div>;
    }
  `;
  const { hooks, firstEarlyReturn } = auditHookOrder(functionBody(broken, 'AdminPanelContent'));
  const lateHooks = hooks.filter((h) => h.index > firstEarlyReturn);
  assert.ok(
    lateHooks.length === hooks.length,
    `判别力失效：修复前的写法本应被全部判为"早退之后的 Hook"，实际只判出 ${lateHooks.length}/${hooks.length}`
  );
});

test('守卫令牌为空时的出口是"渲染另一棵树"，不是提前跳过 Hook', () => {
  // 早退语句本身必须原样保留（这是 T-42 的"请先登录"分支）
  assert.match(
    ADMIN_PANEL,
    /if \(!token\) \{[\s\S]{0,80}?return <div[^>]*>请先登录<\/div>;/,
    'AdminPanel 应当保留令牌为空时的"请先登录"出口'
  );
  // 且必须位于 Hook 调用之后
  const body = functionBody(ADMIN_PANEL, 'AdminPanel');
  const { hooks, firstEarlyReturn } = auditHookOrder(body);
  assert.ok(hooks.length >= 1, `只找到 ${hooks.length} 个 Hook`);
  assert.ok(firstEarlyReturn !== null, '未找到早退出口');
  assert.ok(
    firstEarlyReturn > hooks[hooks.length - 1].index,
    '最后一个 Hook 必须早于早退出口'
  );
});

// ---------------------------------------------------------------------------
// T-45：AdminPanel 拆分
// ---------------------------------------------------------------------------

test('T-45：AdminPanel 已从 App.tsx 拆出，App 不再内联管理后台', () => {
  assert.ok(existsSync(path.join(SRC, 'admin', 'AdminPanel.tsx')));
  assert.ok(!/\bAdminPanelContent\b/.test(APP), 'App.tsx 里仍残留 AdminPanelContent');
  assert.ok(!/\bfunction AdminPanel\b/.test(APP), 'App.tsx 里仍内联着管理后台组件');
  assert.ok(!/showAdminPanel/.test(APP), 'App.tsx 里仍有旧的管理员弹窗布尔量 showAdminPanel');
});

test('T-45：管理后台的管理员 API 全部改走 authFetch（不再裸 fetch）', () => {
  const adminFiles = {
    'AdminPanel.tsx': ADMIN_PANEL,
    'tabs/StatsDashboard.tsx': TABS_STATS,
    'tabs/UsersTable.tsx': TABS_USERS,
    'tabs/InterviewsTable.tsx': TABS_INTERVIEWS,
  };
  for (const [name, src] of Object.entries(adminFiles)) {
    const raw = [...src.matchAll(/await\s+fetch\(/g)];
    assert.deepEqual(raw.map((m) => m[0]), [], `${name} 里仍有裸 fetch，应改用 authFetch`);
  }
  const guarded = [...Object.values(adminFiles).join('\n').matchAll(/await\s+authFetch\(/g)];
  assert.equal(guarded.length, 8, `预期 8 处 authFetch（管理后台原有 8 个请求），实际 ${guarded.length} 处`);
});

test('T-45：三个页签各自持有数据（不再共用一个 loading/successMsg）', () => {
  for (const [name, src] of Object.entries({
    'tabs/StatsDashboard.tsx': TABS_STATS,
    'tabs/UsersTable.tsx': TABS_USERS,
    'tabs/InterviewsTable.tsx': TABS_INTERVIEWS,
  })) {
    assert.match(src, /useState/, `${name} 未持有自己的状态`);
    assert.match(src, /authFetch\(/, `${name} 未自己拉取数据`);
  }
  // 面板壳不应再残留任何 loading / successMsg 状态（注释里提到不算，只看代码行）
  const codeLines = ADMIN_PANEL.split('\n').filter((l) => !/^\s*(\*|\/\/|\/\*)/.test(l));
  const code = codeLines.join('\n');
  assert.ok(!/successMsg/.test(code), 'AdminPanel 仍在持有 successMsg');
  assert.ok(!/useState\(false\)/.test(code), 'AdminPanel 仍在持有本应下沉到页签的加载态');
});

test('T-45：拆分后的单文件行数都在预算内（App.tsx 已从 2716 行降下来）', () => {
  const lines = (s) => s.split('\n').length;
  const budget = {
    'admin/AdminPanel.tsx': ADMIN_PANEL,
    'admin/ReportDetailModal.tsx': REPORT_MODAL,
    'admin/AdminPage.tsx': ADMIN_PAGE,
    'admin/tabs/StatsDashboard.tsx': TABS_STATS,
    'admin/tabs/UsersTable.tsx': TABS_USERS,
    'admin/tabs/InterviewsTable.tsx': TABS_INTERVIEWS,
    'auth/AuthContext.tsx': AUTH_CONTEXT,
    'auth/RequireAuth.tsx': REQUIRE_AUTH,
  };
  for (const [name, src] of Object.entries(budget)) {
    assert.ok(lines(src) <= 400, `${name} 有 ${lines(src)} 行，超出 ≤400 行预算`);
  }
  assert.ok(lines(APP) < 2270, `App.tsx 仍有 ${lines(APP)} 行（T-45 前是 2716 行）`);
  assert.ok(lines(MAIN) <= 80, `main.tsx 作为路由装配点不应臃肿（现 ${lines(MAIN)} 行）`);
});

// ---------------------------------------------------------------------------
// T-44 ②：路由化 + 守卫移到路由层 + 认证状态唯一真源
// ---------------------------------------------------------------------------

test('react-router-dom 真的被用起来了（不再是装了不 import 的摆设）', () => {
  assert.match(MAIN, /from 'react-router-dom'/, 'main.tsx 未引入 react-router-dom');
  assert.match(MAIN, /<BrowserRouter>/, 'main.tsx 未挂 BrowserRouter');
  assert.match(MAIN, /<Routes>/, 'main.tsx 未使用 <Routes>');
  assert.match(MAIN, /<AuthProvider>/, 'main.tsx 未挂 AuthProvider');
});

test('/admin 路由存在且由 RequireAdmin 守卫包裹（守卫在路由层，不在渲染期的 &&）', () => {
  assert.match(MAIN, /path="\/admin"/, 'main.tsx 缺少 /admin 路由');
  assert.match(ADMIN_PAGE, /<RequireAdmin>/, 'AdminPage 未使用 RequireAdmin 守卫');
  assert.match(REQUIRE_AUTH, /export function RequireAdmin/, 'RequireAuth.tsx 未导出 RequireAdmin');
  assert.match(REQUIRE_AUTH, /export function RequireAuth/, 'RequireAuth.tsx 未导出 RequireAuth');
  assert.match(REQUIRE_AUTH, /<Navigate to="\/" replace/, '守卫未在未授权时重定向回首页');
});

test('认证状态收敛进 AuthContext：App.tsx 不再自己 localStorage.getItem 认证三件套', () => {
  assert.match(APP, /useAuth\(\)/, 'App.tsx 未使用 useAuth()');
  assert.ok(
    !/useState<string \| null>\(localStorage\.getItem\('token'\)\)/.test(APP),
    'App.tsx 仍在用 useState 初始化 token（应改由 AuthContext 提供）'
  );
  assert.ok(
    !/localStorage\.(setItem|removeItem)\('token'/.test(APP),
    'App.tsx 仍在直接读写 localStorage 的 token（持久化应由 AuthContext 承担）'
  );
  assert.match(AUTH_CONTEXT, /export function AuthProvider/, 'AuthContext 未导出 AuthProvider');
  assert.match(AUTH_CONTEXT, /export function useAuth/, 'AuthContext 未导出 useAuth');
  for (const key of ['token', 'role', 'username']) {
    assert.match(
      AUTH_CONTEXT,
      new RegExp(`writeStored\\('${key}'`),
      `AuthContext 未把 ${key} 纳入持久化写路径`
    );
  }
});

test('T-44 不改动业务函数体：App 仍保留 logout 的面试态清理（锁定态必须一起清）', () => {
  assert.match(APP, /const logout = \(\) => \{/, 'App.tsx 的 logout 不见了');
  assert.match(APP, /signOut\(\);/, 'logout 未调用 AuthContext 的 signOut');
  assert.match(APP, /setInterviewLocked\(false\)/, 'logout 丢了超时锁定态清理（T-42 不变量）');
  assert.match(APP, /setToast\(null\)/, 'logout 丢了 Toast 清理（T-42 不变量）');
});

test('管理员入口改为路由跳转（URL 即状态，可刷新、可前进后退）', () => {
  assert.match(APP, /navigate\('\/admin'\)/, 'App.tsx 没有跳转 /admin');
  assert.match(APP, /<Navigate to="\/admin" replace \/>/, 'App.tsx 未把管理员账号重定向到 /admin');
});

test('不再触碰死参数（T-11 契约的上游前提仍然成立）', () => {
  for (const [name, src] of Object.entries(FILES)) {
    assert.ok(!/\baction\s*:/.test(src), `${name} 出现 action 死参数`);
    assert.ok(!/\buser_id\s*:/.test(src), `${name} 出现 user_id 死参数`);
  }
});
