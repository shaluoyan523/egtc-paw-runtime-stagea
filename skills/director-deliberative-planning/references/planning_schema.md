# Planning Schema

Director output must include these additional `workflow_skeleton` fields.

## linear_requirement_flow

List of ordered records:

```json
{
  "stage_id": "stage-1",
  "order": 1,
  "name": "Understand task and repo constraints",
  "purpose": "Find what must be changed and what must not be touched.",
  "inputs": ["objective", "repo_policy"],
  "outputs": ["touchpoint_map", "constraint_summary"],
  "risk_level": "low|medium|high",
  "acceptance_evidence": ["evidence_ref", "analysis_log"]
}
```

## stage_structure_decisions

One record per linear stage:

```json
{
  "stage_id": "stage-1",
  "candidate_structures": [
    {
      "structure": "single_agent",
      "fit": "low|medium|high",
      "reason": "Why it could work."
    }
  ],
  "selected_structure": "parallel_exploration",
  "selection_reason": "Why this structure is selected for this stage.",
  "anti_signals": ["Signals that would make this structure wrong."],
  "experience_pattern_ids": ["seed-topology-parallel-explore-implement-verify"]
}
```

Allowed structures:

- `single_agent`
- `parallel_exploration`
- `specialist_pool`
- `proposer_aggregator`
- `graph_message_passing`
- `dynamic_routing`
- `hierarchical_subteams`
- `review_gate`
- `tool_planning`
- `research_route`

## research_route_decisions

List of records:

```json
{
  "stage_id": "stage-2",
  "research_needed": true,
  "reason": "The task names a framework or expert route not covered by local evidence.",
  "available_sources": ["experience_candidates", "repo_files", "bundled_docs"],
  "blocked_sources": ["external_web"],
  "planned_queries_or_searches": ["search repo docs for scheduler extension points"],
  "adopted_expert_route": "Use existing local experience pattern or repo-documented route.",
  "fallback_if_research_blocked": "Proceed with conservative exploration and require replan if evidence is insufficient."
}
```

Every specialized or high-uncertainty stage must have a research route decision. If research is unnecessary, still emit a record with `research_needed=false` and a reason.

## per_stage_agent_allocation

List of records:

```json
{
  "stage_id": "stage-1",
  "agent_count": 2,
  "count_reason": "Two independent read-only surfaces need exploration.",
  "agents": [
    {
      "role": "explorer",
      "task": "Map repo touchpoints.",
      "inputs": ["objective", "repo_policy"],
      "outputs": ["touchpoint_map"],
      "ownership_boundary": "Read-only repository analysis.",
      "write_authority": "none",
      "handoff_target": "implement"
    }
  ]
}
```

The sum of `agent_count` values must equal `workflow_skeleton.agent_allocation.total_agents` and the number of final skeleton nodes.

## plan_derivation_trace

List of concise trace strings:

```json
[
  "stage-1 selected parallel_exploration, producing nodes explore-context and explore-tests.",
  "stage-2 selected single_agent because writes are not yet provably independent, producing node implement."
]
```

The trace must connect the linear stages to final node ids.
