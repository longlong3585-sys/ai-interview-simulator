# 阿里云旧站升级 Runbook（T-52 / T-53 上线手册）

> **适用环境**：阿里云 Ubuntu 22.04，2 核 2G，公网 IP `112.124.54.223`，
> 项目根 `/root/ai-interview-simulator`。旧站是**手动启动的 uvicorn**（无 systemd），
> Nginx 已指向 `frontend/dist` 并反代本机 8000。
>
> **配套交付**：T-52 备份体系（`deploy/systemd/backup.*`）、
> T-53 Nginx 同源托管（`deploy/nginx/*`）。
> 配置文件的逐条解释见 `deploy/nginx/README.md` 与 `deploy/systemd/README.md`。

---

## 0. 怎么用这份手册

* **每一步都是"命令 → 预期输出 → 不通过怎么办"**。看到预期输出再往下走；
  对不上就停在第几步，不要"先往下走看看"。
* 命令里的粘贴符是**你自己的**终端；`sudo` 提示密码时就是服务器的 root 密码。
* 全程把 `APP` 当成项目根变量（第 1 步会定义）。
* **总停机窗口约 15~25 分钟**，主要花在第 4 步（迁移）与第 5 步（前端上传）。
  第 2~3 步（备份 + 拉代码）可以在**不停机**的情况下先做完。
* 唯一的"不可逆"动作是第 4 步的数据库迁移与第 2.3 步的 `git reset --hard`。
  两者都有回滚步骤，但**回滚要用到第 2 步的备份** —— 所以第 2 步没验证通过，
  绝对不要往下走。

### 关键背景：本项目的"数据"只有三样

| 数据 | 位置 | 谁会用 |
|---|---|---|
| SQLite 数据库 | `backend/interview.db`（+ `-wal`/`-shm`） | 用户、面试记录、报告、吊销名单 |
| 用户上传物 | `backend/uploads/avatars/` | 头像 |
| 密钥与配置 | `backend/.env` | 已在 `.gitignore` 里，**git 不会动它** |

> ⚠️ **仓库根目录还有一个 `interview.db`**（历史遗留，不在 `backend/` 下）。
> 应用真正读的是 **`backend/interview.db`**（`database.py` 的默认值是
> `sqlite:///./interview.db`，相对于 uvicorn 的工作目录 = `backend/`）。
> 两个都备份，但**恢复时只恢复 `backend/` 里那个**。

### 全局止损规则

| 现象 | 动作 |
|---|---|
| 任何 `nginx -t` 失败 | **不要** `reload`，停在原地看提示行号 |
| 迁移脚本打印 `MIGRATION_ABORTED` | 走 §4.5 回滚，不要重跑 |
| 前端页面打开是白屏 | 看浏览器 Console 与 `journalctl -u ai-interview -n 50`，先走 §10.4 |
| 拿不准 | 执行 §11 整体回滚；**停机久一点永远好过数据丢一点** |

---

## 1. 预检（不停机，5 分钟）

### 1.1 定义变量（**每个新开终端都要先跑这一段**）

```bash
APP=/root/ai-interview-simulator
DOMAIN=你的域名           # ← 没有域名就先留空，第 6 步会教你申请一个免费的
STAMP=$(date +%F-%H%M)    # 本次升级的时间戳，用于命名备份
cd "$APP" || echo "路径不对！"
echo "APP=$APP STAMP=$STAMP DOMAIN=$DOMAIN"
```

**预期输出**：`APP=/root/ai-interview-simulator STAMP=2026-09-30-14-05 DOMAIN=...`

### 1.2 确认目录结构与服务现状

```bash
ls -la "$APP"
echo "--- 数据库与上传物 ---"
ls -lh "$APP"/backend/interview.db* 2>/dev/null
du -sh "$APP"/backend/uploads 2>/dev/null
echo "--- 8000 端口是谁在监听 ---"
ss -lntp | grep -E ':8000|:80|:443' || echo "（没有监听）"
echo "--- uvicorn 进程 ---"
ps -ef | grep -E "[u]vicorn" || echo "（没有 uvicorn 进程）"
echo "--- nginx ---"
nginx -v 2>&1
systemctl is-active nginx
echo "--- 系统 ---"
lsb_release -d; free -m | head -2; df -h / | tail -1
```

**预期输出要点（逐条核对）**：

* `ls` 里能看到 `backend/`、`frontend/`、`deploy/`、`docs/`、`README.md`；
* `backend/interview.db` 存在（可能还有 `-wal`/`-shm`，**正常**）；
* `du -sh uploads` 有实际大小（旧站有真人用过）；
* `ss` 里能看到 `:8000`（uvicorn）与 `:80`（nginx）；
* `nginx -v` 打印 `nginx version: nginx/1.xx.x` —— **把版本号记下来**，
  第 7 步要用（1.25.1 是 HTTP/2 写法的分界线）；
* `free -m` 的 `available` 应该还有几百 MB。少于 150MB 时先
  `sudo systemctl restart nginx` + 确认没有别的吃内存进程，否则迁移可能 OOM。

**不通过怎么办**：

* 8000 端口不是 uvicorn 在听 → `ss -lntp` 输出的进程名会告诉你，先搞清楚
  再往下（可能是另一个 app，覆盖代码会打坏它）。
* `df -h /` 剩余空间 **< 1GB** → 先清理，第 2 步要写两份备份（库 + uploads），
  空间不够会让备份中途失败。

### 1.3 确认 Python 与前端环境

```bash
"$APP"/backend/venv/bin/python -V 2>/dev/null || echo "⚠️ 没有 backend/venv"
"$APP"/backend/venv/bin/pip -V 2>/dev/null | head -1
ls "$APP"/backend/.env >/dev/null 2>&1 && echo ".env 存在" || echo "⚠️ .env 缺失！"
node -v 2>/dev/null || echo "（服务器上没装 node —— 这是好事，见第 5 步）"
```

**预期输出**：Python 3.10.x（Ubuntu 22.04 自带）或 3.8/3.9；
`.env 存在`；`node` 那行**期望是"没装"或版本很旧** ——
本项目的前端在**本地 Windows 构建**，服务器上不允许跑 `npm run build`（2G 内存会被打爆）。

### 1.4 检查 80 端口现有 nginx 站点配置（避免配冲突）

```bash
ls -l /etc/nginx/sites-enabled/
grep -rn "server_name" /etc/nginx/sites-enabled/ 2>/dev/null
grep -rn "proxy_pass\|root " /etc/nginx/sites-enabled/ 2>/dev/null | head -20
```

**预期输出**：能看到现有站点（可能就是 `default` 或某个自建 conf），
里面 `proxy_pass http://127.0.0.1:8000` 与 `root .../frontend/dist`。

**把文件名记下来**（例如 `default` 或 `ai-interview`）——
第 6 步启用新配置时要 `rm` 掉它，否则同一个 `server_name` 有两个 server 块，
nginx 不报错但**哪个生效取决于加载顺序**，属于"看起来能跑、其实随机"的配置。

---

## 2. 备份 + 强制拉取新代码（停机从这里开始，约 5 分钟）

> 这一节的顺序是**刻意**的：**先停服务 → 再备份 → 再动 git**。
> 运行中的 SQLite 直接 `cp` 可能拿到不一致的快照（WAL 里的数据没并回主文件），
> 而 `git reset --hard` 会重写工作区 —— 备份必须在它之前、且已完成校验。

### 2.1 停掉旧后端（先止损，再动数据）

```bash
# 找到手动启动的 uvicorn 进程
ps -ef | grep -E "[u]vicorn" 
```

**预期输出**：一行 `root ... python -m uvicorn main:app --host 0.0.0.0 --port 8000`
（参数可能略有不同，把这行的 **PID** 抄下来）。

```bash
# 优雅停止（用上面查到的 PID）
kill <PID>
sleep 3
ps -ef | grep -E "[u]vicorn" || echo "✅ uvicorn 已停止"
curl -s -o /dev/null -w "本机 8000 探测 HTTP=%{http_code}\n" http://127.0.0.1:8000/
```

**预期输出**：`✅ uvicorn 已停止`，末尾那行 `HTTP=000`（连不上 = 已停）。

**不通过怎么办**：

* `kill` 之后进程还在 → `kill -9 <PID>`（SQLite 对强杀是安全的，WAL 会自己恢复）。
* 进程是 `screen`/`tmux` 里起的 → `screen -ls` / `tmux ls` 里 `kill` 对应会话。
* 如果它其实是被 systemd 管的（`systemctl status | grep -i uvicorn`）→
  用 `sudo systemctl stop <名字>`，别用 `kill`（否则会被自动拉起）。

### 2.2 备份数据库、上传物与配置

