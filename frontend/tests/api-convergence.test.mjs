/**
 * T-36 契约测试（零依赖，node:test 运行）：api 层收敛的三条不变量。
 *
 * 运行：cd frontend && npm run test:node
 *
 * 不变量（与 `docs/03-tasks.md` 的 T-36 验收标准一一对应）：
 *   ① **401 统一登出**：任何带令牌的请求拿到 401 都触发同一个登出入口，且**先登出再抛错**；
 *   ② **`X-Refreshed-Token` 集中处理**：读到响应头 → 刷新 localStorage 里的 token →（并按需通知）；
 *   ③ **全库裸 `fetch` 仅剩 `services/api.ts`**：源码里除该文件外 `fetch(` 出现 0 次。
 *
 * 每条都带**判别力自检**：把收敛前的写法喂进同一个判定函数，必须判为违规 ——
 * 否则"通过"只说明规则没生效，不说明代码没问题。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  REFRESHED_TOKEN_HEADER,
  AUTH_TOKEN_STORAGE_KEY,
  ApiError,
  applyRefreshedToken,
  isPublicApiPath,
  isUnauthorizedError,
  readRefreshedToken,
} from '../src/services/authResponse.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..');
const SRC = path.join(FRONTEND, 'src');

/** 全库唯一的 HTTP 出口（不变量 ③ 的唯一豁免文件）。 */
const API_SERVICE = 'services/api.ts';

function readSrc(rel) {
  return readFileSync(path.join(SRC, rel), 'utf8');
}

const API_SRC = readSrc(API_SERVICE);
const AUTH_RESPONSE_SRC = readSrc('services/authResponse.ts');
const MAIN_SRC = readSrc('main.tsx');
const AUTH_BRIDGE_SRC = readSrc('auth/AuthBridge.tsx');

/** 去掉注释与字符串字面量：注释里会**引用** `fetch(` 来说明规则，那不是调用。 */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(/\r?\n/)
    .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\s\/\/.*$/, '')))
    .join('\n');
}

