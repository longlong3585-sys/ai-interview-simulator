# T-52 人工验收：每日备份 + 保留 7 份（ADR-019 / NFR-8）

> 对应任务：`docs/03-tasks.md` 的 **T-52**（依赖 T-22；本次解冻执行）
> 上游：`docs/archive/23-manual-verification.md`（T-22 的清理任务 —— 本任务与它**共用同一套
> 单实例锁语义**，因此这次把锁实现抽成了独立模块）
> 交付物（后端）：`scripts/rotate_backup.py`（新增）、`scripts/single_instance.py`（新增，
> 从 `scripts/cleanup.py` 抽出）、`scripts/cleanup.py`（改为委派，行为不变）、
> `tests/test_backup_rotation.py`（新增 57 项）、`tests/test_systemd_units.py`（17 → 34 项）
> 交付物（运维）：`deploy/systemd/backup.service`、`deploy/systemd/backup.timer`（新增）、
> `deploy/systemd/README.md`（重写，含备份 + 恢复 + 排错）
> 交付物（验收）：`backend/scripts/verify_t52_manual.py`（新增，离线一键）

## 1. 这次到底做了什么

ADR-019 要求"每日 `sqlite3 .backup` + 保留 7 份 + 含 `uploads/` 与加密 `.env`"。
原先只有 T-01 的 `make_backup.py` —— 那是**手动**打回滚点用的（还带源码 zip），
不是"每天自动跑、只留 7 份"的持续性保护。T-52 补上后者。

| 环节 | 落点 |
|---|---|
| 每日触发 | `deploy/systemd/backup.timer`：`OnCalendar=*-*-* 03:30:00` + `Persistent=true`（停机补跑） |
| 备份动作 | `scripts/rotate_backup.py`：库 + `uploads.zip` + `.env` + `manifest.json` 打成一个**自包含目录** |
| 保留策略 | `--keep 7`：每次落盘后删掉最老的，稳态 7 个目录 |
| 原子性 | 先写 `.incomplete-<stamp>/`，最后一步 `os.replace()` 改名 ⇒ 时间戳目录永远是完整的 |
| 可恢复性 | 写完**立刻回读校验**；`manifest.json` 里固化原样可粘贴的恢复命令 |
| 单实例 | 与清理共用 `single_instance.py`（`O_CREAT\|O_EXCL` 文件锁，后缀 `.rotate-backup.lock`） |
| 退出码 | 0 正常 / 1 `--verify` 发现坏备份 / 2 出错 / **3 已有实例在跑（不算失败）** |

四个刻意的设计决定：

* **`.env` 读不到时备份仍然成功（只告警）**。数据库与上传物是**不可再生**的；因为一个
  配置文件的属主/权限漂移就把整次备份判失败，会让"备份有没有跑成"这个信号被污染，
  反而掩盖真正的数据备份是否成功。想要硬失败就加 `--require-env`（部署前自检用）。
* **备份产物必须单文件**（沿用 T-17 的结论）。`src.backup(dst)` 会把 WAL 标志一起复制，
  于是备份库也是 WAL 模式，只读打开就会生出 `-wal`/`-shm` —— 拿到备份的人会不确定
  要不要一起拷。恢复演练专门验证"**只拿 `.db` 一个文件**仍能打开"。这里不是"文件在就算成功"。
* **`keep` 指运行结束后的目录总数**。首版把预算写成 `keep - 受保护数`，`keep=1` 时
  第一轮就把唯一的历史备份删了（"留 1 份"被实现成"旧的一律不要"）。现在把推导写在
  `prune()` 的注释里，并有一条专门的回归用例 `test_keep_one_keeps_only_the_newest`。
* **`verify` 校验的是"内容"而不是"数量"**。把 `uploads.zip` 换成**条目数相同**的另一个
  zip，早期实现会报 OK —— 那是"看起来正常实则错误"。现在 manifest 记录每个条目的
  sha256，逐条比对。

