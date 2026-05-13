# EGTC-PAW Runtime v5：Director / Overlooker + TPGO 自改进层工程蓝图

> **EGTC-PAW** = Experience-Guided, Task-Conditioned, Permission-Aware Workflow Runtime。  
> 本版统一术语：**中控 Agent = Director**，**监督者/督导 Agent = Overlooker**。  
> v5 的重点是把 **Learning to Evolve / TPGO** 里的 Textual Parameter Graph、Textual Gradient、GRAO-style Optimization Memory、Targeted Validation / Rollback 引入到已有的权限化、节点化、证据化 workflow runtime 中。

---

## 0. 本版范围

本文件描述一个面向 Codex / coding agent / 多 agent workflow 的可运行系统蓝图。它保留 v4 中的核心工程约束：

- 经验检索只能作为 prior，不能直接决定 agent 数量、沙箱权限、Overlooker 验收标准。
- Director 负责规划、重规划与权限设计，但不能绕过 Compiler、PolicyChecker、SandboxRuntime、Overlooker。
- Codex worker 只负责在 sandbox 内执行节点任务，不能自证节点完成。
- Overlooker 负责基于 EvidenceBundle 验收节点，节点通过后释放。
- Artifact、Evidence、GraphPatch、OverlookerReport 都必须有身份、权限、lineage 与可追溯性。

本文件仍然**不展开评测、发布、运维闭环**，只关注从研究方案到可运行系统所需的核心技术设计。

---

## 1. v5 相比 v4 的新增重点

当前 Stage A-E 已经补齐了单节点信任链、ArtifactStore、PermissionGrounding、Sandbox backend、Codex exec wrapper、EvidenceBundle、Overlooker gate、并发 Runtime、checkpoint/retry、Overlooker-guided retry fork、Stage D 内可执行的 `retry_node` GraphPatch 闭环，以及 Phase E 的高风险节点冲突仲裁、second Overlooker gate、permission / human review placeholder。完整 Artifact lineage、Secret 隔离和 TPGO 自改进层仍属于后续 Stage。v5 在这个安全执行底座上新增一个 **自改进优化层**：

| 新增技术 | 作用 | 在本系统中的落点 |
|---|---|---|
| Textual Parameter Graph, TPG | 把 Director prompt、Overlooker prompt、worker instruction、tool description、policy text 拆成可编辑图节点 | `TPGBuilder` / `TextualParameterGraph` |
| Textual Gradient | 从执行轨迹、EvidenceBundle、OverlookerReport、ValidatorReport 中生成正负自然语言优化信号 | `GradientReflector` |
| Gradient Clustering | 聚类重复失败模式，避免为单个 case 过拟合 | `GradientClusterer` |
| Machine-readable TPG Edit | 把优化建议变成可编译、可审计、可回滚的编辑操作 | `TPGEdit` / `TPGPatch` |
| GRAO-style Optimization Memory | 存储 problem-context / patch / outcome，让优化器学习过去哪些改法有效 | `OptimizationExperienceMemory` |
| Targeted Validation | 只在受影响节点子图和相关历史 case 上验证改动 | `PatchValidationPlan` |
| Rollback + failed update retention | 优化失败时回滚，但保留失败 proposal 作为负样本 | `RollbackRecord` / `NegativeOptimizationCase` |

一句话总结 v5：

> v4 让 workflow **安全可执行**；v5 让 workflow 在安全边界内 **有结构地自我优化**。

---

## 2. 总体设计原则

### 2.1 经验是 prior，不是配置答案

检索到的历史经验只允许提供：

- 编排骨架；
- 权限教训；
- 失败模式；
- Overlooker 检查维度；
- 过去 GraphPatch / TPGEdit 的效果记录。

它不能直接决定：

- 当前任务使用几个 agent；
- 每个节点的 Codex sandbox 权限；
- `writable_paths`；
- 是否开启 network；
- Overlooker 的具体验收标准；
- 是否接受某个节点完成。

这些必须由 Director 基于当前任务、repo policy、runtime state 和 evidence 重新推导。

### 2.2 Director 规划，Runtime 执行，Overlooker 放行

```text
Director
  负责：任务诊断、workflow 设计、agent 数量设计、sandbox 权限设计、GraphPatch、冲突决策。

Codex Worker
  负责：在 sandbox 内完成节点任务，提交 artifact 和结构化结果。

Overlooker
  负责：基于 EvidenceBundle 判断节点是否达到目标，返回 OverlookerReport。

Workflow Runtime
  负责：状态机、调度、并发、事件、checkpoint、GraphPatch 应用、节点推进。

Compiler / PolicyChecker
  负责：检查 Director 的计划是否满足最小权限、反照搬、路径/命令约束、Overlooker 覆盖、冲突规则。
```

### 2.3 Codex worker 只提交，不自证完成

```text
turn.completed
  ≠ NODE_ACCEPTED

WorkerSubmitted
  ≠ NodeAccepted
```

正确链条是：

```text
Codex turn.completed
  → WorkerSubmitted
  → EvidenceCollector.collect()
  → DeterministicValidators.run()
  → Overlooker.review()
  → Director / Runtime decision
  → NodeAccepted / Retry / Replan / Blocked
```

### 2.4 优化不能越过安全边界

TPGO / GRAO-style 自改进层只能提出：

- 修改文本参数图；
- 修改 Director / Overlooker / Worker 指令；
- 修改节点验收 checklist；
- 插入 validator；
- 调整 GraphPatch 策略；
- 收紧或重新 grounding 权限。

它不能直接：

- 放大 sandbox 权限；
- 跳过 Overlooker；
- 绕过 deterministic validator；
- 伪造 EvidenceBundle；
- 把 secret 写入 prompt、event log 或 artifact；
- 未经 Compiler / PolicyChecker 就应用 workflow 结构修改。

---

## 3. 系统总览

### 3.1 分层架构

```text
User Task
  ↓
Task Analyzer
  ↓
Experience Retriever + Pattern Abstractor
  ↓
Director Agent
  ├── TaskDiagnosis
  ├── WorkflowSkeleton
  ├── NodeInstantiation
  ├── PermissionDesign
  └── GraphPatch / TPGPatch proposal
  ↓
Workflow Compiler / PolicyChecker
  ├── permission grounding validation
  ├── anti-copy check
  ├── Overlooker coverage check
  ├── path / command policy check
  └── conflict / concurrency check
  ↓
Workflow Runtime
  ├── Graph Scheduler
  ├── Event Log
  ├── ArtifactStore
  ├── SandboxRuntime
  ├── Codex Exec Wrapper
  ├── Deterministic Validators
  └── Overlooker invocation
  ↓
Node Overlooker
  ├── evidence-cited node acceptance
  ├── pass / fail / blocked / uncertain
  └── OverlookerReport
  ↓
Director
  ├── advance
  ├── retry
  ├── dynamic replan
  ├── permission review request
  └── finalize
  ↓
Reflection / TPGO Layer
  ├── Textual Gradient generation
  ├── Gradient clustering
  ├── TPGEdit / GraphPatch proposal
  ├── targeted validation
  └── OptimizationExperienceMemory
```

