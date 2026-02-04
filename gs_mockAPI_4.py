import sys
import time
import threading
import os
import random
from collections import deque

# --- CONFIGURATION ---
# To switch to REAL TRADING, set this to False and ensure neo_api_client is installed
USE_MOCK_API = True 

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
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
MOVE_TO_TOP = f"{ESC}[H"
CLEAR_LINE = f"{ESC}[K"
CLEAR_SCREEN = f"{ESC}[2J"

GREEN = f"{ESC}[32m"
RED = f"{ESC}[31m"
YELLOW = f"{ESC}[33m"
CYAN = f"{ESC}[36m"
BOLD = f"{ESC}[1m"
RESET = f"{ESC}[0m"

# ==========================================
# 1. STATE MANAGER (The "Brain")
# ==========================================
class StateManager:
    """
    Central repository for all shared data.
    Decouples the UI from the Trading Logic.
    """
    def __init__(self):
        # Data for the "Live Ticker" (Top Line)
        self.monitor_symbol = "None"
        self.monitor_ltp = 0.0
        
        # Data for "Active Trades" (Middle Section)
        # List of dictionaries: {'symbol': 'TCS', 'type': 'B', 'price': 1000, 'status': 'Running'}
        self.active_trades = []
        self.trades_lock = threading.Lock()
        
        # Data for "Logs" (Bottom Section)
        self.log_buffer = deque(maxlen=6)
        # Init logs with empty lines
        for _ in range(6): self.log_buffer.append("")
        
        self.shutdown_flag = threading.Event()

    def update_ticker(self, symbol, ltp):
        self.monitor_symbol = symbol
        self.monitor_ltp = float(ltp)

    def add_active_trade(self, trade_info):
        with self.trades_lock:
            self.active_trades.append(trade_info)

    def remove_active_trade(self, symbol):
        with self.trades_lock:
            self.active_trades = [t for t in self.active_trades if t['symbol'] != symbol]

    def add_log(self, message, level="INFO"):
        timestamp = time.strftime("%H:%M:%S")
        
        # Color coding based on content
        if "PROFIT" in message: fmt_msg = f"{GREEN}{BOLD}{message}{RESET}"
        elif "LOSS" in message: fmt_msg = f"{RED}{BOLD}{message}{RESET}"
        elif "Executing" in message: fmt_msg = f"{CYAN}{message}{RESET}"
        elif "Error" in message: fmt_msg = f"{RED}{message}{RESET}"
        else: fmt_msg = message
            
        self.log_buffer.append(f"[{timestamp}] {fmt_msg}")

# Global Instance
state = StateManager()

# ==========================================
# 2. UI DASHBOARD (The "Face")
# ==========================================
class Dashboard(threading.Thread):
    """
    Renders the UI in 3 Sections:
    1. Live Ticker (User's current focus)
    2. Active Portfolio (Running OCO orders)
    3. Event Logs (History)
    """
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not state.shutdown_flag.is_set():
            # Build the output string
            output = f"{SAVE_CURSOR}{MOVE_TO_TOP}"
            
            # --- SECTION 1: LIVE TICKER ---
            output += f"{BOLD}-------------------- LIVE TRACKER --------------------{RESET}{CLEAR_LINE}\n"
            output += f" WATCHING: {CYAN}{state.monitor_symbol:<10}{RESET} | LTP: {GREEN}{state.monitor_ltp:.2f}{RESET}{CLEAR_LINE}\n"
            output += f"------------------------------------------------------{CLEAR_LINE}\n"
            
            # --- SECTION 2: ACTIVE TRADES ---
            output += f"{BOLD}[ ACTIVE ORDERS ]{RESET}{CLEAR_LINE}\n"
            with state.trades_lock:
                if not state.active_trades:
                    output += f"{YELLOW} No active trades.{RESET}{CLEAR_LINE}\n"
                else:
                    for t in state.active_trades:
                        # E.g. RELIANCE (B) @ 2500 | TGT: 2520 | SL: 2480
                        line = (f" {t['symbol']} ({t['type']}) @ {t['entry']:.1f} | "
                                f"TGT: {t['tgt']:.1f} | SL: {t['sl']:.1f}")
                        output += f"{line}{CLEAR_LINE}\n"
            
            # Pad empty lines to keep UI stable if few trades
            required_lines = 3
            current_lines = len(state.active_trades) if state.active_trades else 1
            for _ in range(required_lines - current_lines):
                output += f"{CLEAR_LINE}\n"

            output += f"------------------------------------------------------{CLEAR_LINE}\n"

            # --- SECTION 3: LOGS ---
            for log in state.log_buffer:
                output += f"{log}{CLEAR_LINE}\n"
            
            output += f"------------------------------------------------------{CLEAR_LINE}\n"
            output += f"{RESTORE_CURSOR}"

            # Render
            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(0.5)

