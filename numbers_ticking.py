import sys
import time

for i in range(100):
    
    sys.stdout.write(f'\r{i}')
    sys.stdout.flush()
    time.sleep(0.1)
