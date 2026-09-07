import unittest
from unittest.mock import MagicMock, patch, call
from botocore.exceptions import ClientError

# Import classes to test
from core.detector import EnvironmentDetector
from core.param_manager import ParameterManager
from core.rebooter import RebootOrchestrator
from core.stager import StagingManager
from core.switcher import SwitchoverManager
from generate_summary import truncate_value

class TestSummaryGenerator(unittest.TestCase):
    """Test cases for generate_summary helper methods."""
    
    def test_truncate_value_none(self):
        self.assertEqual(truncate_value(None), '[Not Set]')
        
    def test_truncate_value_short(self):
        self.assertEqual(truncate_value('hello'), 'hello')
        
    def test_truncate_value_exact(self):
        val = 'a' * 45
        self.assertEqual(truncate_value(val), val)
        
    def test_truncate_value_long(self):
        val = 'a' * 50
        expected = 'a' * 42 + '...'
        self.assertEqual(truncate_value(val), expected)


class TestEnvironmentDetector(unittest.TestCase):
    """Test cases for EnvironmentDetector topology discovery and path validation."""
    
    def setUp(self):
        self.mock_aws = MagicMock()
        self.detector = EnvironmentDetector(self.mock_aws)

    def test_inspect_rds_instance_success(self):
        self.mock_aws.get_cluster.return_value = None
        self.mock_aws.get_instance.return_value = {
            'Engine': 'postgres',
            'EngineVersion': '15.7',
            'DBParameterGroups': [{'DBParameterGroupName': 'my-custom-pg'}],
            'DBInstanceArn': 'arn:aws:rds:us-east-1:123456789012:db:test-instance'
        }
        self.mock_aws.get_pg_family.return_value = 'postgres15'

        state = self.detector.inspect('test-instance')
        
        self.assertFalse(state['is_aurora'])
        self.assertFalse(state['is_cluster'])
        self.assertEqual(state['engine'], 'postgres')
        self.assertEqual(state['current_version'], '15.7')
        self.assertEqual(state['source_pg_name'], 'my-custom-pg')
        self.assertEqual(state['source_pg_family'], 'postgres15')

    def test_inspect_database_not_found(self):
        # Database cluster and instance both not found
        self.mock_aws.get_cluster.return_value = None
        self.mock_aws.get_instance.return_value = None
        
        with self.assertRaises(ValueError):
            self.detector.inspect('missing-db')

    def test_inspect_secrets_manager_blocker_rds(self):
        self.mock_aws.get_cluster.return_value = None
        self.mock_aws.get_instance.return_value = {
            'Engine': 'postgres',
            'EngineVersion': '15.7',
            'DBParameterGroups': [{'DBParameterGroupName': 'my-custom-pg'}],
            'DBInstanceArn': 'arn:aws:rds:us-east-1:123456789012:db:test-instance',
            'MasterUserSecret': {'SecretArn': 'arn:aws:secretsmanager:us-east-1:123:secret:abc'}
        }

        with self.assertRaises(RuntimeError) as context:
            self.detector.inspect('test-instance')
        self.assertIn("Secrets Manager Integration Detected", str(context.exception))

    def test_inspect_secrets_manager_blocker_aurora(self):
        self.mock_aws.get_cluster.return_value = {
            'Engine': 'aurora-postgresql',
            'EngineVersion': '15.10',
            'DBClusterParameterGroup': 'aurora-cluster-pg',
            'DBClusterArn': 'arn:aws:rds:us-east-1:123:cluster:test-cluster',
            'MasterUserSecret': {'SecretArn': 'arn:aws:secretsmanager:us-east-1:123:secret:abc'}
        }

        with self.assertRaises(RuntimeError) as context:
            self.detector.inspect('test-cluster')
        self.assertIn("Secrets Manager Integration Detected", str(context.exception))

    def test_inspect_aurora_cluster_success(self):
        self.mock_aws.get_cluster.return_value = {
            'Engine': 'aurora-postgresql',
            'EngineVersion': '15.10',
            'DBClusterParameterGroup': 'aurora-cluster-pg',
            'DBClusterArn': 'arn:aws:rds:us-east-1:123:cluster:test-cluster',
            'DBClusterMembers': [{'DBInstanceIdentifier': 'writer-node'}]
        }
        self.mock_aws.get_instance.return_value = {
            'DBParameterGroups': [{'DBParameterGroupName': 'aurora-instance-pg'}]
        }
        self.mock_aws.get_pg_family.return_value = 'aurora-postgresql15'
        self.mock_aws.get_scaling_policies.return_value = None

        state = self.detector.inspect('test-cluster')

        self.assertTrue(state['is_aurora'])
        self.assertTrue(state['is_cluster'])
        self.assertEqual(state['engine'], 'aurora-postgresql')
        self.assertEqual(state['source_pg_name'], 'aurora-cluster-pg')
        self.assertIn('aurora-instance-pg', state['aurora_instance_pgs'])

    def test_get_upgrade_target_validation(self):
        self.mock_aws.get_engine_versions.side_effect = lambda engine, version: {
            '15.7': {
                'ValidUpgradeTarget': [
                    {'EngineVersion': '16.11', 'IsMajorVersionUpgrade': True},
                    {'EngineVersion': '15.9', 'IsMajorVersionUpgrade': False}
                ]
            },
            '16.11': {'DBParameterGroupFamily': 'postgres16'},
            '15.9': {'DBParameterGroupFamily': 'postgres15'}
        }[version]

        # Valid Target
        version, family = self.detector.get_upgrade_target('postgres', '15.7', '16.11')
        self.assertEqual(version, '16.11')
        self.assertEqual(family, 'postgres16')

        # Invalid Target
        with self.assertRaises(ValueError):
            self.detector.get_upgrade_target('postgres', '15.7', '17.0')

        # Auto selection
        auto_version, auto_family = self.detector.get_upgrade_target('postgres', '15.7', desired_version=None)
        self.assertEqual(auto_version, '16.11')
        self.assertEqual(auto_family, 'postgres16')

    def test_get_upgrade_target_no_upgrades(self):
        self.mock_aws.get_engine_versions.return_value = {'ValidUpgradeTarget': []}
        with self.assertRaises(ValueError):
            self.detector.get_upgrade_target('postgres', '15.7')


