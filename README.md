# 智学网 AstrBot 插件

> **v2.1.0** — 纯 HTTP 登录，t2i 图片化，LLM 工具，成绩监听

AstrBot 插件，接入智学网成绩查询系统。支持 QQ/微信等多平台。

## 功能

| 功能 | 说明 |
|------|------|
| 成绩查询 | `/zx marks [序号/名称]` — 图片渲染，序号 1=最新 |
| 考试列表 | `/zx exams` — 图片渲染 |
| 账号绑定 | `/zx bind <用户名>` — 一个智学网账号可绑定多个平台用户 |
| 成绩监听 | `/zx watch on [间隔]\|off` — 自动轮询，推送到私聊+群组 |
| LLM 工具 | AI 可通过工具调用查询成绩（需绑定账号） |

## 安装

将 `astrbot_plugin_zhixuewang.zip` 上传到 AstrBot 插件管理页面，或解压到 AstrBot 的 `data/plugins/` 目录。

## 配置

在 AstrBot WebUI 插件配置页面设置：

### 智学网账号列表

添加智学网用户名和密码。用户使用 `/zx bind <用户名>` 绑定后即可查询。

### 成绩监听

| 配置项 | 说明 |
|--------|------|
| 开启新成绩监听 | 开/关 |
| 监听间隔 | 检查频率（分钟） |
| 监听群组 | 按账号配置推送群组（需填 username + UMO） |

UMO 格式示例：`aiocqhttp:GroupMessage:123456`

### 监听推送逻辑

每个智学网账号的新成绩推送到：
1. 该账号所有绑定用户的**私聊**
2. 该账号关联的 watch_groups **群组**

## LLM 工具

插件注册了两个工具供 AI 调用：

- `zhixue_list_exams(user_id)` — 获取考试列表
- `zhixue_query_score(user_id, exam_name)` — 查询成绩

通过 `on_llm_request` 钩子注入用户上下文（发送者 ID、被@用户 ID），AI 可自动识别查谁的成绩。

## 文件结构

```
astrbot_plugin_zhixuewang/
├── main.py              # AstrBot 插件入口（命令、LLM 工具、监听）
├── zhixue_core.py       # 核心逻辑（绑定、缓存、成绩格式化）
├── zhixue_api.py        # 智学网 API 客户端
├── plugin_login.py      # 纯 HTTP 登录（极验 v4 滑块）
├── geeked/              # 极验验证码求解
├── _conf_schema.json    # AstrBot 配置 Schema
├── metadata.yaml        # 插件元数据
└── requirements.txt     # Python 依赖
```

## 技术要点

- **纯 HTTP 登录**：无 Playwright 依赖，服务器可用
- **三段式登录链路**：预登录 → SSO atLogin → SSO 换 ST
- **极验 v4 滑块**：自研 Geeked 求解器
- **accounts.json 绑定系统**：支持一账号多平台绑定
- **考试列表缓存**：5 分钟 TTL，减少 API 调用
- **t2i 图片渲染**：黑白极简风格，失败降级纯文本

## 许可

作者：JerryPig678
