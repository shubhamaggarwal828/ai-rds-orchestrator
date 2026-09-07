class StagingManager:
    """Handles configuring the new Green environment to match the Blue environment perfectly."""
    
    def __init__(self, aws_client):
        self.aws = aws_client

    def restore_scaling_policies(self, state, bg_id):
        print("\n=== Phase 3: Staging & Restoration ===")
        
        if not state['is_cluster'] or not state.get('scaling_policies'):
            print("  [*] No Auto-Scaling policies to restore. Skipping Phase 3.")
            return

        # 1. Discover the Green Cluster ID
        print("[*] Locating Green cluster resources...")
        green_cluster_id = self.aws.get_green_target_identifier(bg_id)
        
        if not green_cluster_id:
            print("  [!] Could not locate Green cluster ID. Skipping scaling restoration.")
            return
            
        green_resource_id = f"cluster:{green_cluster_id}"
        print(f"  [+] Target Green Cluster: {green_cluster_id}")

        # 2. Restore the Base Scalable Target (Min/Max Nodes)
        targets = state['scaling_policies'].get('targets', [])
        for target in targets:
            min_cap = target['MinCapacity']
            max_cap = target['MaxCapacity']
            print(f"[*] Restoring Scalable Target Boundries: Min {min_cap} | Max {max_cap}")
            self.aws.register_scalable_target(green_resource_id, min_cap, max_cap)

        # 3. Dynamically loop through and restore ALL specific policies
        policies = state['scaling_policies'].get('policies', [])
        for policy in policies:
            policy_name = policy['PolicyName']
            policy_type = policy['PolicyType']
            
            # Extract the raw config dictionary. This handles whatever custom cooldowns,
            # target values, or metrics were set on the original database.
            config = policy.get('TargetTrackingScalingPolicyConfiguration')
            
            print(f"[*] Restoring Policy: '{policy_name}' ({policy_type})...")
            
            self.aws.put_scaling_policy(
                resource_id=green_resource_id,
                policy_name=policy_name,
                policy_type=policy_type,
                target_tracking_config=config
            )

        print("  [SUCCESS] All auto-scaling infrastructure restored to the Green environment!")