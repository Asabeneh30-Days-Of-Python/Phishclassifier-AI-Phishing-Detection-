# wsgi.py
import eventlet
eventlet.monkey_patch()  # enable async sockets before any imports

from api.app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    # debug=True for development; use_reloader=False prevents duplicate workers
    socketio.run(
         app,
         host="0.0.0.0",
         port=5000,
         debug=True,
         use_reloader=False,
         allow_unsafe_werkzeug=True
     )
