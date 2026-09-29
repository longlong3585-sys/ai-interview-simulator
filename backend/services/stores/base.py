"""T-16：存储访问抽象层 —— **只有协议与数据类型，没有任何实现**。

为什么需要这一层（ADR-004R §2.8 逃生舱）
----------------------------------------
本轮选定 **SQLite + WAL**（选项 A）。它的已知边界是"不支持多机横向扩展"。
为了让"将来切到 Redis（选项 B）"的成本从"重写业务逻辑"降为
"新增一个实现类 + 改一行装配"，规定：

    所有调用方**只依赖本模块的协议**；不得直接 import 具体实现，
    更不得在业务代码里写 SQL。

重启该 ADR 的触发阈值（docs/02-architecture-v2.md §2.8）：

| 触发条件 | 阈值 |
|---|---|
| 部署形态 | 需要**多台**应用服务器 |
| 写竞争 | 日志出现 `database is locked` > **每日 1 次**（且 busy_timeout 已 15s） |
| 写量 | 峰值写请求 > **50/秒** |
| 会话规模 | 同时活跃会话 > **1000** |

本模块的硬约束（由 tests/test_stores_protocol.py 逐条断言）
----------------------------------------------------------
1. **只依赖标准库**（`typing` / `dataclasses` / `datetime`）。不 import
   sqlalchemy、不 import redis、不 import 项目的 config / database
   —— 保持存储无关且可独立测试。
2. **不含任何 Redis 概念**：没有 `pipeline()`、没有秒级整数 TTL 键、
   没有 `hset`/`zadd`；时间一律是 UTC ISO 字符串。这样两种实现都能满足。
3. 不出现任何 SQL 关键字 —— 协议层不该泄露查询语言。

时间契约（三个实现必须完全一致）
--------------------------------
所有 datetime 参数与返回值都是 **UTC 的 ISO-8601 字符串**
（`datetime.utcnow().isoformat()`，例如 `2026-09-29T03:14:15.123456`），
**不是** float 时间戳。理由：

1. 与 DDL 的 `DATETIME` 列一致（SQLite 里就是 TEXT），不必来回转换；
2. 同格式 UTC ISO 串的**字典序等于时间序**，SQL 里可直接写
   `WHERE expires_at > :now`，无需 `strftime()` 之类的函数（用函数会让索引失效）；
3. 用 float 会让"存储里到底存了什么"变得依赖实现细节。

> ⚠️ 需要"当前时间"请用 `utcnow_iso()`，"多少秒之后"请用 `iso_after()`。
> **实现方不要自己拼时间串** —— 格式一旦漂移，字典序比较会**静默**失效
> （比较不再报错，只是结果悄悄变错），这是最难排查的一类 bug。

TTL 契约（ADR-022 / §6.3，由 config 持有具体数值，本层不重复定义以免漂移）
--------------------------------------------------------------------------
| 对象 | TTL |
|---|---|
| `captcha_store` | 300 秒（惰性 + timer） |
| `auth_attempts` | 10 分钟（timer） |
| `interview_sessions` | 2 小时（惰性置 `abandoned` + timer） |
| `token_blacklist` | **8 小时**（≥ 续期绝对上限，否则被吊销令牌会复活） |
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional, Protocol, runtime_checkable

# ===========================================================================
# 常量：与 DDL 的取值语义一一对应
# ===========================================================================


class SessionStatus(object):
    """会话状态机（ADR-022 + ADR-007R）。

        active ──报告生成成功──────────▶ finished   （释放唯一锁）
           │
           ├──超时 / 用户放弃 / TTL 到期──▶ abandoned （释放唯一锁）
           │                                   │
           │                                   └──仍可补写报告（T-27）──▶ report 非空
           └──（abandoned 不会被"提升"为 finished）

    关于 `abandoned` 之后仍能带报告（v2.4 / ADR-007R）：报告与状态是**两个正交的
    事实** ——
      * `status` 说明这场面试**怎么结束的**（正常完成 / 超时 / 用户放弃）；
      * `report` 说明评分**算过没有**。

    超时面试按 ADR-022R 必须是 `abandoned`（"面试并未正常完成"），
    但按 ADR-007R 又**必须出报告**。若为了写报告把状态改成 `finished`，
    库里就会出现"用户从未完成的面试被记成已完成"，状态语义被报告路径污染。
    因此 `abandoned + report` 是一个**合法组合**，原因由 `ended_reason` 承载。
    """

    ACTIVE = "active"
    FINISHED = "finished"
    ABANDONED = "abandoned"
    ALL = (ACTIVE, FINISHED, ABANDONED)


#: 非 active 的状态。**任何终态都不占用** `UNIQUE(user_id) WHERE status='active'`
#: 的部分唯一索引 —— 这是"用户永远能开新面试"的关键不变量（ADR-022R）。
TERMINAL_STATUSES = (SessionStatus.FINISHED, SessionStatus.ABANDONED)


class EndedReason(object):
    """会话结束原因（ADR-007R）。

    超时场景与"正常完成"必须可区分：`timeout` 时报告需标注
    "因超时自动结束，仅基于已答部分评分"，且未答题**不得计零分**。
    """

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    MANUAL = "manual"
    ALL = (COMPLETED, TIMEOUT, MANUAL)


# ===========================================================================
# 时间工具（三个实现共用，避免各处自行拼串导致格式漂移）
# ===========================================================================


def utcnow_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（本层唯一认可的"现在"）。"""
    return datetime.utcnow().isoformat()


