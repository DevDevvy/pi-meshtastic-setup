#!/usr/bin/env python3
import curses
import json
import sqlite3
import threading
import time
import meshtastic.serial_interface as mserial

# --- CONFIG ---
LOG_JSON = True                   # append raw JSON to log file
LOG_SQLITE = True                 # insert messages into SQLite
LOG_FILE = "/home/pi/meshtastic.log"
DB_FILE  = "/home/pi/meshtastic.db"
DEV_PATH = "/dev/rfcomm0"

# --- SETUP LOGGING ---
if LOG_JSON:
    json_fh = open(LOG_FILE, "a", encoding="utf-8")

if LOG_SQLITE:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        ts   REAL,
        src  TEXT,
        text TEXT
      )
    """)
    conn.commit()

# --- MESHTASTIC CALLBACK ---
messages = []
messages_lock = threading.Lock()

def on_receive(pkt, iface):
    """Fixed callback function for receiving messages"""
    try:
        # Debug: log the raw packet to see what we're getting
        if LOG_JSON:
            json_fh.write(json.dumps(pkt, default=str) + "\n")
            json_fh.flush()
        
        # Try multiple ways to extract the message text
        text = None
        src = "unknown"
        
        # Method 1: Check if packet has decoded text directly
        if hasattr(pkt, 'decoded') and hasattr(pkt.decoded, 'text'):
            text = pkt.decoded.text
        # Method 2: Check decoded dict
        elif isinstance(pkt, dict):
            decoded = pkt.get("decoded", {})
            if isinstance(decoded, dict):
                text = decoded.get("text")
            elif hasattr(decoded, 'text'):
                text = decoded.text
        
        # Extract source information
        if hasattr(pkt, 'fromId'):
            src = str(pkt.fromId)
        elif isinstance(pkt, dict) and 'from' in pkt:
            src = str(pkt['from'])
        elif hasattr(pkt, 'from'):
            src = str(getattr(pkt, 'from'))
        elif isinstance(pkt, dict):
            from_info = pkt.get("from", {})
            if isinstance(from_info, dict):
                src = from_info.get("userAlias", from_info.get("id", "unknown"))
            else:
                src = str(from_info)
        
        if text:
            # Get timestamp
            ts = time.time()  # Default to current time
            if hasattr(pkt, 'rxTime'):
                ts = pkt.rxTime
            elif isinstance(pkt, dict):
                ts = pkt.get("timestamp", time.time())
                if ts > 1e12:  # Convert from milliseconds if needed
                    ts = ts / 1000
            
            # Add to messages with thread safety
            with messages_lock:
                messages.append((src[:10], text))
            
            # Log to SQLite
            if LOG_SQLITE:
                try:
                    conn.execute(
                        "INSERT INTO messages VALUES (?, ?, ?)",
                        (ts, src, text)
                    )
                    conn.commit()
                except Exception as e:
                    print(f"SQLite error: {e}")
    
    except Exception as e:
        print(f"Error in on_receive: {e}")
        # Still log the raw packet for debugging
        if LOG_JSON:
            json_fh.write(f"ERROR: {json.dumps(pkt, default=str)}\n")
            json_fh.flush()

# --- UI ---
def run_ui(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    # Colors: green on black for retro feel (keeping original style)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)      # For errors
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)   # For status

    # Initialize connection
    iface = None
    connection_status = "Connecting..."
    
    def connect_device():
        nonlocal iface, connection_status
        try:
            connection_status = "Connecting..."
            iface = mserial.SerialInterface(devPath=DEV_PATH)
            # FIXED: Correct way to set callback
            iface.onReceive = on_receive
            connection_status = "Connected"
        except Exception as e:
            connection_status = f"Error: {str(e)[:20]}"
            iface = None
    
    # Start connection in background
    threading.Thread(target=connect_device, daemon=True).start()
    
    offset = 0
    last_touch_y = None
    touch_start_time = 0
    
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # Header & Footer (keeping original green styling)
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + ("═"*(w-2)) + "╗")
        
        # Show connection status in header
        header_text = f" RetroMeshtastic Badge [{connection_status}] — Touch or ↑/↓ to scroll "
        stdscr.addstr(1, 0, "║" + header_text.ljust(w-2) + "║")
        
        stdscr.addstr(h-3, 0, "╚" + ("═"*(w-2)) + "╝")
        
        # Touch buttons
        send_btn = "[SEND]"
        reconnect_btn = "[RECONNECT]"
        btn_y = h-2
        
        stdscr.addstr(btn_y, 2, send_btn)
        stdscr.addstr(btn_y, w-len(reconnect_btn)-2, reconnect_btn)
        stdscr.addstr(h-1, 0, "Press 's' to send | 'r' to reconnect | Touch buttons or screen".ljust(w-1))
        stdscr.attroff(curses.color_pair(1))

        # Message window inside box (keeping original logic but with thread safety)
        with messages_lock:
            total_messages = len(messages)
            # Auto-scroll to show newest messages
            max_visible = h - 5  # Adjusted for button space
            if total_messages > max_visible:
                if offset == 0:  # If at top, show latest messages
                    offset = max(0, total_messages - max_visible)
            
            offset = min(offset, max(0, total_messages - max_visible))
            offset = max(0, offset)
            
            visible_messages = messages[offset:offset + max_visible] if messages else []
        
        # Display messages
        for i, (src, txt) in enumerate(visible_messages):
            line = f"{src}: {txt}"
            try:
                stdscr.addstr(2+i, 1, line[:w-2], curses.color_pair(1))
            except curses.error:
                pass  # Ignore if line doesn't fit
        
        # Show status if no messages
        if not messages:
            status_msg = "Waiting for messages..." if connection_status == "Connected" else "Check connection..."
            try:
                stdscr.addstr(3, 1, status_msg[:w-2], curses.color_pair(3))
            except curses.error:
                pass

        stdscr.refresh()
        curses.napms(50)

        # Input handling (enhanced with better touch support)
        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if ch == curses.KEY_UP:
            offset = max(0, offset-1)
        elif ch == curses.KEY_DOWN:
            with messages_lock:
                max_offset = max(0, len(messages)-(h-5))
            offset = min(max_offset, offset+1)
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                
                # Touch on SEND button
                if my == btn_y and 2 <= mx <= 2+len(send_btn):
                    ch = ord('s')  # Trigger send
                
                # Touch on RECONNECT button  
                elif my == btn_y and (w-len(reconnect_btn)-2) <= mx <= w-2:
                    ch = ord('r')  # Trigger reconnect
                
                # Touch scrolling in message area
                elif 2 <= my <= h-4:
                    current_time = time.time()
                    
                    if bstate & curses.BUTTON1_PRESSED:
                        last_touch_y = my
                        touch_start_time = current_time
                    
                    elif bstate & curses.BUTTON1_RELEASED and last_touch_y is not None:
                        touch_duration = current_time - touch_start_time
                        touch_distance = my - last_touch_y
                        
                        # Quick swipe detection
                        if touch_duration < 0.5 and abs(touch_distance) > 0:
                            scroll_amount = max(1, abs(touch_distance))
                            
                            if touch_distance > 0:  # Swiped down = scroll down
                                with messages_lock:
                                    max_offset = max(0, len(messages)-(h-5))
                                offset = min(max_offset, offset + scroll_amount)
                            else:  # Swiped up = scroll up
                                offset = max(0, offset - scroll_amount)
                        
                        last_touch_y = None
                
            except curses.error:
                pass
        
        elif ch in (ord('s'), ord('S')):
            if iface and connection_status == "Connected":
                try:
                    curses.echo()
                    stdscr.addstr(h-1, 0, "Send: ".ljust(w-1))
                    txt = stdscr.getstr(h-1, 6, w-8).decode().strip()
                    curses.noecho()
                    if txt:
                        iface.sendText(txt)
                        # Add to local messages immediately
                        with messages_lock:
                            messages.append(("You", txt))
                        stdscr.addstr(h-1, 0, "Message sent!".ljust(w-1))
                        stdscr.refresh()
                        time.sleep(1)
                except Exception as e:
                    curses.noecho()
                    stdscr.addstr(h-1, 0, f"Send failed: {str(e)[:30]}".ljust(w-1))
                    stdscr.refresh()
                    time.sleep(2)
            else:
                stdscr.addstr(h-1, 0, "Not connected! Press 'r' to reconnect.".ljust(w-1))
                stdscr.refresh()
                time.sleep(1)
        
        elif ch in (ord('r'), ord('R')):
            # Reconnect
            if iface:
                try:
                    iface.close()
                except:
                    pass
            threading.Thread(target=connect_device, daemon=True).start()
        
        elif ch == 27 or ch == ord('q'):  # ESC or Q to quit
            break

if __name__ == "__main__":
    try:
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        # Cleanup
        if LOG_JSON:
            try:
                json_fh.close()
            except:
                pass