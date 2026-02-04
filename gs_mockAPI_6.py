import sys
import time
import threading
import os
import random
from collections import deque
from datetime import datetime

# --- CONFIGURATION ---
# Set to False to use the Real API
USE_MOCK_API = True 
LOG_FILE_NAME = "trade_log.txt"

# Try to import NeoAPI, otherwise force Mock mode
try:
    from neo_api_client import NeoAPI
except ImportError:
    USE_MOCK_API = True
    NeoAPI = None

# --- ANSI COLORS & TERMINAL CODES ---
ESC = "\033"
SAVE_CURSOR = f"{ESC}[s"
RESTORE_CURSOR = f"{ESC}[u"
MOVE_TO_TOP = f"{ESC}[H"
CLEAR_LINE = f"{ESC}[K"
GREEN = f"{ESC}[32m"
RED = f"{ESC}[31m"
YELLOW = f"{ESC}[33m"
CYAN = f"{ESC}[36m"
WHITE = f"{ESC}[37m"
BOLD = f"{ESC}[1m"
RESET = f"{ESC}[0m"

# ==========================================
# 1. STATE MANAGER (Central Data Store)
# ==========================================
class StateManager:
    """
    Stores the state of the entire application:
    - Current "Focus" Symbol (what user is searching)
    - Live Prices (LTP) for ALL subscribed symbols
    - Active Trades (Portfolio)
    - Logs
    """
    def __init__(self):
        # Focus Symbol (The one user is currently typing to enter)
        self.focus_symbol = "None"
        
        # LTP Cache: { 'TCS-EQ': 3200.50, 'RELIANCE-EQ': 2500.00 }
        self.ltp_cache = {}
        self.ltp_lock = threading.Lock()
        
        # Active Trades: List of dicts
        self.active_trades = []
        self.trades_lock = threading.Lock()
        
        # Logs
        self.log_buffer = deque(maxlen=6)
        for _ in range(6): self.log_buffer.append("")
        
        self.shutdown_flag = threading.Event()
        self.log_to_file("--- SESSION STARTED ---")

    def update_ltp(self, symbol, price):
        """Updates the price for a specific symbol"""
        with self.ltp_lock:
            self.ltp_cache[symbol] = float(price)

    def get_ltp(self, symbol):
        with self.ltp_lock:
            return self.ltp_cache.get(symbol, 0.0)

    def set_focus(self, symbol):
        self.focus_symbol = symbol

    def add_active_trade(self, trade_info):
        with self.trades_lock:
            self.active_trades.append(trade_info)

    def remove_active_trade(self, symbol):
        with self.trades_lock:
            self.active_trades = [t for t in self.active_trades if t['symbol'] != symbol]

    def log_to_file(self, raw_message):
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE_NAME, "a") as f:
                f.write(f"[{timestamp}] {raw_message}\n")
        except: pass

    def add_log(self, message):
        self.log_to_file(message)
        timestamp = time.strftime("%H:%M:%S")
        
        if "PROFIT" in message: fmt = f"{GREEN}{BOLD}{message}{RESET}"
        elif "LOSS" in message: fmt = f"{RED}{BOLD}{message}{RESET}"
        elif "Executing" in message: fmt = f"{CYAN}{message}{RESET}"
        elif "Error" in message: fmt = f"{RED}{message}{RESET}"
        else: fmt = message
            
        self.log_buffer.append(f"[{timestamp}] {fmt}")

state = StateManager()

