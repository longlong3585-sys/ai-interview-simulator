import { useState, useEffect } from 'react';
import { API_BASE_URL } from './config';

const difficultyConfig: Record<string, { color: string; label: string }> = {
  '简单': { color: 'bg-emerald-100 text-emerald-700', label: '简单' },
  '中等': { color: 'bg-amber-100 text-amber-700', label: '中等' },
  '困难': { color: 'bg-red-100 text-red-700', label: '困难' },
  'easy': { color: 'bg-emerald-100 text-emerald-700', label: '简单' },
  'medium': { color: 'bg-amber-100 text-amber-700', label: '中等' },
  'hard': { color: 'bg-red-100 text-red-700', label: '困难' },
};

export default function QuestionBank() {
  const [category, setCategory] = useState("后端开发");
  const [questionBank, setQuestionBank] = useState<Record<string, Array<{ text: string; difficulty: string; tags: string[] }>>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_BASE_URL}/api/question_bank`)
      .then(res => res.json())
      .then(data => {
        setQuestionBank(data);
      })
      .catch(err => {
        console.error('题库加载失败:', err);
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  const questions = questionBank[category] || [];

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-slate-50">
      <header className="sticky top-0 z-40 bg-white/80 backdrop-blur-xl border-b border-slate-200/60 shadow-sm">
        <div className="max-w-4xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center shadow-md shadow-primary-200">
              <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
            </div>
            <h1 className="text-lg font-bold bg-gradient-to-r from-primary-700 to-primary-500 bg-clip-text text-transparent">面试题库</h1>
          </div>
          <span className="text-xs text-slate-400">仅供练习参考</span>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8 animate-fade-in">
        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 gap-3">
            <div className="w-10 h-10 rounded-full border-2 border-primary-200 border-t-primary-600 animate-spin" />
            <p className="text-sm text-slate-400">加载题库中...</p>
          </div>
        ) : Object.keys(questionBank).length === 0 ? (
          <div className="text-center py-20">
            <div className="w-16 h-16 mx-auto rounded-2xl bg-slate-100 flex items-center justify-center mb-4">
              <svg className="w-8 h-8 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
            </div>
            <p className="text-slate-500 font-medium">题库加载失败</p>
            <p className="text-sm text-slate-400 mt-1">请确认后端服务已启动</p>
          </div>
        ) : (
          <>
            <div className="flex gap-2 mb-8 flex-wrap">
              {Object.keys(questionBank).map(cat => (
                <button
                  key={cat}
                  className={`px-4 py-2 rounded-full text-sm font-medium transition-all duration-200 ${
                    category === cat
                      ? 'bg-primary-600 text-white shadow-md shadow-primary-200'
                      : 'bg-white text-slate-600 border border-slate-200 hover:border-primary-300 hover:text-primary-600'
                  }`}
                  onClick={() => setCategory(cat)}
                >
                  {cat}
                </button>
              ))}
            </div>

            <div className="text-sm text-slate-400 mb-4">
              共 <span className="font-semibold text-primary-600">{questions.length}</span> 道题目
            </div>

            <ul className="space-y-3">
              {questions.map((q, idx) => {
                const diff = difficultyConfig[q.difficulty] || { color: 'bg-slate-100 text-slate-600', label: q.difficulty || '未标注' };
                return (
                  <li key={idx} className="card animate-slide-up flex items-start gap-4" style={{ animationDelay: `${idx * 0.03}s` }}>
                    <span className="w-8 h-8 rounded-lg bg-primary-50 text-primary-600 flex items-center justify-center text-sm font-bold shrink-0">
                      {idx + 1}
                    </span>
                    <div className="flex-1 min-w-0">
                      <p className="font-semibold text-slate-800 leading-relaxed">{q.text}</p>
                      <div className="flex items-center gap-2 mt-2 flex-wrap">
                        <span className={`badge text-[11px] ${diff.color}`}>{diff.label}</span>
                        {q.tags?.map((tag, ti) => (
                          <span key={ti} className="badge bg-slate-100 text-slate-500 text-[11px]">{tag}</span>
                        ))}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>

            {questions.length === 0 && (
              <div className="text-center py-16 text-slate-400">
                <p>该分类暂无题目</p>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
