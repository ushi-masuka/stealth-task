import sys
import time
import threading
import os
import random
import logging
from collections import deque
from datetime import datetime

# --- CONFIGURATION ---
LOG_FILENAME = "trade_log.txt"

# Try to import NeoAPI, otherwise default to Mock
try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None

# --- ANSI COLORS & TERMINAL CODES (Your Preferred UI Theme) ---
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
# 1. STATE MANAGER (The "Brain")
# ==========================================
class StateManager:
    """
    Central repository for shared data and logging.
    Handles both On-Screen Display and File Persistence.
    """
    def __init__(self):
        # Live Market Data (Dictionary for Multi-Stock Support)
        self.ltp_cache = {} 
        self.ltp_lock = threading.Lock()
        
        # Focus Symbol (What the user is currently typing/viewing)
        self.focus_symbol = "None"
        
        # Active Trades (Portfolio)
        self.active_trades = []
        self.trades_lock = threading.Lock()
        
        # On-Screen Logs (Notification Center)
        self.log_buffer = deque(maxlen=6)
        for _ in range(6): self.log_buffer.append("")
        
        self.shutdown_flag = threading.Event()
        self.log_to_file("--- SESSION STARTED ---")

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
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logging.info(f"[{timestamp}] {raw_message}")
        except: pass

    def add_log(self, message):
        """
        1. Formats message for UI (Colors)
        2. Saves raw message to Text File
        """
        self.log_to_file(message)
        timestamp = time.strftime("%H:%M:%S")
        
        if "PROFIT" in message: fmt_msg = f"{GREEN}{BOLD}{message}{RESET}"
        elif "LOSS" in message: fmt_msg = f"{RED}{BOLD}{message}{RESET}"
        elif "Executing" in message: fmt_msg = f"{CYAN}{message}{RESET}"
        elif "Error" in message: fmt_msg = f"{RED}{message}{RESET}"
        else: fmt_msg = message
            
        self.log_buffer.append(f"[{timestamp}] {fmt_msg}")

# Global Instance
state = StateManager()

# ==========================================
# 2. UI DASHBOARD (The "Face") - YOUR PREFERRED DESIGN
# ==========================================
class Dashboard(threading.Thread):
    """
    Refreshes the top part of the screen with:
    1. Focus Ticker
    2. Active Portfolio
    3. Logs
    """
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not state.shutdown_flag.is_set():
            output = f"{SAVE_CURSOR}{MOVE_TO_TOP}"
            
            # --- SECTION 1: FOCUS TICKER ---
            # Lookup price by the CURRENT focus token (Thread-Safe)
            focus_sym = state.focus_symbol
            focus_ltp = state.get_ltp(focus_sym)
            
            output += f"{BOLD}-------------------- LIVE MONITOR --------------------{RESET}{CLEAR_LINE}\n"
            output += f" FOCUS: {CYAN}{focus_sym:<10}{RESET} | LTP: {GREEN}{focus_ltp:.2f}{RESET}{CLEAR_LINE}\n"
            output += f"------------------------------------------------------{CLEAR_LINE}\n"
            
            # --- SECTION 2: ACTIVE PORTFOLIO ---
            output += f"{BOLD}[ ACTIVE PORTFOLIO ]{RESET}{CLEAR_LINE}\n"
            
            with state.trades_lock:
                if not state.active_trades:
                    output += f"{YELLOW} No active trades.{RESET}{CLEAR_LINE}\n"
                else:
                    for t in state.active_trades:
                        # 1. Get Live Data (Thread-Safe Lookup)
                        # Ideally, 't' should store token ID, but symbol works if mapped correctly
                        symbol = t['symbol']
                        entry = t['entry']
                        qty = int(t['qty'])
                        curr_price = state.get_ltp(t['token']) # Key fix: lookup by token
                        
                        # 2. Calculate P/L
                        if t['type'] == 'B':
                            pnl = (curr_price - entry) * qty
                        else:
                            pnl = (entry - curr_price) * qty
                        
                        # 3. Format
                        pnl_str = f"{pnl:+.2f}"
                        pnl_color = GREEN if pnl >= 0 else RED
                        
                        line = (f" {symbol:<9} ({t['type']}) | "
                                f"Ent: {entry:<7.2f} | "
                                f"LTP: {WHITE}{curr_price:<7.2f}{RESET} | "
                                f"P/L: {pnl_color}{pnl_str}{RESET}")
                        
                        output += f"{line}{CLEAR_LINE}\n"
            
            # Pad empty lines
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
# 3. API ADAPTERS (Real & Mock)
# ==========================================
class RealKotakAdapter:
    def __init__(self, ck,mn, pw, env='prod'):
        try:
            from neo_api_client import NeoAPI
            self.client = NeoAPI(consumer_key=ck, environment=env)
            self.mobile_num = mn
            self.password = pw
            self.env = env
        except ImportError:
            raise ImportError("neo_api_client not installed")

    def login(self):
        print(f"{YELLOW}Authenticating with Kotak Neo ({self.env.upper()})...{RESET}")
        try:
            self.client.login(mobilenumber=self.mobile_num, password=self.password)
            print(f"{CYAN}If OTP sent, enter below. If not, press Enter.{RESET}")
            otp = input("Enter OTP (or leave empty): ").strip()
            if otp:
                self.client.session_2fa(OTP=otp)
                print(f"{GREEN}2FA Validated.{RESET}")
            else:
                print(f"{GREEN}Proceeding...{RESET}")
            
            self.client.on_message = self.on_message_wrapper
            self.client.on_error = lambda e: None 
            
        except Exception as e:
            print(f"{RED}Login Failed: {e}{RESET}")
            sys.exit(1)

    def on_message_wrapper(self, message):
        try:
            payload = message
            if isinstance(message, dict) and 'data' in message:
                payload = message['data']
            if isinstance(payload, list):
                for item in payload:
                    if 'tk' in item and 'ltp' in item:
                        # Update cache using Token ID
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


