"""T-19：`SQLiteSessionStore` —— `SessionStore` 协议的 SQLite 实现。

这是本项目的**第一段真正接入新存储的业务代码**，因此把设计约束写在最前面。

## 四条硬约束（对应任务描述的四个关键词）

### 1. 短事务（最重要）
每个方法自己是一个**短事务**（目标 < 50ms），`with self._session() as s:` 一进一出。
**AI 调用绝不允许出现在这里** —— 那会持写锁 25~30 秒，在 WAL + `BEGIN IMMEDIATE`
（T-15）下把所有请求堵死。正确时序是"事务外调 AI，短事务只落库"。

为此本实现**刻意不暴露任何"打开的事务句柄"**给调用方：
没有 `begin()`、没有可传入的 session 参数。调用方拿不到长事务，就写不出长事务。

### 2. 乐观锁 `version`
`commit_turn` / `finish` / `abandon` 全部带 `WHERE version = :expected`。
`rowcount == 0` 表示有人先提交了（或状态已变、已过期）→ 返回
`CommitResult(applied=False, snapshot=最新快照)`，调用方返回 **409**，
**绝不重试写、更不能用陈旧载荷覆盖**（ADR-004 的关键更正）。

### 3. `seq` 幂等
`find_replay()` 是"避免重复计费"的唯一关卡（ADR-004 第 0 步）。
返回 `ReplayLookup` 结构体而非裸 `str` —— 见 base.py 中关于
"`None` 兼表两义会导致重复计费"的说明。

### 4. 状态机
`active → finished`（报告生成成功）/ `active → abandoned`（超时 / 用户放弃 / TTL 到期）。
**任何非 `active` 状态都不占用** `UNIQUE(user_id) WHERE status='active'`
的部分唯一索引 → 用户永远能开新面试（ADR-022R 的核心不变量）。

## 与 `safe_json_loads` 的有意分歧

T-10 定下"读 JSON 文本列一律走 `safe_json_loads()`（容错、坏数据回退默认值）"。
本模块**不遵守**该约定，改用严格解析并在坏数据上抛 `StoreError`。理由：

`question_status` / `user_answers` 一旦被静默回退成 `[]`，评分就会看到
"0 道题、0 个回答"，进而产出一份**看起来正常、实则完全错误**的报告 ——
正是 ADR-007R 要消除的"误导性 0 分"。会话行损坏属于数据完整性事故，
应当**当场炸掉**并留下 `session_id`，而不是悄悄降级。
`interview_records` 那类"一条坏记录拖垮整个列表"的场景才是容错的适用场合。
"""

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services.stores._sqlite_tx import begin_write
from services.stores.base import (
    ActiveSessionExists,
    CommitResult,
    EndedReason,
    ReplayLookup,
    SessionDraft,
    SessionSnapshot,
    SessionStatus,
    StoreError,
)

logger = logging.getLogger("app.stores.sessions")

#: 快照涉及的全部列（与 migrations/versions/002 + 004 的 DDL 一致）
_COLUMNS = (
    "session_id, user_id, role, questions, question_status, user_answers, "
    "current_index, last_seq, last_reply, version, status, created_at, "
    "updated_at, expires_at, ended_reason, report"
)

#: 需要 JSON 编解码的 TEXT 列
_JSON_COLUMNS = ("questions", "question_status", "user_answers")


