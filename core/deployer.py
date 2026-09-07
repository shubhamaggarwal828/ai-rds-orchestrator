import time
from datetime import datetime

class DeploymentManager:
    def __init__(self, aws_client):
        self.aws = aws_client

    def wait_for_source_ready(self, state):
        print(f"\n[*] Pre-Deployment Check: Verifying source database is ready...")
        while True:
            if state['is_cluster']:
                members = self.aws.rds.describe_db_clusters(DBClusterIdentifier=state['identifier'])['DBClusters'][0].get('DBClusterMembers', [])
                all_available = True
                statuses = []
                for member in members:
                    instance_info = self.aws.rds.describe_db_instances(DBInstanceIdentifier=member['DBInstanceIdentifier'])['DBInstances'][0]
                    if instance_info:
                        status = instance_info.get('DBInstanceStatus', 'unknown')
                        statuses.append(f"{member['DBInstanceIdentifier']}:{status}")
                        if status.lower() != 'available':
                            all_available = False
                if all_available:
                    print("  [+] All Aurora instances are 'available' and ready for deployment.")
                    break
                else:
                    print(f"  [-] Waiting for instances to finish tasks: [{', '.join(statuses)}]")
                    time.sleep(30)
            else:
                info = self.aws.rds.describe_db_instances(DBInstanceIdentifier=state['identifier'])['DBInstances'][0]
                status = info.get('DBInstanceStatus', 'unknown')
                if status.lower() == 'available':
                    print("  [+] Source instance is 'available' and ready for deployment.")
                    break
                else:
                    print(f"  [-] Source status is '{status}'. Waiting 30s...")
                    time.sleep(30)

    def verify_logical_replication_active(self, state):
        """Confirms rds.logical_replication=1 is live on the ACTUAL attached PG before triggering B/G.

        Critically, this re-reads the parameter group name directly from AWS (not from the
        in-memory state dict) so we verify what AWS itself will use when creating the B/G
        deployment — not a stale in-memory value that may be wrong after a failed run.
        """
        is_cluster = state['is_cluster']
        print(f"\n[*] Pre-Deployment Verification: Re-reading actual attached PG from AWS...")

        # Re-read the live PG name straight from the AWS API
        if is_cluster:
            live = self.aws.rds.describe_db_clusters(
                DBClusterIdentifier=state['identifier']
            )['DBClusters'][0]
            actual_pg = live['DBClusterParameterGroup']
        else:
            live = self.aws.rds.describe_db_instances(
                DBInstanceIdentifier=state['identifier']
            )['DBInstances'][0]
            actual_pg = live['DBParameterGroups'][0]['DBParameterGroupName']

        print(f"  [*] AWS reports actual attached PG: '{actual_pg}'")

        # Guard 1: must not be a default PG — AWS B/G rejects them as sources
        if actual_pg.startswith('default.'):
            raise RuntimeError(
                f"\n[BLOCKED] DB '{state['identifier']}' is still using the default parameter group '{actual_pg}'.\n"
                "The custom parameter group was not successfully attached.\n"
                "ACTION REQUIRED: Manually attach a custom PG with rds.logical_replication=1, reboot, and re-run."
            )

        # Guard 2: rds.logical_replication must be '1' in the actual live PG
        val = self.aws.get_runtime_parameter_value(actual_pg, 'rds.logical_replication', is_cluster)
        if val == '1':
            print(f"  [+] rds.logical_replication = 1 confirmed ACTIVE on '{actual_pg}'. Safe to proceed.")
            # Keep state in sync with what AWS actually has
            state['source_pg_name'] = actual_pg
        else:
            raise RuntimeError(
                f"\n[BLOCKED] rds.logical_replication is '{val}' (not '1') on the actual PG '{actual_pg}'.\n"
                "The parameter group was updated but the database has NOT been rebooted yet, "
                "or the wrong PG is attached.\n"
                "ACTION REQUIRED: Reboot the database, wait for it to return to 'available', then re-run."
            )

    def trigger_deployment(self, state, target_version, target_pg_name, target_instance_pg_name=None):
        print("\n=== Phase 2: Triggering Blue/Green Deployment ===")
        self.wait_for_source_ready(state)
        self.verify_logical_replication_active(state)

        timestamp = datetime.utcnow().strftime('%Y%m%d%H%M')
        bg_name = f"{state['identifier']}-bg-{timestamp}"
        
        print(f"[*] Validating Deployment payload...")
        print(f"  -> Target Version: {target_version}")
        print(f"  -> Target Primary PG: {target_pg_name}")
        if target_instance_pg_name:
            print(f"  -> Target Instance PG: {target_instance_pg_name}")
        
        # --- THE FIX IS HERE: Changed 'SourceArn' to 'Source' ---
        kwargs = {
            'Source': state['source_arn'], 
            'BlueGreenDeploymentName': bg_name,
            'TargetEngineVersion': target_version
        }
        # --------------------------------------------------------
        
        if state['is_cluster']:
            kwargs['TargetDBClusterParameterGroupName'] = target_pg_name
            if target_instance_pg_name:
                kwargs['TargetDBParameterGroupName'] = target_instance_pg_name
        else:
            kwargs['TargetDBParameterGroupName'] = target_pg_name

        try:
            res = self.aws.rds.create_blue_green_deployment(**kwargs)
            bg_id = res['BlueGreenDeployment']['BlueGreenDeploymentIdentifier']
            print(f"\n  [SUCCESS] Deployment successfully triggered!")
            print(f"  [+] Deployment ID: {bg_id}")
            return bg_id
        except Exception as e:
            print(f"\n[FATAL ERROR] Failed to trigger deployment: {e}")
            raise

    def wait_for_deployment(self, bg_id):
        print(f"\n[*] Monitoring Deployment Status ({bg_id})...")
        print("  [INFO] This process provisions new hardware and syncs your data.")
        print("  [INFO] It typically takes 15 to 45 minutes. Grab a coffee!")
        start_time = time.time()
        while True:
            try:
                res = self.aws.rds.describe_blue_green_deployments(BlueGreenDeploymentIdentifier=bg_id)
                deployment = res['BlueGreenDeployments'][0]
            except Exception:
                print("  [!] Could not fetch deployment status. Retrying in 60s...")
                time.sleep(60)
                continue
                
            status = deployment['Status']
            elapsed_minutes = int((time.time() - start_time) / 60)
            
            if status == 'AVAILABLE':
                print(f"\n  [SUCCESS] The Green environment is now AVAILABLE and syncing!")
                print(f"  [+] Total provisioning time: {elapsed_minutes} minutes.")
                break
            elif status == 'PROVISIONING':
                print(f"  [-] [{elapsed_minutes}m elapsed] Status: PROVISIONING... building hardware.")
            elif status in ['FAILED', 'DELETING']:
                raise RuntimeError(f"Deployment entered a failure state: {status}")
            else:
                print(f"  [-] [{elapsed_minutes}m elapsed] Status: {status}...")
            time.sleep(60)