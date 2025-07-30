#!/usr/bin/env python3
import curses
import json
import sqlite3
import threading
import time
import sys
from meshtastic.serial_interface import SerialInterface

# --- CONFIG ---
LOG_JSON = True                   # append raw JSON to log file
LOG_SQLITE = True                 # insert messages into SQLite
LOG_FILE = "/home/rangerdan/meshtastic.log"
DB_FILE  = "/home/rangerdan/meshtastic.db"
DEV_PATH = "/dev/rfcomm0"
NODE_ADDR = "00:11:22:33:44:55"  # replace with your node's BLE address

# --- SETUP LOGGING ---
json_fh = None
conn = None

if LOG_JSON:
    try:
        json_fh = open(LOG_FILE, "a", encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not open log file: {e}")

if LOG_SQLITE:
    try:
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
        print(f"Warning: Could not setup SQLite: {e}")

# --- MESHTASTIC CALLBACK ---
messages = []
lock = threading.Lock()
iface = None
interface_ready = threading.Event()
connection_status = "Connecting..."

def on_receive(packet, interface):
    """Callback for received packets"""
    global messages, json_fh, conn
    
    try:
        # Handle different packet types
        if hasattr(packet, 'decoded') and packet.decoded:
            # Text message
            if hasattr(packet.decoded, 'text') and packet.decoded.text:
                text = packet.decoded.text
                src_id = getattr(packet, 'fromId', getattr(packet, 'from', 'unknown'))
                src = str(src_id)
                
                # Get timestamp
                ts = getattr(packet, 'rxTime', time.time())
                
                with lock:
                    messages.append((src, text))
                
                # Log to JSON file
                if LOG_JSON and json_fh:
                    try:
                        json_fh.write(json.dumps({
                            'timestamp': ts,
                            'from': src,
                            'text': text,
                            'raw_packet': str(packet)
                        }) + "\n")
                        json_fh.flush()
                    except Exception as e:
                        pass  # Silent fail for logging
                
                # Log to SQLite
                if LOG_SQLITE and conn:
                    try:
                        conn.execute(
                            "INSERT INTO messages VALUES (?, ?, ?)",
                            (ts, src, text)
                        )
                        conn.commit()
                    except Exception as e:
                        pass  # Silent fail for logging
                        
    except Exception as e:
        # Add error message to display
        with lock:
            messages.append(("ERROR", f"Packet processing error: {str(e)}"))

def on_connection(interface, topic=None):
    """Callback for connection events"""
    global connection_status, interface_ready
    connection_status = "Connected"
    interface_ready.set()

def on_lost_connection(interface, topic=None):
    """Callback for lost connection"""
    global connection_status, interface_ready
    connection_status = "Disconnected"
    interface_ready.clear()

def check_device_exists():
    """Check if the device file exists and is accessible"""
    import os
    import stat
    
    if not os.path.exists(DEV_PATH):
        return False, f"Device {DEV_PATH} does not exist"
    
    try:
        st = os.stat(DEV_PATH)
        if not stat.S_ISCHR(st.st_mode):
            return False, f"Device {DEV_PATH} is not a character device"
        return True, "Device exists and is accessible"
    except Exception as e:
        return False, f"Cannot access {DEV_PATH}: {str(e)}"

def serial_listener():
    """Initialize and manage the Meshtastic interface"""
    global iface, connection_status
    
    # 1) Device check
    exists, msg = check_device_exists()
    if not exists:
        connection_status = f"Device Error: {msg}"
        with lock:
            messages.extend([
                ("SYSTEM", f"Device check failed: {msg}"),
                ("SYSTEM", "Make sure you ran 'sudo rfcomm bind' or paired via bluetoothctl"),
                ("SYSTEM", f"Expected device: {DEV_PATH}")
            ])
        return
    
    # 2) Try to open
    try:
        connection_status = f"Connecting to {DEV_PATH}"
        with lock:
            messages.append(("SYSTEM", f"Attempting to connect to {DEV_PATH}"))
        
        iface = SerialInterface(devPath=DEV_PATH)
        iface.onReceive = on_receive
        iface.onConnection = on_connection
        iface.onLostConnection = on_lost_connection
        
        # 3) Wait for link-up
        for i in range(20):
            time.sleep(0.5)
            connection_status = f"Waiting for connection... ({i+1}/20)"
            if getattr(iface, "isConnected", False) or getattr(iface.stream, "is_open", False):
                break
        
        if iface is None or not (iface.isConnected or iface.stream.is_open):
            connection_status = "Connection failed"
            with lock:
                messages.extend([
                    ("SYSTEM", "Unable to connect after timeout"),
                    ("SYSTEM", "Run `python3 test_connection.py` for diagnostics"),
                ])
            return
        
        # 4) Success!
        connection_status = "Connected"
        interface_ready.set()
        with lock:
            messages.append(("SYSTEM", "Successfully connected"))
        
        # 5) Optional: show node info
        try:
            info = iface.getMyNodeInfo()
            with lock:
                messages.append(("SYSTEM", f"Node info: {str(info)[:60]}…"))
        except Exception:
            pass
        
        # 6) Keep alive
        while True:
            time.sleep(1)
            if not (iface.isConnected or iface.stream.is_open):
                connection_status = "Disconnected"
                interface_ready.clear()
                with lock:
                    messages.append(("SYSTEM", "Lost connection"))
                break

    except Exception as e:
        connection_status = f"Fatal Error: {e}"
        with lock:
            messages.append(("SYSTEM", f"Fatal connection error: {e}"))
            messages.append(("SYSTEM", "Run test_connection.py for diagnostics"))

def send_message(text):
    """Send a text message via Meshtastic"""
    global iface
    
    if not iface or not interface_ready.is_set():
        return False, "Interface not ready"
    
    try:
        iface.sendText(text)
        with lock:
            messages.append(("You", text))
        return True, "Message sent"
    except Exception as e:
        return False, f"Send error: {str(e)}"

def run_ui(stdscr):
    """Main UI loop"""
    global iface, connection_status
    try:
        # Bind directly to /dev/rfcomm0
        iface = SerialInterface(devPath=DEV_PATH)
        # Attach callbacks just like before
        iface.onReceive(on_receive)
        iface.onConnection(on_connection)
        iface.onLostConnection(on_lost_connection)

        connection_status = "Connecting…"
        # Wait up to 5 s for iface to fire onConnection
        if not interface_ready.wait(timeout=5):
            connection_status = "Connection timeout"
        else:
            connection_status = "Connected"
    except Exception as e:
        connection_status = f"Connection error: {e}"
    
    offset = 0
    status_message = ""
    status_color = 1
    
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        
        # Header with connection status
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + ("═"*(w-2)) + "╗")
        header = f"║ RetroMeshtastic Badge — {connection_status} ║"
        stdscr.addstr(1, 0, header.ljust(w-1) + "║")
        stdscr.attroff(curses.color_pair(1))
        
        # Message window
        with lock:
            msg_count = len(messages)
            max_offset = max(0, msg_count - (h-5))
            offset = min(offset, max_offset)
            visible = messages[offset : offset + (h-5)]
        
        for i, (src, txt) in enumerate(visible):
            color = curses.color_pair(3) if src == "You" else curses.color_pair(1)
            if src in ("ERROR", "SYSTEM"):
                color = curses.color_pair(2)
            
            line = f"{src[:12]}: {txt}"
            stdscr.addstr(2+i, 1, line[:w-2], color)
        
        # Status line
        if status_message:
            stdscr.addstr(h-3, 1, status_message[:w-2], curses.color_pair(status_color))
        
        # Footer
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(h-2, 0, "╚" + ("═"*(w-2)) + "╝")
        footer = "Press 's' to send | ↑/↓ scroll | Ctrl-C exit"
        stdscr.addstr(h-1, 0, footer.ljust(w-1))
        stdscr.attroff(curses.color_pair(1))
        
        stdscr.refresh()
        
        # Handle input with timeout
        stdscr.timeout(100)  # 100ms timeout
        try:
            ch = stdscr.getch()
        except curses.error:
            continue
            
        if ch == -1:  # Timeout
            continue
        elif ch == curses.KEY_UP:
            offset = max(0, offset-1)
        elif ch == curses.KEY_DOWN:
            offset = min(max_offset, offset + 1)
        elif ch in (ord('s'), ord('S')):
            # Clear status message
            status_message = ""
            
            # Check if interface is ready
            if not interface_ready.is_set():
                status_message = "Interface not ready - cannot send"
                status_color = 2
                continue
            
            # Get input for message
            stdscr.timeout(-1)  # Blocking mode for input
            curses.echo()
            curses.curs_set(1)
            
            try:
                stdscr.addstr(h-1, 0, "Send: ".ljust(w-1))
                stdscr.refresh()
                
                # Get the message text
                input_text = stdscr.getstr(h-1, 6, min(200, w-8))
                message_text = input_text.decode('utf-8').strip()
                
                if message_text:
                    success, result = send_message(message_text)
                    if success:
                        status_message = "Message sent successfully"
                        status_color = 1
                    else:
                        status_message = result
                        status_color = 2
                else:
                    status_message = "Empty message not sent"
                    status_color = 3
                    
            except Exception as e:
                status_message = f"Input error: {str(e)}"
                status_color = 2
            finally:
                curses.noecho()
                curses.curs_set(0)
                stdscr.timeout(100)  # Back to non-blocking
        elif ch == 3:  # Ctrl-C
            break

def cleanup():
    """Clean up resources"""
    global iface, json_fh, conn
    
    if iface:
        try:
            iface.close()
        except:
            pass
    
    if json_fh:
        try:
            json_fh.close()
        except:
            pass
    
    if conn:
        try:
            conn.close()
        except:
            pass

if __name__ == "__main__":
    try:
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("Meshtastic interface closed.")