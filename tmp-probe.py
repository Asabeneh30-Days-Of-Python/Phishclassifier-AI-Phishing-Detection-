import socket,sys
try:
    s = socket.create_connection(("redis", 6379), timeout=3)
    s.close()
    print("PY_OK")
except Exception as e:
    print("PY_FAIL", type(e).__name__, e)
    sys.exit(1)