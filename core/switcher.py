import time

class SwitchoverManager:
    """Handles the high-risk Cutover phase with replication lag guardrails."""
    
    def __init__(self, aws_client):
        self.aws = aws_client

    def monitor_replication_lag(self, state, bg_id, max_lag_seconds=30):
        print("\n[*] Pre-Switch Safety Check: Monitoring Replication Lag...")
        
        # We need the Green identifier to check its CloudWatch metrics
        green_id = self.aws.get_green_target_identifier(bg_id)
        if not green_id:
            print("  [!] Could not parse Green identifier. Proceeding with caution.")
            return True

        print(f"  [-] Polling CloudWatch for '{green_id}' (Target lag: < {max_lag_seconds}s)")
        
        attempts = 0
        while attempts < 10:
            lag = self.aws.get_replica_lag(green_id, state['is_cluster'])
            
            if lag is None:
                print("  [-] CloudWatch metrics are still warming up... waiting 30s.")
            elif lag <= max_lag_seconds:
                print(f"  [SUCCESS] Replication lag is safe: {lag} seconds.")
                return True
            else:
                print(f"  [!] Lag is currently {lag} seconds. Waiting for it to drop...")
            
            time.sleep(30)
            attempts += 1
            
        print("  [WARNING] Replication lag did not drop below threshold in time.")
        return False

    def execute_switchover(self, bg_id, state, auto_stop_old_db=False): # <-- Added params
            print(f"\n=== Phase 4: Executing Blue/Green Switchover ===")
            
            # --- Wait for Green Target to be 'available' ---
            print("[*] Verifying Green target DB environment is fully available...")
            while True:
                deployment = self.aws.get_blue_green_deployment(bg_id)
                if not deployment:
                    print("  [!] Could not fetch deployment details. Waiting 15s...")
                    time.sleep(15)
                    continue
                
                # Check target members status
                switchover_details = deployment.get('SwitchoverDetails', [])
                all_available = True
                members_status = []
                for detail in switchover_details:
                    target_member_arn = detail.get('TargetMember')
                    if target_member_arn:
                        target_id = target_member_arn.split(':')[-1]
                        try:
                            if ':cluster:' in target_member_arn:
                                info = self.aws.rds.describe_db_clusters(DBClusterIdentifier=target_id)['DBClusters'][0]
                                status = info.get('Status', 'unknown')
                            else:
                                info = self.aws.rds.describe_db_instances(DBInstanceIdentifier=target_id)['DBInstances'][0]
                                status = info.get('DBInstanceStatus', 'unknown')
                        except Exception as e:
                            status = "unknown"
                        members_status.append(f"{target_id}:{status}")
                        if status.lower() != 'available':
                            all_available = False
                
                if not switchover_details:
                    # Fallback to green target identifier from ARN
                    green_id = self.aws.get_green_target_identifier(bg_id)
                    if green_id:
                        try:
                            if state['is_cluster']:
                                info = self.aws.get_cluster(green_id)
                                status = info.get('Status', 'unknown') if info else 'unknown'
                            else:
                                info = self.aws.get_instance(green_id)
                                status = info.get('DBInstanceStatus', 'unknown') if info else 'unknown'
                                
                            members_status.append(f"{green_id}:{status}")
                            if status.lower() != 'available':
                                all_available = False
                        except Exception:
                            all_available = False
                
                if all_available and members_status:
                    print(f"  [+] Green target environment is ready: {', '.join(members_status)}")
                    break
                else:
                    print(f"  [-] Waiting for Green environment to be available: {', '.join(members_status)}. Retrying in 15s...")
                    time.sleep(15)
            
            print("  [INFO] Initiating DNS swap. Write traffic will briefly pause.")
            
            try:
                self.aws.switchover_deployment(bg_id, timeout_seconds=300)
                print("  [+] Switchover command accepted by AWS!")
            except Exception as e:
                print(f"\n[FATAL ERROR] {e}")
                raise

            print("\n[*] Waiting for switchover to complete (this takes a few minutes)...")
            while True:
                deployment = self.aws.get_blue_green_deployment(bg_id)
                status = deployment.get('Status')
                
                if status == 'SWITCHOVER_COMPLETED':
                    print("\n  [SUCCESS] Switchover Complete! Your Green environment is now Production.")
                    break
                elif status in ['FAILED', 'SWITCHOVER_FAILED']:
                    raise RuntimeError("Switchover failed! AWS has rolled back. Blue is still active.")
                else:
                    print(f"  [-] Status: {status}... switching DNS records.")
                    time.sleep(30)

        # --- NEW: Post-Cutover Cleanup ---
            if auto_stop_old_db:
                print(f"\n[*] Post-Cutover: Deleting Blue/Green deployment mapping (keeping target)...")
                self.aws.delete_blue_green_deployment(bg_id, delete_target=False)

                print(f"\n[*] Post-Cutover: Auto-stopping the old database to save costs...")
                # AWS renames the original DB to include '-old1' (or similar)
                # We can find it by looking for the source of the deployment
                source_arn = deployment.get('Source')
                old_identifier = source_arn.split(':')[-1]
                
                print(f"  [-] Waiting for old database '{old_identifier}' to become 'available'...")
                while True:
                    try:
                        if state['is_cluster']:
                            info = self.aws.get_cluster(old_identifier)
                            status = info.get('Status') if info else None
                        else:
                            info = self.aws.get_instance(old_identifier)
                            status = info.get('DBInstanceStatus') if info else None
                        
                        if info is None:
                            print(f"  [!] Old database '{old_identifier}' could not be found.")
                            break
                        
                        if status and status.lower() == 'available':
                            print(f"  [+] Old database '{old_identifier}' is now 'available'.")
                            break
                        
                        print(f"  [-] Status of '{old_identifier}': {status or 'unknown'}. Waiting 30s...")
                        time.sleep(30)
                    except Exception as e:
                        print(f"  [!] Error checking status of '{old_identifier}': {e}. Waiting 30s...")
                        time.sleep(30)

                if self.aws.stop_database(old_identifier, state['is_cluster']):
                    print(f"  [SUCCESS] Shutdown signal sent to '{old_identifier}'. It will remain stopped for up to 7 days.")