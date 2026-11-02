# 调用取不到结果时：及时上报并暂停

**状态**：方案，待评审（未实施）
**涉及模块**：`code/model_runner.py`、`code/compat.py`、`code/decomposition_prover.py`、`code/pipeline.py`、`ui/`
**日期**：2026-11-02

---

## 摘要

现在遇到「模型这一轮根本没交出东西」时，流水线的表现是：**先静默消耗 1.5 小时，再抛一个 traceback 把整个进程打死**。没有分类、没有落盘记录、没有暂停点，用户唯一能做的重跑一次（真实运行里重跑了 7 次）。

本方案要的不是「把错误打印得更漂亮」，而是三件事：

1. **判定**：把「取不到结果」拆成六类可区分的原因（上下文超限 / 输出截断 / 额度耗尽 / 会话静默死亡 / 我们主动超时 / 未知），因为它们的处置完全不同——有的该缩上下文，有的该充值，有的该重试。
2. **上报**：落一份机器可读 + 人可读的 `HALT.md` / `halt.json`，写清是哪一类、哪一次调用、依据是什么、磁盘上保留了什么、下一步怎么做。
3. **暂停**：在**调用边界**停下来，用独立退出码退出，**不消耗 proof 尝试次数**、不咨询 regulator、不跑 Stage 2，磁盘状态保持在可续跑的边界上。

外加一条前置能力：**在花钱之前拦住**（P1 预算闸门）。token 类失败几乎不会自己好，自动重试只是把同一个错误再买一遍。

---

## 一、证据：一次真实运行里的失败形态

`ui/proof_runs/run_20261023_103103`，总共 5.2 小时、3.5M 输入 / 125k 输出：

| 调用 | 输入 | 输出 | 耗时 |
|---|---:|---:|---:|
| `single_prover_a1_r4_p1` | 1,398,282 | 44,683 | 95 min |
| `proof_verify_structural` | 716,179 | 17,277 | 42 min |
| `verdict_structural` | 18,627 | 202 | 2 min |
| `proof_verify_detailed` | 849,672 | 41,572 | 43 min |
| `verdict_final` | 25,174 | 277 | 2 min |
| `regulator_decide_a1_r4_p1` | 487,673 | 21,702 | 33 min |
| **`single_prover_a1_r4_p2`** | **0** | **0** | **93.5 min** |
| **`proof_verify_structural`（第二次）** | **0** | **0** | **1 min** |

从这个目录里能读出两种**不同的**静默失败，它们目前被混在一起：

### (a) 进程非零退出、无任何产出 → traceback 打死进程

`pipeline_stdout.log` 里有 **4 次** `ModelRunnerError: [codex] non_zero_exit: Codex CLI exited with code 1`，分别发生在：

| 行 | 崩在哪个 agent |
|---|---|
| 573 | `run_detailed_verification` |
| 900 | `run_structural_verification` |
| 1253 | `run_single_prover` |
| 1760 | `run_structural_verification` |

四次 `stderr` 要么是空的，要么只有一行 `Reading additional input from stdin...`。**没有任何一条告诉我们为什么。** 每次崩溃之后进程结束，用户重新启动——同一个日志里有 **8 次** `Proof Agent Pipeline` 横幅。

### (b) 正常退出、0/0 用量、93 分钟、返回一句旁白

`single_prover_a1_r4_p2` 没有 `turn.completed` 事件，所以用量是 0/0，但进程退出码是 0，`response` 是两行旁白。这条**不会**抛 `ModelRunnerError`，会被当作一次正常的 proof 尝试继续处理（`39cab90` 之后才被 `looks_like_proof` 拒掉）。

> 结论：本方案必须覆盖 (a) 和 (b) 两种，但**不能把 (b) 归类成 token 耗尽**——(b) 已经由诚实失败通道处理，硬塞进 halt 会掩盖真正的数学进展。

---

## 二、现在的行为（逐条对照代码）

