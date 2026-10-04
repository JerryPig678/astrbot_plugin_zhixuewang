# 智学网 AstrBot 插件

> **v2.5.0** — 纯 HTTP 登录，t2i 图片化，LLM 工具，成绩/作业监听，答题卡批改标注渲染，手阅作业查询

AstrBot 插件，接入智学网成绩查询系统。支持 QQ/微信等多平台。

## 功能

| 功能 | 说明 |
|------|------|
| 成绩查询 | `/zx marks [序号/名称]` — 图片渲染，序号 1=最新 |
| 考试列表 | `/zx exams [all]` — 图片渲染，默认本学年，all 查看全部 |
| **答题卡批改标注** | `/zx sheet <学科> [考试]` — 渲染批改标注长图（见下） |
| 账号绑定 | `/zx bind <用户名>` — 一个智学网账号可绑定多个平台用户 |
| 成绩监听 | `/zx watch on [间隔]\|off` — 自动轮询，新成绩+新作业推送到私聊+群组 |
| **手阅作业** | `/zx hw list [all]`、`/zx hw marks`、`/zx hw sheet` — 查询手阅作业（见下） |
| LLM 工具 | AI 可通过工具调用查询成绩和答题卡（需绑定账号） |

## 答题卡批改标注渲染

`/zx sheet 数学` 会将答题卡原图渲染为带批注的 JPEG 长图，效果对齐智学网网页端：

- **顶部总分栏**：红色总分大字（如 `129 / 150 分`）+ 考试名/科目名
- **错题红框**：得分低于满分的题目区域红框圈出
- **题级标签**：`16题 5/16 -11`（红=有扣分，绿=满分）
- **分小问扣分明细**：`16(2): -3/3` 两列布局（来自 `stepDatas[].stepStandardScore` 满分与 `stepRecords` 得分）
- **老师逐点批注**：`+1` 等标注按原位绘制（markingContent spotMark）

坐标自适应三种答题卡模板：毫米坐标（A3 横 420 / A4 横 297 / A4 纵 210，由 locatePoint 自动判别）、设计分辨率像素 + 百分比坐标。

## 手阅作业查询

网页版没有手阅作业入口，但 zxbReport 接口对普通学生 web 会话开放。插件通过 `POST /zxbReport/report/*` 系列
接口（token 放请求头）实现完整查询，用法与考试命令一致：

| 命令 | 说明 |
|------|------|
| `/zx hw list [all]` | 作业列表，默认最近 10 条，all 查看全部（自动翻页） |
| `/zx hw marks [序号/作业名]` | 作业各科成绩，序号与 list 一致 |
| `/zx hw sheet <学科> [作业]` | 作业答题卡批改标注长图，与 `/zx sheet` 同款渲染 |

- 作业序号与 `/zx marks` 一样从 1 开始（1 = 最新）
- 支持名称模糊匹配，如 `/zx hw marks 化学专题`
- 答题卡渲染复用考试批注管线（总分栏/错题红框/题级标签/逐点批注）；手阅作业通常无分步数据，只显示题级得分
- 监听开启时，发现新作业会自动推送作业名+各科分数（首次运行仅建立基线，不推送历史作业）

- LLM 对话直接说"看看我的生物答题卡"也会触发 `zhixue_query_sheet` 工具并发图。
- 多页答题卡自动纵向拼接为一张长图（最大宽度 1600px）。

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

插件注册了三个工具供 AI 调用：

* `zhixue_list_exams(user_id)` — 获取考试列表
* `zhixue_query_score(user_id, exam_name)` — 查询成绩
* `zhixue_query_sheet(user_id, subject_name, exam_name)` — 查询答题卡（直接发图）

通过 `on_llm_request` 钩子注入用户上下文（发送者 ID、被@用户 ID），AI 可自动识别查谁的成绩。

## 文件结构

```
astrbot_plugin_zhixuewang/
├── main.py              # AstrBot 插件入口（命令、LLM 工具、监听）
├── zhixue_core.py       # 核心逻辑（绑定、缓存、成绩格式化）
├── zhixue_api.py        # 智学网 API 客户端
├── answer_sheet.py      # 答题卡批注渲染（PIL）
├── plugin_login.py      # 纯 HTTP 登录（极验 v4 滑块）
├── geeked/              # 极验验证码求解
├── _conf_schema.json    # AstrBot 配置 Schema
├── metadata.yaml        # 插件元数据
└── requirements.txt     # Python 依赖
```

## 技术要点

* **纯 HTTP 登录**：无 Playwright 依赖，服务器可用
* **三段式登录链路**：预登录 → SSO atLogin → SSO 换 ST
* **极验 v4 滑块**：自研 Geeked 求解器
* **手阅作业**：逆向 zxbReport POST 接口（token 必须放请求头），网页版无入口但接口开放
* **accounts.json 绑定系统**：支持一账号多平台绑定
* **考试列表缓存**：5 分钟 TTL，减少 API 调用
* **t2i 图片渲染**：黑白极简风格，失败降级纯文本
* **答题卡渲染**：PIL 批注，坐标自适应毫米/百分比模板，标签宽度自适应缩字

## 许可

作者：JerryPig678
