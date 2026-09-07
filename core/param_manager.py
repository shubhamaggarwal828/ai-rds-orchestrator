class ParameterManager:
    """Manages creation, modification, and Blue/Green edge cases for Parameter Groups."""
    
    # 1. The "Must Have" static enforcements
    STATIC_ENFORCEMENTS = {
        'rds.logical_replication': '1',
        'synchronous_commit': 'on',
        'rds.force_ssl': '0',
        'idle_in_transaction_session_timeout': '60000', # 1 minute to prevent pg_upgrade lock timeouts
        'wal_sender_timeout': '0'  # CRITICAL: Prevents logical replication sender from timing out during B/G provisioning
    }

    # 2. Replication parameters that need a healthy minimum for B/G to survive
    MINIMUM_THRESHOLDS = {
        'max_replication_slots': 20,
        'max_wal_senders': 20,
        'max_logical_replication_workers': 8,
        'max_worker_processes': 24 # <-- THE FIX: Critical for Postgres logical replication sync
    }

    # 3. Parameters that MUST carry over exactly from source to target to prevent app crashes
    CARRY_OVER_PARAMS = [
        'password_encryption',
        'track_commit_timestamp',
        'shared_buffers', 
        'work_mem',
        'shared_preload_libraries', # CRITICAL: Carries over pg_cron, pgaudit, partman, etc.
        'track_activity_query_size',
        # --- AURORA SPECIFIC CATCHES ---
        'rds.babelfish_status',
        'apg_ccm_enabled',
        'aurora_replica_read_consistency'
    ]

    def __init__(self, aws_client):
        self.aws = aws_client

    def _get_current_params(self, pg_name, is_cluster):
        """Helper to fetch all parameters for evaluation."""
        paginator = self.aws.rds.get_paginator(
            'describe_db_cluster_parameters' if is_cluster else 'describe_db_parameters'
        )
        kwargs = {'DBClusterParameterGroupName': pg_name} if is_cluster else {'DBParameterGroupName': pg_name}
        
        params = {}
        for page in paginator.paginate(**kwargs):
            for p in page['Parameters']:
                params[p['ParameterName']] = p
        return params

    def enforce_bg_rules(self, target_pg_name, is_cluster, source_pg_name=None):
        """Evaluates and pushes all B/G safety rules to the target parameter group."""
        updates = []
        target_params = self._get_current_params(target_pg_name, is_cluster)
        
        source_params = {}
        if source_pg_name:
            source_params = self._get_current_params(source_pg_name, is_cluster)

        # 1. Apply Static Enforcements
        for param, val in self.STATIC_ENFORCEMENTS.items():
            if param in target_params and target_params[param].get('IsModifiable', True):
                updates.append({'ParameterName': param, 'ParameterValue': val, 'ApplyMethod': 'pending-reboot'})
                print(f"  [+] Enforcing Static Rule: {param} = {val}")

        # 2. Apply Minimum Thresholds
        for param, min_val in self.MINIMUM_THRESHOLDS.items():
            if param in target_params and target_params[param].get('IsModifiable', True):
                current_val = target_params[param].get('ParameterValue')
                is_below = False
                if not current_val:
                    is_below = True
                elif str(current_val).isdigit():
                    if int(current_val) < min_val:
                        is_below = True
                else:
                    # ParameterValue is a formula or non-numeric string (e.g., "GREATEST({DBInstanceVCPU*2},8)")
                    # We default to applying the threshold to ensure compliance
                    is_below = True
                    print(f"  [!] Non-numeric parameter value found for {param}: '{current_val}'. Defaulting to threshold.")

                if is_below:
                    updates.append({'ParameterName': param, 'ParameterValue': str(min_val), 'ApplyMethod': 'pending-reboot'})
                    print(f"  [+] Bumping Threshold: {param} from {current_val} to {min_val}")

        # 3. Handle Carry-Overs & Memory Edge Cases
        if source_params:
            for param in self.CARRY_OVER_PARAMS:
                if param in source_params and param in target_params:
                    source_val = source_params[param].get('ParameterValue')
                    
                    if param == 'shared_buffers' and source_val and source_val.isdigit():
                        print(f"  [WARNING] 'shared_buffers' is hardcoded to {source_val}. If this consumes >75% RAM, pg_upgrade will fail.")
                    
                    if source_val and source_val != target_params[param].get('ParameterValue'):
                        updates.append({'ParameterName': param, 'ParameterValue': source_val, 'ApplyMethod': 'pending-reboot'})
                        print(f"  [+] Carrying Over: {param} = {source_val}")

        # Execute Updates
        if updates:
            self.aws.modify_parameters(target_pg_name, updates, is_cluster)
            print(f"  [SUCCESS] Pushed {len(updates)} safety parameters to {target_pg_name}")
        else:
            print(f"  [*] No parameter updates required for {target_pg_name}")

    def handle_default_source_group(self, state):
        """Clones a default group so we can enable logical replication."""
        if not state['source_pg_name'].startswith('default.'):
            print("\n[*] Source is using a custom PG. Applying B/G rules directly.")
            self.enforce_bg_rules(state['source_pg_name'], state['is_cluster'], source_pg_name=state['source_pg_name'])
            return

        print("\n[!] Source uses a default PG. Creating a custom clone for B/G...")
        safe_version = str(state['current_version']).replace('.', '-')
        custom_name = f"{state['identifier']}-pg-{safe_version}"
        
        created = self.aws.create_parameter_group(
            custom_name, state['source_pg_family'], "Custom source for B/G", state['is_cluster']
        )
        
        if created:
            # PUSH RULES FIRST (State-Lock Fix)
            print(f"  [*] Configuring new group {custom_name} before attaching...")
            self.enforce_bg_rules(custom_name, state['is_cluster'], source_pg_name=None)
            
            # ATTACH SECOND
            self.aws.attach_parameter_group(state['identifier'], custom_name, state['is_cluster'])
            print(f"  [+] Attached fully configured custom source group: {custom_name}")
            print("  [CRITICAL] You MUST manually reboot the database to apply logical_replication=1.")
        else:
            # Group already existed — still enforce rules on it
            self.enforce_bg_rules(custom_name, state['is_cluster'], source_pg_name=None)

        # BUG FIX: Update state so prep_target_group and the deployer use the
        # new custom PG name, not the default one that B/G cannot accept as source.
        state['source_pg_name'] = custom_name

    def prep_target_group(self, state, target_version, target_family):
        """Creates and preps the target parameter group for the new version."""
        safe_version = str(target_version).replace('.', '-')
        
        # 1. Prep the Primary Group (RDS Instance or Aurora Cluster)
        target_pg_name = f"{state['identifier']}-pg-{safe_version}"
        print(f"\n[*] Prepping Target Primary PG: {target_pg_name} ({target_family})...")
        
        created = self.aws.create_parameter_group(
            target_pg_name, target_family, f"Target PG for {target_version}", state['is_cluster']
        )
        
        if not created:
            print(f"  [-] Group {target_pg_name} already exists. Enforcing rules anyway.")
            
        self.enforce_bg_rules(target_pg_name, state['is_cluster'], source_pg_name=state['source_pg_name'])
        
        # 2. Prep Aurora Instance-Level Groups (if any)
        target_instance_pg_name = None
        if state['is_aurora'] and state['is_cluster'] and state.get('aurora_instance_pgs'):
            print(f"\n[*] Prepping Aurora Instance-Level PGs...")
            
            # BUG FIX: Instance-level PGs need the instance family, not the cluster family.
            # Fetch it from the first non-default instance PG to get the right family string.
            instance_target_family = self.aws.get_instance_pg_family_for_version(
                state['engine'], target_version
            )
            
            for source_instance_pg in state['aurora_instance_pgs']:
                # Skip default instance groups, AWS handles those automatically
                if source_instance_pg.startswith('default.'):
                    print(f"  [-] Skipping default instance group: {source_instance_pg}")
                    continue
                
                # Create a matching target instance group
                target_instance_pg_name = f"{source_instance_pg}-pg-{safe_version}"
                print(f"  [*] Prepping Instance Target: {target_instance_pg_name} ({instance_target_family})...")
                
                # Instance PGs are ALWAYS is_cluster=False, even for Aurora
                created_inst = self.aws.create_parameter_group(
                    target_instance_pg_name, instance_target_family, f"Target Instance PG for {target_version}", False
                )
                
                # Enforce rules and carry over memory settings
                self.enforce_bg_rules(target_instance_pg_name, False, source_pg_name=source_instance_pg)

        return target_pg_name, target_instance_pg_name