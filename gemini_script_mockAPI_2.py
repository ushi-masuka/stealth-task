import sys
import time
import threading
import os
import random  # Only for Mock generation

# Try to import NeoAPI, fall back to Mock if not installed
try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None

# --- ANSI ESCAPE CODES (For Terminal UI) ---
# These control the cursor position to allow printing while input is active
ESC = "\033"
SAVE_CURSOR = f"{ESC}[s"     # Save current cursor position
RESTORE_CURSOR = f"{ESC}[u"  # Restore saved cursor position
MOVE_TO_TOP = f"{ESC}[H"     # Move cursor to top-left (Row 1, Col 1)
CLEAR_LINE = f"{ESC}[K"      # Clear the current line
GREEN = f"{ESC}[32m"
RESET = f"{ESC}[0m"

# --- GLOBAL SHARED STATE ---
current_monitoring_symbol = None
current_ltp = 0.0
shutdown_event = threading.Event()

# Lock to prevent print statements from colliding
print_lock = threading.Lock()

# ==========================================
# 1. DISPLAY ENGINE (Handles Live Updates)
# ==========================================
class LiveTickerDisplay(threading.Thread):
    """
    Constantly updates Line 1 of the terminal with the LTP.
    Does NOT interfere with the Input prompt at the bottom.
    """
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not shutdown_event.is_set():
            if current_monitoring_symbol:
                # Construct the Status Line
                status_text = (
                    f"{MOVE_TO_TOP}{GREEN}LIVE TRACKER | "
                    f"Symbol: {current_monitoring_symbol} | "
                    f"LTP: {current_ltp:.2f}{RESET}{CLEAR_LINE}"
                )
                
                # Write to stdout without moving the input cursor
                # logic: Save Cursor -> Move Top -> Print -> Restore Cursor
                sys.stdout.write(f"{SAVE_CURSOR}{status_text}{RESTORE_CURSOR}")
                sys.stdout.flush()
            
            time.sleep(0.5) # Update frequency

# ==========================================
# 2. INPUT VALIDATOR (Strict Inputs)
# ==========================================
def get_valid_input(prompt, validator_func, error_msg="Invalid input."):
    """
    Blocks until the user provides input that passes the validator_func.
    No empty inputs allowed.
    """
    while True:
        with print_lock:
            # We use a standard print for the prompt, relying on the 
            # DisplayThread to handle the top line independently.
            sys.stdout.write(f"\n{prompt}")
            sys.stdout.flush()
        
        user_input = sys.stdin.readline().strip()
        
        if not user_input:
            print("Input cannot be empty.")
            continue
            
        if user_input.lower() == 'exit':
            return 'EXIT'

        if validator_func(user_input):
            return user_input
        else:
            print(error_msg)

# ==========================================
# 3. TRADE MANAGER (Background OCO Logic)
# ==========================================
class TradeManager(threading.Thread):
    def __init__(self, client, symbol_info, txn_type, qty, sl_pts, tgt_pts):
        super().__init__()
        self.client = client
        self.symbol_info = symbol_info
        self.txn_type = txn_type
        self.qty = qty
        self.sl_pts = float(sl_pts)
        self.tgt_pts = float(tgt_pts)
        self.daemon = True

    def log(self, msg):
        """Thread-safe logging"""
        with print_lock:
            # \n ensures we don't overwrite the input line too badly
            sys.stdout.write(f"\n[Trade:{self.symbol_info['pTrdSymbol']}] {msg}\n")

    def run(self):
        symbol = self.symbol_info['pTrdSymbol']
        exch_seg = self.symbol_info['pExchSeg']
        
        self.log(f"Executing {self.txn_type} Order...")

        # 1. Place Market Entry
        try:
            entry_resp = self.client.place_order(
                exchange_segment=exch_seg, product="MIS", price="0", order_type="MKT",
                quantity=self.qty, validity="DAY", trading_symbol=symbol, 
                transaction_type=self.txn_type
            )
            
            # Simulated check for success
            if not entry_resp or 'nOrdNo' not in entry_resp:
                self.log(f"Entry Failed: {entry_resp}")
                return

            self.log(f"Entry Placed. ID: {entry_resp['nOrdNo']}")
            
            # 2. Get Entry Price (Approximation via global LTP for speed)
            # In production, fetch 'average_price' from trade book
            entry_price = current_ltp
            if entry_price == 0:
                time.sleep(1) # wait for tick
                entry_price = current_ltp

            # 3. Calculate Exits
            if self.txn_type == "B":
                sl_price = entry_price - self.sl_pts
                tgt_price = entry_price + self.tgt_pts
                exit_type = "S"
            else:
                sl_price = entry_price + self.sl_pts
                tgt_price = entry_price - self.tgt_pts
                exit_type = "B"

            self.log(f"Placing OCO | SL: {sl_price:.2f} | TGT: {tgt_price:.2f}")

            # 4. Place Legs
            tgt_resp = self.client.place_order(
                exchange_segment=exch_seg, product="MIS", price=str(round(tgt_price, 2)),
                order_type="L", quantity=self.qty, validity="DAY", 
                trading_symbol=symbol, transaction_type=exit_type
            )
            sl_resp = self.client.place_order(
                exchange_segment=exch_seg, product="MIS", price=str(round(sl_price, 2)),
                trigger_price=str(round(sl_price, 2)), order_type="SL", 
                quantity=self.qty, validity="DAY", trading_symbol=symbol, 
                transaction_type=exit_type
            )

            tgt_id = tgt_resp.get('nOrdNo')
            sl_id = sl_resp.get('nOrdNo')

            # 5. Monitor Loop (The OCO logic)
            while not shutdown_event.is_set():
                time.sleep(2)
                
                # Check Target
                tgt_hist = self.client.order_history(order_id=tgt_id)
                if self._is_filled(tgt_hist):
                    self.log(f"Target Hit! Cancelling SL ({sl_id})")
                    self.client.cancel_order(order_id=sl_id)
                    break
                
                # Check SL
                sl_hist = self.client.order_history(order_id=sl_id)
                if self._is_filled(sl_hist):
                    self.log(f"SL Hit! Cancelling Target ({tgt_id})")
                    self.client.cancel_order(order_id=tgt_id)
                    break

        except Exception as e:
            self.log(f"Error: {e}")

    def _is_filled(self, order_data):
        # Helper to parse API response
        try:
            if 'data' in order_data and order_data['data']:
                return order_data['data'][0].get('ordSt', '').lower() == 'traded'
        except:
            pass
        return False

