# 评审与改造记录：把 verdict 从模型判定改为代码判定

**状态**：已实施（分支 `refactor/deterministic-verdict`）
**涉及模块**：`code/decomposition_prover.py`、`code/smoke_test.py`、`prompts/decomposition-prover/`
**日期**：2026-11-01

---

## 摘要

流水线中的 `verdict` agent 原本由 LLM 读取验证报告、判断证明是否通过，并向主循环返回 `DONE` / `CONTINUE`。评审结论是：**该任务的信息量等价于一次字段比较，不需要生成式模型**。本次改造将其替换为确定性函数 `parse_report_verdict` / `compute_verdict`，同时消除了原有实现中的三处隐患：

1. 下游解析用的是子串包含检查 `"DONE" in response`，存在把失败报告误判为通过的具体路径；
2. 主循环（LLM 判定）与续跑检测（代码判定）对同一份报告并行两套逻辑，结论可能不一致；
3. `verdict` 是整条流水线中唯一决定「是否接受证明」却**不落盘任何证据**的环节。

改造后每次判断零模型调用、零延迟，且判定依据写入日志。

---

## 一、被替代的对象

### 1.1 调用链

```
proof.md
  └─→ proof_verify_structural.md ─→ structural_verification.md
        └─→ verdict_proof.md (STRUCTURAL) ─┬─ DONE ──→ proof_verify_detailed.md
                                            └─ CONTINUE ─→ regulator（改证明/改计划/推翻重来）
  └─→ 两份报告齐备 ─→ verdict_proof.md (FINAL) ─┬─ DONE ──→ 接受，输出 proof.md
                                                └─ CONTINUE ─→ regulator
```

`verdict` 同时承担两个职责：

| 模式 | 职责 | 失败代价 |
|---|---|---|
| STRUCTURAL | **成本闸门**：判断是否值得花大代价进入 Phase 6 逐步校验 | 多跑一次昂贵验证 |
| FINAL | **终审**：判断是否接受这份证明并结束流水线 | 错误发布一份假证明 |

代码中的调用点（改造前）：`decomposition_prover.py` L1825（结构闸门）、L1895（终审），以及 resume 路径 L1602 / L1639。

### 1.2 旧实现的完整行为

```python
role_cfg = get_agent_role_cfg(config, "verdict")          # provider: codex
prompt = load_prompt(..., "decomposition-prover/verdict_proof.md", mode=..., ...)
response = await run_model_for_agent(...)                 # 一次完整 CLI 子进程往返
response_upper = response.strip().upper()
if "DONE" in response_upper:                              # 子串包含，非相等
    verdict = "DONE"
else:
    verdict = "CONTINUE"
```

---

## 二、为什么可以代码化

### 2.1 信息流存在断点

报告文件 → LLM 阅读 → 生成一个词 → Python 字符串比较。

中间环节不改变信息内容：报告里的 `Overall Verdict` 是格式化的二值字段，判断规则是恒等映射（`PASS → DONE`）。LLM 在此只是转述磁盘上的字符串，却引入了生成式的不确定性。

### 2.2 下游解析本身就会误判

`"DONE" in response_upper` 是包含检查。具体失效场景：

| LLM 输出 | 判定结果 | 正确性 |
|---|---|---|
| `DONE` | DONE | ✅ |
| `CONTINUE` | CONTINUE | ✅ |
| `I cannot reply DONE because Phase 3 failed.` | **DONE** | ❌ 错误接受 |
| `NOT DONE` | **DONE** | ❌ 错误接受 |

注意：提示词第 65 行明确要求「必须恰好一个词」，但下游用 `in` 而非 `==`，**上游的格式约束被下游完全浪费**。若下游写成 `== "DONE"`，任何格式违规都会安全落回 CONTINUE。

### 2.3 双轨判定（最严重的结构问题）

改造前，同一文件内存在两条对**同一份报告**独立判定的路径：

| 路径 | 实现 | 位置 |
|---|---|---|
| 主循环 | LLM 判定 → 子串匹配 | `run_verdict` |
| 续跑检测 | `"OVERALL VERDICT: PASS" in content.upper()` | L719、L735 |

