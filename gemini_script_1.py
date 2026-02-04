import sys
import time
import threading
import logging
import json

# Try to import the library; handle error if missing
try:
    from neo_api_client import NeoAPI
except ImportError:
    print("CRITICAL ERROR: 'neo_api_client' not found.")
    print("Please install it: pip install .")
    sys.exit(1)

# ==========================================
# 1. CONFIGURATION
# ==========================================
CONFIG = {
    "consumer_key": "YOUR_CONSUMER_KEY",
    "consumer_secret": "YOUR_CONSUMER_SECRET",
    "mobile_number": "YOUR_MOBILE_NUMBER",
    "password": "YOUR_PASSWORD",  # Login Password or MPIN
    "environment": "prod"         # 'prod' or 'uat'
}

# ==========================================
# 2. SHARED STATE (Concurrency Handling)
# ==========================================
# Mapping: Token ID (str) -> Last Traded Price (float)
# The WebSocket thread WRITES to this. The UI/Trade threads READ from this.
LTP_DATA = {}

# We use a Lock to prevent "Race Conditions" where two threads read/write simultaneously
DATA_LOCK = threading.Lock()

# ANSI Escape Codes for CLI UI
ANSI_CLEAR_SCREEN = "\033[2J"
ANSI_HOME = "\033[H"
ANSI_SAVE_CURSOR = "\033[s"
ANSI_RESTORE_CURSOR = "\033[u"
ANSI_CLEAR_LINE = "\033[K"

# Logging setup (Saved to file, so it doesn't mess up the CLI UI)
logging.basicConfig(filename="trade_log.txt", level=logging.INFO, 
                    format='%(asctime)s [%(levelname)s] %(message)s')

# ==========================================
# 3. WEBSOCKET CALLBACKS
# ==========================================
def on_message(message):
    """
    Called by the Background WebSocket Thread whenever data arrives.
    Updates the shared LTP_DATA dictionary.
    """
    try:
        # The structure of 'message' can vary. We assume standard Kotak Feed format.
        # Usually: {'type': 'sf', 'data': [{'tk': '2885', 'ltp': '2400.00', ...}]}
        # Or sometimes a list directly.
        
        payload = message
        if isinstance(message, dict) and 'data' in message:
            payload = message['data']
            
        if isinstance(payload, list):
            for item in payload:
                if 'tk' in item and 'ltp' in item:
                    token = str(item['tk'])
                    try:
                        price = float(item['ltp'])
                        with DATA_LOCK:
                            LTP_DATA[token] = price
                    except ValueError:
                        pass
    except Exception as e:
        logging.error(f"WebSocket Parse Error: {e}")

def on_error(error_msg):
    logging.error(f"WebSocket Error: {error_msg}")

# ==========================================
# 4. TRADE MANAGEMENT (The "Business Logic")
# ==========================================
class TradeManager(threading.Thread):
    """
    A Background Worker that manages a single active trade.
    1. Executes the Entry Order.
    2. Monitors the Price in the background.
    3. Executes Exit (OCO) when Target or SL is hit.
    """
    def __init__(self, client, symbol, token, direction, qty, sl_pts, tgt_pts):
        super().__init__()
        self.client = client
        self.symbol = symbol
        self.token = str(token)
        self.direction = direction.upper() # 'B' or 'S'
        self.qty = str(qty)
        self.sl_pts = float(sl_pts)
        self.tgt_pts = float(tgt_pts)
        self.daemon = True # Thread dies if main program closes

    def run(self):
        logging.info(f"[{self.symbol}] Starting Trade Manager...")
        
        # --- STEP 1: EXECUTE ENTRY ---
        try:
            txn_type = "BUY" if self.direction == "B" else "SELL"
            
            # Place Market Order
            # Note: Hardcoded 'NSE', 'MIS' for simplicity as per common interview assumptions
            self.client.place_order(
                exchange_segment="nse_cm", product="MIS", price="0", order_type="MKT",
                quantity=self.qty, validity="DAY", trading_symbol=self.symbol,
                transaction_type=txn_type
            )
            logging.info(f"[{self.symbol}] Entry Order Sent ({txn_type}).")
            
            # Simulate "Wait for Fill" & Get Entry Price
            # In a real app, we would query the Order Book. 
            # Here, we assume fill at current LTP for logic testing.
            time.sleep(1) 
            with DATA_LOCK:
                entry_price = LTP_DATA.get(self.token)
            
            if not entry_price:
                logging.error(f"[{self.symbol}] No LTP found. Aborting OCO.")
                return

            # Calculate Exit Levels
            if self.direction == "B":
                sl_price = entry_price - self.sl_pts
                tgt_price = entry_price + self.tgt_pts
            else:
                sl_price = entry_price + self.sl_pts
                tgt_price = entry_price - self.tgt_pts
                
            logging.info(f"[{self.symbol}] Filled @ {entry_price}. Watching SL: {sl_price}, TGT: {tgt_price}")

            # --- STEP 2: MONITOR (OCO LOGIC) ---
            trade_active = True
            while trade_active:
                with DATA_LOCK:
                    current_ltp = LTP_DATA.get(self.token)
                
                if current_ltp:
                    # Check EXIT Conditions
                    exit_reason = None
                    
                    if self.direction == "B":
                        if current_ltp >= tgt_price: exit_reason = "TARGET HIT"
                        elif current_ltp <= sl_price: exit_reason = "STOPLOSS HIT"
                    else: # Sell
                        if current_ltp <= tgt_price: exit_reason = "TARGET HIT"
                        elif current_ltp >= sl_price: exit_reason = "STOPLOSS HIT"
                    
                    # Execute Exit if Triggered
                    if exit_reason:
                        exit_txn = "SELL" if self.direction == "B" else "BUY"
                        self.client.place_order(
                            exchange_segment="nse_cm", product="MIS", price="0", order_type="MKT",
                            quantity=self.qty, validity="DAY", trading_symbol=self.symbol,
                            transaction_type=exit_txn
                        )
                        logging.info(f"[{self.symbol}] {exit_reason} @ {current_ltp}. Exit Order Sent.")
                        trade_active = False # Stop monitoring
                
                time.sleep(0.5) # Sleep to save CPU
                
        except Exception as e:
            logging.error(f"[{self.symbol}] Manager Error: {e}")