### 3.2 四张核心图

v5 中系统不再只有一张 workflow graph，而是四张互相关联的图。

| 图 | 作用 | 典型节点 | 典型边 |
|---|---|---|---|
| Executable Workflow Graph | 运行时执行图 | Node Capsule、Codex Worker、Overlooker Gate、Validator | dependency、data flow、control flow |
| Textual Parameter Graph, TPG | 可优化文本参数图 | Director logic、Overlooker checklist、worker instruction、tool description、policy text | semantic dependency、prompt reference、tool binding |
| Permission Graph | 权限与能力图 | Actor、CapabilityToken、SandboxProfile、SecretRef、ArtifactRef | can_read、can_write、can_execute、can_submit |
| Artifact Lineage Graph | artifact 追踪图 | diff、test log、report、patch、bundle | produced_by、validated_by、consumed_by、derived_from |

其中 TPG 是 v5 新增重点。它不是替代 workflow graph，而是覆盖所有会影响 agent 行为的文本参数层。

---

## 4. 角色定义

### 4.1 Director

Director 是系统的规划者和运行时组织者。

职责：

- 诊断任务类型、风险、复杂度、可并行性；
- 选择或拒绝检索经验；
- 生成 WorkflowSkeleton；
- 实例化 Node Capsule；
- 设计 Codex worker 数量和 sandbox 权限；
- 为每个节点生成 Overlooker acceptance packet；
- 在运行中根据事件生成 GraphPatch；
- 处理冲突、失败、权限不足和重规划；
- 生成最终交付结果。

限制：

- 不能直接授权高权限 sandbox；
- 不能绕过 Compiler / PolicyChecker；
- 不能伪造 EvidenceBundle；
- 不能覆盖 deterministic validator 的 hard fail；
- 不能直接让节点完成，必须经过 Overlooker gate；
- 不能把历史经验照搬成当前 workflow。

### 4.2 Codex Worker

Codex Worker 是节点执行者。

职责：

- 在指定 sandbox 内执行节点任务；
- 运行命令、读写允许路径、生成 patch 或 report；
- 输出结构化 WorkerResult；
- 提交 artifact。

限制：

- 不能自己宣布 NodeAccepted；
- 不能越过 sandbox；
- 不能访问未授权 secret；
- 不能写未授权路径；
- 不能伪造 test result 或 sandbox report。

### 4.3 Overlooker

Overlooker 是节点验收者。它是 Director 的子 agent，但不继承 Director 的全局权限。

职责：

- 接收 NodeAcceptancePacket；
- 查看 EvidenceBundle；
- 检查 success criteria / failure criteria；
- 返回 OverlookerReport；
- 建议 advance / retry / replan / permission review / second opinion；
- 节点完成后释放。

限制：

- 不能直接改 workflow graph；
- 不能直接创建 Codex worker；
- 不能扩大 sandbox 权限；
- 不能跳过 deterministic validator；
- `pass` 必须引用 evidence_ref，否则降级为 `uncertain`。

### 4.4 Runtime / Compiler / PolicyChecker

Runtime 是执行层，Compiler / PolicyChecker 是落地前的结构化防线。

```text
Director says: I propose this workflow.
Compiler says: Is it structurally valid?
PolicyChecker says: Is it permitted?
Runtime says: I can execute it safely.
Overlooker says: This node is acceptable based on evidence.
```

---

## 5. 核心数据对象

### 5.1 WorkflowBlueprint

```yaml
workflow_blueprint:
  workflow_id: string
  objective: string
  director:
    model: string
    planning_mode: experience_guided | from_scratch | hybrid
  nodes: list[NodeCapsule]
  edges: list[WorkflowEdge]
  policies:
    max_parallel_codex_workers: int
    max_workspace_writers_same_repo: int
    network_default: false
    require_overlooker_for_every_node: true
    require_director_for_permission_escalation: true
  adaptation_plan: AdaptationPlan
  provenance:
    retrieved_patterns: list[PatternRef]
    rejected_patterns: list[RejectedPattern]
```

### 5.2 Node Capsule

Node Capsule 是执行、权限、证据、验收的最小闭环单元。

```yaml
node_capsule:
  node_id: string
  goal: string
  phase: diagnose | implement | test | review | finalize | custom

  executor:
    type: codex
    worker_count: int
    sandbox_profile: SandboxProfileRef
    output_schema: SchemaRef

  permission_grounding:
    report_ref: artifact://...
    required_capabilities:
      - read_source
      - write_tests
      - run_local_tests
    denied_capabilities:
      - network
      - dependency_install

  overlooker:
    type: node_overlooker
    acceptance_packet: NodeAcceptancePacket
    release_on: node_accepted

  validators:
    deterministic:
      - output_schema_valid
      - changed_files_within_allowed_paths
      - no_secret_leak
      - relevant_tests_executed
    semantic:
      - overlooker_review

  runtime_contract:
    hard_timeout_sec: int
    heartbeat_timeout_sec: int
    max_retries: int
    replan_allowed: true
```

### 5.3 GraphPatch

GraphPatch 是 Director 在运行中进行动态重规划的唯一合法输出形式。

```yaml
graph_patch:
  patch_id: string
  target_graph_version: int
  issued_by: actor_id
  capability_token: token_ref
  reason:
    summary: string
    evidence_refs: list[ArtifactRef]
  operations:
    - op: retry_node | replace_worker | split_node | insert_node | add_edge | remove_edge | update_join_policy | update_schedule
      args: object
  constraints:
    no_permission_escalation: bool
    max_added_nodes: int
    max_added_retries: int
  signature: string
```

### 5.4 OverlookerReport

```yaml
overlooker_report:
  report_id: string
  node_id: string
  issued_by: actor_id
  verdict: pass | fail | blocked | uncertain
  confidence: float
  criteria_results:
    - criterion: string
      status: pass | fail | unknown
      evidence_refs: list[ArtifactRef]
      notes: string
  failure_type: none | schema_failure | test_failure | insufficient_evidence | permission_insufficient | policy_violation | semantic_mismatch
  recommended_action: advance | retry_same_node | retry_with_modified_instruction | request_director_replan | request_permission_review | require_second_overlooker | require_human_review
  release_overlooker: bool
  signature: string
```

