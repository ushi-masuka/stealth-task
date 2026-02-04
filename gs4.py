import sys
import time
import threading
import os
import random
import logging
from collections import deque
from datetime import datetime

# --- CONFIGURATION ---
LOG_FILENAME = "uat_trade_log.txt"

# Try to import NeoAPI
try:
    from neo_api_client import NeoAPI
except ImportError:
    print("CRITICAL: neo_api_client not installed. Please install it.")
    sys.exit(1)

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

# Logging Setup
logging.basicConfig(filename=LOG_FILENAME, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ==========================================
# 1. STATE MANAGER (Thread-Safe Data)
# ==========================================
class StateManager:
    def __init__(self):
        # Live Market Data (Dictionary for Multi-Stock Support)
        self.ltp_cache = {} 
        self.ltp_lock = threading.Lock()
        
        self.focus_symbol = "None"
        
        self.active_trades = []
        self.trades_lock = threading.Lock()
        
        self.log_buffer = deque(maxlen=6)
        for _ in range(6): self.log_buffer.append("")
        
        self.shutdown_flag = threading.Event()
        self.log_to_file("--- UAT SESSION STARTED ---")

    def update_ltp(self, symbol, price):
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
            logging.info(raw_message)
        except: pass

    def add_log(self, message):
        self.log_to_file(message)
        timestamp = time.strftime("%H:%M:%S")
        
        if "PROFIT" in message: fmt_msg = f"{GREEN}{BOLD}{message}{RESET}"
        elif "LOSS" in message: fmt_msg = f"{RED}{BOLD}{message}{RESET}"
        elif "Executing" in message: fmt_msg = f"{CYAN}{message}{RESET}"
        elif "Error" in message: fmt_msg = f"{RED}{message}{RESET}"
        else: fmt_msg = message
            
        self.log_buffer.append(f"[{timestamp}] {fmt_msg}")

state = StateManager()

# ==========================================
# 2. UAT ADAPTER (Strict File Compliance)
# ==========================================
class UATKotakAdapter:
    def __init__(self, consumer_key):
        # CORRECTED: Removed consumer_secret based on your file check
        self.client = NeoAPI(
            consumer_key=consumer_key, 
            environment='uat'
        )        

    def login(self, mobile, ucc, totp, mpin):
        print(f"{YELLOW}Authenticating with UAT Environment...{RESET}")
        try:
            # 1. TOTP Login (Generates View Token)
            print(f"--> Validating TOTP for {mobile}...")
            # Note: password arg in totp_login might be needed depending on strictness, 
            # but docstring says mobile, ucc, totp
            resp = self.client.totp_login(mobile_number=mobile, ucc=ucc, totp=totp)
            
            # Check for error in response dictionary
            if not resp or (isinstance(resp, dict) and 'error' in resp):
                raise Exception(f"TOTP Login Failed: {resp}")

            # 2. MPIN Validate (Generates Edit Token)
            print(f"--> Validating MPIN...")
            valid_resp = self.client.totp_validate(mpin=mpin)
            
            if not valid_resp or (isinstance(valid_resp, dict) and 'error' in valid_resp):
                raise Exception(f"MPIN Validation Failed: {valid_resp}")
                
            print(f"{GREEN}UAT Session Active. 2FA Completed.{RESET}")
            
            # 3. Bind WebSocket
            self.client.on_message = self.on_message_wrapper
            self.client.on_error = lambda e: None # Silence connection noise
            self.client.on_close = lambda m: state.add_log("WebSocket Closed")
            
        except Exception as e:
            print(f"{RED}Critical Login Error: {e}{RESET}")
            sys.exit(1)

    def on_message_wrapper(self, message):
        """Parse UAT WebSocket Feed"""
        try:
            payload = message
            if isinstance(message, dict) and 'data' in message:
                payload = message['data']
            if isinstance(payload, list):
                for item in payload:
                    if 'tk' in item and 'ltp' in item:
                        state.update_ltp(item['tk'], item['ltp'])
        except: pass

    def search(self, symbol):
        return self.client.search_scrip('nse_cm', symbol)

    def subscribe(self, token):
        self.client.subscribe(instrument_tokens=[{"instrument_token": token, "exchange_segment": "nse_cm"}])

    def place_order(self, **kwargs):
        return self.client.place_order(**kwargs)

    def get_order_history(self, order_id):
        return self.client.order_history(order_id=order_id)

    def cancel_order(self, order_id):
        return self.client.cancel_order(order_id=order_id)

# ==========================================
# 3. TRADING ENGINE (The Business Logic)
# ==========================================
class OCOStrategy(threading.Thread):
    def __init__(self, adapter, symbol_info, token, txn_type, qty, sl_pts, tgt_pts):
        super().__init__()
        self.adapter = adapter
        self.scrip = symbol_info
        self.symbol = symbol_info['pTrdSymbol']
        self.token = token
        self.txn_type = txn_type
        self.qty = qty
        self.sl_pts = float(sl_pts)
        self.tgt_pts = float(tgt_pts)
        self.daemon = True

    def run(self):
        state.add_log(f"[{self.symbol}] Initiating {self.txn_type} Order...")

        try:
            # 1. MARKET ENTRY
            entry_resp = self.adapter.place_order(
                exchange_segment=self.scrip['pExchSeg'], product="MIS", 
                price="0", order_type="MKT", quantity=self.qty, 
                validity="DAY", trading_symbol=self.symbol, 
                transaction_type=self.txn_type
            )

            if not entry_resp or 'nOrdNo' not in entry_resp:
                state.add_log(f"[{self.symbol}] Entry Failed: {entry_resp}")
                return

            # 2. RESOLVE ENTRY PRICE
            time.sleep(1) # Wait for backend
            entry_price = state.get_ltp(self.token)
            if entry_price == 0: entry_price = 1000.0 # Fallback
            
            # Register in Dashboard
            state.add_active_trade({
                'symbol': self.symbol, 'token': self.token,
                'type': self.txn_type, 'entry': entry_price, 
                'qty': self.qty, 'sl': 0, 'tgt': 0
            })
            state.add_log(f"[{self.symbol}] Entry @ {entry_price:.2f}")

            # 3. CALC LEGS
            if self.txn_type == 'B':
                sl_price = entry_price - self.sl_pts
                tgt_price = entry_price + self.tgt_pts
                exit_type = 'S'
            else:
                sl_price = entry_price + self.sl_pts
                tgt_price = entry_price - self.tgt_pts
                exit_type = 'B'

            # 4. PLACE EXITS
            tgt_resp = self.adapter.place_order(
                exchange_segment=self.scrip['pExchSeg'], product="MIS", 
                price=str(round(tgt_price, 2)), order_type="L", 
                quantity=self.qty, validity="DAY", 
                trading_symbol=self.symbol, transaction_type=exit_type
            )
            
            sl_resp = self.adapter.place_order(
                exchange_segment=self.scrip['pExchSeg'], product="MIS", 
                price=str(round(sl_price, 2)), trigger_price=str(round(sl_price, 2)),
                order_type="SL", quantity=self.qty, validity="DAY", 
                trading_symbol=self.symbol, transaction_type=exit_type
            )

            tgt_id = tgt_resp.get('nOrdNo')
            sl_id = sl_resp.get('nOrdNo')
            
            state.add_log(f"[{self.symbol}] OCO Set | SL: {sl_price:.1f} | TGT: {tgt_price:.1f}")

            # 5. MONITOR LOOP
            while not state.shutdown_flag.is_set():
                time.sleep(2)
                
                if self._check_fill(tgt_id):
                    state.add_log(f"[{self.symbol}] TGT HIT! (PROFIT)")
                    self.adapter.cancel_order(order_id=sl_id)
                    break
                
                if self._check_fill(sl_id):
                    state.add_log(f"[{self.symbol}] SL HIT! (LOSS)")
                    self.adapter.cancel_order(order_id=tgt_id)
                    break

        except Exception as e:
            state.add_log(f"[{self.symbol}] Error: {e}")
        finally:
            state.remove_active_trade(self.symbol)

    def _check_fill(self, order_id):
        try:
            hist = self.adapter.get_order_history(order_id)
            if 'data' in hist and hist['data']:
                return hist['data'][0].get('ordSt', '').lower() == 'traded'
        except: pass
        return False

# ==========================================
# 4. UI DASHBOARD (Notification Center)
# ==========================================
class Dashboard(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not state.shutdown_flag.is_set():
            output = f"{SAVE_CURSOR}{MOVE_TO_TOP}"
            
            # --- SECTION 1: TICKER ---
            focus_sym = state.focus_symbol
            focus_price = state.get_ltp(focus_sym)
            
            output += f"{BOLD}-------------------- UAT LIVE TRACKER --------------------{RESET}{CLEAR_LINE}\n"
            output += f" WATCHING TOKEN: {CYAN}{focus_sym:<10}{RESET} | LTP: {GREEN}{focus_price:.2f}{RESET}{CLEAR_LINE}\n"
            output += f"----------------------------------------------------------{CLEAR_LINE}\n"
            
            # --- SECTION 2: PORTFOLIO ---
            output += f"{BOLD}[ ACTIVE ORDERS ]{RESET}{CLEAR_LINE}\n"
            with state.trades_lock:
                if not state.active_trades:
                    output += f"{YELLOW} No active trades.{RESET}{CLEAR_LINE}\n"
                else:
                    for t in state.active_trades:
                        curr_price = state.get_ltp(t['token'])
                        entry = t['entry']
                        qty = int(t['qty'])
                        
                        # Calc P/L
                        if t['type'] == 'B': pnl = (curr_price - entry) * qty
                        else: pnl = (entry - curr_price) * qty
                        
                        pnl_str = f"{pnl:+.2f}"
                        pnl_color = GREEN if pnl >= 0 else RED
                        
                        line = (f" {t['symbol']:<9} ({t['type']}) | Ent: {entry:<6.1f} | "
                                f"LTP: {WHITE}{curr_price:<6.1f}{RESET} | {pnl_color}{pnl_str}{RESET}")
                        output += f"{line}{CLEAR_LINE}\n"
            
            # Spacer
            required_lines = 4
            current_lines = len(state.active_trades) if state.active_trades else 1
            for _ in range(required_lines - current_lines):
                output += f"{CLEAR_LINE}\n"

            output += f"----------------------------------------------------------{CLEAR_LINE}\n"

            # --- SECTION 3: LOGS ---
            for log in state.log_buffer:
                output += f"{log}{CLEAR_LINE}\n"
            
            output += f"----------------------------------------------------------{CLEAR_LINE}\n"
            output += f"{RESTORE_CURSOR}"

            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(0.5)

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
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{BOLD}KOTAK NEO CLI - UAT MODE{RESET}")
    print(f"{YELLOW}This mode connects to Kotak's UAT (Sandbox) Environment.{RESET}")
    print("Please provide UAT Credentials (request from API Dashboard).")
    
    # 1. Credentials (Updated: No Consumer Secret)
    ck = input("Consumer Key: ").strip()
    mn = input("Mobile Number: ").strip()
    ucc = input("UCC (Client Code): ").strip()
    totp = input("Current OTP (TOTP/App Code): ").strip()
    mpin = input("MPIN: ").strip()
    
    # 2. Init Adapter (Updated: Only Consumer Key passed)
    adapter = UATKotakAdapter(ck)
    
    # 3. Perform 2-Step Login
    adapter.login(mn, ucc, totp, mpin)
    
    # 4. Start UI
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" * 18)
    Dashboard().start()

    # 5. Main Loop
    while True:
        try:
            sym = get_input("Enter Symbol (or 'exit'): ", lambda x: True)
            if sym == 'EXIT': break

            results = adapter.search(sym)
            if not results:
                state.add_log(f"Symbol '{sym}' not found in UAT.")
                continue
            
            scrip = results[0]
            token = scrip['pSymbol'] # Unique Token ID
            display_name = scrip['pTrdSymbol']
            
            # Setup Tracking
            state.set_focus(token) 
            if state.get_ltp(token) == 0.0: state.update_ltp(token, 0.0)
            
            adapter.subscribe(token)
            state.add_log(f"Tracking {display_name} (Token: {token})")

            # Trade Inputs
            bs = get_input("Buy/Sell (B/S): ", lambda x: x.upper() in ['B','S']).upper()
            if bs == 'EXIT': break
            
            qty = get_input("Quantity: ", lambda x: x.isdigit())
            if qty == 'EXIT': break
            
            sl = get_input("SL Points: ", lambda x: x.replace('.', '', 1).isdigit())
            if sl == 'EXIT': break
            
            tgt = get_input("Target Points: ", lambda x: x.replace('.', '', 1).isdigit())
            if tgt == 'EXIT': break

            # Launch Thread
            tm = OCOStrategy(adapter, scrip, token, bs, qty, sl, tgt)
            tm.start()

        except KeyboardInterrupt:
            break
            
    state.shutdown_flag.set()
    print(f"\n{RESET}System Shutdown.")

if __name__ == "__main__":
    main()