| 环节 | 现状 | 后果 |
|---|---|---|
| `model_runner` 三个 runner | 无任何 timeout；`run_cli_async` 的 `timeout` 参数**从未被传入** | 挂起 93 分钟不可中断 |
| `run_cli_async(timeout=...)` | 把 `timeout` 传给 `subprocess.Popen(...)`，而 `Popen` 不认这个参数 | 该参数一用就 `TypeError`，是死参数 |
| claude runner | 对 `non_zero_exit` / `empty_response` / `subprocess_error` 一律重试 3 次（30/60/120 s） | 上下文超限类错误白烧 3.5 分钟 + 3 次请求 |
| 错误分类 | 只有 `subprocess_error` / `non_zero_exit` / `json_parse_error` / `empty_response` 四种，都是**机制**不是**原因** | 无法判断该缩上下文还是该充值 |
| `pipeline.main` | `await run_decomposition_prover(...)` 外面**没有 try/except** | 任何 runner 错误 → 裸 traceback → 进程死 → Stage 2（总结）永远不跑 |
| `decomposition/STATUS.md` | 崩在 `run_single_prover` 时停在 `PROVING` | UI 上表现为「卡在 PROVING」，而不是「已暂停」 |
| `token_usage.json` | 只记 in/out/elapsed | 0/0 的那条既可能是崩溃也可能是旁白，无法区分 |
| UI | 只能看到「有新输出 / 没新输出」和日志尾巴 | 用户得自己 grep traceback |

---

## 三、方案总览

```
                    ┌──────────────────────────────────────┐
  调用前  ── G1 ──▶ │ 预算闸门：prompt 估算 / 累计 / 连续失败 │──▶ 超限即 HALT
                    └──────────────────────────────────────┘
                    ┌──────────────────────────────────────┐
  调用中  ── G3 ──▶ │ 空闲看门狗（流式）：N 分钟无新事件      │──▶ 杀进程 + HALT
                    └──────────────────────────────────────┘
                    ┌──────────────────────────────────────┐
  调用后  ── G2 ──▶ │ 分类器：六类原因 + 是否可重试          │──▶ TokenExhausted
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                       HALT：写 HALT.md / halt.json
                             STATUS.md → HALTED
                             sys.exit(3)，不跑 Stage 2
                             不消耗 proof 尝试、不咨询 regulator
```

三道闸门对应三种时序，缺一不可：G1 省钱、G2 定因、G3 止血。**只有 G3 能解决 93 分钟挂起**，但它风险最高（见第五节分期）。

---

## 四、分类表（G2 的核心）

一次调用「取不到可用结果」归为下表六类之一。**判定顺序从上到下，先命中先定。**

| # | 类别 | 判定信号 | 可重试 | 处置建议 |
|---|---|---|---|---|
| 1 | `token_context` 上下文超限 | stdout/stderr 文本命中 `context length` / `context_length_exceeded` / `maximum context` / `prompt is too long` / `too many tokens` / `exceeds the maximum` | 否 | 缩小输入：related_work、previous proof、human help 三选一裁剪；或把该步拆小 |
| 2 | `token_output` 输出被截断 | 同上，命中 `max_tokens` / `max output` / `output limit` / `incomplete` / `length` 且 claude 的 `subtype` 以 `error_max` 开头 | 否 | 提示词要求分段写入（`f8cba94` 已做）；或调低单步体量 |
| 3 | `quota` 额度 / 限流 | `insufficient_quota` / `quota` / `credit` / `rate limit` / `429` / `resource_exhausted` / `overloaded` | 短期否 | 充值或换模型；**不要**用同一个 key 重试 |
| 4 | `session_died` 会话静默死亡 | 退出码 ≠ 0 **且**无 `turn.completed`（codex）/ 无 `is_error` 字段（claude）/ 无 `stats`（gemini），且文本未命中 1–3 | 1 次 | 观察到的 (a) 类。**必须**把 stderr 尾部原样带进报告，否则无从定因 |
| 5 | `timeout` 我们主动超时 | G3 的空闲/总时长看门狗触发，或 G1 闸门拦截 | 否 | 看是哪一条：空闲→CLI 挂死；总时长→单次任务过大 |
| 6 | `unknown` | 以上都不命中 | 1 次 | 报告里带全部原始尾部，交给人判断 |

**诚实说明**：第 1–3 类的字面量 pattern **没有**在本仓库的真实日志中被验证过（真实日志里只有退出码 1 和空 stderr）。因此：

- 分类器必须是**附加**判断，不能替代第 4 类；
- 每次 halt 的报告都**无条件**附上原始 stdout/stderr 尾部（≤50 行）和退出码，即使分类器判错了，人也能一眼看出；
- 新增一个 `HALT.md` 里的「未能归类」小节，把没命中的尾部单独存档，方便以后补 pattern。

