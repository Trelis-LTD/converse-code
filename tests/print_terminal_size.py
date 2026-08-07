import os


size = os.get_terminal_size()
print(f"{size.columns}x{size.lines}", flush=True)