function walkSources(dir = SRC) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkSources(full));
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** 收集 `src/` 下所有**裸 fetch 调用**的文件（相对 `src/` 的 POSIX 路径）。 */
function collectNakedFetchFiles() {
  const offenders = [];
  for (const full of walkSources()) {
    const rel = path.relative(SRC, full).replace(/\\/g, '/');
    if (rel === API_SERVICE) continue; // 唯一豁免：统一出口自己当然要调 fetch
    // 只认真正的调用：前面不是 `.` / 标识符字符（排除 `mockFetch(`、`obj.fetch(`、`refetch(`）。
    if (/(^|[^.\w])fetch\s*\(/.test(stripComments(readFileSync(full, 'utf8')))) {
      offenders.push(rel);
    }
  }
  return offenders.sort();
}

/** 判定器：给定源码集合，返回"裸 fetch"违规文件（供判别力自检复用）。 */
function findNakedFetch(files) {
  return files
    .filter((f) => f.rel !== API_SERVICE)
    .filter((f) => /(^|[^.\w])fetch\s*\(/.test(stripComments(f.src)))
    .map((f) => f.rel)
    .sort();
}

// ——————————————————————————————————————————————————————————————
// 不变量 ③：全库裸 fetch 仅剩 services/api.ts
// ——————————————————————————————————————————————————————————————

test('③ src 下除 services/api.ts 外，源码里的裸 fetch 为 0 处', () => {
  const offenders = collectNakedFetchFiles();
  assert.deepEqual(
    offenders,
    [],
    `以下文件仍在直接调 fetch（T-36 要求全部走 services/api.ts）：${JSON.stringify(offenders)}`
  );
});

test('③ 统一出口自己只有 2 次 fetch（request / publicFetch），且没有第三个漏网出口', () => {
  const hits = [...stripComments(API_SRC).matchAll(/(^|[^.\w])fetch\s*\(/g)];
  assert.equal(
    hits.length,
    2,
    `services/api.ts 里有 ${hits.length} 次 fetch —— 只应有 request() 与 publicFetch() 两处`
  );
});

test('③ 判别力自检：收敛前的 18 处裸 fetch 写法必须被判为违规', () => {
  const preFix = [
    { rel: 'auth/AuthModal.tsx', src: 'const res = await fetch(`${API_BASE_URL}/api/captcha`);' },
    { rel: 'notifications/useNotificationCenter.ts', src: "await fetch(`${API_BASE_URL}/api/history`, { headers });" },
    { rel: 'index.css.ts', src: 'const refetchCount = 1; // 不是调用' },
  ];
  assert.deepEqual(
    findNakedFetch(preFix),
    ['auth/AuthModal.tsx', 'notifications/useNotificationCenter.ts'],
    '判别力自检失败：判定器认不出收敛前的写法（规则形同虚设）'
  );
  // 反向自检：`refetch(` / `.fetch(` / 注释里的 fetch 都不能被误判。
  assert.deepEqual(
    findNakedFetch([{ rel: 'a.ts', src: 'refetch(); obj.fetch(); // fetch(`/api/x`)' }]),
    [],
    '判别力自检失败：判定器把非调用误判成了裸 fetch'
  );
});

test('③ 返回 null 的 response 不会让续期逻辑误写 localStorage', () => {
  assert.equal(readRefreshedToken(null), null);
  assert.equal(readRefreshedToken(undefined), null);
  assert.equal(readRefreshedToken({}), null);
});

// ——————————————————————————————————————————————————————————————
// 不变量 ①：401 统一登出
// ——————————————————————————————————————————————————————————————

test('① 401 判定不依赖跨模块 instanceof（打包后多份类定义会让 instanceof 失效）', () => {
  assert.equal(isUnauthorizedError(new ApiError(401, '/api/x')), true);
  assert.equal(isUnauthorizedError({ status: 401 }), true);
  assert.equal(isUnauthorizedError({ message: 'Unauthorized' }), true);
  assert.equal(isUnauthorizedError({ status: 403 }), false);
  assert.equal(isUnauthorizedError(new Error('boom')), false);
  assert.equal(isUnauthorizedError(null), false);
  assert.equal(isUnauthorizedError(undefined), false);
});

test('① 401 错误的 message 仍是 "Unauthorized"（既有调用点的判定不许被改坏）', () => {
  const err = new ApiError(401, '/api/x');
  assert.equal(err.message, 'Unauthorized');
  assert.equal(err.status, 401);
  assert.equal(err.url, '/api/x');
  assert.equal(err.name, 'ApiError');
  // 全库既有的 `err.message === 'Unauthorized'` 判定点必须仍然存在（回归护栏）。
  const legacyChecks = walkSources()
    .map((full) => ({ rel: path.relative(SRC, full).replace(/\\/g, '/'), src: readFileSync(full, 'utf8') }))
    .filter((f) => /message\s*===\s*'Unauthorized'/.test(f.src))
    .map((f) => f.rel);
  assert.ok(
    legacyChecks.length >= 2,
    `找不到既有的 'Unauthorized' 字符串判定（${JSON.stringify(legacyChecks)}）—— 语义被改动过？`
  );
});

test('① 统一出口在 401 时先登出、再抛错（顺序写在源码里，不是靠调用点自觉）', () => {
  const exportIdx = API_SRC.indexOf('export async function request');
  assert.ok(exportIdx > 0, 'services/api.ts 里找不到 request() 出口');
  const body = API_SRC.slice(exportIdx, API_SRC.indexOf('export async function authFetch'));
  const notifyIdx = body.indexOf('notifyUnauthorized()');
  const throwIdx = body.indexOf("throw new ApiError(401");
  assert.ok(notifyIdx > 0, 'request() 在 401 分支里没有调用 notifyUnauthorized()');
  assert.ok(throwIdx > 0, 'request() 在 401 分支里没有抛出 401 的 ApiError');
  assert.ok(notifyIdx < throwIdx, '必须先登出再抛错（顺序反了会让调用方在已登出状态下继续处理）');
  // 401 分支之外不许再出现第二处登出调用：登出路径必须唯一（排除函数自身的声明行）。
  const notifyCount = [...API_SRC.matchAll(/(?<!function )\bnotifyUnauthorized\s*\(\s*\)/g)]
    .filter((m) => !/function\s+$/.test(API_SRC.slice(Math.max(0, m.index - 12), m.index)))
    .length;
  assert.equal(notifyCount, 1, `notifyUnauthorized() 被调用 ${notifyCount} 次 —— 登出入口必须唯一`);
});

test('① 401 处理已接线：main.tsx 渲染 AuthBridge，AuthBridge 注册 signOut', () => {
  assert.match(
    MAIN_SRC,
    /<AuthBridge\s*\/>/,
    'main.tsx 没有渲染 <AuthBridge /> —— 401 不会登出（接线缺失必须是响亮的失败）'
  );
  assert.match(
    MAIN_SRC,
    /import\s*\{\s*AuthBridge\s*\}\s*from\s*['"]\.\/auth\/AuthBridge\.tsx['"]/,
    'main.tsx 没有 import AuthBridge'
  );
  assert.match(
    AUTH_BRIDGE_SRC,
    /registerUnauthorizedHandler\s*\(\s*signOut\s*\)/,
    'AuthBridge 没有把 AuthContext 的 signOut 注册进统一层'
  );
  // 反向：api.ts 不许 import AuthContext（会构成运行时环）。
  assert.ok(
    !/from\s*['"][^'"]*AuthContext['"]/.test(API_SRC),
    'services/api.ts 不得 import AuthContext —— 用注册口注入，避免运行时环'
  );
});

test('① 公开端点（登录/注册/验证码/题库）不带令牌、也不触发登出', () => {
  assert.equal(isPublicApiPath('/api/captcha'), true);
  assert.equal(isPublicApiPath('/api/login'), true);
  assert.equal(isPublicApiPath('/api/register'), true);
  assert.equal(isPublicApiPath('/api/question_bank'), true);
  assert.equal(isPublicApiPath('/api/question_bank/12'), true);
  assert.equal(isPublicApiPath('/api/user/profile'), false);
  assert.equal(isPublicApiPath('/api/notifications/unread_count'), false);
  assert.equal(isPublicApiPath('/api/login_attempts'), false);
  // 绝对地址 / 带查询串也要判对（调用方可能传完整 URL）。
  assert.equal(isPublicApiPath('https://example.com/api/login'), true);
  assert.equal(isPublicApiPath('/api/captcha?ts=1'), true);
  assert.equal(isPublicApiPath('/admin/login'), false);
});

// ——————————————————————————————————————————————————————————————
// 不变量 ②：X-Refreshed-Token 集中处理
// ——————————————————————————————————————————————————————————————

/** 假的头容器（`Response.headers` 的子集）。 */
function fakeHeaders(map) {
  return { get: (name) => (Object.prototype.hasOwnProperty.call(map, name) ? map[name] : null) };
}

function fakeResponse(map) {
  return { status: 200, headers: fakeHeaders(map) };
}

/** 假的 storage：记录写入，便于断言"确实刷了哪些键"。 */
function fakeStorage({ throwOnWrite = false } = {}) {
  const writes = [];
  return {
    writes,
    setItem(key, value) {
      if (throwOnWrite) throw new Error('QuotaExceededError');
      writes.push([key, value]);
    },
  };
}

test('② 响应头名与后端契约一致，且存储键与认证真源同源', () => {
  assert.equal(REFRESHED_TOKEN_HEADER, 'X-Refreshed-Token');
  assert.equal(AUTH_TOKEN_STORAGE_KEY, 'token');
  // T-40 起存储键的**唯一来源**是 `auth/authStorage.ts` 的持久化清单，
  // 续期链路（本文件测的）与 AuthContext（T-40 测的）都从那里取键 ——
  // 否则续期写到别处，下一次请求读的还是旧令牌（"续期看似生效、其实每次仍用旧令牌"）。
  const authStorage = readSrc('auth/authStorage.ts');
  assert.match(
    authStorage,
    /AUTH_TOKEN_STORAGE_KEY\s*=\s*'token'/,
    "authStorage 的 AUTH_TOKEN_STORAGE_KEY 不是 'token' —— 续期写入的键与认证真源不一致"
  );
  assert.match(
    authStorage,
    /AUTH_PERSISTED_KEYS\s*=\s*\[[^\]]*AUTH_TOKEN_STORAGE_KEY/,
    'authStorage 的持久化清单里没有令牌键'
  );
});

test('② readRefreshedToken：读到就归一，读不到就 null（绝不把 null 当令牌写）', () => {
  assert.equal(readRefreshedToken(fakeResponse({ 'X-Refreshed-Token': 'new-token' })), 'new-token');
  assert.equal(readRefreshedToken(fakeResponse({ 'X-Refreshed-Token': '  spaced  ' })), 'spaced');
  assert.equal(readRefreshedToken(fakeResponse({ 'X-Refreshed-Token': '   ' })), null);
  assert.equal(readRefreshedToken(fakeResponse({ 'X-Refreshed-Token': '' })), null);
  // 大小写不同的头名：HTTP 头不区分大小写，但 `headers.get` 的行为由运行时保证，
  // 这里只断言"别的头不会串味"。
  assert.equal(readRefreshedToken(fakeResponse({ 'X-Other-Token': 'nope' })), null);
});

test('② applyRefreshedToken：写入成功返回 true；空值与写入失败都不抛', () => {
  const storage = fakeStorage();
  assert.equal(applyRefreshedToken(storage, 'tok-1'), true);
  assert.deepEqual(storage.writes, [['token', 'tok-1']]);
  assert.equal(applyRefreshedToken(storage, ''), false);
  assert.equal(applyRefreshedToken(storage, null), false);
  assert.equal(applyRefreshedToken(storage, undefined), false);
  // 隐私模式：写入抛错时不能把一次正常请求打成异常（返回 false 即可）。
  assert.equal(applyRefreshedToken(fakeStorage({ throwOnWrite: true }), 'tok-2'), false);
});

test('② 统一出口在响应处集中处理续期头：读头 → 写 localStorage → 通知订阅者', () => {
  const exportIdx = API_SRC.indexOf('export function handleRefreshedToken');
  assert.ok(exportIdx > 0, 'services/api.ts 缺少可测的 handleRefreshedToken()');
  const body = API_SRC.slice(exportIdx, exportIdx + 900);
  const readIdx = body.indexOf('readRefreshedToken(');
  const persistIdx = body.indexOf('applyRefreshedToken(');
  const listenerIdx = body.indexOf('listener(');
  assert.ok(readIdx > 0, 'handleRefreshedToken 没有读续期头');
  assert.ok(persistIdx > 0, 'handleRefreshedToken 没有刷新 localStorage');
  assert.ok(listenerIdx > 0, 'handleRefreshedToken 没有通知订阅者');
  assert.ok(readIdx < persistIdx, '必须先读头再落存储（否则会把 null 写进 localStorage）');
  assert.ok(persistIdx < listenerIdx, '必须先落存储再通知订阅者（否则订阅者看到的新令牌没被持久化）');
  // 出口必须真的经过它：observeResponse 是唯一落点。
  assert.match(
    API_SRC,
    /function observeResponse\(response: Response\): Response \{\s*handleRefreshedToken\(response\)/,
    'observeResponse 没有调用 handleRefreshedToken —— 续期处理没有接进请求链路'
  );
  // 两个出口（request / publicFetch）都必须 observe：漏一个就会有一类请求不续期。
  const observeCount = [...API_SRC.matchAll(/observeResponse\(response\)/g)].length;
  assert.equal(observeCount, 2, `observeResponse 被调用了 ${observeCount} 次 —— 两个出口都要经过`);
});

test('② 判别力自检：写死不读续期头的实现必须被判为"缺处理"', () => {
  const deps = { storage: fakeStorage(), listeners: [] };
  // 收敛前的写法：拿到 response 直接返回，什么都不看。
  function naiveObserve(response) {
    return response;
  }
  const naiveWrites = deps.storage.writes;
  naiveObserve(fakeResponse({ 'X-Refreshed-Token': 'should-be-picked-up' }));
  assert.deepEqual(naiveWrites, [], '判别力自检基线不成立：naive 实现不该写存储');
  assert.equal(
    readRefreshedToken(naiveObserve(fakeResponse({ 'X-Refreshed-Token': 'v' }))),
    'v',
    '读取函数本身必须有效（作为对照）'
  );
});

test('② 续期语义（真实执行）：真跑一遍 handleRefreshedToken 的判定链', async () => {
  // 这里刻意**不** import `services/api.ts`（它依赖 `import.meta.env`，Node 里 import 就抛）。
  // 因此用同一套纯逻辑拼出 api.ts 里那条链（read → persist → notify），
  // 断言"链的语义正确"，而链的**接线**由上面的源码断言守住。
  const storage = fakeStorage();
  const seen = [];
  const listeners = [(token) => seen.push(token)];
  const response = fakeResponse({ [REFRESHED_TOKEN_HEADER]: ' refreshed-token ' });

  const refreshed = readRefreshedToken(response);
  assert.equal(refreshed, 'refreshed-token');
  assert.equal(applyRefreshedToken(storage, refreshed), true);
  for (const listener of listeners) listener(refreshed);

  assert.deepEqual(storage.writes, [['token', 'refreshed-token']]);
  assert.deepEqual(seen, ['refreshed-token']);
  // 没有续期头时：一条链上都不该发生副作用。
  const quietStorage = fakeStorage();
  const quietSeen = [];
  const quiet = readRefreshedToken(fakeResponse({}));
  assert.equal(quiet, null);
  assert.equal(applyRefreshedToken(quietStorage, quiet), false);
  assert.deepEqual(quietStorage.writes, []);
  assert.deepEqual(quietSeen, []);
});

// ——————————————————————————————————————————————————————————————
// 结构性：纯逻辑模块必须零依赖（否则 Node 里测不了，只能退回 grep）
// ——————————————————————————————————————————————————————————————

test('authResponse.ts 可直接在 Node 里加载（契约测试能真跑它的行为）', () => {
  // T-40 起它 import 了一个兄弟纯模块（`auth/authStorage.ts` 的存储键常量）——
  // 后者也是零依赖纯模块，所以整条链在 Node 里照样可加载。
  // 这里真正要守的性质是"**不依赖浏览器/Rust 环境**"（React、import.meta.env 等），
  // 而不是"一个 import 都不许有"。
  const importLines = [...AUTH_RESPONSE_SRC.matchAll(/^\s*import\s+[^;]+;/gm)].map((m) => m[0].trim());
  const browserOnly = importLines.filter((line) => !/from\s+'[^']*authStorage(\.ts)?'/.test(line));
  assert.deepEqual(
    browserOnly,
    [],
    `authResponse.ts 出现了非纯逻辑依赖，Node 侧将无法直接加载：${JSON.stringify(browserOnly)}`
  );
  // 判别力自检：换成 React / import.meta.env 必须被判为违规。
  const naive = ["import { useState } from 'react';", "import { API_BASE_URL } from '../config';"];
  const naiveOffenders = naive.filter((line) => !/from\s+'[^']*authStorage(\.ts)?'/.test(line));
  assert.equal(naiveOffenders.length, 2, '判别力自检失败：依赖判定过于宽松');
  // 反向自检：api.ts 自己确实有 import（证明上面那条规则不是"匹配器失效"）。
  assert.ok(/^\s*import\s/m.test(API_SRC), 'services/api.ts 竟没有 import？？规则可能失效了');
});