### 不做的事

- **不**用「耗时」作为判定依据。健康的 prover 单次 95 分钟，挂死的也是 93 分钟——**时长完全无法区分两者**。这一点是设计里最容易踩的坑。
- **不**把 (b) 类（正常退出 + 旁白）算作 token 耗尽。它走 `ProofNotProduced`。

---

## 五、暂停语义

### 暂停发生在哪

**只在调用与调用之间。** 无法暂停在某个 agent 的内部步骤——那是 CLI 子进程自己的循环，我们只能杀或等。这一点必须在文档和 UI 文案里说清，否则用户会以为能「停在 HALF 页」继续。

### 具体路径

1. `run_model_for_agent` 抛 `TokenExhausted(ModelRunnerError)`（新增子类，`error_type` 见分类表）。
2. `run_single_prover` **不**捕获它——它不是 `ProofNotProduced`，现有的 `except ProofIncomplete / ProofNotProduced` 分支不会误吞，**因此不会消耗 proof 尝试次数、不会咨询 regulator**。
3. `run_decomposition_prover` 顶层捕获一次，做完三件事：
   - 尽力拼装已完成的步骤（见下节，**关键**）；
   - `decomp_logger.update_status(state="HALTED", recent_activity=<一句话原因>)`；
   - 写 `HALT.md` / `halt.json`，然后 `raise RunHalted(...) from exc`。
4. `pipeline.main` 捕获 `RunHalted`：打印横幅 → **跳过 Stage 2** → `sys.exit(3)`。
5. `run.py` 透传 3 → `process_manager` 看到退出码 3 → UI 显示「已暂停（原因）」+「继续运行」按钮（续跑逻辑已经存在，见下）。

### 退出码约定

| 码 | 含义 |
|---|---|
| 0 | 完成 |
| 1 | 常规失败 / 前置检查未过 |
| **3** | **因调用取不到结果而暂停（可续跑）** |

UI 用退出码就能区分「挂了」和「暂停了」，不必解析文本。

### 为什么不消耗尝试次数

`max_proof_attempts: 8` 是**数学**预算，不是**基础设施**预算。一次网络/额度故障不该吃掉八分之一的机会。暂停后 `detect_resume()` 会按磁盘状态回到 `prove`，`proof` 计数不变。

---

## 六、与 P1 续写的衔接（方案里最容易漏的一环）

P1 的续写靠 `interrupted_partial()` 判定：**有 `proof_partial.md` 且没有 `proof.md`** 才算中断。

但现在的 `proof_partial.md` 只在 `run_single_prover` **正常返回之后**、发现拼装不完整时才写。如果 `TokenExhausted` 从 `run_codex_agent` 直接抛出，代码根本走不到那里 —— **检查点在盘上，但没有任何东西把它标成「可续写」**，P1 就是死代码，回到 P0 之前的处境。

因此 halt 路径必须补一步：

```python
# 在 run_decomposition_prover 捕获 TokenExhausted 时
assembled = assemble_proof_from_steps(proof_dir, problem_file, decomposition)
if assembled:
    text, present, missing = assembled
    write_file(os.path.join(proof_dir, "proof_partial.md"), text)
```

还有第二个缺口，halt 让它变成常规路径：**同目录续跑**。`detect_resume` 在「有检查点、无 `proof.md`」时返回 `resume_point="prove"` 且 `proof` **不递增**，即重跑同一个 `proof_N`。而 `run_single_prover` 只在 `state.proof > 1` 时准备续写参数，同目录时 `carried_steps` / `remaining_steps` 都是 `none`，提示词会告诉模型「这不是续写」——但目录里明明有 8 个步骤文件。

修法：把「当前目录已有检查点」也当作续写输入。新增

```python
def existing_checkpoints(proof_dir, decomposition) -> tuple[list[str], list[str]]
```

`run_single_prover` 里改成：续写来源 = 上一个中断尝试的结转 **∪** 本目录已有文件；两种情况都渲染 `carried_steps` / `remaining_steps`，只是「上一份 partial 的路径」这一栏可能为空。

---

## 七、报告产物

### `HALT.md`（输出根目录，人读）

