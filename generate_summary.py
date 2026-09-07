import os
import json
import glob
from datetime import datetime

# Critical parameter classifications
CRITICAL_SAFETY_PARAMS = {
    'rds.logical_replication': 'Enables logical replication (required for B/G)',
    'synchronous_commit': 'Configures replication sync durability',
    'rds.force_ssl': 'Forces SSL connections',
    'idle_in_transaction_session_timeout': 'Prevents pg_upgrade lock timeouts',
    'wal_sender_timeout': 'Prevents wal sender timeouts during syncing',
    'max_replication_slots': 'Replication slots capacity limit',
    'max_wal_senders': 'Wal senders capacity limit',
    'max_logical_replication_workers': 'Logical replication worker limit',
    'max_worker_processes': 'Maximum background worker processes'
}

CARRY_OVER_PARAMS = [
    'password_encryption',
    'track_commit_timestamp',
    'shared_buffers',
    'work_mem',
    'shared_preload_libraries',
    'track_activity_query_size',
    'rds.babelfish_status',
    'apg_ccm_enabled',
    'aurora_replica_read_consistency'
]

def truncate_value(val, max_len=45):
    if val is None:
        return '[Not Set]'
    val_str = str(val)
    if len(val_str) > max_len:
        return val_str[:max_len-3] + "..."
    return val_str

def parse_report(filepath):
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    metadata = data.get('metadata', {})
    db_id = metadata.get('database_identifier', 'unknown')
    engine = metadata.get('engine', 'unknown')
    is_aurora = metadata.get('is_aurora', False)
    timestamp = metadata.get('timestamp', '')
    
    scaling = data.get('post_cutover_actions', {}).get('aurora_auto_scaling', None)
    
    pg_data = data.get('parameter_groups', {})
    source_pg = pg_data.get('source', {})
    target_pg = pg_data.get('target', {})
    
    source_name = source_pg.get('name', 'N/A')
    target_name = target_pg.get('name', 'N/A')
    
    source_params = source_pg.get('parameters', {})
    target_params = target_pg.get('parameters', {})
    
    # Calculate difference
    diffs = {}
    
    # Check all parameter names
    all_param_names = sorted(list(set(source_params.keys()) | set(target_params.keys())))
    
    for name in all_param_names:
        src_val = source_params.get(name, {}).get('ParameterValue')
        tgt_val = target_params.get(name, {}).get('ParameterValue')
        
        # Check if they differ
        if src_val != tgt_val:
            param_meta = target_params.get(name) or source_params.get(name)
            description = param_meta.get('Description', 'No description available.')
            apply_type = param_meta.get('ApplyType', 'unknown')
            
            diffs[name] = {
                'source_value': src_val if src_val is not None else '[Not Set]',
                'target_value': tgt_val if tgt_val is not None else '[Not Set]',
                'apply_type': apply_type,
                'description': description
            }
            
    return {
        'filepath': filepath,
        'db_id': db_id,
        'engine': engine,
        'is_aurora': is_aurora,
        'timestamp': timestamp,
        'scaling': scaling,
        'source_name': source_name,
        'target_name': target_name,
        'diffs': diffs,
        'source_params_count': len(source_params),
        'target_params_count': len(target_params)
    }