### 5.5 EvidenceBundle

```yaml
evidence_bundle:
  bundle_id: string
  node_id: string
  worker_id: string
  produced_by: runtime
  artifacts:
    codex_event_log: ArtifactRef
    command_log: ArtifactRef
    diff_report: ArtifactRef
    test_reports: list[ArtifactRef]
    sandbox_report: ArtifactRef
    validator_reports: list[ArtifactRef]
    resource_report: ArtifactRef
    policy_report: ArtifactRef
  summary:
    changed_files: list[string]
    commands_executed: list[string]
    tests_passed: bool
    sandbox_violations: list[string]
    secret_findings: list[string]
  signature: string
```

---

## 6. 身份与信任链

### 6.1 ActorIdentity

```yaml
actor_identity:
  actor_id: string
  actor_type: director | overlooker | worker | runtime | compiler | policy_checker | watchdog | reflector | optimizer
  run_id: string
  node_id: string | null
  tenant_id: string | null
  repo_id: string | null
  capabilities:
    - submit_graph_patch
    - submit_worker_result
    - submit_overlooker_report
    - write_artifact
    - read_artifact
    - request_permission_review
```

### 6.2 CapabilityToken

CapabilityToken 是短期、作用域明确的提交/读取/执行授权。

```yaml
capability_token:
  token_id: string
  issued_to: actor_id
  issued_by: runtime
  scope:
    run_id: string
    node_id: string | null
    allowed_actions:
      - write_artifact
      - submit_overlooker_report
  expires_at: timestamp
  nonce: string
  signature: string
```

### 6.3 SignedArtifact / SignedGraphPatch

每个关键 artifact 与 graph patch 都要绑定：

- content hash；
- produced_by；
- run_id / node_id；
- capability token；
- signature；
- lineage parents。

这能防止恶意 worker 伪造 EvidenceBundle，也能让 Overlooker 引用的 evidence 可验证。

---

## 7. ArtifactStore 与 Lineage

ArtifactStore 是 EvidenceBundle 的底层。没有 ArtifactStore，`artifact://...` 就只是字符串。

### 7.1 ArtifactStore 接口

```python
class ArtifactStore:
    def put(self, content: bytes, metadata: ArtifactMetadata, token: CapabilityToken) -> ArtifactRef: ...
    def get(self, ref: ArtifactRef, actor: ActorIdentity) -> bytes: ...
    def stat(self, ref: ArtifactRef) -> ArtifactMetadata: ...
    def verify_hash(self, ref: ArtifactRef) -> bool: ...
    def link_lineage(self, parent_refs: list[ArtifactRef], child_ref: ArtifactRef, operation: str) -> None: ...
```

### 7.2 ArtifactRef

```yaml
artifact_ref:
  uri: artifact://sha256/ab12...
  content_hash: sha256
  media_type: text/x-diff | text/plain | application/json | application/xml | application/octet-stream
  size_bytes: int
  produced_by: actor_id
  run_id: string
  node_id: string | null
  created_at: timestamp
  access_policy: read_by_run | read_by_node | director_only | overlooker_only
```

### 7.3 LineageGraph 需要回答的问题

```text
这个 diff 是哪个 worker 生成的？
基于哪个 workspace snapshot？
哪个 test log 验证了它？
哪个 OverlookerReport 引用了它？
是否被 merge 到最终 artifact？
是否被后续 patch 覆盖？
是否含有 secret scanning finding？
```

MVP 可用：

```text
local content-addressable filesystem + SQLite metadata
```

后续可替换：

```text
S3 / MinIO + Postgres metadata + KMS signature
```

---

## 8. 经验检索工程化

### 8.1 经验库的三类记录

#### 编排骨架经验

```yaml
orchestration_pattern:
  pattern_id: string
  name: parallel_readonly_diagnosis_single_writer
  applicable_when:
    - root_cause_uncertain
    - repo_available
    - write_conflict_risk_high
  skeleton:
    phases:
      - readonly_diagnosis
      - director_selects_diagnosis
      - single_writer_patch
      - independent_test
      - overlooker_review
  invariants:
    - writer_count_same_workspace <= 1
    - diagnosis_requires_no_write
    - worker_cannot_self_certify
```

#### 权限经验

```yaml
permission_pattern:
  pattern_id: string
  lesson: "本地测试修复通常不需要 network；dependency install 才需要显式审批。"
  recommended_default:
    network: false
  escalation_conditions:
    - dependency_missing
    - private_registry_required
```

#### 失败经验

```yaml
failure_pattern:
  pattern_id: string
  symptom:
    - worker says completed
    - no relevant test evidence
  likely_cause: worker_self_certification
  recommended_overlooker_rule:
    - fail_without_test_evidence
```

### 8.2 检索策略

至少应混合：

- task description embedding；
- repo metadata embedding；
- failure signature embedding；
- code context embedding；
- BM25 keyword retrieval；
- reranker。

检索质量过低时必须允许 abstain：

```yaml
retrieval_decision:
  use_experience: false
  reason: "top-k patterns mismatch current repo stack and task type"
  fallback: from_scratch_director_planning
```

### 8.3 Pattern Abstractor

Pattern Abstractor 负责去实例化：

```text
历史 case 中的具体 agent 数量、具体路径、具体命令、具体 sandbox profile
  ↓
抽象成 role slots、phase skeleton、permission lessons、failure lessons
```

这样 Director 拿到的是经验先验，而不是可直接执行配置。

---

## 9. Director Agent 设计

### 9.1 Staged Planning

Director 不应一次性生成完整 workflow，而应分阶段输出。

```text
Stage 1: TaskDiagnosis
Stage 2: WorkflowSkeleton
Stage 3: NodeInstantiation
Stage 4: PermissionDesign
Stage 5: Overlooker Acceptance Packet Generation
Stage 6: Compiler-facing WorkflowBlueprint
```

### 9.2 TaskDiagnosis schema

```yaml
task_diagnosis:
  task_type: coding_fix | refactor | test_generation | data_analysis | research | gui_operation | custom
  repo_required: bool
  risk_level: low | medium | high
  uncertainty: low | medium | high
  parallelizable: bool
  likely_required_capabilities:
    - read_repo
    - run_tests
    - write_source
  likely_sensitive_areas:
    - auth
    - payment
    - secrets
  planning_mode: experience_guided | from_scratch | hybrid
```

### 9.3 WorkflowSkeleton schema

