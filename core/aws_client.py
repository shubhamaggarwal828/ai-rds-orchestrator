import boto3
from botocore.exceptions import ClientError
import datetime

class AWSClient:
    """Wraps boto3 calls to keep AWS logic isolated."""
    def __init__(self, region_name=None):
        # Create a session. If region_name is None, Boto3 automatically uses 
        # your AWS CLI default region or the AWS_DEFAULT_REGION environment variable.
        session = boto3.Session(region_name=region_name)
        
        self.region = session.region_name
        if not self.region:
            raise ValueError("\n[FATAL ERROR] No AWS region found! Please configure your AWS CLI or pass a region explicitly.")
            
        print(f"[*] AWS Session established in region: {self.region}")
        
        self.rds = session.client('rds')
        self.app_autoscaling = session.client('application-autoscaling')
        self.cloudwatch = session.client('cloudwatch')

    def list_all_databases(self):
        """Discovers all Aurora clusters and standalone RDS instances with live Blue/Green deployment tracking."""
        databases = []
        cluster_members = set()
        green_targets = set()

        # 0. Scan Live AWS Blue/Green Deployments
        bg_by_source = {}
        completed_bgs = {}
        try:
            bgs = self.rds.describe_blue_green_deployments().get('BlueGreenDeployments', [])
            for bg in bgs:
                src_arn = bg.get('Source', '')
                tgt_arn = bg.get('Target', '')
                src_id = src_arn.split(':')[-1] if src_arn else ''
                tgt_id = tgt_arn.split(':')[-1] if tgt_arn else ''
                bg_status = bg.get('Status')

                bg_info = {
                    'bg_id': bg['BlueGreenDeploymentIdentifier'],
                    'bg_name': bg['BlueGreenDeploymentName'],
                    'status': bg_status,
                    'source_id': src_id,
                    'target_arn': tgt_arn,
                    'target_id': tgt_id,
                    'tasks': bg.get('Tasks', []),
                    'target_version': bg.get('TargetEngineVersion')
                }

                if bg_status in ['PROVISIONING', 'AVAILABLE', 'SWITCHOVER_IN_PROGRESS']:
                    if tgt_id:
                        green_targets.add(tgt_id)
                    if src_id:
                        bg_by_source[src_id] = bg_info
                else:
                    # Completed, deleting, or cleaned up B/G deployments
                    if tgt_id:
                        completed_bgs[tgt_id] = bg_info
                    if src_id:
                        completed_bgs[src_id] = bg_info
                    if src_id.endswith('-old1'):
                        base_name = src_id[:-5]
                        completed_bgs[base_name] = bg_info
        except Exception as e:
            print(f"  [!] Warning: Could not describe B/G deployments: {e}")

        # 1. Discover Aurora Clusters
        try:
            clusters_resp = self.rds.describe_db_clusters()
            for c in clusters_resp.get('DBClusters', []):
                c_id = c['DBClusterIdentifier']
                if c_id in green_targets:
                    continue # Exclude in-flight Green replica targets

                for m in c.get('DBClusterMembers', []):
                    cluster_members.add(m.get('DBInstanceIdentifier'))
                
                bg_info = bg_by_source.get(c_id) or completed_bgs.get(c_id)
                bg_status = bg_info['status'] if bg_info else None
                has_active = bool(bg_status and bg_status not in ['SWITCHOVER_COMPLETED', 'DELETED', 'FAILED'])
                has_completed = bool(bg_status == 'SWITCHOVER_COMPLETED')
                is_old = bool('-old' in c_id)

                databases.append({
                    'id': c_id,
                    'type': 'Aurora Cluster',
                    'is_cluster': True,
                    'is_aurora': True,
                    'engine': c['Engine'],
                    'version': c['EngineVersion'],
                    'status': f"B/G {bg_status}" if has_active else ('Upgraded' if has_completed else c['Status']),
                    'aws_status': c['Status'],
                    'parameter_group': c.get('DBClusterParameterGroup'),
                    'arn': c.get('DBClusterArn'),
                    'multi_az': c.get('MultiAZ', True),
                    'members_count': len(c.get('DBClusterMembers', [])),
                    'endpoint': c.get('Endpoint'),
                    'has_active_bg': has_active,
                    'has_completed_bg': has_completed,
                    'is_old_instance': is_old,
                    'bg_deployment': bg_info
                })
        except Exception as e:
            print(f"  [!] Warning: Could not list DB clusters: {e}")

        # 2. Discover Standalone RDS Instances (excluding cluster members & in-flight Green targets)
        try:
            instances_resp = self.rds.describe_db_instances()
            for inst in instances_resp.get('DBInstances', []):
                inst_id = inst['DBInstanceIdentifier']
                if inst_id in cluster_members or inst.get('DBClusterIdentifier') or inst_id in green_targets:
                    continue # Exclude Aurora cluster members and in-flight Green deployment targets
                
                bg_info = bg_by_source.get(inst_id) or completed_bgs.get(inst_id)
                bg_status = bg_info['status'] if bg_info else None
                has_active = bool(bg_status and bg_status not in ['SWITCHOVER_COMPLETED', 'DELETED', 'FAILED'])
                has_completed = bool(bg_status == 'SWITCHOVER_COMPLETED')
                is_old = bool('-old' in inst_id)

                pg_name = inst['DBParameterGroups'][0]['DBParameterGroupName'] if inst.get('DBParameterGroups') else 'default'
                databases.append({
                    'id': inst_id,
                    'type': 'RDS Instance',
                    'is_cluster': False,
                    'is_aurora': 'aurora' in inst.get('Engine', ''),
                    'engine': inst['Engine'],
                    'version': inst['EngineVersion'],
                    'status': f"B/G {bg_status}" if has_active else ('Upgraded' if has_completed else inst['DBInstanceStatus']),
                    'aws_status': inst['DBInstanceStatus'],
                    'parameter_group': pg_name,
                    'arn': inst.get('DBInstanceArn'),
                    'multi_az': inst.get('MultiAZ', False),
                    'instance_class': inst.get('DBInstanceClass'),
                    'allocated_storage': inst.get('AllocatedStorage'),
                    'endpoint': inst.get('Endpoint', {}).get('Address') if isinstance(inst.get('Endpoint'), dict) else inst.get('Endpoint'),
                    'has_active_bg': has_active,
                    'has_completed_bg': has_completed,
                    'is_old_instance': is_old,
                    'bg_deployment': bg_info
                })
        except Exception as e:
            print(f"  [!] Warning: Could not list DB instances: {e}")

        return databases

    def get_cluster(self, identifier):
        try:
            return self.rds.describe_db_clusters(DBClusterIdentifier=identifier)['DBClusters'][0]
        except ClientError as e:
            if e.response['Error']['Code'] in ['DBClusterNotFoundFault', 'DBClusterNotFound']:
                return None
            raise

    def get_instance(self, identifier):
        try:
            return self.rds.describe_db_instances(DBInstanceIdentifier=identifier)['DBInstances'][0]
        except ClientError as e:
            if e.response['Error']['Code'] in ['DBInstanceNotFoundFault', 'DBInstanceNotFound']:
                return None
            raise


    def get_pg_family(self, pg_name, is_cluster=False):
        if is_cluster:
            res = self.rds.describe_db_cluster_parameter_groups(DBClusterParameterGroupName=pg_name)
            return res['DBClusterParameterGroups'][0]['DBParameterGroupFamily']
        else:
            res = self.rds.describe_db_parameter_groups(DBParameterGroupName=pg_name)
            return res['DBParameterGroups'][0]['DBParameterGroupFamily']

    def get_engine_versions(self, engine, version=None):
        if version:
            return self.rds.describe_db_engine_versions(Engine=engine, EngineVersion=version)['DBEngineVersions'][0]
        return self.rds.describe_db_engine_versions(Engine=engine)['DBEngineVersions']

    def get_instance_pg_family_for_version(self, engine, version):
        """Returns the instance-level DB parameter group family for an Aurora engine+version.
        
        Aurora cluster PG families (e.g. 'aurora-postgresql16') differ from instance PG
        families in some representations. This fetches the canonical instance family directly
        from the describe_db_engine_versions API so we always pass the correct family when
        creating instance-level parameter groups.
        """
        # For Aurora, the instance family is fetched via the engine without the '-cluster' suffix
        instance_engine = engine.replace('-cluster', '') if '-cluster' in engine else engine
        result = self.rds.describe_db_engine_versions(
            Engine=instance_engine, EngineVersion=version
        )['DBEngineVersions']
        if not result:
            raise ValueError(f"Could not resolve instance PG family for {instance_engine} {version}")
        return result[0]['DBParameterGroupFamily']


    def get_runtime_parameter_value(self, pg_name, param_name, is_cluster=False):
        """Reads the live, in-effect value of a parameter from the parameter group.
        
        A parameter set with ApplyMethod='pending-reboot' will still show its OLD value
        here until the instance is rebooted. This is the key check before triggering B/G —
        if the value is still the default (not '1'), the reboot hasn't taken effect yet.
        """
        paginator = self.rds.get_paginator(
            'describe_db_cluster_parameters' if is_cluster else 'describe_db_parameters'
        )
        kwargs = {'DBClusterParameterGroupName': pg_name} if is_cluster else {'DBParameterGroupName': pg_name}
        for page in paginator.paginate(**kwargs):
            for p in page['Parameters']:
                if p['ParameterName'] == param_name:
                    return p.get('ParameterValue')
        return None

    def check_parameter_exists(self, pg_name, param_name, is_cluster=False):
        paginator = self.rds.get_paginator(
            'describe_db_cluster_parameters' if is_cluster else 'describe_db_parameters'
        )
        kwargs = {'DBClusterParameterGroupName': pg_name} if is_cluster else {'DBParameterGroupName': pg_name}
        
        for page in paginator.paginate(**kwargs):
            if any(p['ParameterName'] == param_name for p in page['Parameters']):
                return True
        return False

    def modify_parameters(self, pg_name, parameters, is_cluster=False):
        if is_cluster:
            self.rds.modify_db_cluster_parameter_group(DBClusterParameterGroupName=pg_name, Parameters=parameters)
        else:
            self.rds.modify_db_parameter_group(DBParameterGroupName=pg_name, Parameters=parameters)

    def create_parameter_group(self, pg_name, family, description, is_cluster=False):
        try:
            if is_cluster:
                self.rds.create_db_cluster_parameter_group(DBClusterParameterGroupName=pg_name, DBParameterGroupFamily=family, Description=description)
            else:
                self.rds.create_db_parameter_group(DBParameterGroupName=pg_name, DBParameterGroupFamily=family, Description=description)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'DBParameterGroupAlreadyExistsFault':
                return False
            raise
            
    def attach_parameter_group(self, identifier, pg_name, is_cluster=False):
        if is_cluster:
            self.rds.modify_db_cluster(
                DBClusterIdentifier=identifier,
                DBClusterParameterGroupName=pg_name,
                ApplyImmediately=True
            )
        else:
            # ApplyImmediately=True ensures the PG association is reflected in
            # describe_db_instances immediately so downstream checks and the B/G
            # API call see the correct custom PG, not the stale default.
            self.rds.modify_db_instance(
                DBInstanceIdentifier=identifier,
                DBParameterGroupName=pg_name,
                ApplyImmediately=True
            )

    # --- METHODS FOR AURORA REBOOTS & PRE-FLIGHT CHECKS ---
    def get_cluster_members(self, cluster_identifier):
        """Returns a list of all DB instances (Writer and Readers) in an Aurora Cluster."""
        try:
            cluster = self.rds.describe_db_clusters(DBClusterIdentifier=cluster_identifier)['DBClusters'][0]
            return cluster.get('DBClusterMembers', [])
        except self.rds.exceptions.DBClusterNotFoundFault:
            return []

    def reboot_instance(self, instance_identifier):
        """Issues a reboot command to a specific DB instance."""
        try:
            self.rds.reboot_db_instance(DBInstanceIdentifier=instance_identifier)
            return True
        except Exception as e:
            print(f"  [!] Failed to reboot {instance_identifier}: {e}")
            return False

    def get_scaling_policies(self, identifier):
        """Fetches the Application Auto Scaling targets and policies for an Aurora cluster."""
        resource_id = f"cluster:{identifier}"
        try:
            targets = self.app_autoscaling.describe_scalable_targets(
                ServiceNamespace='rds',
                ResourceIds=[resource_id]
            ).get('ScalableTargets', [])

            if not targets:
                return None # No scaling configured

            policies = self.app_autoscaling.describe_scaling_policies(
                ServiceNamespace='rds',
                ResourceId=resource_id
            ).get('ScalingPolicies', [])

            return {"targets": targets, "policies": policies}
        except ClientError as e:
            print(f"  [!] Warning: Could not fetch scaling policies: {e}")
            return None
