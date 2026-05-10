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
