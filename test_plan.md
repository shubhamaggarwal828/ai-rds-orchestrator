# AWS RDS Blue/Green Upgrade Engine - Comprehensive Test Plan

This document outlines a complete test suite for the RDS Blue/Green Upgrade Engine. It includes standard baseline test cases, as well as rigorous **Edge Cases** known to break AWS Blue/Green deployments and `pg_upgrade`. 

---

## 1. Setup Guide: Standard Scenarios

Use these AWS CLI commands to spin up baseline test databases. **Note**: Replace `YourSecurePassword123` securely.

### Scenario 1: Standard RDS (Default Parameter Group)
Tests the script's ability to detect a default parameter group, create a custom clone, push B/G rules, attach it, and prompt for/handle the required reboot correctly.
```bash
aws rds create-db-instance \
    --db-instance-identifier test-rds-default \
    --db-instance-class db.t4g.medium --engine postgres --engine-version 15.7 \
    --allocated-storage 20 --master-username postgres --master-user-password "YourSecurePassword123" \
    --backup-retention-period 1 --no-multi-az --no-publicly-accessible
```

### Scenario 2: Standard RDS (Custom PG, No Mods)
Tests the script's ability to reuse an existing custom parameter group.
```bash
aws rds create-db-parameter-group --db-parameter-group-name test-pg-custom-nomods --db-parameter-group-family postgres15 --description "Custom PG without mods"
aws rds create-db-instance \
    --db-instance-identifier test-rds-custom \
    --db-instance-class db.t4g.medium --engine postgres --engine-version 15.7 \
    --allocated-storage 20 --master-username postgres --master-user-password "YourSecurePassword123" \
    --db-parameter-group-name test-pg-custom-nomods --backup-retention-period 1 --no-publicly-accessible
```

### Scenario 3: Standard RDS (Multi-AZ)
Tests if the deployment wait logic handles Multi-AZ deployments correctly.
```bash
aws rds create-db-instance \
    --db-instance-identifier test-rds-multiaz \
    --db-instance-class db.t4g.medium --engine postgres --engine-version 15.7 \
    --allocated-storage 20 --master-username postgres --master-user-password "YourSecurePassword123" \
    --backup-retention-period 1 --multi-az --no-publicly-accessible
```

### Scenario 4: Aurora Postgres (Single Writer, Default PG)
Tests the `is_cluster` detection, cluster-level snapshotting, and cluster vs instance parameter groups.
```bash
aws rds create-db-cluster \
    --db-cluster-identifier test-aurora-single \
    --engine aurora-postgresql --engine-version 15.10 \
    --master-username postgres --master-user-password "YourSecurePassword123" --backup-retention-period 1
aws rds create-db-instance \
    --db-instance-identifier test-aurora-single-writer \
    --db-cluster-identifier test-aurora-single \
    --db-instance-class db.t4g.medium --engine aurora-postgresql
```

### Scenario 5: Aurora Postgres (Writer + Reader)
Tests the reboot orchestration (rebooting writers first, then readers to minimize failover chaos).
```bash
aws rds create-db-cluster \
    --db-cluster-identifier test-aurora-multi \
    --engine aurora-postgresql --engine-version 15.10 \
    --master-username postgres --master-user-password "YourSecurePassword123" --backup-retention-period 1
aws rds create-db-instance \
    --db-instance-identifier test-aurora-multi-writer \
    --db-cluster-identifier test-aurora-multi \
    --db-instance-class db.t4g.medium --engine aurora-postgresql
aws rds create-db-instance \
    --db-instance-identifier test-aurora-multi-reader \
    --db-cluster-identifier test-aurora-multi \
    --db-instance-class db.t4g.medium --engine aurora-postgresql
```

---

## 2. Setup Guide: Edge Cases & Constraints

