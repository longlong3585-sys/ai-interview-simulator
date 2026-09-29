/**
 * T-40 契约测试（零依赖，node:test 运行）：token + userId **集中并持久化**。
 *
 * 运行：cd frontend && npm run test:node
 *
 * 验收标准（`docs/03-tasks.md` §阶段 4）：
 *   * **刷新后 `userId` 仍存在**；
 *   * **登出清理干净**。
 *
 * 这两条都不能靠"看代码像不像"来证明 —— 所以本文件用**假 storage 真跑**整个闭环：
 *
 *     登录写入 → 模拟刷新（重新还原）→ 断言 userId 仍在 → 登出 → 断言四个键全清空
 *
 * 三层，缺一层都会留下"看起来改好了"的假象：
 *   1. **纯逻辑**（`src/auth/authStorage.ts`）：写入/还原/清空的真实行为 + 脏数据防御；
 *   2. **接线**（`src/auth/AuthContext.tsx`）：唯一写路径、只在 hydrated 之后写、
 *      `signIn`/`signOut`/`applyRefreshedToken` 三个入口都在；
 *   3. **T-36 口子兑现**（`AuthBridge.tsx`）：续期令牌真的回灌了内存态。
 *
 * 每条判定都带判别力自检（把"没做这件事"的写法喂进同一个判定器，必须判为违规）。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  AUTH_PERSISTED_KEYS,
  AUTH_SNAPSHOT_EMPTY,
  AUTH_TOKEN_STORAGE_KEY,
  AUTH_USER_ID_STORAGE_KEY,
  clearAuthStorage,
  parseStoredRole,
  parseStoredUserId,
  persistAuthSnapshot,
  readAuthValue,
  restoreAuthState,
  writeAuthValue,
} from '../src/auth/authStorage.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..');
const SRC = path.join(FRONTEND, 'src');

const read = (...parts) => readFileSync(path.join(SRC, ...parts), 'utf8');

const AUTH_CONTEXT = read('auth', 'AuthContext.tsx');
const AUTH_STORAGE = read('auth', 'authStorage.ts');
const AUTH_BRIDGE = read('auth', 'AuthBridge.tsx');
const MAIN = read('main.tsx');
const AUTH_MODAL = read('auth', 'AuthModal.tsx');
const API_SERVICE = read('services', 'api.ts');

/** 假的 localStorage：记录每一次读写，便于断言"到底动了哪几个键"。 */
function fakeStorage(initial = {}, { throwOnWrite = false } = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    writes: [],
    removes: [],
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem(key, value) {
      if (throwOnWrite) throw new Error('QuotaExceededError');
      this.writes.push([key, value]);
      map.set(key, value);
    },
    removeItem(key) {
      this.removes.push(key);
      map.delete(key);
    },
  };
}

/** 模拟一次"登录成功后落盘"的完整快照。 */
const LOGGED_IN = {
  token: 'jwt-abc',
  userId: 42,
  role: 'user',
  username: 'alice',
};

// ——————————————————————————————————————————————————————————————
// 1. 纯逻辑：刷新后 userId 仍存在 / 登出清理干净
// ——————————————————————————————————————————————————————————————

test('刷新后 userId 仍存在：写入 → 重新还原，整份会话身份原样回来', () => {
  const storage = fakeStorage();
  assert.equal(
    persistAuthSnapshot(storage, { ...LOGGED_IN, username: LOGGED_IN.username }, AUTH_PERSISTED_KEYS),
    true,
    '持久化写路径返回了失败'
  );

  // "刷新页面" = 换一份新内存，只带着同一份 storage 重新走 restoreAuthState()。
  const restored = restoreAuthState(storage, AUTH_PERSISTED_KEYS);
  assert.equal(restored.userId, 42, '刷新后 userId 丢了 —— 这正是 Bug 2 的验收标准');
  assert.equal(restored.token, 'jwt-abc');
  assert.equal(restored.role, 'user');
  assert.equal(restored.username, 'alice');
  // 存储里的 userId 是**数字序列化**的结果，不是 `[object Object]` 之类。
  assert.equal(storage.getItem('userId'), '42');
});

test('登出清理干净：四个键全部消失，且不误伤同源的其他数据', () => {
  const storage = fakeStorage({
    token: 'jwt-abc', userId: '42', role: 'user', username: 'alice',
    'ui-theme': 'dark', // 不属于认证状态，登出不该动它
  });
  assert.equal(clearAuthStorage(storage, AUTH_PERSISTED_KEYS), true);
  for (const key of ['token', 'userId', 'role', 'username']) {
    assert.equal(storage.getItem(key), null, `登出后 ${key} 仍在存储里`);
  }
  assert.equal(storage.getItem('ui-theme'), 'dark', '登出误删了非认证数据（用了 clear()？）');
  assert.deepEqual(
    restoreAuthState(storage, AUTH_PERSISTED_KEYS),
    AUTH_SNAPSHOT_EMPTY,
    '清空后再还原，应当得到一份空快照'
  );
});

