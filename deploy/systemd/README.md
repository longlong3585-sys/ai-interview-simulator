# systemd 定时清理 · 部署说明

> **开发环境是 Windows、没有 systemd**，因此本目录的文件**只在 Linux 服务器上部署时使用**。
> 在 Windows 上请直接用单次手动执行：
> ```powershell
> cd backend
> .\venv\Scripts\python.exe scripts\cleanup.py --dry-run   # 先看
> .\venv\Scripts\python.exe scripts\cleanup.py             # 再跑
> ```

## 文件

| 文件 | 作用 |
|---|---|
| `cleanup.service` | `Type=oneshot`，跑一次 `scripts/cleanup.py` 后退出 |
| `cleanup.timer` | 每 15 分钟触发一次 `cleanup.service`；开机补跑错过的触发 |

## 安装步骤

```bash
# 1) 按实际路径改这两处：
#    cleanup.service 里的 WorkingDirectory / ExecStart / ReadWritePaths
#    （示例用 /opt/interview/backend，User=interview）
sudo vi /opt/interview/deploy/systemd/cleanup.service

# 2) 放到 systemd 目录
sudo cp /opt/interview/deploy/systemd/cleanup.{service,timer} /etc/systemd/system/

# 3) 先手工干跑一次 service，确认退出码为 0（或 3 = 已有实例在跑，也算正常）
sudo systemctl start cleanup.service
systemctl status cleanup.service --no-pager
journalctl -u cleanup.service -n 30 --no-pager

# 4) 启用定时器
sudo systemctl daemon-reload
sudo systemctl enable --now cleanup.timer

# 5) 确认已排期
systemctl list-timers cleanup.timer --no-pager
```

## 为什么用 systemd timer 而不是 APScheduler

本项目的部署形态是 `uvicorn --workers 2`。若把调度器放进应用进程，
**每个 worker 都会跑一份** → 同一个清理任务被并发触发 2 次。
`sleep(900)` 那种写法同理，还会随 worker 数放大。
把它放到进程之外，就没有"几个 worker 就几份定时器"的问题。

`Type=oneshot` 还顺带提供了**第二层单实例保障**：systemd 在一个 unit
实例仍在运行时不会启动第二个。第一层是 `cleanup.py` 自己的
`O_CREAT|O_EXCL` 文件锁（跨平台，Windows 手动执行时也生效）。

## `SuccessExitStatus=0 3` 是刻意的

`cleanup.py` 在"已有另一个实例在跑"时返回 **3**，表示**本次跳过**。
这**不是失败** —— 若不给 `SuccessExitStatus` 加上 3，systemd 会把它记成
`failed`，监控就会持续误告警。加上之后 0 与 3 都算成功。

其余退出码：
| 码 | 含义 |
|---|---|
| 0 | 正常完成（含"本次没有需要清理的东西"） |
| 2 | 参数/环境错误（如数据库文件不存在）—— **应当告警** |
| 3 | 已有另一个实例在运行，本次跳过 |

## 部署后怎么确认它真的在跑

```bash
# 看定时器下次触发时间与上次触发时间
systemctl list-timers cleanup.timer --no-pager

# 看最近几次执行的输出（清理通知的三类计数都打在日志里）
journalctl -u cleanup.service -n 50 --no-pager

# 手动触发一次并立刻看结果
sudo systemctl start cleanup.service && journalctl -u cleanup.service -n 10 --no-pager
```

## 清理哪些数据

| 表 | 过期依据 | 动作 |
|---|---|---|
| `interview_sessions` | `expires_at <= now` | **置 `abandoned`**（不删除）—— 释放"同用户同时只能有一个 active 会话"的部分唯一索引 |
| `captcha_store` | `expires_at <= now` | 删除 |
| `auth_attempts` | `attempted_at < now - 10min` | 删除 |

**为什么会话只置 `abandoned` 而不删除**：ADR-007R 规定超时后**仍要出报告**，
会话行（含 `questions` / `user_answers`）在报告生成前必须存在；
删掉就等于丢数据。

**因此 `interview_sessions` 会只增不减** —— 这是一个**已知的开放项**，
不是遗漏。是否需要"归档 N 天前的终态会话"应当单独决策（涉及历史保留策略），
当前不在 T-22 范围内。

## 已知未覆盖项

`token_blacklist` **已覆盖**（T-51 起）。此前"当前不清理"的理由是
T-16 刻意不定义该表协议（挂在 ADR-003 选 B 之下），而表是空的。
T-51 落地后协议已存在（`TokenBlacklistStore`）、表开始有数据，
`cleanup.py` 因此补上了第四个动作：只删 `expires_at <= now` 的记录。

⚠️ 这条 WHERE 是本脚本里**最不能写错**的一行：删掉仍生效的吊销记录
等于让被吊销的令牌复活（`docs/02-arch-review.md` R-4）。
`tests/test_cleanup.py::test_blacklist_purge_never_removes_live_revocations`
专门钉住它。
