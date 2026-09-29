/**
 * T-49 / FR-9.2 契约测试：题库条目缺字段**不许崩**。
 *
 * 运行：cd frontend && npm run test:node      （零依赖、不联网、毫秒级）
 *
 * ── 为什么这一条是**行为测试**而不是源码 grep ──────────────────────────────
 * `questionTags()` 是纯函数且刻意不 import `config`（后者读 `import.meta.env`，
 * 在 Node 里会抛），所以可以直接 `import` 进 node:test 用真实数据断言：
 * 缺 `tags`、`tags: null`、`tags: '字符串'`、条目本身为 null …… 都必须返回 `[]`。
 *
 * 同时保留一条**源码结构**断言（渲染层必须走 questionTags），
 * 因为"函数没抛"不等于"页面用上了它"—— 修复前页面直接写的是 `q.tags.map`。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { questionTags } from '../src/interview/questionBankEntry.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(HERE, '..', 'src');
const MODAL = readFileSync(path.join(SRC, 'interview', 'QuestionBankModal.tsx'), 'utf8');
const QUERY = readFileSync(path.join(SRC, 'interview', 'questionBankEntry.ts'), 'utf8');

/** 去注释后再做"代码形状"断言：注释里正**引用**着修复前的写法 `q.tags.map(...)`。 */
function codeOnly(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(/\r?\n/)
    .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\s\/\/.*$/, '')))
    .join('\n');
}

const MODAL_CODE = codeOnly(MODAL);

// ---------------------------------------------------------------------------
// 1. 真实行为：缺字段一律收敛成 []
// ---------------------------------------------------------------------------

test('T-49：缺 tags 的题目返回空数组，而不是抛异常（FR-9.2）', () => {
  const cases = [
    ['完全没有 tags 字段', { text: '什么是闭包？' }],
    ['tags 显式为 undefined', { tags: undefined }],
    ['tags 为 null（后端 NULL 落库）', { tags: null }],
    ['tags 是字符串（后端改类型）', { tags: 'javascript' }],
    ['tags 是对象', { tags: { a: 1 } }],
    ['条目本身是 null', null],
    ['条目本身是 undefined', undefined],
    ['条目是空对象', {}],
  ];
  for (const [label, entry] of cases) {
    assert.doesNotThrow(() => questionTags(entry), `${label} 时抛异常了`);
    assert.deepEqual(questionTags(entry), [], `${label} 应返回 []`);
  }
});

test('T-49：正常 tags 原样返回（不过度加工，避免修 bug 修出新行为）', () => {
  assert.deepEqual(questionTags({ tags: ['react', 'hooks'] }), ['react', 'hooks']);
  assert.deepEqual(questionTags({ tags: [] }), []);
  const shared = ['a'];
  assert.equal(questionTags({ tags: shared }), shared, '不应复制数组（渲染层只读）');
});

test('判别力：直接写 q.tags.map 在缺字段时**确实会崩**（证明上面不是在防一个假想问题）', () => {
  // 修复前的真实写法
  const before = (q) => q.tags.map((tag) => tag);
  assert.throws(() => before({ text: '缺字段' }), TypeError);
  assert.throws(() => before({ tags: null }), TypeError);
  // 而修复后的写法在同样输入下安然无事
  assert.deepEqual(questionTags({ text: '缺字段' }), []);
});

test('判别力：判定函数本身能识别"没走 questionTags"的渲染代码', () => {
  const bad = '<span>{q.tags.map(tag => <i key={tag}>{tag}</i>)}</span>';
  const good = '<span>{questionTags(q).map(tag => <i key={tag}>{tag}</i>)}</span>';
  assert.ok(/\bq\.tags\.map\(/.test(bad), '裸 q.tags.map 未被识别');
  assert.ok(!/\bq\.tags\.map\(/.test(good), '修好后的写法被误判');
});

// ---------------------------------------------------------------------------
// 2. 源码结构：渲染层真的用上了它，且分类缺失也不崩
// ---------------------------------------------------------------------------

test('T-49：题库弹窗的标签渲染走 questionTags(q)，且不再出现裸 q.tags.map', () => {
  assert.match(MODAL_CODE, /questionTags\(q\)\.map\(/, '题库弹窗没有用 questionTags 渲染标签');
  assert.ok(!/\bq\.tags\.map\(/.test(MODAL_CODE), '题库弹窗里仍有裸 q.tags.map（缺字段即白屏）');
  // 分类整体缺失时也不能崩：`questionBank[cat]?.map` 的可选链是同一类防护
  assert.match(MODAL_CODE, /questionBank\[selectedBankCategory\]\?\.map\(/, '分类缺失时没有可选链防护');
});

test('T-49：防御性读取放在不依赖 import.meta.env 的模块里（否则 Node 里跑不了行为测试）', () => {
  assert.match(QUERY, /export function questionTags/, 'questionBankEntry.ts 未导出 questionTags');
  assert.ok(
    !/from '\.\.\/config'/.test(QUERY),
    'questionBankEntry.ts 不应 import config（import.meta.env 在 Node 里会抛，行为测试将无法直接 import）',
  );
});
