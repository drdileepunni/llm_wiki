import threading

# Set by the uvicorn shutdown hook. Checked by long-running pipeline threads
# between chunks and between file writes so they can exit cleanly on Ctrl+C.
shutdown_event = threading.Event()