def iso_after(seconds: float, base: Optional[str] = None) -> str:
    """`base`（默认现在）之后 `seconds` 秒的 ISO 串。

    参数
    ----
    seconds: 秒数，可为负（用于构造"已过期"的测试数据）。
    base:    ISO 串；给了就从它往后算，不给自己取当前时间。
    """
    origin = datetime.fromisoformat(base) if base else datetime.utcnow()
    return (origin + timedelta(seconds=seconds)).isoformat()


# ===========================================================================
# 异常：调用方按类型分支，不解析字符串
# ===========================================================================


class StoreError(Exception):
    """存储层可预期错误基类。一切非它子类的异常都视为缺陷（应记堆栈）。"""


class SessionNotFound(StoreError):
    """按 session_id 找不到会话。"""

    def __init__(self, session_id):
        StoreError.__init__(self, "会话不存在：%s" % session_id)
        self.session_id = session_id


class ActiveSessionExists(StoreError):
    """该用户已有 active 会话 —— 部分唯一索引拒绝了插入（ADR-023）。

    调用方应返回 **409**，并**在响应体里直接携带会话摘要**
    （`current_index` / `last_seq` / `total`），使前端无需额外一次往返
    就能给出"继续上次面试 / 放弃并重新开始"（ADR-022 R-10）。
    """

    def __init__(self, user_id, existing=None):
        StoreError.__init__(self, "用户 %s 已有进行中的面试" % user_id)
        self.user_id = user_id
        #: 若存储实现顺手查到了冲突会话，放在这里；允许为 None。
        self.existing = existing


# ===========================================================================
# 数据类型
# ===========================================================================


@dataclass(frozen=True)
class ReplayLookup(object):
    """`seq` 幂等前置去重的结果（ADR-004 第 0 步）。

    ⚠️ **这是 T-19 实施时补上的协议修正。** T-16 首版把 `find_replay` 的返回
    类型写成 `Optional[str]`，用 `None` 同时表示两件事：

        * "这不是重发，请继续正常流程"（要调 AI）
        * "这是重发，但上次的回复恰好是空的"（**不要**调 AI）

    两者在 `None` 上撞车。若调用方把后者当成前者，就会**重复调用 AI 并重复计费**
    —— 而这恰恰是 ADR-023 幂等机制要消除的头号问题。
    一个"看不出来"的歧义，代价是真实账单，因此改成显式的结构体。
    """

    #: True = 客户端在重发（`seq <= last_seq`），调用方应直接返回 `reply`，
    #: **不调用 AI、不推进索引**。
    is_replay: bool
    #: 仅当 `is_replay=True` 时有意义；允许为 `None`（上次回复正文为空）。
    reply: Optional[str] = None