```bash
cd "$APP"
mkdir -p backup/manual-${STAMP}
B=backup/manual-${STAMP}

# ① 数据库：用 SQLite 官方 backup API（WAL 安全、产物是单文件）
#    复用仓库里的 T-01/T-17 脚本，不是 cp
cp -a backend/interview.db "${B}/interview.db.bak-${STAMP}"
if [ -x backend/venv/bin/python ]; then
  if [ -f backend/scripts/make_backup.py ]; then
    backend/venv/bin/python backend/scripts/make_backup.py --label "pre-t53-${STAMP}" --no-archive \
      || echo "（脚本备份失败，稍后用 cp 的那份）"
  else
    echo "（仓库里还没有 make_backup.py —— 本步是首次拉代码前的备份，用 cp 的那份）"
  fi
fi

# ② 上传物：整目录归档
tar czf "${B}/uploads.tar.gz" -C backend uploads

# ③ 配置（含密钥）——单独一份，权限收紧
cp -a backend/.env "${B}/env.bak"
chmod 600 "${B}/env.bak"

# ④ 清单与校验和
{
  echo "created_at: $(date -Is)"
  echo "host: $(hostname)  ip: 112.124.54.223"
  echo "app_dir: $APP"
  echo "git_head_before: $(git -C "$APP" rev-parse HEAD 2>/dev/null || echo none)"
  echo "--- sizes ---"
  ls -l "${B}"
  echo "--- sha256 ---"
  sha256sum "${B}"/* 2>/dev/null
} | tee "${B}/MANIFEST.txt"
```

**预期输出**：`ls` 里至少有 4 个文件
（`interview.db.bak-<STAMP>`、`uploads.tar.gz`、`env.bak`、`MANIFEST.txt`），
外加 `make_backup.py` 可能生成的 `backup/interview_<时间戳>.db`。

### 2.3 ✅ 验证备份真的可用（**这一步不通过就绝对不要往下走**）

```bash
cd "$APP"
B=backup/manual-${STAMP}

# 库能打开、完整性 ok、行数打印出来
sqlite3 "${B}/interview.db.bak-${STAMP}" "PRAGMA integrity_check;"
sqlite3 "${B}/interview.db.bak-${STAMP}" \
  "SELECT 'users='||(SELECT COUNT(*) FROM users),
          'records='||(SELECT COUNT(*) FROM interview_records);"

# 并存的 T-52 体系产物（若已有）也能校验
[ -f backend/scripts/verify_backup.py ] && \
  backend/venv/bin/python backend/scripts/verify_backup.py --db "${B}/interview.db.bak-${STAMP}"

# uploads 归档可读
tar tzf "${B}/uploads.tar.gz" | head -5
tar tzf "${B}/uploads.tar.gz" | wc -l
```

**预期输出**：

```
ok
users=3 records=5          ← 行数必须与你库里的真实数量一致
VERIFY_OK backup/manual-.../interview.db.bak-...
uploads/avatars/...
uploads/avatars/user_1_xxx.jpg
(uploads 文件数)            ← 应与第 1.2 步 du 看到的规模相符，不是 0
```

> **行数记下来**。第 4 步迁移之后要再查一次**完全相同的数字** ——
> 迁移的安全闸之一就是"行数不得减少"，这里是你的独立对照。

**不通过怎么办**：

* `integrity_check` 不是 `ok` → **立刻停手**。这说明停止服务前的库可能已经被写坏，
  优先用云盘快照恢复，不要在坏库上继续升级。
* `users=` 是空/报错 → 说明备份文件是空的或不是数据库：检查
  `ls -l` 的大小，重做 2.2。
* `tar tzf` 报错 → uploads 归档坏了，重做 2.2 的 ②。

### 2.4 记下当前的 git 状态（回滚锚点）

```bash
cd "$APP"
git log --oneline -3
git rev-parse HEAD | tee backup/manual-${STAMP}/git_head_before.txt
git status --short | head -20
```

**预期输出**：最后一行是 `origin/main` 之前的某个老 commit 哈希。
**把它抄到纸上/记事本里** —— §11 回滚要用。

`git status` 若列出改了但没提交的文件，先看清楚是什么
（可能是服务器上直接改过的代码）。这些改动会被下一步的 `reset --hard`
**丢弃**，所以如果里面有重要改动，现在先 `cp` 一份出来。

### 2.5 强制拉取新代码（`git fetch --all && git reset --hard origin/main`）

> **为什么必须用 `reset --hard` 而不是 `git pull`**：
> 我们刚在 GitHub 上**强制覆盖**了旧提交（历史被重写），
> 服务器上的本地分支与新的 `origin/main` 没有共同祖先。
> 此时 `git pull` 会因为"分叉 / 冲突"直接失败，或者进入一个半合并状态
> —— 那比不动更糟。`fetch --all` + `reset --hard origin/main` 是
> "以远端为准、彻底覆盖本地"的标准做法。

```bash
cd "$APP"

# ① 先看清远端到底有什么，以及本地会丢掉什么
git fetch --all --prune
git log --oneline -3 origin/main
git diff --stat HEAD origin/main | tail -5

# ② 覆盖本地（工作区 + 索引都对齐 origin/main）
git reset --hard origin/main

# ③ 确认
git log --oneline -3
git rev-parse HEAD
git status --short
```

**预期输出**：

* ① 里 `origin/main` 的顶端是你在本地推上去的那个新 commit；
  `git diff --stat` 会列出将被改变的文件（**应该只看到源码/配置/文档，不应出现
  `backend/interview.db`、`backend/uploads/`、`backend/.env`** ——
  这三个都在 `.gitignore` 里，git 不会碰它们。**如果 diff 里出现了它们，立刻停手看 §2.6**）。
* ② `git reset --hard` 打印 `HEAD is now at <hash> <subject>`。
* ③ `git status --short` **输出为空**（干净）；`git rev-parse HEAD` 与上一步一致。

**不通过怎么办**：

* `git fetch` 报鉴权失败 → 仓库是私有的，需要配置 deploy key / token：
  `git remote -v` 看地址；HTTPS 的话用 `git config credential.helper store`
  并准备 PAT，或改成 SSH（`git@github.com:...`）并放好 `~/.ssh/id_ed25519`。
* `git reset --hard` 之后 `git status` 里有 `D backend/interview.db` → 
  **说明这个库被 git 跟踪了**（.gitignore 没生效）。立即执行：
  ```bash
  git checkout HEAD -- backend/interview.db || \
    cp backup/manual-${STAMP}/interview.db.bak-${STAMP} backend/interview.db
  ```
  然后核对 `sqlite3 backend/interview.db "PRAGMA integrity_check;"` 是 `ok`。

### 2.6 ✅ 确认数据文件完好（重置代码之后、动数据库之前）

```bash
cd "$APP"
echo "--- 关键文件是否还在 ---"
ls -lh backend/interview.db backend/.env
ls backend/uploads/avatars/ | head -3
ls backend/uploads/avatars/ | wc -l
echo "--- 库是否还能读 ---"
sqlite3 backend/interview.db "PRAGMA integrity_check;
  SELECT 'users='||(SELECT COUNT(*) FROM users),
         'records='||(SELECT COUNT(*) FROM interview_records);"
echo "--- .env 是否还是原来那份 ---"
diff -q backend/.env backup/manual-${STAMP}/env.bak && echo "✅ .env 未被改动"
git check-ignore -v backend/interview.db backend/.env backend/uploads/avatars
```

**预期输出**：三个文件都在；`ok` 且行数与 §2.3 完全一致；
`✅ .env 未被改动`；最后一条给出三条 ignore 规则（证明它们在版本控制之外）。

---

## 3. 安装新增的 Python 依赖（不停机，2 分钟）

### 3.1 先看新增了什么依赖

```bash
cd "$APP"
git log --oneline -1
echo "--- requirements.txt 的变化（相对你手里的旧版本） ---"
git diff "$(cat backup/manual-${STAMP}/git_head_before.txt)" HEAD -- backend/requirements.txt backend/requirements-dev.txt | head -60
```

**预期输出**：可能为空（没有新增依赖），也可能列出新增行。

> **实测结论（2026-09-30，本仓库）**：`backend/requirements.txt` 的 42 项
> **已经覆盖全部第三方 import**（fastapi / starlette / uvicorn / sqlalchemy /
> python-jose / passlib / bcrypt / python-dotenv / python-multipart /
> openai / python-docx / PyPDF2 / pillow / captcha / lxml / psycopg2-binary …）。
> **不需要 alembic** —— 本项目的迁移是自己实现的
> （`backend/migrations/runner.py` + `versions/00X_*.py`），不是 Alembic。
> 所以这一步大概率是"确认无新增"，但**仍要跑一遍安装**：
> 服务器现有的 venv 可能是半年前的，缺某个间接依赖就会在启动时炸。

