import threading
import time #couldnt get race condition without getting the update val to sleep . since the instruction was incredibly fast. 


db={"val":0}

def update_val():
    local_copy=db['val']

    #on making it sleep for various different amounts of time we can see different values in the result. (mostly random)
    time.sleep(0.00001)
    local_copy+=1
    db['val']=local_copy

threads=[]

for i in range(1000):
    t=threading.Thread(target=update_val)
    threads.append(t)
    t.start()


for t in threads:
    t.join()

print(db['val'])