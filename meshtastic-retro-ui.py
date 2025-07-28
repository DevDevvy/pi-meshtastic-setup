#!/usr/bin/env python3
import curses
import json
import sqlite3
import threading
import time
import sys
import os
from datetime import datetime

# Import the correct interface based on your connection type
try:
    # For USB/Serial connection (most common)
    from meshtastic.serial_interface import SerialInterface
    CONNECTION_TYPE = "serial"
except ImportError:
    try:
        # For BLE connection
        from meshtastic.ble_interface import BLEInterface
        CONNECTION_TYPE = "ble"
    except ImportError:
        print("Error: Neither serial nor BLE interface available. Install meshtastic package.")
        sys.exit(1)

# --- CONFIG ---
LOG_JSON = True
LOG_SQLITE = True
LOG_FILE = "/home/pi/meshtastic.log"
DB_FILE = "/home/pi/meshtastic.db"

# Connection settings - adjust based on your setup
if CONNECTION_TYPE == "serial":
    # For USB connection, try common serial ports
    DEV_PATH = "/dev/ttyUSB0"  # or /dev/ttyACM0 for some devices
else:
    # For BLE connection
    NODE_ADDR = "00:11:22:33:44:55"  # replace with actual BLE address

# --- SETUP LOGGING ---
try:
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
except Exception as e:
    print(f"Error setting up logging: {e}")

# --- MESHTASTIC INTERFACE ---
messages = []
lock = threading.Lock()
iface = None
connection_status = "Disconnected"

def on_receive(packet, interface):
    """Callback for received packets"""
    global messages, connection_status
    
    try:
        # Handle different packet types
        if hasattr(packet, 'decoded') and packet.decoded:
            # Handle text messages
            if hasattr(packet.decoded, 'text') and packet.decoded.text:
                text = packet.decoded.text
                src_id = getattr(packet, 'fromId', getattr(packet, 'from', 'unknown'))
                src = str(src_id)
                
                timestamp = getattr(packet, 'rxTime', time.time())
                
                with lock:
                    messages.append((src, text, timestamp))
                
                # Log to JSON
                if LOG_JSON:
                    log_entry = {
                        'timestamp': timestamp,
                        'from': src,
                        'text': text,
                        'raw_packet': str(packet)
                    }
                    json_fh.write(json.dumps(log_entry) + "\n")
                    json_fh.flush()
                
                # Log to SQLite
                if LOG_SQLITE:
                    try:
                        conn.execute(
                            "INSERT INTO messages VALUES (?, ?, ?)",
                            (timestamp, src, text)
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"SQLite error: {e}")
        
    except Exception as e:
        print(f"Error in on_receive: {e}")

def on_connection(interface, topic=None):
    """Callback for connection events"""
    global connection_status
    connection_status = "Connected"

def on_lost_connection(interface, topic=None):
    """Callback for lost connection"""
    global connection_status
    connection_status = "Disconnected"

def serial_listener():
    """Initialize and maintain connection to Meshtastic device"""
    global iface, connection_status
    
    max_retries = 10
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            connection_status = "Connecting..."
            
            if CONNECTION_TYPE == "serial":
                # Try common serial ports
                ports_to_try = ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1", "/dev/serial0"]
                for port in ports_to_try:
                    if os.path.exists(port):
                        try:
                            iface = SerialInterface(devPath=port)
                            break
                        except Exception as e:
                            continue
                else:
                    raise Exception("No valid serial port found")
            else:
                # BLE connection
                iface = BLEInterface(address=NODE_ADDR)
            
            # Set up callbacks
            iface.onReceive = on_receive
            if hasattr(iface, 'onConnection'):
                iface.onConnection = on_connection
            if hasattr(iface, 'onLostConnection'):
                iface.onLostConnection = on_lost_connection
            
            connection_status = "Connected"
            
            # Keep the connection alive
            while True:
                time.sleep(1)
                if not iface or getattr(iface, '_closed', False):
                    break
                    
        except KeyboardInterrupt:
            break
        except Exception as e:
            connection_status = f"Error: {str(e)[:20]}"
            retry_count += 1
            if retry_count < max_retries:
                time.sleep(5)
            else:
                connection_status = "Failed"
                break