### 3.2 安装（保留旧 venv，只增量安装）

```bash
cd "$APP/backend"

# 先升级 pip 自身，避免老 pip 解析 wheel 失败
venv/bin/python -m pip install --upgrade pip

# 增量安装运行时依赖（幂等：已装的会跳过）
venv/bin/python -m pip install -r requirements.txt

# 打印关键包版本，确认装上了
venv/bin/python -m pip list 2>/dev/null | grep -iE \
  "fastapi|uvicorn|sqlalchemy|jose|passlib|bcrypt|dotenv|multipart|openai|pypdf2|python-docx|pillow|captcha"
```

**预期输出**：结尾是 `Successfully installed ...` 或 `Requirement already satisfied`
若干行；最后 `pip list` 打印出 12 行左右的包与版本。

**不通过怎么办**：

* `pip install` 报网络超时 → 换阿里云镜像后重试：
  ```bash
  venv/bin/python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
  ```
* 报某个包编译失败（缺少 `gcc`/`python3-dev`）→
  ```bash
  sudo apt-get update && sudo apt-get install -y build-essential python3-dev libpq-dev
  ```
  再重跑上面的安装命令。
* `psycopg2-binary` 装不上且你并不用 PostgreSQL → 可以先注释掉它：
  本项目生产用 SQLite（`DATABASE_URL=sqlite:///./interview.db`），
  PostgreSQL 只是备选。**但要记下这个偏离**。

### 3.3 ✅ 确认应用能被 import（不启动服务）

```bash
cd "$APP/backend"
venv/bin/python -c "
import main
print('APP IMPORT OK')
print('  路由数：', len(main.app.routes))
"
```

**预期输出**：`APP IMPORT OK` + `路由数： 30` 左右的数字。

**不通过怎么办**：报错行的文件名与行号就是答案。常见两种：

* `ModuleNotFoundError: No module named 'xxx'` → 缺依赖，回到 3.2 单独装它。
* `RuntimeError: SECRET_KEY 未设置`（T-07 的 fail-fast）→ `.env` 丢了或损坏，
  用备份恢复：`cp backup/manual-${STAMP}/env.bak backend/.env && chmod 600 backend/.env`。

> ⚠️ import 成功不代表能跑（比如数据库路径问题）。真正的启动验证在第 9 步。

---

> **新开终端？先把变量再声明一次**（§1.1 的同一条命令，复制即用）：
> ```bash
> APP=/root/ai-interview-simulator
> DOMAIN=<你的域名或 112.124.54.223>
> STAMP=$(ls -t "$APP"/backup/manual-* 2>/dev/null | head -1 | sed 's#.*manual-##')
> APP_USER=root          # 与 §8.1 的选择保持一致
> ```

## 4. 安全执行数据库迁移（停机中，约 3 分钟）

> **本步是全程唯一"不可逆"的动作**，但脚本自带三重保护：
> ① 迁移前**自动备份**到 `backup/interview_<时间戳>.db`；
> ② 框架闸门（行数不得减少、`integrity_check`、`foreign_key_check`）；
> ③ 每步 DDL 后跑该迁移声明的校验语句，不符则**中止**。
>
> 因此"失败"本身不危险 —— **危险的是失败之后不做回滚就硬启动**。

### 4.1 先只读：看当前状态与将要做什么

```bash
cd "$APP/backend"

echo "=== 当前状态 ==="
venv/bin/python scripts/migrate.py --status

echo "=== 预演（不改动任何东西） ==="
venv/bin/python scripts/migrate.py --dry-run
```

**预期输出**（样例，版本号以实际为准）：

```
database: /root/ai-interview-simulator/backend/interview.db
  state    : legacy            ← 或 up-to-date
  detail   : ...
  applied  : 001               ← 老库可能只有 001，或是空的
  pending  : 002, 003, 004
  note     : legacy database -> baseline will be STAMPED only
```

```
database: .../interview.db
  [dry-run] ... 不会执行任何 DDL
```

**不通过怎么办**：

* 报 `数据库不存在` → 确认路径：`ls -l "$APP/backend/interview.db"`。
  若库其实在别处（例如仓库根），用 `--db` 显式指定，**别猜**。
* `state` 是 `partial`（结构不完整）→ 脚本会拒绝自动处理。
  这说明库被外部改过，先 `sqlite3 interview.db ".tables"` 看现状，
  再决定（通常是用备份里的库覆盖回来更省事）。

### 4.2 执行迁移（脚本会自动先备份）

```bash
cd "$APP/backend"
venv/bin/python scripts/migrate.py | tee /tmp/migrate-${STAMP}.log
echo "退出码=$?"
```

**预期输出**：逐行打印每个迁移做了什么，结尾类似：

```
  [备份] 完成，integrity_check=ok，行数={'users': 3, ...}
  002 ... 4 张新表 + 5 个索引
  003 ...
  004 ...
  foreign_key_check 无违规
MIGRATION OK / 迁移完成
退出码=0
```

同时会在 `$APP/backup/` 下多出一个 `interview_<时间戳>.db`。

**不通过怎么办 → 见 §4.5，不要重跑。**

### 4.3 ✅ 迁移后立刻核对（数据没丢）

```bash
cd "$APP"
echo "--- 行数必须与 §2.3 完全一致 ---"
sqlite3 backend/interview.db \
  "SELECT 'users='||(SELECT COUNT(*) FROM users),
          'records='||(SELECT COUNT(*) FROM interview_records);"
echo "--- 完整性 ---"
sqlite3 backend/interview.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
echo "--- 表清单 ---"
sqlite3 backend/interview.db ".tables"
echo "--- 迁移版本 ---"
venv/bin/python -c "import sys; sys.path.insert(0,'backend'); from migrations.runner import status; print(status('backend/interview.db'))" 2>/dev/null \
  || (cd backend && venv/bin/python scripts/migrate.py --status)
```

**预期输出**：

* 行数与 §2.3 **一字不差**（`users=3 records=5`）；
* `integrity_check` = `ok`，`foreign_key_check` **没有任何输出**；
* 表清单里能看到新表：`interview_sessions`、`captcha_store`、`auth_attempts`、
  `token_blacklist`、`schema_migrations`；
* `pending` 为空。

**不通过怎么办**：任一条不符 → **走 §4.5 回滚**，不要尝试"再迁一次"。

### 4.4 迁移成功后的继续条件

三条全绿才继续：

- [ ] `users=` / `records=` 与迁移前一致
- [ ] `integrity_check=ok` 且 `foreign_key_check` 无输出
- [ ] `pending` 为空

### 4.5 🔙 迁移失败 / 数据不对时的回滚（**先停手，再按顺序做**）

```bash
cd "$APP"
echo "=== 有哪些可用的回滚点 ==="
ls -lt backup/ manual-* 2>/dev/null | head
ls -lt backup/manual-${STAMP}/ | head
ls -lt backup/interview_*.db 2>/dev/null | head -3

# ① 选一个：优先用迁移脚本自己刚生成的（backup/interview_<最近时间戳>.db），
#    其次是 §2.2 手工备份的那份
RB=backup/interview_$(ls -t backup/interview_*.db | head -1 | sed 's#.*interview_##')
echo "将用 $RB 回滚"

# ② 回滚前先把"当前这份（迁移后的）"另存，便于事后分析
cp -a backend/interview.db "backup/interview.db.failed-migration-${STAMP}"

# ③ 覆盖回去（同时清掉 WAL 附属文件，避免旧库配新 WAL）
rm -f backend/interview.db-wal backend/interview.db-shm
cp -a "$RB" backend/interview.db

# ④ 验证
sqlite3 backend/interview.db "PRAGMA integrity_check;
  SELECT 'users='||(SELECT COUNT(*) FROM users),
         'records='||(SELECT COUNT(*) FROM interview_records);"

# ⑤ 如果连代码也要退回去，用 §2.4 记下的哈希
# git reset --hard $(cat backup/manual-${STAMP}/git_head_before.txt)
```

**预期输出**：`ok` + 与 §2.3 相同的行数。

回滚完成后的选择：

* **就此打住**：按 §9 用旧代码把服务重新起起来（systemd 单元里可临时把
  `ExecStart` 指回旧代码，或先 `git reset --hard <旧哈希>`）。把
  `/tmp/migrate-${STAMP}.log` **完整保存**下来，那是唯一的现场证据。
* **排查后再升**：拿到日志后找失败的那条 DDL，**在本地**用一份真库副本
  复现（不要在生产库上试）。

---