def _loads_strict(raw: Any, column: str, session_id: str):
    """严格解析 JSON 文本列。坏数据 -> 抛错（**不**回退默认值，理由见模块说明）。"""
    if raw is None or raw == "":
        raise StoreError(
            "会话 %s 的 %s 列为空 —— 会话数据不完整，拒绝静默降级"
            % (session_id, column)
        )
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise StoreError(
            "会话 %s 的 %s 列不是合法 JSON（%s）—— 拒绝静默降级为默认值，"
            "否则会产出误导性的评分报告" % (session_id, column, exc)
        )


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class SQLiteSessionStore(object):
    """`SessionStore` 协议在 SQLite 上的实现。

    构造参数
    --------
    session_factory:
        SQLAlchemy 的 `sessionmaker`。默认用 `database.SessionLocal`，
        这样会**继承 T-15 的 engine 契约**（WAL / `busy_timeout=15000` /
        `foreign_keys=ON` / `BEGIN IMMEDIATE`）。测试可注入独立工厂。
        延迟到方法调用时才解析默认值，避免 import 期的副作用。
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _factory(self):
        if self._session_factory is None:
            from database import SessionLocal  # 延迟导入：避免 import 期绑定引擎
            self._session_factory = SessionLocal
        return self._session_factory

    def _session(self):
        return self._factory()()

    @staticmethod
    def _to_snapshot(row) -> SessionSnapshot:
        m = row._mapping
        sid = m["session_id"]
        return SessionSnapshot(
            session_id=sid,
            user_id=m["user_id"],
            role=m["role"],
            questions=_loads_strict(m["questions"], "questions", sid),
            question_status=_loads_strict(m["question_status"], "question_status", sid),
            user_answers=_loads_strict(m["user_answers"], "user_answers", sid),
            current_index=m["current_index"],
            last_seq=m["last_seq"],
            last_reply=m["last_reply"],
            version=m["version"],
            status=m["status"],
            created_at=m["created_at"],
            updated_at=m["updated_at"],
            expires_at=m["expires_at"],
            ended_reason=m["ended_reason"],
            report=m["report"],
        )

    def _fetch(self, s, session_id: str) -> Optional[SessionSnapshot]:
        row = s.execute(
            text("SELECT %s FROM interview_sessions WHERE session_id = :sid" % _COLUMNS),
            {"sid": session_id},
        ).fetchone()
        return self._to_snapshot(row) if row else None

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> Optional[SessionSnapshot]:
        s = self._session()
        try:
            return self._fetch(s, session_id)
        finally:
            s.close()

    def get_active(self, user_id: int, now: str) -> Optional[SessionSnapshot]:
        s = self._session()
        try:
            row = s.execute(
                text(
                    "SELECT %s FROM interview_sessions "
                    "WHERE user_id = :uid AND status = :st AND expires_at > :now "
                    "ORDER BY created_at DESC LIMIT 1" % _COLUMNS
                ),
                {"uid": user_id, "st": SessionStatus.ACTIVE, "now": now},
            ).fetchone()
            return self._to_snapshot(row) if row else None
        finally:
            s.close()

    def find_replay(self, session_id: str, seq: int) -> Optional[ReplayLookup]:
        s = self._session()
        try:
            row = s.execute(
                text(
                    "SELECT last_seq, last_reply FROM interview_sessions "
                    "WHERE session_id = :sid"
                ),
                {"sid": session_id},
            ).fetchone()
            if row is None:
                return None
            last_seq, last_reply = row[0], row[1]
            if seq is not None and last_seq is not None and seq <= last_seq:
                return ReplayLookup(is_replay=True, reply=last_reply)
            return ReplayLookup(is_replay=False)
        finally:
            s.close()

    # ------------------------------------------------------------------
    # 自愈
    # ------------------------------------------------------------------

    def abandon_expired_for_user(self, user_id: int, now: str) -> int:
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            result = s.execute(
                text(
                    "UPDATE interview_sessions "
                    "SET status = :abandoned, ended_reason = :reason, "
                    "    version = version + 1, updated_at = :now "
                    "WHERE user_id = :uid AND status = :active AND expires_at <= :now"
                ),
                {
                    "abandoned": SessionStatus.ABANDONED,
                    "reason": EndedReason.TIMEOUT,
                    "now": now,
                    "uid": user_id,
                    "active": SessionStatus.ACTIVE,
                },
            )
            s.commit()
            return result.rowcount or 0
        finally:
            s.close()

    # ------------------------------------------------------------------
    # 写（各自一个短事务）
    # ------------------------------------------------------------------

    def create(self, draft: SessionDraft) -> SessionSnapshot:
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            s.execute(
                text(
                    "INSERT INTO interview_sessions "
                    "(session_id, user_id, role, questions, question_status, "
                    " user_answers, current_index, last_seq, last_reply, version, "
                    " status, created_at, updated_at, expires_at, ended_reason, report) "
                    "VALUES (:sid, :uid, :role, :questions, :question_status, "
                    " :user_answers, :current_index, :last_seq, :last_reply, :version, "
                    " :status, :created_at, :updated_at, :expires_at, :ended_reason, :report)"
                ),
                {
                    "sid": draft.session_id,
                    "uid": draft.user_id,
                    "role": draft.role,
                    "questions": _dumps(draft.questions),
                    "question_status": _dumps(draft.question_status),
                    "user_answers": _dumps(draft.user_answers),
                    "current_index": draft.current_index,
                    "last_seq": draft.last_seq,
                    "last_reply": draft.last_reply,
                    "version": draft.version,
                    "status": draft.status,
                    "created_at": draft.created_at,
                    "updated_at": draft.updated_at,
                    "expires_at": draft.expires_at,
                    "ended_reason": None,
                    # 新会话还没有报告；报告由 finish() 在置终态时写入
                    "report": None,
                },
            )
            s.commit()
        except IntegrityError as exc:
            s.rollback()
            # 不能只靠异常文本判断（不同 SQLite 版本措辞不同）。
            # 直接回查一次：若该用户确实已有 active 会话，就是部分唯一索引挡的。
            if self._has_active(s, draft.user_id):
                raise ActiveSessionExists(draft.user_id)
            raise StoreError(
                "新建会话失败（不是 active 冲突，请检查 user_id=%r 是否存在）：%s"
                % (draft.user_id, exc)
            )
        finally:
            s.close()

        created = self.get(draft.session_id)
        if created is None:  # 理论上不可能；留个明确的错误而不是 None 穿透
            raise StoreError("会话创建后立即查不到：%s" % draft.session_id)
        return created

    def _has_active(self, s, user_id: int) -> bool:
        row = s.execute(
            text(
                "SELECT count(*) FROM interview_sessions "
                "WHERE user_id = :uid AND status = :st"
            ),
            {"uid": user_id, "st": SessionStatus.ACTIVE},
        ).fetchone()
        return bool(row and row[0])

    def _guarded_update(self, s, sql: str, params: dict) -> int:
        result = s.execute(text(sql), params)
        return result.rowcount or 0

    def commit_turn(self, commit) -> CommitResult:
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            changed = self._guarded_update(
                s,
                "UPDATE interview_sessions "
                "SET current_index = :ci, question_status = :qs, "
                "    user_answers = :ua, last_seq = :ls, last_reply = :lr, "
                "    version = version + 1, updated_at = :updated_at "
                "WHERE session_id = :sid "
                "  AND version = :expected "
                "  AND status = :active "
                "  AND expires_at > :now",
                {
                    "ci": commit.current_index,
                    "qs": _dumps(commit.question_status),
                    "ua": _dumps(commit.user_answers),
                    "ls": commit.last_seq,
                    "lr": commit.last_reply,
                    # 注意两个时间参数用途不同，不能混用：
                    #   updated_at = 写回的"现在"
                    #   now        = 过期守卫的比较基准（两者通常同值，但语义不同）
                    "updated_at": commit.updated_at,
                    "now": commit.now,
                    "sid": commit.session_id,
                    "expected": commit.expected_version,
                    "active": SessionStatus.ACTIVE,
                },
            )
            s.commit()
            if changed:
                return CommitResult(applied=True, snapshot=self._fetch(s, commit.session_id))
            # rowcount == 0：不要重试写，把最新状态交给调用方去返回 409
            return CommitResult(applied=False, snapshot=self._fetch(s, commit.session_id))
        finally:
            s.close()

    def finish(self, session_id: str, expected_version: int, report_json: str,
               ended_reason: str, now: str) -> CommitResult:
        self._validate_reason(ended_reason)
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            changed = self._guarded_update(
                s,
                "UPDATE interview_sessions "
                "SET report = :report, status = :finished, ended_reason = :reason, "
                "    version = version + 1, updated_at = :now "
                "WHERE session_id = :sid AND version = :expected AND status = :active",
                {
                    "report": report_json,
                    "finished": SessionStatus.FINISHED,
                    "reason": ended_reason,
                    "now": now,
                    "sid": session_id,
                    "expected": expected_version,
                    "active": SessionStatus.ACTIVE,
                },
            )
            s.commit()
            return CommitResult(applied=bool(changed),
                                snapshot=self._fetch(s, session_id))
        finally:
            s.close()

    def abandon(self, session_id: str, expected_version: int,
                ended_reason: str, now: str) -> CommitResult:
        """置 `abandoned`。**过期自愈也走这里**（`expected_version` 传当前值）。

        注意与 `finish` 的差别：`abandon` 不带 `expires_at > :now` 守卫 ——
        它本就是在"已过期"或"用户主动放弃"时调用，加过期守卫会让超时路径**永远失败**。
        """
        self._validate_reason(ended_reason)
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            changed = self._guarded_update(
                s,
                "UPDATE interview_sessions "
                "SET status = :abandoned, ended_reason = :reason, "
                "    version = version + 1, updated_at = :now "
                "WHERE session_id = :sid AND version = :expected AND status = :active",
                {
                    "abandoned": SessionStatus.ABANDONED,
                    "reason": ended_reason,
                    "now": now,
                    "sid": session_id,
                    "expected": expected_version,
                    "active": SessionStatus.ACTIVE,
                },
            )
            s.commit()
            return CommitResult(applied=bool(changed),
                                snapshot=self._fetch(s, session_id))
        finally:
            s.close()

    def abandon_all_expired(self, now: str) -> int:
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            result = s.execute(
                text(
                    "UPDATE interview_sessions "
                    "SET status = :abandoned, ended_reason = :reason, "
                    "    version = version + 1, updated_at = :now "
                    "WHERE status = :active AND expires_at <= :now"
                ),
                {
                    "abandoned": SessionStatus.ABANDONED,
                    "reason": EndedReason.TIMEOUT,
                    "now": now,
                    "active": SessionStatus.ACTIVE,
                },
            )
            s.commit()
            return result.rowcount or 0
        finally:
            s.close()

    @staticmethod
    def _validate_reason(ended_reason: str):
        if ended_reason not in EndedReason.ALL:
            raise StoreError(
                "ended_reason 取值非法：%r（必须是 %s 之一）"
                % (ended_reason, " / ".join(EndedReason.ALL))
            )
