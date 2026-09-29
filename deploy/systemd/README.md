# systemd 定时任务 · 部署说明（清理 + 备份）

> **开发环境是 Windows、没有 systemd**，因此本目录的文件**只在 Linux 服务器上部署时使用**。
> 在 Windows 上请直接用单次手动执行：
> ```powershell
> cd backend
> .\venv\Scripts\python.exe scripts\cleanup.py --dry-run        # 先看要清什么
> .\venv\Scripts\python.exe scripts\cleanup.py                  # 再跑
> .\venv\Scripts\python.exe scripts\rotate_backup.py --dry-run  # 先看要备份到哪
> .\venv\Scripts\python.exe scripts\rotate_backup.py            # 再跑（真写盘）
> .\venv\Scripts\python.exe scripts\rotate_backup.py --verify   # 校验已有备份能否恢复
> ```

## 文件

| 文件 | 作用 | 由哪个任务引入 |
|---|---|---|
| `cleanup.service` | `Type=oneshot`，跑一次 `scripts/cleanup.py` 后退出 | T-22 |
| `cleanup.timer` | 每 15 分钟触发一次 `cleanup.service`；开机补跑错过的触发 | T-22 |
| `backup.service` | `Type=oneshot`，跑一次 `scripts/rotate_backup.py --keep 7` 后退出 | T-52 |
| `backup.timer` | 每日 03:30 触发一次 `backup.service`；开机补跑错过的触发 | T-52 |

两条定时任务的分工：**清理负责"删"，备份负责"留"**。它们写同一个目录下的
不同文件，互不阻塞（锁文件后缀也不同：`.cleanup.lock` / `.rotate-backup.lock`）。

---

## 安装步骤（两个 timer 一起装）

```bash
# 0) 真实部署路径（阿里云旧站）：/root/ai-interview-simulator
#    先把下面所有 /root/ai-interview-simulator 换成你的实际路径。
APP=/root/ai-interview-simulator

# 1) 按实际路径改这四处（service 文件里的 WorkingDirectory / ExecStart /
#    ReadWritePaths / User）：
vi $APP/deploy/systemd/cleanup.service
vi $APP/deploy/systemd/backup.service

# 2) 放到 systemd 目录
sudo cp $APP/deploy/systemd/cleanup.{service,timer} /etc/systemd/system/
sudo cp $APP/deploy/systemd/backup.{service,timer} /etc/systemd/system/

# 3) 让备份目录存在且运行用户可以写（**否则备份必然失败**）
sudo mkdir -p $APP/backup
sudo chown -R interview:interview $APP/backup
sudo chmod 700 $APP/backup        # 备份里有 .env 与用户上传物，不给别人看

# 4) 先手工干跑一次 service，确认退出码为 0（或 3 = 已有实例在跑，也算正常）
sudo systemctl start cleanup.service
systemctl status cleanup.service --no-pager
journalctl -u cleanup.service -n 30 --no-pager

sudo systemctl start backup.service
systemctl status backup.service --no-pager
journalctl -u backup.service -n 60 --no-pager
ls -l $APP/backup/

# 5) 启用定时器
sudo systemctl daemon-reload
sudo systemctl enable --now cleanup.timer
sudo systemctl enable --now backup.timer

# 6) 确认已排期
systemctl list-timers cleanup.timer backup.timer --no-pager
```

---

## 备份（T-52 / ADR-019）

### 保留策略：`keep` 指的是**磁盘上的总份数**

`--keep 7` = 一次备份跑完后，`backup/` 下最多 7 个时间戳目录，
每次新备份落盘后删掉最老的那一份。连续跑 7 天之后达到稳态。

`backup/` 的结构：

```
backup/
├── 20260930_033000/            # 一个自包含的目录 = 一次备份
│   ├── interview.db            # sqlite3 backup API 快照（WAL 安全、单文件、integrity_check=ok）
│   ├── uploads.zip             # backend/uploads/ 全量
│   ├── .env                    # 密钥与配置（读不到时只告警，见下）
│   └── manifest.json           # 每个文件的 sha256 / 行数 / 恢复命令
├── 20261001_033000/
└── ...
```

