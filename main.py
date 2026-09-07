import argparse
from core.aws_client import AWSClient
from core.detector import EnvironmentDetector
from core.param_manager import ParameterManager
from core.rebooter import RebootOrchestrator
from core.reporter import UpgradeReporter
from core.snapshotter import SnapshotManager
from core.deployer import DeploymentManager 
from core.stager import StagingManager
from core.switcher import SwitchoverManager

def run_upgrade_engine(db_identifier, target_version=None, region=None, 
                       auto_reboot=False, auto_deploy=False, 
                       auto_switchover=False, auto_stop_old_db=False):
    
    print(f"=== Starting Blue/Green Upgrade Engine ===")
    
    aws = AWSClient(region_name=region)
    detector = EnvironmentDetector(aws)
    param_manager = ParameterManager(aws)
    rebooter = RebootOrchestrator(aws)
    reporter = UpgradeReporter(param_manager)
    snapshotter = SnapshotManager(aws)
    deployer = DeploymentManager(aws) 
    stager = StagingManager(aws)
    switcher = SwitchoverManager(aws)

    try:
        # --- PHASE 1: PRE-FLIGHT ---
        state = detector.inspect(db_identifier)
        validated_version, target_family = detector.get_upgrade_target(
            state['engine'], state['current_version'], desired_version=target_version
        )
        print(f"\n[*] Validated Target: {validated_version} ({target_family})")

        snapshotter.take_preflight_snapshot(state)
        param_manager.handle_default_source_group(state)
        # --- UPDATED LINE ---
        target_pg, target_instance_pg = param_manager.prep_target_group(state, validated_version, target_family)
        reporter.generate_report(state, target_pg)

        print("\n=== Reboot Orchestration ===")
        if auto_reboot:
            rebooter.execute_reboot(state)
        else:
            user_input = input("[?] Trigger the required reboot sequence now? (y/n): ").strip().lower()
            if user_input in ['y', 'yes']:
                rebooter.execute_reboot(state)

        # --- PHASE 2: DEPLOYMENT ---
        print("\n=== Phase 2: Deployment Gateway ===")
        bg_id = None
        
        if auto_deploy:
            print(f"[*] Auto-deploy ENABLED. Triggering B/G deployment to {validated_version}...")
            # --- UPDATED LINE ---
            bg_id = deployer.trigger_deployment(state, validated_version, target_pg, target_instance_pg)
        else:
            deploy_input = input(f"[?] All pre-flight checks passed. TRIGGER Blue/Green deployment? (y/n): ").strip().lower()
            if deploy_input in ['y', 'yes']:
                # --- UPDATED LINE ---
                bg_id = deployer.trigger_deployment(state, validated_version, target_pg, target_instance_pg)
            else:
                print("\n[INFO] Deployment paused. Exiting.")
                return

        if bg_id:
            deployer.wait_for_deployment(bg_id)
            
            # --- PHASE 3: STAGING ---
            stager.restore_scaling_policies(state, bg_id)
            
            # --- PHASE 4: SWITCHOVER ---
            print("\n=== Phase 4: Cutover Gateway ===")
            
            # 1. Guardrail Check
            is_lag_safe = switcher.monitor_replication_lag(state, bg_id)
            
            if not is_lag_safe and not auto_switchover:
                print("\n[WARNING] Replication lag is high. Manual confirmation is highly recommended.")
            
            # 2. Execution Gate
            if auto_switchover:
                print(f"[*] Auto-switchover ENABLED. Bypassing human approval...")
                switcher.execute_switchover(bg_id, state, auto_stop_old_db=auto_stop_old_db)
            else:
                switch_input = input(f"\n[CRITICAL] Are you ready to route production traffic to the new {validated_version} database? (y/n): ").strip().lower()
                if switch_input in ['y', 'yes']:
                    switcher.execute_switchover(bg_id, state, auto_stop_old_db=auto_stop_old_db)
                else:
                    print("\n[INFO] Switchover aborted. The Green environment will remain active for manual cutover in the AWS Console.")

            print("\n=== UPGRADE WORKFLOW COMPLETE ===")

    except RuntimeError as e:
        print(f"\n[BLOCKED] {e}")
    except Exception as e:
        print(f"\n[FATAL ERROR] {str(e)}")

if __name__ == "__main__":
    # ==========================================
    # --- HARDCODED CONFIGURATION ---
    # ==========================================
    TARGET_DB = "test-aurora-multi"
    DESIRED_VERSION = "17.9"         
    AWS_REGION = None # Set to None to auto-detect from your CLI profile        
    
    # Set these to True to fully automate a non-prod pipeline
    AUTO_REBOOT_ENABLED = True
    AUTO_DEPLOY_ENABLED = True       
    AUTO_SWITCHOVER_ENABLED = True  
    AUTO_STOP_OLD_DB = False # Safely freeze the old DB for 7 days post-cutover
    # ==========================================
    
    run_upgrade_engine(
        db_identifier=TARGET_DB, 
        target_version=DESIRED_VERSION,
        region=AWS_REGION,
        auto_reboot=AUTO_REBOOT_ENABLED,
        auto_deploy=AUTO_DEPLOY_ENABLED,
        auto_switchover=AUTO_SWITCHOVER_ENABLED,
        auto_stop_old_db=AUTO_STOP_OLD_DB
    )