### Edge Case 1: The "Replication Starved" PG
Limits `max_worker_processes` and `max_replication_slots`. Our script must intercept and bump these thresholds.
```bash
aws rds create-db-parameter-group --db-parameter-group-name test-pg-starved --db-parameter-group-family postgres15 --description "Starved replication settings"
aws rds modify-db-parameter-group --db-parameter-group-name test-pg-starved \
    --parameters "ParameterName=max_worker_processes,ParameterValue=8,ApplyMethod=pending-reboot" "ParameterName=max_replication_slots,ParameterValue=5,ApplyMethod=pending-reboot"
aws rds create-db-instance \
    --db-instance-identifier test-rds-starved \
    --db-instance-class db.t4g.medium --engine postgres --engine-version 15.7 \
    --allocated-storage 20 --master-username postgres --master-user-password "YourSecurePassword123" \
    --db-parameter-group-name test-pg-starved --no-publicly-accessible --backup-retention-period 1
```

### Edge Case 2: The "RAM Killer" PG
Hardcodes `shared_buffers` instead of using a formula. The script must warn the user but copy the parameter.
```bash
aws rds create-db-parameter-group --db-parameter-group-name test-pg-memory --db-parameter-group-family postgres15 --description "Hardcoded memory"
aws rds modify-db-parameter-group --db-parameter-group-name test-pg-memory \
    --parameters "ParameterName=shared_buffers,ParameterValue=800000,ApplyMethod=pending-reboot"
aws rds create-db-instance \
    --db-instance-identifier test-rds-memory \
    --db-instance-class db.t4g.medium --engine postgres --engine-version 15.7 \
    --allocated-storage 20 --master-username postgres --master-user-password "YourSecurePassword123" \
    --db-parameter-group-name test-pg-memory --no-publicly-accessible --backup-retention-period 1
```

### Edge Case 3: The "Extension Crash" PG
Adds `pg_cron`. The script must perfectly carry this over to the target parameter group.
```bash
aws rds create-db-parameter-group --db-parameter-group-name test-pg-extensions --db-parameter-group-family postgres15 --description "Preload libraries"
aws rds modify-db-parameter-group --db-parameter-group-name test-pg-extensions \
    --parameters "ParameterName=shared_preload_libraries,ParameterValue=pg_cron,pg_stat_statements,ApplyMethod=pending-reboot"
aws rds create-db-instance \
    --db-instance-identifier test-rds-extensions \
    --db-instance-class db.t4g.medium --engine postgres --engine-version 15.7 \
    --allocated-storage 20 --master-username postgres --master-user-password "YourSecurePassword123" \
    --db-parameter-group-name test-pg-extensions --no-publicly-accessible --backup-retention-period 1
```

### Edge Case 4: Aurora Specific Constraints (`apg_ccm_enabled`)
Modifies Aurora Cluster Cache Management. If not copied perfectly to the green cluster, AWS rejects the deployment.
```bash
aws rds create-db-cluster-parameter-group --db-cluster-parameter-group-name test-aurora-ccm-pg --db-parameter-group-family aurora-postgresql15 --description "Cluster Cache Management enabled"
aws rds modify-db-cluster-parameter-group --db-cluster-parameter-group-name test-aurora-ccm-pg \
    --parameters "ParameterName=apg_ccm_enabled,ParameterValue=1,ApplyMethod=pending-reboot" "ParameterName=aurora_replica_read_consistency,ParameterValue=session,ApplyMethod=pending-reboot"
aws rds create-db-cluster \
    --db-cluster-identifier test-aurora-ccm \
    --engine aurora-postgresql --engine-version 15.7 \
    --master-username postgres --master-user-password "YourSecurePassword123" \
    --db-cluster-parameter-group-name test-aurora-ccm-pg --backup-retention-period 1
aws rds create-db-instance \
    --db-instance-identifier test-aurora-ccm-writer \
    --db-cluster-identifier test-aurora-ccm \
    --db-instance-class db.t4g.medium --engine aurora-postgresql
```

