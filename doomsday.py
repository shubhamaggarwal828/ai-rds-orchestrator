import time
import boto3
from botocore.exceptions import ClientError

class DoomsdayRollback:
    def __init__(self, region_name=None):
        session = boto3.Session(region_name=region_name)
        self.region = session.region_name
        self.rds = session.client('rds')
        print(f"[!] DOOMSDAY PROTOCOL INITIATED in {self.region}")

    def unlock_blue_green_deployment(self, base_identifier):
        """Finds active B/G deployments holding the DBs hostage and deletes the record."""
        print("\n[*] PRE-CHECK: Scanning for active Blue/Green deployments locking the instances...")
        try:
            bgs = self.rds.describe_blue_green_deployments()['BlueGreenDeployments']
            for bg in bgs:
                bg_id = bg['BlueGreenDeploymentIdentifier']
                
                if base_identifier in bg['BlueGreenDeploymentName'] or \
                   (bg.get('Source') and base_identifier in bg['Source']) or \
                   (bg.get('Target') and base_identifier in bg['Target']):
                    
                    print(f"  [!] Found locking B/G Deployment: {bg['BlueGreenDeploymentName']} ({bg_id})")
                    print("  [!] Deleting B/G deployment record to unlock instances (DBs will NOT be deleted)...")
                    
                    self.rds.delete_blue_green_deployment(
                        BlueGreenDeploymentIdentifier=bg_id,
                        DeleteTarget=False
                    )
                    
                    while True:
                        try:
                            self.rds.describe_blue_green_deployments(BlueGreenDeploymentIdentifier=bg_id)
                            print("  [*] Waiting for B/G deployment record to be deleted by AWS...")
                            time.sleep(10)
                        except ClientError as e:
                            if 'BlueGreenDeploymentNotFoundFault' in str(e):
                                break
                            raise
                    print("  [+] B/G Deployment record deleted. Database namespaces are unlocked.")
                    return
            print("  [+] No active B/G deployment locks found.")
        except ClientError as e:
            print(f"  [!] Warning: Could not check B/G deployments: {e}")

    def ensure_available(self, identifier, is_cluster, is_rename_target=False):
        """Ensures the database is not stopped or modifying before we try to rename it."""
        print(f"\n[*] Checking state of '{identifier}'...")
        not_found_retries = 20 # Allow up to 5 minutes for AWS to register a new name
        
        while True:
            try:
                if is_cluster:
                    info = self.rds.describe_db_clusters(DBClusterIdentifier=identifier)['DBClusters'][0]
                    status = info['Status']
                else:
                    info = self.rds.describe_db_instances(DBInstanceIdentifier=identifier)['DBInstances'][0]
                    status = info['DBInstanceStatus']
                
                if status == 'available':
                    print(f"  [+] '{identifier}' is available.")
                    return
                elif status == 'stopped':
                    print(f"  [!] '{identifier}' is stopped. Booting it up...")
                    if is_cluster:
                        self.rds.start_db_cluster(DBClusterIdentifier=identifier)
                    else:
                        self.rds.start_db_instance(DBInstanceIdentifier=identifier)
                
                print(f"  [*] Status is '{status}'. Waiting 15s...")
                time.sleep(15)
            except ClientError as e:
                if 'NotFound' in str(e):
                    # If we just renamed it, AWS takes time to populate the new name in the API
                    if is_rename_target and not_found_retries > 0:
                        print(f"  [*] AWS hasn't registered '{identifier}' yet. Waiting 15s...")
                        not_found_retries -= 1
                        time.sleep(15)
                        continue
                    else:
                        raise RuntimeError(f"Database '{identifier}' not found! Cannot verify status.")
                raise

    def get_old_identifier(self, base_identifier, is_cluster):
        """Finds the '-old' database that AWS left behind after the switch."""
        matches = []
        try:
            if is_cluster:
                clusters = self.rds.describe_db_clusters()['DBClusters']
                for c in clusters:
                    if c['DBClusterIdentifier'].startswith(f"{base_identifier}-old"):
                        matches.append(c['DBClusterIdentifier'])
            else:
                instances = self.rds.describe_db_instances()['DBInstances']
                for i in instances:
                    if i['DBInstanceIdentifier'].startswith(f"{base_identifier}-old"):
                        matches.append(i['DBInstanceIdentifier'])
            
            if matches:
                return sorted(matches)[-1]
            return None
        except ClientError as e:
            print(f"[FATAL] Could not scan for old databases: {e}")
            return None

    def execute_rollback(self, base_identifier, is_cluster=False):
        print(f"\n[*] Target Base Identifier: {base_identifier}")
        print(f"[*] Architecture: {'Aurora Cluster' if is_cluster else 'RDS Instance'}")
        
        # 0. Clean up B/G Locks
        self.unlock_blue_green_deployment(base_identifier)

        # 1. Find the old Blue database
        old_id = self.get_old_identifier(base_identifier, is_cluster)
        if not old_id:
            raise RuntimeError(
                f"Could not find an old database matching '{base_identifier}-old'.\n"
                f"  -> Check the AWS Console. If the switchover failed entirely, the original database might still be named '{base_identifier}'."
            )
        print(f"  [+] Found original Blue database: {old_id}")

        # 2. Rename the broken Green database out of the way
        failed_id = f"{base_identifier}-failed-upgrade"
        print(f"\n[*] STEP 1: Moving broken Green database ({base_identifier} -> {failed_id})...")
        
        try:
            # We wrap this in a try/catch. If it fails because base_identifier isn't found, 
            # it likely means STEP 1 succeeded previously and we just need to verify the failed_id state.
            if is_cluster:
                self.rds.modify_db_cluster(
                    DBClusterIdentifier=base_identifier,
                    NewDBClusterIdentifier=failed_id,
                    ApplyImmediately=True
                )
            else:
                self.rds.modify_db_instance(
                    DBInstanceIdentifier=base_identifier,
                    NewDBInstanceIdentifier=failed_id,
                    ApplyImmediately=True
                )
            print("  [*] Rename initiated. Polling until namespace is completely freed...")
        except ClientError as e:
            if 'NotFound' in str(e):
                print(f"  [*] '{base_identifier}' not found. Assuming STEP 1 rename is already in progress or completed.")
            else:
                raise RuntimeError(f"Failed to rename broken database: {e}")

        # Wait for the newly named "failed" DB to become available
        self.ensure_available(failed_id, is_cluster, is_rename_target=True) 

        # 3. Ensure the old DB is available before renaming
        self.ensure_available(old_id, is_cluster)

        # 4. Rename the original Blue database back to the primary identifier
        print(f"\n[*] STEP 2: Restoring original Blue database ({old_id} -> {base_identifier})...")
        try:
            if is_cluster:
                self.rds.modify_db_cluster(
                    DBClusterIdentifier=old_id,
                    NewDBClusterIdentifier=base_identifier,
                    ApplyImmediately=True
                )
            else:
                self.rds.modify_db_instance(
                    DBInstanceIdentifier=old_id,
                    NewDBInstanceIdentifier=base_identifier,
                    ApplyImmediately=True
                )
            print("  [*] Restore initiated. Polling until original namespace is active...")
        except ClientError as e:
            raise RuntimeError(f"Failed to restore old database. NAMESPACE COLLISION LIKELY: {e}")

        # Wait for the restored DB to become available under its proper name
        self.ensure_available(base_identifier, is_cluster, is_rename_target=True)

        print("\n" + "="*40)
        print("=== DOOMSDAY ROLLBACK COMPLETE ===")
        print("="*40)
        print(f"[INFO] The application should automatically reconnect to the old database shortly.")
        print(f"[INFO] The failed upgrade is preserved as: {failed_id}")

if __name__ == "__main__":
    # ==========================================
    # --- EMERGENCY CONFIGURATION ---
    # ==========================================
    TARGET_DB = "test-aurora-multi"
    IS_AURORA_CLUSTER = True
    AWS_REGION = None 
    # ==========================================
    
    print("\n" + "!"*50)
    print("WARNING: DOOMSDAY ROLLBACK SCRIPT")
    print("This will forcefully swap database identifiers.")
    print("Any data written to the new database in the last few minutes will be abandoned.")
    print("!"*50 + "\n")
    
    confirm = input(f"Are you ABSOLUTELY sure you want to rollback '{TARGET_DB}'? (Type 'ROLLBACK'): ")
    
    if confirm == 'ROLLBACK':
        doomsday = DoomsdayRollback(region_name=AWS_REGION)
        try:
            doomsday.execute_rollback(TARGET_DB, is_cluster=IS_AURORA_CLUSTER)
        except Exception as e:
            print(f"\n[ROLLBACK FAILED] {e}")
    else:
        print("Rollback aborted.")