两者无共享逻辑。极端情况下，同一份报告中中断后 resume 与正常重跑可能得出相反结论。

### 2.4 项目内已有代码先例

`verify/verify.py:364` 的 `parse_structural_verdict()` 早已是纯正则实现，并带保守降级（解析失败 → `FAIL`）。同一个仓库里已有正确做法，只是 `decomposition_prover.py` 没有复用。

### 2.5 成本

`config.yaml` 当前上限为 `max_proof_attempts=8`、`max_revisions=4`、`max_decompositions=4`，理论最坏 `4 × 4 × 8 = 128` 轮 proof，每轮 2 次 verdict → **最坏 256 次无谓的模型调用**。每次均为串行阻塞的主循环中的一次完整 CLI 往返（输入含 86 行模板 + agent 自行读取的报告约 150–250 行），通常耗时十秒至一分钟。

---

## 三、`verdict_proof.md` 原文逐段解读

原文 86 行，以下按结构解读。

### 3.1 标题与任务分类（L1–3）

```
# Verdict Task: Decomposition Proof Verification
> **Decision task.** Read the verification result file(s), then make a verdict.
```

`Decision task` 是刻意分类：对比 `decomposition.md` / `single_prover.md` / `proof_verify_*.md` 均标为 **Agentic task**（需读文件、跑工具、写产物），本文件与 `regulator.md` 标为 **Decision task**（只读、只判断）。

这个区分在语义上正确，但**未落到实现约束**：`run_verdict` 仍走完整的 `run_model_for_agent`（具备 shell 与写文件能力），分类只是文案。

### 3.2 模式声明（L5–10）

两个模式功能迥异却共用模板与同一 provider（`codex`）：

| 模式 | 角色 | 理想成本档位 |
|---|---|---|
| STRUCTURAL | 预筛 | 可用轻量模型或纯代码 |
| FINAL | 终审 | 应用最强模型 |

当前是「一刀切」，没有成本分级——预筛用了与终审同级的资源。

**命名陷阱**：`regulator.md` 的 `{mode}` 也取 `FINAL`，但含义是「重试预算耗尽，写失败分析」，即**失败路径的收尾**；本文件的 `FINAL` 是**成功路径的终审**。同词反义，易致误改。

### 3.3 STRUCTURAL 分支（L14–30）

```
Reply with ONLY the single word 'DONE' if:
1. Overall Verdict in structural verification is PASS
2. All five phases passed (Problem Integrity, Completeness, Citations,
   Decomposition Adherence, Additional Rules)
```

两个条件在正常情况下冗余（`Overall Verdict` 本就是五阶段的聚合），设置双条件的唯一理由是防御**报告自身不一致**（如 Phase 3 判 FAIL 但 Summary 误写 PASS）。设计意图合理，但它要求 LLM 做它并不擅长的跨段落一致性校验。

同时存在**隐式耦合**：条件 2 假定 `proof_verify_structural.md` 必然输出 Summary 表。verdict 的正确性因此依赖另一个提示词的输出格式，而两处均未声明该依赖——若后者改格式，前者会静默失效（不报错，只是再也匹配不到「五阶段」）。

### 3.4 FINAL 分支（L34–57）

```
1. Structural Verification PASSED: Overall Verdict = PASS
2. Detailed Verification PASSED: Overall Verdict = PASS
3. No Unresolved Issues: Neither report flags critical issues that remain unaddressed
```

条件 1、2 机械可判。**条件 3 是全文唯一的主观条款**，且 `critical issues` 无操作定义。

实践中该条件几乎不可达地恒真：`proof_verify_structural.md` 的 Phase 2a 明文规定「证明自己承认漏洞 = FAIL」，因此任何含未解决问题的报告本就已是 FAIL，走不到 FINAL 判 DONE。该条只增加了模型的解读自由度，未增加实际约束。

### 3.5 Important Rules（L61–67）

五条规则的排布顺序值得肯定：**硬否决条件 → 输出格式 → 默认倾向**。

但第 2 条暴露结构缺陷：

> If any verification report is **empty or missing**, reply CONTINUE