`.incomplete-*` 前缀的目录是**没写完的半成品**（上次崩溃/断电留下的），
不参与保留计数，下次运行会自动清掉。出现它不表示备份坏了。

### 每天怎么确认它真的在跑

```bash
systemctl list-timers backup.timer --no-pager         # 下次/上次触发时间
journalctl -u backup.service -n 60 --no-pager        # 看 BACKUP_OK 与自校验结果
python $APP/backend/scripts/rotate_backup.py --verify --out $APP/backup
```

`--verify` 会逐个备份目录回读校验（数据库 integrity_check、行数、
`uploads.zip` 条目与逐条 sha256、`.env` sha256），全部通过退出码 0，
有任何一份不健康退出码 1 —— 可以挂进监控。

### 恢复一份备份

```bash
APP=/root/ai-interview-simulator
B=$APP/backup/20260930_033000

# 1) 停后端（必须先停，否则 SQLite 正在写的时候覆盖文件会得到损坏的库）
sudo systemctl stop ai-interview

# 2) 覆盖前先把"现在这份"留一份，免得恢复了错的备份就没退路
sudo cp $APP/backend/interview.db $APP/backend/interview.db.before-restore-$(date +%F-%H%M)

# 3) 恢复数据库 / 上传物 / 配置
sudo cp $B/interview.db   $APP/backend/interview.db
sudo unzip -o $B/uploads.zip -d $APP/backend/uploads/
sudo cp $B/.env           $APP/backend/.env && sudo chmod 600 $APP/backend/.env

# 4) 权限交给运行用户，然后启动
sudo chown -R interview:interview $APP/backend
sudo systemctl start ai-interview

# 5) 验一下
python $APP/backend/scripts/verify_backup.py --db $APP/backend/interview.db \
  --manifest $B/manifest.json
curl -s localhost:8000/api/health
```

`manifest.json` 里的 `restore_hint` 字段就是上面这段命令（路径已填好），
事故现场直接抄即可。

### `.env` 读不到时备份**不会**失败（刻意的）

原因：数据库与上传物是**不可再生**的数据；因为一个配置文件的属主/权限
漂移就把整次备份判失败，会让"备份有没有跑成"这个信号被污染，
反而掩盖真正的数据备份是否成功。

此时：备份照做，`manifest.json` 里 `env_file: null` + `env_warning` 写明原因，
日志有 `⚠️ .env 未备份`，退出码仍是 0。

想把它变成硬失败（例如上线前的自检）就加 `--require-env`：

```bash
python scripts/rotate_backup.py --require-env --out /root/ai-interview-simulator/backup
```

**部署后请确认 `.env` 是能读的**：

```bash
sudo -u interview test -r $APP/backend/.env && echo "可读" || echo "不可读 —— 去修属主/权限"
```

### 为什么 `ProtectHome` 不能写 `true`（踩过的坑）

`backup.service` 里刻意用 `ProtectSystem=full` + `ProtectHome=read-only`，
而不是常见的 `strict` + `true` 组合。原因是**旧站的项目根在 `/root` 下**：
`ProtectHome=true` 会让整个 `/root` 对服务不可见，于是备份脚本连
`interview.db` 都打不开，日志只会说"数据库不存在" —— 而文件明明在那里。
排查这种"配置对了但读不到"的问题非常费时间，所以这里只用 `full`。

---

## 清理（T-22）

### 为什么用 systemd timer 而不是 APScheduler

本项目的部署形态是 `uvicorn --workers 2`。若把调度器放进应用进程，
**每个 worker 都会跑一份** → 同一个清理任务被并发触发 2 次。
`sleep(900)` 那种写法同理，还会随 worker 数放大。
把它放到进程之外，就没有"几个 worker 就几份定时器"的问题。