# ==========================================
# 5. UI HELPER (The "In-Place" Updater)
# ==========================================
class LTPDisplay(threading.Thread):
    """
    Updates the top of the screen with the current price 
    while the user types at the bottom.
    """
    def __init__(self, token):
        super().__init__()
        self.token = str(token)
        self.running = True
        self.daemon = True

    def run(self):
        while self.running:
            with DATA_LOCK:
                price = LTP_DATA.get(self.token, "Waiting...")
            
            # UI TRICK: 
            # 1. Save Cursor (Bottom)
            # 2. Jump to Row 2, Col 15 (Header area)
            # 3. Print Price
            # 4. Restore Cursor (Bottom)
            sys.stdout.write(ANSI_SAVE_CURSOR)
            sys.stdout.write(f"\033[2;15H{ANSI_CLEAR_LINE}{price}")
            sys.stdout.write(ANSI_RESTORE_CURSOR)
            sys.stdout.flush()
            time.sleep(0.2)

    def stop(self):
        self.running = False

# ==========================================
# 6. MAIN APPLICATION LOOP
# ==========================================
def main():
    # --- SETUP UI ---
    sys.stdout.write(ANSI_CLEAR_SCREEN)
    sys.stdout.write(ANSI_HOME)
    print("========================================")
    print("Current LTP:  --                        ") # Row 2 (Target for Update)
    print("========================================")
    print("LOG: Check trade_log.txt for debug info.")
    print("----------------------------------------")
    
    # --- LOGIN ---
    print("Initializing API...")
    try:
        client = NeoAPI(
            consumer_key=CONFIG['consumer_key'], 
            consumer_secret=CONFIG['consumer_secret'], 
            environment=CONFIG['environment']
        )
        client.login(mobilenumber=CONFIG['mobile_number'], password=CONFIG['password'])
        client.on_message = on_message
        client.on_error = on_error
        # Note: In a real scenario, you might need to call client.session_2fa(OTP) here
        
    except Exception as e:
        print(f"Login Failed: {e}")
        return

    print("API Connected. Starting Main Loop.")
    print("(Type 'EXIT' as symbol to quit)")

    # --- INPUT LOOP ---
    while True:
        try:
            # 1. Prompt for Symbol
            symbol = input("\nWhich symbol to enter? (e.g., RELIANCE): ").strip().upper()
            if symbol == "EXIT": break
            if not symbol: continue

            # 2. Search & Subscribe
            try:
                # Search 'nse_cm' for the symbol
                scrip_data = client.search_scrip(exchange_segment="nse_cm", symbol=symbol)
                
                # Validation: Did we find it?
                target_token = None
                trading_symbol = None
                
                if scrip_data and isinstance(scrip_data, list):
                    # Loop to find exact match or take first
                    for scrip in scrip_data:
                        if scrip.get('pTrdSymbol') == symbol + "-EQ" or scrip.get('pSymbol') == symbol:
                            target_token = scrip.get('pSymbol')
                            trading_symbol = scrip.get('pTrdSymbol')
                            break
                    # Fallback to first if no exact match
                    if not target_token:
                        target_token = scrip_data[0].get('pSymbol')
                        trading_symbol = scrip_data[0].get('pTrdSymbol')

                if not target_token:
                    print("Symbol not found on NSE.")
                    continue

                # Subscribe
                client.subscribe(instrument_tokens=[{"instrument_token": target_token, "exchange_segment": "nse_cm"}])
                
            except Exception as e:
                print(f"Error finding symbol: {e}")
                continue

            # 3. Start In-Place Tracking (Non-Blocking Display)
            tracker = LTPDisplay(target_token)
            tracker.start()

            # 4. Get Trade Parameters (While Price Updates at Top)
            # The tracker thread is updating Row 2 while we sit here in input()
            try:
                direction = input("B/S?: ").strip().upper()
                qty = input("Quantity?: ").strip()
                sl_pts = input("SL Points?: ").strip()
                tgt_pts = input("Target Points?: ").strip()
            except ValueError:
                print("Invalid Input.")
                tracker.stop()
                continue
            
            # Stop the visual tracker for this symbol (we are moving to execution)
            tracker.stop()
            tracker.join() # Clean exit of thread

            # 5. Execute & Background (Non-Blocking)
            # We spin up a NEW thread for this trade and immediately loop back
            manager = TradeManager(client, trading_symbol, target_token, direction, qty, sl_pts, tgt_pts)
            manager.start()
            
            print(f"Trade Initiated for {trading_symbol}. Monitoring in background...")
            
        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            logging.error(f"Main Loop Error: {e}")

if __name__ == "__main__":
    main()