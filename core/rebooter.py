import time
from botocore.exceptions import ClientError

class RebootOrchestrator:
    """Handles safely rebooting instances and clusters to apply pending parameters."""
    
    def __init__(self, aws_client):
        self.aws = aws_client

    def _wait_for_instance(self, instance_id):
        """Polls the instance until it is in the 'available' state before rebooting."""
        while True:
            try:
                res = self.aws.rds.describe_db_instances(DBInstanceIdentifier=instance_id)
                status = res['DBInstances'][0]['DBInstanceStatus']
                
                if status.lower() == 'available':
                    return True
                    
                print(f"  [-] Instance {instance_id} is '{status}'. Waiting 30s before rebooting...")
                time.sleep(30)
            except Exception as e:
                print(f"  [!] Error checking status for {instance_id}: {e}")
                time.sleep(30)

    def _wait_for_reboot_complete(self, instance_id):
        """Waits for a reboot to fully complete.
        
        Sleeps briefly first so the status has time to transition away from
        'available' into 'rebooting', then polls until it returns to 'available'.
        Without this, the deployer's wait_for_source_ready() can race ahead and
        trigger B/G before pending parameters (e.g. rds.logical_replication) are live.
        """
        print(f"  [*] Waiting for reboot to complete on {instance_id}...")
        time.sleep(15)  # Give the API time to transition status away from 'available'
        while True:
            try:
                res = self.aws.rds.describe_db_instances(DBInstanceIdentifier=instance_id)
                status = res['DBInstances'][0]['DBInstanceStatus']
                if status.lower() == 'available':
                    print(f"  [+] {instance_id} is back online and 'available'.")
                    return
                print(f"  [-] {instance_id} status: '{status}'. Waiting 20s...")
                time.sleep(20)
            except Exception as e:
                print(f"  [!] Error checking reboot status for {instance_id}: {e}")
                time.sleep(20)

    def execute_reboot(self, state):
        print("\n=== Starting Reboot Orchestration ===")
        
        if state['is_cluster']:
            print(f"[*] Discovering Aurora cluster members for '{state['identifier']}'...")
            res = self.aws.rds.describe_db_clusters(DBClusterIdentifier=state['identifier'])
            members = res['DBClusters'][0].get('DBClusterMembers', [])
            
            # Sort to reboot writers first, then readers to minimize failover chaos
            writers = [m for m in members if m['IsClusterWriter']]
            readers = [m for m in members if not m['IsClusterWriter']]
            
            for w in writers:
                instance_id = w['DBInstanceIdentifier']
                print(f"[*] Rebooting Writer Node: {instance_id}...")
                self._wait_for_instance(instance_id)
                
                try:
                    self.aws.rds.reboot_db_instance(DBInstanceIdentifier=instance_id)
                    print("  [+] Reboot command issued.")
                    self._wait_for_reboot_complete(instance_id)
                except ClientError as e:
                    print(f"  [!] Failed to reboot {instance_id}: {e}")
                
            if readers:
                print("  [-] Pausing for 10 seconds before cycling Readers...")
                time.sleep(10)
                for r in readers:
                    instance_id = r['DBInstanceIdentifier']
                    print(f"[*] Rebooting Reader Node: {instance_id}...")
                    self._wait_for_instance(instance_id)
                    
                    try:
                        self.aws.rds.reboot_db_instance(DBInstanceIdentifier=instance_id)
                        print("  [+] Reboot command issued.")
                        self._wait_for_reboot_complete(instance_id)
                    except ClientError as e:
                        print(f"  [!] Failed to reboot {instance_id}: {e}")
                    
            print("  [+] All cluster reboot commands issued.")
            
        else:
            # Standard RDS Logic
            instance_id = state['identifier']
            print(f"[*] Rebooting standard RDS instance: {instance_id}...")
            
            # Wait for any modifying tasks to finish first!
            self._wait_for_instance(instance_id)
            
            try:
                self.aws.rds.reboot_db_instance(DBInstanceIdentifier=instance_id)
                print("  [+] Reboot command issued.")
                self._wait_for_reboot_complete(instance_id)
            except ClientError as e:
                raise RuntimeError(f"Failed to reboot {instance_id}: {e}")