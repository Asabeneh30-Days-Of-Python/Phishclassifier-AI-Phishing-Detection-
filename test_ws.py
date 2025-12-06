# test_ws.py
import requests, json, websocket

# 1) Get the polling handshake to retrieve sid
poll = requests.get("http://127.0.0.1:5000/socket.io/?EIO=4&transport=polling")

# Response body starts with "0" then a JSON
payload = poll.text.lstrip("0")
data    = json.loads(payload)
sid     = data["sid"]
print("Got sid:", sid)

# 2) Perform the WebSocket upgrade with that sid
ws_url = (
    "ws://127.0.0.1:5000/socket.io/"
    f"?EIO=4&transport=websocket&sid={sid}"
)
print("Connecting to WS:", ws_url)
try:
    ws = websocket.create_connection(ws_url)
    msg = ws.recv()
    print("WS received:", msg)
    ws.close()
    print("✅ WebSocket upgrade succeeded")
except Exception as e:
    print("❌ WebSocket failed:", e)