> **新开终端？先把变量再声明一次**（§1.1 的同一条命令，复制即用）：
> ```bash
> APP=/root/ai-interview-simulator
> DOMAIN=<你的域名或 112.124.54.223>
> STAMP=$(ls -t "$APP"/backup/manual-* 2>/dev/null | head -1 | sed 's#.*manual-##')
> APP_USER=root          # 与 §8.1 的选择保持一致
> ```

## 5. 前端：本地构建 → 上传覆盖 dist（停机中，约 8 分钟）

> ### ⚠️ 绝对不要在服务器上跑 `npm run build`
>
> 服务器只有 **2G 内存**。`vite build` + `tsc -b` 的峰值内存通常是 1~1.5GB，
> 叠加 nginx、uvicorn、SQLite 之后极易触发 OOM Killer ——
> 轻则构建失败留下**半个 dist**（页面白屏，且看不出来为什么），
> 重则把 uvicorn 或 ssh 一起杀掉。
>
> 而且阿里云这台机器上大概率**根本没装 node** —— 那不是问题，是好事。
>
> 正确做法只有一个：**本地 Windows 构建 → 传 dist 上去**。

### 5.1 本地 Windows：构建

在**你本机**（不是服务器）的 PowerShell 里：

```powershell
cd "D:\AI 驱动的智能面试准备与模拟系统\frontend"

# ① 确认依赖是全新的（package-lock 变了就要重装）
npm ci                      # 比 npm install 更严格，按 lock 文件精确还原

# ② 确认没有误配 API 地址：必须是"留空 = 相对路径"
Get-Content .env.example
if (Test-Path .env.production) { Write-Host "⚠️ 存在 .env.production，内容如下（VITE_API_BASE_URL 必须为空）:"; Get-Content .env.production }
else { Write-Host "✅ 没有 .env.production（默认相对路径，正确）" }

# ③ 构建
npm run build

# ④ 确认产物
Get-ChildItem dist | Select-Object Name, Length
Get-ChildItem dist\assets | Measure-Object | Select-Object Count
Select-String -Path dist\index.html -Pattern "assets/index-" | Select-Object -First 3
```

**预期输出**：

* `npm ci` 结尾 `added N packages in Xs`；
* **没有 `.env.production`** 或里面的 `VITE_API_BASE_URL=` 是空的；
* `npm run build` 结尾形如 `✓ built in 8.5s`；
* `dist/` 里有 `index.html`、`assets/`、`favicon.svg`、`icons.svg`；
* `assets/` 里能看到 `index-<hash>.js` 与 `index-<hash>.css`。

> **为什么强调"相对路径"**：T-34 把硬编码的 `http://127.0.0.1:8000` 干掉了。
> 若这里填了 `http://112.124.54.223:8000`，HTTPS 页面会被浏览器按
> **混合内容**拦掉，所有按钮失效，且控制台报错很直白但不明显。
> 留空 = 请求打到当前页面 origin = 由 Nginx 同源反代，这才是设计口径。

**构建失败怎么办**：

* `tsc -b` 报类型错误 → 先把本地 `npm run test:node` 与 `npx tsc -b` 跑通再传，
  **不要**用 `vite build --force` 绕过类型检查。
* `npm ci` 报 lock 不同步 → `npm install` 更新 lock 并提交，再 `npm ci`。

### 5.2 上传 dist（二选一；**推荐 rsync**）

#### 路线 A：rsync（推荐 —— 只传变化的文件，且能删掉服务器上的陈旧产物）

在**本机 PowerShell**（Windows 10/11 自带 `ssh`，rsync 需要 Git Bash 或 WSL）：

```powershell
# 用 Git Bash / WSL 执行：
cd "/d/AI 驱动的智能面试准备与模拟系统/frontend"

# ① 先干跑（--dry-run），确认要传/要删什么，不做任何改动
rsync -avz --delete --dry-run dist/ root@112.124.54.223:/root/ai-interview-simulator/frontend/dist/

# ② 确认无误后真传
#    注意 dist/ 结尾的斜杠：带斜杠 = 传目录内容；不带 = 多套一层 dist/dist
rsync -avz --delete dist/ root@112.124.54.223:/root/ai-interview-simulator/frontend/dist/
```

**预期输出**：`--dry-run` 列出 `index.html`、`assets/index-<新hash>.js` 等；
由于 Vite 每次构建的 hash 不同，会看到一批 `deleting assets/index-<旧hash>.js`
—— **这正是 `--delete` 的价值**（不删的话 dist 会越堆越大，2G 磁盘迟早满）。

#### 路线 B：scp（本机原生 PowerShell 即可，但不会删旧文件）

```powershell
cd "D:\AI 驱动的智能面试准备与模拟系统\frontend"

# ① 打包（tar 是 Ubuntu 自带；Windows 10+ 也有 tar.exe）
tar -czf dist.tar.gz -C dist .

# ② 上传
scp dist.tar.gz root@112.124.54.223:/root/ai-interview-simulator/

# ③ 到服务器上解压（见下一段）
```

然后在**服务器**上：

```bash
cd "$APP"
# 先把旧的挪走（不要直接覆盖，出问题还能退回）
mv frontend/dist frontend/dist.old-${STAMP} 2>/dev/null || true
mkdir -p frontend/dist
tar xzf dist.tar.gz -C frontend/dist
rm -f dist.tar.gz
ls -l frontend/dist | head
```

**预期输出**：`frontend/dist/` 里有 `index.html` 与 `assets/`；
`frontend/dist.old-<STAMP>` 是上一版。

> 无论走哪条路线，**服务器上的 `frontend/dist` 必须与本地 `dist` 内容一致**。
> 用下面的 5.3 校验。

### 5.3 ✅ 校验上传结果（本地与服务器对哈希）

**本机 PowerShell**：
```powershell
cd "D:\AI 驱动的智能面试准备与模拟系统\frontend"
Get-FileHash dist\index.html -Algorithm SHA256 | Select-Object Hash
(Get-ChildItem dist\assets | Measure-Object).Count
```

**服务器**：
```bash
cd "$APP/frontend/dist"
sha256sum index.html
ls assets | wc -l
grep -o 'assets/index-[A-Za-z0-9_-]*\.js' index.html | head -2
ls assets | grep -E 'index-.*\.js$' | head -3
```

**预期输出**：`index.html` 的 sha256 两边**完全一致**；
`index.html` 里引用的 hash 文件名在 `assets/` 里**确实存在**
（引用与文件对不上就是白屏的头号原因）。

### 5.4 权限（让 nginx 能读）

```bash
cd "$APP"
# nginx 以 www-data 运行，需要能"穿过"父目录读到 dist
chmod 755 frontend frontend/dist frontend/dist/assets
find frontend/dist -type f -exec chmod 644 {} \;
sudo -u www-data test -r frontend/dist/index.html && echo "✅ nginx 可读 index.html"
```

**预期输出**：`✅ nginx 可读 index.html`。

> ⚠️ 项目在 `/root` 下，而 `/root` 默认是 `700`。若上面那条 `sudo -u www-data`
> 报 permission denied，需要给 nginx 放行路径（见第 6.2 步的说明）：
> ```bash
> chmod 711 /root
> ```
> 这是"放行穿越"而不是"放开内容"（`711` = 别人只能进目录、不能列目录）。
> 若你介意，替代方案是把项目移到 `/opt/ai-interview-simulator`
> （但那样 Nginx/systemd 里所有路径都要改，**本次升级不动它**）。

---

> **新开终端？先把变量再声明一次**（§1.1 的同一条命令，复制即用）：
> ```bash
> APP=/root/ai-interview-simulator
> DOMAIN=<你的域名或 112.124.54.223>
> STAMP=$(ls -t "$APP"/backup/manual-* 2>/dev/null | head -1 | sed 's#.*manual-##')
> APP_USER=root          # 与 §8.1 的选择保持一致
> ```

## 6. 配置 Nginx（同源托管，约 5 分钟）

### 6.1 装片段与站点配置

```bash
cd "$APP"

# ① 建目录（幂等）
sudo mkdir -p /etc/nginx/snippets /var/www/certbot
sudo chown -R www-data:www-data /var/www/certbot

# ② 片段（四个）
for f in acme-challenge security-headers html-no-cache assets-cache; do
  sudo cp deploy/nginx/${f}.conf /etc/nginx/snippets/${f}.conf
done
ls -l /etc/nginx/snippets/

# ③ 站点配置（先装 HTTP 版）
sudo cp deploy/nginx/ai-interview.conf /etc/nginx/sites-available/ai-interview

# ④ 替换占位符（⚠️ 改成你自己的域名/IP 与路径）
sudo sed -i "s|YOUR_DOMAIN|${DOMAIN:-112.124.54.223}|g" /etc/nginx/sites-available/ai-interview
sudo sed -i "s|APP_DIR|${APP}|g"                     /etc/nginx/sites-available/ai-interview

# ⑤ 确认替换干净（不该再出现 YOUR_DOMAIN / APP_DIR）
grep -n "YOUR_DOMAIN\|APP_DIR" /etc/nginx/sites-available/ai-interview || echo "✅ 占位符已全部替换"
grep -n "server_name\|root \|proxy_pass\|client_max_body_size" /etc/nginx/sites-available/ai-interview
```

