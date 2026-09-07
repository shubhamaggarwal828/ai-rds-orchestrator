import unittest
from fastapi.testclient import TestClient
from server.server import app, app_state
from server.simulator import MockAWSClient
from agent.agent_engine import RDSUpgradeAgent

class TestAgentAndAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app_state["mode"] = "simulator"
        app_state["llm_provider"] = "built_in"

    def test_get_config(self):
        res = self.client.get("/api/config")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["mode"], "simulator")
        self.assertEqual(data["llm_provider"], "built_in")

    def test_list_databases(self):
        res = self.client.get("/api/databases")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("databases", data)
        self.assertGreater(len(data["databases"]), 0)
        # Verify cluster and instance discovery
        ids = [d["id"] for d in data["databases"]]
        self.assertIn("production-aurora-pg14", ids)
        self.assertIn("analytics-rds-pg13", ids)

    def test_audit_database_success(self):
        res = self.client.get("/api/databases/production-aurora-pg14/audit")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        audit = data["audit"]
        self.assertEqual(audit["database_id"], "production-aurora-pg14")
        self.assertEqual(audit["engine"], "aurora-postgresql")
        self.assertEqual(audit["current_version"], "14.9")
        self.assertEqual(audit["target_version"], "15.6")
        self.assertIn("static_enforcements", audit)

    def test_agent_chat_discover_fleet(self):
        res = self.client.post("/api/chat", json={"message": "List all databases in the fleet"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("reply", data)
        self.assertEqual(data["tool_called"], "list_databases")
        self.assertIn("production-aurora-pg14", data["reply"])

    def test_agent_chat_audit(self):
        res = self.client.post("/api/chat", json={"message": "Run preflight check on production-aurora-pg14"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("reply", data)
        self.assertEqual(data["tool_called"], "audit_preflight")
        self.assertIn("Pre-Flight Audit Report", data["reply"])

    def test_upgrade_pipeline_and_cutover(self):
        # 1. Start upgrade
        start_res = self.client.post("/api/upgrade/start", json={
            "db_identifier": "production-aurora-pg14",
            "target_version": "15.6",
            "auto_reboot": True,
            "auto_deploy": True,
            "auto_switchover": False
        })
        self.assertEqual(start_res.status_code, 200)
        task_id = start_res.json()["task_id"]

        # 2. Check status
        status_res = self.client.get(f"/api/upgrade/status/{task_id}")
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.json()
        self.assertEqual(status_data["db_identifier"], "production-aurora-pg14")

        # 3. Test cutover approval endpoint
        switch_res = self.client.post("/api/upgrade/switchover", json={"task_id": task_id})
        self.assertIn(switch_res.status_code, [200, 400])

    def test_doomsday_rollback_api(self):
        res = self.client.post("/api/doomsday/rollback", json={
            "db_identifier": "production-aurora-pg14",
            "is_cluster": True
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["restored_identifier"], "production-aurora-pg14")

if __name__ == "__main__":
    unittest.main()