@dataclass(frozen=True)
class SessionSnapshot(object):
    """一次会话的完整快照（只读）。

    冻结（frozen）：快照一旦取出就代表"某一时刻的状态"，
    业务代码不得就地修改它去假装写库 —— 写入必须经过 `SessionStore` 的方法。
    这能挡住"改了内存对象以为已持久化"这类静默 bug。
    """

    session_id: str
    user_id: int
    role: str
    questions: List[Any]
    question_status: List[str]
    user_answers: List[Any]
    current_index: int
    last_seq: int
    version: int
    status: str
    created_at: str
    updated_at: str
    expires_at: str
    #: 上次响应正文，供 `seq` 幂等重放（ADR-004 第 0 步 / ADR-023）。
    last_reply: Optional[str] = None
    #: `completed` / `timeout` / `manual`；仅终态有值（ADR-007R）。
    ended_reason: Optional[str] = None
    #: 评分报告（JSON 文本）。由 `finish()` 与置终态**同一短事务**写入
    #: （ADR-004 报告路径："报告落库与置 finished 同事务"）。
    #:
    #: ⚠️ **T-19 补加**：ADR-004 明确写的是
    #: `UPDATE interview_sessions SET report=:json, status='finished', ...`，
    #: 但 §6.2 的建表语句里**没有 report 列** —— ADR 与 DDL 互相矛盾。
    #: T-16 的协议又已经声明了 `finish(..., report_json, ...)`，两处对不上。
    #: 经确认以 ADR-004 为准，由迁移 004 补上该列。
    report: Optional[str] = None

    @property
    def total_questions(self) -> int:
        return len(self.questions)

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE

    def is_expired(self, now: str) -> bool:
        """字典序比较即时间序比较 —— 前提是双方都是同格式 UTC ISO 串。"""
        return self.expires_at <= now

    def summary(self) -> dict:
        """409 响应体用的最小摘要（ADR-022 R-10）。"""
        return {
            "session_id": self.session_id,
            "current_index": self.current_index,
            "last_seq": self.last_seq,
            "total": self.total_questions,
            "status": self.status,
        }


@dataclass(frozen=True)
class SessionDraft(object):
    """新建会话所需的全部字段（由 `start_interview` 构造）。"""

    session_id: str
    user_id: int
    role: str
    questions: List[Any]
    question_status: List[str]
    user_answers: List[Any]
    current_index: int
    created_at: str
    updated_at: str
    expires_at: str
    last_seq: int = 0
    last_reply: Optional[str] = None
    status: str = SessionStatus.ACTIVE
    version: int = 0


@dataclass(frozen=True)
class TurnCommit(object):
    """一轮对话的**乐观锁写入意图**（ADR-004 第 3 步）。

    全部列都是"基于快照算出的**期望值**"，而不是 `current_index + 1`
    这类增量 —— 增量在多 worker 下会用陈旧载荷覆盖他人已提交的状态
    （v2.1 的关键更正：冲突必须显式暴露为 409，绝不能静默重放）。
    """

    session_id: str
    expected_version: int
    current_index: int
    question_status: List[str]
    user_answers: List[Any]
    last_seq: int
    #: 供幂等重放；反问路径下也可能有值。
    last_reply: Optional[str]
    updated_at: str
    #: 过期守卫用的"现在"：`WHERE expires_at > :now`（ADR-004 第 3 步）。
    now: str


@dataclass(frozen=True)
class CommitResult(object):
    """乐观锁写入结果。

    `applied=False` 表示 `rowcount == 0`（version 不匹配 / 已非 active / 已过期）。
    此时调用方**不得重试写**，应返回 **409**，并把 `snapshot`
    （存储实现顺手取到的最新状态）放进响应体。
    """

    applied: bool
    snapshot: Optional[SessionSnapshot] = None


# ===========================================================================
# 协议
# ===========================================================================


