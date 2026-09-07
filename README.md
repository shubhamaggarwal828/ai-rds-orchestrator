# AWS RDS Blue/Green AI Upgrade Orchestrator & Dashboard

A next-generation, AI-agentic orchestration utility and modern web dashboard for automating zero-downtime major and minor version upgrades of AWS RDS and Aurora PostgreSQL databases using Blue/Green deployments.

Powered by a multi-provider AI reasoning engine (**Local Flash / Ollama / LM Studio**, **Google Gemini 2.5 Flash**, or zero-setup **Deterministic Heuristic Agent**), the engine discovers fleet topology, evaluates parameter risks, simulates Blue/Green staging, monitors replication lag, coordinates safe switchovers, and provides automated Doomsday rollback.

---

## 🚀 Key Features

* 🧠 **AI Agent Copilot**: Inspects database fleet, detects parameter bottlenecks, explains upgrade risks, and orchestrates actions via natural language.
* 🖥️ **Mission Control Web UI**: Beautiful dark-mode glassmorphic interface to discover all RDS instances/Aurora clusters across regions, run pre-flight audits, and track multi-phase Blue/Green upgrades with live terminal streaming.
* 🛡️ **Autonomous Parameter Remediation**: Enforces required logical replication settings (`rds.logical_replication`, `synchronous_commit`, `wal_sender_timeout`), bumps threshold limits (`max_worker_processes`, `max_replication_slots`), and migrates custom preload libraries (`pgaudit`, `pg_cron`, `pg_partman`).
* 🔄 **Application Auto-Scaling Carryover**: Captures and re-attaches Application Auto-Scaling rules and CloudWatch policies that AWS Blue/Green does not migrate natively.
* 🚦 **Safe Cutover Gate & Replica Lag Monitor**: Verifies CloudWatch replica lag is under safe thresholds (<30s) before executing DNS switchovers.
* 🚨 **Doomsday Emergency Rollback**: One-click recovery protocol that clears locking B/G records, renames failed green DBs, and restores original blue database identifiers without namespace collisions.
* 🧪 **Dual Mode Execution**: Instant **Sandbox Simulator** for cost-free local testing and dry-runs, plus **Live AWS Mode** for real production upgrades.

---

## 1. Quick Start

### A. Launch Web UI Dashboard
```bash
# 1. Activate environment and install dependencies
source venv/bin/activate
pip install -r requirements.txt

# 2. Launch the AI Orchestrator UI
python3 run_ui.py
```
Open **`http://localhost:8000`** in your browser.

---

## 2. AI Brain & Model Options

You can select the model directly from the top navigation bar or settings modal:
1. **Deterministic Agent (Local Fast - Zero Setup)**: Fast, deterministic rule-based AI reasoner that works out of the box with zero keys or local servers.
2. **Local Flash / Ollama**: Connect to any local model (`ollama run llama3.2`, `mistral`, `qwen2.5`) via `http://localhost:11434/v1`.
3. **Gemini 2.5 Flash**: Set `GEMINI_API_KEY` in environment or in the UI settings for advanced multimodal and cloud reasoning.

---

## 2. Directory & Code Architecture

```
.
├── main.py                     # Primary upgrade execution script
├── doomsday.py                 # Emergency restoration rollback script
├── generate_summary.py         # JSON report parser and summary markdown compiler
├── test_plan.md                # Test execution matrix & edge-case setup scripts
├── core/                       # Core orchestration component library
│   ├── aws_client.py           # Standardized AWS API communications wrapper
│   ├── detector.py             # Engine, class, and PG family environment discovery
│   ├── param_manager.py        # Safety parameters logic and carryovers manager
│   ├── rebooter.py             # Cluster & instance reboot orchestration
│   ├── reporter.py             # Raw JSON audit log generator
│   ├── snapshotter.py          # Pre-flight data-safety snapshot manager
│   ├── deployer.py             # Blue/Green deployment creation & tracking gateway
│   ├── stager.py               # Auto-scaling and policy migration manager
│   └── switcher.py             # Replication lag monitor & DNS switchover coordinator
└── upgrade_reports/            # Generated raw JSONs and compiled Markdown summaries
```

---

## 3. Modification & Extensibility Guide

Use this guide when updating parameters, changing upgrade checks, or adjusting stager rules:

### A. Modifying Parameter Group Adjustments & Carryovers
All parameter checks, defaults, and override rules are defined inside `core/param_manager.py`.

* **To enforce a new parameter value for all targets**:
  Add it to the `STATIC_ENFORCEMENTS` dictionary inside `ParameterManager`:
  ```python
  STATIC_ENFORCEMENTS = {
      'rds.logical_replication': '1',
      'new_parameter_name': 'desired_value',  # <-- Add here
  }
  ```
* **To increase a threshold check**:
  Add or edit items in the `MINIMUM_THRESHOLDS` dictionary:
  ```python
  MINIMUM_THRESHOLDS = {
      'max_replication_slots': 20,
      'my_custom_limit': 100,  # <-- Add here
  }
  ```
* **To pass a parameter value unmodified from the old PG to the new PG**:
  Add the parameter name to the list `CARRY_OVER_PARAMS`:
  ```python
  CARRY_OVER_PARAMS = [
      'shared_preload_libraries',
      'my_app_specific_parameter',  # <-- Add here
  ]
  ```

### B. Changing Staging Policies (E.g., Migrating Alarm Rules or Security Groups)
AWS Blue/Green does not carry over secondary resources. The restoration logic is located in `core/stager.py`.

* **To add a new resource migration rule (e.g. migrating CloudWatch Alarms)**:
  1. Add a method inside `StagingManager`:
     ```python
     def restore_cloudwatch_alarms(self, source_id, target_id):
         # Fetch alarms associated with source_id
         # Put alarms onto target_id using self.aws.cloudwatch
         pass
     ```
  2. Invoke it from `restore_scaling_policies` in `core/stager.py`:
     ```python
     def restore_scaling_policies(self, state, bg_deployment_id):
         # ... Existing auto-scaling logic ...
         self.restore_cloudwatch_alarms(state['identifier'], green_id)
     ```

### C. Altering Rollback Targets
The fallback parameters are defined inside `doomsday.py` under the `__main__` entrypoint:
```python
if __name__ == "__main__":
    TARGET_DB = "test-aurora-multi"   # Change this to target database identifier
    IS_AURORA_CLUSTER = True          # True for Aurora Cluster, False for standard RDS
    AWS_REGION = "us-east-1"          # Set specific region or None for default profile
```

---

## 4. Setup & Running Instructions

### Prerequisites
* AWS CLI installed and configured with appropriate permissions.
* Python 3.8+.

### Setup Virtual Environment
```bash
python3 -m venv myenv
source myenv/bin/activate
pip install -r requirements.txt
```
*(If boto3 is not yet installed in your system python, run the above within your virtual environment).*

### Execution Flows

1. **Simulate / Run Upgrade Engine**:
   Edit the hardcoded configurations in `main.py` (lines 104-112) or pass arguments to the handler, then execute:
   ```bash
   python3 main.py
   ```

2. **Trigger Summary Reports Compiler**:
   ```bash
   python3 generate_summary.py
   ```
   This will output a visual markdown comparison report in `upgrade_reports/summary_report.md`.

3. **Emergency Restore (Rollback)**:
   Ensure `TARGET_DB` and `IS_AURORA_CLUSTER` are set correctly inside `doomsday.py`, then run:
   ```bash
   python3 doomsday.py
   ```
   *Type `ROLLBACK` when prompted to execute.*
