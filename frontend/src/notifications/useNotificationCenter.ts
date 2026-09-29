/**
 * T-47：通知中心 + 面试历史的**数据与状态**（原 App.tsx 第 95~103、831~1026 行）。
 *
 * 与 UI（`NotificationCenter.tsx`）分开的原因：`unreadCount` 要显示在**顶部导航**的
 * 消息红点上，`openHistory()` 又要被面试流程（报告保存成功后）调用 ——
 * 状态留在 App 会让 App 重新变胖，留在 UI 组件里又拿不到这两处引用。
 *
 * T-48：原先这里还有一个 `historyListRef`（`useRef` 声明 + 绑到 `<ul ref=…>`，
 * 但**从未被读过** —— 高亮滚动走的是 `document.getElementById('record-…')`）。
 * 只写不读的 ref 已删除，`<ul>` 也不再挂 ref。
 */

import { useEffect, useState } from 'react';
import { API_BASE_URL } from '../config';

export interface NotificationCenterApi {
  history: any[];
  notifications: any[];
  unreadCount: number;
  expandedHistoryId: number | null;
  setExpandedHistoryId: (id: number | null) => void;
  highlightId: number | null;
  showInfoPanel: boolean;
  setShowInfoPanel: (open: boolean) => void;
  infoTab: 'notifications' | 'history';
  setInfoTab: (tab: 'notifications' | 'history') => void;
  markAllRead: () => Promise<void>;
  deleteNotification: (id: number) => Promise<void>;
  clearAllNotifications: () => Promise<void>;
  handleNotificationClick: (n: any) => Promise<void>;
  /** 报告保存成功后调用：刷新历史 + 直接切到「面试历史」页签。 */
  openHistory: () => void;
  /** 登出时清空（T-42：不能让下一个账号继承上一个的面板内容）。 */
  clear: () => void;
}

interface UseNotificationCenterOptions {
  token: string | null;
  userRole: string | null;
}

export function useNotificationCenter({ token, userRole }: UseNotificationCenterOptions): NotificationCenterApi {
  const [history, setHistory] = useState<any[]>([]);
  const [notifications, setNotifications] = useState<any[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [expandedHistoryId, setExpandedHistoryId] = useState<number | null>(null);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [showInfoPanel, setShowInfoPanel] = useState(false);
  const [infoTab, setInfoTab] = useState<'notifications' | 'history'>('notifications');

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

  const openHistory = () => {
    setInfoTab('history');
    setShowInfoPanel(true);
    if (token) void loadHistory(token);
  };

  const clear = () => {
    setHistory([]);
    setUnreadCount(0);
    setNotifications([]);
    setShowInfoPanel(false);
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

  return {
    history, notifications, unreadCount,
    expandedHistoryId, setExpandedHistoryId, highlightId,
    showInfoPanel, setShowInfoPanel, infoTab, setInfoTab,
    markAllRead, deleteNotification, clearAllNotifications, handleNotificationClick,
    openHistory, clear,
  };
}
