/**
 * T-11 契约测试：前端不得再发送死参数（FR-4.9）。
 *
 * 背景：后端已于 T-04/T-11 移除 `ChatRequest.user_id`、`ChatRequest.action`、
 * `ReportRequest.user_id`。前端若继续发送，虽因 Pydantic extra='ignore' 不会报错，
 * 但会造成"接口接受这些参数"的错觉，日后极易被误用 —— 必须在契约层守住。
 *
 * 判定方式：`user_id:` / `action:` 作为**对象属性**出现即视为发送。
 * 注意 `setUserId(data.user_id)` 是读取响应的属性访问，**不带冒号**，
 * 不会被本规则误伤。
 *
 * 运行：cd frontend && npm run test:node
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_TSX = path.resolve(HERE, '..', 'src', 'App.tsx');
const source = readFileSync(APP_TSX, 'utf8');

function lineOf(pattern) {
  const lines = source.split('\n');
  const hits = [];
  lines.forEach((line, i) => {
    if (pattern.test(line)) hits.push(`  ${i + 1}: ${line.trim()}`);
  });
  return hits;
}

test('前端不再发送 action 死参数（ChatRequest.action 已移除）', () => {
  const hits = lineOf(/\baction\s*:/);
  assert.deepEqual(
    hits,
    [],
    `发现 action 属性赋值，后端已不再有该字段：\n${hits.join('\n')}`
  );
});

test('前端不再发送 user_id 死参数（ChatRequest/ReportRequest 已移除）', () => {
  const hits = lineOf(/\buser_id\s*:/);
  assert.deepEqual(
    hits,
    [],
    `发现 user_id 属性赋值。注意：读取响应用的 data.user_id（无冒号）是合法的，` +
      `不应被误判。实际问题行：\n${hits.join('\n')}`
  );
});

test('识别规则本身有效（防止正则写错导致永远通过）', () => {
  // 正样本：这些写法必须能被识别出来
  assert.ok(/\buser_id\s*:/.test('const b = { user_id: 1 };'));
  assert.ok(/\baction\s*:/.test("bodyObj.action = 'start';".replace('.action =', '.action:')));
  // 负样本：读取响应的属性访问不应被误伤
  assert.ok(!/\buser_id\s*:/.test('setUserId(data.user_id);'));
  assert.ok(!/\baction\s*:/.test('const x = res.action;'));
});
