import { useState, useEffect, useRef } from 'react';
import { API_BASE_URL } from './config';
import { authFetch } from './services/api';

function AdminPanelContent({ token }: { token: string | null }) {
  if (!token) return <div className="p-4 text-center text-gray-500">请先登录</div>;

  const [activeTab, setActiveTab] = useState<'users' | 'interviews' | 'stats'>('stats');
  const [users, setUsers] = useState<any[]>([]);
  const [interviews, setInterviews] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [successMsg, setSuccessMsg] = useState('');
  const [detailInterview, setDetailInterview] = useState<any>(null);

  const fetchUsers = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/users`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      const data = await res.json();
      setUsers(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const fetchInterviews = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/interviews`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      const data = await res.json();
      setInterviews(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const fetchStats = async () => {
    if (!token) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/stats`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      const data = await res.json();
      setStats(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!token) return;
    if (activeTab === 'users') fetchUsers();
    else if (activeTab === 'interviews') fetchInterviews();
    else if (activeTab === 'stats') fetchStats();
  }, [activeTab, token]);

  const updateInterview = async (id: number, status: string, comment: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/interviews/${id}`, {
        method: 'PATCH',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ status, admin_comment: comment })
      });
      if (res.ok) {
        setInterviews(interviews.map(i => i.id === id ? { ...i, status, admin_comment: comment } : i));
        setSuccessMsg('✅ 更新成功');
        setTimeout(() => setSuccessMsg(''), 2000);
      } else {
        const err = await res.json();
        alert(err.detail || '更新失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const resetPassword = async (userId: number, newPassword: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/users/${userId}/reset_password`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Authorization': `Bearer ${token}`
        },
        body: new URLSearchParams({ new_password: newPassword }).toString()
      });
      if (res.ok) {
        alert('密码重置成功');
      } else {
        const err = await res.json();
        alert(err.detail || '重置失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const toggleActive = async (userId: number) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/users/${userId}/toggle_active`, {
        method: 'PATCH',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        fetchUsers();
      } else {
        const err = await res.json();
        alert(err.detail || '操作失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const deleteUser = async (userId: number) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/users/${userId}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        fetchUsers();
      } else {
        const err = await res.json();
        alert(err.detail || '删除失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  const deleteInterview = async (id: number) => {
    if (!confirm('确定要删除这条面试记录吗？删除后不可恢复。')) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/interviews/${id}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        fetchInterviews();
      } else {
        const err = await res.json();
        alert(err.detail || '删除失败');
      }
    } catch (err) {
      console.error(err);
      alert('网络错误');
    }
  };

  return (
    <div>
      <div className="flex gap-2 mb-4 border-b">
        <button onClick={() => setActiveTab('stats')} className={`px-4 py-2 ${activeTab === 'stats' ? 'border-b-2 border-green-500 font-bold' : 'text-gray-500'}`}>📊 仪表盘</button>
        <button onClick={() => setActiveTab('users')} className={`px-4 py-2 ${activeTab === 'users' ? 'border-b-2 border-green-500 font-bold' : 'text-gray-500'}`}>用户管理</button>
        <button onClick={() => setActiveTab('interviews')} className={`px-4 py-2 ${activeTab === 'interviews' ? 'border-b-2 border-green-500 font-bold' : 'text-gray-500'}`}>面试记录</button>
      </div>

      {loading && <p className="text-center py-4">加载中...</p>}
      {successMsg && <p className="text-center py-2 text-green-600 font-medium transition-opacity">{successMsg}</p>}

      {activeTab === 'stats' && !loading && stats && (
        <div className="space-y-6">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-blue-50 p-4 rounded shadow">
              <h3 className="text-sm text-gray-500">总用户数</h3>
              <p className="text-2xl font-bold">{stats.total_users}</p>
            </div>
            <div className="bg-green-50 p-4 rounded shadow">
              <h3 className="text-sm text-gray-500">总面试次数</h3>
              <p className="text-2xl font-bold">{stats.total_interviews}</p>
            </div>
            <div className="bg-yellow-50 p-4 rounded shadow">
              <h3 className="text-sm text-gray-500">平均综合得分</h3>
              <p className="text-2xl font-bold">{stats.avg_overall_score}</p>
            </div>
            <div className="bg-purple-50 p-4 rounded shadow">
              <h3 className="text-sm text-gray-500">通过率</h3>
              <p className="text-2xl font-bold">{stats.pass_rate}%</p>
            </div>
          </div>
          <div className="bg-gray-50 p-4 rounded shadow">
            <h3 className="font-bold mb-2">近7天面试趋势</h3>
            <div className="flex items-end space-x-2 h-40">
              {stats.daily_interviews?.map((day: any) => (
                <div key={day.date} className="flex flex-col items-center flex-1">
                  <div className="bg-blue-500 w-full rounded-t" style={{ height: `${Math.max(day.count * 20, 10)}px` }}></div>
                  <span className="text-xs mt-1">{day.date.slice(5)}</span>
                </div>
              )) || <div className="text-center text-gray-500 w-full">暂无数据</div>}
            </div>
          </div>
        </div>
      )}

      {activeTab === 'users' && !loading && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-100">
                <th className="p-2 text-left">ID</th>
                <th className="p-2 text-left">用户名</th>
                <th className="p-2 text-left">角色</th>
                <th className="p-2 text-left">邮箱</th>
                <th className="p-2 text-left">状态</th>
                <th className="p-2 text-left">创建时间</th>
                <th className="p-2 text-left">操作</th>
               </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id} className="border-b">
                  <td className="p-2">{u.id}</td>
                  <td className="p-2">{u.username}</td>
                  <td className="p-2">
                    <span className={`px-2 py-1 rounded text-xs ${u.role === 'admin' ? 'bg-red-100 text-red-700' : 'bg-blue-100 text-blue-700'}`}>{u.role}</span>
                  </td>
                  <td className="p-2">{u.email || '-'}</td>
                  <td className="p-2">
                    <span className={`px-2 py-1 rounded text-xs ${u.is_active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-700'}`}>{u.is_active ? '活跃' : '禁用'}</span>
                  </td>
                  <td className="p-2">{new Date(u.created_at).toLocaleString()}</td>
                  <td className="p-2">
                    <div className="flex gap-1 flex-wrap">
                      <button
                        onClick={() => {
                          const newPwd = prompt('请输入新密码（至少8位）');
                          if (newPwd && newPwd.length >= 8) {
                            resetPassword(u.id, newPwd);
                          } else if (newPwd) {
                            alert('密码长度至少8位');
                          }
                        }}
                        className="bg-blue-500 text-white px-2 py-1 rounded text-xs"
                      >
                        重置密码
                      </button>
                      {u.role !== 'admin' && (
                        <>
                          <button
                            onClick={() => toggleActive(u.id)}
                            className={`px-2 py-1 rounded text-xs ${u.is_active ? 'bg-yellow-500 text-white' : 'bg-green-500 text-white'}`}
                          >
                            {u.is_active ? '禁用' : '启用'}
                          </button>
                          <button
                            onClick={() => {
                              if (confirm(`确定删除用户 ${u.username}？`)) {
                                deleteUser(u.id);
                              }
                            }}
                            className="bg-red-500 text-white px-2 py-1 rounded text-xs"
                          >
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {users.length === 0 && <p className="text-center py-4 text-gray-500">暂无用户</p>}
        </div>
      )}

      {activeTab === 'interviews' && !loading && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-100">
                <th className="p-2 text-left">ID</th>
                <th className="p-2 text-left">用户</th>
                <th className="p-2 text-left">岗位</th>
                <th className="p-2 text-left">得分</th>
                <th className="p-2 text-left">状态</th>
                <th className="p-2 text-left">评语</th>
                <th className="p-2 text-left">创建时间</th>
                <th className="p-2 text-left">操作</th>
               </tr>
            </thead>
            <tbody>
              {interviews.map(i => (
                <tr key={i.id} className="border-b hover:bg-gray-50">
                  <td className="p-2">{i.id}</td>
                  <td className="p-2">{i.username}</td>
                  <td className="p-2">{i.role}</td>
                  <td className="p-2">
                    {i.report ? (
                      <span className={`font-bold ${i.report.overall_score >= 7 ? 'text-green-600' : i.report.overall_score >= 4 ? 'text-yellow-600' : 'text-red-600'}`}>
                        {i.report.overall_score}
                      </span>
                    ) : '-'}
                  </td>
                  <td className="p-2">
                    <span className={`px-2 py-1 rounded text-xs ${
                      i.status === 'approved' ? 'bg-green-100 text-green-700' :
                      i.status === 'rejected' ? 'bg-red-100 text-red-700' :
                      'bg-yellow-100 text-yellow-700'
                    }`}>
                      {i.status === 'approved' ? '已通过' : i.status === 'rejected' ? '已拒绝' : '待审核'}
                    </span>
                  </td>
                  <td className="p-2">
                    <input
                      type="text"
                      value={i.admin_comment || ''}
                      onChange={e => {
                        const newInterviews = [...interviews];
                        const idx = newInterviews.findIndex(item => item.id === i.id);
                        newInterviews[idx].admin_comment = e.target.value;
                        setInterviews(newInterviews);
                      }}
                      className="border rounded px-1 py-0.5 text-sm w-32"
                      placeholder="评语"
                    />
                  </td>
                  <td className="p-2">{new Date(i.created_at).toLocaleString()}</td>
                  <td className="p-2">
                    <div className="flex gap-1">
                      <button
                        onClick={() => setDetailInterview(i)}
                        className="bg-purple-500 text-white px-2 py-1 rounded text-xs hover:bg-purple-600"
                      >
                        详情
                      </button>
                      <select value={i.status} onChange={e => {
                        const newInterviews = [...interviews];
                        const idx = newInterviews.findIndex(item => item.id === i.id);
                        newInterviews[idx].status = e.target.value;
                        setInterviews(newInterviews);
                      }} className="border rounded px-2 py-1 text-xs">
                        <option value="pending">待审核</option>
                        <option value="approved">通过</option>
                        <option value="rejected">拒绝</option>
                      </select>
                      <button
                        onClick={() => updateInterview(i.id, i.status, i.admin_comment || '')}
                        className="bg-blue-500 text-white px-2 py-1 rounded text-xs"
                      >
                        更新
                      </button>
                      <button
                        onClick={() => deleteInterview(i.id)}
                        className="bg-red-400 text-white px-2 py-1 rounded text-xs hover:bg-red-600"
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {interviews.length === 0 && <p className="text-center py-4 text-gray-500">暂无面试记录</p>}
        </div>
      )}

      {detailInterview && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setDetailInterview(null)}>
          <div className="bg-white rounded-lg p-6 max-w-lg w-full max-h-[80vh] overflow-auto" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-lg font-bold">面试报告详情</h2>
              <button onClick={() => setDetailInterview(null)} className="text-gray-500 hover:text-gray-700 text-xl">×</button>
            </div>
            <div className="text-sm space-y-3">
              <div className="flex gap-4">
                <span>用户：<strong>{detailInterview.username}</strong></span>
                <span>岗位：<strong>{detailInterview.role}</strong></span>
                <span>时间：<strong>{new Date(detailInterview.created_at).toLocaleString()}</strong></span>
              </div>
              {detailInterview.report ? (
                <>
                  <div className="flex items-center justify-center p-3 bg-blue-50 rounded-lg">
                    <div className="text-center">
                      <div className={`text-3xl font-extrabold ${detailInterview.report.overall_score >= 7 ? 'text-green-600' : detailInterview.report.overall_score >= 4 ? 'text-yellow-600' : 'text-red-600'}`}>
                        {detailInterview.report.overall_score}/10
                      </div>
                      <div className="text-xs text-gray-500">综合得分</div>
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="bg-gray-50 p-2 rounded">
                      <div className="text-xs text-gray-500">表达能力</div>
                      <div className="font-bold">{detailInterview.report.expression_score}/10</div>
                    </div>
                    <div className="bg-gray-50 p-2 rounded">
                      <div className="text-xs text-gray-500">技术深度</div>
                      <div className="font-bold">{detailInterview.report.technical_score}/10</div>
                    </div>
                    <div className="bg-gray-50 p-2 rounded">
                      <div className="text-xs text-gray-500">逻辑思维</div>
                      <div className="font-bold">{detailInterview.report.logic_score}/10</div>
                    </div>
                  </div>
                  {detailInterview.report.details && (
                    <div className="bg-gray-50 p-2 rounded">
                      <p className="text-xs text-gray-500 mb-1">总结</p>
                      <p>{detailInterview.report.details}</p>
                    </div>
                  )}
                  {detailInterview.report.suggestion && (
                    <div className="bg-orange-50 p-2 rounded border border-orange-200">
                      <p className="text-xs text-gray-500 mb-1">改进建议</p>
                      <p className="text-orange-800">{detailInterview.report.suggestion}</p>
                    </div>
                  )}
                </>
              ) : (
                <p className="text-gray-400 text-center py-4">暂无报告数据</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function App() {
  const [messages, setMessages] = useState<Array<{role: string, content: string}>>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [questions, setQuestions] = useState<string[]>([]);
  const [report, setReport] = useState<any>(null);
  const [role, setRole] = useState('后端开发');
  const [enableSpeech, setEnableSpeech] = useState(false);
  const [hasResume, setHasResume] = useState(false);
  const [resumeFullText, setResumeFullText] = useState('');
  const [resumePreview, setResumePreview] = useState('');
  const [resumeFileName, setResumeFileName] = useState('');
  const [resumeError, setResumeError] = useState('');
  const [resumeDragOver, setResumeDragOver] = useState(false);
  const [interviewStarted, setInterviewStarted] = useState(false);
  const [currentQuestionIndex, setCurrentQuestionIndex] = useState(0);
  const [questionStatus, setQuestionStatus] = useState<string[]>([]);
  const [totalQuestions, setTotalQuestions] = useState(0);
  const [interviewFinished, setInterviewFinished] = useState(false);
  const [timeLeft, setTimeLeft] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 题库弹窗
  const [showQuestionBank, setShowQuestionBank] = useState(false);
  const [selectedBankCategory, setSelectedBankCategory] = useState('后端开发');
  const [questionBank, setQuestionBank] = useState<Record<string, { text: string; difficulty: string; tags: string[] }[]>>({});
  const [questionBankLoading, setQuestionBankLoading] = useState(false);
  
  // 用户认证
  const [token, setToken] = useState<string | null>(localStorage.getItem('token'));
  const [_userId, setUserId] = useState<number | null>(null);
  const [userRole, setUserRole] = useState<string | null>(localStorage.getItem('role'));
  const [currentUsername, setCurrentUsername] = useState<string>(localStorage.getItem('username') || '');
  const [showAdminPanel, setShowAdminPanel] = useState(false);
  const [showInfoPanel, setShowInfoPanel] = useState(false);
  const [infoTab, setInfoTab] = useState<'notifications' | 'history'>('notifications');
  const [expandedHistoryId, setExpandedHistoryId] = useState<number | null>(null);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const historyListRef = useRef<HTMLUListElement>(null);
  const [notifications, setNotifications] = useState<any[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [showProfile, setShowProfile] = useState(false);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmNewPassword, setConfirmNewPassword] = useState('');
  const [passwordChangeMsg, setPasswordChangeMsg] = useState('');
  const [passwordChanging, setPasswordChanging] = useState(false);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [_passwordError, setPasswordError] = useState('');
  const [authError, setAuthError] = useState('');
  const [emailError, setEmailError] = useState('');
  const [email, setEmail] = useState('');
  const [agreeTerms, setAgreeTerms] = useState(false);
  const [captchaId, setCaptchaId] = useState('');
  const [captchaImage, setCaptchaImage] = useState('');
  const [captchaCode, setCaptchaCode] = useState('');
  const [captchaLoading, setCaptchaLoading] = useState(false);
  const [authLoading, setAuthLoading] = useState(false);
  const [userProfile, setUserProfile] = useState<any>({});
  const [editProfile, setEditProfile] = useState<any>({});
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [avatarZoom, setAvatarZoom] = useState(1);
  const [history, setHistory] = useState<any[]>([]);
  const [passwordRules, setPasswordRules] = useState({
    length: false,
    kind: false,
    noRepeat: false
  });
  
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const recognitionRef = useRef<any>(null);
  const [isListening, setIsListening] = useState(false);
  const [speechSupported, setSpeechSupported] = useState(true);

  // 自动滚动
  useEffect(() => {
    if (chatContainerRef.current) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  // 语音识别初始化
  useEffect(() => {
    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
      const SpeechRecognition = (window as any).webkitSpeechRecognition || (window as any).SpeechRecognition;
      recognitionRef.current = new SpeechRecognition();
      recognitionRef.current.continuous = false;
      recognitionRef.current.interimResults = false;
      recognitionRef.current.lang = 'zh-CN';
      recognitionRef.current.onresult = (event: any) => {
        const transcript = event.results[0][0].transcript;
        setInput(transcript);
        setIsListening(false);
        setTimeout(() => sendMessage(), 100);
      };
      recognitionRef.current.onerror = () => setIsListening(false);
      recognitionRef.current.onend = () => setIsListening(false);
    } else {
      setSpeechSupported(false);
    }
  }, []);

  // 语音合成
  const speakText = (text: string) => {
    if (!enableSpeech || !window.speechSynthesis) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'zh-CN';
    utterance.rate = 0.9;
    
    const trySetVoice = () => {
      const voices = window.speechSynthesis.getVoices();
      if (voices.length) {
        const preferred = voices.find(v => v.lang.includes('zh-CN') && (v.name.includes('Tingting') || v.name.includes('Huihui') || v.name.includes('Xiaoxiao') || v.name.includes('Google')));
        if (preferred) utterance.voice = preferred;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(utterance);
      } else {
        setTimeout(trySetVoice, 100);
      }
    };
    trySetVoice();
  };

  const startListening = () => {
    if (recognitionRef.current && !isListening) {
      setInput('');
      recognitionRef.current.start();
      setIsListening(true);
    }
  };

  // 发送消息
  const sendMessage = async () => {
    if (!input.trim() || loading || !hasResume || !token) return;

    const lowerInput = input.trim().toLowerCase();
    const skipWords = ['不会', '忘记了', '不知道', '不了解', '没学过', '没接触过', '没经验',
      '太难', '换一个', '换个', '换道', '简单的', '跳过去', '跳过吧', '略过',
      '答不上', '答不出来', '搞不定', '想不起来', '没做过', '换个简单', '下一题', '跳过'];
    const isSkip = skipWords.some(w => lowerInput.includes(w));

    const userMessage = { role: 'user', content: input };
    setMessages(prev => [...prev, userMessage]);
    setInput('');
    setLoading(true);
    try {
      const bodyObj: any = {
        message: input,
        role: role,
        user_id: _userId,
      };
      if (!interviewStarted && messages.length === 0 && resumeFullText) {
        bodyObj.resume_context = resumeFullText;
        bodyObj.action = 'start';
      }
      const res = await authFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(bodyObj)
      });
      const data = await res.json();
      const aiContent = data.reply || '';
      const aiMessage = { role: 'assistant', content: aiContent };
      setMessages(prev => [...prev, aiMessage]);

      if (data.current_index !== undefined) {
        setCurrentQuestionIndex(data.current_index);
        if (!data.finished && isSkip) {
          setQuestionStatus(prev => {
            const cp = [...prev];
            if (cp[currentQuestionIndex]) cp[currentQuestionIndex] = 'skipped';
            return cp;
          });
        } else if (!data.finished) {
          setQuestionStatus(prev => {
            const cp = [...prev];
            if (cp[currentQuestionIndex]) cp[currentQuestionIndex] = 'answered';
            return cp;
          });
        }
      }

      if (data.finished) {
        setInterviewFinished(true);
        setTimeout(() => endInterview(), 1500);
      }

      if (enableSpeech && aiContent) speakText(aiContent);
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logout();
        alert('登录已过期，请重新登录');
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: '网络错误，请稍后重试' }]);
      }
    } finally {
      setLoading(false);
    }
  };

  const skipQuestion = async () => {
    if (loading || !interviewStarted || interviewFinished) return;
    setLoading(true);
    try {
      const res = await authFetch('/api/skip_question', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      });
      const data = await res.json();
      const aiMessage = { role: 'assistant', content: data.reply };
      setMessages(prev => [...prev, { role: 'user', content: '[跳过此题]' }, aiMessage]);
      if (data.current_index !== undefined) {
        setCurrentQuestionIndex(data.current_index);
        setQuestionStatus(prev => {
          const cp = [...prev];
          if (cp[data.current_index - 1]) cp[data.current_index - 1] = 'skipped';
          return cp;
        });
      }
      if (data.finished) {
        setInterviewFinished(true);
        setTimeout(() => endInterview(), 1500);
      }
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logout();
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: '跳过失败，请手动输入"跳过"重试' }]);
      }
    } finally {
      setLoading(false);
    }
  };

  // 上传简历
  const validateFile = (file: File) => {
    const allowed = ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'];
    if (!allowed.includes(file.type)) {
      setResumeError('仅支持 PDF 或 DOCX 格式');
      return false;
    }
    if (file.size > 5 * 1024 * 1024) {
      setResumeError('文件大小不能超过 5MB');
      return false;
    }
    setResumeError('');
    return true;
  };

  const handleResumeFile = async (file: File) => {
    if (!validateFile(file)) return;
    setResumeFileName(file.name);
    setUploading(true);
    const formData = new FormData();
    formData.append('file', file);
    try {
      const res = await authFetch('/api/resume/upload', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setQuestions(data.questions);
        setHasResume(true);
        setResumePreview(data.preview);
        setInterviewStarted(false);
        setCurrentQuestionIndex(0);
        setQuestionStatus([]);
        setTotalQuestions(0);
        setInterviewFinished(false);
        if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
        setTimeLeft(null);
        if (data.full_text) setResumeFullText(data.full_text);
        setResumeError('');
      } else {
        setResumeError(data.detail || data.error || '解析失败');
        setHasResume(false);
      }
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logout();
        setResumeError('登录已过期，请重新登录');
      } else {
        setResumeError('上传失败，请检查网络或后端服务');
      }
    } finally {
      setUploading(false);
    }
  };

  const handleResumeDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setResumeDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) handleResumeFile(file);
  };

  const startInterview = async () => {
    if (questions.length === 0) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        role: role,
        resume_text: resumeFullText,
        questions_json: JSON.stringify(questions),
      });
      const res = await authFetch('/api/start_interview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params.toString()
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setQuestions(data.questions);
        setTotalQuestions(data.total);
        setCurrentQuestionIndex(data.current_index);
        setQuestionStatus(new Array(data.total).fill('pending'));
        setInterviewStarted(true);
        setInterviewFinished(false);
        setMessages([
          { role: 'assistant', content: data.greeting }
        ]);
        setTimeLeft(15 * 60);
        if (timerRef.current) clearInterval(timerRef.current);
        timerRef.current = setInterval(() => {
          setTimeLeft(prev => {
            if (prev === null || prev <= 1) {
              clearInterval(timerRef.current!);
              timerRef.current = null;
              endInterview();
              return 0;
            }
            return prev - 1;
          });
        }, 1000);
      } else {
        setResumeError(data.detail || '开始面试失败');
      }
    } catch (err: any) {
      console.error(err);
      if (err.message === 'Unauthorized') {
        logout();
        setResumeError('登录已过期，请重新登录');
      } else {
        setResumeError('无法连接服务器');
      }
    } finally {
      setLoading(false);
    }
  };

  const endInterview = async () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setTimeLeft(null);
    if (messages.length === 0) {
      alert('还没有任何对话，无法生成报告');
      return;
    }
    setLoading(true);
    const currentMessages = messages;
    const currentRole = role;
    try {
      const res = await authFetch('/api/generate_report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: currentMessages, user_id: _userId })
      });
      const data = await res.json();
      setReport(data);
      setMessages([]);
      setHasResume(false);
      setInterviewStarted(false);
      setCurrentQuestionIndex(0);
      setQuestionStatus([]);
      setTotalQuestions(0);
      setInterviewFinished(false);
      setQuestions([]);
      setResumeFullText('');
      setResumePreview('');
      setResumeFileName('');
      setInput('');

      if (data && data.expression_score !== undefined) {
        try {
          const saveRes = await authFetch('/api/save_interview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: currentRole, messages: currentMessages, report: data })
          });
          if (!saveRes.ok) {
            console.error('保存面试记录失败，HTTP', saveRes.status);
          } else {
            await loadHistory(token || localStorage.getItem('token') || '');
            setShowInfoPanel(true);
            setInfoTab('history');
          }
        } catch (saveErr) {
          console.error('保存面试记录异常', saveErr);
        }
      }
    } catch (err: any) {
      console.error(err);
      alert('生成报告失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  // 登录/注册
  const loadCaptcha = async () => {
    setCaptchaLoading(true);
    setCaptchaCode('');
    try {
      const res = await fetch(`${API_BASE_URL}/api/captcha`);
      if (!res.ok) {
        console.error('验证码加载失败 HTTP', res.status);
        return;
      }
      const id = res.headers.get('X-Captcha-Id');
      if (id) setCaptchaId(id);
      const blob = await res.blob();
      if (captchaImage) URL.revokeObjectURL(captchaImage);
      setCaptchaImage(URL.createObjectURL(blob));
    } catch (err) {
      console.error('验证码加载失败', err);
    } finally {
      setCaptchaLoading(false);
    }
  };

  const validateEmail = (email: string) => {
    return /^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$/.test(email);
  };

  const handleAuth = async () => {
    setAuthError('');
    setEmailError('');
    if (!captchaCode) {
      setAuthError('请输入验证码');
      return;
    }
    if (authMode === 'register') {
      const usernamePattern = /^[\u4e00-\u9fa5a-zA-Z0-9_]{3,16}$/;
      if (!usernamePattern.test(username)) {
        setAuthError('用户名必须为3-16位字母、数字、下划线或中文');
        return;
      }
      if (!email) {
        setEmailError('邮箱不能为空');
        return;
      }
      if (!validateEmail(email)) {
        setEmailError('邮箱格式不正确');
        return;
      }
      const pwd = password;
      const lenOk = pwd.length >= 8 && pwd.length <= 16;
      const hasLetter = /[A-Za-z]/.test(pwd);
      const hasDigit = /\d/.test(pwd);
      const hasSymbol = /[!@#$%^&*()_+\-=[\]{};':"\\|,.<>/?]/.test(pwd);
      const kindOk = [hasLetter, hasDigit, hasSymbol].filter(Boolean).length >= 2;
      const repeatOk = !/(.)\1{5,}/.test(pwd) &&
                        !/012345|123456|234567|345678|456789|567890/.test(pwd) &&
                        !/abcdef|bcdefg|cdefgh|defghi|efghij|fghijk/.test(pwd);
      if (!lenOk) { setAuthError('密码长度应为8-16位'); return; }
      if (!kindOk) { setAuthError('密码必须包含字母、数字、符号中至少2种'); return; }
      if (!repeatOk) { setAuthError('请勿输入连续、重复位以上字母或数字'); return; }
      if (password !== confirmPassword) { setAuthError('两次输入的密码不一致'); return; }
      if (!agreeTerms) { setAuthError('请先阅读并同意用户协议'); return; }
    }
    setAuthLoading(true);
    try {
      const url = authMode === 'login' ? `${API_BASE_URL}/api/login` : `${API_BASE_URL}/api/register`;
      const params = authMode === 'login'
        ? new URLSearchParams({ username, password, captcha_id: captchaId, captcha_code: captchaCode })
        : new URLSearchParams({ username, password, email, captcha_id: captchaId, captcha_code: captchaCode });
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params.toString()
      });
      const data = await res.json();
      if (res.ok) {
        if (authMode === 'login') {
          setToken(data.access_token);
          setUserId(data.user_id);
          setUserRole(data.role);
          setCurrentUsername(username);
          localStorage.setItem('token', data.access_token);
          localStorage.setItem('role', data.role);
          localStorage.setItem('username', username);
          setShowAuthModal(false);
          setUsername('');
          setPassword('');
          setCaptchaCode('');
          setCaptchaId('');
          setEmail('');
          setEmailError('');
          if (data.role !== 'admin') {
            loadHistory(data.access_token);
            loadUnreadCount(data.access_token);
          } else {
            setHistory([]);
            setUnreadCount(0);
            setNotifications([]);
            setShowInfoPanel(false);
          }
          loadUserProfile(data.access_token);
        } else {
          setAuthMode('login');
          setAuthError('注册成功，请登录');
          setEmail('');
          setAgreeTerms(false);
        }
      } else {
        setAuthError(data.detail || '操作失败');
        loadCaptcha();
      }
    } catch (err) {
      setAuthError('网络错误');
      loadCaptcha();
    } finally {
      setAuthLoading(false);
    }
  };

  const loadHistory = async (tok: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/history`, {
        headers: { 'Authorization': `Bearer ${tok}` }
      });
      if (res.ok) {
        const data = await res.json();
        setHistory(data);
      }
    } catch (err) {
      console.error(err);
    }
  };

  const loadNotifications = async (tok: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/notifications`, {
        headers: { 'Authorization': `Bearer ${tok}` }
      });
      if (res.ok) {
        const data = await res.json();
        setNotifications(data);
        setUnreadCount(data.filter((n: any) => !n.is_read).length);
      }
    } catch (err) {
      console.error(err);
    }
  };

  const loadUserProfile = async (tok: string) => {
    try {
      const [profileRes, statsRes] = await Promise.all([
        fetch(`${API_BASE_URL}/api/user/profile`, {
          headers: { 'Authorization': `Bearer ${tok}` }
        }),
        fetch(`${API_BASE_URL}/api/user/stats`, {
          headers: { 'Authorization': `Bearer ${tok}` }
        })
      ]);
      if (profileRes.ok && statsRes.ok) {
        const profile = await profileRes.json();
        const stats = await statsRes.json();
        setUserProfile({ ...profile, ...stats });
      }
    } catch (err) {
      console.error(err);
    }
  };

  const loadUnreadCount = async (tok: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/notifications/unread_count`, {
        headers: { 'Authorization': `Bearer ${tok}` }
      });
      if (res.ok) {
        const data = await res.json();
        setUnreadCount(data.count);
      }
    } catch (err) {
      console.error(err);
    }
  };

  const markAllRead = async () => {
    if (!token) return;
    try {
      await fetch(`${API_BASE_URL}/api/notifications/read_all`, {
        method: 'PATCH',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      setUnreadCount(0);
      setNotifications(notifications.map(n => ({ ...n, is_read: true })));
    } catch (err) {
      console.error(err);
    }
  };

  const deleteNotification = async (id: number) => {
    if (!token) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/notifications/${id}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        setNotifications(notifications.filter(n => n.id !== id));
        loadUnreadCount(token);
      }
    } catch (err) {
      console.error(err);
    }
  };

  const clearAllNotifications = async () => {
    if (!token) return;
    try {
      await fetch(`${API_BASE_URL}/api/notifications/clear_all`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      setNotifications([]);
      setUnreadCount(0);
    } catch (err) {
      console.error(err);
    }
  };

  const handleNotificationClick = async (n: any) => {
    if (!token) return;
    if (!n.is_read) {
      try {
        await fetch(`${API_BASE_URL}/api/notifications/${n.id}/read`, {
          method: 'PATCH',
          headers: { 'Authorization': `Bearer ${token}` }
        });
        setNotifications(notifications.map(item => item.id === n.id ? { ...item, is_read: true } : item));
        loadUnreadCount(token);
      } catch (err) {
        console.error(err);
      }
    }
    if (n.target_type === 'interview_record' && n.target_id) {
      try {
        const res = await fetch(`${API_BASE_URL}/api/history_item/${n.target_id}`, {
          headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.status === 404) {
          alert('该面试记录已不存在');
          setInfoTab('history');
          return;
        }
        if (res.status === 403) {
          alert('无权限查看该记录');
          setInfoTab('history');
          return;
        }
        if (res.ok) {
          const record = await res.json();
          const exists = history.some(h => h.id === record.id);
          if (!exists) {
            setHistory(prev => [record, ...prev]);
          }
          setHighlightId(record.id);
          setInfoTab('history');
        }
      } catch (err) {
        console.error(err);
        alert('网络错误，请稍后重试');
      }
    } else {
      setInfoTab('history');
    }
  };

  useEffect(() => {
    if (highlightId !== null) {
      const timer = setTimeout(() => setHighlightId(null), 3000);
      let attempts = 0;
      const maxAttempts = 20;
      const tryScroll = () => {
        const el = document.getElementById(`record-${highlightId}`);
        if (el) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else if (++attempts < maxAttempts) {
          setTimeout(tryScroll, 50);
        }
      };
      setTimeout(tryScroll, 100);
      return () => {
        clearTimeout(timer);
      };
    }
  }, [highlightId]);

  const handleChangePassword = async () => {
    if (!token) return;

    setPasswordChangeMsg('');

    if (!oldPassword) {
      setPasswordChangeMsg('请输入原密码');
      return;
    }
    if (!newPassword) {
      setPasswordChangeMsg('请输入新密码');
      return;
    }
    if (newPassword.length < 6) {
      setPasswordChangeMsg('新密码长度至少6位');
      return;
    }
    if (newPassword !== confirmNewPassword) {
      setPasswordChangeMsg('两次输入的新密码不一致');
      return;
    }
    if (newPassword === oldPassword) {
      setPasswordChangeMsg('新密码不能和原密码相同');
      return;
    }

    setPasswordChanging(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/change_password`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Authorization': `Bearer ${token}`
        },
        body: new URLSearchParams({
          old_password: oldPassword,
          new_password: newPassword
        }).toString()
      });

      const data = await res.json();

      if (res.ok) {
        setPasswordChangeMsg('密码修改成功，请重新登录');
        setOldPassword('');
        setNewPassword('');
        setConfirmNewPassword('');
        setShowProfile(false);
        setTimeout(() => logout(), 2000);
      } else {
        setPasswordChangeMsg(data.detail || '修改失败');
      }
    } catch (err) {
      console.error(err);
      setPasswordChangeMsg('网络错误，请稍后重试');
    } finally {
      setPasswordChanging(false);
    }
  };

  const logout = () => {
    setToken(null);
    setUserId(null);
    setUserRole(null);
    setCurrentUsername('');
    localStorage.removeItem('token');
    localStorage.removeItem('role');
    localStorage.removeItem('username');
    setHistory([]);
    setMessages([]);
    setReport(null);
    setHasResume(false);
    setInterviewStarted(false);
    setCurrentQuestionIndex(0);
    setQuestionStatus([]);
    setTotalQuestions(0);
    setInterviewFinished(false);
    setTimeLeft(null);
    setQuestions([]);
    setResumeFullText('');
    setResumePreview('');
    setResumeFileName('');
    setResumeError('');
    setUnreadCount(0);
    setNotifications([]);
    setShowInfoPanel(false);
    setShowProfile(false);
  };

  useEffect(() => {
    const validateToken = async () => {
      const savedToken = localStorage.getItem('token');
      if (savedToken) {
        try {
          const res = await fetch(`${API_BASE_URL}/api/user/profile`, {
            headers: { 'Authorization': `Bearer ${savedToken}` }
          });
          if (!res.ok && res.status === 401) {
            logout();
          }
        } catch {
          logout();
        }
      }
    };
    validateToken();
  }, []);

  useEffect(() => {
    if (token) {
      if (userRole === 'admin') {
        setHistory([]);
        setUnreadCount(0);
        setNotifications([]);
      } else {
        loadHistory(token);
        loadUnreadCount(token);
      }
      loadUserProfile(token);
    }
  }, [token, userRole]);

  useEffect(() => {
    if (token && userRole === 'admin') {
      setShowAdminPanel(true);
    }
  }, [token, userRole]);

  useEffect(() => {
    if (!token) return;
    if (userRole === 'admin') {
      setUnreadCount(0);
      setNotifications([]);
      return;
    }
    const interval = setInterval(() => {
      loadUnreadCount(token);
    }, 30000);
    return () => clearInterval(interval);
  }, [token, userRole]);

  useEffect(() => {
    if (token && showInfoPanel && userRole !== 'admin') {
      loadNotifications(token);
    }
  }, [token, showInfoPanel, userRole]);

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-slate-50">
      {/* 顶部导航 */}
      <header className="sticky top-0 z-40 bg-white/80 backdrop-blur-xl border-b border-slate-200/60 shadow-sm">
        <div className="max-w-4xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center shadow-md shadow-primary-200">
              <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
            </div>
            <h1 className="text-lg font-bold bg-gradient-to-r from-primary-700 to-primary-500 bg-clip-text text-transparent">AI 智能面试模拟</h1>
          </div>
          <nav className="flex items-center gap-1">
            <button onClick={async () => {
              setShowQuestionBank(true);
              setQuestionBankLoading(true);
              try {
                const res = await fetch(`${API_BASE_URL}/api/question_bank`);
                const data = await res.json();
                setQuestionBank(data);
              } catch (err) {
                console.error(err);
              } finally {
                setQuestionBankLoading(false);
              }
            }} className="btn-ghost text-sm flex items-center gap-1.5">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
              <span>题库</span>
            </button>
            {token ? (
              <>
                {userRole === 'admin' && (
                  <button onClick={() => setShowAdminPanel(true)} className="btn-ghost text-sm flex items-center gap-1.5 text-primary-600">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
                    <span>管理</span>
                  </button>
                )}
                {userRole !== 'admin' && (
                  <button onClick={() => { setInfoTab('notifications'); setShowInfoPanel(!showInfoPanel); }} className="btn-ghost text-sm flex items-center gap-1.5 relative">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" /></svg>
                    <span>消息</span>
                    {unreadCount > 0 && <span className="absolute -top-0.5 -right-0.5 bg-red-500 text-white text-[10px] font-bold rounded-full min-w-[18px] h-[18px] flex items-center justify-center px-1">{unreadCount > 99 ? '99+' : unreadCount}</span>}
                  </button>
                )}
                <button onClick={() => { setEditProfile({ ...userProfile }); setPasswordChangeMsg(''); setOldPassword(''); setNewPassword(''); setConfirmNewPassword(''); setShowProfile(true); }} className="btn-ghost text-sm flex items-center gap-1.5">
                  <div className="w-6 h-6 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-xs font-semibold">
                    {currentUsername?.charAt(0)?.toUpperCase() || '?'}
                  </div>
                  <span>{currentUsername || '个人'}</span>
                </button>
                <button onClick={logout} className="btn-ghost text-sm flex items-center gap-1.5 text-red-500 hover:text-red-600 hover:bg-red-50">
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
                  <span>退出</span>
                </button>
              </>
            ) : (
              <button onClick={() => { setShowAuthModal(true); loadCaptcha(); }} className="btn-primary text-sm px-4 py-2">
                登录 / 注册
              </button>
            )}
          </nav>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-6 animate-fade-in">

      {/* 消息中心 - 通知 + 面试历史 */}
      {showInfoPanel && token && (
        <div className="mb-4 border rounded bg-white shadow">
          <div className="flex border-b">
            <button
              onClick={() => setInfoTab('notifications')}
              className={`flex-1 py-2.5 text-sm font-medium transition ${infoTab === 'notifications' ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50' : 'text-gray-500 hover:text-gray-700'}`}
            >
              🔔 通知 {unreadCount > 0 && <span className="inline-flex items-center justify-center bg-red-500 text-white text-xs rounded-full w-5 h-5 ml-1">{unreadCount}</span>}
            </button>
            <button
              onClick={() => setInfoTab('history')}
              className={`flex-1 py-2.5 text-sm font-medium transition ${infoTab === 'history' ? 'text-blue-600 border-b-2 border-blue-600 bg-blue-50' : 'text-gray-500 hover:text-gray-700'}`}
            >
              📋 面试历史
            </button>
          </div>

          {infoTab === 'notifications' && (
            <div className="p-3">
              <div className="flex justify-between items-center mb-2">
                <span className="text-xs text-gray-400">即时提醒</span>
                <div className="flex gap-2 text-xs">
                  {unreadCount > 0 && <button onClick={markAllRead} className="text-blue-500 underline">全部已读</button>}
                  {notifications.length > 0 && <button onClick={clearAllNotifications} className="text-red-400 underline">清空全部</button>}
                </div>
              </div>
              {notifications.length === 0 && <p className="text-center py-6 text-gray-400 text-sm">暂无通知，一切安好 🎉</p>}
              <ul className="space-y-1 max-h-64 overflow-y-auto">
                {notifications.map((n) => {
                  const iconMap: Record<string, string> = {
                    interview_approved: '✅',
                    interview_rejected: '❌',
                    interview_pending: '⏳',
                    new_comment: '💬',
                    system: '🔔',
                  };
                  return (
                    <li
                      key={n.id}
                      onClick={() => handleNotificationClick(n)}
                      className={`p-3 rounded-xl cursor-pointer flex items-start gap-3 transition-all duration-200 border ${n.is_read ? 'bg-white border-slate-100' : 'bg-primary-50/60 border-primary-100 font-medium'} hover:shadow-sm`}
                    >
                      <span className="text-lg mt-0.5 shrink-0">{iconMap[n.type] || '🔔'}</span>
                      <div className="flex-1 min-w-0">
                        <p className={`text-sm leading-relaxed ${n.is_read ? 'text-slate-600' : 'text-slate-900'}`}>{n.message}</p>
                        <p className="text-xs text-slate-400 mt-1">{new Date(n.created_at).toLocaleString()}</p>
                      </div>
                      <button
                        onClick={(e) => { e.stopPropagation(); deleteNotification(n.id); }}
                        className="text-slate-300 hover:text-red-500 text-sm shrink-0 p-1 hover:bg-red-50 rounded-lg transition-colors"
                        title="删除"
                      >✕</button>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {infoTab === 'history' && (
        <div className="p-3">
          <span className="text-xs text-slate-400 font-medium">面试记录</span>
          {history.length === 0 && <p className="text-center py-6 text-slate-400 text-sm">暂无面试记录</p>}
          <ul ref={historyListRef} className="space-y-2 mt-2 max-h-80 overflow-y-auto scroll-smooth">
            {history.map((h) => (
              <li
                key={h.id}
                id={`record-${h.id}`}
                onClick={() => setExpandedHistoryId(expandedHistoryId === h.id ? null : h.id)}
                className={`rounded-xl p-3 cursor-pointer transition-all duration-300 border ${highlightId === h.id ? 'bg-amber-50 border-amber-400 ring-2 ring-amber-300 scale-[1.02] shadow-md' : 'bg-white border-slate-100 hover:border-slate-200 hover:shadow-sm'} ${expandedHistoryId === h.id ? 'ring-2 ring-primary-300 border-primary-200 shadow-md' : ''}`}
              >
                <div className="flex justify-between items-start mb-1.5">
                  <div className="flex items-center gap-2">
                    <span className="w-6 h-6 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-[10px] font-bold">AI</span>
                    <p className="font-semibold text-sm text-slate-800">{h.role}</p>
                  </div>
                  <span className="text-xs text-slate-400">{new Date(h.created_at).toLocaleDateString()}</span>
                </div>
                <div className="flex items-center gap-3 text-sm">
                  <span className={`font-bold text-sm ${h.report.overall_score >= 7 ? 'text-emerald-600' : h.report.overall_score >= 4 ? 'text-amber-600' : 'text-red-600'}`}>
                    得分 {h.report.overall_score}/10
                  </span>
                  {h.status && (
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${h.status === 'approved' ? 'bg-emerald-100 text-emerald-700' : h.status === 'rejected' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}`}>
                      {h.status === 'approved' ? '已通过' : h.status === 'rejected' ? '未通过' : '待审核'}
                    </span>
                  )}
                  <span className="text-xs text-slate-400 ml-auto">{expandedHistoryId === h.id ? '收起 ▲' : '展开 ▼'}</span>
                </div>
                {expandedHistoryId === h.id && (
                  <div className="mt-3 pt-3 border-t border-slate-100 text-xs space-y-2">
                    <div className="flex gap-4">
                      <span className="text-slate-500">表达能力 <span className="font-bold text-slate-800">{h.report.expression_score}/10</span></span>
                      <span className="text-slate-500">技术深度 <span className="font-bold text-slate-800">{h.report.technical_score}/10</span></span>
                      <span className="text-slate-500">逻辑思维 <span className="font-bold text-slate-800">{h.report.logic_score}/10</span></span>
                    </div>
                    {h.report.details && <p className="text-slate-600 bg-slate-50 p-3 rounded-xl border border-slate-100 leading-relaxed">📋 {h.report.details}</p>}
                    {h.report.suggestion && <p className="text-amber-700 bg-amber-50 p-3 rounded-xl border border-amber-200 leading-relaxed">💡 {h.report.suggestion}</p>}
                    {h.admin_comment && <p className="text-blue-700 bg-blue-50 p-3 rounded-xl border border-blue-200 leading-relaxed">✏ 管理员评语：{h.admin_comment}</p>}
                  </div>
                )}
                {expandedHistoryId !== h.id && h.admin_comment && (
                  <p className="text-xs text-slate-400 mt-1.5 truncate">评语：{h.admin_comment}</p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
        </div>
      )}

      {/* 个人中心弹窗 */}
      {showProfile && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-11/12 max-w-md max-h-[90vh] overflow-auto">
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-xl font-bold">👤 个人中心</h2>
              <button onClick={() => setShowProfile(false)} className="text-gray-500 text-xl">×</button>
            </div>
            <div className="space-y-4">
              <div className="text-center">
                <div className="w-20 h-20 mx-auto rounded-full bg-gray-200 flex items-center justify-center text-3xl overflow-hidden">
                  {userProfile.avatar ? (
                    <img src={`${API_BASE_URL}${userProfile.avatar}`} alt="avatar" className="w-full h-full object-cover" />
                  ) : (
                    '👤'
                  )}
                </div>
                <label className="mt-2 text-blue-500 text-sm cursor-pointer underline">
                  <input type="file" accept="image/*" className="hidden" onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (!file) return;
                    if (file.size > 2 * 1024 * 1024) { alert('图片大小不能超过 2MB'); return; }
                    setAvatarFile(file);
                    setAvatarZoom(1);
                    setAvatarPreview(URL.createObjectURL(file));
                  }} />
                  上传头像
                </label>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">用户名</label>
                <input type="text" value={currentUsername} disabled className="w-full border rounded p-2 bg-gray-100" />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">昵称</label>
                <input type="text" value={editProfile.nickname || ''} onChange={e => setEditProfile({ ...editProfile, nickname: e.target.value })} className="w-full border rounded p-2" placeholder="设置昵称" />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">性别</label>
                <select value={editProfile.gender || ''} onChange={e => setEditProfile({ ...editProfile, gender: e.target.value })} className="w-full border rounded p-2">
                  <option value="">未设置</option>
                  <option value="male">男</option>
                  <option value="female">女</option>
                  <option value="other">其他</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">生日</label>
                <input type="date" value={editProfile.birthday || ''} onChange={e => setEditProfile({ ...editProfile, birthday: e.target.value })} className="w-full border rounded p-2" />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">个人简介</label>
                <textarea value={editProfile.bio || ''} onChange={e => setEditProfile({ ...editProfile, bio: e.target.value })} className="w-full border rounded p-2" rows={3} placeholder="介绍一下自己" />
              </div>
              <button onClick={async () => {
                if (!token) return;
                const res = await authFetch('/api/user/profile', {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({
                    nickname: editProfile.nickname,
                    bio: editProfile.bio,
                    gender: editProfile.gender,
                    birthday: editProfile.birthday || null
                  })
                });
                if (res.ok) {
                  setUserProfile({ ...editProfile, total_interviews: userProfile.total_interviews, avg_score: userProfile.avg_score });
                  setShowProfile(false);
                  alert('资料更新成功');
                } else {
                  const err = await res.json();
                  alert(err.detail || '更新失败');
                }
              }} className="w-full bg-blue-500 text-white rounded p-2 hover:bg-blue-600">保存资料</button>
              <div className="border-t pt-4">
                <h3 className="font-bold mb-2">面试统计</h3>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div className="bg-gray-50 p-2 rounded">面试次数</div>
                  <div className="bg-gray-50 p-2 rounded text-center">{userProfile.total_interviews || 0}</div>
                  <div className="bg-gray-50 p-2 rounded">平均得分</div>
                  <div className="bg-gray-50 p-2 rounded text-center">{userProfile.avg_score || '-'}</div>
                </div>
              </div>
              <div className="border-t pt-4">
                <h3 className="font-bold mb-2">修改密码</h3>
                <div className="space-y-2">
                  <input type="password" placeholder="原密码" value={oldPassword} onChange={e => setOldPassword(e.target.value)} className="w-full border rounded p-2" />
                  <input type="password" placeholder="新密码" value={newPassword} onChange={e => setNewPassword(e.target.value)} className="w-full border rounded p-2" />
                  <input type="password" placeholder="确认新密码" value={confirmNewPassword} onChange={e => setConfirmNewPassword(e.target.value)} className="w-full border rounded p-2" />
                  {passwordChangeMsg && <p className={`text-sm ${passwordChangeMsg.includes('成功') ? 'text-green-600' : 'text-red-600'}`}>{passwordChangeMsg}</p>}
                  <button onClick={handleChangePassword} disabled={passwordChanging} className="w-full bg-blue-500 text-white rounded p-2 hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed">{passwordChanging ? '修改中...' : '确认修改'}</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 头像裁剪弹窗 */}
      {avatarPreview && avatarFile && (
        <div className="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-[60]">
          <div className="bg-white rounded-lg p-6 w-11/12 max-w-sm">
            <h3 className="text-lg font-bold mb-3">调整头像</h3>
            <div className="w-48 h-48 mx-auto rounded-full overflow-hidden bg-gray-200 mb-4">
              <img
                src={avatarPreview}
                alt="preview"
                className="w-full h-full object-cover"
                style={{ transform: `scale(${avatarZoom})` }}
              />
            </div>
            <div className="flex items-center gap-3 mb-4">
              <span className="text-sm text-gray-500">缩放</span>
              <input
                type="range"
                min="0.5"
                max="2"
                step="0.1"
                value={avatarZoom}
                onChange={e => setAvatarZoom(parseFloat(e.target.value))}
                className="flex-1"
              />
              <span className="text-sm w-8 text-right">{avatarZoom}x</span>
            </div>
            <div className="flex gap-3">
              <button
                onClick={() => { setAvatarPreview(null); setAvatarFile(null); }}
                className="flex-1 border rounded p-2 hover:bg-gray-50"
              >
                取消
              </button>
              <button
                onClick={async () => {
                  if (!avatarFile || !token) return;
                  const canvas = document.createElement('canvas');
                  const size = 200;
                  canvas.width = size;
                  canvas.height = size;
                  const ctx = canvas.getContext('2d');
                  if (!ctx) return;
                  const img = new Image();
                  img.src = avatarPreview;
                  await new Promise(resolve => { img.onload = resolve; });
                  const sx = (img.width - img.width / avatarZoom) / 2;
                  const sy = (img.height - img.height / avatarZoom) / 2;
                  const sw = img.width / avatarZoom;
                  const sh = img.height / avatarZoom;
                  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, size, size);
                  const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.85));
                  if (!blob) return;
                  const formData = new FormData();
                  formData.append('file', blob, 'avatar.jpg');
                  try {
                    const res = await authFetch('/api/user/avatar', {
                      method: 'POST',
                      body: formData
                    });
                    if (res.ok) {
                      const data = await res.json();
                      setUserProfile((prev: any) => ({ ...prev, avatar: data.avatar_url }));
                      setEditProfile((prev: any) => ({ ...prev, avatar: data.avatar_url }));
                    } else {
                      const err = await res.json();
                      alert(err.detail || '上传失败');
                    }
                  } catch (err) {
                    console.error(err);
                    alert('上传失败');
                  }
                  setAvatarPreview(null);
                  setAvatarFile(null);
                }}
                className="flex-1 bg-blue-500 text-white rounded p-2 hover:bg-blue-600"
              >
                确认上传
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 管理员面板*/}
      {showAdminPanel && userRole === 'admin' && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setShowAdminPanel(false)}>
          <div className="bg-white rounded-lg p-6 w-11/12 max-w-4xl max-h-[90vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-2xl font-bold">⚙️ 管理后台</h2>
              <button onClick={() => setShowAdminPanel(false)} className="text-gray-500 text-xl">X</button>
            </div>
            <AdminPanelContent token={token} />
          </div>
        </div>
      )}

      {/* 未登录提示 */}
      {!token && (
        <div className="animate-slide-up">
          <div className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-primary-600 via-primary-700 to-indigo-800 p-8 md:p-12 mb-8 shadow-float">
            <div className="absolute inset-0 bg-[url('data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjAiIGhlaWdodD0iNjAiIHZpZXdCb3g9IjAgMCA2MCA2MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48ZyBmaWxsPSJub25lIiBmaWxsLXJ1bGU9ImV2ZW5vZGQiPjxnIGZpbGw9IiNmZmYiIGZpbGwtb3BhY2l0eT0iMC4wNSI+PGNpcmNsZSBjeD0iMzAiIGN5PSIzMCIgcj0iMiIvPjwvZz48L2c+PC9zdmc+')] opacity-50"></div>
            <div className="relative z-10 flex flex-col md:flex-row items-center gap-6">
              <div className="flex-1 text-white">
                <span className="badge bg-white/20 text-white border-white/30 mb-3">AI-Powered</span>
                <h2 className="text-2xl md:text-3xl font-extrabold mb-3 leading-tight">准备好迎接下一次面试了吗？</h2>
                <p className="text-primary-100 text-sm md:text-base leading-relaxed max-w-lg">
                  上传简历，AI 面试官将自动生成针对性问题，模拟真实面试场景，并提供专业评估报告。
                </p>
                <div className="flex gap-3 mt-5">
                  <button onClick={() => { setShowAuthModal(true); loadCaptcha(); }} className="bg-white text-primary-700 font-semibold px-5 py-2.5 rounded-xl shadow-md hover:shadow-lg hover:scale-[1.02] active:scale-[0.98] transition-all duration-200 text-sm">
                    免费开始 →
                  </button>
                  <button onClick={async () => {
                    setShowQuestionBank(true);
                    setQuestionBankLoading(true);
                    try {
                      const res = await fetch(`${API_BASE_URL}/api/question_bank`);
                      const data = await res.json();
                      setQuestionBank(data);
                    } catch (err) {
                      console.error(err);
                    } finally {
                      setQuestionBankLoading(false);
                    }
                  }} className="bg-white/10 text-white border border-white/20 font-semibold px-5 py-2.5 rounded-xl hover:bg-white/20 transition-all duration-200 text-sm">
                    浏览题库
                  </button>
                </div>
              </div>
              <div className="hidden md:flex items-center justify-center">
                <div className="w-44 h-44 rounded-3xl bg-white/10 backdrop-blur-xl border border-white/20 flex items-center justify-center animate-float shadow-2xl">
                  <svg className="w-20 h-20 text-white/80" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>
                </div>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            {[
              { icon: '📄', title: '简历智能解析', desc: '支持 PDF/DOCX 格式，自动提取关键信息' },
              { icon: '🤖', title: 'AI 面试模拟', desc: 'DeepSeek 驱动的真实面试场景对话' },
              { icon: '📊', title: '专业评估报告', desc: '多维度评分 + 改进建议，助力成长' },
            ].map((f, i) => (
              <div key={i} className="card text-center animate-slide-up" style={{ animationDelay: `${i * 0.1}s` }}>
                <div className="text-3xl mb-3">{f.icon}</div>
                <h3 className="font-semibold text-slate-800 mb-1">{f.title}</h3>
                <p className="text-xs text-slate-500 leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 管理员提示卡 */}
      {token && userRole === 'admin' && (
        <div className="card border-primary-200 bg-gradient-to-r from-primary-50 to-indigo-50 mb-6 animate-slide-up">
          <div className="flex items-start gap-4">
            <div className="w-10 h-10 rounded-xl bg-primary-100 flex items-center justify-center text-xl shrink-0">🛡️</div>
            <div className="flex-1">
              <h3 className="font-bold text-primary-800 mb-1">管理员模式</h3>
              <p className="text-sm text-primary-600">您当前处于管理员账户，不能进行面试。请使用管理后台管理用户和面试记录。</p>
            </div>
            <button onClick={() => setShowAdminPanel(true)} className="btn-primary text-sm px-4 py-2 shrink-0">
              打开管理后台
            </button>
          </div>
        </div>
      )}

      {/* 登录后的功能区域 */}
      {token && userRole !== 'admin' && (
        <>
          {report ? (
            /* 报告展示 */
            <div className="animate-scale-in">
              <div className="card p-0 overflow-hidden shadow-float">
                <div className="bg-gradient-to-r from-primary-600 to-indigo-700 px-6 py-5 text-white">
                  <div className="flex justify-between items-center mb-1">
                    <h3 className="font-bold text-lg">面试评估报告</h3>
                <button
                  onClick={() => {
                    const content = `面试评估报告\n\n` +
                      `综合得分：${report.overall_score}/10\n` +
                      `表达能力：${report.expression_score}/10\n` +
                      `技术深度：${report.technical_score}/10\n` +
                      `逻辑思维：${report.logic_score}/10\n` +
                      `已回答：${report.answered_count || 0}/${report.total_questions} 个问题\n\n` +
                      `总结：${report.details}\n\n` +
                      `改进建议：${report.suggestion}`;
                    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `面试报告_${new Date().toLocaleDateString()}.txt`;
                    a.click();
                    URL.revokeObjectURL(url);
                  }}
                    className="text-white/80 hover:text-white text-xs font-medium flex items-center gap-1 bg-white/10 rounded-lg px-3 py-1.5 hover:bg-white/20 transition-colors">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
                      导出 TXT
                    </button>
                  </div>
                  <p className="text-primary-100 text-xs">AI 面试官根据对话内容生成的详细评估</p>
                </div>
                <div className="p-6">
                  <div className="flex items-center justify-center mb-6">
                    <div className={`w-28 h-28 rounded-full flex items-center justify-center border-4 ${report.overall_score >= 7 ? 'border-emerald-300 bg-emerald-50' : report.overall_score >= 4 ? 'border-amber-300 bg-amber-50' : 'border-red-300 bg-red-50'}`}>
                      <div className="text-center">
                        <div className={`text-3xl font-extrabold ${report.overall_score >= 7 ? 'text-emerald-600' : report.overall_score >= 4 ? 'text-amber-600' : 'text-red-600'}`}>
                          {report.overall_score}
                        </div>
                        <div className="text-xs text-slate-400 font-medium">/ 10</div>
                      </div>
                    </div>
                  </div>
                  <div className="space-y-3 mb-6">
                {[
                  { label: '表达能力', score: report.expression_score },
                  { label: '技术深度', score: report.technical_score },
                  { label: '逻辑思维', score: report.logic_score },
                ].map(item => {
                  const pct = (item.score / 10) * 100;
                  const barColor = item.score >= 7 ? 'bg-gradient-to-r from-emerald-500 to-emerald-400' : item.score >= 4 ? 'bg-gradient-to-r from-amber-500 to-amber-400' : 'bg-gradient-to-r from-red-500 to-red-400';
                  return (
                    <div key={item.label} className="flex items-center gap-3">
                      <span className="text-xs text-slate-500 font-medium w-16 text-right">{item.label}</span>
                      <div className="flex-1 h-3 bg-slate-100 rounded-full overflow-hidden">
                        <div className={`h-full ${barColor} rounded-full transition-all duration-700 ease-out`} style={{ width: `${pct}%` }} />
                      </div>
                      <span className={`text-sm font-bold w-10 ${item.score >= 7 ? 'text-emerald-600' : item.score >= 4 ? 'text-amber-600' : 'text-red-600'}`}>
                        {item.score}/10
                      </span>
                    </div>
                  );
                })}
              </div>

              {report.total_questions !== undefined && (
                <p className="text-center text-xs text-slate-400 mb-4">
                  回答了 <span className="font-semibold text-primary-600">{report.answered_count || 0}</span> / {report.total_questions} 个问题
                </p>
              )}
              <div className="space-y-3">
                <div className="bg-slate-50 rounded-xl p-4">
                  <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">总结</h4>
                  <p className="text-sm text-slate-700 leading-relaxed">{report.details}</p>
                </div>
                <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
                  <h4 className="text-xs font-semibold text-amber-700 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
                    改进建议
                  </h4>
                  <p className="text-sm text-amber-800 leading-relaxed">{report.suggestion}</p>
                </div>
              </div>
              <button onClick={() => {
                setReport(null);
                setInterviewStarted(false);
                setCurrentQuestionIndex(0);
                setQuestionStatus([]);
                setTotalQuestions(0);
                setInterviewFinished(false);
                setTimeLeft(null);
                setResumePreview('');
                setResumeFileName('');
                setResumeError('');
              }} className="btn-primary w-full mt-5 py-3 text-sm flex items-center justify-center gap-2">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                开始新面试（需重新上传简历）
              </button>
                </div>
              </div>
            </div>
          ) : (
            /* 面试进行中 */
            <>
              {/* 简历上传 */}
              <div className="card mb-5 animate-slide-up">
                <div className="flex items-center gap-3 mb-4">
                  <div className="w-8 h-8 rounded-lg bg-primary-100 flex items-center justify-center text-primary-600 shrink-0">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
                  </div>
                  <div>
                    <h3 className="font-semibold text-sm text-slate-800">上传简历</h3>
                    <p className="text-xs text-slate-400">支持 PDF / DOCX，最大 5MB</p>
                  </div>
                </div>
                <div
                  className={`relative border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-300 ${resumeDragOver ? 'border-primary-400 bg-primary-50/50 scale-[1.01]' : hasResume ? 'border-emerald-400 bg-emerald-50/30' : 'border-slate-200 hover:border-primary-300 hover:bg-slate-50/50'}`}
                  onDragOver={e => { e.preventDefault(); setResumeDragOver(true); }}
                  onDragLeave={() => setResumeDragOver(false)}
                  onDrop={handleResumeDrop}
                  onClick={() => document.getElementById('resume-file-input')?.click()}
                >
                  {uploading ? (
                    <div className="flex flex-col items-center gap-3 text-primary-600">
                      <div className="w-10 h-10 rounded-full border-2 border-primary-200 border-t-primary-600 animate-spin" />
                      <span className="text-sm font-medium">正在解析简历...</span>
                    </div>
                  ) : hasResume ? (
                    <div className="flex flex-col items-center gap-2">
                      <div className="w-10 h-10 rounded-full bg-emerald-100 flex items-center justify-center">
                        <svg className="w-5 h-5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                      </div>
                      <span className="font-semibold text-emerald-700">{resumeFileName}</span>
                      <span className="text-xs text-slate-400">点击或拖拽更换文件</span>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center gap-2">
                      <div className="w-10 h-10 rounded-full bg-slate-100 flex items-center justify-center">
                        <svg className="w-5 h-5 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" /></svg>
                      </div>
                      <span className="font-medium text-slate-600">点击选择文件或拖拽到此处</span>
                      <span className="text-xs text-slate-400">支持 PDF、DOCX 格式</span>
                    </div>
                  )}
                </div>
                <input
                  id="resume-file-input"
                  type="file"
                  accept=".pdf,.docx"
                  className="hidden"
                  onChange={e => { const file = e.target.files?.[0]; if (file) handleResumeFile(file); }}
                  disabled={uploading}
                />
                {resumeError && (
                  <div className="mt-3 p-3 bg-red-50 border border-red-200 rounded-xl flex items-center gap-2 text-red-600 text-sm">
                    <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                    <span>{resumeError}</span>
                  </div>
                )}
                {resumePreview && (
                  <details className="mt-3 group">
                    <summary className="text-sm text-primary-600 cursor-pointer hover:text-primary-800 font-medium flex items-center gap-1">
                      <svg className="w-4 h-4 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
                      简历预览（前 500 字）
                    </summary>
                    <p className="text-xs text-slate-600 mt-2 bg-slate-50 p-3 rounded-xl border border-slate-200 whitespace-pre-wrap max-h-32 overflow-y-auto scrollbar-thin leading-relaxed">{resumePreview}</p>
                  </details>
                )}
                {questions.length > 0 && (
                  <div className="mt-4 p-4 bg-primary-50/50 rounded-xl border border-primary-100">
                    <p className="font-semibold text-sm text-primary-800 mb-2 flex items-center gap-1.5">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                      面试问题 共 {questions.length} 题
                    </p>
                    <ol className="space-y-1.5">
                      {questions.map((q, idx) => (
                        <li key={idx} className="flex items-start gap-2 text-sm text-slate-700">
                          <span className="w-5 h-5 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center text-[10px] font-bold shrink-0 mt-0.5">{idx + 1}</span>
                          <span>{q}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
                {!hasResume && !uploading && resumeError && (
                  <button onClick={() => document.getElementById('resume-file-input')?.click()} className="mt-3 text-sm text-primary-600 font-medium hover:text-primary-800 flex items-center gap-1">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                    重试上传
                  </button>
                )}
                {hasResume && !interviewStarted && messages.length === 0 && (
                  <button onClick={startInterview} disabled={loading} className="btn-primary w-full mt-4 py-3 text-sm">
                    {loading ? (
                      <span className="flex items-center justify-center gap-2">
                        <div className="w-4 h-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                        正在准备面试...
                      </span>
                    ) : (
                      <span className="flex items-center justify-center gap-2">
                        开始面试
                        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5m0 0l-5 5m5-5H6" /></svg>
                      </span>
                    )}
                  </button>
                )}
              </div>

          {/* 控制面板 */}
          <div className="flex flex-wrap gap-3 items-center mb-4">
            <div className="flex items-center gap-2">
              <label className="text-xs font-semibold text-slate-500 uppercase tracking-wider">岗位</label>
              <select
                className={`text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-primary-400 transition-all ${interviewStarted ? 'bg-slate-50 text-slate-400 cursor-not-allowed' : ''}`}
                value={role}
                onChange={e => {
                  if (interviewStarted) {
                    alert('面试进行中，请先结束当前面试再切换岗位。');
                    return;
                  }
                  setRole(e.target.value);
                }}
                disabled={interviewStarted}
              >
                <option>后端开发</option><option>前端开发</option><option>全栈开发</option>
                <option>算法工程师</option><option>移动开发</option><option>测试开发</option>
                <option>运维开发</option><option>数据工程</option><option>机器学习工程师</option>
                <option>嵌入式开发</option>
              </select>
            </div>
            <label className="flex items-center gap-2 text-sm cursor-pointer group">
              <div className={`w-9 h-5 rounded-full relative transition-colors duration-200 ${enableSpeech ? 'bg-primary-500' : 'bg-slate-300'}`}>
                <div className={`w-4 h-4 rounded-full bg-white absolute top-0.5 shadow-sm transition-transform duration-200 ${enableSpeech ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
              </div>
              <span className="text-xs text-slate-500 group-hover:text-slate-700">AI 语音</span>
              <input type="checkbox" checked={enableSpeech} onChange={e => setEnableSpeech(e.target.checked)} className="hidden" />
            </label>
          </div>

          {interviewStarted && (
            <div className="card mb-4 animate-fade-in">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="text-xs text-slate-400 font-medium">
                    进度 <span className="text-primary-600 font-bold text-sm">{currentQuestionIndex}/{totalQuestions}</span>
                  </span>
                  <div className="flex gap-1.5">
                    {questionStatus.map((s, i) => (
                      <div
                        key={i}
                        className={`w-2.5 h-2.5 rounded-full transition-all duration-300 ${
                          s === 'answered' ? 'bg-emerald-500 shadow-sm shadow-emerald-200' :
                          s === 'skipped' ? 'bg-amber-400' :
                          i === currentQuestionIndex ? 'bg-primary-500 animate-pulse-soft shadow-sm shadow-primary-200' :
                          'bg-slate-200'
                        }`}
                        title={`Q${i+1}: ${s === 'answered' ? '已回答' : s === 'skipped' ? '已跳过' : '待回答'}`}
                      />
                    ))}
                  </div>
                  {questions.length > 0 && (
                    <details className="group">
                      <summary className="text-xs text-slate-400 cursor-pointer hover:text-primary-600 font-medium">问题清单</summary>
                      <div className="absolute mt-2 bg-white border border-slate-200 rounded-xl shadow-lg p-3 z-10 min-w-[200px]">
                        <ol className="text-xs space-y-1">
                          {questions.map((q, i) => (
                            <li key={i} className={questionStatus[i] === 'answered' ? 'text-emerald-600' : questionStatus[i] === 'skipped' ? 'text-amber-500 line-through' : 'text-slate-500'}>
                              {i + 1}. {q}
                            </li>
                          ))}
                        </ol>
                      </div>
                    </details>
                  )}
                </div>
                {timeLeft !== null && (
                  <div className={`flex items-center gap-1.5 text-sm font-mono font-bold px-3 py-1.5 rounded-xl ${
                    timeLeft <= 60 ? 'bg-red-50 text-red-600 border border-red-200 animate-pulse' :
                    timeLeft <= 180 ? 'bg-amber-50 text-amber-700 border border-amber-200' :
                    'bg-primary-50 text-primary-700 border border-primary-200'
                  }`}>
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                    {Math.floor(timeLeft / 60)}:{String(timeLeft % 60).padStart(2, '0')}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* 聊天区域 */}
          <div ref={chatContainerRef} className="bg-slate-50 rounded-2xl p-5 h-[420px] overflow-y-auto mb-4 scrollbar-thin border border-slate-200/60 shadow-inner">
            {messages.map((msg, idx) => (
              <div key={idx} className={`mb-3 flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'} animate-slide-up`} style={{ animationDelay: '0ms' }}>
                {msg.role === 'assistant' && (
                  <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-xs font-bold shrink-0 mr-2 mt-1 shadow-sm">
                    AI
                  </div>
                )}
                <div className={`max-w-[75%] ${msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-ai'}`}>
                  <p className="text-sm leading-relaxed whitespace-pre-wrap">{msg.content}</p>
                </div>
              </div>
            ))}
            {loading && (
              <div className="flex justify-start mb-3">
                <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white text-xs font-bold shrink-0 mr-2 mt-1 shadow-sm">
                  AI
                </div>
                <div className="chat-bubble-ai max-w-[75%]">
                  <div className="flex gap-1.5">
                    <div className="w-2 h-2 rounded-full bg-slate-300 animate-bounce" style={{ animationDelay: '0ms' }} />
                    <div className="w-2 h-2 rounded-full bg-slate-300 animate-bounce" style={{ animationDelay: '150ms' }} />
                    <div className="w-2 h-2 rounded-full bg-slate-300 animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* 输入区域 */}
          <div className="flex gap-2 items-end">
            <textarea
              className="flex-1 input-field resize-none min-h-[48px] max-h-24 text-sm"
              rows={2}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
              placeholder={interviewStarted ? `第 ${currentQuestionIndex + 1}/${totalQuestions} 题 - 输入回答...` : hasResume ? "请先点击「开始面试」" : "请先上传简历"}
              disabled={!interviewStarted || interviewFinished}
            />
            {speechSupported && (
              <button onClick={startListening} disabled={isListening || !interviewStarted} className={`w-11 h-11 rounded-xl flex items-center justify-center shrink-0 transition-all duration-200 ${isListening ? 'bg-red-500 text-white shadow-lg shadow-red-200 animate-pulse' : 'bg-slate-100 text-slate-500 hover:bg-slate-200'}`} title="语音输入">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" /></svg>
              </button>
            )}
            {interviewStarted && !interviewFinished && (
              <button onClick={skipQuestion} disabled={loading} className="btn-secondary text-sm px-4 h-11 shrink-0 flex items-center gap-1.5" title="跳过此题">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" /></svg>
                跳过
              </button>
            )}
            <button onClick={sendMessage} disabled={loading || !interviewStarted || interviewFinished} className="btn-primary h-11 px-5 shrink-0 flex items-center gap-1.5 text-sm">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" /></svg>
              发送
            </button>
            <button onClick={endInterview} disabled={loading || messages.length === 0} className="btn-danger h-11 px-4 shrink-0 flex items-center gap-1.5 text-sm">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              结束
            </button>
          </div>
            </>
          )}
        </>
      )}

      {/* 题库弹窗 */}
      {showQuestionBank && (
        <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => setShowQuestionBank(false)}>
          <div className="animate-scale-in bg-white rounded-2xl p-6 max-w-2xl w-full max-h-[80vh] overflow-auto shadow-float m-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-5">
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center">
                  <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" /></svg>
                </div>
                <h2 className="text-xl font-bold text-slate-800">面试题库</h2>
              </div>
              <button onClick={() => setShowQuestionBank(false)} className="btn-ghost w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-600">
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
                        {q.tags.map(tag => (
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
      )}

      {/* 登录/注册弹窗 */}
      {showAuthModal && (
        <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => setShowAuthModal(false)}>
          <div className="animate-scale-in bg-white rounded-2xl p-6 w-96 max-h-[90vh] overflow-auto shadow-float" onClick={(e) => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-5">
              <h2 className="text-xl font-bold text-slate-800">{authMode === 'login' ? '登录' : '注册'}</h2>
              <button onClick={() => setShowAuthModal(false)} className="btn-ghost w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-600">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <input
              type="text"
              placeholder="用户名"
              value={username}
              onChange={e => setUsername(e.target.value)}
              className="input-field mb-2"
            />
            {authMode === 'register' && (
              <div className="text-xs mb-2 text-slate-400">
                3-16位字母、数字、下划线或中文
              </div>
            )}
            <input
              type="password"
              placeholder="密码"
              value={password}
              onChange={e => {
                const pwd = e.target.value;
                setPassword(pwd);
                if (authMode === 'register') {
                  const lenOk = pwd.length >= 8 && pwd.length <= 16;
                  const hasLetter = /[A-Za-z]/.test(pwd);
                  const hasDigit = /\d/.test(pwd);
                  const hasSymbol = /[!@#$%^&*()_+\-=[\]{};':"\\|,.<>/?]/.test(pwd);
                  const kindOk = [hasLetter, hasDigit, hasSymbol].filter(Boolean).length >= 2;
                  const repeatOk = !/(.)\1{5,}/.test(pwd) &&
                                    !/012345|123456|234567|345678|456789|567890/.test(pwd) &&
                                    !/abcdef|bcdefg|cdefgh|defghi|efghij|fghijk/.test(pwd);
                  setPasswordRules({ length: lenOk, kind: kindOk, noRepeat: repeatOk });
                  if (pwd.length > 0) {
                    if (!lenOk) setPasswordError('密码长度应为8-16位');
                    else if (!kindOk) setPasswordError('密码必须包含字母、数字、符号中至少2种');
                    else if (!repeatOk) setPasswordError('请勿输入连续、重复位以上字母或数字');
                    else setPasswordError('');
                  } else setPasswordError('');
                }
              }}
              className="input-field mb-2"
            />
            {authMode === 'register' && (
              <>
                <div className="text-xs mb-2 space-y-1">
                  <div className={passwordRules.length ? 'text-emerald-600' : 'text-slate-400'}>
                    {passwordRules.length ? '✓' : '✗'} 长度8-16个字
                  </div>
                  <div className={passwordRules.kind ? 'text-emerald-600' : 'text-slate-400'}>
                    {passwordRules.kind ? '✓' : '✗'} 必须包含字母、数字、符号中至少2种
                  </div>
                  <div className={passwordRules.noRepeat ? 'text-emerald-600' : 'text-slate-400'}>
                    {passwordRules.noRepeat ? '✓' : '✗'} 请勿输入连续、重复6位以上字母或数字
                  </div>
                </div>
                <input
                  type="email"
                  placeholder="邮箱（必填，如 user@example.com）"
                  value={email}
                  onChange={e => {
                    const val = e.target.value;
                    setEmail(val);
                    if (val && !validateEmail(val)) {
                      setEmailError('邮箱格式不正确');
                    } else {
                      setEmailError('');
                    }
                  }}
                  className={`input-field mb-2 ${emailError ? 'border-red-400 focus:ring-red-400' : ''}`}
                />
                {emailError && <p className="text-red-500 text-xs mb-2">{emailError}</p>}
                <input
                  type="password"
                  placeholder="确认密码"
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  className="input-field mb-3"
                />
                <label className="flex items-center text-sm text-slate-600 mb-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={agreeTerms}
                    onChange={e => setAgreeTerms(e.target.checked)}
                    className="mr-2 rounded"
                  />
                  我已阅读并同意<a href="#" className="text-primary-600 underline hover:text-primary-800">《用户协议》</a>
                </label>
              </>
            )}
            <div className="mb-3">
              <div className="flex gap-2 items-center mb-2">
                {captchaImage ? (
                  <img src={captchaImage} alt="验证码" className="h-10 border border-slate-200 rounded-lg" />
                ) : (
                  <div className="h-10 bg-slate-100 rounded-lg flex items-center justify-center text-sm text-slate-400 flex-1">
                    {captchaLoading ? '加载中...' : '点击刷新获取'}
                  </div>
                )}
                <button
                  type="button"
                  onClick={loadCaptcha}
                  disabled={captchaLoading}
                  className="btn-ghost text-xs shrink-0"
                >
                  {captchaLoading ? '加载中...' : '换一张'}
                </button>
              </div>
              <input
                type="text"
                placeholder="请输入验证码"
                value={captchaCode}
                onChange={e => setCaptchaCode(e.target.value)}
                className="input-field"
                autoComplete="off"
              />
            </div>
            {authError && <p className="text-red-500 text-sm mb-3">{authError}</p>}
            <button onClick={handleAuth} disabled={authLoading || (authMode === 'register' && (!username || !password || !email || !validateEmail(email) || !agreeTerms || password !== confirmPassword))} className="btn-primary w-full py-2.5 text-sm">
              {authLoading ? '验证中...' : authMode === 'login' ? '登录' : '注册'}
            </button>
            <p className="text-sm text-center mt-4 text-slate-500">
              {authMode === 'login' ? '没有账号？' : '已有账号？'}
              <button
                onClick={() => {
                  setAuthMode(authMode === 'login' ? 'register' : 'login');
                  setAuthError('');
                  setEmailError('');
                  setPasswordError('');
                  setPassword('');
                  setConfirmPassword('');
                  setEmail('');
                  setAgreeTerms(false);
                  setPasswordRules({ length: false, kind: false, noRepeat: false });
                  setCaptchaCode('');
                  loadCaptcha();
                }}
                className="text-primary-600 hover:text-primary-800 font-medium ml-1"
              >
                {authMode === 'login' ? '立即注册' : '去登录'}
              </button>
            </p>
          </div>
        </div>
      )}
      </main>
    </div>
  );
}

export default App;
