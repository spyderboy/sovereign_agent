from typing import TypedDict
import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

class AgentState(TypedDict):
    project_root:      str   # absolute path to the target project folder
    backlog_path:      str
    current_task:      str
    task_brief:        dict   # structured brief from Architect (task, file_hint, acceptance, complexity)
    build_status:      str
    test_logs:         str
    validator_verdict: dict   # structured verdict from Validator (status, summary, issues)
    iteration_count:   int


state_lock = threading.Lock()
state_manager = None


class StateManager:
    def __init__(self):
        self.state = {
            "project_root":  os.getcwd(),
            "backlog_path":  "backlog.md",
            "current_task":  "idle",
            "build_status":  "idle",
            "test_logs":     "",
            "iteration_count": 0
        }
        self.server = None
        self.running = False

    def start(self, port=50000):
        """Start the state management server"""
        print(f'StateManager starting on port {port}...')
        self.server = HTTPServer(('localhost', port), StateHandler)
        self.running = True
        print('StateManager server started!')
        print('Listening for requests...')
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            print('\nShutting down StateManager...')
            self.stop()

    def stop(self):
        """Stop the state management server"""
        if self.running:
            self.server.shutdown()
            self.server.socket.close()
            self.running = False
            print('StateManager stopped.')


class StateHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(content_length) if content_length > 0 else b''

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/v1.0/ready':
            self._send_json_response({"status": "ready", "port": 50000})
            return

        elif path == '/state':
            with state_lock:
                self._send_json_response(state_manager.state)
            return

        elif path == '/state/update':
            try:
                body = json.loads(self._read_body())
                for key, value in body.items():
                    if key in state_manager.state:
                        state_manager.state[key] = value
                with state_lock:
                    self._send_json_response({"status": "updated", "changes": list(body.keys())})
            except Exception as e:
                self._send_json_response({"error": str(e)}, 400)
            return

        elif path == '/state/set':
            try:
                body = json.loads(self._read_body())
                for key, value in body.items():
                    if key in state_manager.state:
                        state_manager.state[key] = value
                with state_lock:
                    self._send_json_response({"status": "set", "keys": list(body.keys())})
            except Exception as e:
                self._send_json_response({"error": str(e)}, 400)
            return

        else:
            self._send_json_response({"error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split('?')[0]

        if path == '/state/set':
            try:
                body = json.loads(self._read_body())
                for key, value in body.items():
                    if key in state_manager.state:
                        state_manager.state[key] = value
                with state_lock:
                    self._send_json_response({"status": "set", "keys": list(body.keys())})
            except Exception as e:
                self._send_json_response({"error": str(e)}, 400)
            return

        else:
            self._send_json_response({"error": "Not found"}, 404)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
        print(f'StateManager starting on port {port}...')
        state_manager = StateManager()
        state_manager.start(port)
    else:
        print('Usage: python state.py [port]')