**预期输出**：4 个 snippet 文件；`✅ 占位符已全部替换`；
最后一行能看到 `server_name 112.124.54.223;`、`root /root/ai-interview-simulator/frontend/dist;`、
`proxy_pass http://ai_interview_backend;`、`client_max_body_size 8m;`。

### 6.2 停用旧站点、启用新站点

```bash
# ① 先看现有站点（第 1.4 步记下的名字）
ls -l /etc/nginx/sites-enabled/

# ② 停用旧站点（把 <旧文件名> 换成实际名字，例如 default）
sudo rm -f /etc/nginx/sites-enabled/<旧文件名>
# ⚠️ 若那个 default 只是默认页（里面只有 root /var/www/html），删掉链接即可；
#    若它承载着别的站点，就不要删，改成给它换个 server_name。

# ③ 启用新站点
sudo ln -sfn /etc/nginx/sites-available/ai-interview /etc/nginx/sites-enabled/ai-interview
ls -l /etc/nginx/sites-enabled/

# ④ 处理 /root 的穿越权限（项目在 /root 下，nginx 需要能进来）
chmod 711 /root
ls -ld /root
```

**预期输出**：`sites-enabled/` 里只有 `ai-interview -> /etc/nginx/sites-available/ai-interview`；
`ls -ld /root` 显示 `drwx--x--x`。

### 6.3 ✅ 语法检查（**先 `-t` 再 `reload`，永远如此**）

```bash
sudo nginx -t
```

**预期输出**：

```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

**不通过怎么办**：

* `unknown directive "include"` / 文件不存在 → snippet 没拷进去，
  回到 6.1 的 ②。
* `duplicate upstream "ai_interview_backend"` → 你同时启用了 HTTP 与 HTTPS 两份配置。
  `rm` 掉其中一个链接。
* `host not found in upstream` → uvicorn 还没起（第 8 步才起）。这**不影响 `-t`**，
  只影响 reload 后的首个请求；但若报的是 `upstream` 名字打错，就要回去改配置。
* `open() "/etc/nginx/snippets/xxx.conf" failed` → 路径拼错或文件没拷。

### 6.4 reload 并做第一次冒烟

```bash
sudo systemctl reload nginx
systemctl is-active nginx
curl -s -o /dev/null -w "index: %{http_code}\n" http://127.0.0.1/
curl -s -o /dev/null -w "/admin: %{http_code}\n" http://127.0.0.1/admin
curl -s -o /dev/null -w "/assets 探测: %{http_code}\n" http://127.0.0.1/assets/
```

**预期输出（关键！）**：

```
active
index: 200
/admin: 200        ← 必须是 200。是 404 说明回退没生效；是 301 说明写成了跳转
/assets 探测: 404  ← 404 是对的（我们没有 /assets/ 这个目录本身）
```

> 此时 `/api/...` 会返回 **502**（uvicorn 还没起），这是**预期**的 —— 第 8 步之后再看。

**不通过怎么办**：

* `/admin` 是 404 → `location = /admin` 没生效。检查
  `grep -n "location = /admin" -A3 /etc/nginx/sites-available/ai-interview`
  是否写着 `try_files /index.html =404;`。
* `/admin` 是 301/302 → 有人把回退写成了 `return 301`，改回 `try_files`。
* 全部 404 → `root` 指向的 dist 目录为空或路径不对：
  `ls -l /root/ai-interview-simulator/frontend/dist/index.html`。

---

> **新开终端？先把变量再声明一次**（§1.1 的同一条命令，复制即用）：
> ```bash
> APP=/root/ai-interview-simulator
> DOMAIN=<你的域名或 112.124.54.223>
> STAMP=$(ls -t "$APP"/backup/manual-* 2>/dev/null | head -1 | sed 's#.*manual-##')
> APP_USER=root          # 与 §8.1 的选择保持一致
> ```

## 7. HTTPS：域名 + certbot（约 10 分钟）

> **Let's Encrypt 不给纯 IP 签证书**。如果你还没有域名，先做 7.1；
> 已经有域名就跳到 7.2。

### 7.1 没有域名？先拿一个免费的（5 分钟）

1. 打开 <https://www.duckdns.org>，用 GitHub/Google 账号登录。
2. 建一个子域名，例如 `ai-interview-demo`，得到 `ai-interview-demo.duckdns.org`。
3. 在 DuckDNS 页面把 **current ip** 填成 `112.124.54.223`（或用它的 token URL 自动更新）。
4. 验证解析（在**你本机或服务器**都行）：

```bash
getent hosts ai-interview-demo.duckdns.org
# 或
nslookup ai-interview-demo.duckdns.org 223.5.5.5
```

**预期输出**：解析出 `112.124.54.223`。**不是这个 IP 就不要继续**，
certbot 必然失败（HTTP-01 校验要能从这个域名访问到你的服务器）。

> 用阿里云自己的域名也一样：在**云解析 DNS** 里加一条 A 记录
> （主机记录 `@` 或 `www`，记录值 `112.124.54.223`），等 1~10 分钟生效。
> **中国大陆的阿里云服务器**：域名若未备案，80/443 端口会被拦截 ——
> 这种情况要么完成备案，要么先用 IP + HTTP 验证功能，HTTPS 等备案后再做。

然后更新 `DOMAIN` 并重刷 nginx 配置里的域名：

```bash
DOMAIN=ai-interview-demo.duckdns.org
sudo sed -i "s|server_name .*;|server_name ${DOMAIN};|g" /etc/nginx/sites-available/ai-interview
sudo nginx -t && sudo systemctl reload nginx
grep -n "server_name" /etc/nginx/sites-available/ai-interview
```

### 7.2 安装 certbot 并签发

```bash
# ① 装 certbot（Ubuntu 22.04 用 snap 或 apt 都行，这里用 apt 更省事）
sudo apt-get update
sudo apt-get install -y certbot

# ② 先用 staging 环境试一次（不消耗正式额度、不会把自己卡在限流里）
sudo certbot certonly --webroot -w /var/www/certbot \
  -d "${DOMAIN}" --non-interactive --agree-tos \
  -m "admin@${DOMAIN}" --staging
```

**预期输出**：结尾 `Congratulations! Your certificate and chain have been saved at:
/etc/letsencrypt/live/<DOMAIN>/fullchain.pem`。

**不通过怎么办（按报错分）**：

| 报错 | 原因 | 处理 |
|---|---|---|
| `Invalid response ... 404` | webroot 目录不对，或 80 端口没到本机 | `ls /var/www/certbot` 存在？`sudo nginx -t`；`curl -I http://${DOMAIN}/.well-known/acme-challenge/test` 看是不是 404 还是连不上 |
| `Timeout during connect` | 安全组/防火墙没放行 80 | 阿里云控制台 → 安全组 → 入方向放行 **80、443**；服务器上 `sudo ufw status` |
| `DNS problem: NXDOMAIN` | 域名没解析或还没生效 | 回到 7.1 验证解析 |
| `too many failed authorizations` | 试错太多次 | 等 1 小时，或先用 staging |
| 80 端口被运营商/备案拦截 | 大陆未备案域名的常见情况 | 先用 IP+HTTP 上线，备案后再做 HTTPS |

```bash
# ③ staging 成功后，删掉试签发正式证书
sudo rm -rf /etc/letsencrypt/live/${DOMAIN} /etc/letsencrypt/archive/${DOMAIN} \
            /etc/letsencrypt/renewal/${DOMAIN}.conf
sudo certbot certonly --webroot -w /var/www/certbot \
  -d "${DOMAIN}" --non-interactive --agree-tos -m "admin@${DOMAIN}"

# ④ 确认证书就位
sudo ls -l /etc/letsencrypt/live/${DOMAIN}/
sudo openssl x509 -in /etc/letsencrypt/live/${DOMAIN}/fullchain.pem -noout -subject -dates
```

**预期输出**：四个文件 `cert.pem / chain.pem / fullchain.pem / privkey.pem`；
`openssl` 打印 `subject=CN = <DOMAIN>` 与 90 天后的 `notAfter`。

### 7.3 切到 HTTPS 配置

