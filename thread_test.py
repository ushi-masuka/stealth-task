import sys
import time
import threading
import os; os.system('')

# ANSI Escape Codes
CLEAR_SCREEN = "\033[2J"
MOVE_TO_TOP = "\033[1;1H"
CLEAR_LINE = "\033[K"
SAVE_CURSOR = "\033[s"
RESTORE_CURSOR = "\033[u"

def counter():
    # Note: We removed the clear screen from here. 
    # The thread should only update data, not wipe the UI.
    
    for i in range(50):
        # 1. Save cursor (User is typing at the bottom)
        sys.stdout.write(SAVE_CURSOR)

        # 2. Jump strictly to Line 1, Column 15 (After "Live Counter: ")
        # We don't overwrite the whole line, just the number part!
        sys.stdout.write("\033[1;15H" + CLEAR_LINE + f"{i}")

        # 3. Restore cursor (Back to bottom)             
        sys.stdout.write(RESTORE_CURSOR)
        sys.stdout.flush()   
        
        time.sleep(0.2)

if __name__=="__main__":
    # STEP 1: Main thread sets up the "UI Canvas"
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.write(MOVE_TO_TOP)
    
    # Print the Layout
    print("Live Counter: --")  # This is Line 1
    print("----------------")  # This is Line 2
    # The input prompt will be on Line 3
    
    # STEP 2: Start the background worker
    t = threading.Thread(target=counter)
    t.start()
    
    # STEP 3: User Interaction
    # We use a standard print for the prompt to ensure the cursor sits correctly
    user_input = input("Please enter your name: ")
    
    # Clean up (Show we are done)
    print(f"\nAccepted: {user_input}")