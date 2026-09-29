/**
 * T-13 契约测试：跳过词必须是**单一来源**（FR-4.10）。
 *
 * 背景：修复前 `App.tsx` 与服务端各硬编码一份完全相同的 23 词列表。
 * 任一侧单独改动都会让"前端本地判定"与"服务端判定"分歧，且**不会有任何报错**。
 *
 * 修复后：唯一来源是后端常量，经 `GET /api/interview/config` 下发。
 * 因此本测试的角色从"断言两份列表一致"改为"断言前端确实没有第二份列表" ——
 * 后者才是新架构下真正要守的不变量。
 *
 * 运行：cd frontend && npm run test:node
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const APP_TSX = path.join(REPO_ROOT, 'frontend', 'src', 'App.tsx');
const INTERVIEW_PY = path.join(REPO_ROOT, 'backend', 'routers', 'interview.py');

const appSource = readFileSync(APP_TSX, 'utf8');
const pySource = readFileSync(INTERVIEW_PY, 'utf8');

/** 抽出后端 SKIP_WORDS 常量的字面量。 */
function backendSkipWords() {
  const m = pySource.match(/SKIP_WORDS\s*=\s*\[([\s\S]*?)\]/);
  assert.ok(m, '后端未找到 SKIP_WORDS 常量');
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

test('后端 SKIP_WORDS 常量存在且为 23 个不重复词', () => {
  const words = backendSkipWords();
  assert.equal(words.length, 23, `SKIP_WORDS 数量由 23 变为 ${words.length}，若为有意变更请同步更新本断言`);
  assert.equal(new Set(words).size, 23, 'SKIP_WORDS 存在重复项');
  assert.ok(words.every((w) => w.trim() !== ''), 'SKIP_WORDS 存在空串');
});

/** 去掉注释，避免注释里提到跳过词造成误判。 */
function stripComments(source) {
  return source
    .split(/\r?\n/)
    .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\/\/.*$/, '')))
    .join('\n')
    .replace(/\/\*[\s\S]*?\*\//g, '');
}

/** 找出所有"含 2 个以上跳过词的数组字面量" —— 即硬编码列表。 */
function hardcodedSkipWordLists(source, words) {
  const stripped = stripComments(source);
  const literals = [...stripped.matchAll(/\[([^[\]]*)\]/g)].map((m) => m[1]);
  return literals
    .map((body) => ({
      body: body.trim().slice(0, 120),
      hits: words.filter((w) => body.includes(`'${w}'`) || body.includes(`"${w}"`)),
    }))
    .filter((x) => x.hits.length >= 2);
}

test('前端不再硬编码跳过词列表（单一来源不变量的核心）', () => {
  const words = backendSkipWords();
  const offending = hardcodedSkipWordLists(appSource, words);

  assert.deepEqual(
    offending,
    [],
    `前端仍存在硬编码的跳过词列表：${JSON.stringify(offending)}。` +
      `跳过词的唯一来源应为后端 GET /api/interview/config。`
  );
});

test('硬编码检测规则本身有效（防止规则写错导致永远通过）', () => {
  const words = backendSkipWords();

  // 正样本：一份典型的硬编码列表必须被识别出来
  const syntheticList = `const skipWords = [${words
    .slice(0, 5)
    .map((w) => `'${w}'`)
    .join(', ')}];`;
  assert.equal(
    hardcodedSkipWordLists(syntheticList, words).length,
    1,
    '检测规则未识别出明显的硬编码列表'
  );

  // 负样本：文案里偶然出现单个跳过词不应被误判
  // （App.tsx 里就有 '跳过失败，请手动输入"跳过"重试' 这样的提示文案）
  const messageOnly = `setMessages(prev => [...prev, { content: '跳过失败，请手动输入"跳过"重试' }]);`;
  assert.equal(
    hardcodedSkipWordLists(messageOnly, words).length,
    0,
    '含单个跳过词的文案被误判为硬编码列表'
  );

  // 注释里提到跳过词也不应被误判
  const inComment = `// 跳过词（如 不会 / 下一题）由后端下发\nconst x = 1;`;
  assert.equal(hardcodedSkipWordLists(inComment, words).length, 0);
});

test('前端确实从 config 接口获取跳过词', () => {
  assert.ok(
    /\/api\/interview\/config/.test(appSource),
    '前端未调用 /api/interview/config'
  );
  assert.ok(
    /setSkipWords\s*\(/.test(appSource),
    '前端未把后端下发的 skip_words 写入状态'
  );
});

test('后端确实提供了 /api/interview/config 路由并返回 skip_words', () => {
  assert.ok(
    /@router\.get\(\s*["']\/interview\/config["']\s*\)/.test(pySource),
    '后端缺少 GET /api/interview/config 路由'
  );
  assert.ok(
    /"skip_words"\s*:/.test(pySource),
    'config 路由未返回 skip_words 字段'
  );
});

test('跳过词判定与下发的常量同源（都引用 SKIP_WORDS）', () => {
  // 换行一律写成 `\r?\n`：这个断言只看"函数体里有没有引用 SKIP_WORDS"，
  // 与换行符无关。写成 `\n\n` 会让**任何**把文件存成 CRLF 的编辑器
  // （Notepad、甚至某些工具链）把它变成一条假警报 —— 断言本身没错，
  // 但报出来的原因会指向完全无关的地方（曾经真的踩过一次）。
  const m = pySource.match(/def _is_skip_message[\s\S]*?\r?\n\r?\n/);
  assert.ok(m, '未找到 _is_skip_message 函数');
  assert.ok(
    /SKIP_WORDS/.test(m[0]),
    '_is_skip_message 未引用 SKIP_WORDS，可能又出现了一份独立列表'
  );
});
