import sys
import time
import threading
import logging
from neo_api_client import NeoAPI

# --- Configuration ---
# Replace these with your actual credentials


# --- Global Shared Data ---
# Stores the latest Last Traded Price (LTP) for tokens
# Format: { 'instrument_token': price_float }
live_feed_cache = {}
feed_lock = threading.Lock()

# --- WebSocket Callbacks ---

def on_message(message):
    """
    Callback for WebSocket messages. 
    Updates the global `live_feed_cache` with the latest prices.
    """
    try:
        # The message can be a list of ticks or a single dict
        data_list = message if isinstance(message, list) else [message]
        
        with feed_lock:
            for data in data_list:
                # 'tk' = Instrument Token, 'ltp' = Last Traded Price
                if 'tk' in data and 'ltp' in data:
                    live_feed_cache[str(data['tk'])] = float(data['ltp'])
                # Handle Index feeds (key might be 'iv' instead of 'ltp')
                elif 'tk' in data and 'iv' in data:
                    live_feed_cache[str(data['tk'])] = float(data['iv'])
    except Exception:
        # Suppress errors in the high-frequency callback to keep CLI clean
        pass

def on_error(error_message):
    # Log errors silently to avoid disrupting the CLI UI
    pass

def on_open(message):
    pass

def on_close(message):
    pass

# --- Background Trade Manager ---

class TradeManager(threading.Thread):
    """
    Background thread that manages the lifecycle of a single trade.
    1. Executes the Market Entry Order.
    2. Places Stop-Loss and Target orders.
    3. Monitors orders to enforce One-Cancels-Other (OCO) logic.
    """
    def __init__(self, client, symbol_info, txn_type, qty, sl_points, tgt_points):
        super().__init__()
        self.client = client
        self.symbol_info = symbol_info
        self.txn_type = txn_type.upper()  # 'B' or 'S'
        self.qty = str(qty)
        self.sl_pts = float(sl_points)
        self.tgt_pts = float(tgt_points)
        self.stop_event = threading.Event()
        self.daemon = True  # Thread dies if main program exits

    def run(self):
        symbol = self.symbol_info['pTrdSymbol']
        exch_seg = self.symbol_info['pExchSeg']
        
        # Log to a separate line to avoid messing up the main input prompt slightly
        print(f"\n[TradeManager] Starting execution for {symbol} ({self.txn_type})...")

        # 1. Place Market Entry Order
        try:
            entry_resp = self.client.place_order(
                exchange_segment=exch_seg,
                product="MIS",  # Assuming Intraday
                price="0",
                order_type="MKT",
                quantity=self.qty,
                validity="DAY",
                trading_symbol=symbol,
                transaction_type=self.txn_type
            )

            # Check for success (API response usually contains 'nOrdNo')
            if not entry_resp or "nOrdNo" not in entry_resp:
                print(f"\n[Error] Entry Failed for {symbol}: {entry_resp}")
                return

            order_id = entry_resp['nOrdNo']
            print(f"[TradeManager] Entry Placed. Order ID: {order_id}")

            # 2. Determine Entry Price
            # Ideally, we fetch the fill price from `order_history`. 
            # For speed in this CLI, we use the current live LTP from our cache.
            token = str(self.symbol_info['pSymbol'])
            time.sleep(1) # Wait briefly for fill/tick
            
            with feed_lock:
                entry_price = live_feed_cache.get(token, 0.0)
            
            if entry_price == 0.0:
                print(f"[TradeManager] Warning: Could not fetch LTP for {symbol}. OCO aborted.")
                return

            # 3. Calculate SL and Target Prices
            if self.txn_type == "B":
                sl_price = entry_price - self.sl_pts
                tgt_price = entry_price + self.tgt_pts
                exit_txn_type = "S"
            else:
                sl_price = entry_price + self.sl_pts
                tgt_price = entry_price - self.tgt_pts
                exit_txn_type = "B"

            # 4. Place OCO Legs (Target & SL)
            # Note: Ensure your account has margin for both legs if the broker doesn't link them automatically.
            
            # Place Target (Limit Order)
            tgt_resp = self.client.place_order(
                exchange_segment=exch_seg,
                product="MIS",
                price=str(round(tgt_price, 2)),
                order_type="L",
                quantity=self.qty,
                validity="DAY",
                trading_symbol=symbol,
                transaction_type=exit_txn_type
            )
            
            # Place Stop Loss (SL Limit Order)
            sl_resp = self.client.place_order(
                exchange_segment=exch_seg,
                product="MIS",
                price=str(round(sl_price, 2)),
                trigger_price=str(round(sl_price, 2)), # Trigger at the same price
                order_type="SL",
                quantity=self.qty,
                validity="DAY",
                trading_symbol=symbol,
                transaction_type=exit_txn_type
            )

            tgt_id = tgt_resp.get('nOrdNo')
            sl_id = sl_resp.get('nOrdNo')

            if not tgt_id or not sl_id:
                print(f"\n[Error] Failed to place one of the exit orders for {symbol}. Check positions!")
                return
            
            print(f"[TradeManager] Exits set: SL @ {sl_price:.2f} | TGT @ {tgt_price:.2f}")

            # 5. Monitor Loop (The "OCO" Logic)
            while not self.stop_event.is_set():
                time.sleep(2)  # Check status every 2 seconds

                # Check Target Status
                tgt_hist = self.client.order_history(order_id=tgt_id)
                if self._is_completed(tgt_hist):
                    print(f"\n[OCO] Target HIT for {symbol}. Cancelling SL ({sl_id})...")
                    self.client.cancel_order(order_id=sl_id)
                    break

                # Check SL Status
                sl_hist = self.client.order_history(order_id=sl_id)
                if self._is_completed(sl_hist):
                    print(f"\n[OCO] SL HIT for {symbol}. Cancelling Target ({tgt_id})...")
                    self.client.cancel_order(order_id=tgt_id)
                    break

        except Exception as e:
            print(f"\n[TradeManager] Exception for {symbol}: {e}")

    def _is_completed(self, order_data):
        """Helper to check if order status is 'traded' (completed)"""
        try:
            # API returns a list in 'data', usually index 0 is latest status
            if 'data' in order_data and len(order_data['data']) > 0:
                status = order_data['data'][0].get('ordSt', '').lower()
                return status == 'traded'
        except:
            return False
        return False