```yaml
workflow_skeleton:
  phases:
    - id: diagnose
      purpose: "定位失败原因"
      role_slots:
        - readonly_diagnoser
    - id: implement
      purpose: "生成最小 patch"
      role_slots:
        - writer
    - id: test
      purpose: "独立测试"
      role_slots:
        - test_runner
    - id: review
      purpose: "Overlooker 验收"
      role_slots:
        - node_overlooker
  dependency_edges:
    - from: diagnose
      to: implement
    - from: implement
      to: test
    - from: test
      to: review
```

### 9.4 AdaptationPlan

Director 必须说明如何改造检索经验。

```yaml
adaptation_plan:
  reused_elements:
    - "read-only diagnosis before writing"
    - "single writer for same workspace"
  modified_elements:
    - field: diagnoser_count
      from_pattern: 1
      current_value: 2
      reason: "当前失败日志不足，需要两个只读诊断视角"
    - field: implementation_sandbox
      from_pattern: workspace_write_all
      current_value: workspace_write_limited_paths
      reason: "当前任务只涉及 src/ 与 tests/"
  rejected_elements:
    - element: network_enabled
      reason: "当前任务没有依赖安装需求"
```

### 9.5 Director abstention

Director 可以拒绝使用检索经验。

```yaml
director_pattern_decision:
  use_retrieved_experience: false
  selected_patterns: []
  rejected_patterns:
    - pattern_id: pattern_123
      reason: "历史 pattern 针对 Python repo，但当前 repo 是 TypeScript"
  planning_mode: from_scratch
```

---

## 10. RepoPolicy 与 Permission Grounding

### 10.1 RepoPolicyInferencer

RepoPolicyInferencer 从 repo 中推导事实来源：

- `.github/workflows/*`；
- `package.json`；
- `pyproject.toml`；
- `requirements.txt`；
- `go.mod`；
- `Cargo.toml`；
- `Makefile`；
- repo tree；
- org-level policy；
- sensitive path configuration。

### 10.2 RepoPolicy schema

```yaml
repo_policy:
  repo_id: string
  language_stack:
    - typescript
  package_managers:
    - npm
  test_commands:
    - command: npm test
      source: package.json:scripts.test
      confidence: 0.95
  lint_commands:
    - command: npm run lint
      source: package.json:scripts.lint
      confidence: 0.90
  source_paths:
    - src/
  test_paths:
    - tests/
    - __tests__/
  config_paths:
    - package.json
    - tsconfig.json
  sensitive_paths:
    - .env
    - secrets/
    - .ssh/
    - .aws/
  commands_requiring_approval:
    - npm install
    - pnpm install
    - pip install
  forbidden_commands:
    - git push
    - rm -rf /
```

### 10.3 PermissionGroundingReport

```yaml
permission_grounding_report:
  node_id: implement_patch
  requested_capabilities:
    - write_source
    - write_tests
    - run_local_tests
  grounded_facts:
    - fact: "src/ exists"
      source: repo_tree
    - fact: "tests/ exists"
      source: repo_tree
    - fact: "npm test is declared"
      source: package.json:scripts.test
  proposed_sandbox:
    network: false
    writable_paths:
      - src/
      - tests/
    allowed_commands:
      - npm test
  denied_capabilities:
    - network
    - dependency_install
    - write_sensitive_paths
  confidence: 0.91
```

---

## 11. Workflow Compiler 与 Policy Engine

### 11.1 Compiler pipeline

```text
WorkflowBlueprint
  → schema validation
  → actor/capability validation
  → repo permission grounding validation
  → sandbox profile validation
  → anti-copy check
  → Overlooker coverage validation
  → path / command allowlist validation
  → writable path conflict detection
  → graph acyclicity / scheduling validation
  → signed compiled workflow
```

### 11.2 规则表达

MVP 可以先使用 Python rules + JSON Schema。若需要组织级可扩展策略，可引入 OPA/Rego。

示例规则：

```rego
deny[msg] {
  input.node.executor.sandbox.network == true
  not input.node.permission_grounding.network_required
  msg := "network=true requires grounded network_required fact"
}

deny[msg] {
  input.node.executor.sandbox.writable_paths[_] == input.repo_policy.sensitive_paths[_]
  msg := "node cannot write sensitive paths"
}
```

### 11.3 反照搬检测

```text
blueprint_pattern_similarity > threshold
AND adaptation_plan.modified_elements is empty
AND sandbox profile / agent count / phase order are identical
→ reject or request Director justification
```

### 11.4 并发写冲突检测

```text
if two active nodes share same workspace
AND both have write permission
AND writable_paths overlap
→ block parallel scheduling
```

可选处理：

- 改成串行；
- 使用 isolated worktree；
- 只允许 one writer；
- Director 生成 GraphPatch 分解任务。

---

## 12. Sandbox 工程实现

### 12.1 SandboxProfile

```yaml
sandbox_profile:
  profile_id: workspace_editor_no_network
  backend: codex_native | docker | gvisor | firecracker
  sandbox_mode: read_only | workspace_write | full_access_isolated
  network:
    mode: none | allowlist | full
    allowlist:
      - registry.npmjs.org
  filesystem:
    read_paths:
      - ./
    writable_paths:
      - src/
      - tests/
    forbidden_paths:
      - .env
      - secrets/
      - .ssh/
  commands:
    allow:
      - npm test
      - npm run lint
    require_approval:
      - npm install
    deny:
      - git push
      - rm -rf /
  resources:
    cpu_quota: "2"
    memory_mb: 4096
    disk_mb: 8192
    hard_timeout_sec: 600
  secrets:
    allowed_secret_refs: []
```

### 12.2 Backend mapping

| SandboxProfile 字段 | Codex native | Docker | gVisor | Firecracker |
|---|---|---|---|---|
| read-only | Codex sandbox mode | bind mount ro | runsc ro mount | read-only shared fs |
| workspace-write | Codex workspace-write | overlay upperdir | overlay + runsc | microVM overlay |
| network:none | Codex network off | `--network none` | isolated netstack | no egress device |
| resources | Codex/runtime wrapper | cgroup | cgroup/runsc | VM resource config |
| audit | Codex JSONL + wrapper | logs + fanotify/eBPF | runsc logs + audit | VM logs + host monitor |

### 12.3 Sandbox event stream

```yaml
sandbox_event:
  event_id: string
  run_id: string
  node_id: string
  worker_id: string
  timestamp: timestamp
  event_type: file_read | file_write | command_exec | network_attempt | policy_violation | resource_limit
  target: string
  allowed: bool
  reason: string
```

---

## 13. Codex Exec Wrapper

### 13.1 Wrapper 职责