### Edge Case 6: Aurora Auto-Scaling Policies
When an Aurora cluster is used in production, it often has Application Auto-Scaling policies attached to spin up read replicas based on CPU utilization. AWS Blue/Green does **not** copy these policies natively. Our script (`StagingManager`) must capture the scaling target and target-tracking configurations from the source and attach them to the Green cluster before cutover.
```bash
aws rds create-db-cluster \
    --db-cluster-identifier test-aurora-autoscaling \
    --engine aurora-postgresql --engine-version 15.7 \
    --master-username postgres --master-user-password "YourSecurePassword123" --backup-retention-period 1
aws rds create-db-instance \
    --db-instance-identifier test-aurora-autoscaling-writer \
    --db-cluster-identifier test-aurora-autoscaling \
    --db-instance-class db.t4g.medium --engine aurora-postgresql

# Register the auto-scaling target (Min 1, Max 5 replicas)
aws application-autoscaling register-scalable-target \
    --service-namespace rds \
    --resource-id cluster:test-aurora-autoscaling \
    --scalable-dimension rds:cluster:ReadReplicaCount \
    --min-capacity 1 --max-capacity 5

# Attach the scaling policy (Scale up at 75% CPU)
aws application-autoscaling put-scaling-policy \
    --service-namespace rds \
    --resource-id cluster:test-aurora-autoscaling \
    --scalable-dimension rds:cluster:ReadReplicaCount \
    --policy-name test-aurora-scaling-policy \
    --policy-type TargetTrackingScaling \
    --target-tracking-scaling-policy-configuration '{"TargetValue":75.0,"PredefinedMetricSpecification":{"PredefinedMetricType":"RDSReaderAverageCPUUtilization"}}'
```

---

## 3. Test Execution Matrix

Test permutations of `AUTO_REBOOT_ENABLED` (R), `AUTO_DEPLOY_ENABLED` (D), `AUTO_SWITCHOVER_ENABLED` (S).

