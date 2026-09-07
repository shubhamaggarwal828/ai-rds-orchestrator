import time
from datetime import datetime
from botocore.exceptions import ClientError

class SnapshotManager:
    """Handles taking pre-flight backups of the database before making changes."""
    
    def __init__(self, aws_client):
        self.aws = aws_client

    def take_preflight_snapshot(self, state):
        print("\n=== Taking Pre-Flight Snapshot ===")
        
        timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M')
        snapshot_id = f"{state['identifier']}-preflight-{timestamp}".replace('--', '-')
        
        print(f"[*] Initiating snapshot: {snapshot_id}...")
        
        try:
            if state['is_cluster']:
                self.aws.create_cluster_snapshot(state['identifier'], snapshot_id)
                print("  [+] Aurora Cluster Snapshot initiated.")
            else:
                self.aws.create_instance_snapshot(state['identifier'], snapshot_id)
                print("  [+] RDS Instance Snapshot initiated.")
        except Exception as e:
            print(f"  [!] Failed to initiate snapshot: {e}")
            return None

        # --- NEW: Polling Loop to Wait for Completion ---
        print("  [*] Waiting for snapshot to complete (this ensures the DB is safe to reboot)...")
        start_time = time.time()
        
        while True:
            try:
                if state['is_cluster']:
                    res = self.aws.rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=snapshot_id)
                    status = res['DBClusterSnapshots'][0]['Status']
                else:
                    res = self.aws.rds.describe_db_snapshots(DBSnapshotIdentifier=snapshot_id)
                    status = res['DBSnapshots'][0]['Status']
                
                elapsed_minutes = int((time.time() - start_time) / 60)
                
                if status.lower() == 'available':
                    print(f"\n  [SUCCESS] Snapshot {snapshot_id} is fully backed up and available!")
                    break
                elif status.lower() in ['failed', 'error']:
                    raise RuntimeError(f"Snapshot failed with status: {status}")
                else:
                    print(f"  [-] [{elapsed_minutes}m elapsed] Snapshot status: {status}... waiting 30s")
                    time.sleep(30)
                    
            except ClientError as e:
                print(f"  [!] Error checking snapshot status: {e}. Retrying...")
                time.sleep(30)
                
        return snapshot_id