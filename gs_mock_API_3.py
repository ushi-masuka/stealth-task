import sys
import time
import threading
import os
import random
from collections import deque

# Try to import NeoAPI, fall back to Mock if not installed
try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None

# --- ANSI ESCAPE CODES (For Terminal UI) ---
ESC = "\033"
SAVE_CURSOR = f"{ESC}[s"     # Save cursor position
RESTORE_CURSOR = f"{ESC}[u"  # Restore cursor position
MOVE_TO_TOP = f"{ESC}[H"     # Move to Row 1, Col 1
CLEAR_LINE = f"{ESC}[K"      # Clear current line
GREEN = f"{ESC}[32m"
YELLOW = f"{ESC}[33m"
CYAN = f"{ESC}[36m"
RESET = f"{ESC}[0m"

# --- GLOBAL SHARED STATE ---
current_monitoring_symbol = "None"
current_ltp = 0.0
shutdown_event = threading.Event()

# LOG BUFFER: Stores the last 5 messages to display at the top
log_buffer = deque(maxlen=5) 
# Initialize with empty strings to keep layout fixed
for _ in range(5):
    log_buffer.append("")

# ==========================================
# 1. DISPLAY ENGINE (Ticker + Log Area)
# ==========================================
class LiveDashboard(threading.Thread):
    """
    Renders the Top Section of the screen:
    Line 1: Live Ticker
    Line 2: Separator
    Line 3-7: Recent Log Messages (The 'Notification Area')
    Line 8: Separator
    """
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not shutdown_event.is_set():
            # 1. Construct the Header Text
            ticker_line = (
                f"{GREEN}LIVE TRACKER | "
                f"Symbol: {current_monitoring_symbol:<10} | "
                f"LTP: {current_ltp:.2f}{RESET}"
            )
            
            # 2. Build the output block
            # We use MOVE_TO_TOP to always write from line 1
            output = f"{SAVE_CURSOR}{MOVE_TO_TOP}"
            
            # Print Ticker
            output += f"{ticker_line}{CLEAR_LINE}\n"
            output += f"--------------------------------------------------{CLEAR_LINE}\n"
            
            # Print Logs from Buffer (Fixed 5 lines)
            for msg in log_buffer:
                # Add color to specific keywords for readability
                formatted_msg = msg
                if "Target Hit" in msg: formatted_msg = f"{GREEN}{msg}{RESET}"
                elif "SL Hit" in msg: formatted_msg = f"{YELLOW}{msg}{RESET}"
                elif "Executing" in msg: formatted_msg = f"{CYAN}{msg}{RESET}"
                
                output += f"{formatted_msg}{CLEAR_LINE}\n"
            
            output += f"--------------------------------------------------{CLEAR_LINE}"
            output += f"{RESTORE_CURSOR}"

            # 3. Write purely to stdout
            sys.stdout.write(output)
            sys.stdout.flush()
            
            time.sleep(0.5)

# ==========================================
# 2. LOGGING HELPER
# ==========================================
def add_log(message):
    """Adds a message to the top buffer instead of printing it."""
    timestamp = time.strftime("%H:%M:%S")
    log_buffer.append(f"[{timestamp}] {message}")

# ==========================================
# 3. INPUT VALIDATOR
# ==========================================
def get_valid_input(prompt, validator_func, error_msg="Invalid input."):
    while True:
        # Clear the input line area before asking (visual cleanup)
        sys.stdout.write(f"\r{CLEAR_LINE}{prompt}")
        sys.stdout.flush()
        
        try:
            user_input = sys.stdin.readline().strip()
        except ValueError:
            continue
        
        if not user_input:
            continue
            
        if user_input.lower() == 'exit':
            return 'EXIT'

        if validator_func(user_input):
            return user_input
        else:
            # Flash error message briefly on the input line
            sys.stdout.write(f"\r{CLEAR_LINE}{YELLOW}Error: {error_msg}{RESET}")
            sys.stdout.flush()
            time.sleep(1.5)

