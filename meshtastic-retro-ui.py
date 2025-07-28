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
    # Set up terminal properly
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    
    # Try to enable mouse, but don't fail if it doesn't work
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except:
        pass

    # Colors: green on black for retro feel (keeping original style)
    if curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)      # For errors
        curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)   # For status
    else:
        # Fallback if no colors available
        pass

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
        try:
            h, w = stdscr.getmaxyx()
            # Minimum size check
            if h < 10 or w < 30:
                stdscr.clear()
                stdscr.addstr(0, 0, "Terminal too small!")
                stdscr.refresh()
                time.sleep(1)
                continue
        except:
            # Fallback dimensions
            h, w = 24, 80
        
        try:
            stdscr.erase()
        except curses.error:
            continue

        # Safe string output function
        def safe_addstr(y, x, text, attr=0):
            try:
                # Ensure we don't write past terminal boundaries
                if y >= 0 and y < h and x >= 0 and x < w:
                    # Convert to ASCII and truncate if needed
                    safe_text = str(text).encode('ascii', 'replace').decode('ascii')
                    max_len = w - x - 1
                    if len(safe_text) > max_len:
                        safe_text = safe_text[:max_len]
                    if attr and curses.has_colors():
                        stdscr.addstr(y, x, safe_text, attr)
                    else:
                        stdscr.addstr(y, x, safe_text)
            except curses.error:
                pass  # Silently ignore if we can't write

        # Header & Footer (keeping original green styling) - using safe output
        color_attr = curses.color_pair(1) if curses.has_colors() else 0
        
        # Use simpler characters if Unicode fails
        try:
            safe_addstr(0, 0, "╔" + ("═"*(w-2)) + "╗", color_attr)
        except:
            safe_addstr(0, 0, "+" + ("-"*(w-2)) + "+", color_attr)
        
        # Show connection status in header
        header_text = f" RetroMeshtastic Badge [{connection_status}] — Touch or ↑/↓ to scroll "
        header_line = "║" + header_text[:w-2].ljust(w-2) + "║"
        try:
            safe_addstr(1, 0, header_line, color_attr)
        except:
            safe_addstr(1, 0, "|" + header_text[:w-2].ljust(w-2) + "|", color_attr)
        
        try:
            safe_addstr(h-3, 0, "╚" + ("═"*(w-2)) + "╝", color_attr)
        except:
            safe_addstr(h-3, 0, "+" + ("-"*(w-2)) + "+", color_attr)
        
        # Touch buttons
        send_btn = "[SEND]"
        reconnect_btn = "[RECONNECT]"
        btn_y = h-2
        
        safe_addstr(btn_y, 2, send_btn, color_attr)
        safe_addstr(btn_y, w-len(reconnect_btn)-2, reconnect_btn, color_attr)
        
        footer_text = "Press 's' to send | 'r' to reconnect | Touch buttons or screen"
        safe_addstr(h-1, 0, footer_text[:w-1].ljust(w-1), color_attr)

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
        
        # Display messages with bounds checking
        for i, (src, txt) in enumerate(visible_messages):
            if 2 + i < h - 3:  # Make sure we don't write past the box
                line = f"{src}: {txt}"
                safe_addstr(2+i, 1, line, color_attr)
        
        # Show status if no messages with bounds checking
        if not messages:
            status_msg = "Waiting for messages..." if connection_status == "Connected" else "Check connection..."
            if 3 < h - 3:  # Make sure we have room to display
                status_color = curses.color_pair(3) if curses.has_colors() else 0
                safe_addstr(3, 1, status_msg, status_color)

        try:
            stdscr.refresh()
        except curses.error:
            pass
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
                    safe_addstr(h-1, 0, "Send: ".ljust(w-1))
                    stdscr.refresh()
                    
                    # Get input safely
                    try:
                        txt = stdscr.getstr(h-1, 6, w-8).decode('ascii', 'replace').strip()
                    except:
                        txt = ""
                    
                    curses.noecho()
                    if txt:
                        iface.sendText(txt)
                        # Add to local messages immediately
                        with messages_lock:
                            messages.append(("You", txt))
                        safe_addstr(h-1, 0, "Message sent!".ljust(w-1))
                        stdscr.refresh()
                        time.sleep(1)
                except Exception as e:
                    curses.noecho()
                    safe_addstr(h-1, 0, f"Send failed: {str(e)[:20]}".ljust(w-1))
                    stdscr.refresh()
                    time.sleep(2)
            else:
                safe_addstr(h-1, 0, "Not connected! Press 'r' to reconnect.".ljust(w-1))
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
        # Check terminal size before starting
        import os
        try:
            rows, cols = os.popen('stty size', 'r').read().split()
            if int(cols) < 40 or int(rows) < 10:
                print("Warning: Terminal too small. Need at least 40x10 characters.")
        except:
            pass  # Ignore if we can't check size
        
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Error starting interface: {e}")
        print("Make sure you're running in a proper terminal environment.")
    finally:
        # Cleanup
        if LOG_JSON:
            try:
                json_fh.close()
            except:
                pass