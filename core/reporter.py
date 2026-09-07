import json
import os
from datetime import datetime

class UpgradeReporter:
    """Generates an audit report of the upgrade prep, including parameters and scaling policies."""
    
    def __init__(self, param_manager):
        self.param_manager = param_manager

    def generate_report(self, state, target_pg_name):
        print("\n[*] Generating Upgrade Audit Report...")
        
        # Fetch the raw parameters directly from AWS via the param_manager helper
        source_params = self.param_manager._get_current_params(state['source_pg_name'], state['is_cluster'])
        target_params = self.param_manager._get_current_params(target_pg_name, state['is_cluster'])

        report = {
            "metadata": {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "database_identifier": state['identifier'],
                "engine": state['engine'],
                "is_aurora": state['is_aurora']
            },
            "post_cutover_actions": {
                "aurora_auto_scaling": state.get('scaling_policies', "None Configured")
            },
            "parameter_groups": {
                "source": {
                    "name": state['source_pg_name'],
                    "parameters": source_params
                },
                "target": {
                    "name": target_pg_name,
                    "parameters": target_params
                }
            }
        }

        # Save to a local reports directory
        os.makedirs('upgrade_reports', exist_ok=True)
        filename = f"upgrade_reports/{state['identifier']}_prep_report.json"
        
        # default=str handles AWS datetime objects cleanly
        with open(filename, 'w') as f:
            json.dump(report, f, indent=4, default=str) 
            
        print(f"  [+] Report saved locally: {filename}")
        return filename