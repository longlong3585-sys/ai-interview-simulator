/**
 * T-45：管理后台的「面试报告详情」弹窗。
 *
 * 从 `AdminPanel.tsx` 拆出的第二个部分（第一个是面板本体，原在 `App.tsx` 内）。
 * 拆它的理由很直接：拆完面板本体后 `AdminPanel.tsx` 仍有 472 行，
 * 越过 T-45~T-47 的「单文件 ≤400 行」预算 —— 而这 100 行是**纯展示**，
 * 除了 `detailInterview` 与一个关闭回调之外不依赖任何面板状态，
 * 是最自然的第一刀。
 *
 * 副作用（正向）：弹窗不再参与面板的重渲染路径，报告数据变了也不会牵动表格。
 */

export interface InterviewDetail {
  id: number;
  username?: string;
  role?: string;
  created_at: string;
  status?: string;
  admin_comment?: string;
  report?: {
    overall_score: number;
    expression_score: number;
    technical_score: number;
    logic_score: number;
    details?: string;
    suggestion?: string;
  } | null;
}

export default function ReportDetailModal({
  detail,
  onClose,
}: {
  detail: InterviewDetail;
  onClose: () => void;
}) {
  const scoreClass = (score: number) =>
    score >= 7 ? 'text-green-600' : score >= 4 ? 'text-yellow-600' : 'text-red-600';

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-lg p-6 max-w-lg w-full max-h-[80vh] overflow-auto" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-bold">面试报告详情</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700 text-xl">×</button>
        </div>
        <div className="text-sm space-y-3">
          <div className="flex gap-4">
            <span>用户：<strong>{detail.username}</strong></span>
            <span>岗位：<strong>{detail.role}</strong></span>
            <span>时间：<strong>{new Date(detail.created_at).toLocaleString()}</strong></span>
          </div>
          {detail.report ? (
            <>
              <div className="flex items-center justify-center p-3 bg-blue-50 rounded-lg">
                <div className="text-center">
                  <div className={`text-3xl font-extrabold ${scoreClass(detail.report.overall_score)}`}>
                    {detail.report.overall_score}/10
                  </div>
                  <div className="text-xs text-gray-500">综合得分</div>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="bg-gray-50 p-2 rounded">
                  <div className="text-xs text-gray-500">表达能力</div>
                  <div className="font-bold">{detail.report.expression_score}/10</div>
                </div>
                <div className="bg-gray-50 p-2 rounded">
                  <div className="text-xs text-gray-500">技术深度</div>
                  <div className="font-bold">{detail.report.technical_score}/10</div>
                </div>
                <div className="bg-gray-50 p-2 rounded">
                  <div className="text-xs text-gray-500">逻辑思维</div>
                  <div className="font-bold">{detail.report.logic_score}/10</div>
                </div>
              </div>
              {detail.report.details && (
                <div className="bg-gray-50 p-2 rounded">
                  <p className="text-xs text-gray-500 mb-1">总结</p>
                  <p>{detail.report.details}</p>
                </div>
              )}
              {detail.report.suggestion && (
                <div className="bg-orange-50 p-2 rounded border border-orange-200">
                  <p className="text-xs text-gray-500 mb-1">改进建议</p>
                  <p className="text-orange-800">{detail.report.suggestion}</p>
                </div>
              )}
            </>
          ) : (
            <p className="text-gray-400 text-center py-4">暂无报告数据</p>
          )}
        </div>
      </div>
    </div>
  );
}