`Type=oneshot` 还顺带提供了**第二层单实例保障**：systemd 在一个 unit
实例仍在运行时不会启动第二个。第一层是 `cleanup.py` 自己的
`O_CREAT|O_EXCL` 文件锁（跨平台，Windows 手动执行时也生效）。
备份脚本 `rotate_backup.py` 用的是**同一套锁实现**
（`scripts/single_instance.py`，T-52 抽出），只是后缀不同。

### `SuccessExitStatus=0 3` 是刻意的

`cleanup.py` 与 `rotate_backup.py` 在"已有另一个实例在跑"时都返回 **3**，
表示**本次跳过**。这**不是失败** —— 若不给 `SuccessExitStatus` 加上 3，
systemd 会把它记成 `failed`，监控就会持续误告警。加上之后 0 与 3 都算成功。

其余退出码：

| 码 | cleanup.py | rotate_backup.py |
|---|---|---|
| 0 | 正常完成（含"没有需要清理的东西"） | 正常完成（含 `--dry-run`、含 `.env` 读不到但数据备份成功） |
| 1 | —（不用） | `--verify` 发现已有备份不健康 |
| 2 | 参数/环境错误（如数据库文件不存在）—— **应当告警** | 同左（库不存在、写不进去、备份后 integrity_check 不是 ok） |
| 3 | 已有另一个实例在运行，本次跳过 | 同左 |

### 清理哪些数据

| 表 | 过期依据 | 动作 |
|---|---|---|
| `interview_sessions` | `expires_at <= now` | **置 `abandoned`**（不删除）—— 释放"同用户同时只能有一个 active 会话"的部分唯一索引 |
| `captcha_store` | `expires_at <= now` | 删除 |
| `auth_attempts` | `attempted_at < now - 10min` | 删除 |
| `token_blacklist` | `expires_at <= now` | 删除（T-51 起） |

**为什么会话只置 `abandoned` 而不删除**：ADR-007R 规定超时后**仍要出报告**，
会话行（含 `questions` / `user_answers`）在报告生成前必须存在；
删掉就等于丢数据。

**因此 `interview_sessions` 会只增不减** —— 这是一个**已知的开放项**，
不是遗漏。是否需要"归档 N 天前的终态会话"应当单独决策（涉及历史保留策略），
当前不在 T-22 范围内。

`token_blacklist` **已覆盖**（T-51 起）。此前"当前不清理"的理由是
T-16 刻意不定义该表协议（挂在 ADR-003 选 B 之下），而表是空的。
⚠️ 那条 WHERE 是本脚本里**最不能写错**的一行：删掉仍生效的吊销记录
等于让被吊销的令牌复活（`docs/02-arch-review.md` R-4）。
`tests/test_cleanup.py::test_blacklist_purge_never_removes_live_revocations`
专门钉住它。

---

## 排错速查

| 现象 | 多半是 | 怎么确认 |
|---|---|---|
| `backup.service` 退出码 2，日志说"数据库不存在" | `ProtectHome=true` 把 `/root` 藏了，或 `WorkingDirectory`/`ExecStart` 路径没改 | `systemctl cat backup.service` 看这两项；确认 `ProtectHome` 不是 `true` |
| 备份失败：Permission denied | `backup/` 不属于运行用户 | `ls -ld $APP/backup`，`chown interview:interview` |
| 日志里 `⚠️ .env 未备份` | `.env` 属主/权限不给运行用户读 | `sudo -u interview cat $APP/backend/.env` |
| `systemctl list-timers` 里没有 backup.timer | 忘了 `enable`，或 `Unit=` 写错 | `systemctl status backup.timer`；`systemctl cat backup.timer` 看 `Unit=` |
| 备份目录数量超出预期 | `keep` 没传（默认 7），或有人手工放进去非时间戳目录（轮转不碰它们） | `ls $APP/backup/`；`journalctl -u backup.service` 看每轮删了谁 |
| 磁盘被备份占满 | 每次备份 ≈ 库 + uploads 的大小；7 份就是 7 倍 | `du -sh $APP/backup/`；必要时 `--keep 3` 并在别处留冷备 |