def generate_markdown_summary():
    report_files = glob.glob('upgrade_reports/*_prep_report.json')
    if not report_files:
        print("No reports found matching upgrade_reports/*_prep_report.json")
        return
    
    parsed_reports = []
    for fp in report_files:
        try:
            parsed_reports.append(parse_report(fp))
        except Exception as e:
            print(f"Error parsing {fp}: {e}")
            
    # Sort by DB Identifier
    parsed_reports.sort(key=lambda x: x['db_id'])
    
    md_lines = []
    md_lines.append("# AWS RDS Blue/Green Upgrade Engine - Prep Reports Summary")
    md_lines.append(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md_lines.append("\nThis document compiles and summarizes the upgrade preparation reports generated for your AWS RDS and Aurora PostgreSQL databases. It details critical parameter changes, carryover configurations, and potential validation alerts.")
    md_lines.append("\n## 1. Executive Summary Table")
    md_lines.append("\n| Database Identifier | Engine | Source PG | Target PG | Parameter Diffs | Auto Scaling |")
    md_lines.append("| :--- | :--- | :--- | :--- | :---: | :--- |")
    
    for r in parsed_reports:
        scaling_status = "Configured" if r['scaling'] and r['scaling'] != "None Configured" else "None"
        diff_count = len(r['diffs'])
        md_lines.append(f"| **{r['db_id']}** | `{r['engine']}` | `{r['source_name']}` | `{r['target_name']}` | {diff_count} | {scaling_status} |")
        
    md_lines.append("\n---")
    md_lines.append("\n## 2. Detailed Database Audits")
    
    for r in parsed_reports:
        md_lines.append(f"\n### Database: `{r['db_id']}`")
        md_lines.append(f"- **Engine**: `{r['engine']}` (Aurora: {r['is_aurora']})")
        md_lines.append(f"- **Source Parameter Group**: `{r['source_name']}` ({r['source_params_count']} parameters)")
        md_lines.append(f"- **Target Parameter Group**: `{r['target_name']}` ({r['target_params_count']} parameters)")
        
        # Auto-scaling section
        if r['is_aurora']:
            scaling_val = r['scaling']
            if scaling_val and scaling_val != "None Configured":
                md_lines.append(f"- **Aurora Auto-Scaling**: Captured scaling policy configuration:")
                md_lines.append("\n```json\n" + json.dumps(scaling_val, indent=2) + "\n```")
            else:
                md_lines.append("- **Aurora Auto-Scaling**: None configured or inherited.")
                
        # Warnings and Alerts based on parameters
        has_warnings = False
        warning_box = []
        
        # Check for static logical replication enforcement
        log_rep = r['diffs'].get('rds.logical_replication')
        if log_rep and log_rep['target_value'] == '1':
            warning_box.append("> [!IMPORTANT]\n> **Logical Replication Enabled**: `rds.logical_replication` has been changed from `{}` to `1`. **A reboot of the source database is required** to apply this change before initiating the Blue/Green deployment.".format(log_rep['source_value']))
            has_warnings = True
            
        # Check for starved replication parameters
        for p in ['max_replication_slots', 'max_worker_processes', 'max_logical_replication_workers']:
            diff_p = r['diffs'].get(p)
            if diff_p:
                warning_box.append("> [!NOTE]\n> **B/G Threshold Elevate**: Parameter `{}` has been elevated to `{}` (was `{}`). This prevents replica synchronization starvation during provisioning.".format(p, diff_p['target_value'], diff_p['source_value']))
                has_warnings = True
                
        # Check for hardcoded shared_buffers warning
        sb = r['diffs'].get('shared_buffers') or target_params_check(r, 'shared_buffers')
        if sb and sb.isdigit():
            warning_box.append("> [!WARNING]\n> **Hardcoded Buffer Allocation**: `shared_buffers` is statically set to `{}` bytes. Ensure this does not consume >75% of your target DB instance memory class, or the `pg_upgrade` utility will fail.".format(sb))
            has_warnings = True

        if has_warnings:
            md_lines.append("\n#### Critical Guardrails & Required Actions")
            md_lines.extend(warning_box)
            
        # Diff Table
        if r['diffs']:
            md_lines.append("\n#### Parameter Modifications Diff")
            md_lines.append("\n| Parameter Name | Source Value | Target Value | Apply Type | Classification / Purpose |")
            md_lines.append("| :--- | :--- | :--- | :--- | :--- |")
            
            # Sort diffs: put safety params first, then carryover, then others
            sorted_diff_keys = sorted(r['diffs'].keys(), key=lambda k: (
                0 if k in CRITICAL_SAFETY_PARAMS else 
                (1 if k in CARRY_OVER_PARAMS else 2), k
            ))
            
            for k in sorted_diff_keys:
                diff = r['diffs'][k]
                source_val_trunc = truncate_value(diff['source_value'])
                target_val_trunc = truncate_value(diff['target_value'])
                
                source_val = f"`{source_val_trunc}`" if diff['source_value'] != '[Not Set]' else "*Not Set*"
                target_val = f"`{target_val_trunc}`" if diff['target_value'] != '[Not Set]' else "*Not Set*"
                
                # Check category
                category = "General parameter"
                if k in CRITICAL_SAFETY_PARAMS:
                    category = f"**Safety**: {CRITICAL_SAFETY_PARAMS[k]}"
                elif k in CARRY_OVER_PARAMS:
                    category = f"**Carryover**: Match source behavior"
                    
                md_lines.append(f"| `{k}` | {source_val} | {target_val} | `{diff['apply_type']}` | {category} |")
        else:
            md_lines.append("\n*No parameter group modifications detected between source and target groups.*")
            
        md_lines.append("\n---")
        
    summary_path = 'upgrade_reports/summary_report.md'
    with open(summary_path, 'w') as f:
        f.write('\n'.join(md_lines))
        
    print(f"\n[SUCCESS] Compiled comprehensive summary to: {summary_path}")

def target_params_check(report_data, param_name):
    # Quick helper to extract target value even if it didn't diff
    filepath = report_data['filepath']
    with open(filepath, 'r') as f:
        data = json.load(f)
    tgt_params = data.get('parameter_groups', {}).get('target', {}).get('parameters', {})
    return tgt_params.get(param_name, {}).get('ParameterValue')

if __name__ == '__main__':
    generate_markdown_summary()
