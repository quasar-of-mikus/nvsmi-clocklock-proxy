import socket
import threading
import select
import sys
import subprocess
import time

# --- Configuration ---
# Network
CLIENT_PORT = 3333  # Port for frontend
FORWARD_PROXY_PORT = 8080 # Port that the backend uses
BUFFER_SIZE = 4096

# GPU Performance
GPU_GRAPHICS_CLOCK = "1740" # The target graphics clock speed in MHz for nvidia-smi -lgc
GPU_MEMORY_CLOCK = "9999"   # The target memory clock speed in MHz for nvidia-smi -lmc
# --------------------

# Global counter for active connections and a lock for thread-safe access
active_connections_count = 0
active_connections_lock = threading.Lock()

# GPU Cooldown
LAST_CLOCK_CHANGE_TIME = 0.0
CLOCK_COOLDOWN_SECONDS = 0.5 # 1 second cooldown
cooldown_lock = threading.Lock()

# New global variables to handle cooldown logic
cooldown_timer = None 
last_pending_action = None # This will store the last requested action
ACTION_LOCK = 'lock'
ACTION_RESET = 'reset'

class ClientHandler(threading.Thread):
    def __init__(self, client_socket, client_address, forward_proxy_address):
        super().__init__()
        self.client_socket = client_socket
        self.client_address_str = f"{client_address[0]}:{client_address[1]}"
        self.forward_proxy_address = forward_proxy_address
        self.target_socket = None

    def _log(self, message):
        """Helper method for standardized logging."""
        print(f"[{self.client_address_str}] {message}")

    def _run_nvidia_smi(self, command, action_description):
        """A generic helper method to run nvidia-smi commands."""
        self._log(f"Attempting to {action_description}.")
        result = subprocess.run(command, capture_output=True, text=True)
        print(f"{action_description} STDOUT:\n", result.stdout)
        if result.stderr:
            print(f"{action_description} STDERR:\n", result.stderr)
        return result.returncode == 0

    def _lock_clocks(self):
        """Performs the actual clock lock."""
        self._log("Performing GPU clock lock.")
        gfx_ok = self._run_nvidia_smi(
            ['nvidia-smi', '-lgc', GPU_GRAPHICS_CLOCK],
            f"lock GPU graphics clock to {GPU_GRAPHICS_CLOCK} MHz"
        )
        mem_ok = self._run_nvidia_smi(
            ['nvidia-smi', '-lmc', GPU_MEMORY_CLOCK],
            f"lock GPU memory clock to {GPU_MEMORY_CLOCK} MHz"
        )
        if not all([gfx_ok, mem_ok]):
            self._log("WARNING: One or both nvidia-smi lock commands failed.")

    def _reset_clocks(self):
        """Performs the actual clock reset."""
        self._log("Performing GPU clock reset.")
        gfx_ok = self._run_nvidia_smi(['nvidia-smi', '-rgc'], "reset GPU graphics clock")
        mem_ok = self._run_nvidia_smi(['nvidia-smi', '-rmc'], "reset GPU memory clock")
        if not all([gfx_ok, mem_ok]):
            self._log("WARNING: One or both nvidia-smi reset commands failed.")

    def _cooldown_executor(self):
        """This function is executed by the timer to perform the last pending action."""
        global last_pending_action, LAST_CLOCK_CHANGE_TIME
        with cooldown_lock:
            # Execute the last remembered action
            if last_pending_action == ACTION_LOCK:
                self._lock_clocks()
            elif last_pending_action == ACTION_RESET:
                self._reset_clocks()
            
            LAST_CLOCK_CHANGE_TIME = time.time()
            last_pending_action = None # Clear the pending action after execution
            self._log("Cooldown executor finished.")

    def _schedule_action(self, action_type):
        """Schedules a GPU action, respecting the cooldown period."""
        global cooldown_timer, last_pending_action, LAST_CLOCK_CHANGE_TIME
        
        with cooldown_lock:
            # Always remember the latest action requested
            last_pending_action = action_type
            
            # If a timer is already running, we don't need to do anything else.
            # The next action will be the one we just saved.
            if cooldown_timer and cooldown_timer.is_alive():
                self._log(f"A timer is already active. Overwriting last pending action with '{action_type}'.")
                return

            current_time = time.time()
            time_since_last_change = current_time - LAST_CLOCK_CHANGE_TIME
            
            # If we're still in the cooldown period, schedule a new timer
            if time_since_last_change < CLOCK_COOLDOWN_SECONDS:
                delay = CLOCK_COOLDOWN_SECONDS - time_since_last_change
                self._log(f"Cooldown active. Scheduling '{action_type}' in {delay:.2f} seconds.")
                cooldown_timer = threading.Timer(delay, self._cooldown_executor)
                cooldown_timer.daemon = True
                cooldown_timer.start()
            else:
                # No cooldown, so execute immediately
                self._log(f"No cooldown active. Executing '{action_type}' immediately.")
                if action_type == ACTION_LOCK:
                    self._lock_clocks()
                elif action_type == ACTION_RESET:
                    self._reset_clocks()
                
                LAST_CLOCK_CHANGE_TIME = current_time
                last_pending_action = None # Clear the pending action

    def run(self):
        global active_connections_count
        is_connect_method = False
        try:
            self._log("Client connected.")

            with active_connections_lock:
                active_connections_count += 1
                self._log(f"Active connections: {active_connections_count}")
                if active_connections_count == 1:
                    self._log("First connection, scheduling GPU clocks to be locked.")
                    self._schedule_action(ACTION_LOCK)

            initial_request = self.client_socket.recv(BUFFER_SIZE)
            if not initial_request:
                self._log("No initial request from client. Closing.")
                return

            self._log(f"Received initial request ({len(initial_request)} bytes):\n{initial_request.decode('latin-1', errors='ignore')[:200]}...")
            is_connect_method = initial_request.startswith(b"CONNECT")

            if is_connect_method:
                try:
                    connect_line = initial_request.split(b'\r\n')[0].decode()
                    target_host, target_port_str = connect_line.split()[1].split(':')
                    target_port = int(target_port_str)
                except (ValueError, IndexError):
                    self._log("Malformed CONNECT request.")
                    return

                self._log(f"CONNECT request to {target_host}:{target_port}")
                self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.target_socket.connect((target_host, target_port))
                self.client_socket.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._log(f"Connecting to forward proxy: {self.forward_proxy_address[0]}:{self.forward_proxy_address[1]}")
                self.target_socket.connect(self.forward_proxy_address)
                self.target_socket.sendall(initial_request)
                self._log("Forwarded initial request to forward proxy.")

            self.mirror_traffic()

        except ConnectionRefusedError:
            self._log("Connection refused by target or forward proxy.")
            if not is_connect_method:
                try:
                    self.client_socket.sendall(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
                except Exception as e:
                    self._log(f"Error sending 503 to client: {e}")
        except socket.error as e:
            self._log(f"Socket error: {e}")
        except Exception as e:
            self._log(f"An unexpected error occurred: {e}")
        finally:
            self.close_connections()
            with active_connections_lock:
                active_connections_count -= 1
                self._log(f"Active connections: {active_connections_count}")
                if active_connections_count == 0:
                    self._log("Last connection closed. Scheduling GPU clocks to be reset.")
                    self._schedule_action(ACTION_RESET)

    def mirror_traffic(self):
        """Mirrors traffic between client and target sockets."""
        self._log("Starting traffic mirroring.")
        sockets = [self.client_socket, self.target_socket]
        socket_map = {self.client_socket: self.target_socket, self.target_socket: self.client_socket}
        socket_names = {self.client_socket: "Client", self.target_socket: "Target"}

        while True:
            try:
                readable, _, _ = select.select(sockets, [], [], 1)
                for sock in readable:
                    data = sock.recv(BUFFER_SIZE)
                    if not data:
                        self._log(f"{socket_names[sock]} closed connection.")
                        return
                    
                    other_sock = socket_map[sock]
                    other_sock.sendall(data)
            except (socket.error, OSError) as e:
                self._log(f"Mirroring socket error: {e}. Stopping.")
                return
            except Exception as e:
                self._log(f"Mirroring unexpected error: {e}. Stopping.")
                return

    def _close_socket(self, sock, name):
        """Helper to shut down and close a single socket."""
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError as e:
                self._log(f"Error closing {name} socket: {e}")

    def close_connections(self):
        """Closes both client and target connections."""
        self._log("Closing connections.")
        self._close_socket(self.client_socket, "client")
        self._close_socket(self.target_socket, "target")
        self._log("Connections closed.")

class ProxyServer:
    def __init__(self, client_port, forward_proxy_port):
        self.client_port = client_port
        self.forward_proxy_port = forward_proxy_port
        self.client_listener = None
        self.running = False

    def start(self):
        try:
            self.client_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.client_listener.bind(('127.0.0.1', self.client_port))
            self.client_listener.listen(5)
            self.running = True
            print(f"Proxy server listening on 127.0.0.1:{self.client_port}")
            print(f"HTTP traffic will be forwarded to 127.0.0.1:{self.forward_proxy_port}")
            print("HTTPS (CONNECT) traffic will be tunnelled directly to the destination.")

            while self.running:
                try:
                    client_socket, client_address = self.client_listener.accept()
                    handler = ClientHandler(client_socket, client_address, ('127.0.0.1', self.forward_proxy_port))
                    handler.daemon = True
                    handler.start()
                except OSError as e:
                    if self.running:
                        print(f"Error accepting client connection: {e}")
        except OSError as e:
            print(f"Failed to start server: {e}")
            if e.errno == 98: # Address already in use
                print(f"Port {self.client_port} is already in use.")
            sys.exit(1)
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if self.client_listener:
            print("\nShutting down server...")
            self.client_listener.close()
            print("Server shut down.")

if __name__ == "__main__":
    proxy = ProxyServer(CLIENT_PORT, FORWARD_PROXY_PORT)
    try:
        proxy.start()
    except KeyboardInterrupt:
        pass # The stop() method is called in the finally block
    finally:
        proxy.stop()