@runtime_checkable
class SessionStore(Protocol):
    """面试会话存储。

    **事务边界是硬要求**（ADR-004）：每个方法自己是一个**短事务**
    （目标 < 50ms）。调用方**绝不允许**把 AI 调用（25~30s）圈进
    `commit_turn` 所在的事务 —— 那会持写锁几十秒，
    在 WAL + `BEGIN IMMEDIATE` 下把所有请求堵死，
    正是 ADR-002/ADR-004 要消除的 `database is locked`。

    正确时序（`/api/chat`）：

        0. 事务外 → find_replay(seq)      命中则直接返回，不调 AI、不推进
        1. 事务外 → get() 取快照           非 active / 已过期 → 409 + 摘要
        2. 事务外 → 调 DeepSeek            ← 不持任何写锁
        3. 短事务 → commit_turn(TurnCommit)  applied=False → 409，不重试
    """

    # ---------------- 读 ----------------

    def get(self, session_id: str) -> Optional[SessionSnapshot]:
        """取快照；不存在返回 `None`（不抛异常，便于"取不到就 404"的调用方）。"""
        ...

    def get_active(self, user_id: int, now: str) -> Optional[SessionSnapshot]:
        """取该用户**未过期**的 active 会话；没有返回 `None`。

        只对 `status='active' AND expires_at > now` 生效 —— 已过期的行
        即使状态仍是 active 也**不算活跃**（ADR-022 惰性判定的口径统一在此）。
        """
        ...

    def find_replay(self, session_id: str, seq: int) -> Optional[ReplayLookup]:
        """`seq` 幂等前置去重（ADR-004 第 0 步）。

        返回
        ----
        `None`
            会话不存在。调用方应返回 404 / 409，**不要**继续往下走。
        `ReplayLookup(is_replay=False)`
            这是一个新序号，继续正常流程（取快照 → 事务外调 AI → 短事务写入）。
        `ReplayLookup(is_replay=True, reply=...)`
            客户端在重发（`seq <= last_seq`）。调用方据此**直接响应、
            不调用 AI、不推进索引** —— 这是"避免重复计费"的唯一关卡。

        注意返回的是**结构体而不是裸 `str`**：见 `ReplayLookup` 的说明，
        用 `None` 兼表两义会导致重复计费。
        """
        ...

    def get_last_ended(self, user_id: int) -> Optional[SessionSnapshot]:
        """取该用户**最近结束**（`finished` / `abandoned`）的会话；没有返回 `None`。

        T-27 新增。存在的理由是一条**硬需求**：ADR-007R 裁决"超时仍要出报告"，
        而超时后会话已经是 `abandoned` —— `get_active()` 永远取不到它，
        报告就无从生成（这正是"超时不出报告"在存储层的根因）。

        与 `get_active` 的分工：
          * `get_active` 回答"**现在**在面试吗"（只认未过期的 active）；
          * `get_last_ended` 回答"**刚刚**那场面试是怎么结束的"。

        排序 `updated_at DESC, created_at DESC`：终态行的 `updated_at` 就是
        它结束的时刻，这才是"最近结束"；按 `created_at` 取到的是"最近开始"。

        注意：**不**按"有没有报告"过滤 —— "这场面试要不要出报告"是业务判断，
        属调用方；存储层只如实返回最近结束的那一行。
        """
        ...

    # ---------------- 自愈（ADR-022 R-10）----------------

    def abandon_expired_for_user(self, user_id: int, now: str) -> int:
        """把该用户**已过期但仍 active** 的行置为 `abandoned`，返回影响行数。

        为什么必须做：这样的行**仍占着部分唯一索引**，会造成
        "`start_interview` 返回 409，但 `/api/interview/session` 又返回 null"
        的自相矛盾。`start_interview` 必须在插入前先调用本方法。
        """
        ...

    # ---------------- 写（各自一个短事务）----------------

    def create(self, draft: SessionDraft) -> SessionSnapshot:
        """新建会话。

        抛出
        ----
        ActiveSessionExists
            该用户已有 active 会话（部分唯一索引拒绝）。调用方**必须先**调用
            `abandon_expired_for_user()` 自愈，再捕获本异常返回 409。
        """
        ...

    def commit_turn(self, commit: TurnCommit) -> CommitResult:
        """以乐观锁提交一轮对话（ADR-004 第 3 步）。

        条件：`session_id = :sid AND version = :expected AND status = 'active'
        AND expires_at > :now`；成功时 `version = version + 1`。

        返回 `CommitResult(applied=False)` 表示条件不满足 —— 调用方返回 409，
        **不得重试写**。
        """
        ...

    def finish(self, session_id: str, expected_version: int, report_json: str,
               ended_reason: str, now: str) -> CommitResult:
        """置为 `finished` 并落库报告（ADR-004 报告路径）。

        AI 评分必须在**事务外**完成；本方法只做"报告落库 + 置终态 + version+1"
        这一个短事务，二者必须同事务（ADR-004 事务边界）。
        `ended_reason` 取 `EndedReason.ALL` 之一。
        """
        ...

    def attach_report(self, session_id: str, expected_version: int,
                      report_json: str, now: str) -> CommitResult:
        """给**已经结束**（`abandoned`）的会话补写报告，**不改状态**（T-27）。

        为什么不能复用 `finish()`：`finish` 会把状态置为 `finished`，而 ADR-022R
        明确裁决"超时 → `abandoned`，**不是** `finished`（面试并未正常完成）"。
        借用 `finish` 落库报告，等于用报告路径污染状态语义。

        为什么必须有它：ADR-007R 要求"超时仍要出报告"，此时会话已是 `abandoned`。
        报告与状态是**两个正交的事实**（见 `SessionStatus` 说明）。

        条件：`session_id = :sid AND version = :expected AND status = 'abandoned'
        AND report IS NULL`；成功时 `version = version + 1`。

        `report IS NULL` 是**防覆盖**守卫：报告一旦写出就不再重写
        （重算一次要多花一次 AI 调用，而且会覆盖用户已经看过的结论）。
        `applied=False` 表示条件不满足，调用方返回 409 / 复用已有报告，
        **不得重试写**。
        """
        ...

    def abandon(self, session_id: str, expected_version: int,
                ended_reason: str, now: str) -> CommitResult:
        """置为 `abandoned`（用户主动放弃 / 超时 / TTL 到期），释放唯一锁。

        `ended_reason` 取 `EndedReason.MANUAL`（用户点击）或
        `EndedReason.TIMEOUT`（超时归零）。
        """
        ...

    def abandon_all_expired(self, now: str) -> int:
        """把所有已过期却仍 active 的会话置为 `abandoned`，返回影响行数。

        供 T-22 的定时清理使用（systemd timer，单实例）。
        注意：**不删除行** —— `interview_sessions` 是"进行中的会话"，
        历史成就由 `interview_records` 承载；此处只保证唯一锁被释放。
        """
        ...


