# 2-Layer ABM-FBS Capability Assessment Plan

## Objective

Reproduce the Bott & Mesmer (2019) paper *"Agent-Based Simulation of Hardware-Intensive Design Teams Using the Function-Behavior-Structure Framework"* (Systems 7(3), 37) using a **2-layer architecture** (no middle management hierarchy), **without MCP tools** — to assess raw code-generation capability.

## Reference Documents

- Paper PDF: [`docs/systems-07-00037-v2.pdf`](/home/tomo/project/001_abm/mcp_test/docs/systems-07-00037-v2.pdf)
- Previous (3-layer, MCP-assisted) implementation: [`abm_fbs_sim/`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/)
- QCD comparison: [`QCD_COMPARISON.md`](/home/tomo/project/001_abm/mcp_test/QCD_COMPARISON.md)
- EKP vs Pure comparison: [`QCD_EKP_VS_PURE.md`](/home/tomo/project/001_abm/mcp_test/QCD_EKP_VS_PURE.md)

## Target Paper Results

| Metric | Software (Agile vs Waterfall) | Launch Vehicle (Agile vs Waterfall) |
|--------|-------------------------------|-----------------------------------|
| **Effort** | **-41.9%** | **-0.8%** |
| **Wall Clock** | **-62.0%** | **-12.4%** |
| **Rework** | **-57.2%** | **+3.3%** |

## Architecture: 2-Layer Model

```
┌─────────────────────────────────────────────────┐
│               SimulationEngine                   │
│  ┌───────────────────────────────────────────┐  │
│  │           Agent Pool (N agents)           │  │
│  │  ┌──────┐ ┌──────┐ ┌──────┐              │  │
│  │  │Agt 1 │ │Agt 2 │ │Agt 3 │  ... Agt N   │  │
│  │  │FBS   │ │FBS   │ │FBS   │              │  │
│  │  └──────┘ └──────┘ └──────┘              │  │
│  └───────────────────────────────────────────┘  │
│  Processes: Waterfall / Agile                    │
│  Coupling: LaunchVehicleAscentModel              │
└─────────────────────────────────────────────────┘
```

- **Layer 1**: `DesignerAgent` — individual designer with FBS Markov model
- **Layer 2**: `SimulationEngine` — manages all agents, runs phases
- **No** `Team`, `MiddleLevelTeam`, `TopLevelTeam` classes

## Implementation Plan (8 Steps)

### Step 1: Project Setup
**Files:** [`pyproject.toml`](/home/tomo/project/001_abm/mcp_test/pyproject.toml), `.python-version`

- Create project directory `abm_fbs_sim_2layer/` inside `/home/tomo/project/001_abm/mcp_test/`
- Set up `pyproject.toml` with `numpy>=2.5.0` dependency, Python 3.13
- Verify `uv` environment works

**Success criteria:** `uv run python -c "import numpy; print(numpy.__version__)"` succeeds

---

### Step 2: FBS Markov Model
**File:** [`fbs_model.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/fbs_model.py)

- Implement 5 FBS states: `R(0)`, `F(1)`, `Be(2)`, `S(3)`, `D(4)`
- Default 5×5 transition matrix matching paper calibration
- Methods: `attempt_transition()`, `attempt_transition_to_target()`, `reset()`, `set_state()`
- Use `numpy.random.Generator` for reproducibility
- `create_varied_performer()` for agent diversity

**CRITICAL:** The transition logic must be "if draw > prob → advance" (NOT "while loop until target"). See [`QCD_EKP_VS_PURE.md`](/home/tomo/project/001_abm/mcp_test/QCD_EKP_VS_PURE.md) line 58-66 for the common bug.

**Success criteria:** Unit test verifying:
- Default matrix rows sum to 1.0
- `attempt_transition()` advances probabilistically
- `reset()` returns to state R

---

### Step 3: DesignerAgent
**File:** [`agent.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/agent.py) (simplified version in simulation.py lines 43-103)

- Attributes: `agent_id`, `role` (lead/designer), `fbs` model, `current_state`
- Tracking: `effort_hours`, `rework_hours`, `completed`, `functions_completed`
- Methods: `step(target, is_rework)`, `reset()`, `is_at(state)`, `has_completed()`
- Each time step = 8 hours (1 working day)
- Rework flag marks effort as rework

**2-layer simplification:** No team assignment, no `AgentFactory` — agents created directly via function.

**Success criteria:** Agent can step through FBS states and eventually reach completion.

---

### Step 4: SimulationEngine & Software Simulation (Waterfall)
**File:** [`simulation.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/simulation.py) + [`run_sw.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/run_sw.py)

- `SimulationEngine` manages list of `DesignerAgent`s
- `run_waterfall_phase(from_state, to_state, only_bottom)` — advances all agents
- `collect_metrics()` → `SimMetrics` (wall_clock_hours, effort_hours, rework_hours)

**Waterfall phases (Software):**
1. SRR: All agents R → F (formulation)
2. SFR: All agents F → Be (specification)
3. PDR: Bottom agents (role=designer) only Be → S (synthesis)
4. CDR: Bottom agents only S → D (documentation, with rework)

**Agent creation for Software:**
- 1 top lead + 4 module leads (role=lead)
- 12 teams × 8 designers = 96 (role=designer)
- Total: ~101 agents, each with unique FBS seed