- 启动 `codex exec`；
- 挂载 workspace / sandbox；
- 传入节点 prompt 和 output schema；
- 解析 JSONL event stream；
- 捕获 stdout / stderr / commands / file changes；
- 处理 timeout、crash、OOM、hang；
- 生成 WorkerResult；
- 写入 ArtifactStore。

### 13.2 JSONL event mapping

```yaml
codex_event_mapping:
  thread.started: WorkerStarted
  turn.started: WorkerTurnStarted
  item.started: WorkerProgress
  item.completed: WorkerProgress
  turn.completed: WorkerSubmitted
  turn.failed: WorkerFailed
  error: WorkerRuntimeError
```

### 13.3 生命周期

```text
CREATED
  ↓
PROCESS_STARTED
  ↓
WORKER_STARTED
  ↓
RUNNING
  ↓
WORKER_SUBMITTED
  ↓
EVIDENCE_COLLECTED
  ↓
VALIDATED
  ↓
OVERLOOKER_REVIEWING
  ↓
NODE_ACCEPTED / NODE_FAILED / NODE_BLOCKED / NODE_UNCERTAIN
```

### 13.4 错误处理

```yaml
wrapper_failure:
  start_timeout: retry_or_reassign
  idle_timeout: kill_and_report_no_progress
  hard_timeout: cancel_and_replan
  json_parse_error: fail_worker_result
  process_exit_nonzero: worker_failed
  oom_detected: resource_failure
  sandbox_violation: block_and_report
```

---

## 14. EvidenceBundle 标准化采集

### 14.1 diff 采集

- 使用 `git diff` 或 overlay diff；
- 路径范围过滤；
- 大 diff 摘要；
- binary 文件标记；
- changed_files 列表；
- forbidden path 检测。

### 14.2 test result adapter

支持：

- pytest；
- jest；
- go test；
- cargo test；
- JUnit XML；
- TAP。

输出：

```yaml
test_report:
  command: string
  status: pass | fail | skipped | unknown
  total: int
  passed: int
  failed: int
  duration_ms: int
  raw_output_ref: ArtifactRef
  parsed_cases: list[TestCase]
```

### 14.3 ValidatorReport

```yaml
validator_report:
  validator_id: string
  node_id: string
  status: pass | fail | warning
  checks:
    - name: changed_files_within_allowed_paths
      status: pass
      evidence_refs: list[ArtifactRef]
    - name: no_secret_leak
      status: pass
      evidence_refs: list[ArtifactRef]
```

---

## 15. Overlooker 可靠性

### 15.1 Anti rubber-stamp

Overlooker 的 `pass` 必须满足：

- 每条 pass criterion 都有 evidence_ref；
- 至少一个 deterministic validator 通过；
- 必要测试证据存在；
- 无 policy violation；
- 无 secret leak；
- 没有把 worker 自述当作唯一证据。

否则：

```text
pass → uncertain
```

### 15.2 多 Overlooker 一致性

高风险节点可要求：

```yaml
multi_overlooker_policy:
  required: true
  count: 2
  consensus: both_pass | majority | director_arbitration
  apply_when:
    - touches_auth
    - touches_payment
    - uses_secret
    - requests_permission_escalation
```

### 15.3 Prompt 隔离

Overlooker 不应看到 Director 的乐观推理或未验证假设，只看：

- NodeAcceptancePacket；
- EvidenceBundle；
- ValidatorReport；
- SandboxReport；
- success / failure criteria；
- allowed verdicts。

---

## 16. 冲突仲裁

### 16.1 优先级

```text
Policy / deterministic validator > Overlooker > Director preference
```

示例：

- validator fail：不能 NodeAccepted；
- policy violation：必须 block；
- Overlooker fail：Director 不能直接覆盖，只能 retry、replan、second-overlooker 或 human review；
- Director 想扩权：必须 permission review。

### 16.2 DecisionConflict

```yaml
decision_conflict:
  conflict_id: string
  node_id: string
  director_intent: advance | retry | replan | escalate_permission
  overlooker_verdict: pass | fail | blocked | uncertain
  validator_status: pass | fail | warning
  conflict_type:
    - director_wants_advance_but_overlooker_failed
    - overlooker_pass_but_validator_failed
    - permission_escalation_disagreement
    - multiple_overlookers_disagree
  resolution_policy: second_overlooker | director_replan | human_review | hard_block
```

### 16.3 状态机

```text
OVERLOOKER_REPORTED
  ↓
if validator_fail → NODE_BLOCKED
if policy_violation → NODE_BLOCKED
if overlooker_pass + validator_pass → DIRECTOR_DECISION
if director_disagrees → CONFLICT_REVIEW
if critical_node + disagreement → SECOND_OVERLOOKER
if unresolved → HUMAN_REVIEW_OR_ABORT
```

---

## 17. 状态机与并发 Runtime

### 17.1 Node 状态机

```text
NODE_PLANNED
  ↓
NODE_COMPILED
  ↓
OVERLOOKER_BOUND
  ↓
CODEX_ASSIGNED
  ↓
CODEX_RUNNING
  ↓
CODEX_SUBMITTED
  ↓
EVIDENCE_COLLECTION
  ↓
DETERMINISTIC_VALIDATION
  ↓
OVERLOOKER_REVIEW
  ↓
DIRECTOR_RECEIVES_REPORT
  ↓
NODE_ACCEPTED / NODE_FAILED / NODE_BLOCKED / NODE_UNCERTAIN
  ↓
OVERLOOKER_RELEASED
  ↓
SUCCESSORS_ACTIVATED
```

### 17.2 动态重规划

动态重规划是 Director 在执行过程中基于 event log、node status、OverlookerReport、validator result、budget 和 concurrency constraints 生成 GraphPatch。

它可以做：

- retry node；
- replace worker；
- split node；
- insert validator node；
- add Overlooker；
- update join policy；
- change scheduling；
- isolate parallel writers；
- request permission review。

它不能直接做：

- 自动放大 sandbox；
- 跳过 Overlooker；
- 覆盖 validator fail；
- 无限 replan。

### 17.3 Replan budget

```yaml
replan_budget:
  max_graph_patches_per_run: 5
  max_replans_per_node: 2
  max_retry_depth: 3
  on_budget_exhausted: escalate_or_abort
```

---

## 18. v5 新增：Textual Parameter Graph Optimization Layer

这一节是 v5 的核心新增技术。它借鉴 TPGO 的思想：不要只调一整段 prompt，而要把 MAS 的文本配置拆成图，并通过执行轨迹生成 textual gradients，再用历史 optimization experience 学习“怎么改系统”。

### 18.1 TPG 不等于 Workflow Graph

Workflow Graph 负责执行：