## 2. 傻瓜验证（一条命令，不起服务、不联网、不动仓库文件）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t52_manual.py
```

可选参数：`--skip-full-tests`（跳过约 90 秒的全量测试）、`--keep-temp`（保留临时目录排障）。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| A | 静态核对：7 个交付物存在；`Unit=` 指向真实文件；`ProtectHome` 不是 `true`；`--keep 7`；`SuccessExitStatus=0 3`；`cleanup.service` 未被改坏 | 全部 PASS |
| B | ① 新增测试 91 项；② **全量** 671 项 | 两段都 `OK` |
| C | **独立复核**（本脚本自己重跑关键语义，不用测试里的断言）：8 天真实备份 + 保留 7 份 + dry-run 口径 + 锁跳过 + **恢复演练** | 13 条全 PASS |
| D | 7 类损坏的**破坏性探针**（只在副本上做） | 7/7 被抓住，原备份未被弄脏 |

### 最近一次实跑结果（真实输出）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t52_manual.py

A 段 · 交付物与关键配置（静态，可离线）        [PASS]
B 段 · 自动化测试                            [PASS]  新增 91 项 OK；全量 671 项 OK
C 段 · 独立复核（真跑备份 + 恢复演练）         [PASS]
  连跑 8 天之后恰好保留 7 份：20261001_033000 … 20261007_033000
  被删的是最老的一份（20260930_033000）
  前 7 天一份都没删、第 8 天才删第 1 份（判据没写反）
  备份库是单文件（没有 -wal/-shm 附属文件）
  备份库 integrity_check=ok，journal_mode=delete
  uploads.zip 里就是那个头像文件，内容逐字节一致
  `.env` 读不到时：数据备份照做、有告警、退出码 0
  --require-env 时同一情形判失败（退出码 2）
  --dry-run 没有写文件、也没有删目录（3 份原样）
  dry-run 预告要删的 ['20261001_033000'] 与真跑删掉的完全一致
  已有实例持锁时：退出码 3（跳过）且没有产生任何备份
  恢复演练：只把 interview.db 复制到干净目录，仍 integrity_check=ok、users=3 records=4
  上传物恢复演练：解压后头像与原文件逐字节一致
D 段 · 破坏性探针（7 类损坏都必须被抓住）      [PASS]
  篡改数据库内容 / 截断数据库 / 删 manifest / manifest 改行数 /
  换同数量的另一个 zip / zip 变垃圾 / 删 .env   —— 7/7 被抓住
  验收结束后，原始备份仍然健康（探针只在副本上做）

汇总：A PASS / B PASS / C PASS / D PASS
✅ T-52 人工验收通过（每日备份 + 保留 7 份 + 恢复演练）
退出码 0
```

## 3. 服务器上怎么部署（摘要，完整版见 `deploy/systemd/README.md`）

```bash
APP=/root/ai-interview-simulator      # 阿里云旧站的真实路径

# 1) 改路径（service 里的 WorkingDirectory / ExecStart / ReadWritePaths / User）
vi $APP/deploy/systemd/backup.service
vi $APP/deploy/systemd/cleanup.service

# 2) 装单元
sudo cp $APP/deploy/systemd/{cleanup,backup}.{service,timer} /etc/systemd/system/

# 3) 备份目录要存在且运行用户可写（否则备份必然失败）
sudo mkdir -p $APP/backup && sudo chown -R interview:interview $APP/backup && sudo chmod 700 $APP/backup

# 4) 先手工跑一次
sudo systemctl start backup.service
journalctl -u backup.service -n 60 --no-pager
ls -l $APP/backup/

# 5) 启用
sudo systemctl daemon-reload
sudo systemctl enable --now cleanup.timer backup.timer
systemctl list-timers cleanup.timer backup.timer --no-pager
```

### Linux 上还没验的两件事（如实声明，非遗漏）

开发机是 **Windows、没有 systemd**，所以下面两条只能等服务器部署时确认；
本次验收用**静态检查**（B-① 的 34 项）钉住了它们最易错的写法：

1. **`backup.timer` 真的到点触发**：`systemctl list-timers backup.timer` 有下次触发时间，
   且 `journalctl -u backup.service` 里出现 `BACKUP_OK`。
2. **`Persistent=true` 的补跑**：关机期间错过的触发在开机后补跑一次
   （手工验证：`sudo systemctl stop backup.timer; sudo systemctl start backup.timer`
   观察是否立即补跑）。

`backup.service` 特意用 `ProtectSystem=full` + `ProtectHome=read-only`，**不用**
`ProtectHome=true`：旧站的项目根在 `/root/ai-interview-simulator`，设为 `true` 会让
`/root` 对服务不可见，备份连 `interview.db` 都读不到，而日志只说"数据库不存在" ——
这类"配置对了但读不到"的问题排查成本极高，所以有一条专门的回归用例
（`test_protect_home_is_not_true`）钉住它。

## 4. 已知边界（刻意如此）

* **不驱动真实 systemd**：见上。timer 行为与补跑在服务器上验。
* **不测跨天时钟跳变**：8 天备份用注入时钟（`run_backup(now=...)`）在几秒内跑完，
  测的是保留策略本身。时钟被往前调的情形由 `protect` 参数保底
  （`test_just_created_backup_is_never_pruned`）。
* **不校验磁盘配额**：本任务证明"备份正确、能恢复"，不证明"磁盘装得下 7 份"。
  每份约等于 `interview.db + uploads` 的大小，7 份就是 7 倍 —— 部署时请
  `du -sh $APP/backup/` 并核对磁盘余量；不够就把 `--keep` 调小并在别处留冷备。
* **备份不含源码**：代码在 GitHub 上（这是 T-52 与 `make_backup.py` 的分工差异，
  后者的源码 zip 仍然保留给回滚点用）。
