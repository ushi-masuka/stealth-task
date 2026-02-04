import sys
import time
import threading
import os
import random

# --- 1. CONFIGURATION & ANSI MAGIC ---
os.system("")  # Enable ANSI colors in Windows CMD

# ANSI Codes for screen manipulation
ESC = "\033"
SAVE = f"{ESC}7"     # Save Cursor Position
RESTORE = f"{ESC}8"  # Restore Cursor Position
CLR_LINE = f"{ESC}[K"
GREEN, RED, CYAN, YELLOW, RESET = f"{ESC}[32m", f"{ESC}[31m", f"{ESC}[36m", f"{ESC}[33m", f"{ESC}[0m"

# Global State
state = {
    "running": True,
    "symbol": "WAITING",  # The symbol currently being WATCHED on screen
    "ltp": 0.0,
    "logs": [""] * 5
}

# --- 2. HELPER FUNCTIONS ---
def print_at(row, text):
    """Moves cursor to specific row and prints text without scrolling."""
    sys.stdout.write(f"{ESC}[{row};1H{text}{CLR_LINE}")
    sys.stdout.flush()

def log(msg):
    """Adds message to the fixed log window (Lines 3-7)"""
    timestamp = time.strftime("%H:%M:%S")
    state["logs"].pop(0)
    state["logs"].append(f"[{timestamp}] {msg}")

def get_input_at(row, prompt, validator=None, error_msg="Invalid Input"):
    """Locks the user on a specific row until valid input is provided."""
    while True:
        sys.stdout.write(f"{ESC}[{row};1H{CLR_LINE}{prompt}")
        sys.stdout.flush()
        txt = sys.stdin.readline().strip().upper()
        
        if not txt: continue
        if txt == 'EXIT': return 'EXIT'
        
        if validator:
            try:
                return validator(txt)
            except ValueError:
                sys.stdout.write(f"{ESC}[{row};1H{CLR_LINE}{RED}{error_msg}{RESET}")
                sys.stdout.flush()
                time.sleep(1)
                continue
        return txt

# --- 3. UI THREAD (The Visuals) ---
def ui_renderer():
    """Constantly refreshes Top Bar and Logs, leaving Input area alone."""
    os.system('cls' if os.name == 'nt' else 'clear')
    print_at(2, "-" * 60)
    print_at(8, "-" * 60)

    while state["running"]:
        sys.stdout.write(SAVE)
        
        if state["symbol"] == "WAITING":
            sym_display = f"{YELLOW}WAITING...{RESET}"
            ltp_display = "0.00"
        else:
            sym_display = f"{CYAN}{state['symbol']:<10}{RESET}"
            ltp_display = f"{state['ltp']:.2f}"

        header = f"{GREEN}LIVE TERMINAL{RESET} | Symbol: {sym_display} | LTP: {ltp_display}"
        print_at(1, header)
        
        for i, line in enumerate(state["logs"]):
            print_at(3 + i, line)
            
        sys.stdout.write(RESTORE)
        sys.stdout.flush()
        time.sleep(0.2)

# --- 4. TRADING LOGIC (The Brain) ---
class TradeWorker(threading.Thread):
    def __init__(self, api, symbol, txn, qty, sl, tgt):
        super().__init__()
        self.api = api
        self.details = (symbol, txn, qty, sl, tgt)
        self.daemon = True

    def run(self):
        sym, txn, qty, sl_pts, tgt_pts = self.details
        log(f"{CYAN}OPEN:{RESET} {sym} {txn} Market Order...")
        
        # 1. Place Market Entry
        self.api.place_order(sym, "MKT", qty, 0)
        
        # 2. Simulate Wait for Fill
        time.sleep(1)
        # Fetch the SPECIFIC price for this symbol, not the global state
        fill_price = self.api.get_last_price(sym)
        log(f"{GREEN}FILLED:{RESET} {sym} @ {fill_price:.2f}")

        # 3. Calculate Legs
        if txn == "B":
            sl_price = fill_price - sl_pts
            tgt_price = fill_price + tgt_pts
        else:
            sl_price = fill_price + sl_pts
            tgt_price = fill_price - tgt_pts

        log(f"OCO: {sym} SL @ {sl_price:.2f} | TGT @ {tgt_price:.2f}")
        
        # 4. Place Fake OCO Orders
        sl_id = self.api.place_order(sym, "SL", qty, sl_price)
        tgt_id = self.api.place_order(sym, "L", qty, tgt_price)

        # 5. Watch for Exit
        while state["running"]:
            time.sleep(0.5)
            if self.api.check_status(tgt_id):
                log(f"{GREEN}PROFIT:{RESET} {sym} Target Hit! Cancelling SL.")
                self.api.cancel_order(sl_id)
                break
            if self.api.check_status(sl_id):
                log(f"{RED}LOSS:{RESET} {sym} SL Hit! Cancelling Target.")
                self.api.cancel_order(tgt_id)
                break