```text
diagnose → implement_patch → run_tests → review
```

TPG 负责表示可优化文本参数：

```text
Director role prompt
Director planning logic
Overlooker acceptance checklist
Codex worker instruction
Tool description
Policy explanation
Conflict resolution text
Permission grounding prompt
```

换句话说：

```text
Workflow Graph tells the system what to run.
TPG tells the system what textual parameters shape agent behavior.
```

### 18.2 TPGBuilder

TPGBuilder 从当前系统配置中构建文本参数图。

```yaml
textual_parameter_graph:
  graph_id: string
  version: int
  nodes:
    - node_id: director_role_v1
      node_type: director_role
      content_ref: artifact://...
      owner: director
      editable: true
    - node_id: director_permission_logic_v1
      node_type: director_logic
      content_ref: artifact://...
      editable: true
    - node_id: overlooker_acceptance_logic_v1
      node_type: overlooker_logic
      content_ref: artifact://...
      editable: true
    - node_id: codex_worker_instruction_v1
      node_type: worker_instruction
      content_ref: artifact://...
      editable: true
    - node_id: tool_test_runner_description_v1
      node_type: tool_description
      content_ref: artifact://...
      editable: true
  edges:
    - from: director_permission_logic_v1
      to: sandbox_profile_generation
      relation: governs
    - from: overlooker_acceptance_logic_v1
      to: overlooker_report_schema
      relation: produces
```

### 18.3 Textual Gradient

Textual Gradient 是从执行轨迹中提炼出的优化信号，分为 positive 和 negative。

```yaml
textual_gradient:
  gradient_id: string
  run_id: string
  node_id: string | null
  gradient_type: positive | negative
  scope:
    - director_planning
    - permission_grounding
    - worker_execution
    - overlooker_review
    - workflow_scheduling
    - conflict_arbitration
  observed_symptom: string
  root_cause_hypothesis: string
  generalizable_lesson: string
  target_tpg_nodes:
    - overlooker_acceptance_logic_v1
  suggested_change_type:
    - rewrite_instruction
    - add_validation_checkpoint
    - tighten_permission_rule
    - insert_overlooker_gate
    - change_join_policy
  evidence_refs:
    - artifact://...
```

要求：

- 只能描述可泛化行为模式；
- 不能把某个 case 的答案写进 prompt；
- 必须引用 evidence_ref；
- 必须标注影响范围；
- 必须区分根因是 Director、worker、Overlooker、runtime 还是 policy。

### 18.4 Positive Gradient

成功案例也要学习。

```yaml
positive_gradient:
  summary: "read-only diagnosis before workspace-write patch reduced permission risk without hurting success"
  reusable_pattern:
    - readonly_diagnosis
    - single_writer
    - independent_test
    - evidence_cited_overlooker
  target_tpg_nodes:
    - director_workflow_skeleton_logic_v1
```

可学习内容包括：

- 成功的最小权限设计；
- 成功的节点拆分策略；
- 成功的 Overlooker checklist；
- 成功的 join policy；
- 成功的 retry / repair strategy。

### 18.5 Negative Gradient

失败案例要定位到可改节点。

```yaml
negative_gradient:
  observed_symptom: "Overlooker passed a patch without relevant test evidence"
  root_cause_hypothesis: "overlooker acceptance logic allowed Codex self-report as sufficient evidence"
  generalizable_lesson: "Overlooker pass must require parsed test report or explicit validator pass for test-related nodes"
  target_tpg_nodes:
    - overlooker_acceptance_logic_v1
  suggested_change_type:
    - rewrite_instruction
    - add_validation_checkpoint
```

### 18.6 Gradient Clustering

不要为单个失败过拟合。系统应将 negative gradients 聚类成 error clusters。

```yaml
gradient_cluster:
  cluster_id: string
  cluster_name: missing_test_evidence_self_certification
  member_gradients: list[GradientRef]
  common_symptoms:
    - worker says completed
    - no parsed test report
    - overlooker pass too easily
  likely_systemic_cause: overlooker_acceptance_logic_too_weak
  recommended_targets:
    - overlooker_acceptance_logic_v1
    - deterministic_validator_config_v1
```

常见 cluster：

- Overlooker rubber-stamp；
- Director permission overgrant；
- missing test evidence；
- writer path conflict；
- premature finalization；
- sandbox profile mismatch；
- repeated replan oscillation；
- tool misuse due to unclear tool description。

### 18.7 TPGEdit

TPGEdit 是机器可读的文本参数图编辑操作。

```yaml
tpg_edit:
  edit_id: string
  target_tpg_version: int
  reason:
    cluster_id: string
    evidence_refs: list[ArtifactRef]
  operations:
    - op: rewrite_node
      node_id: overlooker_acceptance_logic_v1
      new_content_ref: artifact://...
    - op: add_node
      node:
        node_id: overlooker_test_evidence_rule_v1
        node_type: overlooker_logic
        content_ref: artifact://...
    - op: add_edge
      from: overlooker_test_evidence_rule_v1
      to: overlooker_acceptance_logic_v1
      relation: constrains
  constraints:
    no_permission_expansion: true
    no_skip_validator: true
    minimal_change_required: true
```

允许操作：

- `rewrite_node`；
- `add_node`；
- `remove_node`；
- `add_edge`；
- `remove_edge`；
- `tighten_policy_text`；
- `add_overlooker_checklist_item`；
- `add_worker_instruction_constraint`；
- `add_director_planning_constraint`。

禁止操作：

- 直接扩大 sandbox 权限；
- 删除 Overlooker gate；
- 删除 deterministic validator；
- 删除 artifact signature check；
- 删除 secret redaction；
- 删除 conflict arbitration。

### 18.8 GraphPatch + TPGEdit 的关系

```text
GraphPatch
  改运行时 workflow 结构。

TPGEdit
  改 agent / tool / policy 的文本参数。
```

例子：

```text
问题：Codex 经常提交 patch 但不跑测试。

GraphPatch 方案：在 implement_patch 后插入 run_targeted_tests 节点。
TPGEdit 方案：修改 worker instruction 和 Overlooker checklist，要求测试证据。
```

通常二者可以组合：

```yaml
optimization_patch:
  graph_patch:
    operations:
      - insert_node: run_targeted_tests
  tpg_edit:
    operations:
      - rewrite_node: overlooker_acceptance_logic_v1
      - rewrite_node: codex_worker_instruction_v1
```

### 18.9 GRAO-style OptimizationExperienceMemory

存储的不只是 SOP，而是：

```text
遇到了什么优化问题？
提出了什么 patch？
结果如何？
是否产生副作用？
是否回滚？
```