```bash
cd "$APP"

# ① 装 HTTPS 站点配置
sudo cp deploy/nginx/ai-interview-ssl.conf /etc/nginx/sites-available/ai-interview-ssl
sudo sed -i "s|YOUR_DOMAIN|${DOMAIN}|g" /etc/nginx/sites-available/ai-interview-ssl
sudo sed -i "s|APP_DIR|${APP}|g"        /etc/nginx/sites-available/ai-interview-ssl
grep -n "YOUR_DOMAIN\|APP_DIR" /etc/nginx/sites-available/ai-interview-ssl || echo "✅ 占位符已替换"
grep -n "ssl_certificate\|server_name\|listen 443" /etc/nginx/sites-available/ai-interview-ssl

# ② 互斥切换：**删掉 HTTP 那个链接**，再建 HTTPS 链接
#    （两份都有 upstream，同时启用会 duplicate upstream → nginx 起不来）
sudo rm -f /etc/nginx/sites-enabled/ai-interview
sudo ln -sfn /etc/nginx/sites-available/ai-interview-ssl /etc/nginx/sites-enabled/ai-interview-ssl
ls -l /etc/nginx/sites-enabled/

# ③ 语法检查 + reload
sudo nginx -t
sudo systemctl reload nginx
```

**预期输出**：`sites-enabled/` 里只有 `ai-interview-ssl`；
`nginx -t` 两行 `ok` / `successful`。

**不通过怎么办**：

* `duplicate upstream` → 还有别的文件在定义同名 upstream，`ls sites-enabled/` 逐个查。
* `cannot load certificate` → 证书路径里的域名与 `${DOMAIN}` 不一致；
  `sudo ls /etc/letsencrypt/live/` 看真实目录名。
* `[warn] "listen ... http2" is deprecated`（只是警告）→ 见
  `deploy/nginx/README.md` 的 HTTP/2 段，按 `nginx -v` 选择写法。

### 7.4 ✅ 验收标准逐条验（**这是 T-53 的核心**）

```bash
echo "=== ① http:// 必须 301 跳 https:// ==="
curl -s -o /dev/null -D - "http://${DOMAIN}/" | head -3
curl -s -o /dev/null -D - "http://${DOMAIN}/admin" | head -3

echo "=== ② /admin 刷新必须 200（不是 301/404） ==="
curl -s -o /dev/null -w "/admin -> %{http_code}\n" "https://${DOMAIN}/admin"

echo "=== ③ 安全响应头 ==="
curl -s -o /dev/null -D - "https://${DOMAIN}/" | grep -iE "strict-transport|content-security|x-content-type|x-frame"

echo "=== ④ 证书信息 ==="
echo | openssl s_client -connect ${DOMAIN}:443 -servername ${DOMAIN} 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

**预期输出**：

```
HTTP/1.1 301 Moved Permanently
Location: https://<DOMAIN>/
...
/admin -> 200
strict-transport-security: max-age=2592000
content-security-policy: default-src 'self'; ...
x-content-type-options: nosniff
x-frame-options: SAMEORIGIN
subject=CN = <DOMAIN>
notAfter=...（约 90 天后）
```

### 7.5 确认自动续期（**不做这一步 = 90 天后站点挂掉**）

```bash
# ① 看 systemd 定时器（apt 装的 certbot 会自动装这个）
systemctl list-timers | grep -i certbot
systemctl status certbot.timer --no-pager | head -5

# ② 干跑一次续期（不真的签）
sudo certbot renew --dry-run
```

**预期输出**：`certbot.timer` 在列表里（通常一天跑两次）；
`--dry-run` 结尾 `Congratulations, all simulated renewals succeeded`。

**不通过怎么办**：

* 没有 `certbot.timer` → `sudo systemctl enable --now certbot.timer`。
* `--dry-run` 失败且报 404 → 80 端口的 ACME location 被 301 吃掉了。
  检查 `/etc/nginx/sites-available/ai-interview-ssl` 的第二个 server 块里
  `include /etc/nginx/snippets/acme-challenge.conf;` **在** `location /` 之前，
  且用的是 `^~`（选路优先级里 `^~` 高于普通前缀）。

---

> **新开终端？先把变量再声明一次**（§1.1 的同一条命令，复制即用）：
> ```bash
> APP=/root/ai-interview-simulator
> DOMAIN=<你的域名或 112.124.54.223>
> STAMP=$(ls -t "$APP"/backup/manual-* 2>/dev/null | head -1 | sed 's#.*manual-##')
> APP_USER=root          # 与 §8.1 的选择保持一致
> ```

## 8. 给后端配 systemd 守护进程（约 5 分钟）

> 从这一步起，uvicorn 由 systemd 托管：**开机自启、崩溃自动重启**，
> 不再有"服务器重启后站点挂了没人知道"的情况。

### 8.1 准备运行用户与目录

```bash
APP=/root/ai-interview-simulator
APP_USER=root        # ← 见下面对照表选择，选完就不要再改

# 若决定用专用用户（推荐）：
#   sudo useradd -r -s /usr/sbin/nologin -d "$APP" interview
#   sudo chown -R interview:interview "$APP/backend" "$APP/backup" "$APP/frontend/dist"
#   APP_USER=interview
echo "APP_USER=$APP_USER"

sudo mkdir -p "$APP/backup"
sudo chmod 700 "$APP/backup"
sudo chown -R "$APP_USER":"$APP_USER" "$APP/backup"
ls -ld "$APP/backup"
```

> **`APP_USER` 怎么选**（这是个真实取舍，Runbook 不替你决定）：
>
> | 选择 | 优点 | 代价 |
> |---|---|---|
> | `root` | 立刻能跑；`/root` 下的项目天然可读写 | 服务被攻破即整机失守 |
> | `interview`（推荐） | 最小权限 | 要 `chown` 整个项目；`/root` 下的家目录权限要单独处理 |
>
> 本项目**当前形态**（项目在 `/root` 下、旧进程就是 root 起的）用 `root` 最省事、
> 风险可控（只有你一个人用）；但**只要将来对外开放，就该换 `interview`**。
> 换的时候只需改 `User=`/`Group=` 两行并 `chown`。

### 8.2 写后端 service

```bash
sudo tee /etc/systemd/system/ai-interview.service >/dev/null <<EOF
[Unit]
Description=AI 面试模拟系统 · 后端 API（uvicorn）
Documentation=file://${APP}/README.md
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP}/backend
# ⚠️ 必须显式 --workers 2（见 README：SQLite + 多 worker 的并发契约已在 T-15 处理）
#    host 用 127.0.0.1：只让本机 Nginx 访问，不直接暴露 8000 到公网
ExecStart=${APP}/backend/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2

User=${APP_USER}
Group=${APP_USER}

# 崩溃自动重启（always 覆盖"非零退出""被信号杀""被 OOM Killer 干掉"）
Restart=always
RestartSec=3
# 5 分钟内重启超过 10 次就放弃，避免无限重启刷日志把磁盘写满
StartLimitIntervalSec=300
StartLimitBurst=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ai-interview

# --- 加固（后端能显著受益，因为它会解析用户上传的 PDF/DOCX） ---
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
# 需要写：数据库及其 -wal/-shm、上传目录、日志
ReadWritePaths=${APP}/backend

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
systemctl cat ai-interview.service | head -20
```

**预期输出**：打印出刚写入的 unit 内容，`ExecStart` 与路径都是你的真实路径。

> 💡 关于 `ProtectHome=read-only`：项目在 `/root` 下，**不能**用
> `ProtectHome=true`（会让 `/root` 对服务不可见，后端连数据库都读不到，
> 报错却只说"文件不存在"）。这与 `deploy/systemd/backup.service` 的处理一致。

### 8.3 启动并验证后端

```bash
sudo systemctl start ai-interview
sleep 3
systemctl status ai-interview --no-pager | head -15
echo "--- 最近日志 ---"
journalctl -u ai-interview -n 30 --no-pager
echo "--- 端口 ---"
ss -lntp | grep 8000
echo "--- 直连后端 ---"
curl -s -o /dev/null -w "backend / -> %{http_code}\n" http://127.0.0.1:8000/
curl -s -o /dev/null -w "backend /api/captcha -> %{http_code}\n" http://127.0.0.1:8000/api/captcha
```

**预期输出**：

* `systemctl status` 显示 `Active: active (running)`；
* 日志里有 `Uvicorn running on http://127.0.0.1:8000` 与 `Application startup complete.`；
* `ss` 能看到 8000；
* 两个 curl 都是 **200**。

**不通过怎么办**：

