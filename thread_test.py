import sys
import time
import threading
import os; os.system('')

# ANSI Escape Codes
MOVE_UP = "\033[F"
CLEAR_LINE = "\033[K"
SAVE_CURSOR = "\033[s"  # Saves where you are currently typing
RESTORE_CURSOR = "\033[u" # Jumps back to where you were
HOME = "\033[H" # Jumps to the top left corner




def counter():

    #clears screen at startup
    sys.stdout.write("\033[2J")

    for i in range(101):
        sys.stdout.write(SAVE_CURSOR) #save where the cursor was

        sys.stdout.write("\033[1;1H" + CLEAR_LINE) #to jump to the top
        sys.stdout.write(f'live counter:{i}')
        
        sys.stdout.write(RESTORE_CURSOR)
        sys.stdout.flush()
        
        time.sleep(0.2)
    return None




if __name__=="__main__":
    t=threading.Thread(target=counter)
    t.start()
    user_input=input("\nplease enter your name: ")
    print(f'\n{user_input}')


    