# --- CLI Helper Functions ---

def search_symbol(client, symbol_str):
    """Searches for a symbol (NSE Cash) and returns the scrip dictionary."""
    try:
        # Defaulting to NSE Cash (nse_cm) for this example
        results = client.search_scrip(exchange_segment="nse_cm", symbol=symbol_str)
        if results and isinstance(results, list) and len(results) > 0:
            return results[0]  # Return best match
    except Exception as e:
        print(f"Error searching symbol: {e}")
    return None

def track_live_feed(client, scrip_info):
    """
    Subscribes to a token and updates the console line in-place.
    Blocks until user interrupts (Ctrl+C).
    """
    token = str(scrip_info['pSymbol'])
    exch = scrip_info['pExchSeg']
    symbol_name = scrip_info['pTrdSymbol']

    # Subscribe to the token
    client.subscribe(instrument_tokens=[{"instrument_token": token, "exchange_segment": exch}])
    
    print(f"\n--- Tracking {symbol_name} ---")
    print("Press 'Ctrl+C' to Stop Tracking and Enter Trade Params.\n")
    
    try:
        while True:
            with feed_lock:
                price = live_feed_cache.get(token, "Waiting...")
            
            # \r moves cursor to start of line, allowing overwrite
            sys.stdout.write(f"\rCurrent LTP: {price:<10}")
            sys.stdout.flush()
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        # Catch Ctrl+C to exit the tracking loop gracefully
        print("\n\n[Mode Switch] Enter Trade Parameters:")

# --- Main Application ---

def main():
    print("Initializing Kotak Neo API...")
    
    # 1. Initialize Client
    # Assuming valid keys are provided.
    client = NeoAPI(
        consumer_key=CONSUMER_KEY,
        environment='prod'
    )
    
    # Login Logic (Assumed pre-completed per prompt, but calling necessary methods)
    # In a real run, you would uncomment the following:
    client.totp_login(mobile_number=MOBILE_NUMBER, totp="" ,ucc=UNIQUE_CLIENT_CODE)
    client.totp_validate(mpin="YOUR_MPIN",)

    # Set Callbacks
    client.on_message = on_message
    client.on_error = on_error
    client.on_open = on_open
    client.on_close = on_close

    print("Client Ready. Starting Workflow.")

    while True:
        try:
            # 2. Input Symbol
            user_input = input("\nWhich symbol to enter? (or 'exit'): ").strip()
            if user_input.lower() == 'exit':
                sys.exit(0)
            
            if not user_input: 
                continue

            # 3. Search Symbol
            scrip = search_symbol(client, user_input)
            if not scrip:
                print("Symbol not found. Please try again.")
                continue

            # 4. Live Tracking (Blocking Loop)
            track_live_feed(client, scrip)

            # 5. Trade Parameters (Post-Interrupt)
            bs_input = input("B/S?: ").strip().upper()
            if bs_input not in ['B', 'S']:
                print("Invalid input. returning to start.")
                continue
                
            qty_input = input("Quantity?: ").strip()
            sl_input = input("SL points?: ").strip()
            tgt_input = input("Target points?: ").strip()
            
            confirm = input("Press Enter to Execute Trade (or type 'n' to cancel): ")
            if confirm.lower() == 'n':
                continue

            # 6. Async Execution
            # Create a new thread for this specific trade
            trade_thread = TradeManager(
                client=client,
                symbol_info=scrip,
                txn_type=bs_input,
                qty=qty_input,
                sl_points=sl_input,
                tgt_points=tgt_input
            )
            trade_thread.start()
            
            print(f"[Info] Order execution started in background. You may enter a new symbol now.")

        except Exception as e:
            print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