# --- SNAPSHOT METHODS ---
    def create_instance_snapshot(self, instance_identifier, snapshot_identifier):
        try:
            self.rds.create_db_snapshot(
                DBSnapshotIdentifier=snapshot_identifier,
                DBInstanceIdentifier=instance_identifier
            )
            return True
        except ClientError as e:
            print(f"  [!] Failed to create instance snapshot: {e}")
            return False

    def create_cluster_snapshot(self, cluster_identifier, snapshot_identifier):
        try:
            self.rds.create_db_cluster_snapshot(
                DBClusterSnapshotIdentifier=snapshot_identifier,
                DBClusterIdentifier=cluster_identifier
            )
            return True
        except ClientError as e:
            print(f"  [!] Failed to create cluster snapshot: {e}")
            return False

# --- BLUE/GREEN DEPLOYMENT METHODS ---
    def create_blue_green_deployment(self, source_arn, bg_name, target_version, target_pg_name, is_cluster):
        """Triggers the creation of the Blue/Green environment."""
        try:
            kwargs = {
                'BlueGreenDeploymentName': bg_name,
                'Source': source_arn,
                'TargetEngineVersion': str(target_version)
            }
            
            # AWS uses different parameter keys for clusters vs standard instances
            if is_cluster:
                kwargs['TargetDBClusterParameterGroupName'] = target_pg_name
            else:
                kwargs['TargetDBParameterGroupName'] = target_pg_name

            response = self.rds.create_blue_green_deployment(**kwargs)
            return response['BlueGreenDeployment']
        except ClientError as e:
            raise RuntimeError(f"AWS API Error during Blue/Green creation: {e}")

    def get_blue_green_deployment(self, bg_deployment_identifier):
        """Fetches the current status of a deployment."""
        try:
            response = self.rds.describe_blue_green_deployments(
                BlueGreenDeploymentIdentifier=bg_deployment_identifier
            )
            if response.get('BlueGreenDeployments'):
                return response['BlueGreenDeployments'][0]
            return None
        except ClientError as e:
            print(f"  [!] Failed to get deployment status: {e}")
            return None
    def delete_blue_green_deployment(self, bg_deployment_identifier, delete_target=False):
        """Deletes the Blue/Green deployment."""
        try:
            self.rds.delete_blue_green_deployment(
                BlueGreenDeploymentIdentifier=bg_deployment_identifier,
                DeleteTarget=delete_target
            )
            return True
        except ClientError as e:
            print(f"  [!] Failed to delete blue/green deployment {bg_deployment_identifier}: {e}")
            return False

