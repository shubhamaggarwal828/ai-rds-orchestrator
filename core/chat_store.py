import os
import json
import time
from typing import Dict, Any, List, Optional

CHAT_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chat_history.json")

class ChatStore:
    """Thread-safe persistent JSON store supporting multiple isolated Chat Sessions (per RDS or topic)."""
    
    def __init__(self, file_path: str = CHAT_FILE_PATH):
        self.file_path = file_path
        self._ensure_storage()

    def _ensure_storage(self):
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        if not os.path.exists(self.file_path):
            initial_data = {
                "active_session_id": "session_default",
                "user_profile": {
                    "name": "Cloud DevOps Lead",
                    "role": "Database Administrator",
                    "avatar": "👨‍💻"
                },
                "sessions": {
                    "session_default": {
                        "id": "session_default",
                        "title": "General Fleet Copilot",
                        "db_identifier": None,
                        "created_at": time.time(),
                        "updated_at": time.time(),
                        "messages": [
                            {
                                "id": "msg_welcome",
                                "sender": "agent",
                                "user_name": "RDS Upgrade AI Agent",
                                "user_role": "AI Copilot",
                                "avatar": "🤖",
                                "timestamp": time.strftime("%H:%M:%S"),
                                "text": "Hello! I am your persistent **AWS RDS & Aurora Upgrade AI Copilot**.\n\nYou can create dedicated chat sessions per database using **`+ New Chat`** or ask directly:\n- **\"Upgrade test-rds-pg14 to 18.3\"**\n- **\"Rename test-rds-pg14 to poc-rds-standalone\"**\n- **\"Stop old database test-rds-pg14-old1\"**\n- **\"Delete old database test-rds-pg14-old1\"**\n- **\"Doomsday rollback test-aurora-pg14\"**",
                                "action_buttons": []
                            }
                        ]
                    }
                }
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(initial_data, f, indent=2, default=str)
        else:
            # Auto-migrate legacy format if needed
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if "sessions" not in raw:
                    legacy_msgs = raw.get("messages", [])
                    raw["sessions"] = {
                        "session_default": {
                            "id": "session_default",
                            "title": "General Fleet Copilot",
                            "db_identifier": None,
                            "created_at": time.time(),
                            "updated_at": time.time(),
                            "messages": legacy_msgs
                        }
                    }
                    raw["active_session_id"] = "session_default"
                    with open(self.file_path, "w", encoding="utf-8") as f:
                        json.dump(raw, f, indent=2, default=str)
            except Exception:
                pass

    def get_data(self) -> Dict[str, Any]:
        self._ensure_storage()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Clean up ghost/empty messages
            changed = False
            for s_id, s in data.get("sessions", {}).items():
                orig_len = len(s.get("messages", []))
                s["messages"] = [
                    m for m in s.get("messages", [])
                    if (m.get("text") and m.get("text").strip()) or (m.get("action_buttons") and len(m.get("action_buttons")) > 0)
                ]
                if len(s["messages"]) != orig_len:
                    changed = True
            if changed:
                self._save_data(data)
            return data
        except Exception:
            return {
                "active_session_id": "session_default",
                "user_profile": {"name": "Cloud DevOps Lead", "role": "Database Administrator", "avatar": "👨‍💻"},
                "sessions": {}
            }

    def _save_data(self, data: Dict[str, Any]):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

    def get_user_profile(self) -> Dict[str, str]:
        return self.get_data().get("user_profile", {
            "name": "Cloud DevOps Lead",
            "role": "Database Administrator",
            "avatar": "👨‍💻"
        })

    def update_user_profile(self, name: Optional[str] = None, role: Optional[str] = None, avatar: Optional[str] = None) -> Dict[str, str]:
        data = self.get_data()
        profile = data.setdefault("user_profile", {})
        if name:
            profile["name"] = name
        if role:
            profile["role"] = role
        if avatar:
            profile["avatar"] = avatar
        self._save_data(data)
        return profile

    # --- SESSIONS API ---

    def list_sessions(self) -> List[Dict[str, Any]]:
        data = self.get_data()
        sessions = data.get("sessions", {})
        active_id = data.get("active_session_id", "session_default")
        
        result = []
        for s_id, s in sessions.items():
            result.append({
                "id": s["id"],
                "title": s.get("title", "Chat Session"),
                "db_identifier": s.get("db_identifier"),
                "created_at": s.get("created_at", time.time()),
                "updated_at": s.get("updated_at", time.time()),
                "message_count": len(s.get("messages", [])),
                "is_active": (s_id == active_id),
                "last_message": s.get("messages", [])[-1]["text"][:60] if s.get("messages") else ""
            })
        
        return sorted(result, key=lambda x: x["updated_at"], reverse=True)

    def create_session(self, title: Optional[str] = None, db_identifier: Optional[str] = None) -> Dict[str, Any]:
        data = self.get_data()
        sessions = data.setdefault("sessions", {})
        
        s_id = f"session_{int(time.time()*1000)}"
        if not title:
            if db_identifier:
                title = f"{db_identifier} Upgrade"
            else:
                title = f"Chat Session #{len(sessions) + 1}"

        welcome_text = f"Started new dedicated chat thread for **`{db_identifier}`**." if db_identifier else "Started a new AI Copilot conversation."
        
        new_session = {
            "id": s_id,
            "title": title,
            "db_identifier": db_identifier,
            "created_at": time.time(),
            "updated_at": time.time(),
            "messages": [
                {
                    "id": f"msg_welcome_{s_id}",
                    "sender": "agent",
                    "user_name": "RDS Upgrade AI Agent",
                    "user_role": "AI Copilot",
                    "avatar": "🤖",
                    "timestamp": time.strftime("%H:%M:%S"),
                    "text": welcome_text,
                    "action_buttons": [
                        {"label": f"🛡️ Audit {db_identifier}", "action": "audit", "target": db_identifier, "style": "secondary"},
                        {"label": f"🚀 Upgrade {db_identifier}", "action": "upgrade_prompt", "target": db_identifier, "style": "primary"}
                    ] if db_identifier else []
                }
            ]
        }
        
        sessions[s_id] = new_session
        data["active_session_id"] = s_id
        self._save_data(data)
        return new_session

    def get_session(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        data = self.get_data()
        sessions = data.setdefault("sessions", {})
        
        if not session_id or session_id not in sessions:
            session_id = data.get("active_session_id")
            if not session_id or session_id not in sessions:
                if sessions:
                    session_id = list(sessions.keys())[0]
                else:
                    return self.create_session()
        
        data["active_session_id"] = session_id
        self._save_data(data)
        return sessions[session_id]

    def delete_session(self, session_id: str) -> bool:
        data = self.get_data()
        sessions = data.get("sessions", {})
        if session_id in sessions and len(sessions) > 1:
            del sessions[session_id]
            if data.get("active_session_id") == session_id:
                data["active_session_id"] = list(sessions.keys())[0]
            self._save_data(data)
            return True
        return False

    def add_message(
        self,
        sender: str,
        text: str,
        session_id: Optional[str] = None,
        user_name: Optional[str] = None,
        user_role: Optional[str] = None,
        avatar: Optional[str] = None,
        action_buttons: Optional[List[Dict[str, Any]]] = None,
        tool_called: Optional[str] = None,
        tool_result: Optional[Any] = None
    ) -> Dict[str, Any]:
        data = self.get_data()
        profile = data.get("user_profile", {})
        sessions = data.setdefault("sessions", {})
        
        if not session_id or session_id not in sessions:
            session_id = data.get("active_session_id")
            if not session_id or session_id not in sessions:
                session_obj = self.create_session()
                session_id = session_obj["id"]
                data = self.get_data()
                sessions = data.get("sessions", {})

        session = sessions[session_id]

        if sender == "user":
            u_name = user_name or profile.get("name", "Cloud DevOps Lead")
            u_role = user_role or profile.get("role", "Database Administrator")
            u_avatar = avatar or profile.get("avatar", "👨‍💻")
        else:
            u_name = "RDS Upgrade AI Agent"
            u_role = "AI Copilot"
            u_avatar = "🤖"
            if not (text and text.strip()) and not action_buttons:
                text = "I have processed your request. Please let me know what actions or checks you'd like to perform next."

        msg_id = f"msg_{int(time.time()*1000)}"
        msg_obj = {
            "id": msg_id,
            "sender": sender,
            "user_name": u_name,
            "user_role": u_role,
            "avatar": u_avatar,
            "timestamp": time.strftime("%H:%M:%S"),
            "text": text or "",
            "action_buttons": action_buttons or [],
            "tool_called": tool_called,
            "tool_result": tool_result
        }

        session.setdefault("messages", []).append(msg_obj)
        session["updated_at"] = time.time()
        
        # Auto-update session title if it was a default title and this is first user message
        if sender == "user" and session.get("title", "").startswith("Chat Session #"):
            session["title"] = text[:32] + ("..." if len(text) > 32 else "")

        self._save_data(data)
        return msg_obj

    def clear_session(self, session_id: Optional[str] = None):
        data = self.get_data()
        sessions = data.get("sessions", {})
        if not session_id:
            session_id = data.get("active_session_id", "session_default")
            
        if session_id in sessions:
            sessions[session_id]["messages"] = [
                {
                    "id": f"msg_welcome_{session_id}",
                    "sender": "agent",
                    "user_name": "RDS Upgrade AI Agent",
                    "user_role": "AI Copilot",
                    "avatar": "🤖",
                    "timestamp": time.strftime("%H:%M:%S"),
                    "text": "Thread cleared. How can I assist you with this database?",
                    "action_buttons": []
                }
            ]
            sessions[session_id]["updated_at"] = time.time()
            self._save_data(data)