```yaml
optimization_experience:
  experience_id: string
  problem_context:
    error_cluster: missing_test_evidence_self_certification
    task_family: coding_fix
    repo_stack: typescript_jest
    affected_nodes:
      - implement_patch
      - run_tests
    affected_tpg_nodes:
      - overlooker_acceptance_logic_v1
      - codex_worker_instruction_v1
  proposed_solution:
    graph_patch_ref: artifact://...
    tpg_edit_ref: artifact://...
    summary: "insert targeted test node and require parsed TestReport evidence for Overlooker pass"
  outcome:
    effectiveness_score: 0.82
    safety_score: 0.95
    cost_delta: +0.12
    time_delta_sec: +45
    rollback_required: false
    downstream_regression: false
  learned_lesson: "For test-related coding nodes, Overlooker pass should require parsed test artifact, not worker self-report."
```

### 18.10 Targeted Validation

每次 TPGEdit / GraphPatch 不应全量验证所有历史任务，而应验证受影响范围。

```yaml
patch_validation_plan:
  patch_id: string
  affected_components:
    - overlooker_acceptance_logic_v1
    - implement_patch_node_template
  validation_scope:
    historical_failure_clusters:
      - missing_test_evidence_self_certification
    affected_task_families:
      - coding_fix
    affected_repo_stacks:
      - typescript_jest
  accept_if:
    - false_pass_rate_decreases
    - no_policy_violation_added
    - no_permission_expansion
    - cost_delta_within_budget
  rollback_if:
    - validator_regression
    - overlooker_false_fail_rate_explodes
    - permission_risk_increases_without_benefit
```

### 18.11 Rollback + failed update retention

如果优化 patch 失败：

```text
回滚 TPG version / Workflow version。
保留失败 proposal 作为 NegativeOptimizationCase。
下次遇到类似问题时，GRAO-style memory 可以告诉 Optimizer：这个改法过去失败过。
```

```yaml
rollback_record:
  rollback_id: string
  patch_id: string
  from_version: int
  to_version: int
  reason: "targeted validation introduced overlooker false failures"
  retained_as_negative_case: true
```

---

## 19. Secret / 凭据管理

### 19.1 原则

```text
Secret 不进普通 prompt。
Secret 不进 event log。
Secret 不进 artifact 明文。
Secret 不给 Overlooker 直接查看。
Secret 只以 SecretRef 形式授权给特定节点和 worker。
```

### 19.2 SecretRef

```yaml
secret_ref:
  id: secret://npm-token-ci
  scope: tenant | repo | workflow | node
  allowed_actors:
    - worker:dependency_installer
  allowed_nodes:
    - install_dependencies
  exposure_mode: env_var | mounted_file | brokered_token
  redaction_policy: always
  audit_required: true
```

### 19.3 SandboxProfile 中的 secret 字段

```yaml
secrets:
  allowed_secret_refs:
    - secret://npm-token-ci
  injection:
    mode: env_var
    env_name: NPM_TOKEN
  log_redaction:
    enabled: true
```

### 19.4 Secret 防泄漏机制

- stdout / stderr masking；
- artifact scanning；
- event log redaction；
- prompt secret detector；
- Overlooker 只能看到 secret access report；
- secret usage audit；
- suspected leak 触发 `SecretViolation`。

---

## 20. 多租户 / 多 Repo 隔离基础

MVP 可以不实现完整多租户平台，但必须预留隔离上下文。

```yaml
isolation_context:
  tenant_id: string
  repo_id: string
  run_id: string
  workspace_id: string
  policy_scope:
    - org_policy
    - tenant_policy
    - repo_policy
    - workflow_policy
```

约束：

- artifact_ref 必须绑定 tenant_id / repo_id / run_id；
- worker 不能读其他 run 的 artifact；
- secret_ref 不能跨 tenant；
- repo policy 不能隐式跨 repo 复用；
- sandbox workspace 必须 per-run 隔离；
- cross-repo task 必须显式声明 repo graph。

---

## 21. 实现阶段建议

### Phase A：最小可运行节点执行

目标：跑通一个完整单节点闭环。

```text
Codex Worker
  → EvidenceBundle
  → deterministic validators
  → Overlooker
  → NodeAccepted
```

实现：

- Codex Exec Wrapper；
- JSONL event parser；
- local ArtifactStore；
- basic ActorIdentity / CapabilityToken；
- Node Capsule schema；
- EvidenceBundle schema；
- basic validators；
- single Node Overlooker；
- SQLite event log。

核心验收：

```text
turn.completed 不会直接变成 NODE_ACCEPTED。
```

### Phase B：Director v1 + 权限 grounding + compiler

目标：让 Director 能生成初始 workflow，并且权限设计有 repo 事实依据。

实现：

- Director staged planning；
- TaskDiagnosis；
- WorkflowSkeleton；
- NodeInstantiation；
- RepoPolicyInferencer；
- PermissionGroundingReport；
- WorkflowCompiler；
- basic anti-copy；
- path / command allowlist。

### Phase C：Sandbox backend 与资源限制

目标：让 SandboxProfile 真正生效。

实现：

- Codex native sandbox mapping；
- Docker backend optional；
- overlay filesystem；
- network none enforcement；
- resource limits；
- sandbox event stream；
- ResourceReport。

### Phase D：并发 Runtime 与动态重规划

目标：从单节点升级到多节点 DAG，支持并发调度和 GraphPatch。

实现：

- Graph Scheduler；
- checkpoint / resume；
- parallelism control；
- write conflict detection；
- retry budget；
- livelock detection；
- v5-style OverlookerReport: verdict、confidence、cited_evidence、failure_type、recommended_action、release_overlooker；
- Stage D GraphPatch schema；
- Compiler validate_patch；
- Director runtime GraphPatch；
- runtime apply compiled retry_node patch；
- Overlooker-guided retry fork from accepted upstream workspace。

边界：

- Phase D 只执行 `retry_node` 这类有明确 runtime 行为的补丁；
- `insert_node`、`split_node`、`replace_worker`、`add_edge`、`remove_edge`、`update_join_policy` 的 compiler 校验入口进入 Phase E；
- `update_schedule` 暂不启用，留到更完整的调度策略阶段；
- TPGEdit、TextualGradient、OptimizationMemory、targeted validation、rollback retention 进入 Phase F；
- Secret、redaction、artifact secret scanning、per-run isolation boundary 进入 Phase G。

### Phase E：冲突仲裁与高风险节点

目标：处理 Director、Overlooker、Validator、Policy 之间的意见冲突，并承接 Phase D 未启用的高级 GraphPatch 操作。

实现：