# --- BLUE/GREEN HELPER METHODS ---
    def get_green_target_identifier(self, bg_id):
        """Extracts the dynamically generated Green cluster identifier from the deployment ARN."""
        deployment = self.get_blue_green_deployment(bg_id)
        if not deployment or 'Target' not in deployment:
            return None
        # Example ARN: arn:aws:rds:us-east-1:123456789012:cluster:database-1-green-abcde
        target_arn = deployment['Target']
        return target_arn.split(':')[-1]

    # --- AUTO-SCALING RESTORATION METHODS (PHASE 3) ---
    def register_scalable_target(self, resource_id, min_capacity, max_capacity):
        """Registers a Green Aurora cluster with the Auto-Scaling service."""
        try:
            self.app_autoscaling.register_scalable_target(
                ServiceNamespace='rds',
                ResourceId=resource_id,
                ScalableDimension='rds:cluster:ReadReplicaCount',
                MinCapacity=min_capacity,
                MaxCapacity=max_capacity
            )
            return True
        except ClientError as e:
            print(f"  [!] Failed to register scalable target: {e}")
            return False

    def put_scaling_policy(self, resource_id, policy_name, policy_type, target_tracking_config=None):
        """Dynamically attaches any scaling policy to the Green cluster."""
        try:
            kwargs = {
                'ServiceNamespace': 'rds',
                'ResourceId': resource_id,
                'ScalableDimension': 'rds:cluster:ReadReplicaCount',
                'PolicyName': policy_name,
                'PolicyType': policy_type
            }
            
            # Inject the exact configuration block captured from the source DB
            if target_tracking_config:
                kwargs['TargetTrackingScalingPolicyConfiguration'] = target_tracking_config

            self.app_autoscaling.put_scaling_policy(**kwargs)
            return True
        except ClientError as e:
            print(f"  [!] Failed to put scaling policy: {e}")
            return False
