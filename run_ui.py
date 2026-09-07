#!/usr/bin/env python3
"""
AWS RDS Blue/Green Upgrade Orchestrator - AI Agentic Dashboard Launcher
Run this script to start the FastAPI web server and launch the AI Orchestrator UI.
"""
import sys
import os
import uvicorn
import webbrowser
import threading
import time

def open_browser():
    time.sleep(1.2)
    url = "http://127.0.0.1:8000"
    print(f"\n[+] Opening RDS AI Upgrade Orchestrator in browser: {url}\n")
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[-] Could not automatically launch browser: {e}")

if __name__ == "__main__":
    print("==========================================================")
    print("   AWS RDS & Aurora AI Agent Upgrade Orchestrator v2.0    ")
    print("==========================================================")
    print("[*] Starting backend server on http://127.0.0.1:8000 ...")

    # Start browser opener in background thread
    threading.Thread(target=open_browser, daemon=True).start()

    # Start Uvicorn web server
    uvicorn.run("server.server:app", host="127.0.0.1", port=8000, log_level="info", reload=False)