这要求 agent **先读文件**才知是否为空，而 LLM 最典型的失效模式恰恰是跳过阅读直接作答。**用 LLM 检查文件存在性，是一个自指盲区：检查者自身的可靠性无法被检查。**

### 3.6 Decision Logic（L71–86）

伪代码形式，但**与上方文字规则不等价**，见第四节。

---

## 四、原文的四处规格缺陷

### 缺陷 1：文件内部自相矛盾（最关键）

| 规则位置 | STRUCTURAL 的 DONE 条件 | FINAL 的 DONE 条件 |
|---|---|---|
| 文字 Criteria（L20–22 / L40–43） | Overall PASS **且** 五阶段全过 | 两份 PASS **且** 无未解决问题 |
| 伪代码（L74–86） | 仅 Overall PASS | 仅两份 PASS |

伪代码**削弱**了文字规则，且位于文末（近因效应）。若模型优先遵循伪代码，则「五阶段校验」与「无未解决问题校验」实际从未执行，闸门严格性低于设计意图。

同一份规格用两种形式描述同一规则且不等价，是提示词工程中的典型故障。

### 缺陷 2：可审计性断层

| Agent | 是否落盘 | 可事后复盘 |
|---|---|---|
| `regulator` | 写 `regulator_decision.md`（含分析、决策、理由） | ✅ |
| `verdict` | **不写文件**，仅 stdout 一个词 | ❌ |

整条流水线中，唯一决定「是否接受证明」的判断，恰恰是唯一不留证据的判断。日志仅有 `verdict: DONE` 一行，无法追查其依据了报告的哪一段。

### 缺陷 3：输出约束被下游浪费

见 2.2。上游约束「恰好一个词」，下游用 `in` 匹配，两者互相抵消防护。

### 缺陷 4：模式与成本档位不匹配

STRUCTURAL 的错误代价是「多跑一次 Phase 6」，FINAL 的错误代价是「错误发布」，风险差一个数量级，却共用同一 provider 配置。

---

## 五、替代设计

### 5.1 报告格式契约

来自真实产物 `ui/proof_runs/run_20261030_214906/.../structural_verification.md`：

```markdown
## Summary

| Check | Status |
|-------|--------|
| Phase 1: Problem-Statement Integrity | PASS |
...
### Overall Verdict: PASS
```

`proof_verify_detailed.md` 的模板同样输出 `### Overall Verdict: [PASS/FAIL]`。字段名与位置稳定，可安全正则化。

### 5.2 判定规则

| 情形 | 判定 |
|---|---|
| 报告含 `Overall Verdict: PASS` | `PASS` |
| 报告含 `Overall Verdict: FAIL` | `FAIL` |
| 文件不存在 / 为空 / 路径为空 | `MISSING` |
| 找不到判定字段 | `UNPARSEABLE` |
| 同文件出现多个判定字段 | 取**最后一处**（文末 Summary 为权威；前文可能是引用） |

`compute_verdict(mode, structural, detailed)`：
- STRUCTURAL：`structural == PASS` → `DONE`，否则 `CONTINUE`
- FINAL：`structural == PASS` **且** `detailed == PASS` → `DONE`，否则 `CONTINUE`

任何 `MISSING` / `UNPARSEABLE` 都落入 `CONTINUE`，与旧提示词「empty or missing → CONTINUE」「when in doubt → CONTINUE」一致。

### 5.3 三个必须防御的边界陷阱

**陷阱 1：未填充的模板占位符。**
detail 报告模板字段写作 `### Overall Verdict: [PASS/FAIL]`。若 agent 原样输出，朴素正则会从 `[PASS/FAIL]` 中匹配到 `PASS` → **误判通过**。
防御：正则加负向断言 `(?!\s*/\s*(?:PASS|FAIL)\s*\])`；同时 `[` 本身也会阻断 `(PASS|FAIL)` 的匹配，形成双重保险。

**陷阱 2：正文引用历史结论。**
报告可能出现「The structural report said PASS」这类引用，或详细报告复述结构报告的结论。
防御：只匹配 `Overall (Structural )?Verdict:` 前缀，并取**最后一处**——文末 Summary 是权威字段，前文命中多为引用。

