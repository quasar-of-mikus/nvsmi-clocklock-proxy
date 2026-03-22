Works fine with no measurable overhead if gen speed <= 100 t/s

Default config, edit per your machine:
```py
# --- Configuration ---
CLIENT_PORT = 3333
FORWARD_PROXY_PORT = 8080
BUFFER_SIZE = 16384

GPU_GRAPHICS_CLOCK = "1740"
GPU_MEMORY_CLOCK = "9999"
CLOCK_COOLDOWN_SECONDS = 1.0 # Avoids spam

GPU_BOUND_KEYWORDS = [
    b"/completion",
    b"/v1/chat/completions",
    b"/v1/completions",
    b"/embedding",
    b"/v1/embeddings"
]
```
