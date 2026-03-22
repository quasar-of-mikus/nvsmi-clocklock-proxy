import socket
import threading
import select
import subprocess
import time
import queue
import sys

# --- Configuration ---
CLIENT_PORT = 3333
FORWARD_PROXY_PORT = 8080
BUFFER_SIZE = 16384

GPU_GRAPHICS_CLOCK = "1740"
GPU_MEMORY_CLOCK = "9999"
CLOCK_COOLDOWN_SECONDS = 1.0

GPU_BOUND_KEYWORDS = [
    b"/completion",
    b"/v1/chat/completions",
    b"/v1/completions",
    b"/embedding",
    b"/v1/embeddings"
]

def log(msg):
    print(f"{msg}", flush=True)

class GPUCommandWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.cmd_queue = queue.Queue()
        self.last_execution_time = 0

    def request_action(self, action):
        self.cmd_queue.put(action)

    def run(self):
        while True:
            action = self.cmd_queue.get()
            try:
                while not self.cmd_queue.empty():
                    action = self.cmd_queue.get_nowait()
            except queue.Empty: pass

            elapsed = time.time() - self.last_execution_time
            if elapsed < CLOCK_COOLDOWN_SECONDS:
                time.sleep(CLOCK_COOLDOWN_SECONDS - elapsed)

            if action == 'LOCK':
                log(f" >>> [GPU] LOCKING: {GPU_GRAPHICS_CLOCK}/{GPU_MEMORY_CLOCK}")
                subprocess.run(['nvidia-smi', '-lgc', GPU_GRAPHICS_CLOCK], capture_output=True)
                subprocess.run(['nvidia-smi', '-lmc', GPU_MEMORY_CLOCK], capture_output=True)
            elif action == 'RESET':
                log(" <<< [GPU] RESETTING CLOCKS")
                subprocess.run(['nvidia-smi', '-rgc'], capture_output=True)
                subprocess.run(['nvidia-smi', '-rmc'], capture_output=True)

            self.last_execution_time = time.time()
            self.cmd_queue.task_done()

gpu_worker = GPUCommandWorker()
gpu_worker.start()

gpu_tasks_count = 0
gpu_tasks_lock = threading.Lock()

class ClientHandler(threading.Thread):
    def __init__(self, client_socket, client_address, forward_proxy_address):
        super().__init__(daemon=True)
        self.client_socket = client_socket
        self.client_address = client_address
        self.forward_proxy_address = forward_proxy_address
        self.target_socket = None
        self.is_gpu_active_for_this_conn = False
        self.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def run(self):
        global gpu_tasks_count
        try:
            # log(f"[Conn] New connection from {self.client_address[0]}")
            
            # Initial peek to handle connection setup
            initial_data = self.client_socket.recv(BUFFER_SIZE)
            if not initial_data:
                return

            self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.target_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            if initial_data.startswith(b"CONNECT"):
                target_info = initial_data.split(b'\r\n')[0].split()[1].decode().split(':')
                self.target_socket.connect((target_info[0], int(target_info[1])))
                self.client_socket.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                self.target_socket.connect(self.forward_proxy_address)
                # We process this first chunk like any other in the loop
                self.process_client_data(initial_data)

            self.mirror_traffic()

        except Exception as e:
            pass
        finally:
            self.close_connections()

    def process_client_data(self, data):
        """Checks data for keywords and forwards it."""
        global gpu_tasks_count
        
        # Only scan if we haven't already activated GPU for this specific connection
        if not self.is_gpu_active_for_this_conn:
            lower_data = data[:2048].lower() # Scanning first 2kb is enough for headers
            for kw in GPU_BOUND_KEYWORDS:
                if kw in lower_data:
                    log(f"[Detect] GPU Path Found: {kw.decode()}")
                    self.is_gpu_active_for_this_conn = True
                    with gpu_tasks_lock:
                        gpu_tasks_count += 1
                        if gpu_tasks_count == 1:
                            gpu_worker.request_action('LOCK')
                    break
        
        self.target_socket.sendall(data)

    def mirror_traffic(self):
        sockets = [self.client_socket, self.target_socket]
        while True:
            try:
                readable, _, _ = select.select(sockets, [], [], 1.0)
                if not readable: continue
                
                for sock in readable:
                    data = sock.recv(BUFFER_SIZE)
                    if not data: return
                    
                    if sock is self.client_socket:
                        self.process_client_data(data)
                    else:
                        self.client_socket.sendall(data)
            except: return

    def close_connections(self):
        global gpu_tasks_count
        # If this connection was responsible for a GPU lock, decrement
        if self.is_gpu_active_for_this_conn:
            with gpu_tasks_lock:
                gpu_tasks_count -= 1
                if gpu_tasks_count == 0:
                    gpu_worker.request_action('RESET')
        
        for s in [self.client_socket, self.target_socket]:
            if s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                    s.close()
                except: pass

class ProxyServer:
    def __init__(self, port, target_port):
        self.port = port
        self.target_port = target_port

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', self.port))
        listener.listen(128)
        log(f"Proxy: 127.0.0.1:{self.port} -> 127.0.0.1:{self.target_port}")
        try:
            while True:
                client_sock, addr = listener.accept()
                ClientHandler(client_sock, addr, ('127.0.0.1', self.target_port)).start()
        except KeyboardInterrupt: pass
        finally: listener.close()

if __name__ == "__main__":
    ProxyServer(CLIENT_PORT, FORWARD_PROXY_PORT).start()
