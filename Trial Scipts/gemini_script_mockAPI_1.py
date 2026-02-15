import sys
import time
import threading
import random

# ==========================================
# PART 1: MOCK API (Simulates Kotak Neo)
# ==========================================

class MockNeoAPI:
    def __init__(self, consumer_key=None, consumer_secret=None, environment='prod'):
        print("[MockAPI] Initialized in Test Mode")
        self.on_message = None
        self.on_error = None
        self.on_open = None
        self.on_close = None
        self.running = False
        self.active_orders = {}  # Store order status
        
    # --- Auth Bypasses ---
    def totp_login(self, mobile_number, password):
        print(f"[MockAPI] Login simulated for {mobile_number}")
        return {'stat': 'Ok'}

    def totp_validate(self, mpin):
        print(f"[MockAPI] MPIN validated")
        return {'stat': 'Ok'}
        
    # --- Data Simulation ---
    def search_scrip(self, exchange_segment, symbol):
        # Always return a valid dummy result for any symbol entered
        print(f"[MockAPI] Searching for {symbol}...")
        return [{
            'pSymbol': '12345',         # Dummy Token
            'pExchSeg': 'nse_cm',
            'pTrdSymbol': f"{symbol.upper()}-EQ",
            'pDesc': f"{symbol.upper()} Limited"
        }]

    def subscribe(self, instrument_tokens, isIndex=False, isDepth=False):
        # Start a background thread to push fake ticks to the main script
        print(f"[MockAPI] Subscribed to {instrument_tokens}")
        self.running = True
        t = threading.Thread(target=self._simulate_ticks, args=(instrument_tokens,), daemon=True)
        t.start()

    def _simulate_ticks(self, tokens):
        """Generates random price movement"""
        base_price = 2500.00
        while self.running:
            time.sleep(1) # 1 tick per second
            # Randomly fluctuate price by +/- 0.50
            change = random.choice([-0.50, 0.0, 0.50, 1.0])
            base_price += change
            
            # Construct payload resembling real API
            # - Payload structure
            payload = {
                'tk': tokens[0]['instrument_token'],
                'ltp': str(f"{base_price:.2f}")
            }
            
            if self.on_message:
                self.on_message([payload]) # Send as list

    # --- Order Simulation ---
    def place_order(self, **kwargs):
        # Generate a fake Order ID
        order_id = str(random.randint(100000, 999999))
        print(f"[MockAPI] Order Placed: {kwargs['transaction_type']} {kwargs['quantity']} Qty @ {kwargs.get('price', 'MKT')}")
        
        # Store initial status as 'pending'
        self.active_orders[order_id] = {
            'status': 'pending',
            'created_at': time.time(),
            'type': kwargs['order_type']
        }
        
        # Return success response
        return {'nOrdNo': order_id, 'stat': 'Ok'}

    def order_history(self, order_id):
        # Simulate order filling after 5 seconds
        order = self.active_orders.get(order_id)
        if not order:
            return {'data': []}
            
        # Logic: If 5 seconds passed, mark as 'traded' (filled)
        if time.time() - order['created_at'] > 5:
            order['status'] = 'traded'
        
        # Return response structure
        return {'data': [{'ordSt': order['status']}]}

    def cancel_order(self, order_id):
        print(f"[MockAPI] CANCEL REQUEST received for Order ID: {order_id}")
        if order_id in self.active_orders:
            self.active_orders[order_id]['status'] = 'cancelled'
        return {'stat': 'Ok'}

# ==========================================
# PART 2: YOUR ORIGINAL LOGIC (Unchanged)
# ==========================================

# Global Shared Data
live_feed_cache = {}
feed_lock = threading.Lock()

def on_message(message):
    try:
        data_list = message if isinstance(message, list) else [message]
        with feed_lock:
            for data in data_list:
                if 'tk' in data and 'ltp' in data:
                    live_feed_cache[str(data['tk'])] = float(data['ltp'])
    except Exception:
        pass

def on_error(error_message): pass
def on_open(message): pass
def on_close(message): pass