@runtime_checkable
class CaptchaStore(Protocol):
    """验证码存储（ADR-014）。

    语义要点（与 `auth.py` 现状一致，迁移时不得改变）：
      * TTL 到期即失效；
      * **只有校验成功才消费**（`used=True` 或直接删除）——
        输错不消费，允许用户在有效期内重试；
      * 校验成功后再校验同一个 `captcha_id` 必须失败（一次性）。
    """

    def save(self, captcha_id: str, code: str, expires_at: str) -> None:
        """保存一个新验证码。`expires_at` 为 ISO 串。"""
        ...

    def verify_and_consume(self, captcha_id: str, code: str, now: str) -> bool:
        """校验验证码；**成功时消费**它。

        返回 `True` 仅当：存在、未消费、未过期、且 `code` 匹配。
        返回 `False` 的所有情形都**不消费**（含"输错"与"不存在"）。
        """
        ...

    def purge_expired(self, now: str) -> int:
        """删除已过期的验证码，返回删除行数（供 T-22 定时清理）。"""
        ...


@runtime_checkable
class RateLimitStore(Protocol):
    """登录失败限流（ADR-014）。

    **只记录失败，成功不写** —— 这是刻意的：登录成功是高频路径，
    若成功也写就会把它变成写热点，与 ADR-002 的"降低写竞争"目标冲突。
    """

    def count_failures(self, ip: str, since: str) -> int:
        """统计该 IP 在 `since`（含）之后的失败次数 —— 滑动窗口的实现基础。"""
        ...

    def record_failure(self, ip: str, at: str) -> None:
        """记录一次失败。**只在失败时调用。**"""
        ...

    def clear(self, ip: str) -> None:
        """清除该 IP 的失败记录（登录成功后调用，让用户立刻恢复）。"""
        ...

    def purge_older_than(self, before: str) -> int:
        """删除 `before` 之前的记录，返回删除行数（供 T-22 定时清理）。"""
        ...