class TestParameterManager(unittest.TestCase):
    """Test cases for ParameterManager rule enforcement payload compilation."""
    
    def setUp(self):
        self.mock_aws = MagicMock()
        self.pm = ParameterManager(self.mock_aws)

    def test_enforce_bg_rules_static_and_thresholds(self):
        target_params_mock = {
            'rds.logical_replication': {'ParameterName': 'rds.logical_replication', 'IsModifiable': True, 'ParameterValue': '0'},
            'max_replication_slots': {'ParameterName': 'max_replication_slots', 'IsModifiable': True, 'ParameterValue': '5'},
            'max_worker_processes': {'ParameterName': 'max_worker_processes', 'IsModifiable': True, 'ParameterValue': '8'},
            'shared_preload_libraries': {'ParameterName': 'shared_preload_libraries', 'IsModifiable': True, 'ParameterValue': 'pg_stat_statements'}
        }
        self.pm._get_current_params = MagicMock(return_value=target_params_mock)
        
        self.pm.enforce_bg_rules(target_pg_name='target-pg', is_cluster=False, source_pg_name=None)
        
        self.mock_aws.modify_parameters.assert_called_once()
        args, kwargs = self.mock_aws.modify_parameters.call_args
        
        target_pg_arg, updates, is_cluster_arg = args
        self.assertEqual(target_pg_arg, 'target-pg')
        self.assertFalse(is_cluster_arg)
        
        update_dict = {u['ParameterName']: u['ParameterValue'] for u in updates}
        self.assertEqual(update_dict.get('rds.logical_replication'), '1')
        self.assertEqual(update_dict.get('max_replication_slots'), '20')
        self.assertEqual(update_dict.get('max_worker_processes'), '24')

    def test_enforce_bg_rules_non_numeric_and_warnings(self):
        target_params_mock = {
            'max_worker_processes': {'ParameterName': 'max_worker_processes', 'IsModifiable': True, 'ParameterValue': 'GREATEST({DBInstanceVCPU*2},8)'},
            'shared_buffers': {'ParameterName': 'shared_buffers', 'IsModifiable': True, 'ParameterValue': 'default'}
        }
        source_params_mock = {
            'shared_buffers': {'ParameterName': 'shared_buffers', 'ParameterValue': '800000'} # Hardcoded memory
        }

        self.pm._get_current_params = MagicMock(side_effect=lambda pg, is_cluster: {
            'source-pg': source_params_mock,
            'target-pg': target_params_mock
        }[pg])

        # We redirect print output or inspect modify_parameters call
        self.pm.enforce_bg_rules(target_pg_name='target-pg', is_cluster=False, source_pg_name='source-pg')

        # Check modifications
        args, _ = self.mock_aws.modify_parameters.call_args
        updates = args[1]
        update_dict = {u['ParameterName']: u['ParameterValue'] for u in updates}
        
        # Verify max_worker_processes elevated (from formula) to threshold (24)
        self.assertEqual(update_dict.get('max_worker_processes'), '24')
        # Verify memory carried over
        self.assertEqual(update_dict.get('shared_buffers'), '800000')

    def test_handle_default_source_group_cloning(self):
        state = {
            'identifier': 'test-db',
            'is_cluster': False,
            'current_version': '15.7',
            'source_pg_name': 'default.postgres15',
            'source_pg_family': 'postgres15'
        }
        self.mock_aws.create_parameter_group.return_value = True
        self.pm.enforce_bg_rules = MagicMock()
        
        self.pm.handle_default_source_group(state)
        
        # Verify custom group created
        self.mock_aws.create_parameter_group.assert_called_with('test-db-pg-15-7', 'postgres15', 'Custom source for B/G', False)
        # Verify attached
        self.mock_aws.attach_parameter_group.assert_called_with('test-db', 'test-db-pg-15-7', False)
        # Verify state updated to new custom PG name
        self.assertEqual(state['source_pg_name'], 'test-db-pg-15-7')