# ==========================================
# 3. TRADING LOGIC (The "Engine")
# ==========================================
class OCOStrategy(threading.Thread):
    """
    Manages the lifecycle of a single trade:
    Entry -> Bracket Orders -> OCO Monitoring -> Exit
    """
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
            # Note: For Real API, ensure you use the correct product code (MIS/NRML)
            entry_resp = self.client.place_order(
                exchange_segment=self.scrip['pExchSeg'], 
                product="MIS", 
                price="0", 
                order_type="MKT",
                quantity=self.qty, 
                validity="DAY", 
                trading_symbol=self.symbol, 
                transaction_type=self.txn_type
            )

            if not entry_resp or 'nOrdNo' not in entry_resp:
                state.add_log(f"[{self.symbol}] Entry Error: {entry_resp}")
                return

            # 2. RESOLVE ENTRY PRICE
            # In a real app, fetch trade book. Here we use the Ticker LTP as approximation.
            time.sleep(1) 
            entry_price = state.monitor_ltp if state.monitor_ltp > 0 else 1000.0
            
            # 3. CALCULATE LEGS
            if self.txn_type == 'B':
                sl_price = entry_price - self.sl_pts
                tgt_price = entry_price + self.tgt_pts
                exit_type = 'S'
            else:
                sl_price = entry_price + self.sl_pts
                tgt_price = entry_price - self.tgt_pts
                exit_type = 'B'

            # Register in Dashboard
            trade_record = {
                'symbol': self.symbol, 
                'type': self.txn_type, 
                'entry': entry_price,
                'sl': sl_price,
                'tgt': tgt_price
            }
            state.add_active_trade(trade_record)
            state.add_log(f"[{self.symbol}] Entry @ {entry_price:.2f}. OCO Active.")

            # 4. PLACE EXITS
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

            # 5. MONITOR LOOP
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
            state.add_log(f"[{self.symbol}] Exception: {e}")
        finally:
            # Clean up dashboard
            state.remove_active_trade(self.symbol)

    def _check_fill(self, order_id):
        """Returns True if order is Traded"""
        try:
            hist = self.client.order_history(order_id=order_id)
            if 'data' in hist and hist['data']:
                return hist['data'][0].get('ordSt', '').lower() == 'traded'
        except: pass
        return False

# ==========================================
# 4. MOCK API (Testing Adapter)
# ==========================================
class MockNeoAPI:
    def __init__(self): 
        self.orders = {}
        state.add_log("System: Mock API Initialized")

    def on_message(self, *args): pass 
    def on_error(self, *args): pass
    
    def search_scrip(self, exchange_segment, symbol):
        # Return dummy data for any symbol
        return [{
            'pSymbol': '12345', 
            'pExchSeg': 'nse_cm', 
            'pTrdSymbol': f"{symbol.upper()}-EQ"
        }]

    def subscribe(self, instrument_tokens, **kwargs):
        # Start fake price generator
        threading.Thread(target=self._mock_feed, daemon=True).start()

    def _mock_feed(self):
        # Generate random price around 1000
        price = 1000.0
        while True:
            time.sleep(0.5)
            price += random.choice([-0.5, 0.5, 1.0, -1.0])
            state.update_ticker(state.monitor_symbol, price)

    def place_order(self, **kwargs):
        oid = str(random.randint(10000, 99999))
        self.orders[oid] = {'status': 'pending', 't': time.time()}
        return {'nOrdNo': oid, 'stat': 'Ok'}

    def order_history(self, order_id):
        # Auto-fill after 6 seconds
        if order_id in self.orders:
            if time.time() - self.orders[order_id]['t'] > 6:
                self.orders[order_id]['status'] = 'traded'
            return {'data': [{'ordSt': self.orders[order_id]['status']}]}
        return {}
    
    def cancel_order(self, order_id): return {}

# ==========================================
# 5. INPUT HELPER
# ==========================================
def get_input(prompt, validator):
    """
    Handles input safely at the bottom of the screen.
    """
    while True:
        sys.stdout.write(f"\r{CLEAR_LINE}{prompt}")
        sys.stdout.flush()
        
        try:
            txt = sys.stdin.readline().strip()
        except ValueError: continue
        
        if not txt: continue
        if txt.lower() == 'exit': return 'EXIT'
        
        if validator(txt): return txt
        
        # Show Error briefly
        sys.stdout.write(f"\r{CLEAR_LINE}{RED}Invalid Input!{RESET}")
        sys.stdout.flush()
        time.sleep(1)

# ==========================================
# 6. MAIN EXECUTION
# ==========================================
def main():
    # A. INITIALIZATION
    if USE_MOCK_API:
        client = MockNeoAPI()
    else:
        # REAL API SETUP
        state.add_log("System: Connecting to Real API...")
        client = NeoAPI(
            consumer_key="YOUR_KEY", 
            consumer_secret="YOUR_SECRET", 
            environment='prod'
        )
        # client.totp_login(...) # Add your auth flow here
        
        # Real WebSocket Callback Adapter
        def on_msg_adapter(msg):
            # Parse real msg and update state
            data = msg if isinstance(msg, list) else [msg]
            for d in data:
                if 'ltp' in d:
                    state.update_ticker(state.monitor_symbol, d['ltp'])
        
        client.on_message = on_msg_adapter

    # Prepare Screen
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" * 15) # Reserve space for Dashboard
    
    # Start Dashboard
    Dashboard().start()

    # B. MAIN LOOP
    while True:
        try:
            # 1. Symbol Entry
            sym = get_input("Enter Symbol (or 'exit'): ", lambda x: True)
            if sym == 'EXIT': break

            # 2. Search & Subscribe
            scrip_list = client.search_scrip('nse_cm', sym)
            if not scrip_list:
                state.add_log(f"Symbol '{sym}' not found.", "ERROR")
                continue
            
            scrip = scrip_list[0]
            state.update_ticker(scrip['pTrdSymbol'], 0.0) # Reset ticker
            
            client.subscribe(instrument_tokens=[{
                "instrument_token": scrip['pSymbol'], 
                "exchange_segment": scrip['pExchSeg']
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
    print(f"\n{RESET}System Shutdown.")

if __name__ == "__main__":
    main()