class TradeManager(threading.Thread):
    def __init__(self, client, symbol_info, txn_type, qty, sl_points, tgt_points):
        super().__init__()
        self.client = client
        self.symbol_info = symbol_info
        self.txn_type = txn_type.upper()
        self.qty = str(qty)
        self.sl_pts = float(sl_points)
        self.tgt_pts = float(tgt_points)
        self.stop_event = threading.Event()
        self.daemon = True

    def run(self):
        symbol = self.symbol_info['pTrdSymbol']
        exch_seg = self.symbol_info['pExchSeg']
        
        print(f"\n[TradeManager] Executing {self.txn_type} order for {symbol}...")

        # 1. Entry
        entry_resp = self.client.place_order(
            exchange_segment=exch_seg, product="MIS", price="0", order_type="MKT",
            quantity=self.qty, validity="DAY", trading_symbol=symbol, transaction_type=self.txn_type
        )
        
        if not entry_resp or "nOrdNo" not in entry_resp:
            print(f"Entry Failed")
            return

        print(f"[TradeManager] Entry Placed. Order ID: {entry_resp['nOrdNo']}")
        
        # Simulate getting entry price (using cache for speed)
        time.sleep(1)
        token = str(self.symbol_info['pSymbol'])
        with feed_lock:
            entry_price = live_feed_cache.get(token, 2500.0) # Default if cache empty
            
        # 2. Calculate Exit Prices
        if self.txn_type == "B":
            sl_price = entry_price - self.sl_pts
            tgt_price = entry_price + self.tgt_pts
            exit_txn_type = "S"
        else:
            sl_price = entry_price + self.sl_pts
            tgt_price = entry_price - self.tgt_pts
            exit_txn_type = "B"

        # 3. Place Target & SL
        tgt_resp = self.client.place_order(
            exchange_segment=exch_seg, product="MIS", price=str(tgt_price),
            order_type="L", quantity=self.qty, validity="DAY", 
            trading_symbol=symbol, transaction_type=exit_txn_type
        )
        
        sl_resp = self.client.place_order(
            exchange_segment=exch_seg, product="MIS", price=str(sl_price),
            trigger_price=str(sl_price), order_type="SL", quantity=self.qty, 
            validity="DAY", trading_symbol=symbol, transaction_type=exit_txn_type
        )

        tgt_id = tgt_resp.get('nOrdNo')
        sl_id = sl_resp.get('nOrdNo')
        
        print(f"[TradeManager] Exits set: SL @ {sl_price:.2f} | TGT @ {tgt_price:.2f}")

        # 4. Monitor Loop (The OCO Logic)
        print("[TradeManager] Waiting for simulated fill (approx 5s)...")
        while not self.stop_event.is_set():
            time.sleep(1) 

            # Check Target
            tgt_hist = self.client.order_history(order_id=tgt_id)
            if self._is_completed(tgt_hist):
                print(f"\n[OCO] Target HIT for {symbol}. Cancelling SL ({sl_id})...")
                self.client.cancel_order(order_id=sl_id)
                break

            # Check SL
            sl_hist = self.client.order_history(order_id=sl_id)
            if self._is_completed(sl_hist):
                print(f"\n[OCO] SL HIT for {symbol}. Cancelling Target ({tgt_id})...")
                self.client.cancel_order(order_id=tgt_id)
                break

    def _is_completed(self, order_data):
        if 'data' in order_data and len(order_data['data']) > 0:
            status = order_data['data'][0].get('ordSt', '').lower()
            return status == 'traded'
        return False

# ==========================================
# PART 3: MAIN EXECUTION
# ==========================================

def track_live_feed(client, scrip_info):
    token = str(scrip_info['pSymbol'])
    client.subscribe(instrument_tokens=[{"instrument_token": token, "exchange_segment": "nse_cm"}])
    print(f"\n--- Tracking {scrip_info['pTrdSymbol']} (Mock) ---")
    print("Press 'Ctrl+C' to Stop Tracking and Enter Trade Params.\n")
    try:
        while True:
            with feed_lock:
                price = live_feed_cache.get(token, "Waiting...")
            sys.stdout.write(f"\rCurrent LTP: {price:<10}")
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\n[Mode Switch] Enter Trade Parameters:")

def main():
    # USE THE MOCK API INSTEAD OF REAL ONE
    client = MockNeoAPI() 
    
    # Initialize callbacks
    client.on_message = on_message
    
    # Login Flow (Fake)
    client.totp_login("1234567890", "password")
    client.totp_validate("1234")

    while True:
        try:
            user_input = input("\nWhich symbol to enter? (or 'exit'): ").strip()
            if user_input.lower() == 'exit': sys.exit(0)
            if not user_input: continue

            # Search (Will return dummy 'RELIANCE-EQ' etc)
            scrip = client.search_scrip("nse_cm", user_input)[0]

            # Live Tracking
            track_live_feed(client, scrip)

            # Trade Inputs
            bs_input = input("B/S?: ").strip().upper() or "B"
            qty_input = input("Quantity?: ").strip() or "50"
            sl_input = input("SL points?: ").strip() or "10"
            tgt_input = input("Target points?: ").strip() or "20"
            
            # Start Thread
            t = TradeManager(client, scrip, bs_input, qty_input, sl_input, tgt_input)
            t.start()
            
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