# ==========================================
# 4. MOCK API (For Testing Logic)
# ==========================================
class MockNeoAPI:
    def __init__(self, **kwargs):
        self.orders = {}
    
    def on_message(self, *args): pass 
    def on_error(self, *args): pass
    
    def search_scrip(self, exchange_segment, symbol):
        return [{
            'pSymbol': '12345',
            'pExchSeg': 'nse_cm',
            'pTrdSymbol': f"{symbol.upper()}-EQ"
        }]

    def subscribe(self, *args, **kwargs):
        # Start a fake tick generator
        t = threading.Thread(target=self._tick_gen, daemon=True)
        t.start()

    def _tick_gen(self):
        global current_ltp
        current_ltp = 1000.0
        while True:
            time.sleep(0.5)
            current_ltp += random.choice([-0.5, 0.5, 1.0, -1.0])

    def place_order(self, **kwargs):
        oid = str(random.randint(1000, 9999))
        self.orders[oid] = {'status': 'pending', 't': time.time()}
        return {'nOrdNo': oid, 'stat': 'Ok'}

    def order_history(self, order_id):
        # Simulate fill after 5 seconds
        if order_id in self.orders:
            if time.time() - self.orders[order_id]['t'] > 5:
                self.orders[order_id]['status'] = 'traded'
            return {'data': [{'ordSt': self.orders[order_id]['status']}]}
        return {}

    def cancel_order(self, order_id):
        return {}

# ==========================================
# 5. MAIN WORKFLOW
# ==========================================
def main():
    global current_monitoring_symbol, current_ltp

    print("Initializing System...")
    
    # --- SELECT MODE: MOCK OR REAL ---
    # To use Real API: client = NeoAPI(consumer_key="...", ...)
    client = MockNeoAPI() 
    
    # Start the UI Display Thread
    # This thread keeps line 1 updated with LTP while you type below
    ui_thread = LiveTickerDisplay()
    ui_thread.start()

    # Clear Screen to start fresh
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" * 2) # Make space for the top bar

    while True:
        try:
            # 1. Ask for Symbol (The "New Order" Loop)
            # The top line will update to "Waiting..." or keep previous until valid
            
            symbol_input = get_valid_input(
                prompt="Enter Symbol to Track (or 'exit'): ",
                validator_func=lambda x: True
            )
            
            if symbol_input == 'EXIT':
                shutdown_event.set()
                break

            # 2. Search & Subscribe
            scrip_list = client.search_scrip('nse_cm', symbol_input)
            if not scrip_list:
                print("Symbol not found.")
                continue
            
            scrip = scrip_list[0]
            current_monitoring_symbol = scrip['pTrdSymbol']
            
            # Start Subscription (Real API needs this)
            client.subscribe(instrument_tokens=[
                {"instrument_token": scrip['pSymbol'], "exchange_segment": scrip['pExchSeg']}
            ])

            # 3. Get Trade Parameters (LTP Updates LIVE at the top during this)
            
            bs = get_valid_input(
                prompt="Buy or Sell? (B/S): ", 
                validator_func=lambda x: x.upper() in ['B', 'S'],
                error_msg="Please enter 'B' or 'S' only."
            ).upper()
            if bs == 'EXIT': break

            qty = get_valid_input(
                prompt="Quantity: ", 
                validator_func=lambda x: x.isdigit() and int(x) > 0,
                error_msg="Quantity must be a positive integer."
            )
            if qty == 'EXIT': break

            sl = get_valid_input(
                prompt="Stop Loss (Points): ", 
                validator_func=lambda x: x.replace('.', '', 1).isdigit(),
                error_msg="Enter a valid number."
            )
            if sl == 'EXIT': break

            tgt = get_valid_input(
                prompt="Target (Points): ", 
                validator_func=lambda x: x.replace('.', '', 1).isdigit(),
                error_msg="Enter a valid number."
            )
            if tgt == 'EXIT': break

            # 4. Execute Trade (Non-Blocking)
            manager = TradeManager(client, scrip, bs, qty, sl, tgt)
            manager.start()
            
            print(f"\n---> Trade Launched in Background for {current_monitoring_symbol}")
            print("---> You can now enter the next symbol.")

        except KeyboardInterrupt:
            print("\nExiting...")
            shutdown_event.set()
            break

if __name__ == "__main__":
    main()