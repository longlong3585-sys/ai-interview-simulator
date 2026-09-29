/**
 * T-47：个人中心（原 App.tsx 的 JSX 第 1377~1557 行的两个弹窗）。
 *
 * 设计取舍：资料/头像/密码状态全部由本组件自持，而不是留在 App 里。
 * 原因是这些状态**没有任何其他消费者**：`userProfile` 只在这里显示，
 * 顶部导航用的用户名来自 `AuthContext`（`currentUsername`）。
 * 于是 App 少掉 8 个 state + 两个加载函数。
 *
 * 唯一需要外部配合的两件事走 props：
 *   · `onLogout`：改密成功后强制重新登录（原实现 `setTimeout(() => logout(), 2000)`）；
 *   · `open` / `onClose`：因为顶部按钮和"改密成功后关闭"都要控制它。
 */

import { useEffect, useRef, useState } from 'react';
import { API_BASE_URL } from '../config';
import { authFetch } from '../services/api';
import { passwordError } from '../utils/passwordRules';

interface ProfilePanelProps {
  open: boolean;
  token: string | null;
  username: string;
  onClose: () => void;
  onLogout: () => void;
}

export function ProfilePanel({ open, token, username, onClose, onLogout }: ProfilePanelProps) {
  const [userProfile, setUserProfile] = useState<any>({});
  const [editProfile, setEditProfile] = useState<any>({});
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [avatarZoom, setAvatarZoom] = useState(1);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmNewPassword, setConfirmNewPassword] = useState('');
  const [passwordChangeMsg, setPasswordChangeMsg] = useState('');
  const [passwordChanging, setPasswordChanging] = useState(false);

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

  useEffect(() => {
    if (token) void loadUserProfile(token);
  }, [token]);

  /**
   * 面板打开时把资料复制进"可编辑副本"（原实现由顶部按钮在点击瞬间完成）。
   * 用 `wasOpen` 卡住状态迁移：`userProfile` 异步到达时不能覆盖用户正在编辑的内容。
   */
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setEditProfile({ ...userProfile });
      setPasswordChangeMsg('');
      setOldPassword('');
      setNewPassword('');
      setConfirmNewPassword('');
    }
    wasOpen.current = open;
  }, [open, userProfile]);

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
    // T-08 / FR-1.5：改为与注册**同一套**规则（原先只要求 ≥6 位，
    // 导致用户输入 6-7 位时前端放行、后端拒绝）
    const pwdError = passwordError(newPassword);
    if (pwdError) {
      setPasswordChangeMsg(pwdError);
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
        onClose();
        setTimeout(() => onLogout(), 2000);
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

  const saveProfile = async () => {
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
      onClose();
      alert('资料更新成功');
    } else {
      const err = await res.json();
      alert(err.detail || '更新失败');
    }
  };

  /** 头像裁剪 + 上传（原实现在 JSX 的 onClick 里内联，搬出来逐字不改）。 */
  const uploadAvatar = async () => {
    if (!avatarFile || !token) return;
    const canvas = document.createElement('canvas');
    const size = 200;
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const img = new Image();
    img.src = avatarPreview ?? '';
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
  };

  return (
    <>
      {/* 个人中心弹窗 */}
      {open && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-11/12 max-w-md max-h-[90vh] overflow-auto">
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-xl font-bold">👤 个人中心</h2>
              <button onClick={onClose} className="text-gray-500 text-xl">×</button>
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
                <input type="text" value={username} disabled className="w-full border rounded p-2 bg-gray-100" />
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
              <button onClick={saveProfile} className="w-full bg-blue-500 text-white rounded p-2 hover:bg-blue-600">保存资料</button>
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
                onClick={uploadAvatar}
                className="flex-1 bg-blue-500 text-white rounded p-2 hover:bg-blue-600"
              >
                确认上传
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
