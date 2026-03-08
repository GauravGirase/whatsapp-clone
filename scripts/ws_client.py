#!/usr/bin/env python3
"""
Manual WebSocket test client.
Usage:
  pip install websockets httpx
  python scripts/ws_client.py --username alice --password SecurePass1
"""
import asyncio
import json
import argparse
import httpx
import websockets


API_BASE = "http://localhost:8000/api/v1"
WS_URL   = "ws://localhost:8000/ws"


async def get_token(username: str, password: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{API_BASE}/auth/login", json={
            "username": username,
            "password": password,
        })
        resp.raise_for_status()
        return resp.json()["access_token"]


async def interactive_client(token: str):
    uri = f"{WS_URL}?token={token}"
    print(f"🔌 Connecting to {uri}")

    async with websockets.connect(uri) as ws:
        print("✅ Connected! Type JSON events or shortcuts:")
        print("  ping          → send ping")
        print("  typing <id>   → send typing_start to conversation")
        print("  read <ids>    → mark message ids as read (comma-separated)")
        print("  quit          → disconnect\n")

        async def receive_loop():
            async for message in ws:
                event = json.loads(message)
                print(f"\n📩 {json.dumps(event, indent=2)}")

        recv_task = asyncio.create_task(receive_loop())

        try:
            while True:
                line = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
                if line.strip() == "quit":
                    break
                elif line.strip() == "ping":
                    await ws.send(json.dumps({"type": "ping"}))
                elif line.startswith("typing "):
                    conv_id = line.split(" ", 1)[1].strip()
                    await ws.send(json.dumps({
                        "type": "typing_start",
                        "data": {"conversation_id": conv_id}
                    }))
                elif line.startswith("read "):
                    ids = [i.strip() for i in line.split(" ", 1)[1].split(",")]
                    await ws.send(json.dumps({
                        "type": "read_receipt",
                        "data": {"message_ids": ids}
                    }))
                else:
                    try:
                        event = json.loads(line)
                        await ws.send(json.dumps(event))
                    except json.JSONDecodeError:
                        print("❌ Invalid JSON. Try: ping, typing <id>, read <ids>, or raw JSON")
        finally:
            recv_task.cancel()

    print("👋 Disconnected")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="alice")
    parser.add_argument("--password", default="SecurePass1")
    args = parser.parse_args()

    print(f"🔑 Logging in as {args.username}...")
    token = await get_token(args.username, args.password)
    print(f"✅ Got token: {token[:40]}...")
    await interactive_client(token)


if __name__ == "__main__":
    asyncio.run(main())