**陷阱 3：`Overall Structural Verdict` 变体。**
`verify/verify.py` 使用该写法，主报告用 `Overall Verdict:`。正则需同时兼容：`overall\s+(?:structural\s+)?verdict`。

---

## 六、已执行的改动（第 1–12 项）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `code/decomposition_prover.py` | 新增 `import re` |
| 2 | 同上 | 删除死函数 `parse_verdict()`（原 L173–192，文档字符串自述 unused） |
| 3 | 同上 | 新增 `_VERDICT_RE`、`parse_report_verdict()`、`compute_verdict()` |
| 4 | 同上 | `run_verdict()` 改为薄包装：**签名完全不变**，内部调用 `compute_verdict`；保留 `VERDICT_STRUCTURAL` / `VERDICT_FINAL` 状态更新以兼容 STATUS.md 解析；新增证据日志 |
| 5 | 同上 | `DEFAULT_MODELS` 移除 `"verdict"` 角色，加注释说明判定已代码化 |
| 6 | 同上 | 续跑检测 L719 / L735 改用 `parse_report_verdict(...) == "PASS"`，消除双轨判定 |
| 7 | 同上 | 删除死函数 `run_proof_verification()`（原 L1397–1479，自述 unused） |
| 8 | `prompts/decomposition-prover/verdict_proof.md` | `git mv` 至 `archive/verdict_proof.md`（保留历史，与 archive 既有用途一致） |
| 9 | `code/smoke_test.py` | 从 `decomposition_prompt_files` 列表移除该文件 |
| 10 | 同上 | 删除其渲染测试块 |
| 11 | 同上 | `required_agents` 移除 `"verdict"` |
| 12 | 同上 | 新增 **Test 3c**：`parse_report_verdict` / `compute_verdict` 的 17 项断言（PASS / FAIL / 加粗 / Structural 变体 / last-wins 两个方向 / 未填充占位符 / 无字段 / 空文件 / 空路径 / 缺失文件 / STRUCTURAL 与 FINAL 各分支） |

### 关键实现

```python
_VERDICT_RE = re.compile(
    r"overall\s+(?:structural\s+)?verdict\s*:?\s*\**\s*(PASS|FAIL)"
    r"(?!\s*/\s*(?:PASS|FAIL)\s*\])",   # never accept an un-filled [PASS/FAIL] slot
    re.IGNORECASE,
)


def parse_report_verdict(path: str) -> str:
    if not path:
        return "MISSING"
    text = read_file(path)          # compat.read_text returns "" when absent
    if not text or not text.strip():
        return "MISSING"
    matches = _VERDICT_RE.findall(text)
    if not matches:
        return "UNPARSEABLE"
    return matches[-1].upper()      # trailing Summary is authoritative


def compute_verdict(mode: str, structural_report: str,
                    detailed_report: str = "") -> str:
    if parse_report_verdict(structural_report) != "PASS":
        return "CONTINUE"
    if mode.upper() == "FINAL" and parse_report_verdict(detailed_report) != "PASS":
        return "CONTINUE"
    return "DONE"
```

`run_verdict` 保持 `async` 且签名不变，因此 4 个活跃调用点（L1602 / L1639 / L1825 / L1895）**零改动**。未被消费的参数（`prompts_dir` / `config` / `claude_opts` / `tracker`）保留并加注释说明。

---

## 七、未执行的改动（第 13–15 项）

| # | 文件 | 内容 | 未执行原因 |
|---|---|---|---|
| 13 | `ui/utils.py` | `AGENT_NAMES` 移除 `"verdict"` | `ui/config_panel.py:294` 用 `AGENT_NAMES` 构造 `decomp_agents`，而 L311 是**整体替换** `models` 字典（不是合并）。一旦移除，用户下次在 UI 点保存会**静默删除** `config.yaml` 里的 `models.verdict`。留待确认不再回滚后再动 |
| 14 | `config.yaml` | 移除 `verdict:` 角色块 | 同上；且 `get_agent_role_cfg` 对多余配置完全容错（不再被调用），留着无害 |
| 15 | `verify/verify.py` | 复用 `parse_report_verdict` | 独立验证器（Standalone Verifier）是另一条链路，其报告格式与 decomposition 链路不同，属独立重构，不宜混入本次改动 |