test('持久化清单就是那 4 个键，一个不少一个不多', () => {
  assert.deepEqual(
    [...AUTH_PERSISTED_KEYS].sort(),
    ['role', 'token', 'userId', 'username'],
    '持久化清单与预期不一致（漏一个 = 登出漏清，多一个 = 越权删数据）'
  );
  // 令牌键必须与 T-36 的续期链路用同一个字面量，否则"续期写到别处"。
  assert.equal(AUTH_TOKEN_STORAGE_KEY, 'token');
  assert.equal(AUTH_USER_ID_STORAGE_KEY, 'userId');
});

test('判别力自检：不做 userId 持久化的实现必须被判为"刷新即丢"', () => {
  // 修复前：只写 token/role/username（T-44 的口径），userId 不落盘。
  const storage = fakeStorage();
  persistAuthSnapshot(
    storage,
    { token: 'jwt-abc', userId: null, role: 'user', username: 'alice' },
    AUTH_PERSISTED_KEYS
  );
  assert.equal(
    restoreAuthState(storage, AUTH_PERSISTED_KEYS).userId,
    null,
    '判别力自检失败：null 的 userId 竟然被还原成了数字'
  );
  // 反向：真正写了就必须读得回来（证明上面那条不是因为"永远读不到"）。
  persistAuthSnapshot(storage, { ...LOGGED_IN }, AUTH_PERSISTED_KEYS);
  assert.equal(restoreAuthState(storage, AUTH_PERSISTED_KEYS).userId, 42);
});

// ——————————————————————————————————————————————————————————————
// 2. 脏数据防御（存储是用户可改的）
// ——————————————————————————————————————————————————————————————

test('userId 解析器拒绝一切非正整数（宁可退化成匿名，也不拿脏值去拼请求）', () => {
  assert.equal(parseStoredUserId('42'), 42);
  assert.equal(parseStoredUserId(' 42 '), 42);
  for (const bad of ['', '   ', 'abc', '12abc', '0', '-1', '3.5', 'NaN', 'Infinity',
                     '1e3', '0x10', null, undefined]) {
    assert.equal(parseStoredUserId(bad), null, `${JSON.stringify(bad)} 不该被解析成 userId`);
  }
});

test('role 解析器只放行白名单：被改坏的值退化成"无角色"', () => {
  assert.equal(parseStoredRole('admin'), 'admin');
  assert.equal(parseStoredRole('user'), 'user');
  assert.equal(parseStoredRole('root'), null, 'role 白名单被绕过 —— 前端守卫会做出无法解释的判断');
  assert.equal(parseStoredRole(''), null);
  assert.equal(parseStoredRole(null), null);
});

test('存储不可用时只降级、绝不抛（隐私模式不能让应用白屏）', () => {
  const throwing = {
    getItem() { throw new Error('SecurityError'); },
    setItem() { throw new Error('SecurityError'); },
    removeItem() { throw new Error('SecurityError'); },
  };
  assert.equal(readAuthValue(throwing, 'token'), null);
  assert.equal(writeAuthValue(throwing, 'token', 'x'), false);
  assert.equal(clearAuthStorage(throwing, AUTH_PERSISTED_KEYS), false);
  assert.equal(persistAuthSnapshot(throwing, { ...LOGGED_IN }, AUTH_PERSISTED_KEYS), false);
  assert.deepEqual(
    restoreAuthState(throwing, AUTH_PERSISTED_KEYS),
    AUTH_SNAPSHOT_EMPTY,
    '存储抛错时应还原成空快照，而不是崩掉'
  );
});

test('写 null 等于删除该键（空快照落盘后存储里不留空字符串）', () => {
  const storage = fakeStorage({ token: 'jwt-abc', userId: '42' });
  persistAuthSnapshot(storage, { token: null, userId: null, role: null, username: null }, AUTH_PERSISTED_KEYS);
  assert.equal(storage.getItem('token'), null);
  assert.equal(storage.getItem('userId'), null);
  // 判别力：必须是 removeItem，而不是 setItem('null') 那种"看起来清了"的写法。
  assert.deepEqual(storage.removes.sort(), ['role', 'token', 'userId', 'username']);
});

// ——————————————————————————————————————————————————————————————
// 3. 接线：AuthContext 的写路径与三个入口
// ——————————————————————————————————————————————————————————————

