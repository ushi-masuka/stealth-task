import sys
import time
import threading
import os
import random
import logging
from collections import deque
from datetime import datetime

# ==========================================
# 1. CONFIGURATION & STYLING
# ==========================================
LOG_FILENAME = "simulation_log.txt"

# ANSI Colors
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

logging.basicConfig(filename=LOG_FILENAME, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ==========================================
# 2. STATE MANAGER (Shared Data)
# ==========================================
class StateManager:
    """Thread-safe storage for Prices, Trades, and Logs."""
    def __init__(self):
        # Data
        self.ltp_cache = {} 
        self.active_trades = []
        self.log_buffer = deque(maxlen=6) # Keeping last 6 logs
        for _ in range(6): self.log_buffer.append("")
        
        # UI State
        self.focus_token = None
        self.focus_symbol = "N/A"
        
        # Locks
        self.lock = threading.Lock()
        self.shutdown_flag = threading.Event()

    def update_price(self, token, price):
        with self.lock:
            self.ltp_cache[token] = float(price)

    def get_price(self, token):
        with self.lock:
            return self.ltp_cache.get(token, 0.0)

    def add_trade(self, trade):
        with self.lock:
            self.active_trades.append(trade)

    def remove_trade(self, token):
        with self.lock:
            self.active_trades = [t for t in self.active_trades if t['token'] != token]

    def add_log(self, msg):
        logging.info(msg)
        timestamp = time.strftime("%H:%M:%S")
        
        # Coloring
        if "PROFIT" in msg: fmt = f"{GREEN}{BOLD}{msg}{RESET}"
        elif "LOSS" in msg: fmt = f"{RED}{BOLD}{msg}{RESET}"
        elif "Executing" in msg: fmt = f"{CYAN}{msg}{RESET}"
        elif "Error" in msg: fmt = f"{RED}{msg}{RESET}"
        else: fmt = msg
            
        with self.lock:
            self.log_buffer.append(f"[{timestamp}] {fmt}")

state = StateManager()

# ==========================================
# 3. MOCK ADAPTER (Simulation Engine)
# ==========================================
class MockAdapter:
    """Simulates Kotak API behavior."""
    def __init__(self):
        self.orders = {}
        self.subscribed_tokens = set()

    def search_scrip(self, symbol):
        # Deterministic Token ID based on symbol name
        token_id = str(sum(ord(c) for c in symbol))
        return [{
            'pSymbol': token_id, 
            'pTrdSymbol': f"{symbol.upper()}-EQ",
            'pExchSeg': 'nse_cm'
        }]

    def subscribe(self, token):
        if token in self.subscribed_tokens: return
        self.subscribed_tokens.add(token)
        # Start a dedicated feed thread for this token
        t = threading.Thread(target=self._feed_loop, args=(token,), daemon=True)
        t.start()

    def _feed_loop(self, token):
        price = 1000.0 + (int(token) % 500)
        while True:
            time.sleep(0.5)
            price += random.choice([-1.5, -0.5, 0.0, 0.5, 1.5])
            state.update_price(token, price)

    def place_order(self, **kwargs):
        oid = str(random.randint(10000, 99999))
        self.orders[oid] = {'status': 'pending', 't': time.time()}
        return {'nOrdNo': oid}

    def order_history(self, oid):
        if oid in self.orders:
            # Auto-fill after 2 seconds
            if time.time() - self.orders[oid]['t'] > 2:
                self.orders[oid]['status'] = 'traded'
            return {'data': [{'ordSt': self.orders[oid]['status']}]}
        return {}

    def cancel_order(self, oid):
        if oid in self.orders: self.orders[oid]['status'] = 'cancelled'
        return {'stat': 'Ok'}

# ==========================================
# 4. TRADE MANAGER (The Logic)
# ==========================================
class TradeManager(threading.Thread):
    """Manages one active trade (Entry -> OCO -> Exit)."""
    def __init__(self, client, symbol, token, txn, qty, sl, tgt):
        super().__init__()
        self.client = client
        self.symbol = symbol
        self.token = token
        self.txn = txn
        self.qty = qty
        self.sl_pts = sl
        self.tgt_pts = tgt
        self.daemon = True

    def run(self):
        state.add_log(f"[{self.symbol}] Strategy Started ({self.txn})...")
        
        # 1. Entry
        resp = self.client.place_order(trading_symbol=self.symbol)
        if not resp: return
        
        # 2. Simulate Fill
        time.sleep(2.5) 
        entry_price = state.get_price(self.token)
        if entry_price == 0: entry_price = 1000.0

        # 3. Add to UI
        state.add_trade({
            'symbol': self.symbol, 'token': self.token, 
            'type': self.txn, 'entry': entry_price, 'qty': self.qty
        })
        state.add_log(f"[{self.symbol}] Filled @ {entry_price:.2f}")

        # 4. Calc Levels
        if self.txn == 'B':
            sl_price = entry_price - self.sl_pts
            tgt_price = entry_price + self.tgt_pts
        else:
            sl_price = entry_price + self.sl_pts
            tgt_price = entry_price - self.tgt_pts

        # 5. Place OCO
        tgt_id = self.client.place_order()['nOrdNo']
        sl_id = self.client.place_order()['nOrdNo']
        state.add_log(f"[{self.symbol}] OCO Active: SL {sl_price:.1f} | TGT {tgt_price:.1f}")

        # 6. Monitor Loop
        while not state.shutdown_flag.is_set():
            time.sleep(1)
            # Check Target
            if self._is_filled(tgt_id):
                state.add_log(f"[{self.symbol}] TARGET HIT! (PROFIT)")
                self.client.cancel_order(sl_id)
                break
            # Check SL
            if self._is_filled(sl_id):
                state.add_log(f"[{self.symbol}] STOP LOSS HIT! (LOSS)")
                self.client.cancel_order(tgt_id)
                break
        
        state.remove_trade(self.token)

    def _is_filled(self, oid):
        hist = self.client.order_history(oid)
        if 'data' in hist and hist['data']:
            return hist['data'][0]['ordSt'] == 'traded'
        return False

# ==========================================
# 5. DASHBOARD (The UI)
# ==========================================
class Dashboard(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while not state.shutdown_flag.is_set():
            # Data Fetch
            curr_price = state.get_price(state.focus_token)
            
            # --- RENDER ---
            output = f"{SAVE_CURSOR}{MOVE_TO_TOP}"
            output += f"{BOLD}-------------------- TRADING SIMULATION --------------------{RESET}{CLEAR_LINE}\n"
            output += f" FOCUS: {CYAN}{state.focus_symbol:<10}{RESET} | LTP: {GREEN}{curr_price:.2f}{RESET}{CLEAR_LINE}\n"
            output += f"------------------------------------------------------------{CLEAR_LINE}\n"
            
            # Active Trades
            output += f"{BOLD}[ ACTIVE POSITIONS ]{RESET}{CLEAR_LINE}\n"
            with state.lock:
                if not state.active_trades:
                    output += f"{YELLOW} No active trades.{RESET}{CLEAR_LINE}\n"
                else:
                    for t in state.active_trades:
                        ltp = state.get_price(t['token'])
                        pnl = (ltp - t['entry']) * int(t['qty']) if t['type'] == 'B' else (t['entry'] - ltp) * int(t['qty'])
                        color = GREEN if pnl >= 0 else RED
                        line = f" {t['symbol']:<9} ({t['type']}) | Ent: {t['entry']:<7.2f} | LTP: {WHITE}{ltp:<7.2f}{RESET} | {color}{pnl:+.2f}{RESET}"
                        output += f"{line}{CLEAR_LINE}\n"
            
            # Padding
            req_lines = 4
            curr_lines = len(state.active_trades) if state.active_trades else 1
            output += (f"{CLEAR_LINE}\n" * (req_lines - curr_lines))

            output += f"------------------------------------------------------------{CLEAR_LINE}\n"

            # Logs
            with state.lock:
                for log in state.log_buffer:
                    output += f"{log}{CLEAR_LINE}\n"
            
            output += f"------------------------------------------------------------{CLEAR_LINE}\n"
            output += f"{RESTORE_CURSOR}"

            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(0.5)

# ==========================================
# 6. INPUT HELPER
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
# 7. MAIN
# ==========================================
def main():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{BOLD}Initializing Simulator...{RESET}")
    time.sleep(1)
    
    adapter = MockAdapter()
    
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" * 18) 
    Dashboard().start()

    while True:
        try:
            # 1. Symbol
            sym = get_input("Symbol (e.g. RELIANCE): ", lambda x: True)
            if sym == 'EXIT': break
            
            # 2. Search
            res = adapter.search_scrip(sym)
            scrip = res[0]
            token = scrip['pSymbol']
            
            state.set_focus(scrip['pTrdSymbol'], token)
            if state.get_price(token) == 0.0: state.update_price(token, 0.0)
            adapter.subscribe(token)
            state.add_log(f"Tracking {scrip['pTrdSymbol']}")

            # 3. Params
            bs = get_input("B/S: ", lambda x: x.upper() in ['B', 'S']).upper()
            if bs == 'EXIT': break
            
            qty = get_input("Qty: ", lambda x: x.isdigit())
            if qty == 'EXIT': break
            
            sl = get_input("SL Pts: ", lambda x: x.isdigit())
            if sl == 'EXIT': break
            
            tgt = get_input("Tgt Pts: ", lambda x: x.isdigit())
            if tgt == 'EXIT': break

            # 4. Run
            TradeManager(adapter, scrip['pTrdSymbol'], token, bs, qty, float(sl), float(tgt)).start()

        except KeyboardInterrupt:
            break
            
    state.shutdown_flag.set()
    print(f"\n{RESET}Shutdown.")

if __name__ == "__main__":
    main()