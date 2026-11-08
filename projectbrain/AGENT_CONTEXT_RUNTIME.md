# Agent Context Runtime

## The product thesis

ProjectBrain should evolve beyond a code-search or static-analysis tool. Its useful position is a **model-agnostic context compiler and change-verification runtime for coding agents**.

The product asks a different question from ordinary code search:

> For this task and this exact repository revision, what is the smallest evidence-backed working set an agent needs to inspect before it can safely plan and make a change?

That working set is a task evidence packet: a revision-pinned collection of relevant symbols, source spans, dependency paths, required tests, owners, policy boundaries, and explicit unknowns.

```text
Task
  -> source / git / builds / tests / ownership / policies
  -> revision-aware organization graph
  -> minimal evidence frontier for the task
  -> LLM agent reads only necessary source and rules
  -> pre-edit and pre-PR verification
  -> review, test, merge, revert, and incident outcomes improve confidence
```

## Why agents need it

Code generation is rarely the expensive part of a serious coding task. The expensive loop is uncertainty reduction:

```text
Task -> search -> read candidate files -> infer architecture -> plan -> edit
     -> discover a missed dependency -> re-read -> revise -> test -> explain
```

An agent context runtime reduces four costs:

| Cost | Product responsibility |
| --- | --- |
| Context cost | Select the smallest sufficient files, symbols, source spans, and rules. |
| Tool and latency cost | Replace repeated grep/read loops with structured, evidence-backed navigation. |
| Reasoning cost | Separate facts, import-resolved links, candidates, and unknowns. |
| Rework cost | Detect incomplete plans, missing tests, unsupported assumptions, and risky dependencies before a PR. |

Modern agent platforms already make this a real concern. Claude Code documents that file reads, outputs, instructions, tools, and conversation history all fill context; it compacts as sessions grow. Its cost guidance explicitly recommends typed code intelligence and preprocessing to reduce unnecessary file reads and tokens. OpenAI documents compaction, prompt caching, tool calling, and multi-agent orchestration. Gemini Code Assist uses codebase indexing and private-repository retrieval to inject relevant code into prompts.

Sources:

- [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Claude Code: how it works](https://code.claude.com/docs/en/how-claude-code-works)
- [Claude Code: cost management](https://code.claude.com/docs/en/costs)
- [Gemini local codebase awareness](https://docs.cloud.google.com/gemini/docs/codeassist/configure-local-codebase-awareness?hl=en)
- [Gemini Code Assist code customization](https://docs.cloud.google.com/gemini/docs/codeassist/code-customization-overview?authuser=5)

## The product unit: an evidence packet

The desired agent interface is not a broad ranked file list. It is a compact packet like this:

```json
{
  "revision": "commit SHA",
  "task": "Add coupon support to checkout",
  "entry_points": ["route:POST:/checkout"],
  "required_read_order": [
    {
      "file": "routes/checkout.py",
      "symbols": ["checkout"],
      "why": "request entry point"
    },
    {
      "file": "services/discount.py",
      "symbols": ["apply_discount"],
      "why": "import-resolved call"
    }
  ],
  "change_closure": {
    "likely_edit_files": ["..."],
    "required_tests": ["..."],
    "owners": ["..."],
    "policy_boundaries": ["payments", "PII"]
  },
  "evidence": [{"commit": "...", "file": "...", "line": 42}],
  "unknowns": ["Coupon persistence path is dynamically resolved"],
  "stop_conditions": ["Run checkout integration tests"]
}
```

The agent should use it before planning, before editing, and before creating a PR. The packet needs exact source provenance and an explicit distinction between proven facts, likely links, and unknowns.

## What ProjectBrain already has

| Existing capability | Product role |
| --- | --- |
| `query` and graph queries | Task scoping and minimal-context selection |
| `get_context` and `compress_context` | Source-backed working-memory compiler |
| `find_relationship` and `trace_flow` | Cross-boundary execution-path reconstruction |
| `verify_claim` | Evidence adjudicator for agent statements |
| `verify_plan` | Pre-edit completeness gate |
| `impact` | Post-edit blast-radius analysis |
| Guided sessions | Stateful exploration controller |
| `co_change_analysis` | Organizational knowledge beyond imports |

The most important existing design decision is confidence-aware output. A coding agent needs to know whether a link is structural, import-resolved, merely candidate-based, unavailable because parsing is partial, or unknown. A high-risk agent policy can then require more evidence, a test, or approval before crossing an uncertain boundary.

## What is not a moat

None of these is sufficient on its own:

- Tree-sitter parsing
- AST or symbol graphs
- vector search over source code
- MCP endpoints
- generic codebase summaries
- ranked files
- visual dependency graphs

Claude already recommends typed symbol navigation; Gemini Enterprise already indexes private repositories, finds similar code, and inserts it into prompts. Big tech can reproduce a static graph and retrieval layer.

## The moat: private change intelligence

The defensible product is a **revision-aware, evidence-backed model of how a particular company safely changes software**.

### 1. Compiler-grade semantic truth

Use stable symbol IDs, real definition/reference resolution, type-aware relationships, build-system awareness, and provenance tied to repository and commit. Start with the language stack of a target customer group; do not chase every language at once.

### 2. Change closure

Given a behavior change, calculate the smallest set of code, contracts, tests, owners, and policies that must be considered before an agent may call the change complete. This is more valuable than simple graph traversal.

### 3. Longitudinal organization knowledge

Learn from private history without training on customer source code:

- files and tests that change together
- review feedback and rejected PRs
- reverts and incidents
- flaky or required tests
- high-risk paths and code ownership
- policies that were triggered in prior changes

The result is a tenant-specific operational map that gets better as the company uses it.

### 4. Outcome feedback

Record task -> evidence packet -> plan -> edit -> test -> review -> merge/revert. Evaluate whether the packet predicted the correct files, tests, risk, and review issues. Use results to calibrate confidence, ranking, and policy.

### 5. Policy at the moment of action

The runtime becomes enterprise infrastructure when it can enforce statements such as:

- A payment flow change requires ledger invariants and Payments owner review.
- A PII-field change requires data-classification evidence.
- An agent cannot assert a relationship supported only by candidate edges.
- A public API change requires downstream-consumer analysis.
- A model-serving change requires an evaluation report before merge.

## Why regulated fintechs would buy

Fintechs pay for controlled, auditable AI-assisted engineering. The valuable output is an immutable change record:

- task, human, and agent identity
- repository and exact revision
- source evidence consulted
- agent claims and confidence
- code considered, edited, and omitted
- policies evaluated
- tests executed and results
- owner approvals
- final PR and deployment reference

The promise is not that the agent writes a controller faster. It is that it cannot silently make a payment, ledger, identity, or privacy change without demonstrating the required evidence and controls.

## Why AI labs and agent-heavy companies would buy

Multi-agent systems multiply duplicated exploration. Each agent may start with a new context, reread the same files, repeat tool calls, and pursue divergent assumptions. Claude Code documents that agent-team usage scales with the number and duration of separate agent contexts.

ProjectBrain can lower that cost by:

- sharing revision-pinned evidence packets across agents, retries, and branches
- assigning agents to non-overlapping graph regions
- caching semantic summaries and dependency facts
- routing well-scoped work to cheaper models
- escalating only ambiguous or high-risk tasks to expensive models
- measuring cost per accepted PR, not token count alone

AI labs will only buy rather than build if the product is model-agnostic, works with their private infrastructure, produces high-quality evaluation telemetry, and reaches production faster than an internal implementation.

## Product sequence

1. **Agent Context Runtime** — revision-pinned evidence packets, small response budgets, source citations, explicit unknowns, and adapters for OpenAI, Claude, Gemini, and internal agents.
2. **Change Closure Engine** — `before_plan`, `before_edit`, and `before_pr` checks for files, tests, contracts, owners, and risk boundaries.
3. **Compiler-backed language depth** — language-server and build metadata for one valuable initial stack.
4. **Enterprise connectors** — source control, CI, `CODEOWNERS`, issues, service catalog, API specs, deploys, and incidents.
5. **Policy and audit** — permission mirroring, SSO/RBAC, tenant isolation, retention, immutable audit records, and self-hosted/VPC deployment.
6. **Outcome learning** — evaluate every prediction against tests, review, merge, revert, and incident outcomes.

## Proof before enterprise scale-up

Run paired agent evaluations on real repository tasks. Hold model, task, revision, permissions, tool budget, and time budget constant; compare a normal agent with an agent that receives ProjectBrain evidence packets.

| Metric | What it proves |
| --- | --- |
| Context tokens before first edit | Exploration-cost reduction |
| Tool calls and wall-clock time before first edit | Navigation efficiency |
| Files/tests omitted from final plan | Change-completeness quality |
| Final test pass rate | Implementation correctness |
| Reviewer-requested follow-up changes | Human-trust proxy |
| Unsupported agent claims | Grounding quality |
| Cost per accepted PR | Executive value |

Do not claim token savings or safety before these evaluations establish them. A useful product must improve accepted-task cost, plan completeness, or review burden without harming correctness.

## Positioning

> ProjectBrain compiles verified, minimal working context for coding agents and enforces evidence-backed change completeness.

That is more durable than “AI code search,” more useful than a generic code graph, and more valuable than an isolated MCP tool.