| Test ID | Scenario | Flags (R, D, S) | Expected Outcome | Actual Result | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-01** | **Standard** - Default PG | `False, False, False` | Prompts for custom clone & reboot. Stops if user says no. | **Pass.** Script correctly identified default PG, prompted for clone creation/reboot, and terminated gracefully on negative user input. Tested both major/minor paths. | Verified Doomsday rollback workflow. |
| **TC-02** | **Standard** - Default PG | `True, True, True` | Fully automates E2E upgrade to target version. | **Pass.** Completed fully automated end-to-end upgrade from PostgreSQL version 15.7 to 16.11. | Execution logs detailed in Annex 1.1. |
| **TC-03** | **Standard** - Custom PG (No Mods) | `True, True, True` | Modifies existing custom PG. B/G succeeds. | **Pass.** Detected and reused custom parameter group. Injected mandatory parameters and completed deployment successfully. | Tested on custom `test-pg-custom-nomods`. |
| **TC-04** | **Standard** - Multi-AZ | `True, True, True` | Fully automates E2E. AZ failovers handled. | **Pass.** Upgrade executed seamlessly. Target database provisioned correctly under Multi-AZ configuration. | AZ failover logic completed successfully. |
| **TC-05** | **Aurora** - Single Writer | `True, True, True` | Modifies Cluster PG. Correctly handles instance PG defaults. | **Pass.** Successfully applied Cluster Parameter Group modifications and correctly resolved default DB instance parameters. | Verified single-writer cluster architecture. |
| **TC-06** | **Aurora** - Writer + Reader | `True, True, True` | Reboots writer, then reader. Both green targets available before switch. | **Pass.** Orchestrated sequential reboot sequence (writer first, followed by readers) to preserve active telemetry. | Both green environments were verified available. |
| **TC-07** | **Proxy** - RDS Proxy Attached | `True, True, True` | Proxy does not block B/G trigger. *(Attach proxy via Console for test)* | **Pass.** Verified that presence of active RDS Proxy did not interfere with B/G deployment creation. | Proxy route successfully pointed to Green DB. |
| **EC-01** | **Edge** - Starved Replication | `True, True, True` | Script bumps target thresholds to 24 workers, 20 slots. B/G succeeds. | **Pass.** Automatically identified constrained PG limits; successfully elevated limits (`max_worker_processes` to 24, `max_replication_slots` to 20). | Prevented replication replication lag stalls. |
| **EC-02** | **Edge** - RAM Killer (Hardcoded) | `True, True, True` | Script prints `[WARNING]` about hardcoded `shared_buffers`, but copies value. | **Pass.** Emitted expected `[WARNING]` about static `shared_buffers` allocations while carrying them over correctly to prevent failures. | User alert generated in logs. |
| **EC-03** | **Edge** - Extension Crash | `True, True, True` | Script copies `shared_preload_libraries`. Green DB boots successfully. | **Pass.** Carried over `shared_preload_libraries` (including `pg_cron`, `pg_stat_statements`). Green environment booted successfully. | Extenions functional post-upgrade. |
| **EC-04** | **Edge** - Aurora Constraints | `True, True, True` | Script copies `apg_ccm_enabled=1` to Target Aurora Cluster PG. | **Pass.** Successfully copied Aurora Cluster Cache Management parameters (`apg_ccm_enabled=1`) to prevent creation rejection. | Cluster cache management preserved. |
| **EC-05** | **Edge** - Password Encryption | `True, True, True` | Script copies `password_encryption` (md5 vs scram) to prevent broken logins. | **Pass.** Retained source `password_encryption` settings in the target parameter group. | Prevented client authentication failures. |
| **EC-06** | **Edge** - Aurora Auto-Scaling | `True, True, True` | Script detects scaling target/policy and maps it exactly to Green cluster. | **Pass.** Identified active Application Auto-Scaling rules on Blue cluster and successfully attached them to the Green cluster. | Auto-scaling logic verified post-switchover. |

---

## 4. Cleanup Commands

```bash
# Delete RDS Instances (Standard)
for db in test-rds-default test-rds-custom test-rds-multiaz test-rds-starved test-rds-memory test-rds-extensions; do
  aws rds delete-db-instance --db-instance-identifier $db --skip-final-snapshot --delete-automated-backups
done

# Delete Aurora Clusters
for cl in test-aurora-single test-aurora-multi test-aurora-ccm test-aurora-autoscaling; do
  aws rds delete-db-instance --db-instance-identifier $cl-writer --skip-final-snapshot
  aws rds delete-db-instance --db-instance-identifier $cl-reader --skip-final-snapshot
  aws rds delete-db-cluster --db-cluster-identifier $cl --skip-final-snapshot
done

# Delete Parameter Groups
for pg in test-pg-custom-nomods test-pg-starved test-pg-memory test-pg-extensions; do
  aws rds delete-db-parameter-group --db-parameter-group-name $pg
done
aws rds delete-db-cluster-parameter-group --db-cluster-parameter-group-name test-aurora-ccm-pg
```


ANNEX 1.1

=== Starting Blue/Green Upgrade Engine ===
[*] AWS Session established in region: us-east-1
[*] Inspecting identifier: test-rds-default...
  -> Detected: RDS Instance (postgres 15.7)
  -> Current Primary PG: default.postgres15 (postgres15)
[*] Validating requested upgrade path: 15.7 -> 16.11...

[*] Validated Target: 16.11 (postgres16)

=== Taking Pre-Flight Snapshot ===
[*] Initiating snapshot: test-rds-default-preflight-20260616-0330...
  [+] RDS Instance Snapshot initiated.
  [*] Waiting for snapshot to complete (this ensures the DB is safe to reboot)...
  [-] [0m elapsed] Snapshot status: creating... waiting 30s
  [-] [0m elapsed] Snapshot status: creating... waiting 30s
  [-] [1m elapsed] Snapshot status: creating... waiting 30s
  [-] [1m elapsed] Snapshot status: creating... waiting 30s

  [SUCCESS] Snapshot test-rds-default-preflight-20260616-0330 is fully backed up and available!

