<div align="center">

# Sourced Intel

**一个问题进去,一份有来源、可核查的报告出来。**

一个七节点的 [LangGraph](https://github.com/langchain-ai/langgraph) 流水线,把
*"深圳最近三个月的储能产业动态"* 变成一份自包含的 HTML 报告——**每一个数字都由代码算出,
每一条引用都指向真实文档,每一处失败都写出来而不是藏起来。**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.12-blue.svg)](#快速开始)
[![运行时依赖: 4](https://img.shields.io/badge/runtime%20deps-4-brightgreen.svg)](pyproject.toml)
[![离线测试: 287](https://img.shields.io/badge/offline%20tests-287-brightgreen.svg)](#测试)
[![报告离线可渲染](https://img.shields.io/badge/report%20renders-offline-brightgreen.svg)](#报告)

<br />

![报告:刊头、执行摘要与关键数字](docs/screenshots/screenshot-1-overview.png)

<sub>真实输出。README 里的每张截图都来自
[`examples/demo-report.html`](examples/demo-report.html) 这个模板渲染的结果,没有一张是摆拍的。</sub>

</div>

---

## 30 秒跑起来

```bash
pip install -r requirements.txt
python3 scripts/demo.py            # 写出 examples/demo-report.html
```

这会用内置样例数据渲染一份完整报告。**demo 本身不发任何 HTTP 请求、不需要 API key**——
仓库里的 `examples/demo-report.html` 就是这条命令的输出,直接双击打开也行。

要跑真实数据源,需要一个 [智谱 GLM](https://open.bigmodel.cn) 的 key(免费额度就够):

```bash
python3 scripts/preflight.py       # 检查 key、配置、每个数据源
cp .env.example .env               # 然后填 GLM_API_KEY
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                   # 得到 `sourced-intel` 命令
sourced-intel run "深圳最近三个月的储能产业动态"
```

---

## 为什么它不是又一个「让 LLM 写报告」的壳

自动调研的难点不是生成文字,而是让输出**可核查**。三条规则贯穿整个代码库,而且是用结构
强制、不是靠「好好跟模型说」:

| 规则 | 怎么强制的 |
|---|---|
| **模型永远不提供 URL** | 模型要填的 schema 里根本没有 `source_url` 字段——它只返回一个 `doc_id`,真实 URL 由代码从自己的文档表回填。编造引用不是「不太可能」,而是*结构上写不出来*。 |
| **模型永远不做算术** | 计数、求和、占比、分组都在 `nodes/aggregate.py` 里用纯代码算。分析提示词拿到的是算好的数字,并被告知每个数字必须原样出现在输入里。少于 5 条记录时系统拒绝下任何结论,并明确说出来。 |
| **缺失的数据就让它缺失** | 没有任何字段被回填。没有主体的记录打 `needs_review` 移到附录,而不是猜一个;只写了月份、没写具体哪天的日期,绝不会被渲染成「当月 1 号」。 |

还有一条关于报告本身的:

> **失败要披露,不藏。** 报告最后有一个 *Coverage & failures* 章节,点名每一个失败或降级的
> 数据源。一个悄悄丢数据的流水线,比一个承认丢数据的更糟。

---

## 报告

单文件 HTML——没有构建步骤、没有资源目录,能直接邮件转发。

<table>
<tr>
<td width="50%"><img src="docs/screenshots/screenshot-2-analysis.png" alt="区县分级着色地图与月度趋势图" /></td>
<td width="50%"><img src="docs/screenshots/screenshot-3-findings.png" alt="带内联引用编号的结论" /></td>
</tr>
<tr>
<td><sub><b>地图和图表都是内联 SVG。</b> 由浏览器直接绘制,不需要脚本、不请求 CDN,断网也能看、打印也正常。Leaflet 只在加载成功时才把地图升级成可交互的瓦片。</sub></td>
<td><sub><b>一个来源,一个编号。</b> 每个不同的 URL 只分配一次引用编号,结论和证据表共用,最后在参考文献列表里统一解析。同一个编号绝不会指向两份不同的文档。</sub></td>
</tr>
<tr>
<td><img src="docs/screenshots/screenshot-4-evidence.png" alt="带筛选工具栏的证据表和参考文献列表" /></td>
<td><img src="docs/screenshots/screenshot-5-dark.png" alt="同一份报告的深色模式" /></td>
</tr>
<tr>
<td><sub><b>证据表是服务端渲染的</b>,关掉 JavaScript 也能用;搜索框、类型筛选和行数是叠在上面的增强功能。</sub></td>
<td><sub><b>一套 token 同时出浅色和深色。</b> 主题在首次绘制前就应用好了,从磁盘打开时不会有颜色闪变。</sub></td>
</tr>
</table>

---

## 工作原理

```
问题
   │
   ├─① parse_intent ──── LLM + JSON schema ──────────► QuerySpec
   │                                                    │
   ├─② retrieve ──────── GDELT + china5e(并发) ────────► list[RawDoc] + failed_sources
   │                                                    │
   ├─③ extract ───────── LLM,每批×8,并发 ──────────────► list[IntelItem]
   │                                                    │
   ├─④ aggregate ─────── 纯 Python,无 LLM ─────────────► 区域/月份/类型分桶
   │                                                    │
   ├─⑤ analyze ───────── LLM 解读给定数字 ──────────────► findings + 证据 URL
   │                                                    │
   ├─⑥ generate_report ─ Jinja2 + 内联 SVG ────────────► report.html
   │                                                    │
   └─⑦ quality_gate ──── 阈值 + 重试预算 ───────────────► QualityReport
```

完整的节点契约、提示词版本和工作示例 I/O:**[`docs/workflow.md`](docs/workflow.md)**。

### 数据源

| 来源 | 角色 | 实际返回什么 |
|---|---|---|
| **GDELT DOC 2.0** | 广度(全球新闻) | `url, title, seendate, domain, language, sourcecountry`——**只有标题,没有正文** |
| **china5e.com** | 深度(行业新闻) | 标题 + 发布时间 + 来源 + **完整正文**,每页 40 条 |
| **DataV GeoAtlas** | 地理底图 | 区县 GeoJSON,含 `adcode / name / centroid / geometry` |

> 选这几个来源靠的是连通性实测,不是读需求文档:原始需求里点名的三个来源已经死了——一个被
> DNS 劫持、一个域名已注销、两个是只有 JS 的空壳。证据在
> [`docs/PITFALLS.md`](docs/PITFALLS.md)。

**降级是特性,不是事故。** GDELT 对共享 IP 限流到大约每 20–40 秒才成功一次,所以它被当作
可选项:每次运行只试一次、30 分钟磁盘缓存,失败了报告照样出,并且把失败写出来。两个
「以不同方式失败」的来源,比一个「永远成功」的更值钱。

---

## 工程笔记

### 它当初要满足的四个要求

| 要求 | 实现 | 验证方式 |
|---|---|---|
| **状态快照** | `state.py::save_snapshot`,包装在 `graph.py` | `ls data/runs/<run_id>/snapshots/` |
| **容错** | 每个节点 `try/except`、有界重试、批次减半 | 弄坏一个来源,运行照样完成 |
| **追踪** | `tracing.py` → `trace.jsonl` | `cat data/runs/<run_id>/trace.jsonl` |
| **上下文裁剪** | `state.py::NODE_STATE_SLICES`(每节点白名单) | `node_payload_stats()` |

### 那些本不该这么难的事

真正的活儿大多花在「只有上了负载才暴露」的失败模式上。全部写进了
**[`docs/PITFALLS.md`](docs/PITFALLS.md)**——37 条,每条都是*实际观察到*的,不是脑补的:

- **一个看起来像成功的吞异常。** `render_html` 在任何模板错误时回退到 Markdown,运行时
  行为是对的、失败行为是糟的:一个缺失的 key 会把「排版好的报告」静默变成「纯 Markdown
  文件」,而运行还报 `completed`。现在会记一条 `html_render_fallback` 追踪事件。
- **一个伪装成数据丢失 bug 的并发上限。** 调大 `GLM_MAX_CONCURRENCY` 确实让运行*看起来*
  快了——因为失败的批次被整批丢掉了。抽取节点现在会把被拒的批次减半(8 → 4 → 2 → 1)重试。
  实测上限是 **2 个并发请求**;每种配置跑十次,并发 3 时成功 4–5 次/10,跟间隔无关。
- **一个没跑测试却报成功的测试工具。** 沙箱的 `sitecustomize.py` shim 拦截了
  `mkdir`/`unlink`,于是 `python -m pytest` 报 127 passed + 48 errors,而 `python -E` 报
  175 passed。工具链眼里的世界,不是文件系统。

### 测试

```bash
pytest tests/ -m "not slow" -q     # 287 个测试,~2 秒,无网络、无凭证
python3 -m unittest discover -s tests -p "test_parsing.py"   # 58 个测试,纯标准库
```

测试套件刻意拆成三层:

- **零安装。** 解析层、金额/日期对账、来源适配器,只用 `unittest` 和标准库测。CI 用一个
  什么都不装的 job 跑它——一旦「零安装」这个说法不成立,那个 job 就失败。
- **离线。** 287 个测试,覆盖带桩 provider 的流水线节点、报告契约(章节编号、引用注册表、
  日期精度、HTML 转义)、demo 数据集的内部一致性、CLI。
- **`slow`。** 一次针对真实 API 的端到端运行,用显式标记排除在 CI 之外,这样新加的
  「忘了打标记」的测试会响亮地失败,而不是被悄悄跳过。

### 复现性能数字

`scripts/run_stats.py` 读流水线自己写的追踪日志,打印的是分布而不是一个头条数字:

```bash
python3 scripts/run_stats.py --exclude 205122   # 一次运行,手动排除,已命名
```

写作时记录的十次运行:**41–110 s 总耗时,中位数 61 s**。波动全来自外部——抓取(0.9–42.5 s)、
抽取(7.5–33.5 s,跨三波串行)、还有一次延迟摆动四倍的分析调用。任何单一数字(包括上面这些)
都只是轶闻。输出里的 `markdown fallbacks` 一行**必须读 0**。

---

## 完整快速开始

### 方案 A —— Docker(Windows 推荐)

```bash
cd sourced-intel
cp .env.example .env          # 然后填 GLM_API_KEY
docker compose config --quiet && echo "compose config OK"
docker compose up --build
```

`cp` 那步不能省:两个服务都声明了 `env_file: .env`,缺文件会让 Compose 在构建任何东西之前就停。

### 方案 B —— 本地

```bash
# 0. 两个什么都不装的诊断脚本,直接告诉你*为什么*哪里坏了
python3 scripts/preflight.py                 # 环境、配置、凭证、每个来源
python3 scripts/rehearse_glm.py --docs 3     # 真实链路:抓取 → 解析 → 提示词 → GLM

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[ui,api]"
cp .env.example .env                         # 然后填 GLM_API_KEY

python3 scripts/demo.py                      # 渲染样例报告(不需要 key)
sourced-intel run "深圳最近三个月的储能产业动态"   # 真实运行
sourced-intel stats                            # 汇总你做过的运行
```

Windows 上那两个零安装诊断脚本写法不变,只有斜杠方向变(`python scripts\preflight.py`)。
其余完全一致——所有文件 I/O 都显式 UTF-8,中文在默认 GBK 控制台里也能正确显示。

### 结果去哪了

```
data/runs/run-20260918-.../
├── report.html      单文件:地图 + 图表 + 结论 + 证据 + 参考文献
├── trace.jsonl      每节点一行:耗时、模型、提示词版本、token
└── snapshots/       每个节点之后的完整状态,外加 99_final
```

---

## 目录结构

```
sourced-intel/
├── src/sourced_intel/
│   ├── cli.py                    `sourced-intel` 入口(demo / run / stats / preflight)
│   ├── demo.py                   内置离线样例,以及它为什么存在
│   ├── parsing.py                零依赖的文本、金额、日期解析
│   ├── schemas.py                Pydantic v2 模型——LLM 要填的契约
│   ├── graph.py, state.py, tracing.py, llm.py, api.py, config.py
│   ├── nodes/                    七个流水线节点
│   ├── prompts/                  ★ 提示词文件,按文件名做版本管理
│   ├── tools/                    HTTP 客户端、HTML 解析、来源适配器
│   └── report/                   Jinja2 模板 + 渲染器 + 内联 SVG 地图/图表
├── app/streamlit_app.py          demo UI
├── scripts/
│   ├── preflight.py              纯标准库的环境 + 来源检查
│   ├── rehearse_glm.py           纯标准库的真实链路演练
│   ├── run_stats.py              复现性能分布
│   └── check_sources.py          证明每个来源可达
├── tests/                        零安装、离线、真实三套
├── examples/demo-report.html     ← `sourced-intel demo` 的输出,已提交
└── docs/
    ├── workflow.md               ★ 完整工作流:节点契约、提示词、I/O
    ├── architecture.md           组件与数据流细节
    ├── PITFALLS.md               37 条已验证的坑,每条附复现命令
    ├── DEVELOPMENT_LOG.md        逐轮开发日志
    ├── productization.md         从 demo 到产品的路径
    ├── ai_usage.md               AI 怎么用的、它的输出怎么被核对的
    └── screenshots/              本 README 里的图片
```

---

## 配置

每个值都可以放在 `.env` 里或作为真实环境变量;`Settings.redacted()` 打印生效配置时绝不会泄露 key。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `glm` | `glm`(智谱,OpenAI 兼容)或 `ollama`(本地兜底) |
| `GLM_API_KEY` | — | `glm` **必填**;必须含一个点。`./scripts/preflight.py` 会告诉你有没有 |
| `GLM_MODEL` | `glm-4.7` | **对着 `GET /models` 核对**——名字写错会返回一个误导性的 `429 code 1305` |
| `GLM_THINKING` | `off` | `off` 在允许的模型上关掉推理通道;对始终推理的模型,客户端自己降到 `low` |
| `GLM_MAX_CONCURRENCY` | `2` | 免费额度下的实测上限——见 PITFALLS #32–#34 |
| `OLLAMA_MODEL` | `qwen3.5:4b` | 离线兜底 |
| `CONFIDENCE_THRESHOLD` | `0.6` | 低于这个值,记录移到附录 |
| `EXTRACT_BATCH_SIZE` | `8` | 每次抽取调用处理的文档数 |
| `NODE_MAX_RETRIES` | `2` | 每个节点的重试预算 |

---

## 限制,直说

- **GDELT 天生不可靠。** 每次运行只试一次、磁盘缓存、优雅降级。它只贡献标题,英文标题上的
  金额抽取大多失败。
- **正文只来自一个来源。** 百度系页面——GDELT 中文结果里的一大块——会拦截自动抓取。
- **区域映射是一张 32 条手工维护的表。** 未知区域回退到查询区域,并计入 `unmapped_items`,
  让这个缺口可见。
- **没有实体消歧。** 如果一家来源写「宁德时代」、另一家写「宁德时代深圳研发中心」,就是两个
  主体。报告在 *Flagged for human review* 里标出来,而不是靠猜去合并。
- **底图瓦片首次打开需要网络。** 报告里其余的一切——包括分级着色地图本身——都是自包含的。

---

## 用 AI 建的,而且核对过

这个项目是用 AI 编程助手建的。有意思的不是生成,是分工:

- **助手**用真实 HTTP 请求探了每个公开数据源,汇报原始响应字段;起草了模块骨架、提示词和
  报告模板;用真实页面写了测试夹具。
- **人**决定了哪些来源通过了实测,抓住了来源优先级那个错误(GDELT 被限流、且不返回正文,所以
  扛不起延迟目标),选了 provider,定了每一条验收标准。
- **核对循环**——在写任何解析代码之前先用 `curl` 复核助手的说法、对着逐字页面夹具做单测、
  拒绝一份「读需求」而不是「读实测」拼出来的来源方案——记录在
  [`docs/ai_usage.md`](docs/ai_usage.md)。

---

## License

[MIT](LICENSE)。
