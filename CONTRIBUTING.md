# Contributing to AWS RDS Blue/Green Upgrade Engine

Thank you for contributing! To maintain code quality, robustness, and prevent production database disruptions, please follow the guidelines below when making modifications.

---

## 1. Codebase Philosophy

1. **Safety First**: The primary objective is guarding databases against configuration-related upgrade failures. Warnings should always err on the side of caution.
2. **Modular Architecture**: 
   * Do not make direct Boto3 calls inside orchestrator scripts. Keep all AWS integrations enclosed within [aws_client.py](file:///Users/shubham/Documents/rds-upgrade/core/aws_client.py).
   * Separate lifecycle operations into focused managers under the `core/` library (e.g., `param_manager.py` for parameter evaluation, `stager.py` for resource mapping).
3. **Audit Trail**: Every modification phase must output structured telemetry to the JSON preparation reports so changes remain auditable.

---

## 2. Pull Request & Development Workflow

1. **Branch Naming**: Use clean branch names indicating scope:
   * `feature/add-alarm-migration`
   * `bugfix/fix-instance-pg-family`
2. **Linting & Code Style**:
   * Adhere to PEP 8 standards.
   * Document all public classes and helper functions.
   * Catch and log specific AWS `ClientError` exceptions; avoid raw `except:` catch-alls.

---

## 3. Testing and Verification Guide

Before submitting code, you must verify the changes using the baseline test suite.

### Running Local Validation
* Run the mock unit test suite locally to verify logic changes across the detector, parameter manager, and summary compiler:
  ```bash
  python3 -m unittest test_engine.py -v
  ```
* Make sure your scripts do not introduce syntax or import errors:
  ```bash
  python3 -m py_compile main.py doomsday.py generate_summary.py core/*.py
  ```

### Verifying upgrades on AWS (Non-Prod)
Refer to [test_plan.md](file:///Users/shubham/Documents/rds-upgrade/test_plan.md) to set up test scenarios using the AWS CLI:
1. **Parameter Modifications Audit**:
   * Run an upgrade sequence dry-run (`AUTO_DEPLOY_ENABLED = False`).
   * Run `python3 generate_summary.py`.
   * Verify the diff report in `upgrade_reports/summary_report.md` matches your expected changes.
2. **Auto-Scaling Migration**:
   * Deploy **Scenario 6 (Aurora Auto-Scaling)**.
   * Let the upgrade complete.
   * Verify using AWS Console or CLI that target policies match the source policies.
3. **Emergency Rollback Validation**:
   * Intentionally interrupt an upgrade sequence or execute a dry run switchover.
   * Configure `doomsday.py` with your test database identifier.
   * Run `python3 doomsday.py` and verify the primary identifier shifts back to the original database.
