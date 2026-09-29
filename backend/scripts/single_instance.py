"""单实例互斥锁（跨平台）。

提取自 T-22 的 `scripts/cleanup.py`，供 `cleanup.py` 与 T-52 的
`scripts/rotate_backup.py` **共用同一套语义** —— 备份与清理是同族问题：
都是"外部定时器 + 一次性进程"，都必须防止上一轮没跑完时被再触发一次，
也都必须"失败也要放锁"（否则一次崩溃会把后续 1 小时全挡掉）。

## 设计要点（与 cleanup.py 当年的取舍一致）

**`O_CREAT|O_EXCL` 原子创建**：两个进程同时调用只有一个成功，
不需要 flock（Windows 上语义不同）、不需要 PID 文件协议。

**PID 只用于排查，不用来判断进程存活**。`os.kill(pid, 0)` 在 Windows 上
不是"检查是否存在"，而是会真的去终止进程 —— 典型的"看着跨平台、
其实语义不同"的 API。取而代之的是**按锁文件年龄判断**：比
`stale_after_seconds` 还老就认为是上次崩溃留下的，直接抢占并在日志里说明。
代价是崩溃后最长 `stale_after_seconds` 内不会再跑，可接受 ——
清理与备份都是幂等的兜底动作，晚一次没有正确性影响。

## 与业务脚本的约定

| 返回 | 含义 |
|---|---|
| `acquire_lock() is True` | 拿到锁，调用方**必须**在 `finally` 里 `release_lock()` |
| `acquire_lock() is False` | 已有实例在跑，调用方应打印"跳过"并返回退出码 **3** |
"""

import datetime
import errno
import os

#: 锁文件超过这个年龄视为上次崩溃遗留，可抢占。
DEFAULT_STALE_AFTER_SECONDS = 60 * 60

#: 备份脚本用的锁后缀（与清理的 `.cleanup.lock` 分开：
#: 两者跑的是不同的库操作，没理由互相阻塞）。
BACKUP_LOCK_SUFFIX = ".rotate-backup.lock"

#: 清理脚本用的锁后缀（保持 T-22 原值，勿改 —— 老版本的锁文件还在的话
#: 改名字等于把"上一轮还在跑"这件事忘掉）。
CLEANUP_LOCK_SUFFIX = ".cleanup.lock"


def acquire_lock(lock_path, stale_after=DEFAULT_STALE_AFTER_SECONDS, now=None,
                 log=print):
    """尝试取锁。成功返回 True；已有实例在跑返回 False。

    `now` 可注入（测试用）；不传则取本机当前时间。
    `log` 收到的是**人类可读的中文说明**，便于在 journalctl 里看出
    "这次到底是跳过了还是抢占了遗留锁"。
    """
    moment = now or datetime.datetime.now()
    payload = "%d\n%s\n" % (os.getpid(), moment.isoformat())
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        try:
            age = moment.timestamp() - os.path.getmtime(lock_path)
        except OSError:
            age = 0
        if age < stale_after:
            log("  已有另一个实例在运行（锁文件 %s，%.1f 秒前创建）—— 本次跳过"
                % (os.path.basename(lock_path), age))
            return False
        log("  发现 %.0f 秒前的遗留锁（疑似上次崩溃）—— 抢占" % age)
        try:
            os.remove(lock_path)
        except OSError:
            pass
        # 抢占后重试一次。若此刻别人正好抢先，下一轮的 age 会接近 0 -> 返回 False，
        # 因此不会无限递归。
        return acquire_lock(lock_path, stale_after, now, log)

    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def release_lock(lock_path):
    """放锁。**幂等**：文件已经不在也算成功（否则 except 分支里再抛一次
    会盖掉真正的错误原因）。"""
    try:
        os.remove(lock_path)
    except OSError:
        pass


def lock_path_for(target_path, suffix):
    """锁文件路径 = 目标文件绝对路径 + 后缀。

    放在目标文件旁边（而不是 /tmp）是有意的：`ProtectSystem=strict` 下
    `/tmp` 是 `PrivateTmp`，放那儿的话同一台机器上手动跑与 systemd 跑
    会各自看到**不同的锁**，防重入形同虚设。
    """
    return os.path.abspath(target_path) + suffix
