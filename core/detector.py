class EnvironmentDetector:
    """Discovers the state, architecture, and upgrade paths of the target database."""
    def __init__(self, aws_client):
        self.aws = aws_client

    def inspect(self, db_identifier):
        print(f"[*] Inspecting identifier: {db_identifier}...")
        

        state = {
            'identifier': db_identifier,
            'is_aurora': False,
            'is_cluster': False,
            'engine': None,
            'current_version': None,
            'source_pg_name': None,       # Primary PG (Cluster PG for Aurora, Instance PG for RDS)
            'source_pg_family': None,
            'source_arn': None,           # REQUIRED FOR DEPLOYMENT TRIGGER
            'aurora_instance_pgs': set(), # Tracks instance-level PGs for Aurora readers
            'scaling_policies': None      # Holds auto-scaling backup for Phase 3
        }

        # 2. Check if it's an Aurora Cluster
        cluster = self.aws.get_cluster(db_identifier)
        if cluster:
            # --- SECRETS MANAGER BLOCKER ---
            if cluster.get('MasterUserSecret'):
                raise RuntimeError(
                    f"\n[BLOCKED] Secrets Manager Integration Detected on '{db_identifier}'.\n"
                    "AWS Blue/Green deployments do not support clusters managed by RDS Secrets Manager.\n"
                    "ACTION REQUIRED: Modify the cluster to disable Secrets Manager, set a manual password, and try again."
                )

            state.update({
                'is_aurora': True,
                'is_cluster': True,
                'engine': cluster['Engine'],
                'current_version': cluster['EngineVersion'],
                'source_pg_name': cluster['DBClusterParameterGroup'],
                'source_arn': cluster['DBClusterArn'] 
            })
            print(f"  -> Detected: Aurora Cluster ({state['engine']} {state['current_version']})")
            
            # Extract Instance-level PGs used by the Writer and Readers
            members = cluster.get('DBClusterMembers', [])
            for member in members:
                instance = self.aws.get_instance(member['DBInstanceIdentifier'])
                if instance and instance.get('DBParameterGroups'):
                    state['aurora_instance_pgs'].add(instance['DBParameterGroups'][0]['DBParameterGroupName'])
            
            if state['aurora_instance_pgs']:
                print(f"  -> Detected Aurora Instance PGs: {', '.join(state['aurora_instance_pgs'])}")

            # Fetch Auto-Scaling Backup for Aurora
            scaling_data = self.aws.get_scaling_policies(db_identifier)
            if scaling_data:
                state['scaling_policies'] = scaling_data
                print(f"  -> Detected: Aurora Auto Scaling Policies (Backed up for post-cutover)")

        else:
            # 3. Check if it's a Standard RDS Instance
            instance = self.aws.get_instance(db_identifier)
            if not instance:
                raise ValueError(f"Could not find Cluster or Instance named '{db_identifier}'")
            
            # --- SECRETS MANAGER BLOCKER ---
            if instance.get('MasterUserSecret'):
                raise RuntimeError(
                    f"\n[BLOCKED] Secrets Manager Integration Detected on '{db_identifier}'.\n"
                    "AWS Blue/Green deployments do not support instances managed by RDS Secrets Manager.\n"
                    "ACTION REQUIRED: Modify the instance to disable Secrets Manager, set a manual password, and try again."
                )
            
            state.update({
                'engine': instance['Engine'],
                'current_version': instance['EngineVersion'],
                'source_pg_name': instance['DBParameterGroups'][0]['DBParameterGroupName'],
                'is_aurora': 'aurora' in instance['Engine'],
                'source_arn': instance['DBInstanceArn'] 
            })
            print(f"  -> Detected: RDS Instance ({state['engine']} {state['current_version']})")

        # 4. Get the Parameter Group Family for the primary PG
        state['source_pg_family'] = self.aws.get_pg_family(state['source_pg_name'], state['is_cluster'])
        print(f"  -> Current Primary PG: {state['source_pg_name']} ({state['source_pg_family']})")
        
        return state

    def get_upgrade_target(self, engine, current_version, desired_version=None):
        """Finds or validates the target version for upgrade."""
        info = self.aws.get_engine_versions(engine, current_version)
        valid_upgrades = info.get('ValidUpgradeTarget', [])

        if not valid_upgrades:
            raise ValueError(f"No upgrades of any kind are currently available from {current_version}.")

        if desired_version:
            desired_str = str(desired_version).strip()
            print(f"[*] Validating requested upgrade path: {current_version} -> {desired_str}...")
            
            # 1. Exact match
            for upgrade in valid_upgrades:
                if upgrade['EngineVersion'] == desired_str:
                    target_info = self.aws.get_engine_versions(engine, desired_str)
                    target_family = target_info['DBParameterGroupFamily']
                    return desired_str, target_family
            
            # 2. Major/prefix match (e.g. "18" matching "18.3" or "18.4")
            prefix_matches = [
                u['EngineVersion'] for u in valid_upgrades
                if u['EngineVersion'].startswith(f"{desired_str}.") or u['EngineVersion'].startswith(desired_str)
            ]
            if prefix_matches:
                selected = prefix_matches[-1]
                target_info = self.aws.get_engine_versions(engine, selected)
                target_family = target_info['DBParameterGroupFamily']
                return selected, target_family
                    
            allowed_list = [v['EngineVersion'] for v in valid_upgrades]
            raise ValueError(f"Invalid upgrade target '{desired_str}' from {current_version}. Valid targets: {', '.join(allowed_list)}")

        else:
            # Pick highest major version upgrade if present, else highest available version
            major_upgrades = [v['EngineVersion'] for v in valid_upgrades if v.get('IsMajorVersionUpgrade')]
            if major_upgrades:
                target_version = major_upgrades[-1]
            else:
                target_version = valid_upgrades[-1]['EngineVersion']
                
            target_info = self.aws.get_engine_versions(engine, target_version)
            target_family = target_info['DBParameterGroupFamily']
            print(f"[*] Auto-selected Target Version: {target_version} ({target_family})")
            return target_version, target_family