**Success criteria:** `run_waterfall_software()` produces metrics without errors.

---

### Step 5: Agile Software Simulation
**File:** [`run_sw.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/run_sw.py) (agile section lines 54-157)

- Phase 1: Leads R → F (planning / backlog)
- Phase 2: Designers iterate through 10 functions, each sprint advancing R→F→Be→S→D
- After each sprint completion, reset designers for next function
- Leads remain at F throughout (backlog owners)

**Success criteria:** `run_agile_software()` produces metrics. Compare:
- Agile effort < Waterfall effort (should show ~42% reduction directionally)

---

### Step 6: Coupling Model & Launch Vehicle Simulation
**Files:** [`coupling.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/coupling.py), [`run_lv.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/run_lv.py)

- 9 design variables (Stage 1: 4, Stage 2: 4, Payload: 1)
- `LaunchVehicleAscentModel` — simplified 3-DoF feasibility check:
  1. TWR > 1.2 at liftoff
  2. Stage 1 delta-V >= 4000 m/s
  3. Stage 2 delta-V >= 3000 m/s
  4. Structural mass fraction < 15%
  5. Payload fraction 0.1%–5%
  6. Stage 2 diameter <= Stage 1 diameter
- In CDR phase, invalid design → Type I reformulation (S → Be, rework +8h)

**Agent creation for LV:**
- 1 top lead + 3 stage leads (role=lead)
- 9 subsystems × 8 designers = 72 (role=designer)
- Total: ~76 agents

**Success criteria:** Agile vs Waterfall for LV shows smaller improvement (directionally ~0% effort, ~12% time, +3% rework)

---

### Step 7: Monte Carlo Runner
**File:** [`run_sw.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/run_sw.py) (Monte Carlo section lines 200-256) + [`run_lv.py`](/home/tomo/project/001_abm/mcp_test/abm_fbs_sim/run_lv.py) (Monte Carlo section lines 160-216)

- Run N iterations (start with 10 for quick validation, scale to 100+)
- Each iteration uses incrementing seed for independence
- Collect statistics: mean, std for wall_clock_hours, effort_hours, rework_hours
- Output format matching paper Table 2 & 3 format

**Success criteria:** Can produce comparison table with % differences.

---

### Step 8: Main Entry Point & Validation
**File:** [`main.py`](/home/tomo/project/001_abm/mcp_test/main.py)

- CLI with `--sim-type`, `--iterations`, `--quick`, `--json` flags
- Print formatted tables comparing Waterfall vs Agile
- Display paper reference values alongside simulation results
- Cross-domain comparison summary

**Success criteria:** `uv run python main.py --sim-type all --iterations 10 --quick` runs end-to-end and produces valid-looking comparison tables.

---

## Validation Checkpoints

| Checkpoint | What to Verify | Reference |
|------------|---------------|-----------|
| **C1** | FBS transition matrix rows sum to 1.0 | `fbs_model.py` |
| **C2** | Single agent completes R→F→Be→S→D sequence | `agent.py` test |
| **C3** | Waterfall produces larger effort than Agile for software | Directional sanity |
| **C4** | Agile rework is lower than Waterfall for software | Paper: -57.2% |
| **C5** | LV coupling causes more rework in Agile than waterfall | Paper: +3.3% |
| **C6** | LV Agile time saving is modest vs software | Paper: -12.4% vs -62.0% |
| **C7** | Monte Carlo produces stable statistics | N=10 quick vs N=100 compare |
| **C8** | Absolute values are within reasonable range of paper | Order-of-magnitude check |

## QCD Assessment Criteria (Self-Evaluation)

After implementation, self-assess against these dimensions:

| Dimension | Assessment Question |
|-----------|-------------------|
| **Quality** | Does the simulation produce directionally correct results without code review assistance? |
| **Speed** | How long does each step take without MCP tools (only write_to_file/apply_diff)? |
| **Correctness** | How many iterations/fixes needed before the first successful run? |
| **Paper Fidelity** | Do the % differences match paper values within ±10 percentage points? |
| **Completeness** | Are all 8 steps implemented? Any skipped or placeholder modules? |

## Comparison Matrix (from QCD_EKP_VS_PURE.md)

| Approach | Model | Method | Quality | Time | Result |
|----------|-------|--------|---------|------|--------|
| **Pure DeepSeek (v1)** | DeepSeek V4 Flash | Direct gen | 90/100 | ~19 min | ✅ Working |
| **ekp-forge+Ollama (v2)** | qwen2.5-coder:7b | Aider MCP | 40/100 | ~25 min | ⚠️ Buggy |
| **This attempt (v3)** | DeepSeek V4 Flash | **No MCP, 2-layer** | TBD | TBD | 🎯 Target |

## Key Lessons from Previous Attempts

1. **FBS transition logic**: Use `if draw > prob → target_state`, NOT `while current != target`
2. **`reset()` must go to state R**, not random state
3. **NumPy integration**: Be consistent — either use numpy everywhere or plain Python
4. **Module interfaces**: Ensure `Agent.step()` signature matches what `SimulationEngine.run_waterfall_phase()` expects
5. **Rework tracking**: Separate `effort_hours` and `rework_hours` correctly
