#to understand how slow context switching will make cpu computations due to Global Interpreter Lock



import threading
import time

def heavy_computation():
    s=sum([i**2 for i in range(1000000)])
    print(s) 


start = time.time()
# Run sequentially
heavy_computation()
heavy_computation()
print(f"Sequential Time: {time.time() - start:.4f}s")

start = time.time()
# Run with threads (CPU Bound)
t1 = threading.Thread(target=heavy_computation)
t2 = threading.Thread(target=heavy_computation)
t1.start(); t2.start()
t1.join(); t2.join()
# This is usually SLOWER or same as sequential, proving the GIL limits CPU tasks
print(f"Threaded Time:   {time.time() - start:.4f}s")