| 现象 | 原因 | 处理 |
|---|---|---|
| `Active: activating (auto-restart)` 反复重启 | 启动即崩 | `journalctl -u ai-interview -n 50 --no-pager` 看最后一段 traceback |
| `ModuleNotFoundError` | 依赖没装全 | 回到 §3.2 |
| `RuntimeError: SECRET_KEY 未设置` | `.env` 丢了/路径不对 | `WorkingDirectory` 必须是 `backend/`（`load_dotenv` 按 cwd 找 `.env`） |
| `sqlite3.OperationalError: unable to open database file` | 权限或 `ReadWritePaths` 没覆盖 | `ls -l backend/interview.db` 属主是否等于 `APP_USER`；`ProtectHome` 是否为 `read-only` |
| 端口被占 | 旧 uvicorn 没死 | `sudo ss -lntp \| grep 8000`，`kill` 掉再 `systemctl restart` |
| `permission denied` 读 `/root/...` | `/root` 权限 | `chmod 711 /root`（见 §5.4） |

### 8.4 开机自启

```bash
sudo systemctl enable ai-interview
systemctl is-enabled ai-interview
ls -l /etc/systemd/system/multi-user.target.wants/ | grep ai-interview
```

**预期输出**：`enabled`，且 `multi-user.target.wants/` 里有软链接。

### 8.5 装 T-52 的备份与清理定时器

```bash
cd "$APP"

# ① 把两份 service 里的路径/用户换成真实值
for f in backup cleanup; do
  sudo cp deploy/systemd/${f}.service /etc/systemd/system/
  sudo cp deploy/systemd/${f}.timer   /etc/systemd/system/
  sudo sed -i "s|/root/ai-interview-simulator|${APP}|g" /etc/systemd/system/${f}.service
  sudo sed -i "s|^User=.*|User=${APP_USER}|"            /etc/systemd/system/${f}.service
  sudo sed -i "s|^Group=.*|Group=${APP_USER}|"          /etc/systemd/system/${f}.service
done
sudo systemctl daemon-reload

# ② 确认替换干净
grep -nE "^(User|Group|WorkingDirectory|ExecStart|ReadWritePaths)" /etc/systemd/system/backup.service /etc/systemd/system/cleanup.service

# ③ 先手工各跑一次，看退出码（0 正常 / 3 = 已有实例在跑，也算正常）
sudo systemctl start backup.service
sleep 2
systemctl status backup.service --no-pager | head -8
journalctl -u backup.service -n 20 --no-pager
ls -l "$APP/backup/" | head

sudo systemctl start cleanup.service
journalctl -u cleanup.service -n 15 --no-pager

# ④ 启用定时器
sudo systemctl enable --now cleanup.timer backup.timer
systemctl list-timers cleanup.timer backup.timer --no-pager
```

**预期输出**：

* 两份 service 的 `ExecStart` 都指向 `${APP}/backend/...`，`User` 是你的用户；
* `backup.service` 日志里能看到 `BACKUP_OK dir=...`，`backup/` 下多出
  `2026xxxx_xxxxxx/` 目录（内含 `interview.db`、`uploads.zip`、`.env`、`manifest.json`）；
* `list-timers` 显示两个 timer 的 **NEXT** 时间（cleanup 每 15 分钟，backup 每天 03:30）。

**不通过怎么办**：

* `backup.service` 退出码 2 且日志说"数据库不存在" → 十有八九是把
  `ProtectHome` 改成了 `true`（备份 unit 里刻意用 `read-only`）。
  `systemctl cat backup.service | grep ProtectHome` 确认。
* 备份因权限失败 → `sudo chown -R ${APP_USER}:${APP_USER} "$APP/backup"`。
* 日志里 `⚠️ .env 未备份` → `sudo -u ${APP_USER} test -r "$APP/backend/.env"` 验证可读性。

### 8.6 ✅ 用 systemd 重启一次，验证"重启命令"与自愈

```bash
# 正常重启
sudo systemctl restart ai-interview
sleep 3
systemctl is-active ai-interview

# 模拟崩溃：杀掉 worker，看它是否自动回来
sudo pkill -f "uvicorn main:app"
sleep 6
systemctl is-active ai-interview
journalctl -u ai-interview -n 20 --no-pager | grep -iE "Started|Stopped|Scheduled restart|Main process"
```

**预期输出**：第一次 `active`；`pkill` 之后等待 6 秒，**仍然是 `active`**
（`Restart=always` 生效），日志里有 `Scheduled restart job`。

**以后重启后端的标准命令**（抄下来）：

```bash
sudo systemctl restart ai-interview      # 重启
sudo systemctl status  ai-interview      # 状态
journalctl -u ai-interview -f            # 实时日志（Ctrl+C 退出）
sudo systemctl stop    ai-interview      # 停止
```

---

> **新开终端？先把变量再声明一次**（§1.1 的同一条命令，复制即用）：
> ```bash
> APP=/root/ai-interview-simulator
> DOMAIN=<你的域名或 112.124.54.223>
> STAMP=$(ls -t "$APP"/backup/manual-* 2>/dev/null | head -1 | sed 's#.*manual-##')
> APP_USER=root          # 与 §8.1 的选择保持一致
> ```

## 9. 全站验收（10 分钟，逐条打勾）

### 9.1 自动化冒烟（命令行）

```bash
DOMAIN=<你的域名或 112.124.54.223>

echo "① 首页"
curl -s -o /dev/null -w "  %{http_code}\n" "https://${DOMAIN}/"
echo "② /admin 刷新（SPA 回退）"
curl -s -o /dev/null -w "  %{http_code}\n" "https://${DOMAIN}/admin"
echo "③ 验证码接口（同源反代）"
curl -s "https://${DOMAIN}/api/captcha" | head -c 120; echo
echo "④ 上传体积：上传 6MB 假 PDF，期望 **400**（不是 413）"
head -c 6291456 /dev/urandom > /tmp/big6m.pdf
curl -s -o /tmp/upload-resp.txt -w "  6MB 上传 -> %{http_code}\n" \
  -X POST "https://${DOMAIN}/api/resume/upload" -F "file=@/tmp/big6m.pdf;type=application/pdf"
head -c 200 /tmp/upload-resp.txt; echo
# 说明：未登录时会先返回 401/403（这也证明请求到达了应用、没被 Nginx 掐掉）；
#       要拿到那条"文件大小不能超过 5MB"的 400，请在浏览器里登录后再传（§9.2 第 6 项）。
echo "⑤ 后端看到的真实客户端 IP（应是你本机公网 IP，不是 127.0.0.1）"
journalctl -u ai-interview -n 200 --no-pager | grep -oE "客户端 IP[^,]*|client_ip=[0-9.]+" | tail -3
echo "⑥ http 跳 https"
curl -s -o /dev/null -D - "http://${DOMAIN}/" | head -1
```

**逐条预期**：

| # | 期望 | 不达标的含义 |
|---|---|---|
| ① | `200` | dist 没上传或 root 路径不对 |
| ② | `200` | `/admin` 回退失效（见 §6.4） |
| ③ | 返回一段 JSON（含 `captcha_id`） | 反代或后端有问题 |
| ④ | **`400`** | 若是 `413` → `client_max_body_size` 没生效；若是 `404` → 接口路径不对（该接口需登录时会 401/403，先看是不是 401） |
| ⑤ | 出现你的公网 IP | XFF 没转发或中间还有代理（`TRUSTED_PROXY_COUNT` 要相应调整） |
| ⑥ | `HTTP/1.1 301 Moved Permanently` | 80→443 跳转没生效 |

> ④ 的 `403/401`（未登录）也说明**请求到达了应用**，同样满足
> "不是 413" 这条验收标准；但要看清楚响应体里的 `detail`，别把鉴权失败误判成成功。

### 9.2 浏览器人工验收（必须亲手点）

1. 打开 `https://<DOMAIN>/`，**强制刷新**（Ctrl+F5），确认：
   - [ ] 页面正常渲染（不是白屏、不是样式全丢）；
   - [ ] 浏览器地址栏是**锁形图标**，点开证书主体与你的域名一致；
   - [ ] F12 → Console **没有红色报错**（特别是有没有 `Mixed Content`）；
   - [ ] F12 → Network：所有请求的域都是你的域名（**不该出现 `127.0.0.1:8000`**）。
2. 注册/登录：验证码能显示、能提交、能进主界面。
3. 面试主流程：开始面试 → 答一题 → 结束 → 能出报告。
4. **刷新 `/admin`**：地址栏保持在 `/admin`，页面正常（不是跳回首页、不是 404）。
5. 上传头像（<2MB）：能上传、能显示。
6. 上传一份 6MB 的 PDF 简历：界面提示"文件大小不能超过 5MB"（**不是网关错误页**）。
7. 登出，再访问受保护页面：应跳回登录。

### 9.3 数据一致性终检