---

## 八、验证记录

### 8.1 语法

`python -m py_compile code/decomposition_prover.py code/smoke_test.py` → 通过。

### 8.2 判定函数的行为矩阵

17 项全部通过：

| 用例 | 期望 | 实际 |
|---|---|---|
| `### Overall Verdict: PASS` | PASS | PASS |
| `### Overall Verdict: FAIL` | FAIL | FAIL |
| `### **Overall Verdict:** PASS` | PASS | PASS |
| `### Overall Structural Verdict: PASS` | PASS | PASS |
| 前文 FAIL、文末 PASS（引用场景） | PASS | PASS |
| 前文 PASS、文末 FAIL | FAIL | FAIL |
| `### Overall Verdict: [PASS/FAIL]`（未填充） | UNPARSEABLE | UNPARSEABLE |
| 无判定字段 | UNPARSEABLE | UNPARSEABLE |
| 空文件 | MISSING | MISSING |
| 空路径 | MISSING | MISSING |
| 文件不存在 | MISSING | MISSING |
| `compute_verdict(STRUCTURAL, PASS)` | DONE | DONE |
| `compute_verdict(STRUCTURAL, FAIL)` | CONTINUE | CONTINUE |
| `compute_verdict(STRUCTURAL, 缺失)` | CONTINUE | CONTINUE |
| `compute_verdict(FINAL, PASS, FAIL)` | CONTINUE | CONTINUE |
| `compute_verdict(FINAL, PASS, PASS)` | DONE | DONE |
| `compute_verdict(FINAL, PASS, "")` | CONTINUE | CONTINUE |

### 8.3 真实数据回归

`ui/proof_runs/run_20261030_214906/decomposition/attempt_1/revision_1/proof_1/structural_verification.md` → `parse_report_verdict` 返回 `PASS`，与人工阅读一致。

> 注：仓库中现存的验证报告仅此一份，且为 PASS。FAIL 样例、占位符未填充样例均需在 Test 3c 中构造（已实现）。

---

## 九、行为差异与回滚

### 9.1 预期内的行为变化

| 项 | 变化 |
|---|---|
| `TOKEN_USAGE.md` | 不再出现 `verdict_structural` / `verdict_final` 两条记录（口径变更，历史基线对比需注意） |
| `decomposition/log.txt` | 不再有 `log_agent_call` 记录，改为一行 `Verdict (MODE): reading report files directly` + 一行证据日志 |
| 判定严格度 | 只认字段。若出现「正文全 FAIL 但字段误写 PASS」的极端报告，代码版会比 LLM 更宽松 |
| 状态机 | `VERDICT_STRUCTURAL` / `VERDICT_FINAL` 状态仍会写入，UI 与 STATUS.md 解析不受影响 |

关于第 3 点：若希望保留「读正文纠错」能力，正确做法**不是**留着 LLM，而是在 `compute_verdict` 内加交叉校验（如 STRUCTURAL 额外要求 Summary 表五阶段全为 PASS）。这属于**行为收紧**，可能拒绝此前能通过的边界报告，建议先保持行为等价，观察一段时间再决定。

### 9.2 回滚

```bash
git revert <commit>          # 或
git checkout main -- code/decomposition_prover.py code/smoke_test.py \
                       prompts/decomposition-prover/verdict_proof.md
```

因 `run_verdict` 签名与返回值完全未变，回滚只需恢复实现，调用点无需改动。

---

## 十、结论

原实现并非「写得不好」——在一个注定脆弱的架构里，`verdict_proof.md` 的防御性写法（反复重申、伪代码兜底、默认 CONTINUE）是称职的。问题在架构选择本身：**用一个概率性组件去执行一个确定性判断**。

改造后，该职责的正确表述是一句话：

> 读取两份报告中的 `Overall Verdict` 字段，若均为 `PASS` 则返回 `DONE`，否则返回 `CONTINUE`；任何异常一律返回 `CONTINUE`。

这一句话现在由 12 行代码承担，替代了 86 行提示词、每次一条模型往返，以及一条可能错误接受失败证明的路径。