@patch('time.sleep', return_value=None)
class TestRebootOrchestrator(unittest.TestCase):
    """Test cases for RebootOrchestrator and instance cycle sequencing."""
    
    def setUp(self):
        self.mock_aws = MagicMock()
        self.rebooter = RebootOrchestrator(self.mock_aws)

    def test_execute_reboot_rds_instance(self, mock_sleep):
        state = {
            'identifier': 'rds-db',
            'is_cluster': False
        }
        # Mock instance status response
        self.mock_aws.rds.describe_db_instances.return_value = {
            'DBInstances': [{'DBInstanceStatus': 'available'}]
        }
        
        self.rebooter.execute_reboot(state)
        
        # Verify reboot method called
        self.mock_aws.rds.reboot_db_instance.assert_called_once_with(DBInstanceIdentifier='rds-db')

    def test_execute_reboot_aurora_cluster(self, mock_sleep):
        state = {
            'identifier': 'aurora-cluster',
            'is_cluster': True
        }
        # Mock cluster members discovery
        self.mock_aws.rds.describe_db_clusters.return_value = {
            'DBClusters': [{
                'DBClusterMembers': [
                    {'DBInstanceIdentifier': 'writer-node', 'IsClusterWriter': True},
                    {'DBInstanceIdentifier': 'reader-node', 'IsClusterWriter': False}
                ]
            }]
        }
        # Mock instance status checks (writer available, then reader available)
        self.mock_aws.rds.describe_db_instances.return_value = {
            'DBInstances': [{'DBInstanceStatus': 'available'}]
        }

        self.rebooter.execute_reboot(state)
        
        # Verify reboot sequencing (writer first, then reader)
        self.mock_aws.rds.reboot_db_instance.assert_has_calls([
            call(DBInstanceIdentifier='writer-node'),
            call(DBInstanceIdentifier='reader-node')
        ])