- DecisionConflict schema；
- ConflictResolution 状态机；
- second Overlooker；
- human review placeholder；
- permission escalation path；
- high-risk node policy。
- advanced GraphPatch compiler checks: insert_node、split_node、replace_worker、add_edge、remove_edge、update_join_policy；
- update_schedule deferred；
- bounded replan / retry budget；
- policy / validator > Overlooker > Director conflict priority。

### Phase F：经验检索 + TPGO 自改进层

目标：让经验指导 Director，但不照搬；让系统从执行轨迹中学习如何改自己。

实现：

- ExperienceIndex；
- PatternAbstractor；
- retrieval abstention；
- AdaptationPlan validation；
- TPGBuilder；
- TextualGradient generator；
- GradientClusterer；
- TPGEdit / TPGPatch；
- GraphPatch + TPGEdit combined optimization；
- GRAO-style OptimizationExperienceMemory；
- targeted validation；
- rollback + failed proposal retention。

### Phase G：Secret 与隔离基础

目标：支持需要凭据的节点，同时避免 secret 泄漏，并建立 per-run / per-repo 边界。

实现：

- SecretRef；
- SecretInjectionPolicy；
- stdout / artifact redaction；
- artifact secret scanning；
- IsolationContext；
- per-run artifact access boundary。

---

## 22. MVP 技术选型表

| 组件 | MVP 选型 | 替代方案 | 替换触发条件 |
|---|---|---|---|
| State Store | SQLite | Postgres | 多进程/多机并发 |
| Event Log | SQLite append-only table | Kafka / NATS / Postgres logical log | 事件量大或分布式 |
| Task Queue | asyncio + process pool | Celery / Redis Queue / Temporal | 长任务、分布式 worker |
| Artifact Store | local CAS filesystem | S3 / MinIO | 多机、长期保留 |
| Sandbox | Codex native + subprocess wrapper | Docker / gVisor / Firecracker | 安全等级提升 |
| Policy Engine | Python rules + JSON Schema | OPA/Rego | 组织级可扩展策略 |
| Embedding | 通用 embedding + BM25 | code-specific embedding + reranker | 检索质量不足 |
| Reranker | LLM rerank | cross-encoder | 成本/稳定性需求 |
| Test Adapter | pytest / jest | go test / cargo / JUnit / TAP | 语言扩展 |
| Secret Store | local encrypted config | Vault / cloud secret manager | 多租户/生产 |
| TPG Store | SQLite + ArtifactStore | Graph DB / Postgres | TPG 版本和 lineage 变复杂 |
| Gradient Clusterer | embedding + DBSCAN/HDBSCAN | custom clustering | failure pattern 复杂化 |

---

## 23. 推荐模块接口清单

```text
DirectorPlanner
  diagnose(task, repo_policy, retrieved_experience) -> TaskDiagnosis
  create_skeleton(task_diagnosis) -> WorkflowSkeleton
  instantiate_nodes(skeleton, repo_policy) -> WorkflowBlueprint
  propose_graph_patch(runtime_state) -> GraphPatch

ExperienceRetriever
  retrieve(task, repo_context, failure_signature) -> list[Experience]

PatternAbstractor
  abstract(experience) -> AbstractPattern

RepoPolicyInferencer
  infer(repo_snapshot) -> RepoPolicy

PermissionGrounder
  ground(node_goal, repo_policy, requested_capabilities) -> PermissionGroundingReport

WorkflowCompiler
  compile(blueprint) -> CompiledWorkflow
  validate_patch(graph_patch) -> CompiledGraphPatch

PolicyChecker
  evaluate(compiled_workflow) -> PolicyDecision

SandboxRuntime
  start_worker(node_capsule, sandbox_profile) -> WorkerHandle

CodexExecWrapper
  run(node_prompt, sandbox_profile, output_schema) -> WorkerResult

EvidenceCollector
  collect(worker_result, sandbox_events, repo_snapshot) -> EvidenceBundle

OverlookerRunner
  review(node_acceptance_packet, evidence_bundle) -> OverlookerReport

ConflictResolver
  resolve(decision_conflict) -> ResolutionDecision

TPGBuilder
  build(system_config) -> TextualParameterGraph

GradientReflector
  generate(run_trace, evidence_bundle, reports) -> list[TextualGradient]

GradientClusterer
  cluster(gradients) -> list[GradientCluster]

PatchOptimizer
  propose(cluster, optimization_memory, current_tpg, current_workflow) -> OptimizationPatch

PatchValidator
  validate_targeted(patch, validation_scope) -> PatchValidationResult

OptimizationExperienceMemory
  put(problem_context, proposed_solution, outcome) -> ExperienceRef
  retrieve(problem_context) -> list[OptimizationExperience]
```

---

## 24. 最终架构总结

v5 的核心形态是：

```text
Director
  负责生成 task-conditioned workflow、sandbox 权限设计、GraphPatch 和最终组织决策。

Codex Worker
  在 sandbox 内执行节点任务，只提交 artifact 和结构化结果，不自证完成。

Overlooker
  基于 EvidenceBundle 验收节点，pass/fail/blocked/uncertain，节点完成后释放。

Runtime / Compiler / PolicyChecker
  负责执行、状态、权限、并发、冲突、信任链、Artifact lineage。

TPGO Self-Improvement Layer
  负责把 agent/workflow/policy 文本参数建成 TPG，从执行轨迹生成 textual gradients，聚类失败模式，提出 TPGEdit / GraphPatch，并通过 targeted validation 和 rollback 安全地优化系统。
```

最重要的设计边界：

```text
经验可以指导 Director，但不能替代 Director 的当前任务推导。
Director 可以提出权限设计，但不能直接授权。
Worker 可以提交结果，但不能自证完成。
Overlooker 可以放行节点，但必须引用 evidence。
TPGO 可以优化文本参数和补丁策略，但不能绕过 runtime 安全边界。
```

一句话：

> **EGTC-PAW v5 是一个由 Director 规划、Codex 执行、Overlooker 放行、Runtime 约束、TPGO 层自我优化的权限感知多 agent workflow 系统。**

---

## 25. 参考资料

- Learning to Evolve: A Self-Improving Framework for Multi-Agent Systems via Textual Parameter Graph Optimization, arXiv: https://arxiv.org/abs/2604.20714
- OpenAI Codex non-interactive mode: https://developers.openai.com/codex/noninteractive
- OpenAI Codex sandboxing: https://developers.openai.com/codex/concepts/sandboxing
- OpenAI Codex approvals and security: https://developers.openai.com/codex/agent-approvals-security
- Open Policy Agent / Rego policy language: https://openpolicyagent.org/docs/policy-language
