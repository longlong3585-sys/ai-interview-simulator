/**
 * T-34 / Bug 4（P0）契约测试：API 基地址必须**相对化**且**不含写死的 host**。
 *
 * 运行：cd frontend && npm run test:node      （零依赖、不联网、毫秒级）
 *
 * 分三层，缺一层都会留下"看起来改好了"的假象：
 *   1. **纯逻辑**：`normalizeApiBaseUrl()` 的行为（空值→''、末尾斜杠归一、拼接不变式）；
 *   2. **接线**：`config.ts` 真的用了它，且不再出现 `127.0.0.1`；`vite.config.ts` 真的有
 *      `server.proxy` / `preview.proxy`（否则开发与预览环境下前端会自己吞掉所有请求）；
 *   3. **产物口径**：任何会进入浏览器产物的文件（`src/**`、`index.html`、
 *      `Vite 会加载的 .env*`）都**不许**出现写死的 host —— 这正是验收标准
 *      「构建产物中 grep 不到 127.0.0.1」的可离线版本。
 *
 * 每条判定都带**判别力自检**：把修复前的写法喂进同一个判定函数，必须判为失败。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { normalizeApiBaseUrl } from '../src/utils/apiBaseUrl.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..');
const SRC = path.join(FRONTEND, 'src');

const CONFIG = readFileSync(path.join(SRC, 'config.ts'), 'utf8');
const VITE_CONFIG = readFileSync(path.join(FRONTEND, 'vite.config.ts'), 'utf8');
const INDEX_HTML = readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');

/** 写死的后端地址（Bug 4 的元凶）。`localhost` 一并算：它同样只在开发机成立。 */
const HARDCODED_HOST = /127\.0\.0\.1|localhost|0\.0\.0\.0/;

/** 去掉注释：注释里正**引用**着这些旧地址（那是文档，不是代码，也不会进产物）。 */
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

/** 会进入浏览器产物的文件 → 源码。 */
function shippedFiles() {
  const files = new Map();
  files.set('index.html', INDEX_HTML);
  for (const full of walkSources()) {
    files.set(path.relative(FRONTEND, full).replace(/\\/g, '/'), readFileSync(full, 'utf8'));
  }
  return files;
}

/** 找出写死了 host 的**可发布**文件（注释不算）。 */
function hardcodedHostOffenders(files) {
  const offenders = [];
  for (const [name, src] of files) {
    if (HARDCODED_HOST.test(stripComments(src))) offenders.push(name);
  }
  return offenders.sort();
}

// ---------------------------------------------------------------------------
// 1. 纯逻辑
// ---------------------------------------------------------------------------

test('T-34：未配置 / 空串 / 纯空白 一律解析成空串（= 相对路径）', () => {
  for (const raw of [undefined, null, '', '   ', '\n']) {
    assert.equal(normalizeApiBaseUrl(raw), '', `${JSON.stringify(raw)} 应解析成空串`);
  }
});

