# AI 驱动的智能面试准备与模拟系统

## 项目简介

这是一个基于 AI 的智能面试模拟系统，帮助用户准备技术面试。系统支持简历上传、自动生成面试问题、模拟面试对话、生成评估报告等功能。

## 技术栈

### 后端
- FastAPI
- SQLAlchemy
- OpenAI SDK (用于 DeepSeek API)
- bcrypt (密码加密)

### 前端
- React 19
- TypeScript
- Vite
- Tailwind CSS

## 部署前准备

### 1. 环境配置

在 `backend` 目录下创建 `.env` 文件：

```bash
cd backend
cp .env.example .env
```

编辑 `.env` 文件，填入你的 DeepSeek API Key：

```
DEEPSEEK_API_KEY=sk-your-api-key-here
OPENAI_BASE_URL=https://api.deepseek.com/v1
DATABASE_URL=sqlite:///./interview.db
SECRET_KEY=your-random-secret-key-at-least-64-chars
```

### 2. 后端安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 3. 前端安装依赖

```bash
cd frontend
npm install
```

## 运行项目

### 后端开发服务

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 前端开发服务

```bash
cd frontend
npm run dev
```

## 部署到阿里云

### 1. 初始化 Git 仓库

```bash
git init
git add .
git commit -m "Initial commit"
```

### 2. 推送到 GitHub

```bash
git remote add origin https://github.com/your-username/your-repo.git
git branch -M main
git push -u origin main
```

### 3. 阿里云部署

推荐使用阿里云 ECS 或函数计算进行部署。