class TestStagingManager(unittest.TestCase):
    """Test cases for StagingManager and Auto-Scaling preservation."""
    
    def setUp(self):
        self.mock_aws = MagicMock()
        self.stager = StagingManager(self.mock_aws)

    def test_restore_scaling_policies_no_policies(self):
        state = {
            'is_cluster': True,
            'scaling_policies': None
        }
        self.stager.restore_scaling_policies(state, 'bg-123')
        # Should return without registering target
        self.mock_aws.register_scalable_target.assert_not_called()

    def test_restore_scaling_policies_success(self):
        state = {
            'is_cluster': True,
            'scaling_policies': {
                'targets': [{'MinCapacity': 2, 'MaxCapacity': 10}],
                'policies': [{
                    'PolicyName': 'cpu-scale-up',
                    'PolicyType': 'TargetTrackingScaling',
                    'TargetTrackingScalingPolicyConfiguration': {'TargetValue': 70.0}
                }]
            }
        }
        self.mock_aws.get_green_target_identifier.return_value = 'green-db'
        
        self.stager.restore_scaling_policies(state, 'bg-123')
        
        # Verify scalable bounds restored on Green Cluster resource ID
        self.mock_aws.register_scalable_target.assert_called_with('cluster:green-db', 2, 10)
        # Verify specific tracking policy restored
        self.mock_aws.put_scaling_policy.assert_called_with(
            resource_id='cluster:green-db',
            policy_name='cpu-scale-up',
            policy_type='TargetTrackingScaling',
            target_tracking_config={'TargetValue': 70.0}
        )


@patch('time.sleep', return_value=None)
class TestSwitchoverManager(unittest.TestCase):
    """Test cases for SwitchoverManager lag monitor and cutover coordination."""
    
    def setUp(self):
        self.mock_aws = MagicMock()
        self.switcher = SwitchoverManager(self.mock_aws)

    def test_monitor_replication_lag_success(self, mock_sleep):
        state = {'is_cluster': False}
        self.mock_aws.get_green_target_identifier.return_value = 'green-db'
        
        # Simulate replication lag dropping below max limit
        self.mock_aws.get_replica_lag.side_effect = [None, 45, 12] # Warm-up -> high -> safe
        
        is_safe = self.switcher.monitor_replication_lag(state, 'bg-123', max_lag_seconds=30)
        
        self.assertTrue(is_safe)
        self.assertEqual(self.mock_aws.get_replica_lag.call_count, 3)

    def test_monitor_replication_lag_timeout(self, mock_sleep):
        state = {'is_cluster': False}
        self.mock_aws.get_green_target_identifier.return_value = 'green-db'
        
        # Simulate lag never dropping
        self.mock_aws.get_replica_lag.return_value = 50
        
        is_safe = self.switcher.monitor_replication_lag(state, 'bg-123', max_lag_seconds=30)
        
        self.assertFalse(is_safe)

    def test_execute_switchover_success_with_stop(self, mock_sleep):
        state = {'is_cluster': False}
        
        # Mock deployment status queries during polling:
        # First available checks, then completion status checks
        self.mock_aws.get_blue_green_deployment.side_effect = [
            # Check for green target availability:
            {
                'Status': 'PROVISIONING',
                'SwitchoverDetails': [{'TargetMember': 'arn:aws:rds:us-east-1:123:db:green-db'}]
            },
            # Switchover in progress monitor:
            {'Status': 'SWITCHOVER_IN_PROGRESS'},
            # Switchover complete:
            {
                'Status': 'SWITCHOVER_COMPLETED',
                'Source': 'arn:aws:rds:us-east-1:123:db:old-db'
            }
        ]
        
        # Mock DB instance status checks during green target checks
        self.mock_aws.rds.describe_db_instances.side_effect = [
            # Available check during target check
            {'DBInstances': [{'DBInstanceStatus': 'available'}]},
            # Available check during post-cutover old DB check
            {'DBInstances': [{'DBInstanceStatus': 'available'}]}
        ]
        self.mock_aws.get_instance.return_value = {'DBInstanceStatus': 'available'}
        
        # Execute switchover with auto-stop enabled
        self.switcher.execute_switchover('bg-123', state, auto_stop_old_db=True)
        
        # Verify switchover triggered
        self.mock_aws.switchover_deployment.assert_called_with('bg-123', timeout_seconds=300)
        # Verify deployment record clean-up
        self.mock_aws.delete_blue_green_deployment.assert_called_with('bg-123', delete_target=False)
        # Verify old DB shutdown signal issued
        self.mock_aws.stop_database.assert_called_with('old-db', False)


if __name__ == '__main__':
    unittest.main()