test('T-34：末尾斜杠被归一，拼接后不会出现 `//api`', () => {
  const cases = [
    ['http://127.0.0.1:8000', 'http://127.0.0.1:8000'],
    ['http://127.0.0.1:8000/', 'http://127.0.0.1:8000'],
    ['https://api.example.com///', 'https://api.example.com'],
    ['  https://api.example.com  ', 'https://api.example.com'],
  ];
  for (const [raw, expected] of cases) {
    assert.equal(normalizeApiBaseUrl(raw), expected, `${raw} 归一化结果不对`);
  }
  for (const raw of ['', 'https://api.example.com/', 'https://api.example.com']) {
    const url = `${normalizeApiBaseUrl(raw)}/api/chat`;
    assert.ok(!url.replace(/^https?:\/\//, '').includes('//'), `拼接出了双斜杠：${url}`);
  }
});

test('判别力：修复前的默认值（写死 localhost）必须被判定函数认出来', () => {
  const before = "export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';";
  assert.ok(HARDCODED_HOST.test(before), '判别力失效：修复前的写法没被判为"写死 host"');
  assert.equal(
    normalizeApiBaseUrl(undefined),
    '',
    '未配置时必须落到相对路径，绝不能回退到某个内置 host',
  );
});

// ---------------------------------------------------------------------------
// 2. 接线
// ---------------------------------------------------------------------------

test('T-34：config.ts 走 VITE_API_BASE_URL + 归一化，且代码里不再写死 host', () => {
  assert.match(CONFIG, /import\.meta\.env\.VITE_API_BASE_URL/, 'config.ts 未读取 VITE_API_BASE_URL');
  assert.match(CONFIG, /normalizeApiBaseUrl\(/, 'config.ts 未做归一化（末尾斜杠会拼出 //api）');
  // 注释里解释了旧地址（那是文档），所以这里只看代码
  assert.ok(
    !HARDCODED_HOST.test(stripComments(CONFIG)),
    'config.ts 的**代码**里仍有写死的 host',
  );
  assert.ok(
    /export const API_BASE_URL\s*=/.test(CONFIG),
    'config.ts 未导出 API_BASE_URL（其余模块全靠它）',
  );
});

test('T-34：vite.config.ts 提供 server.proxy 与 preview.proxy，且目标落在 /api 与 /uploads', () => {
  assert.match(VITE_CONFIG, /server:\s*\{\s*proxy/, 'vite.config.ts 缺少 server.proxy（开发环境会自己吞掉请求）');
  assert.match(VITE_CONFIG, /preview:\s*\{\s*proxy/, 'vite.config.ts 缺少 preview.proxy（npm run preview 点不动按钮）');
  assert.match(VITE_CONFIG, /'\/api':\s*\{\s*target/, "proxy 缺少 '/api' 转发");
  assert.match(VITE_CONFIG, /'\/uploads':\s*\{\s*target/, "proxy 缺少 '/uploads' 转发（头像 img src 走这个前缀）");
  assert.match(VITE_CONFIG, /changeOrigin:\s*true/, 'proxy 未设置 changeOrigin（后端拿到的 Host 会是 5173）');
  // 代理目标必须可配置，且默认值只出现在**Node 侧**配置文件里（永不进产物）
  assert.match(VITE_CONFIG, /DEV_PROXY_TARGET/, '代理目标不可配置（换环境要改代码）');
  assert.match(VITE_CONFIG, /loadEnv\(/, '未用 loadEnv 读 .env（只认 shell 变量会让 .env.local 失效）');
});

test('T-34：环境变量模板存在，且明确写了"留空 = 相对路径"', () => {
  const example = path.join(FRONTEND, '.env.example');
  assert.ok(existsSync(example), '缺少 frontend/.env.example（新人不知道要配什么）');
  const text = readFileSync(example, 'utf8');
  assert.match(text, /^VITE_API_BASE_URL=\s*$/m, '.env.example 应示范"留空 = 相对路径"');
  assert.match(text, /DEV_PROXY_TARGET/, '.env.example 未说明代理目标变量');
  assert.match(text, /混合内容/, '.env.example 未警告 HTTPS 页面不能填 http:// 地址');
});

// ---------------------------------------------------------------------------
// 3. 产物口径（验收标准「构建产物中 grep 不到 127.0.0.1」的离线版）
// ---------------------------------------------------------------------------

test('T-34：会进浏览器产物的文件里，写死的 host 为 0', () => {
  const offenders = hardcodedHostOffenders(shippedFiles());
  assert.deepEqual(offenders, [], `这些文件会被打进产物，却写死了 host：${offenders.join('、')}`);
});

test('判别力：把修复前的 config.ts 塞回产物文件集里，必须被判为违规', () => {
  const offenders = hardcodedHostOffenders(
    new Map([
      ['src/config.ts', CONFIG],
      ['synthetic/config.ts', "export const API_BASE_URL = 'http://127.0.0.1:8000';"],
    ]),
  );
  assert.deepEqual(offenders, ['synthetic/config.ts']);
});

test('T-34：会被 Vite 加载的 .env* 里，VITE_ 变量不许出现写死的 host', () => {
  // `.env.example` 只是模板，Vite **不会**加载它；真正会被加载的是下面这些名字。
  const loadable = ['.env', '.env.local', '.env.development', '.env.production', '.env.development.local'];
  const offenders = [];
  for (const name of loadable) {
    const full = path.join(FRONTEND, name);
    if (!existsSync(full)) continue;
    for (const line of readFileSync(full, 'utf8').split(/\r?\n/)) {
      const m = /^\s*(VITE_[A-Z0-9_]+)\s*=\s*(.*)$/.exec(line);
      if (m && HARDCODED_HOST.test(m[2])) offenders.push(`${name}: ${m[1]}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些 VITE_ 变量会被内联进产物，不能写死 host：${offenders.join('；')}`,
  );
});