# --- 5. SMART MOCK API (The Simulator) ---
class MockAPI:
    def __init__(self):
        self.orders = {}
        # MEMORY: Stores prices for all symbols { 'RELIANCE': 2500.0, 'TCS': 3000.0 }
        self.market_data = {} 
        threading.Thread(target=self._market_simulator, daemon=True).start()

    def get_last_price(self, sym):
        """Returns the current simulated price for a specific symbol."""
        if sym not in self.market_data:
            # Initialize price if seen for first time
            self.market_data[sym] = random.uniform(500.0, 3000.0)
        return self.market_data[sym]

    def _market_simulator(self):
        while state["running"]:
            time.sleep(0.5)
            
            # 1. Update prices for ALL tracked symbols
            for sym in self.market_data:
                self.market_data[sym] += random.choice([-2, -1, -0.5, 0.5, 1, 2])
            
            # 2. Update the GLOBAL view only for the watched symbol
            if state["symbol"] in self.market_data:
                state["ltp"] = self.market_data[state["symbol"]]

            # 3. Check Triggers (Context Aware)
            for oid, order in self.orders.items():
                if order['status'] == 'PENDING':
                    # Find the price for THIS order's symbol
                    symbol = order['symbol']
                    current_price = self.market_data.get(symbol, 0)
                    
                    # Trigger Check (Simplified)
                    if abs(current_price - order['price']) < 2.0: 
                        order['status'] = 'TRADED'

    def place_order(self, sym, type, qty, price):
        # Ensure symbol exists in market data
        self.get_last_price(sym)
        
        oid = str(random.randint(1000, 9999))
        self.orders[oid] = {
            'symbol': sym, # Track which symbol this order belongs to
            'price': price, 
            'status': 'PENDING'
        }
        return oid

    def check_status(self, oid):
        return self.orders.get(oid, {}).get('status') == 'TRADED'

    def cancel_order(self, oid):
        if oid in self.orders: self.orders[oid]['status'] = 'CANCELLED'

# --- 6. MAIN INPUT LOOP ---
def main():
    api = MockAPI()
    
    threading.Thread(target=ui_renderer, daemon=True).start()
    time.sleep(0.5)
    input_row = 10
    
    while True:
        try:
            sys.stdout.write(f"{ESC}[{input_row};1H{CLR_LINE}")
            
            # 1. Get Symbol
            sym = get_input_at(input_row, "Enter Symbol (or 'exit'): ")
            if sym == 'EXIT': 
                state["running"] = False
                break
            
            # Update View & Initialize Price Memory
            state["symbol"] = sym
            api.get_last_price(sym) # Ensures Mock API starts tracking it

            # 2. Get Trade Details
            txn = get_input_at(input_row, "Buy/Sell (B/S): ", lambda x: x if x in ['B','S'] else (_ for _ in ()).throw(ValueError), "Enter B or S")
            if txn == 'EXIT': break

            qty = get_input_at(input_row, "Quantity: ", int, "Must be a number")
            if qty == 'EXIT': break
                
            sl = get_input_at(input_row, "SL Points: ", float, "Must be a number")
            if sl == 'EXIT': break

            tgt = get_input_at(input_row, "Target Points: ", float, "Must be a number")
            if tgt == 'EXIT': break
            
            # Start Trade
            TradeWorker(api, sym, txn, qty, sl, tgt).start()
            
            sys.stdout.write(f"{ESC}[{input_row};1H{CLR_LINE}{GREEN}Trade Started!{RESET}")
            time.sleep(1)

        except KeyboardInterrupt:
            state["running"] = False
            break

    print(f"\n{RESET}System Shutdown.")

if __name__ == "__main__":
    main()