# Harness 评测方法论与发现

> 目标：同一模型（`deepseek-v4.1-flash`）下，比较 4 套 coding harness 在
> SWE-bench Verified 固定子集上的解决率与效率，并据此优化 Sluice。

## 一、评测栈选型（调研结论）

| 层 | 选择 | 理由 |
| --- | --- | --- |
| 基准 | SWE-bench **Verified**（500 例） | 事实标准，官方 harness 可复现 |
| 数据 | `SWE-bench/SWE-bench` test parquet（33MB） | 新版数据集自带 `image` / `eval_script` / `log_parser` / `eval_type`，swebench 5.x 直接可用 |
| 镜像 | [LogicStar/SWE-Bench-Verified-Compressed](https://huggingface.co/LogicStar/SWE-Bench-Verified-Compressed) | 5.4GB 压缩包 = 全部 500 镜像（原始 100GB+ 传输）；走 hf-mirror 国内直连无需代理 |
| 评分 | `swebench` 5.0.2 官方 `run_evaluation` | 应用 patch → 容器内跑 FAIL_TO_PASS/PASS_TO_PASS |

### 环境坑（已解决，脚本化）

1. **Docker Hub 直连不可用**，加速器（daocloud/1panel/rat.dev…）白名单拒绝
   `swebench/*` 镜像 → 用 LogicStar 压缩归档离线加载。
2. **Docker 数据盘在 C 盘**（`docker_data.vhdx` 已 213GB，含用户既有 k3s/kind 镜像，
   不可清空）→ 不整体搬迁，改为**选择性加载**（`tools/select_load_images.py`：
   从大 tar 里只取需要的镜像重建小归档），`saved.tar` 落 D 盘，避免 C 盘写爆。
3. **swebench 在 Windows 上把 `eval.sh`/`patch.diff` 写成 CRLF** → 容器内 heredoc
   打补丁失败、`conda activate` 失效、`pytest: command not found`。金标准 0/6。
   修复：`tools/patch_swebench_windows.py` 给两处 `write_text` 加 `newline="\n"`。
4. **`test_output.txt` 按 GBK 写盘崩溃** → 设 `PYTHONUTF8=1`。
5. **psf/requests 测试依赖 httpbin 网络**，金标准也只有 3/6 通过 → 子集剔除
   network-dependent 仓库，改用 flask / seaborn / pytest / django / sympy。
6. **镜像基线自带未提交改动**（exec 位、build/ 残留）→ 提取工作区后打 baseline
   commit，`git diff HEAD` 只含 agent 改动。

修复后金标准：**5/5 resolved**（子集：flask-5014、seaborn-3069/3187、pytest-10051/10081）。

## 二、对比设计（公平性）

- **同一工作区**：镜像 `/testbed` 复制到宿主 → bind mount 回容器 `/testbed`，
  文件工具与容器命令操作同一份文件。
- **同一执行环境**：所有 harness 的 shell 都进同一个评测容器（自带 conda env），
  模型调用留在宿主。
- **同一模型/预算**：`deepseek-v4.1-flash`、temperature 0、~30 步预算。
- **同一裁判**：统一 `git diff HEAD` 取 patch，官方 swebench 评分（不用各 harness
  自己的提交协议）。
- **防作弊**：统计 patch 是否触碰测试文件。

## 三、结果（pilot 子集 5 例，`deepseek-v4.1-flash`，官方 swebench 评分）

| harness | resolved | 平均墙钟 | 平均步数 | 平均 patch | 改测试文件 |
| --- | --- | --- | --- | --- | --- |
| **Sluice v2（优化后）** | **5/5** | 868s | 83 | 1268B | 0 |
| mini-swe-agent（100 行基线） | 5/5 | 227s | 49 | 2610B | 0 |
| **pi**（TypeScript 全功能） | 4/5 | 154s | 30 | 1052B | 0 |
| Sluice v1（优化前） | 2/3 | 653s | 80 | 4427B | 0 |
| mini-agent-harness | 1/5 | 85s | 26 | 1111B | 0 |

子集：`pallets__flask-5014`、`mwaskom__seaborn-3069`、`mwaskom__seaborn-3187`、
`pytest-dev__pytest-10051`、`pytest-dev__pytest-10081`（均为离线可跑；requests 因依赖
httpbin 网络被剔除，金标准在该类上也只有 3/6）。

### 解读

1. **优化有效**：Sluice 从 2/3 → **5/5**，与 100 行的 mini-swe-agent 打平；
   平均 patch 从 4.4KB 降到 1.3KB（证据污染消除），且没有一套 harness 改测试文件作弊。
2. **速度是 Sluice 的短板**：868s vs mini-swe-agent 227s、pi 154s（慢 4-5×）。
   原因明确——每个状态重建 worker 上下文 + 每次转移一次裁决 LLM 调用。
   状态机的「可审计、可追责」是用 token 与延迟换来的。
3. **pi 是「工程完整度」的参照**：4/5、154s、30 步、patch 1.0KB——功能和效率都在第一梯队，
   说明大型 TS harness 的工程优势真实存在。
4. **mini-agent-harness 快但浅**：85s/26 步，只解出 1/5；「短平快」在这个基准上不够。
5. mini-swe-agent 是强基线：极简 + 线性历史 + 无 per-state 开销，本子集全解。

### 结论：Sluice 的定位

在这个基准上，Sluice 的价值不是「更快更准」，而是**过程可控、责任可追**：
- 每次转移都有裁决记录（谁批准、什么理由、哪些条件概率不足）；
- 交付前有显式验收标准核对；
- 执行者不会跳过「复现」直接改代码（状态机强制）。

代价是 4-5× 时间。后续优化方向：**保留裁决记录的同时降低开销**——
同一 issue 内复用 worker 上下文、裁决者批量裁决、把「已满足的条件」缓存。

### token 成本（同一实例 `pallets__flask-5014` 实测）

| harness | prompt tokens | completion | 模型调用 | 墙钟 |
| --- | --- | --- | --- | --- |
| mini-swe-agent | 23,850 | 1,192 | 7 | 20s |
| Sluice v2 | 251,859 | 20,343 | 39 | 210s |
| pi | n/a（流式接口不回报 usage） | — | — | 30s |

**Sluice 的 token 消耗是 mini-swe-agent 的 ~11 倍**：平均每次调用 prompt 6.4k
（msa 是 3.4k），调用次数 39 vs 7。根因是「每个状态重建 worker 上下文」——
状态机的结构化提示（流程规格 + 黑板 + 证据）在每次工具循环里都重复注入。
这是下一轮最值得优化的点：**保留裁决记录，但让 worker 在状态内复用上下文**。

### pi 的接入方式（可复现）

评测镜像里没有 Node，因此把宿主准备的 Linux Node（v24.9.0）+ pi 包（1.5.0）
+ `models.json`（指向本项目的 OpenAI 兼容网关）挂进容器，在 `/testbed` 直接跑
`pi --mode json`。这样 pi 的内置 bash 工具天然落在镜像自带的 conda 环境里，
与其他 harness 完全同环境。步数从 JSON 事件流的 `tool_execution_start` 统计。

## 四、已实施的 Sluice 优化（v1 → v2）

| # | 问题（评测暴露） | 修复 | 证据 |
| --- | --- | --- | --- |
| 1 | 执行者停滞 → 合成空目标请求 → 裁决者只能驳回 → 同状态再停滞，形成死循环（seaborn 两例因此被 Watchdog 终止） | **停滞自动前进**：`Harness._advance_stalled_request` 按「到终态最短步数」挑一个向前状态作为提案，把「从零想目标」变成「是/否判断」 | `tests/test_smoke.py::test_stall_request_advanced_forward` |
| 2 | 单状态 10 步预算对真实仓库偏紧，反复耗尽 | `max_steps_per_state` 10 → 15，`max_cycles` 10 → 14 | flask 265s → 158s |
| 3 | worker 把 `fix_evidence.log`/`repro_*.py` 写进仓库，污染 patch（flask 的 patch 6.9KB 里只有 1 个文件是真修复） | 状态机 `worker_goal` 明确要求「证据写进 summary/evidence 字段，不要在仓库里创建日志/脚本」 | patch 6.9KB → 435B，且只含 `src/flask/blueprints.py` |

## 五、仍未解决（下一轮）

1. **Sluice 的 per-state 上下文重建成本高**：每换状态重建 worker 上下文 + 裁决者一次 LLM 调用，
   在 seaborn 这类大仓库上仍比 mini-swe-agent 慢 2-7 倍；后续考虑
   「同一 issue 内沿用 worker 上下文」或「裁决者批量裁决」。
2. **pi 尚未接入**：它的 shell 工具在宿主执行，拿不到容器内 conda 环境，
   需要额外容器化改造（或接受宿主环境的不公平对比）。
3. 子集只有 5 例（离线可跑部分），扩展子集（django/sympy）需要再加载 10+ 镜像。