test('持久化只有一条写路径，且**只在 hydrated 之后**写（不会抹掉刚还原的会话）', () => {
  assert.match(
    AUTH_CONTEXT,
    /persistAuthSnapshot\(\s*storage/,
    'AuthContext 没有调用唯一持久化写路径'
  );
  // 关键：effect 的第一行必须挡住"还没还原就先落盘"。
  const effectIdx = AUTH_CONTEXT.indexOf('persistAuthSnapshot(');
  const guardIdx = AUTH_CONTEXT.lastIndexOf('if (!state.hydrated) return;', effectIdx);
  assert.ok(guardIdx > 0, '持久化 effect 没有 hydrated 守卫 —— 首帧空快照会把存储里的会话抹掉');
  // 反向：AuthContext 自己不许再直接碰 localStorage 的键（除容错包装）。
  assert.ok(
    !/localStorage\.setItem\(/.test(AUTH_CONTEXT),
    'AuthContext 仍在直接 setItem —— 持久化口径会分裂成两处'
  );
  assert.ok(
    !/localStorage\.removeItem\(/.test(AUTH_CONTEXT),
    'AuthContext 仍在直接 removeItem'
  );
  // 判别力自检：去掉守卫的写法必须被判为缺守卫。
  const naive = "useEffect(() => { persistAuthSnapshot(storage, snapshot); }, [snapshot]);";
  assert.equal(
    /if \(!state\.hydrated\) return;/.test(naive),
    false,
    '判别力自检失败：守卫判定恒真'
  );
});

test('首帧就用 restoreAuthState 还原（不依赖 effect 时序），且接上了 storage 事件', () => {
  assert.match(
    AUTH_CONTEXT,
    /restoreAuthState\(storage, STORAGE_KEYS\)/,
    'AuthProvider 没有在初始化时还原会话身份'
  );
  // useReducer 的惰性初值（第三参数）必须在；否则首帧是空的，会"闪一下未登录"。
  assert.match(AUTH_CONTEXT, /useReducer\(reducer, undefined, \(\) => \{/, '初始化没有走惰性还原');
  assert.match(AUTH_BRIDGE, /addEventListener\('storage'/, '没有监听跨标签页的 storage 事件');
  assert.match(AUTH_BRIDGE, /restoreFromStorage\(\)/, 'storage 事件没有接到 restoreFromStorage');
});

test('三个入口都在：signIn 落 userId、signOut 清盘、applyRefreshedToken 回灌内存', () => {
  assert.match(AUTH_CONTEXT, /signIn: \(payload: SignInPayload\) => void/, 'signIn 签名丢了');
  assert.match(AUTH_CONTEXT, /case 'SIGN_IN':/, 'reducer 缺 SIGN_IN 分支');
  assert.match(AUTH_CONTEXT, /userId: event\.value\.userId \?\? null/, 'SIGN_IN 没有落 userId');
  assert.match(AUTH_CONTEXT, /signOut: \(\) => void/, 'signOut 签名丢了');
  assert.match(AUTH_CONTEXT, /clearAuthStorage\(storage, STORAGE_KEYS\)/, 'signOut 没有立刻清盘');
  assert.match(AUTH_CONTEXT, /case 'SIGN_OUT':/, 'reducer 缺 SIGN_OUT 分支');
  assert.match(
    AUTH_CONTEXT,
    /case 'REFRESH_TOKEN':/,
    'reducer 缺 REFRESH_TOKEN 分支 —— 续期令牌进不了内存态'
  );
});

test('登录响应里的 user_id 真的被交给 AuthContext（否则落盘的永远是 null）', () => {
  assert.match(AUTH_MODAL, /userId: data\.user_id/, 'AuthModal 没有把 data.user_id 交给 signIn');
  assert.match(AUTH_MODAL, /signIn\(\{/, 'AuthModal 没有调用 signIn');
});

test('T-36 的口子已兑现：AuthBridge 订阅 onTokenRefreshed 并调用 applyRefreshedToken', () => {
  assert.match(
    AUTH_BRIDGE,
    /onTokenRefreshed\(applyRefreshedToken\)/,
    'AuthBridge 没有把续期令牌接进 AuthContext（T-36 留的口子没兑现）'
  );
  assert.match(
    AUTH_BRIDGE,
    /registerUnauthorizedHandler\(signOut\)/,
    'AuthBridge 丢了 401 统一登出的接线（T-36 不变量回退）'
  );
  assert.match(API_SERVICE, /export function onTokenRefreshed/, '统一层不再导出 onTokenRefreshed');
  assert.match(MAIN, /<AuthBridge\s*\/>/, 'main.tsx 没有挂载 AuthBridge');
  // 判别力自检：没有订阅的写法必须被判为违规。
  const naive = 'useEffect(() => { return () => {}; }, []);';
  assert.equal(/onTokenRefreshed\(applyRefreshedToken\)/.test(naive), false, '判别力自检失败');
});

test('纯逻辑模块可在 Node 里直接加载（契约测试才能真跑闭包）', () => {
  // 与 T-36 的「authResponse 不得依赖浏览器环境」同一口径：
  // 只允许 import 兄弟纯模块（authResponse 的令牌键常量），不许 React / import.meta.env。
  const imports = [...AUTH_STORAGE.matchAll(/^\s*import\s+[^;]+;/gm)].map((m) => m[0].trim());
  const browserOnly = imports.filter((line) => !/from\s+'[^']*authResponse(\.ts)?'/.test(line));
  assert.deepEqual(browserOnly, [], `authStorage.ts 依赖了浏览器环境：${JSON.stringify(browserOnly)}`);
  assert.match(AUTH_STORAGE, /export const AUTH_TOKEN_STORAGE_KEY = 'token'/, '存储键的唯一来源不在本文件');
});