# ==========================================
# 2. UI DASHBOARD (Renderer)
# ==========================================
class Dashboard(threading.Thread):
    """
    Refreshes the top part of the screen with:
    1. Focus Ticker (The symbol you are preparing to trade)
    2. Active Portfolio (All running trades with Live P/L)
    3. Logs
    """
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not state.shutdown_flag.is_set():
            output = f"{SAVE_CURSOR}{MOVE_TO_TOP}"
            
            # --- SECTION 1: FOCUS TICKER ---
            # Shows the LTP of the symbol user is currently "looking at"
            focus_sym = state.focus_symbol
            focus_ltp = state.get_ltp(focus_sym)
            
            output += f"{BOLD}-------------------- LIVE MONITOR --------------------{RESET}{CLEAR_LINE}\n"
            output += f" FOCUS: {CYAN}{focus_sym:<10}{RESET} | LTP: {GREEN}{focus_ltp:.2f}{RESET}{CLEAR_LINE}\n"
            output += f"------------------------------------------------------{CLEAR_LINE}\n"
            
            # --- SECTION 2: ACTIVE PORTFOLIO (ALL TRADES) ---
            output += f"{BOLD}[ ACTIVE PORTFOLIO ]{RESET}{CLEAR_LINE}\n"
            
            with state.trades_lock:
                if not state.active_trades:
                    output += f"{YELLOW} No active trades.{RESET}{CLEAR_LINE}\n"
                else:
                    for t in state.active_trades:
                        # 1. Get Live Data
                        symbol = t['symbol']
                        entry = t['entry']
                        qty = int(t['qty'])
                        curr_price = state.get_ltp(symbol)
                        
                        # 2. Calculate P/L
                        if t['type'] == 'B':
                            pnl = (curr_price - entry) * qty
                        else:
                            pnl = (entry - curr_price) * qty
                        
                        # 3. Format Colors
                        pnl_str = f"{pnl:+.2f}"
                        pnl_color = GREEN if pnl >= 0 else RED
                        
                        # 4. Construct Line
                        # Format: TCS (B) | Entry: 3200 | LTP: 3205 | P/L: +50.00
                        line = (f" {symbol:<9} ({t['type']}) | "
                                f"Ent: {entry:<7.2f} | "
                                f"LTP: {WHITE}{curr_price:<7.2f}{RESET} | "
                                f"P/L: {pnl_color}{pnl_str}{RESET}")
                        
                        output += f"{line}{CLEAR_LINE}\n"
            
            # Stable UI Padding (Ensure at least 3 lines of space)
            required_lines = 4
            current_lines = len(state.active_trades) if state.active_trades else 1
            for _ in range(required_lines - current_lines):
                output += f"{CLEAR_LINE}\n"

            output += f"------------------------------------------------------{CLEAR_LINE}\n"

            # --- SECTION 3: LOGS ---
            for log in state.log_buffer:
                output += f"{log}{CLEAR_LINE}\n"
            
            output += f"------------------------------------------------------{CLEAR_LINE}\n"
            output += f"{RESTORE_CURSOR}"

            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(0.5)