```bash
cd "$APP"
echo "--- 行数（应与 §2.3 一致，此后只增不减） ---"
sqlite3 backend/interview.db \
  "SELECT 'users='||(SELECT COUNT(*) FROM users),
          'records='||(SELECT COUNT(*) FROM interview_records),
          'sessions='||(SELECT COUNT(*) FROM interview_sessions);"
sqlite3 backend/interview.db "PRAGMA integrity_check;"
echo "--- 迁移版本 ---"
(cd backend && venv/bin/python scripts/migrate.py --status)

echo "--- 备份体系 ---"
ls -lt backup/ | head -5
(cd backend && venv/bin/python scripts/rotate_backup.py --verify --out "$APP/backup") \
  || echo "（--verify 报 1：有备份不健康，见上面的逐份输出）"
systemctl list-timers cleanup.timer backup.timer --no-pager
```

**预期输出**：`users=` / `records=` 与升级前一致（`sessions=` 会新增）；
`integrity_check=ok`；`pending` 为空；`--verify` 里每份都是 `[OK]`。

---

## 10. 附录

### 10.1 日常运维命令速查

```bash
# 后端
sudo systemctl restart ai-interview        # 改完代码/配置后重启
journalctl -u ai-interview -f              # 实时日志
journalctl -u ai-interview --since "1 hour ago" | tail -100

# Nginx
sudo nginx -t && sudo systemctl reload nginx   # 改完配置（永远先 -t）
tail -f /var/log/nginx/ai-interview.error.log
tail -f /var/log/nginx/ai-interview.access.log

# 备份与清理
systemctl list-timers backup.timer cleanup.timer --no-pager
sudo systemctl start backup.service        # 立刻备份一次
(cd "$APP/backend" && venv/bin/python scripts/rotate_backup.py --verify --out "$APP/backup")

# 磁盘（2G 机器要盯）
df -h /; du -sh "$APP/backup"; du -sh "$APP/backend/uploads"
```

### 10.2 以后每次发新版（3 步）

```bash
# 在本地：构建并上传前端（见 §5）
cd "D:\AI 驱动的智能面试准备与模拟系统\frontend"; npm run build
# rsync -avz --delete dist/ root@112.124.54.223:/root/ai-interview-simulator/frontend/dist/

# 在服务器：拉代码（仍用强制拉取，避免历史重写导致的冲突）
cd /root/ai-interview-simulator
git fetch --all && git reset --hard origin/main
(cd backend && venv/bin/python scripts/migrate.py --status)   # 有 pending 才迁移
sudo systemctl restart ai-interview
sudo systemctl reload nginx                                    # 仅当 nginx 配置变过

# 改前端不必重启后端；dist 是 nginx 直接读盘的
```

> ⚠️ 有数据库迁移的版本，**必须**先按 §2.2 备份，再按 §4 走。
> `--status` 显示 `pending: (none)` 时可以直接跳过迁移。

### 10.3 常见故障速查表

| 症状 | 最可能的原因 | 处理 |
|---|---|---|
| 页面白屏、Console 报 404 找不到 `assets/index-xxx.js` | dist 上传不完整（本地与服务器 hash 不一致） | 重做 §5.2 并用 §5.3 对哈希 |
| 页面白屏、Console 报 Mixed Content | 构建时 `VITE_API_BASE_URL` 被填成 http 地址 | 本地清掉 `.env.production` 重新 `npm run build` |
| 按钮全部无反应、Network 里 `/api/*` 502 | 后端没起来 | `systemctl status ai-interview` + `journalctl -u ai-interview -n 50` |
| 刷新 `/admin` 变 404 或跳回首页 | SPA 回退被改成 3xx 或 location 被覆盖 | §6.4 |
| 上传大文件报"网关错误"（413） | `client_max_body_size` 没生效或没 reload | §9.1 ④ |
| 头像不显示（404） | `/uploads/` 没写 `^~`，被静态文件正则抢走，nginx 去 dist 里找图 | `deploy/nginx/README.md` 的 `/uploads/` 段；`grep -n "location ^~ /uploads/"` |
| 登录时提示"尝试次数过多"但没人试 | 后端拿到的 IP 全是 127.0.0.1（XFF 没转发） | §9.1 ⑤；确认 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` |
| 证书 90 天后过期 | 80 端口 ACME location 被 301 吃掉 | §7.5；`certbot renew --dry-run` |
| 服务器重启后站点全挂 | 没 `enable` | `systemctl is-enabled ai-interview nginx` |
| 内存吃紧 / OOM | 2G 太小 + 备份/构建抢内存 | `free -m`；**不要在服务器上构建**；考虑加 swap：`sudo fallocate -l 2G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |

### 10.4 从 T-52 备份恢复数据（单独使用时）

```bash
APP=/root/ai-interview-simulator
B=$APP/backup/2026xxxx_xxxxxx        # 选一份备份目录

sudo systemctl stop ai-interview
cp -a $APP/backend/interview.db $APP/backend/interview.db.before-restore-$(date +%F-%H%M)
rm -f $APP/backend/interview.db-wal $APP/backend/interview.db-shm
cp $B/interview.db $APP/backend/interview.db
unzip -o $B/uploads.zip -d $APP/backend/uploads/
cp $B/.env $APP/backend/.env && chmod 600 $APP/backend/.env
chown -R ${APP_USER}:${APP_USER} $APP/backend
sudo systemctl start ai-interview
sqlite3 $APP/backend/interview.db "PRAGMA integrity_check;"
```

### 10.5 整体回滚（升级失败，退回升级前）

```bash
APP=/root/ai-interview-simulator
S=<§2 那次升级的 STAMP>
cd "$APP"

# ① 停服务
sudo systemctl stop ai-interview 2>/dev/null
ps -ef | grep -E "[u]vicorn" && sudo pkill -f "uvicorn main:app"

# ② 回滚数据库
cp -a backend/interview.db backup/interview.db.rolledback-$(date +%F-%H%M)
rm -f backend/interview.db-wal backend/interview.db-shm
cp -a backup/manual-${S}/interview.db.bak-${S} backend/interview.db

# ③ 回滚上传物与配置
mv backend/uploads backend/uploads.rolledback-$(date +%F-%H%M)
tar xzf backup/manual-${S}/uploads.tar.gz -C backend
cp -a backup/manual-${S}/env.bak backend/.env && chmod 600 backend/.env

# ④ 回滚代码
git reset --hard "$(cat backup/manual-${S}/git_head_before.txt)"

# ⑤ 回滚 nginx（换回 HTTP 版或原站点）
sudo rm -f /etc/nginx/sites-enabled/ai-interview-ssl
sudo ln -sfn /etc/nginx/sites-available/ai-interview /etc/nginx/sites-enabled/ai-interview
sudo nginx -t && sudo systemctl reload nginx

# ⑥ 用旧代码启动（systemd 单元仍在，路径没变，直接起即可）
sudo systemctl start ai-interview
sqlite3 backend/interview.db "PRAGMA integrity_check;
  SELECT 'users='||(SELECT COUNT(*) FROM users),
         'records='||(SELECT COUNT(*) FROM interview_records);"
```

**预期输出**：`ok` + 与升级前相同的行数；站点恢复可访问。

---

## 11. 本次升级的"完成"定义（逐条打勾再收工）

- [ ] §2.2 备份存在且 §2.3 校验通过（含 uploads 归档非空）
- [ ] `git rev-parse HEAD` == `origin/main`
- [ ] `git check-ignore` 证明 `backend/interview.db`、`uploads/`、`.env` 不在版本控制内
- [ ] §3.3 `APP IMPORT OK`
- [ ] §4.3 迁移后行数与迁移前**一致**、`integrity_check=ok`、`pending` 为空
- [ ] §5.3 本地与服务器 `index.html` 的 sha256 **一致**
- [ ] §6.3 `nginx -t` 通过；§6.4 `/admin` 返回 **200**
- [ ] §7.4 `http://` → **301** → `https://`；证书主体是域名；安全头齐全
- [ ] §7.5 `certbot renew --dry-run` 成功
- [ ] §8.3 后端 `active (running)`、`/api/captcha` 200
- [ ] §8.4 `systemctl is-enabled ai-interview` == `enabled`
- [ ] §8.6 杀掉 worker 后**自动重启**
- [ ] §8.5 `cleanup.timer` 与 `backup.timer` 都在 `list-timers` 里
- [ ] §9.1 六条冒烟全部符合预期（尤其 ④ 是 **400 不是 413**）
- [ ] §9.2 浏览器 7 项人工验收通过
- [ ] §9.3 数据行数一致、备份 `--verify` 全 `[OK]`

全部打勾 = T-52 + T-53 上线完成。把这份清单连同
`backup/manual-<STAMP>/MANIFEST.txt`、`/tmp/migrate-<STAMP>.log`
一起留档 —— 下次升级时它们是"上一次的状态基线"。