# --- SWITCHOVER METHODS (PHASE 4) ---
    def get_replica_lag(self, target_identifier, is_cluster):
        """Queries CloudWatch for the ReplicaLag of the Green environment."""
        dimension_name = 'DBClusterIdentifier' if is_cluster else 'DBInstanceIdentifier'
        
        try:
            response = self.cloudwatch.get_metric_statistics(
                Namespace='AWS/RDS',
                MetricName='ReplicaLag',
                Dimensions=[{'Name': dimension_name, 'Value': target_identifier}],
                StartTime=datetime.datetime.utcnow() - datetime.timedelta(minutes=5),
                EndTime=datetime.datetime.utcnow(),
                Period=60,
                Statistics=['Maximum']
            )
            
            datapoints = response.get('Datapoints', [])
            if not datapoints:
                return None # Metrics might still be warming up
                
            # Sort by timestamp and get the most recent lag value
            latest = sorted(datapoints, key=lambda x: x['Timestamp'])[-1]
            return latest['Maximum']
        except Exception as e:
            print(f"  [!] Warning: Could not fetch CloudWatch metrics: {e}")
            return None

    def switchover_deployment(self, bg_id, timeout_seconds=300):
        """Triggers the final switchover with a strict safety timeout."""
        try:
            response = self.rds.switchover_blue_green_deployment(
                BlueGreenDeploymentIdentifier=bg_id,
                SwitchoverTimeout=timeout_seconds
            )
            return True
        except ClientError as e:
            raise RuntimeError(f"Switchover API rejected the request: {e}")
    def stop_database(self, identifier, is_cluster):
        """Safely powers down the old database to save money."""
        try:
            if is_cluster:
                info = self.get_cluster(identifier)
                if not info:
                    return True
                curr_status = info.get('Status', '').lower()
                if curr_status in ['stopped', 'stopping']:
                    return True
                self.rds.stop_db_cluster(DBClusterIdentifier=identifier)
            else:
                info = self.get_instance(identifier)
                if not info:
                    return True
                curr_status = info.get('DBInstanceStatus', '').lower()
                if curr_status in ['stopped', 'stopping']:
                    return True
                self.rds.stop_db_instance(DBInstanceIdentifier=identifier)
            return True
        except ClientError as e:
            err_code = e.response.get('Error', {}).get('Code', '')
            if err_code in ['InvalidDBInstanceStateFault', 'InvalidDBClusterStateFault', 'DBInstanceNotFoundFault', 'DBClusterNotFoundFault']:
                return True
            print(f"  [!] Failed to stop database '{identifier}': {e}")
            raise RuntimeError(str(e))

    def delete_database(self, identifier, is_cluster, skip_final_snapshot=True):
        """Permanently deletes a decommissioned database instance or cluster, handling deletion protection and Aurora member instances."""
        try:
            if is_cluster:
                # 1. Fetch cluster info
                cluster_info = self.get_cluster(identifier)
                if not cluster_info:
                    print(f"  [!] Cluster '{identifier}' not found or already deleted.")
                    return True

                # 2. Disable Deletion Protection on the cluster if enabled
                if cluster_info.get('DeletionProtection', False):
                    print(f"  [*] Disabling deletion protection on cluster '{identifier}'...")
                    try:
                        self.rds.modify_db_cluster(
                            DBClusterIdentifier=identifier,
                            DeletionProtection=False,
                            ApplyImmediately=True
                        )
                    except Exception as e:
                        print(f"  [!] Warning disabling cluster deletion protection: {e}")

                # 3. If cluster is stopped, start it first so AWS permits deleting member instances
                curr_cluster_status = cluster_info.get('Status', '').lower()
                if curr_cluster_status == 'stopped':
                    print(f"  [*] Cluster '{identifier}' is stopped. Starting cluster so AWS permits member instance deletion...")
                    try:
                        self.rds.start_db_cluster(DBClusterIdentifier=identifier)
                    except Exception as e:
                        print(f"  [!] Warning starting cluster for deletion: {e}")

                members = cluster_info.get('DBClusterMembers', [])

                # 4. Wait for member instances to reach 'available' or 'deleting' state before sending delete command
                import time
                for member in members:
                    member_id = member.get('DBInstanceIdentifier')
                    if not member_id:
                        continue

                    # Wait if member is starting
                    for _ in range(30):
                        inst_info = self.get_instance(member_id)
                        if not inst_info:
                            break
                        st = inst_info.get('DBInstanceStatus', '').lower()
                        if st in ['available', 'deleting', 'failed']:
                            break
                        print(f"  [-] Waiting for member instance '{member_id}' status ({st}) to become available for deletion...")
                        time.sleep(5)

                    # Disable deletion protection on member instance if enabled
                    inst_info = self.get_instance(member_id)
                    if inst_info and inst_info.get('DeletionProtection', False):
                        print(f"  [*] Disabling deletion protection on member instance '{member_id}'...")
                        try:
                            self.rds.modify_db_instance(
                                DBInstanceIdentifier=member_id,
                                DeletionProtection=False,
                                ApplyImmediately=True
                            )
                        except Exception:
                            pass

                    # Delete the member instance
                    print(f"  [*] Deleting Aurora member instance '{member_id}'...")
                    try:
                        self.rds.delete_db_instance(
                            DBInstanceIdentifier=member_id,
                            SkipFinalSnapshot=skip_final_snapshot,
                            DeleteAutomatedBackups=True
                        )
                    except ClientError as e:
                        if e.response['Error']['Code'] not in ['DBInstanceNotFoundFault', 'InvalidDBInstanceStateFault']:
                            print(f"  [!] Warning deleting member instance '{member_id}': {e}")

                # 5. Wait for all member instances to transition to deleting/none
                print(f"  [*] Waiting for Aurora member instances to enter deleting state...")
                for _ in range(20):
                    all_deleting = True
                    for member in members:
                        m_id = member.get('DBInstanceIdentifier')
                        if m_id:
                            try:
                                inst = self.get_instance(m_id)
                                status = inst.get('DBInstanceStatus', '').lower() if inst else 'deleted'
                                if status not in ['deleting', 'deleted', 'none']:
                                    all_deleting = False
                                    break
                            except Exception:
                                pass
                    if all_deleting:
                        break
                    time.sleep(4)

                # 6. Delete the cluster itself with automatic retries
                print(f"  [*] Deleting Aurora cluster '{identifier}'...")
                for attempt in range(10):
                    try:
                        self.rds.delete_db_cluster(
                            DBClusterIdentifier=identifier,
                            SkipFinalSnapshot=skip_final_snapshot
                        )
                        print(f"  [+] Aurora cluster '{identifier}' deletion accepted by AWS!")
                        break
                    except ClientError as e:
                        if e.response['Error']['Code'] == 'InvalidDBClusterStateFault' and attempt < 9:
                            print(f"  [-] Waiting for AWS to finalize member deletion state (attempt {attempt+1}/10)...")
                            time.sleep(6)
                            continue
                        raise
            else:
                # Standalone RDS instance
                inst_info = self.get_instance(identifier)
                if not inst_info:
                    print(f"  [!] Instance '{identifier}' not found or already deleted.")
                    return True

                # Check and disable Deletion Protection if enabled
                if inst_info.get('DeletionProtection', False):
                    print(f"  [*] Disabling deletion protection on instance '{identifier}'...")
                    try:
                        self.rds.modify_db_instance(
                            DBInstanceIdentifier=identifier,
                            DeletionProtection=False,
                            ApplyImmediately=True
                        )
                    except Exception as e:
                        print(f"  [!] Warning disabling instance deletion protection: {e}")

                print(f"  [*] Deleting RDS instance '{identifier}'...")
                self.rds.delete_db_instance(
                    DBInstanceIdentifier=identifier,
                    SkipFinalSnapshot=skip_final_snapshot,
                    DeleteAutomatedBackups=True
                )
            return True
        except ClientError as e:
            print(f"  [!] Failed to delete database '{identifier}': {e}")
            raise RuntimeError(str(e))

    def rename_database(self, old_identifier: str, new_identifier: str, is_cluster: bool = False):
        """Renames an RDS instance or Aurora DB cluster immediately."""
        try:
            if is_cluster:
                print(f"  [*] Renaming Aurora DB Cluster '{old_identifier}' ➔ '{new_identifier}'...")
                response = self.rds.modify_db_cluster(
                    DBClusterIdentifier=old_identifier,
                    NewDBClusterIdentifier=new_identifier,
                    ApplyImmediately=True
                )
                return response.get('DBCluster', {})
            else:
                print(f"  [*] Renaming Standalone RDS Instance '{old_identifier}' ➔ '{new_identifier}'...")
                response = self.rds.modify_db_instance(
                    DBInstanceIdentifier=old_identifier,
                    NewDBInstanceIdentifier=new_identifier,
                    ApplyImmediately=True
                )
                return response.get('DBInstance', {})
        except ClientError as e:
            print(f"  [!] Failed to rename database '{old_identifier}' to '{new_identifier}': {e}")
            raise RuntimeError(str(e))

    def _to_pascal_case(self, s: str) -> str:
        """Convert snake_case or kebab-case to PascalCase for AWS boto3 parameters."""
        parts = s.replace('-', '_').split('_')
        acronyms = {'db': 'DB', 'az': 'AZ', 'arn': 'ARN', 'iops': 'Iops', 'vpc': 'VPC', 'ca': 'CA', 'ssl': 'SSL'}
        res = []
        for p in parts:
            if p.lower() in acronyms:
                res.append(acronyms[p.lower()])
            else:
                res.append(p.capitalize())
        return ''.join(res)

    def execute_aws_cli(self, command_str: str) -> Dict[str, Any]:
        """Dynamically executes ANY arbitrary AWS CLI RDS command string using active AWS credentials and region."""
        import subprocess
        import shlex
        import json
        import re

        cmd_clean = command_str.strip()
        if cmd_clean.startswith("```"):
            cmd_clean = re.sub(r'^```(?:bash|sh)?\s*', '', cmd_clean)
            cmd_clean = re.sub(r'\s*```$', '', cmd_clean)

        if "--region" not in cmd_clean and self.region:
            cmd_clean += f" --region {self.region}"

        if "--output" not in cmd_clean:
            cmd_clean += " --output json"

        try:
            args = shlex.split(cmd_clean)
            if not args or args[0] != "aws":
                raise ValueError(f"Command must begin with 'aws' (got: '{cmd_clean}')")

            print(f"  [*] Executing AWS CLI: {cmd_clean}")
            res = subprocess.run(args, capture_output=True, text=True, timeout=45)
            if res.returncode == 0:
                try:
                    parsed_json = json.loads(res.stdout) if res.stdout.strip() else {}
                except Exception:
                    parsed_json = {"raw_output": res.stdout.strip()}
                return {
                    "status": "success",
                    "command": cmd_clean,
                    "output": parsed_json,
                    "stdout": res.stdout.strip()
                }
            else:
                err_msg = res.stderr.strip() or res.stdout.strip() or f"Exited with code {res.returncode}"
                raise RuntimeError(err_msg)
        except Exception as e:
            print(f"  [!] AWS CLI Execution failed: {e}")
            raise RuntimeError(f"AWS CLI execution failed: {str(e)}")

    def modify_database_configuration(self, identifier: str, is_cluster: bool = False, **kwargs):
        """Applies ANY configuration modifications dynamically to an RDS instance or Aurora DB cluster immediately."""
        try:
            modifications_applied = {}

            if is_cluster:
                boto_args = {
                    'DBClusterIdentifier': identifier,
                    'ApplyImmediately': True
                }
                # Dynamic mapping for all arbitrary properties
                for k, v in kwargs.items():
                    if v is not None and k not in ['identifier', 'is_cluster', 'cli_command']:
                        pascal_key = self._to_pascal_case(k)
                        boto_args[pascal_key] = v
                        modifications_applied[pascal_key] = v
                
                # Special handling for Aurora member AutoMinorVersionUpgrade
                if 'AutoMinorVersionUpgrade' in boto_args:
                    cluster_info = self.get_cluster(identifier)
                    if cluster_info:
                        for m in cluster_info.get('DBClusterMembers', []):
                            m_id = m.get('DBInstanceIdentifier')
                            if m_id:
                                try:
                                    self.rds.modify_db_instance(
                                        DBInstanceIdentifier=m_id,
                                        AutoMinorVersionUpgrade=bool(boto_args['AutoMinorVersionUpgrade']),
                                        ApplyImmediately=True
                                    )
                                except Exception as e:
                                    print(f"  [!] Warning modifying member AutoMinorVersionUpgrade: {e}")
                
                print(f"  [*] Modifying Aurora Cluster '{identifier}' with: {boto_args}")
                resp = self.rds.modify_db_cluster(**boto_args)
                return {
                    'identifier': identifier,
                    'is_cluster': True,
                    'modifications_applied': modifications_applied,
                    'cluster': resp.get('DBCluster', {})
                }
            else:
                boto_args = {
                    'DBInstanceIdentifier': identifier,
                    'ApplyImmediately': True
                }
                # Dynamic mapping for all arbitrary properties
                for k, v in kwargs.items():
                    if v is not None and k not in ['identifier', 'is_cluster', 'cli_command']:
                        pascal_key = self._to_pascal_case(k)
                        boto_args[pascal_key] = v
                        modifications_applied[pascal_key] = v

                print(f"  [*] Modifying Standalone RDS Instance '{identifier}' with: {boto_args}")
                resp = self.rds.modify_db_instance(**boto_args)
                return {
                    'identifier': identifier,
                    'is_cluster': False,
                    'modifications_applied': modifications_applied,
                    'instance': resp.get('DBInstance', {})
                }
        except ClientError as e:
            print(f"  [!] Failed to modify database configuration for '{identifier}': {e}")
            raise RuntimeError(str(e))