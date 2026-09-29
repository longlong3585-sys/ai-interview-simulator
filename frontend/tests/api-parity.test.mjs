/**
 * T-03 契约测试（零依赖，node:test 运行）
 *
 * 目标：守住"前端调用的每个 API 路径，后端都真实存在"。
 *
 * `00-topic.md §10.4` 记录过一类隐患：前端调用与后端路由一旦漂移，
 * 表现为运行期 404，而类型系统完全不会报错（前端用的是字符串拼接）。
 *
 * 运行：cd frontend && npm run test:node
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const SRC_DIR = path.join(REPO_ROOT, 'frontend', 'src');
const ROUTERS_DIR = path.join(REPO_ROOT, 'backend', 'routers');

/** 把路径参数统一成 {} —— 让 `${id}` 与 `{id}` 可比。 */
function normalize(p) {
  return p
    .replace(/\$\{[^}]*\}/g, '{}') // 前端模板串
    .replace(/\{[^}]*\}/g, '{}')   // FastAPI 路径参数
    .replace(/\/+$/, '')           // 去尾斜杠
    .replace(/\/+/g, '/');
}

function walk(dir, filter) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full, filter));
    else if (filter(entry.name)) out.push(full);
  }
  return out;
}

/** 收集后端全部路由：APIRouter 前缀 + 各装饰器路径。 */
function collectBackendRoutes() {
  const routes = new Set();
  for (const file of walk(ROUTERS_DIR, (n) => n.endsWith('.py'))) {
    const src = readFileSync(file, 'utf8');
    const prefixMatch = src.match(/APIRouter\(\s*prefix\s*=\s*["']([^"']*)["']/);
    const prefix = prefixMatch ? prefixMatch[1] : '';
    const decoRe = /@router\.(get|post|patch|put|delete)\(\s*["']([^"']+)["']/g;
    let m;
    while ((m = decoRe.exec(src)) !== null) {
      routes.add(normalize(prefix + m[2]));
    }
  }
  // main.py 里的公开根路由
  const mainPy = path.join(REPO_ROOT, 'backend', 'main.py');
  const mainSrc = readFileSync(mainPy, 'utf8');
  for (const m of mainSrc.matchAll(/@app\.(get|post)\(\s*["']([^"']+)["']/g)) {
    routes.add(normalize(m[2]));
  }
  return routes;
}

/** 去掉注释：注释里会**引用**接口路径（"这里打 /api/xxx"）与旧地址，那不是调用。
 *  T-34 起本文件因此改为"看代码不看注释"——否则解释 Bug 4 的注释会把护栏变成假警报。 */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(/\r?\n/)
    .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\s\/\/.*$/, '')))
    .join('\n');
}

/** 收集前端调用的 API 路径。 */
function collectFrontendPaths() {
  const paths = new Set();
  for (const file of walk(SRC_DIR, (n) => /\.(ts|tsx)$/.test(n))) {
    const src = stripComments(readFileSync(file, 'utf8'));
    // 形如 `${API_BASE_URL}/api/xxx` 或 `${API_BASE_URL}${someVar}`
    for (const m of src.matchAll(/\$\{API_BASE_URL\}(\/api\/[^`'"\s)]*)/g)) {
      paths.add(normalize(m[1]));
    }
  }
  return paths;
}

const backendRoutes = collectBackendRoutes();
const frontendPaths = collectFrontendPaths();

test('后端确实解析出了路由（防止解析器失效导致"假通过"）', () => {
  assert.ok(
    backendRoutes.size >= 20,
    `后端仅解析出 ${backendRoutes.size} 条路由，解析器可能已失效`
  );
  assert.ok(
    frontendPaths.size >= 10,
    `前端仅解析出 ${frontendPaths.size} 条 API 路径，解析器可能已失效`
  );
});

test('前端调用的每个 API 路径，后端都必须存在对应路由', () => {
  const missing = [...frontendPaths].filter((p) => !backendRoutes.has(p)).sort();
  assert.deepEqual(missing, [], `以下前端路径在后端找不到对应路由：${JSON.stringify(missing, null, 2)}`);
});

test('匹配器具备判别力（防止"什么都匹配"导致假通过）', () => {
  // 不存在的路径必须匹配不上；否则上一条测试恒真，形同虚设。
  const bogus = [
    '/api/definitely-not-a-real-endpoint',
    '/api/admin/nope/{id}',
    '/api/interview/session-does-not-exist',
  ];
  for (const b of bogus) {
    assert.ok(
      !backendRoutes.has(b),
      `后端路由集合错误地包含了不存在的路径 ${b} —— 匹配器可能过于宽松`
    );
  }
  // 反向：一个真实存在的路由必须匹配得上，证明集合非空且比对有效。
  assert.ok(
    backendRoutes.has('/api/question_bank'),
    '后端路由集合缺少已知真实路由 /api/question_bank'
  );
});

test('前端 API 路径不得残留硬编码的 host', () => {
  const offenders = [];
  for (const file of walk(SRC_DIR, (n) => /\.(ts|tsx)$/.test(n))) {
    const src = stripComments(readFileSync(file, 'utf8'));
    if (/https?:\/\/127\.0\.0\.1/.test(src)) offenders.push(path.relative(REPO_ROOT, file));
  }
  // T-34 起本规则**不再豁免 config.ts**（原来那句"待 T-34 修复后再纳入"已经兑现）：
  // API 基地址默认是空串（相对路径）；写死 host 会让 HTTPS 页面被混合内容拦死、
  // 换域名后全站 CORS，且地址被内联进产物、换环境必须重新构建。
  assert.deepEqual(
    offenders,
    [],
    `不应有文件硬编码 127.0.0.1：${JSON.stringify(offenders)}`
  );
});