```markdown
# 运行已暂停：第 N 次调用取不到结果

**时间**：2026-11-02 09:14:22
**原因分类**：token_context（上下文超限）
**调用**：single_prover_a1_r4_p2 · provider=codex · model=gpt-6-astra
**位置**：attempt 1 / revision 4 / proof 2

## 依据
- 退出码：0
- 用量事件：无 turn.completed
- 命中文本：`Error: context_length_exceeded (requested 412k > 400k)`
- 原始尾部：见下

## 原始输出（最后 50 行）
```

## 影响
- 未消耗 proof 尝试次数（proof 计数仍为 2）
- 已完成步骤：STEP1–STEP6（已拼装到 `proof_partial.md`）
- 未完成：STEP7a, STEP7b, GOAL
- 未运行：structural verification、detailed verification、regulator、Effort Summary

## 下一步
1. 续跑（保留已完成的 6 步）：`python code/run.py problem/problem.tex <output_dir>`
2. 若为上下文超限：裁剪 related_info/related_work.md，或把该步拆成两步后重跑
3. 若为额度：充值后重跑；必要时在 config.yaml 把 single_prover 换到别的 provider

## 未能归类（供补 pattern）
<原始尾部中未被任何规则命中时的片段>
```

### `halt.json`（同目录，机器读）

```json
{"halted_at": "...", "category": "token_context", "call_name": "...",
 "provider": "codex", "model": "...", "exit_code": 0, "elapsed_s": 5610.8,
 "attempt": 1, "revision": 4, "proof": 2, "resume_point": "prove",
 "checkpoints_present": ["STEP1", "..."], "checkpoints_missing": ["STEP7a", "STEP7b", "GOAL"],
 "attempts_consumed": false}
```

UI 只读 json，不 grep Markdown。

### `STATUS.md`

`State: HALTED` + `Recent Activity` 一句话。UI 已经把 `decomposition/STATUS.md` 原样渲染在展开面板里，这条改动零 UI 成本就能看见。

### 顺带修：`update_status` 会重置计数器

`DecompositionLogger.update_status()` 重新读 `STATUS.md` 时把 `attempt/revision/proof` 初始化为 1，任何只传 `state=` 的调用都会把这三个数写回 1。halt 恰恰是「只传 state + activity」的典型场景，会让 UI 在暂停瞬间显示 `attempt 1 / revision 1 / proof 1`。修法：日志器自己持有内存状态，只覆盖传入的字段。

---

## 八、配置（新增 `runner:` 段，全部有默认值）

```yaml
runner:
  # G1 前置预算闸门
  max_prompt_tokens: 400000      # 单次 prompt 估算上限（0 = 关闭）
  max_total_tokens: 0            # 整轮预算（0 = 关闭）；真实一次运行 3.5M in
  # G3 空闲看门狗（P2 才生效）
  idle_timeout_s: 0              # 0 = 只记录最长空闲，不杀
  hard_timeout_s: 0              # 0 = 不限制；真实健康调用 95 min，勿轻设
  # 连续失败
  max_consecutive_failures: 3    # 同类 unknown 连续失败次数，超过即 halt
  halt_exit_code: 3
