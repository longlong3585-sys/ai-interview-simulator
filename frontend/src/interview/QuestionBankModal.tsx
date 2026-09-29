/**
 * T-47：题库弹窗（原 App.tsx 的 JSX 第 2063~2117 行 + 两处内联的取数逻辑）。
 *
 * 拆分前"打开题库 + 拉取数据"这段 `try/catch/finally` 在顶部导航和落地页
 * 各抄了一份；现在收敛成组件内的一个 `open` 副作用，只有一份。
 *
 * T-49 / FR-9.2：标签渲染改走 `questionTags(q)`（缺 `tags` 字段返回 `[]`）。
 * 修复前这里写的是 `q.tags.map(...)` —— 一条缺字段的题目就让整个题库白屏。
 */

import { useEffect, useState } from 'react';
import { fetchQuestionBank } from './questionBank';
import { questionTags, type QuestionBankData } from './questionBankEntry';

export function QuestionBankModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [questionBank, setQuestionBank] = useState<QuestionBankData>({});
  const [selectedBankCategory, setSelectedBankCategory] = useState('后端开发');
  const [questionBankLoading, setQuestionBankLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = async () => {
      setQuestionBankLoading(true);
      try {
        const data = await fetchQuestionBank();
        if (!cancelled) setQuestionBank(data);
      } catch (err) {
        console.error(err);
      } finally {
        if (!cancelled) setQuestionBankLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" onClick={onClose}>
      <div className="animate-scale-in bg-white rounded-2xl p-6 max-w-2xl w-full max-h-[80vh] overflow-auto shadow-float m-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center">
              <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
            </div>
            <h2 className="text-xl font-bold text-slate-800">面试题库</h2>
          </div>
          <button onClick={onClose} className="btn-ghost w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-600">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>
        {questionBankLoading ? (
          <div className="flex flex-col items-center py-12 text-slate-400">
            <div className="w-10 h-10 rounded-full border-2 border-primary-200 border-t-primary-600 animate-spin mb-3" />
            <p className="text-sm">加载中...</p>
          </div>
        ) : Object.keys(questionBank).length === 0 ? (
          <p className="text-center py-12 text-slate-400 text-sm">题库加载失败，请稍后重试</p>
        ) : (
          <>
            <div className="mb-5 flex flex-wrap gap-2">
              {Object.keys(questionBank).map(cat => (
                <button
                  key={cat}
                  className={`px-3.5 py-1.5 rounded-full text-xs font-medium transition-all duration-200 ${selectedBankCategory === cat ? 'bg-primary-600 text-white shadow-md shadow-primary-200' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'}`}
                  onClick={() => setSelectedBankCategory(cat)}
                >
                  {cat}
                </button>
              ))}
            </div>
            <ul className="space-y-2">
              {questionBank[selectedBankCategory]?.map((q, idx) => (
                <li key={idx} className="bg-slate-50 rounded-xl p-3.5 hover:bg-slate-100 transition-colors">
                  <p className="font-medium text-sm text-slate-800">{q.text}</p>
                  <div className="flex items-center gap-2 mt-1.5">
                    <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${q.difficulty === '困难' ? 'bg-red-100 text-red-600' : q.difficulty === '中等' ? 'bg-amber-100 text-amber-600' : 'bg-emerald-100 text-emerald-600'}`}>
                      {q.difficulty}
                    </span>
                    {questionTags(q).map(tag => (
                      <span key={tag} className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-200 text-slate-500">{tag}</span>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
