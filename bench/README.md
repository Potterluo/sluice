# Harness 横向评测（同模型，不同 harness）

目标：在**同一个模型**（默认 `deepseek-v4.1-flash`）与**同一批任务**下，比较不同
coding harness 的解决率与效率，据此优化 Sluice。

## 基准：SWE-bench Verified

| 项 | 说明 |
| --- | --- |
| 任务集 | SWE-bench **Verified**（500 例真实 GitHub issue 修复） |
| 评分 | 官方 `swebench` 包：应用模型 patch → 容器内跑 FAIL_TO_PASS / PASS_TO_PASS |
| 镜像 | [LogicStar/SWE-Bench-Verified-Compressed](https://huggingface.co/LogicStar/SWE-Bench-Verified-Compressed)：5.4GB 压缩包 = 全部 500 个镜像（原始传输 100GB+）；走 hf-mirror.com 国内直连，无需代理 |
| 子集 | 固定抽样（`bench/subset.py`）：pilot=6（requests/flask/seaborn 小仓库），extended≈18（+pytest/django/sympy） |

## Harness 一览

| harness | 形态 | 接入方式 |
| --- | --- | --- |
| **Sluice**（本项目） | 状态机 + 裁决者 + 执行者，纯 Python 标准库 | `main.py --flow flows/swe_bench.json`；shell 工具经 `SLUICE_SHELL_DOCKER` 代理进容器 |
| **mini-agent-harness** | 分层编码 harness（1.7k 行，103 测试） | `cli.py --cwd <ws> --approval auto`；`HARNESS_SHELL_DOCKER` 代理（vendored 副本 1 处最小改动） |
| **mini-swe-agent** | 100 行 bash-only agent，线性历史 | `mini -c <cfg> -t <task>`，自带 docker 环境 |
| **pi**（earendil-works/pi） | TypeScript 全功能 agent | npm 安装，print/JSON 模式（待接入） |

## 公平性设计

1. **同一工作区**：把镜像里的 `/testbed` 复制到宿主，再 bind mount 回容器
   `/testbed`。宿主的 agent 文件工具与容器的测试命令操作同一份文件。
2. **同一执行环境**：所有 harness 的 shell 命令都在**同一个容器**里执行，
   拿到镜像自带的 conda 环境；模型调用留在宿主（走宿主网络）。
3. **同一模型与预算**：同模型、temperature=0、相同步数上限与墙钟超时。
4. **同一裁判**：统一用 `git diff` 取 patch（不用各 harness 自己的提交协议），
   由官方 swebench 应用 patch 并跑测试。
5. **防作弊检查**：patch 若改动测试文件（`test_*.py` / `tests/`），单独标记。

## 指标

- **主指标**：resolve rate（官方判定 resolved）
- 效率：墙钟时间、工具调用数、裁决次数（Sluice 特有）、patch 大小
- 质量：改测试文件的比例、patch 应用失败率

## 目录

```
bench/
  subset.py          选固定子集（pilot/extended）
  build_dataset.py   生成 swebench 可直接读的本地数据集（image 指向 logicstar）
  run.py             驱动：harness × instance → patch + 指标
  evaluate.py        调官方 swebench 评分
  adapters/          各 harness 适配器 + 容器公共层
  vendor/            被评测 harness 的源码（mini-swe-agent / mini-agent-harness）
  data/              数据集 parquet
  results/           patch、日志、评分报告
  workspaces/        每实例的工作区（镜像 /testbed 的宿主副本）
```

## 运行

```bash
# 1) 选子集 + 生成评测数据集
python bench/subset.py pilot
python bench/build_dataset.py pilot

# 2) 跑一个 harness（先验证通路）
python bench/run.py --subset pilot --harness mini-swe-agent --limit 1

# 3) 跑全部 harness
python bench/run.py --subset pilot --harness all

# 4) 官方评分
python bench/evaluate.py --subset pilot --model deepseek-v4.1-flash
```

环境变量：`SLUICE_BASE_URL / SLUICE_API_KEY / SLUICE_MODEL`（三个 harness 共用同一份模型配置，
各自映射到自己的变量名）。