```

默认值刻意偏保守：**`idle_timeout_s` 与 `hard_timeout_s` 默认 0**。理由见下节——两者都依赖流式改造，而「健康会话是否会持续输出事件」这一点在本仓库**未经真实日志验证**（现有日志只有最终汇总，看不到逐事件流）。

---

## 九、分期

### P0：分类 + 上报 + 暂停（不需要流式，风险低，当天可落地）

| 文件 | 改动 |
|---|---|
| `model_runner.py` | `TokenExhausted(ModelRunnerError)`；`classify_failure(stdout, stderr, exit_code, events)`；三个 runner 在 raise 前分类；token 类**不进重试循环**；每次调用记录 `outcome` |
| `decomposition_prover.py` | `RunHalted`；顶层捕获 + 拼装 `proof_partial.md` + `STATUS.md=HALTED`；`existing_checkpoints()`（第六节）；修 `update_status` |
| `pipeline.py` | `try/except RunHalted`，跳过 Stage 2，`sys.exit(3)` |
| `ui/` | 退出码 3 → 「已暂停」+ 原因（读 `halt.json`） |

**校验方式**：拿本次真实运行里 4 次崩溃的 `(exit_code=1, stderr='Reading additional input from stdin...')` 喂进分类器，必须判成 `session_died`（而不是 token），且报告里带原始尾部。

### P1：前置预算闸门（省钱，纯加法）

- 调用前估算 `len(prompt)` 与累计用量，超限直接 halt 并说明「没花这一次钱」。
- `max_consecutive_failures`。
- 这一步能在**不碰 subprocess 层**的前提下，把「每次都跑满 90 分钟才发现不对」变成「第二次就不再开始」。

### P2：流式 + 空闲看门狗（唯一能解决 93 分钟挂起的部分，风险最高）

- 重写 `compat.run_cli_async`：改为读线程 + `proc.communicate(timeout=...)` 或逐行读 stdout，暴露 `idle_timeout_s`；顺手修掉死参数 `timeout`（现在传进去会 `TypeError`）。
- **先只观测不杀**：`idle_timeout_s: 0` 时仅记录每次调用的最大空闲时长，攒一两个真实运行的数据后再打开阈值。
- 只有在观测到「健康调用的事件间隔 P99 << 挂死调用的静默时长」之后，才允许把 `idle_timeout_s` 设成有限值。

---

## 十、校验计划

| # | 用例 | 期望 |
|---|---|---|
| 1 | 真实 4 次崩溃的 `(exit 1, 空 stderr)` | `session_died`，报告含原始尾部 |
| 2 | 合成 codex `{"type":"error","message":"context length exceeded"}` | `token_context`，**不重试** |
| 3 | 合成 claude `{"is_error":true,"subtype":"error_max_turns"}` | `token_output` |
| 4 | 合成 gemini `{"error":{"code":429}}` | `quota` |
| 5 | 正常退出 + 旁白（真实 `proof_2/prover_response.md`） | 仍是 `ProofNotProduced`，**不是** halt |
| 6 | 离线伪造 `TokenExhausted` | `HALT.md`/`halt.json` 落盘、`STATUS.md=HALTED`、退出码 3、Stage 2 未执行 |
| 7 | halt 后 `detect_resume()` | `resume_point="prove"`，`proof` 计数**未增** |
| 8 | halt 在 prover 中途 | `proof_partial.md` 存在，下一次尝试 `carried_steps` 非空（P1 续写真的被触发） |
| 9 | `update_status(state=...)` 后读回 | attempt/revision/proof 保持原值 |

全部可在离线 smoke_test 里跑（`shutil.which` 打桩），不需要真实调用。

---

## 十一、顺带发现的缺陷（不在本方案范围，但必须记录）

| # | 位置 | 问题 |
|---|---|---|
| 1 | `compat.run_cli_async` | `timeout` 参数被传给 `Popen`，而 `Popen` 不认 → 一用就 `TypeError`。今天没有调用方传它，所以没人发现 |
| 2 | `model_runner.run_gemini_agent` | 引用了从未定义的 `thinking_config`（`model_runner.py:489/473`）→ **每次 Gemini 调用必然 `NameError`**。Gemini 通路目前是死的 |
| 3 | `DecompositionLogger.update_status` | 只传 `state=` 时把 attempt/revision/proof 重置为 1 |
| 4 | `token_usage.json` | 不记 `outcome`，0/0 的条目无法区分「崩溃」与「返回旁白」 |
| 5 | `pipeline.main` | 无顶层异常处理；Stage 2 在崩溃时必然丢失 |
| 6 | `claude` runner | 对不可恢复错误重试 3 次 × (30+60+120)s 退避，白烧时间和请求 |

---

## 十二、待定的策略问题

1. **暂停后要自动续跑吗？** 建议**不要**：token/额度类失败不会自己好，自动重试只是重复付费。人确认后再按「继续运行」。
2. **`max_total_tokens` 默认开还是关？** 真实一次运行 3.5M in。若开，默认给多少（5M / 50M / 关闭）？
3. **「及时反馈」的边界**：`HALT.md` + `STATUS.md` + UI 横幅够，还是要桌面通知 / webhook？
4. **P0 是否连 `existing_checkpoints` 一起做？** 它属于 P1 续写的延伸，但 halt 会让「同目录续跑」变成常规路径，不做的话暂停后那 8 个步骤文件在提示词层面等于不存在。