[!] Source uses a default PG. Creating a custom clone for B/G...
  [*] Configuring new group test-rds-default-pg-15-7 before attaching...
  [+] Enforcing Static Rule: rds.logical_replication = 1
  [+] Enforcing Static Rule: synchronous_commit = on
  [+] Enforcing Static Rule: rds.force_ssl = 0
  [+] Enforcing Static Rule: idle_in_transaction_session_timeout = 60000
  [+] Enforcing Static Rule: wal_sender_timeout = 0
  [+] Bumping Threshold: max_logical_replication_workers from None to 8
  [!] Non-numeric parameter value found for max_worker_processes: 'GREATEST({DBInstanceVCPU*2},8)'. Defaulting to threshold.
  [+] Bumping Threshold: max_worker_processes from GREATEST({DBInstanceVCPU*2},8) to 24
  [SUCCESS] Pushed 7 safety parameters to test-rds-default-pg-15-7
  [+] Attached fully configured custom source group: test-rds-default-pg-15-7
  [CRITICAL] You MUST manually reboot the database to apply logical_replication=1.

[*] Prepping Target Primary PG: test-rds-default-pg-16-11 (postgres16)...
  [+] Enforcing Static Rule: rds.logical_replication = 1
  [+] Enforcing Static Rule: synchronous_commit = on
  [+] Enforcing Static Rule: rds.force_ssl = 0
  [+] Enforcing Static Rule: idle_in_transaction_session_timeout = 60000
  [+] Enforcing Static Rule: wal_sender_timeout = 0
  [+] Bumping Threshold: max_logical_replication_workers from None to 8
  [!] Non-numeric parameter value found for max_worker_processes: 'GREATEST({DBInstanceVCPU*2},8)'. Defaulting to threshold.
  [+] Bumping Threshold: max_worker_processes from GREATEST({DBInstanceVCPU*2},8) to 24
  [+] Carrying Over: shared_preload_libraries = pg_stat_statements
  [SUCCESS] Pushed 8 safety parameters to test-rds-default-pg-16-11

[*] Generating Upgrade Audit Report...
  [+] Report saved locally: upgrade_reports/test-rds-default_prep_report.json

=== Reboot Orchestration ===

=== Starting Reboot Orchestration ===
[*] Rebooting standard RDS instance: test-rds-default...
  [-] Instance test-rds-default is 'modifying'. Waiting 30s before rebooting...
  [-] Instance test-rds-default is 'modifying'. Waiting 30s before rebooting...
  [+] Reboot command issued.
  [*] Waiting for reboot to complete on test-rds-default...
  [-] test-rds-default status: 'rebooting'. Waiting 20s...
  [-] test-rds-default status: 'rebooting'. Waiting 20s...
  [+] test-rds-default is back online and 'available'.

=== Phase 2: Deployment Gateway ===
[*] Auto-deploy ENABLED. Triggering B/G deployment to 16.11...

=== Phase 2: Triggering Blue/Green Deployment ===

[*] Pre-Deployment Check: Verifying source database is ready...
  [+] Source instance is 'available' and ready for deployment.

[*] Pre-Deployment Verification: Re-reading actual attached PG from AWS...
  [*] AWS reports actual attached PG: 'test-rds-default-pg-15-7'
  [+] rds.logical_replication = 1 confirmed ACTIVE on 'test-rds-default-pg-15-7'. Safe to proceed.
[*] Validating Deployment payload...
  -> Target Version: 16.11
  -> Target Primary PG: test-rds-default-pg-16-11

  [SUCCESS] Deployment successfully triggered!
  [+] Deployment ID: bgd-ttafodfqsmuzi2y2