# ==========================================
# 3. TRADING LOGIC (OCO Strategy)
# ==========================================
class OCOStrategy(threading.Thread):
    def __init__(self, client, symbol_info, txn_type, qty, sl_pts, tgt_pts):
        super().__init__()
        self.client = client
        self.scrip = symbol_info
        self.symbol = symbol_info['pTrdSymbol']
        self.txn_type = txn_type
        self.qty = qty
        self.sl_pts = float(sl_pts)
        self.tgt_pts = float(tgt_pts)
        self.daemon = True

    def run(self):
        state.add_log(f"[{self.symbol}] Executing {self.txn_type} Order...")

        try:
            # 1. MARKET ENTRY
            entry_resp = self.client.place_order(
                exchange_segment=self.scrip['pExchSeg'], product="MIS", 
                price="0", order_type="MKT", quantity=self.qty, 
                validity="DAY", trading_symbol=self.symbol, 
                transaction_type=self.txn_type
            )

            if not entry_resp or 'nOrdNo' not in entry_resp:
                state.add_log(f"[{self.symbol}] Entry Failed!")
                return

            # 2. RESOLVE ENTRY PRICE
            time.sleep(1) # Wait for fill
            # Use current LTP as entry price proxy
            entry_price = state.get_ltp(self.symbol)
            if entry_price == 0: entry_price = 1000.0 # Fallback safety
            
            # 3. REGISTER TRADE (So it shows in Dashboard)
            trade_record = {
                'symbol': self.symbol,
                'type': self.txn_type,
                'entry': entry_price,
                'qty': self.qty,
                'sl': 0, # Placeholders
                'tgt': 0
            }
            state.add_active_trade(trade_record)
            state.add_log(f"[{self.symbol}] Entry @ {entry_price:.2f}")

            # 4. CALC LEGS
            if self.txn_type == 'B':
                sl_price = entry_price - self.sl_pts
                tgt_price = entry_price + self.tgt_pts
                exit_type = 'S'
            else:
                sl_price = entry_price + self.sl_pts
                tgt_price = entry_price - self.tgt_pts
                exit_type = 'B'
            
            # Update record with targets for display? (Optional, kept simple)

            # 5. PLACE EXITS
            tgt_resp = self.client.place_order(
                exchange_segment=self.scrip['pExchSeg'], product="MIS", 
                price=str(round(tgt_price, 2)), order_type="L", 
                quantity=self.qty, validity="DAY", 
                trading_symbol=self.symbol, transaction_type=exit_type
            )
            
            sl_resp = self.client.place_order(
                exchange_segment=self.scrip['pExchSeg'], product="MIS", 
                price=str(round(sl_price, 2)), trigger_price=str(round(sl_price, 2)),
                order_type="SL", quantity=self.qty, validity="DAY", 
                trading_symbol=self.symbol, transaction_type=exit_type
            )

            tgt_id = tgt_resp.get('nOrdNo')
            sl_id = sl_resp.get('nOrdNo')
            
            state.add_log(f"[{self.symbol}] OCO Set | SL: {sl_price:.2f} | TGT: {tgt_price:.2f}")

            # 6. MONITOR LOOP
            while not state.shutdown_flag.is_set():
                time.sleep(2)
                
                # Check Target
                if self._check_fill(tgt_id):
                    state.add_log(f"[{self.symbol}] Target Hit! (PROFIT)")
                    self.client.cancel_order(order_id=sl_id)
                    break
                
                # Check SL
                if self._check_fill(sl_id):
                    state.add_log(f"[{self.symbol}] SL Hit! (LOSS)")
                    self.client.cancel_order(order_id=tgt_id)
                    break

        except Exception as e:
            state.add_log(f"[{self.symbol}] Error: {e}")
        finally:
            state.remove_active_trade(self.symbol)

    def _check_fill(self, order_id):
        try:
            hist = self.client.order_history(order_id=order_id)
            if 'data' in hist and hist['data']:
                return hist['data'][0].get('ordSt', '').lower() == 'traded'
        except: pass
        return False

# ==========================================
# 4. MOCK API (Multi-Token Support)
# ==========================================
class MockNeoAPI:
    def __init__(self): 
        self.orders = {}
        self.subscribed_tokens = set()
        state.add_log("System: Mock API (Multi-Track) Initialized")

    def on_message(self, *args): pass 
    def on_error(self, *args): pass
    
    def search_scrip(self, exchange_segment, symbol):
        # Return a consistent dummy token based on symbol name length
        # This ensures 'TCS' always gets same token, 'REL' gets another
        token_id = str(sum(ord(c) for c in symbol)) 
        return [{
            'pSymbol': token_id, 
            'pExchSeg': 'nse_cm', 
            'pTrdSymbol': f"{symbol.upper()}-EQ"
        }]

    def subscribe(self, instrument_tokens, **kwargs):
        # Add to set of tracked tokens
        for t in instrument_tokens:
            self.subscribed_tokens.add((t['instrument_token'], t['pTrdSymbol'] if 'pTrdSymbol' in t else 'UNKNOWN'))
        
        # Ensure only one generator runs
        if not hasattr(self, 'feed_thread'):
            self.feed_thread = threading.Thread(target=self._mock_feed, daemon=True)
            self.feed_thread.start()

    def _mock_feed(self):
        # Independent prices for each token
        prices = {}
        
        while True:
            time.sleep(0.5)
            # Update EVERY subscribed token
            # Convert set to list to avoid runtime modification errors
            current_subs = list(self.subscribed_tokens)
            
            for token_id, symbol_name in current_subs: # Using symbol name as key for State
                if symbol_name not in prices:
                    prices[symbol_name] = 1000.0 + random.randint(-50, 50)
                
                # Random walk
                prices[symbol_name] += random.choice([-1.5, -0.5, 0.5, 1.5])
                
                # Update State
                state.update_ltp(symbol_name, prices[symbol_name])

    def place_order(self, **kwargs):
        oid = str(random.randint(10000, 99999))
        self.orders[oid] = {'status': 'pending', 't': time.time()}
        return {'nOrdNo': oid, 'stat': 'Ok'}

    def order_history(self, order_id):
        if order_id in self.orders:
            # Complete after 8 seconds
            if time.time() - self.orders[order_id]['t'] > 8:
                self.orders[order_id]['status'] = 'traded'
            return {'data': [{'ordSt': self.orders[order_id]['status']}]}
        return {}
    
    def cancel_order(self, order_id): return {}

