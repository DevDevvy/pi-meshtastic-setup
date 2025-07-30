import os
import threading
import asyncio
import subprocess
import json
import time
from datetime import datetime
import pygame
from pygame.locals import *

import meshtastic
from meshtastic.ble_interface import BLEInterface
from bleak import BleakScanner

# Configuration
PROJECT_DIR = "/home/pi/meshtastic-badge"
CACHE_FILE = os.path.join(PROJECT_DIR, 'cache', 'messages.jsonl')
FONT_PATH = os.path.join(PROJECT_DIR, 'assets', 'pixel_font.ttf')
SCREEN_WIDTH, SCREEN_HEIGHT = 320, 240  # adjust if needed
BG_COLOR = (0, 0, 0)
FG_COLOR = (0, 255, 0)

# Globals
messages = []  # list of (timestamp, text)
ble_devices = set()
wifi_ssids = set()
scroll_offset = 0
lock = threading.Lock()

# Ensure directories exist
os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

# Load cached messages
if os.path.isfile(CACHE_FILE):
    with open(CACHE_FILE, 'r') as f:
        for line in f:
            try:
                msg = json.loads(line)
                messages.append((msg['time'], msg['text']))
            except Exception:
                pass

# Meshtastic callback

def on_receive(packet):
    try:
        text = packet['decoded']['text']
    except Exception:
        return
    timestamp = datetime.now().strftime('%H:%M:%S')
    with lock:
        messages.append((timestamp, text))
    # Cache to disk
    with open(CACHE_FILE, 'a') as f:
        json.dump({'time': timestamp, 'text': text}, f)
        f.write('\n')

# Thread: Meshtastic BLE listener with auto-discovery

def ble_listener():
    device_mac = None
    while not device_mac:
        print("🔍 Scanning for Meshtastic BLE devices...")
        devices = asyncio.run(BleakScanner.discover(timeout=5.0))
        for d in devices:
            if d.name and d.name.lower().startswith("meshtastic"):
                device_mac = d.address
                print(f"🔗 Found Meshtastic device: {d.name} at {device_mac}")
                break
        if not device_mac:
            time.sleep(5)
    interface = BLEInterface(device_mac)
    interface.onReceive = on_receive
    interface.loop_forever()

# Thread: BLE device scanner

def ble_scan_loop():
    while True:
        devices = asyncio.run(BleakScanner.discover(timeout=5.0))
        with lock:
            ble_devices.clear()
            for d in devices:
                ble_devices.add(d.address)
        time.sleep(10)

# Thread: Wi-Fi SSID scanner

def wifi_scan_loop():
    while True:
        try:
            result = subprocess.check_output(['iw', 'dev', 'wlan0', 'scan'], stderr=subprocess.DEVNULL).decode()
            ssids = set()
            for line in result.splitlines():
                line = line.strip()
                if line.startswith('SSID:'):
                    ssids.add(line.split('SSID:')[1].strip())
            with lock:
                wifi_ssids.clear()
                wifi_ssids.update(ssids)
        except Exception:
            pass
        time.sleep(15)

# Start background threads
threading.Thread(target=ble_listener, daemon=True).start()
threading.Thread(target=ble_scan_loop, daemon=True).start()
threading.Thread(target=wifi_scan_loop, daemon=True).start()

# Initialize Pygame
pygame.init()
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), FULLSCREEN)
pygame.mouse.set_visible(False)

# Load retro font or fallback
if os.path.isfile(FONT_PATH):
    font = pygame.font.Font(FONT_PATH, 16)
else:
    font = pygame.font.SysFont(None, 16)

# Main loop
y = 0
running = True
clock = pygame.time.Clock()
while running:
    for event in pygame.event.get():
        if event.type == QUIT:
            running = False
        elif event.type == KEYDOWN and event.key == K_ESCAPE:
            running = False
        elif event.type == MOUSEBUTTONDOWN:
            x, y = event.pos
            if y < SCREEN_HEIGHT * 0.2:
                scroll_offset = max(0, scroll_offset - 1)
            else:
                scroll_offset += 1

    screen.fill(BG_COLOR)
    with lock:
        # Render recent messages
        visible = messages[max(0, len(messages) - (SCREEN_HEIGHT // 20) - scroll_offset):len(messages) - scroll_offset]
        y = 0
        for ts, msg in visible:
            try:
                surf = font.render(f"{ts} {msg}", True, FG_COLOR)
                screen.blit(surf, (0, y))
            except Exception:
                pass
            y += 20
        # Render BLE & Wi-Fi info
        info = f"BLE:{len(ble_devices)} WiFi:{len(wifi_ssids)}"
        try:
            info_surf = font.render(info, True, FG_COLOR)
            screen.blit(info_surf, (0, SCREEN_HEIGHT - 20))
        except Exception:
            pass

    pygame.display.flip()
    clock.tick(10)

pygame.quit()