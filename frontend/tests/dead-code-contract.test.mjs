/**
 * T-48 契约测试：死代码清理（`historyListRef` / `QuestionBank.tsx` / `_passwordError`）。
 *
 * 运行：cd frontend && npm run test:node      （零依赖、不联网、毫秒级）
 *
 * ── 为什么"grep 为 0"必须写成测试 ──────────────────────────────────────────
 * 验收标准是 `docs/03-tasks.md` 里那句「上述死代码全库 grep 为 0」。
 * 一次性 grep 会在下次重构后又悄悄变回 1 —— 所以把它固化成断言，
 * 并对每条判定做**判别力自检**（拿修复前的真实写法喂进去必须判为"命中"）。
 *
 * 注意扫的是**整棵 src 树**：T-46 / T-47 之后代码分散在多个文件，
 * 只扫 `App.tsx` 的 grep 等于没扫。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(HERE, '..', 'src');

/** 收集 src 下全部 .ts/.tsx。 */
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

/** 全库命中：返回 `文件:行号` 列表（找得到就说明死代码还在）。 */
function grep(sources, pattern) {
  const re = typeof pattern === 'string' ? new RegExp(pattern) : pattern;
  const hits = [];
  for (const [name, src] of sources) {
    src.split(/\r?\n/).forEach((line, i) => {
      if (re.test(line)) hits.push(`${name}:${i + 1}: ${line.trim()}`);
    });
  }
  return hits;
}

/** 去掉注释后的全库文本（注释里正**引用**着被删掉的写法，那是文档不是代码）。 */
function codeOnly(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(/\r?\n/)
    .map((line) => (line.trim().startsWith('//') ? '' : line.replace(/\s\/\/.*$/, '')))
    .join('\n');
}

const CODE_SOURCES = new Map([...SOURCES.entries()].map(([name, src]) => [name, codeOnly(src)]));

// ---------------------------------------------------------------------------
// 1. historyListRef（只写不读的 ref）
// ---------------------------------------------------------------------------

test('T-48：`historyListRef` 全库为 0（只写不读的 ref 已删）', () => {
  const hits = grep(CODE_SOURCES, /\bhistoryListRef\b/);
  assert.deepEqual(hits, [], `仍有 historyListRef：\n${hits.join('\n')}`);
});

test('判别力：修复前的写法必须被同一个 grep 判为命中', () => {
  // 修复前真实存在的两处：声明 + 绑定
  const before = new Map([
    ['synthetic/NotificationCenter.tsx', '<ul ref={historyListRef} className="x">'],
    ['synthetic/useNotificationCenter.ts', 'const historyListRef = useRef<HTMLUListElement>(null);'],
  ]);
  assert.equal(grep(before, /\bhistoryListRef\b/).length, 2, '判别力失效：修复前的写法没被抓到');
});

// ---------------------------------------------------------------------------
// 2. _passwordError（只写不读的状态）
// ---------------------------------------------------------------------------

test('T-48：`_passwordError` / `setPasswordError` 全库为 0（死状态连同 3 处写入已删）', () => {
  for (const pattern of [/_passwordError\b/, /\bsetPasswordError\b/]) {
    const hits = grep(CODE_SOURCES, pattern);
    assert.deepEqual(hits, [], `仍有 ${pattern}：\n${hits.join('\n')}`);
  }
});

test('判别力：修复前的写法必须被同一个 grep 判为命中', () => {
  const before = new Map([
    [
      'synthetic/AuthModal.tsx',
      "const [_passwordError, setPasswordError] = useState('');\nsetPasswordError(passwordError(pwd) ?? '');",
    ],
  ]);
  assert.equal(grep(before, /_passwordError\b/).length, 1, '死状态声明没被抓到');
  assert.equal(grep(before, /\bsetPasswordError\b/).length, 2, '死 setter 的两处写入没被抓到');
});

// ---------------------------------------------------------------------------
// 3. QuestionBank.tsx（从未被引用的旧组件）
// ---------------------------------------------------------------------------