# ==========================================
# 5. INPUT HELPER
# ==========================================
def get_input(prompt, validator):
    while True:
        sys.stdout.write(f"\r{CLEAR_LINE}{prompt}")
        sys.stdout.flush()
        
        try:
            txt = sys.stdin.readline().strip()
        except ValueError: continue
        
        if not txt: continue
        if txt.lower() == 'exit': return 'EXIT'
        
        if validator(txt): return txt
        
        sys.stdout.write(f"\r{CLEAR_LINE}{RED}Invalid Input!{RESET}")
        sys.stdout.flush()
        time.sleep(1)

# ==========================================
# 6. MAIN EXECUTION
# ==========================================
def main():
    if USE_MOCK_API:
        client = MockNeoAPI()
    else:
        # REAL API SETUP
        state.add_log("System: Connecting to Real API...")
        client = NeoAPI(consumer_key="KEY", consumer_secret="SECRET", environment='prod')
        # client.totp_login(...) 
        
        # Real Adapter: Parses messages and updates specific symbols
        def on_msg_adapter(message):
            data_list = message if isinstance(message, list) else [message]
            for data in data_list:
                # Need mapping from token -> Symbol Name
                # In real scenario, search_scrip gives us this. 
                # Ideally we maintain a map: token_map = {'123': 'TCS-EQ'}
                # For now, assuming API returns 'trdSym' or we use raw token if needed.
                # Kotak API often sends 'tk' (token). 
                pass # Implementation depends on exact feed JSON structure
                # Simplified:
                if 'tk' in data and 'ltp' in data:
                    # In this simplified CLI, we might need a global map
                    # stored in state to map 'tk' back to 'TCS-EQ'
                    pass

        # NOTE: For Real API, you must map the incoming 'tk' (token) back to the Symbol Name
        # to update state.update_ltp(SYMBOL, price) correctly.
        
    # Prepare Screen
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" * 18) # Reserve more space for the bigger dashboard
    
    Dashboard().start()

    # --- MAIN INPUT LOOP ---
    while True:
        try:
            # 1. Symbol Entry
            sym = get_input("Enter Symbol (or 'exit'): ", lambda x: True)
            if sym == 'EXIT': break

            # 2. Search
            scrip_list = client.search_scrip('nse_cm', sym)
            if not scrip_list:
                state.add_log(f"Symbol '{sym}' not found.", "ERROR")
                continue
            
            scrip = scrip_list[0]
            trade_symbol = scrip['pTrdSymbol']
            
            # Set Focus (Update Ticker)
            state.set_focus(trade_symbol)
            state.update_ltp(trade_symbol, 0.0) # Reset prev price
            
            # Subscribe (Cumulative)
            # We pass the symbol name in the mock so it knows what to update
            client.subscribe(instrument_tokens=[{
                "instrument_token": scrip['pSymbol'], 
                "exchange_segment": scrip['pExchSeg'],
                "pTrdSymbol": trade_symbol # Passed for Mock tracking
            }])

            # 3. Trade Params
            bs = get_input("Buy/Sell (B/S): ", lambda x: x.upper() in ['B', 'S']).upper()
            if bs == 'EXIT': break
            
            qty = get_input("Quantity: ", lambda x: x.isdigit())
            if qty == 'EXIT': break
            
            sl = get_input("SL Points: ", lambda x: x.replace('.', '', 1).isdigit())
            if sl == 'EXIT': break
            
            tgt = get_input("Target Points: ", lambda x: x.replace('.', '', 1).isdigit())
            if tgt == 'EXIT': break

            # 4. Execute
            OCOStrategy(client, scrip, bs, qty, sl, tgt).start()

        except KeyboardInterrupt:
            break
            
    state.shutdown_flag.set()
    state.log_to_file("--- SESSION ENDED ---")
    print(f"\n{RESET}System Shutdown.")

if __name__ == "__main__":
    main()