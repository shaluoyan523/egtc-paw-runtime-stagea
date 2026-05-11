# EGTC-PAW Runtime v4 Stage A

This folder deploys Phase A from `egtc_paw_runtime_v4_engineering_blueprint_trust_artifacts_sandbox.md`.

Phase A goal:

```text
single worker execution -> WorkerSubmitted -> evidence -> validators -> Overlooker -> NodeAccepted
```

Implemented components:

- `CodexExecWrapper`: subprocess runner with JSONL event parsing.
- `ArtifactStore`: local content-addressable artifact store.
- `IdentityService`: basic `ActorIdentity` and HMAC `CapabilityToken`.
- `NodeCapsule` / `EvidenceBundle`: Stage A schemas.
- `DeterministicValidator`: evidence ref, required artifact, integrity, test, and diff checks.
- `CodexOverlooker`: separate Codex-backed reviewer; cannot pass without `evidence_ref`.
- `EventLog`: SQLite append-only runtime event log.
- `StageARuntime`: single-node orchestration.

Run the demo:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/stagea_demo.py
```

Run a ModelScope SWE-bench smoke test:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/swe_phasea_smoke.py --split train --scan-limit 80 --count 3
```

This downloads/streams `AI-ModelScope/SWE-bench`, selects small cases by patch and issue size, and runs them through the Stage A evidence chain. It is a static Phase A runtime smoke test; it does not clone target repositories or run the official SWE-bench unit-test harness.

Run the same style of smoke test with a real Codex CLI worker:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/swe_phasea_codex_smoke.py --split train --scan-limit 40 --count 1
```

This path launches `codex exec --json` and captures Codex JSONL events as worker evidence.

Run the Phase B Director Agent v1 planning demo:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/phaseb_director_demo.py
```

Run compiler negative checks:

```bash
python3 examples/phaseb_compiler_negative_demo.py
```

This intentionally asks for ungrounded network access and a sensitive write path; the compiler should reject it.

Run Phase B against complex SWE-bench cases:

```bash
python3 examples/phaseb_swe_complex_demo.py --split train --scan-limit 500 --count 3
```

The selector scores cases by patch size, test patch size, issue length, `FAIL_TO_PASS` count, and preferred large Python repositories.

Run Phase B with a real Codex Director Agent session:

```bash
python3 examples/phaseb_codex_director_complex_demo.py --split train --scan-limit 120
```

This creates a Director workspace, launches `codex exec --json`, requires `director_output.json`, and validates the Director's structured output.

Run Phase B with all agent roles performed by Codex:

```bash
python3 examples/phaseb_all_codex_agents_complex_demo.py --split train --scan-limit 120
```

This launches Codex for the Director Agent, one Codex worker per Director-created node, and a Codex Overlooker.

Run the Phase C sandbox/resource demo:

```bash
python3 examples/phasec_sandbox_demo.py
```

Phase C adds Codex-native sandbox mapping, `network:none` sandbox events, resource limits, `ResourceReport`, and evidence artifacts for sandbox event streams.

Run Phase C against a complex SWE-bench case with all roles performed by Codex:

```bash
python3 examples/phasec_all_codex_complex_demo.py --split train --scan-limit 120
```

This launches the Director Agent, one Codex worker per Director-created node, and the Codex overlooker through `CodexExecWrapper`. The report verifies that every agent session produced Phase C sandbox events and a resource report.

Run the Phase D graph runtime demo:

```bash
python3 examples/phase_d_graph_runtime_demo.py
```

Phase D adds a DAG scheduler with parallel read-only workers, a single-writer lock, checkpoint/resume, retry budget, and livelock/deadlock detection. The demo pauses after two accepted diagnosis nodes, resumes from checkpoint, serializes competing writer nodes, retries one flaky verification node from a clean accepted upstream fork, and finishes accepted.

Phase B adds:

- `DirectorAgentV1`: staged central planner named Director Agent.
- `TaskDiagnosis`: task kind, risk, touchpoints, test/code-change needs, unknowns.
- `WorkflowSkeleton`: conservative topology and skeleton nodes.
- `NodeInstantiation`: concrete `NodeCapsule` generation.
- `PermissionGroundingReport`: sandbox profile grounded by `RepoPolicy`.
- `WorkflowCompiler`: structured validation for node ids, skeleton coverage, commands, paths, network, and acceptance criteria.

Phase D adds:

- `GraphRuntime`: multi-node DAG scheduler layered on the Stage A/C execution chain.
- `GraphRunSpec`: graph nodes, edges, parallelism, retry, and overlooker-mode policy.
- Checkpoint/resume under `phased_graph_data/checkpoints`.
- Parallel read-only execution with single-writer scheduling.
- Retry budget plus repeated-failure livelock guard.
- overlooker-guided retry fork points so failed attempts do not poison the next attempt workspace.

Publish as a private GitHub repository:

```bash
export GITHUB_TOKEN=...
python3 scripts/publish_github_private.py --repo egtc-paw-runtime-stagea
```

Use `--org ORG_NAME` to create the private repository under an organization.

Expected result:

- The worker reaches `WorkerSubmitted` first.
- Runtime collects `diff`, `test`, `log`, `stderr`, and `worker_events` artifacts.
- Validators run before the Overlooker.
- The Overlooker report includes `evidence_ref`.
- Final state becomes `NodeAccepted`.

Runtime data is written under:

```text
/home/dataset-local/data1/egtc_paw_runtime_stageA/runtime_data
```

Runtime data and SWE smoke outputs are excluded from Git by `.gitignore`.