def handle_framebuffer_touch():
    """Handle touchscreen input from framebuffer device"""
    touch_device = None
    
    # Try to find touchscreen device
    for device_path in ['/dev/input/event0', '/dev/input/event1', '/dev/input/event2']:
        if os.path.exists(device_path):
            touch_device = device_path
            break
    
    if not touch_device:
        return None, None
    
    try:
        import struct
        
        with open(touch_device, 'rb') as f:
            # Read input event (16 bytes)
            data = f.read(16)
            if len(data) == 16:
                # Unpack input event structure
                tv_sec, tv_usec, type, code, value = struct.unpack('llHHi', data)
                
                if type == 3:  # EV_ABS (absolute positioning)
                    if code == 0:  # ABS_X
                        return 'x', value
                    elif code == 1:  # ABS_Y
                        return 'y', value
                elif type == 1 and code == 330:  # BTN_TOUCH
                    return 'touch', value
    except:
        pass
    
    return None, None

def run_ui(stdscr):
    """Main UI loop optimized for terminal/framebuffer"""
    # Disable cursor
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    
    # Set up colors (works in terminal)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_BLUE, -1)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)
    
    # Enable mouse support if available (works with some terminal touch drivers)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except:
        pass  # Mouse/touch not available
    
    # Start connection thread
    threading.Thread(target=serial_listener, daemon=True).start()
    
    offset = 0
    last_message_count = 0
    last_touch_y = None
    touch_coords = {'x': 0, 'y': 0, 'pressed': False}
    
    while True:
        try:
            h, w = stdscr.getmaxyx()
        except:
            # Fallback for problematic terminals
            h, w = 24, 80
        
        stdscr.erase()
        
        # Simple header (ASCII only for better terminal compatibility)
        header_char = "=" if not curses.has_colors() else "═"
        stdscr.addstr(0, 0, "+" + (header_char * (w-2)) + "+")
        
        status_line = f"| Meshtastic [{connection_status}] - {len(messages)} msgs |"
        if curses.has_colors():
            color = curses.color_pair(1) if connection_status == "Connected" else curses.color_pair(2)
            stdscr.addstr(1, 0, status_line.ljust(w-1) + "|", color)
        else:
            stdscr.addstr(1, 0, status_line.ljust(w-1) + "|")
        
        # Message area
        with lock:
            total_messages = len(messages)
            if total_messages != last_message_count:
                # Auto-scroll to bottom when new messages arrive
                offset = max(0, total_messages - (h-6))
                last_message_count = total_messages
            
            offset = min(offset, max(total_messages - (h-6), 0))
            visible = messages[offset : offset + max(h-6, 0)]
        
        for i, (src, txt, ts) in enumerate(visible):
            try:
                time_str = datetime.fromtimestamp(ts).strftime("%H:%M")
            except:
                time_str = "??:??"
            
            line = f"[{time_str}] {src[:8]}: {txt}"
            try:
                if curses.has_colors():
                    stdscr.addstr(2+i, 1, line[:w-2], curses.color_pair(1))
                else:
                    stdscr.addstr(2+i, 1, line[:w-2])
            except curses.error:
                pass  # Ignore if line doesn't fit
        
        # Show status if no messages
        if not messages:
            msg = "Waiting for messages..."
            try:
                if curses.has_colors():
                    stdscr.addstr(3, 1, msg[:w-2], curses.color_pair(3))
                else:
                    stdscr.addstr(3, 1, msg[:w-2])
            except curses.error:
                pass
        
        # Footer with simple buttons
        footer_y = h-4
        stdscr.addstr(footer_y, 0, "+" + (header_char * (w-2)) + "+")
        
        # Touch/key instructions
        if curses.has_colors():
            stdscr.addstr(footer_y+1, 0, "[ SEND ]".center(w//2), curses.color_pair(5))
            stdscr.addstr(footer_y+1, w//2, "[ RECONNECT ]".center(w//2), curses.color_pair(5))
        else:
            stdscr.addstr(footer_y+1, 0, "[ SEND ]".center(w//2))
            stdscr.addstr(footer_y+1, w//2, "[ RECONNECT ]".center(w//2))
        
        stdscr.addstr(footer_y+2, 0, "Touch buttons or: S=Send, R=Reconnect, Q=Quit".center(w))
        
        try:
            stdscr.refresh()
        except curses.error:
            pass
        
        # Input handling
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1
        
        # Handle mouse/touch events (if supported)
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                
                # Touch on send button area
                if my == footer_y+1 and mx < w//2:
                    ch = ord('s')
                
                # Touch on reconnect button area
                elif my == footer_y+1 and mx >= w//2:
                    ch = ord('r')
                
                # Scroll in message area
                elif 2 <= my < footer_y:
                    if bstate & curses.BUTTON1_PRESSED:
                        last_touch_y = my
                    elif bstate & curses.BUTTON1_RELEASED and last_touch_y is not None:
                        if my > last_touch_y:  # Scroll down
                            with lock:
                                bottom = max(0, len(messages) - (h-6))
                            offset = min(bottom, offset + 3)
                        elif my < last_touch_y:  # Scroll up
                            offset = max(0, offset - 3)
                        last_touch_y = None
                
            except curses.error:
                pass
        
        # Keyboard controls
        elif ch == curses.KEY_UP or ch == ord('k'):
            offset = max(0, offset-1)
        elif ch == curses.KEY_DOWN or ch == ord('j'):
            with lock:
                bottom = max(0, len(messages) - (h-6))
            offset = min(bottom, offset + 1)
        elif ch in (ord('s'), ord('S')):
            if iface and connection_status == "Connected":
                try:
                    curses.echo()
                    stdscr.addstr(h-1, 0, "Send: ")
                    stdscr.clrtoeol()
                    txt = stdscr.getstr(h-1, 6, w-8).decode().strip()
                    curses.noecho()
                    
                    if txt:
                        iface.sendText(txt)
                        with lock:
                            messages.append(("You", txt, time.time()))
                        stdscr.addstr(h-1, 0, "Message sent!")
                        stdscr.clrtoeol()
                        stdscr.refresh()
                        time.sleep(1)
                except Exception as e:
                    curses.noecho()
                    stdscr.addstr(h-1, 0, f"Send failed: {str(e)[:w-15]}")
                    stdscr.clrtoeol()
                    stdscr.refresh()
                    time.sleep(2)
            else:
                stdscr.addstr(h-1, 0, "Not connected!")
                stdscr.clrtoeol()
                stdscr.refresh()
                time.sleep(1)
        elif ch in (ord('r'), ord('R')):
            # Restart connection
            if iface:
                try:
                    iface.close()
                except:
                    pass
            threading.Thread(target=serial_listener, daemon=True).start()
        elif ch in (ord('q'), ord('Q'), 27):  # Q or ESC to quit
            break
        
        time.sleep(0.05)  # Small delay to prevent excessive CPU usage

def main():
    """Main function with proper terminal setup"""
    # Ensure we're running in a proper terminal environment
    if not os.isatty(sys.stdin.fileno()):
        print("Error: This program must be run in a terminal")
        sys.exit(1)
    
    # Set terminal environment variables for better compatibility
    os.environ.setdefault('TERM', 'linux')
    os.environ.setdefault('TERMINFO', '/etc/terminfo:/lib/terminfo:/usr/share/terminfo')
    
    try:
        # Initialize curses
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Cleanup
        if 'iface' in globals() and iface:
            try:
                iface.close()
            except:
                pass
        if LOG_JSON and 'json_fh' in globals():
            try:
                json_fh.close()
            except:
                pass

if __name__ == "__main__":
    main()