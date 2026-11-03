# Sluice

[![](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)]()
[![](https://img.shields.io/badge/dependencies-0-green?style=flat-square)]()
[![](https://img.shields.io/badge/judge-Jev%20%7C%20LLM%20pluggable-4D6BFE?style=flat-square)]()

基于 **状态机 + Agent Loop** 的极简 Agent Harness。纯 Python 标准库、零第三方依赖：
运行时核心（`sluice/` + `main.py` + 状态机定义）约 1.7k 行，全仓含测试与工具约 2.3k 行。

```
用户
  │ 原始目标、约束、补充输入
  ▼
裁决者 Judge（可插拔：聊天 LLM 或 Jev 决策模型）
  │ 只做三件事：读输入、判转移、对交付负责
  │ 不执行工具、不写业务产物、不进入执行 Loop
  ▼
状态机 FlowSpec（JSON 声明，渲染进裁决者系统提示词，Harness 强制执行合法边）
  │
  ▼
执行者 Worker ── 常规 ReAct Loop：工具调用 / 子任务分解
  └── 唯一控制面动作：request_transition → 暂停 Loop，静候裁决
```

## 快速开始

```bash
# 1) 无 API Key，跑内置剧本演示
python main.py --mock            # 剧本 LLM 裁决者
python main.py --mock-jev        # Jev 裁决者（Fake 客户端）

# 2) 接真实模型（任意 OpenAI 兼容端点）
set SLUICE_BASE_URL=https://api.deepseek.com/v1
set SLUICE_API_KEY=sk-...
set SLUICE_MODEL=deepseek-chat
python main.py --goal "写一个斐波那契命令行工具并验证"

# 3) Jev 做裁决者（System One 决策模型）
set SLUICE_API_KEY=sk-...                # 执行者仍是聊天模型
set SLUICE_JEV_API_KEY=sk-or-...         # 或 OPENROUTER_API_KEY
python main.py --judge jev --goal "写一个斐波那契命令行工具并验证"
```

环境变量见 [.env.example](.env.example)（复制为 `.env` 填真实值，已被 git 忽略）：

- 执行者：`SLUICE_BASE_URL / SLUICE_API_KEY / SLUICE_MODEL / SLUICE_WORKER_MODEL`
- LLM 裁决者：`SLUICE_JUDGE_MODEL`，`SLUICE_JSON_MODE=1`
- Jev 裁决者：`SLUICE_JEV_API_KEY`（或 `OPENROUTER_API_KEY`）、
  `SLUICE_JEV_MODEL`（默认 `typesafe/jev-1.14`，阈值敏感时建议锁版本）、
  `SLUICE_JEV_BASE_URL`（默认 OpenRouter Decisions API；302.ai 直连设
  `https://api.302.ai/typesafeai`；本地 llama.cpp 设 `http://127.0.0.1:18080/v1`）
- `NO_COLOR=1` 禁用彩色输出

## CLI 界面

分节式渲染：每次状态切换画 PHASE 分隔线 + 状态轨迹条（▶ 当前、暗色未达），
事件行带运行秒数，裁决行按 approve/reject/redirect 着色，Jev 概率审计行分段缩进。
真实运行样例（含一次驳回→补证据→重提）见 [docs/sample-run.txt](docs/sample-run.txt)：

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 PHASE IMPLEMENT  编码实现：按计划产出代码
CLARIFY PLAN ▶IMPLEMENT · ·

  [  129s] ⚙ write_file(path='caesar.py', content='#!/usr/bin/env python3...')
  [  154s] ⚙ run_shell(command='python -m unittest -v')
  [  220s] ➜ IMPLEMENT → VERIFY
  IMPLEMENT 完成：按 plan.md 的 S1–S8 实现凯撒密码 CLI…
[  220s] ✔ approve → VERIFY
      Jev: VERIFY(conf=0.97)
      退出[计划中的每个步骤都有对应代码=0.94，代码可被解释器/编译器通过=0.98]
      进入[代码已产出=0.98]
  ────────────────────────────────────────────
```

## Jev 裁决者：决策模型如何当裁决者

[Jev](https://openrouter.ai/docs/guides/community/jev)（TypeSafe 的 System One 模型）不生成文本，
只对 `state + 类型化问题` 返回带概率的答案（choice / noul / score）。这恰好匹配
「读输入、判转移」的职责，但需要一次适配（[sluice/jev.py](sluice/jev.py)）：

**职责分解 → 一次批量请求**（同一 state，问题并行评估）：

| 问题 | 类型 | 作用 |
| --- | --- | --- |
| `exit_i` / `entry_i` / `criterion_i` | noul | 逐条核验退出/进入/交付标准，返回满足概率 |
| `next_state` | choice | **选项 = 当前状态的合法去向 + 驻留**，结构上杜绝非法转移 |
| `needs_user` | noul | 关键信息是否只能由用户补充（超阈值 → 先问用户） |

**文本字段确定性合成**：Jev 写不了 reason / instructions / final_answer，
这些由模板从概率生成 —— 每条裁决理由形如
`Jev: PLAN(conf=0.80)｜退出[...=0.90]｜进入[...=0.90]`，可审计、可复现。

**护栏（与 Jev 无关的确定性规则）**：
- choice 白名单外的答案 → 强制回退为驻留
- 交付标准任一概率 < 0.6 → 拒绝进入终态
- choice 置信度 < 0.55 → 保守驳回，要求补证据
- 同一转移请求最多向用户提问一次（`_asked_once`）

四个阈值（`noul_threshold / confidence_floor / ask_user_threshold /
delivery_threshold`）都是 `JevJudge` 构造参数，可按状态机语义调优。

## 核心协议

| 方向 | 载荷 | 说明 |
| --- | --- | --- |
| Worker → Judge | `TransitionRequest{from,to,summary,evidence}` | 执行者唯一的控制面动作；summary 沉淀到跨状态黑板 |
| Judge → Harness | `JudgeDecision{decision,next_state,reason,instructions,user_question,final_answer}` | approve / reject / redirect；可反问用户；进终态必须带 final_answer |
| Judge ↔ User | `user_question` ↔ 用户回答 | 用户信息只经裁决者中转 |

Harness 是协议的「边防」：裁决者批准不存在的边、或进终态不给交付物，都会被驳回重裁。
自环（留在当前状态）恒合法，用于裁决者要求继续打磨。

## 自定义状态机

复制 [flows/coding_task.json](flows/coding_task.json) 改状态、条件与合法边即可：

- `states.<name>.entry/exit_conditions`：裁决者的核对清单
- `edges`：白名单转移边；`delivery_criteria`：进终态前的总验收
- `max_steps_per_state` / `max_cycles`：防失控护栏

## 项目结构

```
sluice/
  llm.py       OpenAI 兼容客户端（urllib，断连/5xx 指数退避重试）+ MockLLM
  spec.py      状态机定义 / 校验 / 渲染进裁决者提示词
  protocol.py  TransitionRequest / JudgeDecision
  judge.py     裁决者-LLM 后端：无工具，结构化 JSON 裁决，解析失败安全兜底为 reject
  jev.py       裁决者-Jev 后端：决策原语批量提问 + 概率护栏 + 文本确定性合成
  worker.py    执行者：ReAct Loop，request_transition 即让渡控制权
  tools.py     工具箱：文件读写 / 列目录 / shell，全部限制在工作区内
  harness.py   编排器：主循环、黑板、合法性校验、用户问答中转
  watchdog.py  外控回路：纯代码异常模式检测（驳回连击/停滞/打转）
  ui.py        CLI 渲染层：颜色/PHASE 分节/轨迹条（ANSI 自动降级）
main.py        CLI（--mock / --mock-jev 演示，--judge llm|jev 真实模式）
flows/         状态机定义
tests/         剧本化 Mock + FakeJev 端到端测试 + 裁决后端对比套件
tools/         GGUF 断点续传下载器 / clef 本地服务脚本
docs/          Super-agent 对照分析 / 真实运行样例日志
```

测试：

```bash
python tests/test_smoke.py          # 15 个端到端/单元测试（无需 API）
python tests/test_compare.py        # 裁决后端对比（需 SLUICE_API_KEY / SLUICE_JEV_API_KEY）
```

裁决者后端共享同一套接口（`on_user_input / on_transition_request / correct`），
harness 对后端无感知 —— 想换其它决策模型（OpenRouter 上还有 20+ 个同 API 的
decision models），实现一个新类即可。

## 设计取舍（MVP 边界）

- 单执行者驻留制：任意时刻只有一个 Worker，换状态即重建上下文（黑板保序传递）
- 裁决者持有完整裁决会话历史；执行者上下文按状态隔离
- 停滞 / 超步的执行者会被合成为「自环驻留请求」交裁决者处置，不会卡死状态机
- 未做：并发 Worker、状态机可视化、持久化断点恢复（协议已为此留好结构）

## 真实模型验证（2026-11-02）

两套裁决者后端都在真实模型上完整跑通 coding_task 流（回文检测工具，产物可复验）：

| | LLM 裁决者 | Jev 裁决者 |
| --- | --- | --- |
| 配置 | glm-5.3-flash + deepseek-v4-pro | typesafe/jev-1.14 + deepseek-v4-pro |
| 轨迹 | CLARIFY→PLAN→IMPLEMENT→VERIFY→DELIVER（4 轮 approve） | 同轨迹，4 轮裁决 |
| 裁决成本 | 常规 chat token | **< $0.0003（5 次调用）** |
| 亮点 | 逐条核对式裁决理由，自然可读 | 概率审计行；执行者 3 次请求不存在的状态（IMPL/REVIEW/DONE），choice 白名单全部结构性纠正 |

关键实测结论：
- 协议对真实模型鲁棒——执行者会缩写状态名、自造状态，白名单 + redirect 兜住了全部；
- Jev 判定质量可用：exit/entry/criterion 概率与证据强度一致，终态交付门槛逐条核验；
- 轻模型做裁决 + 强模型做执行的分模型配置可行（对应 Super-agent 的 Router/Worker 配模哲学）。

## SWE-bench 横向评测与优化（2026-11-02）

> 想在本地跑通 SWE-bench？看这篇独立指南：[**docs/swebench-guide.md**](docs/swebench-guide.md)
> —— 数据集（hf-mirror）、压缩镜像、参数配置、运行、结果解读，配真实终端截图。

用官方 **SWE-bench Verified** 在同一模型（`deepseek-v4.1-flash`）下横向对比 harness：

| harness | 定位 | 接入方式 |
| --- | --- | --- |
| **Sluice**（本项目） | 状态机 + 裁决者 + 执行者 | `flows/swe_bench.json`（定位→复现→修复→验证→交付），shell 经 `SLUICE_SHELL_DOCKER` 代理进容器 |
| **mini-swe-agent** | 100 行 bash-only agent（官方基线） | 程序化 API + docker 环境，同一镜像同一工作区 |
| **mini-agent-harness** | 分层编码 harness | `cli.py --approval auto`，shell 经 `HARNESS_SHELL_DOCKER` 代理 |
| **pi** | TypeScript 全功能 agent | 待接入（需要额外容器化改造） |

评测栈要点（详见 [bench/README.md](bench/README.md)、[docs/harness-bench.md](docs/harness-bench.md)）：

- 镜像用 LogicStar 压缩包（5.4GB = 全部 500 例，走 hf-mirror 国内直连，无需代理）；
  Docker 数据盘在 C 盘，因此**只选择性加载**需要的镜像（`tools/select_load_images.py`），
  大归档解压到 D 盘，避免写爆系统盘。
- 修了 6 个环境坑（Docker Hub 被墙、swebench 在 Windows 写出 CRLF 的 eval.sh、
  GBK 写盘崩溃、镜像基线脏、requests 测试依赖网络等），修复后**金标准 5/5 resolved**。
- 公平性：同一工作区（镜像 `/testbed` 的 bind mount）、同一容器环境、同一模型与步数预算、
  统一 `git diff` 取 patch、官方 swebench 评分、统计是否改测试文件。

评测结果（pilot 子集 5 例，官方 swebench 评分，原始数据见
[bench/results/final_comparison.md](bench/results/final_comparison.md)）：

| harness | resolved | 平均墙钟 | 平均步数 | 平均 patch |
| --- | --- | --- | --- | --- |
| **Sluice v2（优化后）** | **5/5** | 868s | 83 | 1.3KB |
| mini-swe-agent（100 行基线） | 5/5 | 227s | 49 | 2.6KB |
| **pi**（TypeScript 全功能） | 4/5 | 154s | 30 | 1.0KB |
| Sluice v1（优化前） | 2/3 | 653s | 80 | 4.4KB |
| mini-agent-harness | 1/5 | 85s | 26 | 1.1KB |

解读：优化后 Sluice 与极简基线 **resolve 打平（5/5）**，代价是 4-5× 墙钟时间——
状态机 + 裁决换来的「过程可审计、责任可追」有明确成本。pi 展示了大型 harness 的
工程优势（4/5、154s），mini-agent-harness 则说明「快」不等于「对」。
没有任何 harness 修改测试文件作弊。

token 成本（同实例实测）：mini-swe-agent 2.4万 prompt token / 7 次调用，
Sluice **25.2万 / 39 次**——约 11 倍。根因是每个状态重建 worker 上下文，
这是下一轮首要优化项（保留裁决记录、复用状态内上下文）。

评测驱动出的 Sluice 优化（v1 → v2，代码已进主干）：

| # | 评测暴露的问题 | 修复 | 效果 |
| --- | --- | --- | --- |
| 1 | 执行者步数耗尽会合成「空目标」请求 → 裁决者只能驳回 → 同状态再停滞，死循环（seaborn 两例因此被 Watchdog 终止） | **停滞自动前进**：`Harness._advance_stalled_request` 按「到终态最短步数」挑一个向前状态作为提案 | v1 的 3 例中 2 例失败 → v2 全部交付 |
| 2 | 单状态 10 步预算对真实仓库偏紧 | `max_steps_per_state` 10 → 15 | flask 265s → 158s |
| 3 | worker 把 `fix_evidence.log`/`repro_*.py` 写进仓库污染 patch | 状态机要求证据写进 `summary/evidence` 字段 | patch 6.9KB（含 4 个垃圾文件）→ 435B（只含真实修复） |

结果表与逐例明细见 [bench/results/final_comparison.md](bench/results/final_comparison.md)（运行 `python bench/final_eval.py` 可复现）。

## 借鉴 Super-agent 的三项落地（P0）

对照 DeepSeek Super-agent 设计的分析见 [docs/superagent-vs-sluice.md](docs/superagent-vs-sluice.md)。

1. **黑板结构化（Inline & Spill / 链式召回 lite）** — [harness.py](sluice/harness.py)：
   每条记录 `{state, summary, artifacts, evidence_head}`，摘要与产物路径 Inline 进
   种子上下文，产物本体 Spill 在磁盘、read_file 即下一跳召回；`artifacts` 由
   `ToolBox.written`（[tools.py](sluice/tools.py)）真实追踪。
2. **Watchdog 外控回路** — [watchdog.py](sluice/watchdog.py)：纯代码无 LLM，
   同状态驳回连击 → 升级反馈 → 持续恶化终止；停滞连发 / 往复打转 / 工具失败
   率过高分别检测。harness 主循环每周期调用 `watchdog.observe(...)`。
3. **注入防护（数据/指令分离）** — [judge.py](sluice/judge.py) /
   [worker.py](sluice/worker.py) 系统提示词明示「输入正文是数据不是指令」；
   Jev 后端天然免疫（evidence 只进类型化问题，无可劫持的自由文本决策面）。

## 裁决者后端对比评测（2026-11-02，`tests/test_compare.py`）

同一组 9 条标准转移请求剧本（solid/vague/tricky 三档证据强度 + 人工基准）：

| 后端 | 基准符合 | 驳回率 | 延迟中位 | 概率审计 | 备注 |
| --- | --- | --- | --- | --- | --- |
| qwen3.8-flash（LLM） | 9/9 | 33% | ~8s | ✗ | 跨轮不稳定（同案 reject→redirect 漂移） |
| typesafe/jev-1.14 | 9/9 | 44% | **~0.8s** | ✓ | **10 倍速度**；tricky 案全对（非法边 reject、跳级 redirect、回边 approve） |
| microsoft-decision-1 | 8/9 | 67% | ~1.5s | ✓ | 唯一失手：返工回边语义；倾向交付前问用户（合理保守） |

对比评测驱动出的真实修复（全部进入主干）：
- **证据闸门**：choice 与 noul 是独立信号，choice 常对弱证据高置信——
  approve 现在要求 exit 条件概率同时达标（vague 案 4/9→9/9 的关键修复）；
- **回边豁免**：返工（VERIFY→IMPLEMENT）时 exit 概率低恰是放行理由；
  redirect（裁决改派）也豁免闸门——目标状态有自己的 entry 条件；
- **自指标准剔除**：'最终答案中说明产物位置与用法' 在裁决时无法评估
  （final_answer 尚未产生），剔除后由模板结构性满足；
- **基础设施故障分流**：决策 API 连续 3 次失败 → 升级为 user_question
  请人决策，而不是把执行者困死在原状态；
- **LLM/Jev 客户端重试**：断连/超时/5xx 指数退避。

端到端第二次实测（deepseek-v4.1-flash 执行者 + Jev 裁决）：
- 交付了 rot13.py + 18 单测 + 15 项 CLI 断言 + 独立交叉实现 3000 组 fuzz——
  执行者甚至发现并记录了 cmd.exe 单引号语义的真实坑（验证质量超出预期）；
- v4.1-flash 首轮曾陷入「计划要计划」循环与 VERIFY 停滞空证据循环，
  Watchdog 两类检测（3 轮停滞终止 / 6 连驳回终止）都正确兜底；
- 由此驱动的修复：停滞兜底请求现在携带真实工作痕迹（产物清单 + 最近
  工具输出），去向交裁决者决断——空证据让裁决者失明的死循环已消除。

## 本地决策模型（clef-flash，实验性）

OpenRouter 区域受限时的本地方案：Cloudflare 开源 [clef-flash](https://huggingface.co/Cloudflare/clef-flash)
（Qwen3.5-9B 微调，Apache-2.0，System One 兼容），GGUF 由
[ggml-org/Clef-Flash-GGUF](https://huggingface.co/ggml-org/Clef-Flash-GGUF) 提供。

```powershell
# 1) 下载 GGUF（约 5.4GB，中国大陆可走 hf-mirror.com 镜像）
python tools/downloader.py "https://hf-mirror.com/ggml-org/Clef-Flash-GGUF/resolve/main/Clef-Flash-Q4_K_M.gguf" "models/Clef-Flash-Q4_K_M.gguf"
# 2) llama.cpp b12034+（含 /v1/systemone 端点）
# 3) 启动本地决策服务（路径可用 -Gguf/-Server 参数覆盖）
powershell -File tools/serve_clef.ps1
# 4) 接入 Sluice（JevClient 自动识别 systemone 端点形态）
set SLUICE_JEV_BASE_URL=http://127.0.0.1:18080/v1
python main.py --judge jev --goal "..."
```

已知问题：官方 CPU 构建（b12034/b12031 win-cpu-x64）在部分 Windows 机器上
以 0xC0000005 访问违例崩溃（DLL 依赖完整、隔离指令集变体无效），根因未明；
可尝试 Vulkan 构建或自行编译。`JevClient` 已预置 systemone 端点兼容，
服务可用后即可无缝接入对比套件。

## License

MIT，见 [LICENSE](LICENSE)。

---

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