# ==========================================
# 4. TRADE MANAGER
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

    def run(self):
        symbol = self.symbol_info['pTrdSymbol']
        exch_seg = self.symbol_info['pExchSeg']
        
        # LOGGING TO BUFFER NOW
        add_log(f"[{symbol}] Executing {self.txn_type} Order...")

        try:
            # 1. Place Market Entry
            entry_resp = self.client.place_order(
                exchange_segment=exch_seg, product="MIS", price="0", order_type="MKT",
                quantity=self.qty, validity="DAY", trading_symbol=symbol, 
                transaction_type=self.txn_type
            )
            
            if not entry_resp or 'nOrdNo' not in entry_resp:
                add_log(f"[{symbol}] Entry Failed!")
                return

            add_log(f"[{symbol}] Entry ID: {entry_resp['nOrdNo']}")
            
            # 2. Get Entry Price (Simulated via global LTP)
            time.sleep(1) # Wait for fill
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

            add_log(f"[{symbol}] OCO Set | SL: {sl_price:.2f} | TGT: {tgt_price:.2f}")

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

            # 5. Monitor Loop
            while not shutdown_event.is_set():
                time.sleep(2)
                
                tgt_hist = self.client.order_history(order_id=tgt_id)
                if self._is_filled(tgt_hist):
                    add_log(f"[{symbol}] Target Hit! Cancelling SL")
                    self.client.cancel_order(order_id=sl_id)
                    break
                
                sl_hist = self.client.order_history(order_id=sl_id)
                if self._is_filled(sl_hist):
                    add_log(f"[{symbol}] SL Hit! Cancelling Target")
                    self.client.cancel_order(order_id=tgt_id)
                    break

        except Exception as e:
            add_log(f"[{symbol}] Error: {str(e)[:20]}...")

    def _is_filled(self, order_data):
        try:
            if 'data' in order_data and order_data['data']:
                return order_data['data'][0].get('ordSt', '').lower() == 'traded'
        except: pass
        return False

# ==========================================
# 5. MOCK API (For Logic Testing)
# ==========================================
class MockNeoAPI:
    def __init__(self, **kwargs): self.orders = {}
    def on_message(self, *args): pass 
    def on_error(self, *args): pass
    
    def search_scrip(self, exchange_segment, symbol):
        return [{'pSymbol': '12345', 'pExchSeg': 'nse_cm', 'pTrdSymbol': f"{symbol.upper()}-EQ"}]

    def subscribe(self, *args, **kwargs):
        threading.Thread(target=self._tick_gen, daemon=True).start()

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
        if order_id in self.orders:
            if time.time() - self.orders[order_id]['t'] > 5:
                self.orders[order_id]['status'] = 'traded'
            return {'data': [{'ordSt': self.orders[order_id]['status']}]}
        return {}
    
    def cancel_order(self, order_id): return {}

# ==========================================
# 6. MAIN WORKFLOW
# ==========================================
def main():
    global current_monitoring_symbol, current_ltp

    # Start Mock or Real API
    client = MockNeoAPI() 
    
    # 1. Clear Screen and Prepare Layout
    os.system('cls' if os.name == 'nt' else 'clear')
    
    # We print 10 newlines to ensure the input prompt starts 
    # BELOW the reserved area (Lines 1-8 are reserved)
    print("\n" * 10)

    # 2. Start Display Thread
    ui_thread = LiveDashboard()
    ui_thread.start()

    while True:
        try:
            # 3. Input Loop (Happens at the bottom)
            symbol_input = get_valid_input(
                prompt="Enter Symbol to Track (or 'exit'): ",
                validator_func=lambda x: True
            )
            
            if symbol_input == 'EXIT':
                shutdown_event.set()
                break

            # Search Logic
            scrip_list = client.search_scrip('nse_cm', symbol_input)
            if not scrip_list:
                add_log("System: Symbol not found.")
                continue
            
            scrip = scrip_list[0]
            current_monitoring_symbol = scrip['pTrdSymbol']
            add_log(f"System: Tracking {current_monitoring_symbol}")
            
            # Start Subscription
            client.subscribe(instrument_tokens=[{"instrument_token": scrip['pSymbol'], "exchange_segment": scrip['pExchSeg']}])

            # Get Params
            bs = get_valid_input("Buy or Sell? (B/S): ", lambda x: x.upper() in ['B', 'S']).upper()
            if bs == 'EXIT': break

            qty = get_valid_input("Quantity: ", lambda x: x.isdigit() and int(x) > 0)
            if qty == 'EXIT': break

            sl = get_valid_input("Stop Loss (Pts): ", lambda x: x.replace('.', '', 1).isdigit())
            if sl == 'EXIT': break

            tgt = get_valid_input("Target (Pts): ", lambda x: x.replace('.', '', 1).isdigit())
            if tgt == 'EXIT': break

            # Launch Trade
            manager = TradeManager(client, scrip, bs, qty, sl, tgt)
            manager.start()

        except KeyboardInterrupt:
            shutdown_event.set()
            break

if __name__ == "__main__":
    main()