[*] Monitoring Deployment Status (bgd-ttafodfqsmuzi2y2)...
  [INFO] This process provisions new hardware and syncs your data.
  [INFO] It typically takes 15 to 45 minutes. Grab a coffee!
  [-] [0m elapsed] Status: PROVISIONING... building hardware.
  [-] [1m elapsed] Status: PROVISIONING... building hardware.
  [-] [2m elapsed] Status: PROVISIONING... building hardware.
  [-] [3m elapsed] Status: PROVISIONING... building hardware.
  [-] [4m elapsed] Status: PROVISIONING... building hardware.
  [-] [5m elapsed] Status: PROVISIONING... building hardware.
  [-] [6m elapsed] Status: PROVISIONING... building hardware.
  [-] [7m elapsed] Status: PROVISIONING... building hardware.
  [-] [8m elapsed] Status: PROVISIONING... building hardware.
  [-] [9m elapsed] Status: PROVISIONING... building hardware.
  [-] [10m elapsed] Status: PROVISIONING... building hardware.
  [-] [11m elapsed] Status: PROVISIONING... building hardware.
  [-] [12m elapsed] Status: PROVISIONING... building hardware.
  [-] [13m elapsed] Status: PROVISIONING... building hardware.
  [-] [14m elapsed] Status: PROVISIONING... building hardware.
  [-] [15m elapsed] Status: PROVISIONING... building hardware.
  [-] [16m elapsed] Status: PROVISIONING... building hardware.
  [-] [17m elapsed] Status: PROVISIONING... building hardware.

  [SUCCESS] The Green environment is now AVAILABLE and syncing!
  [+] Total provisioning time: 18 minutes.

=== Phase 3: Staging & Restoration ===
  [*] No Auto-Scaling policies to restore. Skipping Phase 3.

=== Phase 4: Cutover Gateway ===

[*] Pre-Switch Safety Check: Monitoring Replication Lag...
  [-] Polling CloudWatch for 'test-rds-default-green-b2xkzk' (Target lag: < 30s)
  [SUCCESS] Replication lag is safe: 0.0 seconds.
[*] Auto-switchover ENABLED. Bypassing human approval...

=== Phase 4: Executing Blue/Green Switchover ===
[*] Verifying Green target DB environment is fully available...
  [-] Waiting for Green environment to be available: test-rds-default-green-b2xkzk:modifying. Retrying in 15s...
  [-] Waiting for Green environment to be available: test-rds-default-green-b2xkzk:modifying. Retrying in 15s...
  [-] Waiting for Green environment to be available: test-rds-default-green-b2xkzk:modifying. Retrying in 15s...
  [-] Waiting for Green environment to be available: test-rds-default-green-b2xkzk:modifying. Retrying in 15s...
  [+] Green target environment is ready: test-rds-default-green-b2xkzk:available
  [INFO] Initiating DNS swap. Write traffic will briefly pause.
  [+] Switchover command accepted by AWS!

[*] Waiting for switchover to complete (this takes a few minutes)...
  [-] Status: SWITCHOVER_IN_PROGRESS... switching DNS records.
  [-] Status: SWITCHOVER_IN_PROGRESS... switching DNS records.
  [-] Status: SWITCHOVER_IN_PROGRESS... switching DNS records.

  [SUCCESS] Switchover Complete! Your Green environment is now Production.

[*] Post-Cutover: Deleting Blue/Green deployment mapping (keeping target)...

[*] Post-Cutover: Auto-stopping the old database to save costs...
  [-] Waiting for old database 'test-rds-default-old1' to become 'available'...
  [+] Old database 'test-rds-default-old1' is now 'available'.
  [SUCCESS] Shutdown signal sent to 'test-rds-default-old1'. It will remain stopped for up to 7 days.

=== UPGRADE WORKFLOW COMPLETE ===