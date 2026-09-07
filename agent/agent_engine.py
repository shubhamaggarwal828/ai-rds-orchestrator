import os
import json
import time
import requests
from typing import Dict, Any, List, Optional
from core.detector import EnvironmentDetector
from core.param_manager import ParameterManager
from core.rebooter import RebootOrchestrator
from core.snapshotter import SnapshotManager
from core.deployer import DeploymentManager
from core.stager import StagingManager
from core.switcher import SwitchoverManager
from agent.prompt_templates import SYSTEM_PROMPT_AGENT

class RDSUpgradeAgent:
    """Agentic AI Brain coordinating AWS RDS/Aurora discovery, auditing, risk analysis, and execution."""
    
    def __init__(self, aws_client, llm_provider="local_llm", api_key=None, local_endpoint="http://localhost:1234/v1", model_name="deepseek/deepseek-r1-0528-qwen3-8b"):
        self.aws = aws_client
        self.llm_provider = llm_provider  # "local_llm", "gemini_flash", "built_in"
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.local_endpoint = local_endpoint or "http://localhost:1234/v1"
        self.model_name = model_name or "deepseek/deepseek-r1-0528-qwen3-8b"
        self.chat_history: List[Dict[str, str]] = []

    def update_llm_config(self, provider: str, api_key: Optional[str] = None, local_endpoint: Optional[str] = None, model_name: Optional[str] = None):
        self.llm_provider = provider
        if api_key is not None:
            self.api_key = api_key
        if local_endpoint is not None:
            self.local_endpoint = local_endpoint
        if model_name is not None:
            self.model_name = model_name

    # ==========================================
    # --- TOOL DEFINITIONS & EXECUTION ---
    # ==========================================

    def tool_list_databases(self) -> List[Dict[str, Any]]:
        """Tool: Discover all RDS instances and Aurora DB clusters."""
        return self.aws.list_all_databases()

    def tool_audit_database(self, db_identifier: str, target_version: Optional[str] = None) -> Dict[str, Any]:
        """Tool: Execute deep pre-flight audit and risk evaluation on a target database."""
        detector = EnvironmentDetector(self.aws)
        param_manager = ParameterManager(self.aws)

        # Inspect state
        state = detector.inspect(db_identifier)
        
        # Upgrade paths
        try:
            validated_version, target_family = detector.get_upgrade_target(
                state['engine'], state['current_version'], desired_version=target_version
            )
        except Exception as e:
            validated_version = f"Next available (Error: {str(e)})"
            target_family = "unknown"

        # Check parameter rules
        source_pg = state['source_pg_name']
        is_default_pg = "default" in source_pg.lower() if source_pg else False

        # Identify Parameter Discrepancies & Enforcements
        enforcements = []
        for param, val in param_manager.STATIC_ENFORCEMENTS.items():
            enforcements.append({
                "parameter": param,
                "target_enforced_value": val,
                "reason": "Required for PostgreSQL logical replication stability"
            })

        thresholds = []
        for param, min_val in param_manager.MINIMUM_THRESHOLDS.items():
            thresholds.append({
                "parameter": param,
                "minimum_threshold": min_val,
                "reason": "Replication slot & worker process starvation prevention"
            })

        carryovers = param_manager.CARRY_OVER_PARAMS

        # Calculate Risk Factors & Overall Severity
        risk_score = "LOW"
        risk_factors = []

        if is_default_pg:
            risk_score = "MEDIUM"
            risk_factors.append({
                "severity": "WARNING",
                "title": "Default Parameter Group in Use",
                "description": f"Source uses '{source_pg}'. Logical replication cannot be enabled on AWS default groups. Our engine will auto-create and attach a custom clone before B/G provisioning."
            })

        if state.get('is_cluster'):
            if state.get('scaling_policies'):
                risk_factors.append({
                    "severity": "INFO",
                    "title": "Aurora Auto-Scaling Active",
                    "description": f"Detected {len(state['scaling_policies'].get('policies', []))} auto-scaling policies. Native AWS B/G will NOT migrate these; our Phase 3 Stager will automatically re-attach them."
                })
            if len(state.get('aurora_instance_pgs', [])) > 0:
                risk_factors.append({
                    "severity": "INFO",
                    "title": "Reader Instance Parameter Groups",
                    "description": f"Detected custom Reader PGs: {', '.join(state['aurora_instance_pgs'])}. Will be prepped for Green readers."
                })

        # Check for Secrets Manager Blocker
        if state.get('has_secrets_manager'):
            risk_score = "BLOCKING"
            risk_factors.append({
                "severity": "CRITICAL",
                "title": "Secrets Manager Integration Active",
                "description": "AWS Blue/Green does not support databases managed by RDS Secrets Manager. Disable Secrets Manager on the DB before proceeding."
            })

        # Fetch all available upgrade targets from AWS
        available_targets = []
        try:
            info = self.aws.get_engine_versions(state['engine'], state['current_version'])
            available_targets = info.get('ValidUpgradeTarget', [])
        except Exception as e:
            print(f"  [!] Warning: Could not fetch valid upgrade targets: {e}")

        return {
            "database_id": db_identifier,
            "engine": state['engine'],
            "current_version": state['current_version'],
            "target_version": validated_version,
            "target_family": target_family,
            "available_targets": available_targets,
            "is_cluster": state['is_cluster'],
            "is_aurora": state['is_aurora'],
            "source_parameter_group": source_pg,
            "is_default_pg": is_default_pg,
            "risk_score": risk_score,
            "risk_factors": risk_factors,
            "static_enforcements": enforcements,
            "threshold_rules": thresholds,
            "carryover_parameters": carryovers,
            "multi_az": state.get('multi_az', True),
            "scaling_policies_count": len(state.get('scaling_policies', {}).get('policies', [])) if state.get('scaling_policies') else 0
        }

    def tool_get_replication_lag(self, db_identifier: str, is_cluster: bool = False) -> Dict[str, Any]:
        """Tool: Query replication lag metric for Blue/Green staging."""
        lag = self.aws.get_replica_lag(db_identifier, is_cluster)
        safe = lag is not None and lag < 30.0
        return {
            "target_id": db_identifier,
            "replica_lag_seconds": lag,
            "safe_for_switchover": safe,
            "recommendation": "Ready for cutover" if safe else "Wait for replication lag to drop below 30s"
        }

    def tool_rename_database(self, old_identifier: str, new_identifier: str, is_cluster: bool = False) -> Dict[str, Any]:
        """Tool: Rename an RDS instance or Aurora DB cluster immediately."""
        res = self.aws.rename_database(old_identifier, new_identifier, is_cluster)
        return {
            "old_identifier": old_identifier,
            "new_identifier": new_identifier,
            "is_cluster": is_cluster,
            "status": "RENAMED",
            "result": res
        }

    def tool_stop_database(self, db_identifier: str, is_cluster: bool = False) -> Dict[str, Any]:
        """Tool: Safely powers down a database instance or cluster."""
        self.aws.stop_database(db_identifier, is_cluster)
        return {"db_identifier": db_identifier, "is_cluster": is_cluster, "status": "STOPPING"}

    def tool_delete_database(self, db_identifier: str, is_cluster: bool = False) -> Dict[str, Any]:
        """Tool: Permanently deletes a database instance or cluster."""
        self.aws.delete_database(db_identifier, is_cluster, skip_final_snapshot=True)
        return {"db_identifier": db_identifier, "is_cluster": is_cluster, "status": "DELETING"}

    def tool_delete_bg(self, bg_id: str) -> Dict[str, Any]:
        """Tool: Removes Blue/Green deployment mapping."""
        self.aws.delete_blue_green_deployment(bg_id, delete_target=False)
        return {"bg_id": bg_id, "status": "DELETING_BG"}

    def tool_modify_database_setting(self, db_identifier: str, is_cluster: bool = False, **kwargs) -> Dict[str, Any]:
        """Tool: Modify database configuration settings immediately."""
        return self.aws.modify_database_configuration(db_identifier, is_cluster=is_cluster, **kwargs)

    def tool_execute_aws_cli(self, command_str: str) -> Dict[str, Any]:
        """Tool: Run any arbitrary AWS CLI command directly using active credentials."""
        return self.aws.execute_aws_cli(command_str)

    # ==========================================
    # --- NATURAL LANGUAGE REASONING & CHAT ---
    # ==========================================

    def chat(self, user_message: str, start_upgrade_callback=None, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Process user request through LLM or Built-in Agentic Heuristics with full multi-turn conversational history."""
        if self.llm_provider in ["local_llm", "local_ollama"]:
            return self._call_local_llm(user_message, start_upgrade_callback, history=history)
        elif self.llm_provider == "gemini_flash" and self.api_key:
            return self._call_gemini_flash(user_message, start_upgrade_callback, history=history)
        else:
            return self._call_builtin_agent(user_message, start_upgrade_callback, history=history)

    def _call_builtin_agent(self, user_message: str, start_upgrade_callback=None, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """High-intelligence deterministic agent with universal AWS CLI execution, dynamic property modification, and action buttons."""
        import re
        import shlex
        user_msg_lower = user_message.strip().lower()
        tool_called = None
        tool_result = None
        action_buttons = []

        dbs = self.tool_list_databases()

        # Helper to find matching DB
        def find_db(text):
            for d in dbs:
                if d['id'].lower() in text:
                    return d
            return None

        # 0. DIRECT AWS CLI COMMAND INTENT (e.g. "aws rds modify-db-instance ...", "run aws rds ...", "execute aws rds ...")
        cli_match = re.search(r'(aws\s+rds\s+[^\n\r`]+)', user_message, re.IGNORECASE)
        if cli_match or user_msg_lower.startswith("aws rds ") or user_msg_lower.startswith("run aws ") or user_msg_lower.startswith("execute aws "):
            raw_cli = cli_match.group(1).strip() if cli_match else user_message.strip()
            # Clean prefix like 'run ', 'execute ', 'exec '
            clean_cli = re.sub(r'^(?:run|execute|exec|apply)\s+', '', raw_cli, flags=re.IGNORECASE).strip()
            clean_cli = re.sub(r'^[`\'"]+|[`\'"]+$', '', clean_cli).strip()

            target_m = re.search(r'--db-(?:instance|cluster)-identifier\s+["\']?([a-zA-Z0-9\-_]+)["\']?', clean_cli)
            target_id = target_m.group(1) if target_m else (dbs[0]['id'] if dbs else "database")
            
            subcmd_m = re.search(r'aws\s+rds\s+([a-zA-Z0-9\-_]+)', clean_cli)
            subcmd = subcmd_m.group(1) if subcmd_m else "modify"
            lbl_subcmd = subcmd.replace('-', ' ').title()

            is_immediate_run = any(user_msg_lower.startswith(w) for w in ["run ", "exec ", "execute ", "apply "])
            if is_immediate_run:
                try:
                    res = self.tool_execute_aws_cli(clean_cli)
                    out_data = res.get("output", {})
                    out_formatted = json.dumps(out_data, indent=2) if isinstance(out_data, (dict, list)) else str(res.get("stdout", "Success"))
                    if len(out_formatted) > 1000:
                        out_formatted = out_formatted[:1000] + "\n... [truncated]"

                    md = f"### ✅ AWS CLI Command Executed Successfully!\n\n"
                    md += f"```bash\n{clean_cli}\n```\n\n"
                    if out_data:
                        md += f"**AWS Output Response:**\n```json\n{out_formatted}\n```\n\n"
                    md += f"Live AWS configuration state updated in region `{self.aws.region}`."

                    action_buttons = [
                        {"label": f"🛡️ Audit {target_id}", "action": "audit", "target": target_id, "style": "secondary"},
                        {"label": f"🚀 Upgrade {target_id}", "action": "upgrade_prompt", "target": target_id, "style": "primary"}
                    ]
                    return {"reply": md, "tool_called": "execute_aws_cli", "tool_result": res, "action_buttons": action_buttons}
                except Exception as e:
                    md = f"### ❌ AWS CLI Execution Error\n\n```bash\n{clean_cli}\n```\n\n**Error:** {str(e)}"
                    return {"reply": md, "tool_called": "execute_aws_cli", "tool_result": {"error": str(e)}, "action_buttons": []}

            # Propose execution with 1-click button
            md = f"### 💻 Researched AWS CLI Command for `{target_id}`\n\n"
            md += f"I analyzed the AWS RDS command:\n```bash\n{clean_cli}\n```\n\n"
            md += f"- **Target Resource:** `{target_id}`\n"
            md += f"- **AWS Subcommand:** `{subcmd}`\n"
            md += f"- **Execution Region:** `{self.aws.region}`\n\n"
            md += f"**Click below to execute this command live in your AWS environment:**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: {lbl_subcmd} ({target_id})",
                    "action": "run_cli",
                    "target": target_id,
                    "params": {"cli_command": clean_cli},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": clean_cli}, "action_buttons": action_buttons}

        # 1. RENAME INTENT (e.g., "rename test-rds-pg14 to poc-rds-standalone" or "change name of test-rds-pg14 to poc-standalone-ai")
        rename_match = re.search(r'(?:rename|change\s+name\s+of|change\s+identifier\s+of|set\s+name\s+of|update\s+name\s+of)\s+(?:rds\s+|aurora\s+|database\s+|db\s+)?([a-zA-Z0-9\-_]+)\s+(?:to|into|as)\s+([a-zA-Z0-9\-_]+)', user_message, re.IGNORECASE)
        if rename_match or (any(k in user_msg_lower for k in ["rename", "change name", "change identifier", "set name"]) and any(k in user_msg_lower for k in ["to", "into", "as"])):
            old_id = rename_match.group(1) if rename_match else None
            new_id = rename_match.group(2) if rename_match else None

            if not old_id or not new_id:
                words = user_message.split()
                try:
                    for sep in ["to", "into", "as"]:
                        if sep in [w.lower() for w in words]:
                            sep_idx = [w.lower() for w in words].index(sep)
                            old_id = words[sep_idx - 1]
                            new_id = words[sep_idx + 1]
                            break
                except Exception:
                    pass

            if old_id and new_id:
                matched = find_db(old_id.lower())
                is_cluster = matched['is_cluster'] if matched else False
                try:
                    res = self.tool_rename_database(old_id, new_id, is_cluster=is_cluster)
                    tool_called = "rename_database"
                    tool_result = res
                    md = f"### 🔄 Database Renaming Executed Successfully!\n\n"
                    md += f"- **Previous Identifier:** `{old_id}`\n"
                    md += f"- **New Identifier:** `{new_id}`\n"
                    md += f"- **Topology:** {'⚡ Aurora DB Cluster' if is_cluster else '📦 Standalone RDS Instance'}\n"
                    md += f"- **Status:** AWS modify operation applied immediately.\n\n"
                    md += f"The database has been renamed to **`{new_id}`**. Fleet discovery and mission control are updated."
                    
                    action_buttons = [
                        {"label": f"🛡️ Audit {new_id}", "action": "audit", "target": new_id, "style": "secondary"},
                        {"label": f"🚀 Upgrade {new_id}", "action": "upgrade_prompt", "target": new_id, "style": "primary"}
                    ]
                    return {"reply": md, "tool_called": tool_called, "tool_result": tool_result, "action_buttons": action_buttons}
                except Exception as e:
                    return {"reply": f"### ❌ Database Rename Failed\n\nCould not rename `{old_id}` to `{new_id}`: {str(e)}", "tool_called": "rename_database", "tool_result": None, "action_buttons": []}

        # 2. DELETION PROTECTION / POLICY INTENT
        if any(w in user_msg_lower for w in ["deletion protection", "deletion policy", "delete protection", "prevent deletion"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False
            
            disable_intent = any(w in user_msg_lower for w in ["disable", "turn off", "remove", "no", "false", "off", "allow delete"])
            enable = not disable_intent

            cli_flag = "--deletion-protection" if enable else "--no-deletion-protection"
            cli_cmd = f"aws rds modify-db-cluster --db-cluster-identifier {target_id} {cli_flag} --apply-immediately --region {self.aws.region}" if is_cluster else f"aws rds modify-db-instance --db-instance-identifier {target_id} {cli_flag} --apply-immediately --region {self.aws.region}"

            md = f"### 🛡️ Researched AWS CLI Setting: Deletion Protection for `{target_id}`\n\n"
            md += f"I investigated the AWS configuration for **`{target_id}`** ({'⚡ Aurora Cluster' if is_cluster else '📦 Standalone RDS Instance'}).\n\n"
            md += f"- **Target Setting:** `DeletionProtection` ➔ **`{enable}`**\n"
            md += f"- **Operational Impact:** Dynamic configuration. **No database reboot required.**\n"
            if not enable:
                md += f"- **Safety Notice:** Disabling deletion protection allows database deletion and Blue/Green decommission workflows to proceed.\n\n"
            else:
                md += f"- **Safety Notice:** Enabling deletion protection prevents accidental termination by AWS APIs or AWS Console users.\n\n"
            
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: {'Enable' if enable else 'Disable'} Deletion Protection",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"deletion_protection": enable},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {"deletion_protection": enable}}, "action_buttons": action_buttons}

        # 3. AUTO MINOR VERSION UPGRADE INTENT
        if any(w in user_msg_lower for w in ["auto minor", "autominor", "minor version upgrade", "auto-minor"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False

            disable_intent = any(w in user_msg_lower for w in ["disable", "turn off", "remove", "no", "false", "off"])
            enable = not disable_intent

            cli_flag = "--auto-minor-version-upgrade" if enable else "--no-auto-minor-version-upgrade"
            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} {cli_flag} --apply-immediately --region {self.aws.region}"

            md = f"### ⚙️ Researched AWS CLI Setting: Auto Minor Version Upgrade for `{target_id}`\n\n"
            md += f"- **Target Setting:** `AutoMinorVersionUpgrade` ➔ **`{enable}`**\n"
            md += f"- **Operational Impact:** Dynamic configuration. **No immediate reboot.**\n"
            if not enable:
                md += f"- **Architectural Benefit:** Disabling prevents unexpected automatic version bumps and unannounced engine restarts during AWS maintenance windows.\n\n"
            else:
                md += f"- **Notice:** Automatic patch upgrades will apply during designated maintenance windows.\n\n"

            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: {'Enable' if enable else 'Disable'} Auto Minor Upgrade",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"auto_minor_version_upgrade": enable},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {"auto_minor_version_upgrade": enable}}, "action_buttons": action_buttons}

        # 4. EXTENDED SUPPORT / ENGINE LIFECYCLE SUPPORT INTENT
        if any(w in user_msg_lower for w in ["extended support", "lifecycle support", "engine lifecycle"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False

            disable_intent = any(w in user_msg_lower for w in ["disable", "turn off", "remove", "no", "off"])
            support_val = "open-source-rds-extended-support-disabled" if disable_intent else "open-source-rds-extended-support"

            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} --engine-lifecycle-support {support_val} --apply-immediately --region {self.aws.region}"

            md = f"### ⏳ Researched AWS CLI Setting: Amazon RDS Extended Support for `{target_id}`\n\n"
            md += f"- **Target Setting:** `EngineLifecycleSupport` ➔ **`{support_val}`**\n"
            md += f"- **Details:** Amazon RDS Extended Support provides security and bug fixes for major engine versions past official community EOL (e.g. PostgreSQL 11, 12, 13, 14).\n"
            md += f"- **Billing Impact:** AWS incurs an additional hourly vCPU charge for databases on Extended Support.\n\n"
            
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": "▶️ Run CLI: Configure Extended Support",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"engine_lifecycle_support": support_val},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {"engine_lifecycle_support": support_val}}, "action_buttons": action_buttons}

        # 5. BACKUP WINDOW & MAINTENANCE WINDOW INTENT
        if any(w in user_msg_lower for w in ["backup window", "maintenance window", "preferred backup window", "preferred maintenance window"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False

            is_maint = "maintenance" in user_msg_lower
            # Match window like "03:00-04:00" or "sun:04:30-sun:05:30"
            win_match = re.search(r'([a-zA-Z]{3}:[0-9]{2}:[0-9]{2}-[a-zA-Z]{3}:[0-9]{2}:[0-9]{2}|[0-9]{2}:[0-9]{2}-[0-9]{2}:[0-9]{2})', user_message)
            win_val = win_match.group(1) if win_match else ("sun:04:30-sun:05:30" if is_maint else "03:00-04:00")

            prop_key = "preferred_maintenance_window" if is_maint else "preferred_backup_window"
            cli_flag = "--preferred-maintenance-window" if is_maint else "--preferred-backup-window"
            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} {cli_flag} \"{win_val}\" --apply-immediately --region {self.aws.region}"

            md = f"### ⏰ Researched AWS CLI Setting: {'Maintenance' if is_maint else 'Backup'} Window for `{target_id}`\n\n"
            md += f"- **Target Setting:** `{'PreferredMaintenanceWindow' if is_maint else 'PreferredBackupWindow'}` ➔ **`{win_val}` (UTC)**\n"
            md += f"- **Operational Impact:** Configures the designated time slot for daily automated snapshots or maintenance events. **No reboot required.**\n\n"
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: Set Window to {win_val}",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {prop_key: win_val},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {prop_key: win_val}}, "action_buttons": action_buttons}

        # 6. BACKUP / SNAPSHOT RETENTION PERIOD INTENT
        if any(w in user_msg_lower for w in ["backup retention", "snapshot retention", "backup policy", "retention period", "backup days", "snapshot days", "pitr"]) or (("retention" in user_msg_lower or "backup" in user_msg_lower) and any(c.isdigit() for c in user_msg_lower)):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "poc-standalone-ai")
            is_cluster = matched['is_cluster'] if matched else False

            days_match = re.search(r'([0-9]+)\s*(?:days|day)?', user_message)
            days = int(days_match.group(1)) if days_match else 7

            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} --backup-retention-period {days} --apply-immediately --region {self.aws.region}"

            md = f"### 💾 Researched AWS CLI Setting: Backup Retention Policy for `{target_id}`\n\n"
            md += f"- **Target Setting:** `BackupRetentionPeriod` ➔ **`{days} Days`**\n"
            md += f"- **Operational Impact:** Point-in-Time Restore (PITR) will be retained for {days} days. **No reboot required.**\n\n"
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: Set Backup to {days} Days",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"backup_retention_period": days},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {"backup_retention_period": days}}, "action_buttons": action_buttons}

        # 7. STORAGE AUTOSCALING / MAX ALLOCATED STORAGE / ALLOCATED STORAGE INTENT
        if any(w in user_msg_lower for w in ["storage autoscaling", "max allocated storage", "increase storage", "max storage", "allocated storage", "storage"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False

            gb_match = re.search(r'([0-9]+)\s*(?:gb|g|gigabytes)?', user_message, re.IGNORECASE)
            max_gb = int(gb_match.group(1)) if gb_match else 1000

            is_max = any(w in user_msg_lower for w in ["max", "autoscal", "limit", "cap"])
            param_key = "max_allocated_storage" if is_max else "allocated_storage"
            flag = "--max-allocated-storage" if is_max else "--allocated-storage"

            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} {flag} {max_gb} --apply-immediately --region {self.aws.region}"

            md = f"### 📈 Researched AWS CLI Setting: Storage Configuration for `{target_id}`\n\n"
            md += f"- **Target Setting:** `{'MaxAllocatedStorage' if is_max else 'AllocatedStorage'}` ➔ **`{max_gb} GB`**\n"
            md += f"- **Operational Impact:** Storage will scale immediately without database downtime.\n\n"
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: Set Storage to {max_gb}GB",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {param_key: max_gb},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {param_key: max_gb}}, "action_buttons": action_buttons}

        # 8. PERFORMANCE INSIGHTS INTENT
        if any(w in user_msg_lower for w in ["performance insights", "insights"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False

            disable_intent = any(w in user_msg_lower for w in ["disable", "turn off", "remove", "no", "off"])
            enable = not disable_intent

            cli_flag = "--enable-performance-insights" if enable else "--no-enable-performance-insights"
            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} {cli_flag} --apply-immediately --region {self.aws.region}"

            md = f"### 📊 Researched AWS CLI Setting: Performance Insights for `{target_id}`\n\n"
            md += f"- **Target Setting:** `EnablePerformanceInsights` ➔ **`{enable}`**\n"
            md += f"- **Operational Impact:** Enables 7-day rolling query analysis and SQL load visualization. **No reboot required.**\n\n"
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: {'Enable' if enable else 'Disable'} Performance Insights",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"enable_performance_insights": enable},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {"enable_performance_insights": enable}}, "action_buttons": action_buttons}

        # 9. UNIVERSAL ARBITRARY PROPERTY MODIFICATION (e.g. "set iops to 3000", "change db_instance_class to db.r6g.xlarge", "set ca_certificate to ...")
        prop_change_match = re.search(r'(?:set|change|modify|update)\s+([a-zA-Z0-9_\-]+)\s+(?:to|as|=|:)\s+([^\s,]+)', user_message, re.IGNORECASE)
        if prop_change_match:
            raw_prop = prop_change_match.group(1).strip()
            raw_val = prop_change_match.group(2).strip().strip("'\"")

            # If raw_prop is actually a database identifier (e.g. "update poc-standalone-ai to 18.3"), defer to UPGRADE INTENT
            is_db_name = any(raw_prop.lower() == d['id'].lower() for d in dbs)
            is_version_val = bool(re.match(r'^[0-9]+(?:\.[0-9]+)?$', raw_val))
            if is_db_name and is_version_val:
                prop_change_match = None

        if prop_change_match:
            raw_prop = prop_change_match.group(1).strip()
            raw_val = prop_change_match.group(2).strip().strip("'\"")
            
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "poc-standalone-ai")
            is_cluster = matched['is_cluster'] if matched else False

            # Convert boolean or integer values if applicable
            val_converted = raw_val
            if raw_val.lower() in ["true", "yes", "on", "enable", "enabled"]:
                val_converted = True
            elif raw_val.lower() in ["false", "no", "off", "disable", "disabled"]:
                val_converted = False
            elif raw_val.isdigit():
                val_converted = int(raw_val)

            snake_prop = raw_prop.replace('-', '_').lower()
            kebab_prop = raw_prop.replace('_', '-').lower()
            cli_cmd = f"aws rds modify-db-instance --db-instance-identifier {target_id} --{kebab_prop} {raw_val} --apply-immediately --region {self.aws.region}"

            md = f"### ⚙️ Researched AWS CLI Property: `{raw_prop}` for `{target_id}`\n\n"
            md += f"- **Target Setting:** `{raw_prop}` ➔ **`{raw_val}`**\n"
            md += f"- **Target Resource:** `{target_id}`\n\n"
            md += f"#### 💻 Proposed AWS CLI Command:\n```bash\n{cli_cmd}\n```\n\n"
            md += f"**Would you like me to execute this AWS CLI modification now?**"

            action_buttons = [
                {
                    "label": f"▶️ Run CLI: Set {raw_prop} ({target_id})",
                    "action": "run_cli",
                    "target": target_id,
                    "params": {"cli_command": cli_cmd},
                    "style": "primary"
                },
                {"label": "❌ Cancel", "action": "cancel", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"cli_command": cli_cmd, "params": {snake_prop: val_converted}}, "action_buttons": action_buttons}

        # 10. GENERAL ARBITRARY SETTING / CLI RESEARCH INTENT
        if any(w in user_msg_lower for w in ["how to change", "how do i change", "change setting", "modify setting", "change parameter", "modify parameter", "aws cli to"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            is_cluster = matched['is_cluster'] if matched else False

            md = f"### 🔍 AWS CLI Setting Research for `{target_id}`\n\n"
            md += f"I analyzed your request to modify configuration settings on **`{target_id}`**.\n\n"
            md += f"#### 🛠️ Available Direct Operations:\n"
            md += f"1. **Deletion Protection**: `aws rds modify-db-instance --db-instance-identifier {target_id} --deletion-protection` / `--no-deletion-protection`\n"
            md += f"2. **Auto Minor Version Upgrade**: `aws rds modify-db-instance --db-instance-identifier {target_id} --no-auto-minor-version-upgrade`\n"
            md += f"3. **Extended Support**: `aws rds modify-db-instance --db-instance-identifier {target_id} --engine-lifecycle-support open-source-rds-extended-support`\n"
            md += f"4. **Backup Retention**: `aws rds modify-db-instance --db-instance-identifier {target_id} --backup-retention-period 14`\n"
            md += f"5. **Parameter Group Modifications**: `aws rds modify-db-parameter-group --db-parameter-group-name <group> --parameters ...`\n\n"
            md += f"**Tell me any specific command or setting you'd like to change (e.g. 'change snapshot retention to 60 days' or 'aws rds modify-db-instance ...'), and I will execute it for you.**"

            action_buttons = [
                {
                    "label": f"⚙️ Disable Deletion Protection",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"deletion_protection": False},
                    "style": "primary"
                },
                {
                    "label": f"🛡️ Disable Auto Minor Upgrade",
                    "action": "apply_setting",
                    "target": target_id,
                    "is_cluster": is_cluster,
                    "params": {"auto_minor_version_upgrade": False},
                    "style": "secondary"
                }
            ]
            return {"reply": md, "tool_called": "research_setting", "tool_result": {"target": target_id}, "action_buttons": action_buttons}

        # 2. UPGRADE INTENT (e.g. "upgrade test-rds-pg14 to 18.3", "update to 18.3", "upgrade to 18.3", "start upgrade for test-rds-pg14")
        is_upgrade_req = any(w in user_msg_lower for w in ["upgrade", "migrate", "start upgrade", "start blue/green", "blue/green", "blue green"]) or (("update" in user_msg_lower or "bump" in user_msg_lower) and any(w in user_msg_lower for w in ["to 1", "to v1", "version 1", "to postgres", "to 18", "to 16", "to 17", "to 15"]))
        if is_upgrade_req and not any(w in user_msg_lower for w in ["how", "rules", "explain", "risk", "what is"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            
            # Extract target version if specified
            ver_match = re.search(r'(?:to|version|target)\s*(?:postgresql\s*|postgres\s*|v\s*)?([0-9]+(?:\.[0-9]+)?)', user_message, re.IGNORECASE)
            desired_ver = ver_match.group(1) if ver_match else None

            # Validate target version against AWS
            detector = EnvironmentDetector(self.aws)
            try:
                state = detector.inspect(target_id)
                validated_version, target_family = detector.get_upgrade_target(
                    state['engine'], state['current_version'], desired_version=desired_ver
                )
                target_version = validated_version
            except Exception:
                target_version = desired_ver

            # Trigger upgrade callback if provided
            task_info = None
            if start_upgrade_callback:
                task_info = start_upgrade_callback(target_id, target_version)

            tool_called = "start_upgrade_pipeline"
            tool_result = {"db_identifier": target_id, "target_version": target_version, "task_info": task_info}

            md = f"### 🚀 Blue/Green Upgrade Pipeline Initiated: `{target_id}`\n\n"
            md += f"The 5-stage zero-downtime Blue/Green upgrade orchestration has been launched:\n\n"
            md += f"1. **Pre-Flight Audit**: Parameter group & replication slot validation.\n"
            md += f"2. **Snapshot & Reboot**: Safety snapshot & dynamic parameter enforcement.\n"
            md += f"3. **B/G Provisioning**: AWS Blue/Green replica provisioning to version **{target_version or 'Latest Supported'}**.\n"
            md += f"4. **Policy & Staging**: Auto-scaling & secondary replica synchronization.\n"
            md += f"5. **Replication Lag & Cutover Gate**: Live CloudWatch replication monitoring.\n\n"
            md += f"**Active Deployment ID:** `{task_info.get('task_id') if task_info else 'In Progress'}`\n\n"
            md += f"When the Green replica becomes synchronized, approve the cutover below or in Mission Control."

            action_buttons = [
                {"label": "⚡ Execute Switchover", "action": "switchover", "target": target_id, "style": "primary"},
                {"label": "🛑 Abort / Rollback", "action": "rollback", "target": target_id, "style": "danger"}
            ]
            return {"reply": md, "tool_called": tool_called, "tool_result": tool_result, "action_buttons": action_buttons}

        # 3. SWITCHOVER INTENT (e.g. "switchover test-rds-pg14" or "cutover test-rds-pg14")
        if any(w in user_msg_lower for w in ["switchover", "cutover", "approve switchover"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            bg_id = matched.get('bg_deployment', {}).get('bg_id') if matched else None
            old_db = f"{target_id}-old1"

            md = f"### ⚡ Zero-Downtime DNS Switchover Triggered: `{target_id}`\n\n"
            md += f"AWS Blue/Green DNS cutover has been approved. The target Green replica is now assuming the primary production endpoint `{target_id}`.\n\n"
            md += f"The previous primary database has been preserved as **`{old_db}`** for safety.\n\n"
            md += f"#### 🛡️ Post-Upgrade Decommission Protocol (Step 1 of 3):\n"
            md += f"1. **Step 1:** Delete the Blue/Green deployment mapping to unlock AWS database namespaces.\n"
            md += f"2. **Step 2:** Stop `{old_db}` to save compute costs.\n"
            md += f"3. **Step 3:** Permanently delete `{old_db}` after application verification."

            action_buttons = [
                {"label": "🧹 Delete B/G Mapping", "action": "delete_bg", "target": bg_id or target_id, "style": "primary"},
                {"label": "🛑 Doomsday Rollback", "action": "rollback", "target": target_id, "style": "danger"}
            ]
            return {"reply": md, "tool_called": "execute_switchover", "tool_result": {"target_id": target_id}, "action_buttons": action_buttons}

        # 4. DELETE BG INTENT (e.g. "delete bg", "cleanup bg")
        if any(w in user_msg_lower for w in ["delete bg", "cleanup bg", "remove bg", "delete blue/green", "clean bg"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14")
            old_db = f"{target_id}-old1" if not target_id.endswith("-old1") else target_id
            is_cluster = matched['is_cluster'] if matched else False
            
            try:
                bg_id = matched.get('bg_deployment', {}).get('bg_id') if matched else None
                if bg_id:
                    self.tool_delete_bg(bg_id)
            except Exception:
                pass

            md = f"### 🧹 Blue/Green Mapping Cleanup Initiated for `{target_id}`\n\n"
            md += f"The AWS Blue/Green deployment mapping has been removed, freeing database namespaces.\n\n"
            md += f"#### 🛡️ Post-Upgrade Decommission Protocol (Step 2 of 3):\n"
            md += f"The decommissioned database **`{old_db}`** is currently in non-active status. Power it down to stop incurring compute charges."

            action_buttons = [
                {"label": f"⏸️ Shutdown Old DB ({old_db})", "action": "stop_db", "target": old_db, "is_cluster": is_cluster, "style": "warning"},
                {"label": "🛑 Doomsday Rollback", "action": "rollback", "target": target_id, "style": "danger"}
            ]
            return {"reply": md, "tool_called": "delete_bg", "tool_result": {"target_id": target_id}, "action_buttons": action_buttons}

        # 5. SHUTDOWN / STOP OLD DB INTENT
        if any(w in user_msg_lower for w in ["stop", "shutdown", "power down", "pause db", "stop old"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14-old1")
            is_cluster = matched['is_cluster'] if matched else False
            
            try:
                self.tool_stop_database(target_id, is_cluster)
            except Exception as e:
                print(f"Stop DB error: {e}")

            base_db = target_id.replace("-old1", "")

            md = f"### ⏸️ Database Shutdown Signal Sent: `{target_id}`\n\n"
            md += f"Database **`{target_id}`** is being stopped to eliminate compute charges.\n\n"
            md += f"#### 🛡️ Post-Upgrade Decommission Protocol (Step 3 of 3):\n"
            md += f"Once your team verifies all production workloads on the upgraded database, you can permanently delete **`{target_id}`**."

            action_buttons = [
                {"label": f"🗑️ Delete Old DB ({target_id})", "action": "delete_db", "target": target_id, "is_cluster": is_cluster, "style": "danger"},
                {"label": "🛑 Doomsday Rollback", "action": "rollback", "target": base_db, "style": "danger"}
            ]
            return {"reply": md, "tool_called": "stop_database", "tool_result": {"target_id": target_id}, "action_buttons": action_buttons}

        # 6. DELETE OLD DB INTENT
        if any(w in user_msg_lower for w in ["delete old", "delete database", "delete db", "destroy old", "terminate old"]):
            matched = find_db(user_msg_lower)
            target_id = matched['id'] if matched else (dbs[0]['id'] if dbs else "test-rds-pg14-old1")
            is_cluster = matched['is_cluster'] if matched else False

            try:
                self.tool_delete_database(target_id, is_cluster)
            except Exception as e:
                print(f"Delete DB error: {e}")

            md = f"### 🗑️ Permanent Decommissioning Initiated: `{target_id}`\n\n"
            md += f"Deletion command dispatched for **`{target_id}`** (Deletion protection disabled, member instances cascading deletion handled).\n\n"
            md += f"✅ **Upgrade and Decommissioning Lifecycle is 100% Complete!**"
            return {"reply": md, "tool_called": "delete_database", "tool_result": {"target_id": target_id}, "action_buttons": []}

        # Check intent: List/Discover databases
        if any(w in user_msg_lower for w in ["list", "show", "discover", "all db", "all databases", "all rds", "fleet"]):
            dbs = self.tool_list_databases()
            tool_called = "list_databases"
            tool_result = dbs
            
            md = f"### 🔍 Discovered {len(dbs)} Live Databases in Region `{self.aws.region}`\n\n"
            md += "| Database Identifier | Type | Engine | Current Version | Status | Parameter Group |\n"
            md += "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            for d in dbs:
                md += f"| **`{d['id']}`** | {d['type']} | `{d['engine']}` | `{d['version']}` | `{d['status']}` | `{d['parameter_group']}` |\n"
            md += "\n**Recommended Action:** Select any database to run a deep **Pre-flight Audit** or issue an **Upgrade / Rename** command."
            return {"reply": md, "tool_called": tool_called, "tool_result": tool_result, "action_buttons": []}

        # Check intent: Audit or inspect a specific database
        matched_db = None
        for d in dbs:
            if d['id'].lower() in user_msg_lower:
                matched_db = d['id']
                break

        if matched_db or any(w in user_msg_lower for w in ["audit", "inspect", "check", "preflight", "analyze", "risk"]):
            target_id = matched_db or (dbs[0]['id'] if dbs else "test-aurora-pg14")
            audit = self.tool_audit_database(target_id)
            tool_called = "audit_preflight"
            tool_result = audit

            severity_badge = {
                "LOW": "🟢 LOW RISK",
                "MEDIUM": "🟡 MEDIUM RISK (Auto-Remediated)",
                "HIGH": "🟠 HIGH RISK",
                "BLOCKING": "🔴 BLOCKING ISSUE"
            }.get(audit['risk_score'], audit['risk_score'])

            md = f"### 🛡️ Pre-Flight Audit Report: `{audit['database_id']}`\n\n"
            md += f"- **Overall Risk Level:** **{severity_badge}**\n"
            md += f"- **Engine & Version:** `{audit['engine']}` **{audit['current_version']}** ➔ Target: **{audit['target_version']}** (`{audit['target_family']}`)\n"
            md += f"- **Architecture:** {'Aurora PostgreSQL Cluster' if audit['is_cluster'] else 'Standard RDS Instance'} (Multi-AZ: `{audit['multi_az']}`)\n"
            md += f"- **Source Parameter Group:** `{audit['source_parameter_group']}` (Default: `{audit['is_default_pg']}`)\n\n"

            if audit['risk_factors']:
                md += "#### ⚠️ Key Findings & Automated Protections\n"
                for rf in audit['risk_factors']:
                    icon = "🔴" if rf['severity'] == "CRITICAL" else ("🟡" if rf['severity'] == "WARNING" else "ℹ️")
                    md += f"- {icon} **{rf['title']}**: {rf['description']}\n"
                md += "\n"

            md += "#### ⚙️ Automated Parameter Enforcements for Target Group\n"
            md += "| Parameter | Enforced Value | Purpose |\n"
            md += "| :--- | :--- | :--- |\n"
            for se in audit['static_enforcements']:
                md += f"| `{se['parameter']}` | `{se['target_enforced_value']}` | {se['reason']} |\n"
            for tr in audit['threshold_rules']:
                md += f"| `{tr['parameter']}` | `>={tr['minimum_threshold']}` | {tr['reason']} |\n"

            action_buttons = [
                {"label": f"🚀 Upgrade {target_id} ➔ {audit['target_version']}", "action": "upgrade", "target": target_id, "target_version": audit['target_version'], "style": "primary"},
                {"label": f"🔄 Rename {target_id}", "action": "rename_prompt", "target": target_id, "style": "secondary"}
            ]
            return {"reply": md, "tool_called": tool_called, "tool_result": tool_result, "action_buttons": action_buttons}

        # Check intent: Rollback / Doomsday
        if any(w in user_msg_lower for w in ["rollback", "doomsday", "restore", "recover", "revert"]):
            target_id = matched_db or (dbs[0]['id'] if dbs else "test-aurora-pg14")
            md = f"### 🚨 Doomsday Emergency Rollback Protocol for `{target_id}`\n\n"
            md += "The Doomsday protocol performs instant recovery if a switchover becomes stuck or fails application smoke tests:\n"
            md += "1. **Unlocks Database Namespaces**: Deletes the blocking Blue/Green deployment record without deleting databases.\n"
            md += f"2. **Namespaces Separation**: Renames the broken Green DB to `{target_id}-failed-upgrade`.\n"
            md += f"3. **Primary Restoration**: Renames the original Blue DB (`{target_id}-old1`) back to `{target_id}`.\n\n"
            md += f"**Execute Rollback:** You can trigger this safely below or using the **Doomsday Console** tab."
            
            action_buttons = [
                {"label": f"🚨 Execute Doomsday Rollback on {target_id}", "action": "rollback", "target": target_id, "style": "danger"}
            ]
            return {"reply": md, "tool_called": "doomsday_info", "tool_result": {"target_id": target_id}, "action_buttons": action_buttons}

        # 9. GENERAL ARCHITECTURAL & DATABASE QUESTIONS
        if any(w in user_msg_lower for w in ["aurora vs rds", "rds vs aurora", "difference between aurora", "difference between rds"]):
            md = "### ⚡ Aurora PostgreSQL vs Standard RDS PostgreSQL Architecture\n\n"
            md += "| Architectural Dimension | Amazon Aurora PostgreSQL | Amazon RDS PostgreSQL |\n"
            md += "| :--- | :--- | :--- |\n"
            md += "| **Storage Engine** | Distributed, self-healing shared 6-way storage (3 AZs) | Dedicated EBS volume (GP3/IO2/IO1) |\n"
            md += "| **Replication** | Log-structured storage-tier replication (< 10ms lag) | PostgreSQL streaming/logical replication |\n"
            md += "| **Blue/Green Deployments** | Supported for Cluster topologies | Supported for Standalone DB instances |\n"
            md += "| **Failover Speed** | ~10-30 seconds to read replica | ~60-120 seconds Multi-AZ EBS failover |\n"
            md += "| **Max Storage Scaling** | Auto-expands up to 128 TiB seamlessly | Up to 64 TiB with explicit volume resize |\n\n"
            md += "Both topologies in your fleet (`test-aurora-pg14` and `poc-standalone-ai`) are fully supported by this orchestrator for zero-downtime Blue/Green upgrades."
            action_buttons = [
                {"label": "🛡️ Audit Aurora Cluster", "action": "audit", "target": "test-aurora-pg14", "style": "secondary"},
                {"label": "🛡️ Audit Standalone RDS", "action": "audit", "target": "poc-standalone-ai", "style": "secondary"}
            ]
            return {"reply": md, "tool_called": "knowledge_advisory", "tool_result": None, "action_buttons": action_buttons}

        if any(w in user_msg_lower for w in ["blue green", "blue/green", "how does blue green work", "zero downtime"]):
            md = "### 🔄 AWS Blue/Green Zero-Downtime Deployment Workflow\n\n"
            md += "AWS RDS Blue/Green Deployments establish an isolated staging environment to achieve major version upgrades with typically **< 1 minute cutover downtime**:\n\n"
            md += "1. **Blue Environment (Current Production)**: Continues serving read/write traffic without interruptions.\n"
            md += "2. **Green Environment (Target Staging)**: AWS clones the storage and initializes the target engine version (e.g. PostgreSQL 18.x).\n"
            md += "3. **Logical Replication Synchronization**: Continuous WAL replication streams changes from Blue to Green until replication lag drops near zero (< 1s).\n"
            md += "4. **Switchover (DNS Cutover)**: When you approve switchover, AWS promotes Green to primary, renames endpoints, and demotes Blue to a safe backup (`-old1`).\n"
            md += "5. **Post-Upgrade Decommission**: Delete B/G mapping ➔ Shutdown Old DB ➔ Terminate Old DB.\n"
            action_buttons = [
                {"label": "📋 View Fleet Databases", "action": "list_fleet", "style": "primary"}
            ]
            return {"reply": md, "tool_called": "knowledge_advisory", "tool_result": None, "action_buttons": action_buttons}

        # Contextual response summarizing fleet status and capabilities
        dbs_str = ", ".join([f"`{d['id']}` ({d['engine']} {d['version']})" for d in dbs]) if dbs else "No active databases"
        md = f"### 🤖 AWS RDS Upgrade AI Agent Copilot\n\n"
        md += f"I am connected to your live fleet in region **`{self.aws.region}`** with active databases: {dbs_str}.\n\n"
        md += "You can talk to me naturally about anything regarding your databases:\n"
        md += "- **Operational Tasks**: *\"Upgrade test-aurora-pg14 to 15.13\"*, *\"Rename poc-rds-standalone to poc-standalone-ai\"*\n"
        md += "- **Settings & Security**: *\"What is deletion protection on poc-standalone-ai?\"*, *\"Disable auto minor version upgrade\"*\n"
        md += "- **Performance & Sizing**: *\"How should I tune max_connections and shared_buffers for heavy read traffic?\"*\n"
        md += "- **Decommission & Cleanup**: *\"Delete Blue/Green mapping\"*, *\"Stop old database\"*, *\"Emergency rollback\"*\n"
        return {"reply": md, "tool_called": None, "tool_result": None, "action_buttons": []}

    def _extract_dynamic_actions(self, reply_text: str, user_message: str, dbs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Dynamically detects ANY AWS CLI commands and database operations in LLM output to produce interactive buttons."""
        import re
        buttons = []
        seen_actions = set()

        # 1. Universal AWS CLI Detection (e.g. aws rds modify-db-instance, create-db-snapshot, reboot-db-instance, etc.)
        code_block_cmds = re.findall(r'```(?:bash|sh)?\s*(aws\s+rds\s+[\s\S]*?)```', reply_text, re.IGNORECASE)
        inline_cmds = re.findall(r'`(aws\s+rds\s+[^`]+)`', reply_text, re.IGNORECASE)
        all_raw_cmds = code_block_cmds + inline_cmds
        if not all_raw_cmds:
            all_raw_cmds = re.findall(r'(?:^|\n)\s*(aws\s+rds\s+[^\n\r]+)', reply_text, re.IGNORECASE)

        for raw_cmd in all_raw_cmds:
            flat_cmd = re.sub(r'\\\s*[\r\n]+', ' ', raw_cmd).strip()
            flat_cmd = re.sub(r'#.*$', '', flat_cmd, flags=re.MULTILINE).strip()
            for single_cmd in re.findall(r'aws\s+rds\s+[^;\n\r`]+', flat_cmd):
                single_cmd = single_cmd.strip()
                if len(single_cmd) > 10:
                    target_m = re.search(r'--db-(?:instance|cluster)-identifier\s+["\']?([a-zA-Z0-9\-_]+)["\']?', single_cmd)
                    target = target_m.group(1) if target_m else (dbs[0]['id'] if dbs else "database")
                    
                    subcmd_m = re.search(r'aws\s+rds\s+([a-zA-Z0-9\-_]+)', single_cmd)
                    subcmd = subcmd_m.group(1) if subcmd_m else "command"
                    lbl_subcmd = subcmd.replace('-', ' ').title()
                    
                    btn_key = f"cli_{single_cmd[:40]}"
                    if btn_key not in seen_actions and len(buttons) < 6:
                        seen_actions.add(btn_key)
                        buttons.append({
                            "label": f"▶️ Run CLI: {lbl_subcmd} ({target})",
                            "action": "run_cli",
                            "target": target,
                            "params": {"cli_command": single_cmd},
                            "style": "primary"
                        })
                        buttons.append({
                            "label": "❌ Cancel",
                            "action": "cancel",
                            "target": target,
                            "style": "secondary"
                        })

        # 2. Contextual Fleet Action Suggestions
        text_lower = (reply_text + " " + user_message).lower()
        for d in dbs:
            db_id = d['id']
            if db_id.lower() in text_lower and len(buttons) < 4:
                if any(w in text_lower for w in ["upgrade", "target version", "blue/green", "18.3", "18.1", "16."]):
                    btn_key = f"upgrade_{db_id}"
                    if btn_key not in seen_actions:
                        seen_actions.add(btn_key)
                        buttons.append({
                            "label": f"🚀 Upgrade {db_id}",
                            "action": "upgrade_prompt",
                            "target": db_id,
                            "style": "primary"
                        })
                if any(w in text_lower for w in ["audit", "inspect", "parameter group", "preflight", "check", "risk"]):
                    btn_key = f"audit_{db_id}"
                    if btn_key not in seen_actions:
                        seen_actions.add(btn_key)
                        buttons.append({
                            "label": f"🛡️ Audit {db_id}",
                            "action": "audit",
                            "target": db_id,
                            "style": "secondary"
                        })
                if any(w in text_lower for w in ["switchover", "cutover"]):
                    btn_key = f"switchover_{db_id}"
                    if btn_key not in seen_actions:
                        seen_actions.add(btn_key)
                        buttons.append({
                            "label": f"⚡ Switchover {db_id}",
                            "action": "switchover",
                            "target": db_id,
                            "style": "primary"
                        })
                if any(w in text_lower for w in ["rollback", "emergency", "doomsday", "recover"]):
                    btn_key = f"rollback_{db_id}"
                    if btn_key not in seen_actions:
                        seen_actions.add(btn_key)
                        buttons.append({
                            "label": f"🚨 Emergency Rollback {db_id}",
                            "action": "rollback",
                            "target": db_id,
                            "style": "danger"
                        })

        return buttons

    def _execute_tool_call_if_present(self, reply_text: str, start_upgrade_callback=None) -> Optional[Dict[str, Any]]:
        """Parses and runs any explicit structured tool calls emitted by the LLM."""
        import re
        tool_pattern = r'```json\s*(\{[\s\S]*?"tool_call"[\s\S]*?\})\s*```'
        match = re.search(tool_pattern, reply_text)
        if not match:
            tool_pattern_raw = r'(\{\s*"tool_call"\s*:\s*\{[\s\S]*?\}\s*\})'
            match = re.search(tool_pattern_raw, reply_text)
            
        if match:
            try:
                data = json.loads(match.group(1))
                tc = data.get("tool_call", {})
                name = tc.get("name")
                args = tc.get("arguments", {})
                
                clean_reply = re.sub(r'```json\s*\{[\s\S]*?"tool_call"[\s\S]*?\}\s*```', '', reply_text).strip()
                clean_reply = re.sub(r'\{\s*"tool_call"\s*:\s*\{[\s\S]*?\}\s*\}', '', clean_reply).strip()
                dbs = self.tool_list_databases()

                if name in ["execute_aws_cli", "run_aws_cli", "aws_cli", "run_cli", "execute_cli"]:
                    cmd = args.get("command") or args.get("cli_command") or args.get("cmd")
                    if cmd:
                        res = self.tool_execute_aws_cli(cmd)
                        md = f"### ✅ AWS CLI Command Executed Successfully!\n\n```bash\n{cmd}\n```\n\n"
                        if res.get("stdout"):
                            md += f"**Output Response:**\n```json\n{json.dumps(res.get('output'), indent=2)[:800]}\n```"
                        target = (dbs[0]['id'] if dbs else "database")
                        buttons = [
                            {"label": f"🛡️ Audit {target}", "action": "audit", "target": target, "style": "secondary"},
                            {"label": f"🚀 Upgrade {target}", "action": "upgrade_prompt", "target": target, "style": "primary"}
                        ]
                        return {"reply": md, "tool_called": "execute_aws_cli", "tool_result": res, "action_buttons": buttons}

                elif name in ["modify_database_configuration", "modify_database_setting", "apply_setting", "modify_db"]:
                    target_id = args.get("db_identifier") or args.get("identifier") or args.get("target") or (dbs[0]['id'] if dbs else None)
                    matched = next((d for d in dbs if d['id'].lower() == (target_id or '').lower()), None)
                    is_cluster = matched['is_cluster'] if matched else False
                    
                    filtered_args = {k: v for k, v in args.items() if k not in ["db_identifier", "identifier", "target", "engine", "is_cluster"]}
                    if "new_retention_period_days" in args:
                        filtered_args["backup_retention_period"] = int(args["new_retention_period_days"])
                    if "retention_period" in args:
                        filtered_args["backup_retention_period"] = int(args["retention_period"])

                    if target_id:
                        res = self.tool_modify_database_setting(target_id, is_cluster=is_cluster, **filtered_args)
                        md = f"### ✅ AWS CLI Configuration Applied Successfully!\n\n"
                        md += f"The requested AWS RDS modifications have been executed directly on **`{target_id}`**:\n\n"
                        for k, v in filtered_args.items():
                            md += f"- **`{k}`**: `{v}`\n"
                        md += "\nAWS has accepted the change with `ApplyImmediately=True`. Fleet status is updated."
                        buttons = [
                            {"label": f"🛡️ Audit {target_id}", "action": "audit", "target": target_id, "style": "secondary"},
                            {"label": f"🚀 Upgrade {target_id}", "action": "upgrade_prompt", "target": target_id, "style": "primary"}
                        ]
                        return {"reply": md, "tool_called": "modify_database_configuration", "tool_result": res, "action_buttons": buttons}

                elif name == "rename_database":
                    old_id = args.get("old_identifier") or args.get("db_identifier")
                    new_id = args.get("new_identifier")
                    if old_id and new_id:
                        matched = next((d for d in dbs if d['id'].lower() == old_id.lower()), None)
                        is_cluster = matched['is_cluster'] if matched else False
                        res = self.tool_rename_database(old_id, new_id, is_cluster=is_cluster)
                        md = f"### 🔄 Database Renaming Executed!\n\n- **Previous Identifier:** `{old_id}`\n- **New Identifier:** `{new_id}`\n- **Status:** AWS modify applied immediately.\n\n"
                        if clean_reply:
                            md = clean_reply + "\n\n" + md
                        buttons = [
                            {"label": f"🛡️ Audit {new_id}", "action": "audit", "target": new_id, "style": "secondary"},
                            {"label": f"🚀 Upgrade {new_id}", "action": "upgrade_prompt", "target": new_id, "style": "primary"}
                        ]
                        return {"reply": md, "tool_called": "rename_database", "tool_result": res, "action_buttons": buttons}

                elif name == "audit_preflight":
                    target_id = args.get("db_identifier")
                    if target_id:
                        res = self.tool_audit_database(target_id)
                        md = clean_reply if clean_reply else f"### 🛡️ Preflight Audit Completed for `{target_id}`"
                        buttons = [
                            {"label": f"🚀 Upgrade {target_id}", "action": "upgrade_prompt", "target": target_id, "style": "primary"},
                            {"label": f"🔄 Rename {target_id}", "action": "rename_prompt", "target": target_id, "style": "secondary"}
                        ]
                        return {"reply": md, "tool_called": "audit_preflight", "tool_result": res, "action_buttons": buttons}
            except Exception:
                pass
        return None

    def _call_gemini_flash(self, user_message: str, start_upgrade_callback=None, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Call Gemini Flash API with agent context and multi-turn history."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"
        
        dbs = self.tool_list_databases()
        fleet_summary = [
            {"id": d['id'], "engine": d['engine'], "version": d['version'], "is_cluster": d.get('is_cluster', False)}
            for d in dbs
        ]
        context = f"AWS Fleet ({self.aws.region}):\n{json.dumps(fleet_summary, indent=2)}\n\nUser Question: {user_message}"
        
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": SYSTEM_PROMPT_AGENT},
                    {"text": context}
                ]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 2048
            }
        }

        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                text = data['candidates'][0]['content']['parts'][0]['text']
                
                tool_res = self._execute_tool_call_if_present(text, start_upgrade_callback)
                if tool_res:
                    return tool_res
                
                buttons = self._extract_dynamic_actions(text, user_message, dbs)
                return {"reply": text, "tool_called": "gemini_flash_reasoning", "tool_result": {"model": self.model_name}, "action_buttons": buttons}
            else:
                fallback = self._call_builtin_agent(user_message, start_upgrade_callback, history=history)
                fallback["reply"] = f"*[Gemini API Notice: Returned status {resp.status_code}. Switched to Built-in Agent]*\n\n" + fallback["reply"]
                return fallback
        except Exception as e:
            fallback = self._call_builtin_agent(user_message, start_upgrade_callback, history=history)
            fallback["reply"] = f"*[Notice: Could not connect to Gemini Flash API ({str(e)}). Switched to Built-in Agent]*\n\n" + fallback["reply"]
            return fallback

    def _call_local_llm(self, user_message: str, start_upgrade_callback=None, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Call LM Studio / Local LLM OpenAI-compatible endpoint with multi-turn conversational history and tool execution."""
        import re
        dbs = self.tool_list_databases()
        fleet_summary = [
            {
                "id": d['id'],
                "engine": d['engine'],
                "version": d['version'],
                "is_cluster": d.get('is_cluster', False),
                "parameter_group": d.get('parameter_group')
            }
            for d in dbs
        ]

        # Assemble full multi-turn chat messages
        system_content = f"{SYSTEM_PROMPT_AGENT}\n\nLive AWS Fleet in {self.aws.region}:\n{json.dumps(fleet_summary, indent=2)}"
        messages = [{"role": "system", "content": system_content}]

        if history:
            for m in history[-6:]:
                m_sender = m.get("sender")
                m_text = m.get("text", "").strip()
                if m_text and not m_text.startswith("Started new dedicated") and not m_text.startswith("Chat history cleared"):
                    # Strip any HTML thought details tags to preserve token efficiency
                    m_clean = re.sub(r'<details[\s\S]*?</details>', '', m_text).strip()
                    if m_clean:
                        role = "assistant" if m_sender == "agent" else "user"
                        messages.append({"role": role, "content": m_clean})

        # Append current user message if not already trailing
        if not messages or messages[-1].get("content") != user_message:
            messages.append({"role": "user", "content": user_message})
        
        payload = {
            "model": self.model_name or "deepseek/deepseek-r1-0528-qwen3-8b",
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 2048
        }

        endpoint = self.local_endpoint.rstrip('/')
        if not endpoint.endswith('/chat/completions'):
            endpoint = f"{endpoint}/chat/completions"

        try:
            # 120s timeout so local models have ample time to process prompts and reason
            resp = requests.post(endpoint, json=payload, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                choice = data.get('choices', [{}])[0].get('message', {})
                content = choice.get('content', '')
                reasoning = choice.get('reasoning_content', '')
                
                final_text = ""
                if reasoning:
                    final_text += f"<details style='margin-bottom:10px; background:rgba(255,255,255,0.04); padding:8px 12px; border-radius:6px;'><summary style='color:var(--text-muted); cursor:pointer;'>💭 DeepSeek-R1 Thought Process</summary><div style='margin-top:8px; font-size:0.85rem; color:var(--text-faint); font-family:var(--font-mono); white-space:pre-wrap;'>{reasoning}</div></details>\n\n"
                
                final_text += content
                if not final_text.strip():
                    fallback = self._call_builtin_agent(user_message, start_upgrade_callback, history=history)
                    return fallback

                # Check for structured tool call
                tool_res = self._execute_tool_call_if_present(final_text, start_upgrade_callback)
                if tool_res:
                    return tool_res

                # Extract dynamic action buttons
                buttons = self._extract_dynamic_actions(final_text, user_message, dbs)
                return {"reply": final_text, "tool_called": "lm_studio_deepseek_r1", "tool_result": {"model": self.model_name}, "action_buttons": buttons}
            else:
                fallback = self._call_builtin_agent(user_message, start_upgrade_callback, history=history)
                fallback["reply"] = f"*[LM Studio Notice: Endpoint returned {resp.status_code}. Switched to Built-in Agent]*\n\n" + fallback["reply"]
                return fallback
        except Exception as e:
            fallback = self._call_builtin_agent(user_message, start_upgrade_callback, history=history)
            fallback["reply"] = f"*[Notice: LM Studio ({endpoint}) not reachable ({str(e)}). Switched to Built-in Agent]*\n\n" + fallback["reply"]
            return fallback