test('T-48：死组件 `src/QuestionBank.tsx` 已删除，且无人 import 它', () => {
  assert.ok(!existsSync(path.join(SRC, 'QuestionBank.tsx')), 'src/QuestionBank.tsx 仍然存在');
  // 判别力：同一个 existsSync 对**活的**文件必须为真，否则"删除"是被假通过掩盖的
  assert.ok(existsSync(path.join(SRC, 'interview', 'QuestionBankModal.tsx')), '把活的题库弹窗也删了？');

  // 只匹配精确的模块说明符（`QuestionBank`，不含 `QuestionBankModal` / `questionBank`）
  const hits = grep(CODE_SOURCES, /from\s+'[^']*\/QuestionBank'/);
  assert.deepEqual(hits, [], `仍有地方 import 已删除的 QuestionBank：\n${hits.join('\n')}`);
});

test('判别力：旧的 import 写法必须被同一个 grep 判为命中', () => {
  const before = new Map([['synthetic/App.tsx', "import QuestionBank from './QuestionBank';"]]);
  assert.equal(grep(before, /from\s+'[^']*\/QuestionBank'/).length, 1, '判别力失效：旧 import 没被抓到');
  // 反向：活的题库弹窗不能被这条规则误伤
  const alive = new Map([['synthetic/App.tsx', "import { QuestionBankModal } from './interview/QuestionBankModal';"]]);
  assert.equal(grep(alive, /from\s+'[^']*\/QuestionBank'/).length, 0, '活文件被误判为已删除组件');
});

// ---------------------------------------------------------------------------
// 4. 清理不许"删错东西"：活着的题库链路必须完整
// ---------------------------------------------------------------------------

test('T-48：清理后题库链路仍然完整（三个活文件都在，且取数只有一个归属）', () => {
  for (const rel of [
    'interview/questionBank.ts',
    'interview/questionBankEntry.ts',
    'interview/QuestionBankModal.tsx',
  ]) {
    assert.ok(SOURCES.has(rel), `缺少 ${rel}`);
  }
  // T-36：不能再靠 `src.includes('/api/question_bank')` 判归属 —— 统一层把
  // **公开端点前缀**登记在 `services/authResponse.ts`（那是路由表，不是调用点）。
  // 因此改成"认调用形态"，两种写法都要认：收敛前的 `${API_BASE_URL}/api/x` 与收敛后的 `'/api/x'`。
  const callExpr = /(?:Get|Post|Put|Patch|Delete|Fetch|request)\s*\(\s*(?:[`'"]\$\{API_BASE_URL\}\/api\/question_bank|[`'"]\/api\/question_bank)/i;
  const callers = [...CODE_SOURCES.entries()]
    .filter(([, src]) => callExpr.test(src))
    .map(([name]) => name)
    .sort();
  assert.deepEqual(callers, ['interview/questionBank.ts'], `题库取数归属不唯一：${callers.join('、')}`);
  // 判别力自检：两种写法都必须被认出来，且无关端点不能被误认。
  assert.match(
    "await fetch(`${API_BASE_URL}/api/question_bank`)",
    callExpr,
    '归属判定认不出收敛前的写法'
  );
  assert.match("await publicGet('/api/question_bank')", callExpr, '归属判定认不出收敛后的写法');
  assert.ok(!callExpr.test("await apiGet('/api/notifications')"), '归属判定把别的端点也认成了题库');
  assert.match(SOURCES.get('App.tsx'), /<QuestionBankModal/, 'App.tsx 仍应装配题库弹窗');
});

test('T-48：清理之后全树仍然都在 ≤400 行预算内（删除不是拆分的替代品）', () => {
  const over = [...SOURCES.entries()]
    .filter(([, src]) => src.split('\n').length > 400)
    .map(([name, src]) => `${name}(${src.split('\n').length} 行)`);
  assert.deepEqual(over, []);
});