class MockKotakAdapter:
    def __init__(self):
        self.orders = {}
        self.subscribed_tokens = set()
        
    def login(self):
        print(f"{GREEN}[Mock] Login Successful.{RESET}")
        time.sleep(1)

    def search(self, symbol):
        # Generate Unique Token based on symbol hash
        token_id = str(sum(ord(c) for c in symbol)) 
        return [{
            'pSymbol': token_id, 
            'pExchSeg': 'nse_cm', 
            'pTrdSymbol': f"{symbol.upper()}-EQ"
        }]

    def subscribe(self, token):
        self.subscribed_tokens.add(token)
        threading.Thread(target=self._data_pump, args=(token,), daemon=True).start()

    def _data_pump(self, token):
        price = 1000.0 + int(token) % 100 
        while True:
            time.sleep(0.5)
            change = random.choice([-1.0, -0.5, 0.0, 0.5, 1.0])
            price += change
            state.update_ltp(token, price)

    def place_order(self, **kwargs):
        oid = str(random.randint(10000, 99999))
        self.orders[oid] = {'status': 'pending', 't': time.time()}
        return {'nOrdNo': oid, 'stat': 'Ok'}

    def get_order_history(self, order_id):
        if order_id in self.orders:
            if time.time() - self.orders[order_id]['t'] > 5:
                self.orders[order_id]['status'] = 'traded'
            return {'data': [{'ordSt': self.orders[order_id]['status']}]}
        return {}

    def cancel_order(self, order_id): return {}

# ==========================================
# 4. TRADING ENGINE (Business Logic)
# ==========================================
class OCOStrategy(threading.Thread):
    def __init__(self, client, symbol_info, token, txn_type, qty, sl_pts, tgt_pts):
        super().__init__()
        self.client = client
        self.scrip = symbol_info
        self.symbol = symbol_info['pTrdSymbol']
        self.token = token
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
            time.sleep(1) 
            # Fetch price specific to THIS token
            entry_price = state.get_ltp(self.token)
            if entry_price == 0: entry_price = 1000.0
            
            # 3. REGISTER TRADE
            trade_record = {
                'symbol': self.symbol,
                'token': self.token, # Store token for P/L lookup
                'type': self.txn_type,
                'entry': entry_price,
                'qty': self.qty,
                'sl': 0, 'tgt': 0
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
            hist = self.client.get_order_history(order_id)
            if 'data' in hist and hist['data']:
                return hist['data'][0].get('ordSt', '').lower() == 'traded'
        except: pass
        return False

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
    print(f"{BOLD}Welcome to Kotak Neo CLI{RESET}")
    print("1. Mock Mode")
    print("2. UAT Mode")
    print("3. PROD Mode")
    
    mode = input("Select Mode (1/2/3): ").strip()
    
    env_map = {'2': 'uat', '3': 'prod'}
    
    if mode in ['2', '3']:
        env = env_map[mode]
        print(f"\n[{env.upper()} MODE] Enter Credentials:")
        ck = input("Consumer Key: ").strip()
        mn = input("Mobile Number: ").strip()
        pw = input("Password/MPIN: ").strip()
        
        # Initialize Real Adapter with UAT or PROD environment
        adapter = RealKotakAdapter(ck,mn,pw, env=env)
    else:
        adapter = MockKotakAdapter()

    # Login
    adapter.login()
    
    # Start UI
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" * 18) # Reserve more space for the bigger dashboard
    Dashboard().start()

    # Main Loop
    while True:
        try:
            # 1. Symbol Entry
            sym = get_input("Enter Symbol (or 'exit'): ", lambda x: True)
            if sym == 'EXIT': break

            # 2. Search
            results = adapter.search(sym)
            if not results:
                state.add_log(f"Symbol '{sym}' not found.", "ERROR")
                continue
            
            scrip = results[0]
            token = scrip['pSymbol'] # Important: Unique Token
            display_name = scrip['pTrdSymbol']
            
            # Set Focus (Update Ticker) using Token ID
            state.set_focus(token) 
            # Reset prev price if empty
            if state.get_ltp(token) == 0.0: state.update_ltp(token, 0.0)
            
            adapter.subscribe(token)
            
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
            # Pass TOKEN so thread knows exactly what to watch
            tm = OCOStrategy(adapter, scrip, token, bs, qty, sl, tgt)
            tm.start()

        except KeyboardInterrupt:
            break
            
    state.shutdown_flag.set()
    state.log_to_file("--- SESSION ENDED ---")
    print(f"\n{RESET}System Shutdown.")

if __name__ == "__main__":
    main()
