/**
 * T-03 契约测试（零依赖，node:test 运行）
 *
 * 目标：守住 `00-topic.md §10.3` 记录的"跳过词前后端各存一份、易漂移"风险
 *      （对应需求 FR-4.10）。
 *
 * 该风险目前无测试覆盖：两份列表一旦不一致，用户"打跳过词"的行为
 * 在前端判定与后端判定之间就会出现分歧，且不会有任何报错。
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

/** 从 `name = [ ... ]` 中抽出字符串字面量（兼容单/双引号）。 */
function extractList(source, varName) {
  const re = new RegExp(`${varName}\\s*=\\s*\\[([\\s\\S]*?)\\]`);
  const m = source.match(re);
  if (!m) throw new Error(`未找到变量 ${varName} 的列表字面量`);
  const items = [];
  const itemRe = /['"]([^'"]+)['"]/g;
  let hit;
  while ((hit = itemRe.exec(m[1])) !== null) items.push(hit[1]);
  return items;
}

test('跳过词：前端与后端列表必须完全一致（FR-4.10）', () => {
  const frontend = extractList(readFileSync(APP_TSX, 'utf8'), 'skipWords');
  const backend = extractList(readFileSync(INTERVIEW_PY, 'utf8'), 'skip_words');

  assert.ok(frontend.length > 0, '前端跳过词列表为空');
  assert.ok(backend.length > 0, '后端跳过词列表为空');

  const onlyFrontend = frontend.filter((w) => !backend.includes(w));
  const onlyBackend = backend.filter((w) => !frontend.includes(w));

  assert.deepEqual(
    onlyFrontend,
    [],
    `这些跳过词只存在于前端，后端不认：${JSON.stringify(onlyFrontend)}`
  );
  assert.deepEqual(
    onlyBackend,
    [],
    `这些跳过词只存在于后端，前端不认：${JSON.stringify(onlyBackend)}`
  );
  assert.equal(
    frontend.length,
    backend.length,
    `数量不一致：前端 ${frontend.length}，后端 ${backend.length}`
  );
});

test('跳过词：列表中不得有重复项或空串', () => {
  const frontend = extractList(readFileSync(APP_TSX, 'utf8'), 'skipWords');
  const seen = new Set();
  const dupes = [];
  for (const w of frontend) {
    if (w.trim() === '') dupes.push('(空串)');
    if (seen.has(w)) dupes.push(w);
    seen.add(w);
  }
  assert.deepEqual(dupes, [], `跳过词存在重复/空项：${JSON.stringify(dupes)}`);
});

test('跳过词：数量快照为 23（变更时必须显式更新本测试）', () => {
  const frontend = extractList(readFileSync(APP_TSX, 'utf8'), 'skipWords');
  assert.equal(
    frontend.length,
    23,
    `跳过词数量由 23 变为 ${frontend.length}。若为有意变更，请同步更新本断言与 01-spec.md FR-4.10。`
  );
});
