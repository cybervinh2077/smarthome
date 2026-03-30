#!/usr/bin/env python3
"""
Jetson Nano AI Dashboard
MQTT subscriber + Ollama LLM + curses TUI
"""

import json
import threading
import time
from datetime import datetime

import curses
import ollama
import paho.mqtt.client as mqtt

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883
ROOM_TOPIC  = "home/room1"
LLM_MODEL   = "phi3:mini"

SYSTEM_PROMPT = """Bạn là trợ lý AI nhà thông minh. Nhiệm vụ:
1. Trả lời câu hỏi về cảm biến phòng ngắn gọn bằng tiếng Việt.
2. Tư vấn bật/tắt điều hòa theo rule:
   - Nhiệt > 28C + có người → bật 26C
   - Không có người > 15 phút → tắt
   - Độ ẩm > 70% → thông gió
3. Nếu người dùng ra lệnh (bật/tắt/timer điều hòa), trả về JSON:
   {{"action": "bat_ac|tat_ac|timer_ac", "temp": 26, "duration": 60}}
   kèm giải thích ngắn.

Dữ liệu hiện tại: Nhiệt={temp}C, Độ ẩm={hum}%, Motion={motion}, AC={ac}"""


# ── Dashboard Class ───────────────────────────────────────────────────────────
class JetsonAIDashboard:
    def __init__(self):
        self.temp      = 0.0
        self.hum       = 0.0
        self.motion    = "OFF"
        self.ac_status = "OFF"
        self.lock      = threading.Lock()

        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ── MQTT ─────────────────────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(f"{ROOM_TOPIC}/sensor/+")
            client.subscribe(f"{ROOM_TOPIC}/ac/status")
        else:
            print(f"[MQTT] Connect failed, rc={rc}")

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode()
        with self.lock:
            if "temp"     in topic: self.temp      = float(payload)
            elif "hum"    in topic: self.hum        = float(payload)
            elif "motion" in topic: self.motion     = "ON" if payload == "1" else "OFF"
            elif "ac/status" in topic: self.ac_status = payload.upper()

    # ── AI ───────────────────────────────────────────────────────────────────
    def ai_query(self, user_query: str) -> str:
        with self.lock:
            sys_prompt = SYSTEM_PROMPT.format(
                temp=self.temp, hum=self.hum,
                motion=self.motion, ac=self.ac_status
            )
        try:
            resp  = ollama.chat(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user",   "content": user_query},
                ],
                options={"num_predict": 120, "temperature": 0.2},
            )
            reply = resp["message"]["content"].strip()

            # Parse JSON command và publish về ESP32 nếu có
            if "{" in reply and "}" in reply:
                try:
                    start = reply.index("{")
                    end   = reply.rindex("}") + 1
                    cmd   = json.loads(reply[start:end])
                    self.client.publish(f"{ROOM_TOPIC}/ac/command", json.dumps(cmd))
                    reply = reply[:start] + "[CMD→ESP32 SENT] " + reply[end:]
                except (ValueError, json.JSONDecodeError):
                    pass

            return reply
        except Exception as e:
            return f"[AI ERROR] {e}"

    # ── TUI ──────────────────────────────────────────────────────────────────
    def _draw(self, stdscr, ai_reply: str):
        stdscr.clear()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN,   curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_CYAN,    curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_YELLOW,  curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_RED,     curses.COLOR_BLACK)

        H, W = stdscr.getmaxyx()

        # Header
        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(0, 0, "=" * (W - 1))
        stdscr.addstr(1, 2, f"JETSON NANO AI DASHBOARD  |  {LLM_MODEL}  |  {datetime.now().strftime('%H:%M:%S')}")
        stdscr.addstr(2, 0, "=" * (W - 1))
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        # Sensor data
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(4, 2, "[ SENSOR DATA ]")
        stdscr.attroff(curses.color_pair(2))

        with self.lock:
            t, h, m, ac = self.temp, self.hum, self.motion, self.ac_status

        stdscr.addstr(5, 4, f"Temperature : {t:.1f} C")
        stdscr.addstr(6, 4, f"Humidity    : {h:.0f} %")

        stdscr.attron(curses.color_pair(5) if m == "ON" else curses.color_pair(1))
        stdscr.addstr(7, 4, f"Motion      : {m}")
        stdscr.attroff(curses.color_pair(5) if m == "ON" else curses.color_pair(1))

        stdscr.attron(curses.color_pair(2) if ac == "ON" else curses.color_pair(3))
        stdscr.addstr(8, 4, f"AC Status   : {ac}")
        stdscr.attroff(curses.color_pair(2) if ac == "ON" else curses.color_pair(3))

        # AI response
        stdscr.addstr(10, 0, "-" * (W - 1))
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        stdscr.addstr(11, 2, "[ AI RESPONSE ]")
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)

        max_len = W - 6
        lines   = []
        for chunk in (ai_reply or "Chưa có câu trả lời. Nhấn phím bên dưới.").split("\n"):
            while len(chunk) > max_len:
                lines.append(chunk[:max_len])
                chunk = chunk[max_len:]
            lines.append(chunk)
        for i, line in enumerate(lines[:6]):
            stdscr.addstr(12 + i, 4, line[:W - 5])

        # Commands
        stdscr.addstr(19, 0, "-" * (W - 1))
        stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(20, 2, "[ COMMANDS ]")
        stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

        cmds = [
            "[t] Nhiệt độ phòng?",
            "[h] Độ ẩm OK không?",
            "[a] Tình trạng AC?",
            "[c] Tư vấn bật/tắt AC?",
            "[b] Bật điều hòa 26°C",
            "[x] Tắt điều hòa",
            "[q] Thoát",
        ]
        for i, cmd in enumerate(cmds):
            stdscr.addstr(21 + i, 4, cmd)

        stdscr.refresh()

    def _run_tui(self, stdscr):
        stdscr.nodelay(True)
        ai_reply  = ""
        ai_thread = None

        def ask(query):
            nonlocal ai_reply
            ai_reply = "Đang suy nghĩ..."
            ai_reply = self.ai_query(query)

        KEY_MAP = {
            ord('t'): "Nhiệt độ phòng hiện tại?",
            ord('h'): "Độ ẩm phòng thế nào? Có cần thông gió?",
            ord('a'): "Tình trạng máy lạnh hiện tại?",
            ord('c'): "Dựa nhiệt độ + motion, tư vấn bật/tắt điều hòa.",
            ord('b'): "Bật điều hòa 26 độ ngay.",
            ord('x'): "Tắt điều hòa ngay.",
        }

        while True:
            self._draw(stdscr, ai_reply)
            key  = stdscr.getch()
            busy = ai_thread is not None and ai_thread.is_alive()

            if key == ord('q'):
                break
            elif key in KEY_MAP and not busy:
                ai_thread = threading.Thread(
                    target=ask, args=(KEY_MAP[key],), daemon=True
                )
                ai_thread.start()

            time.sleep(0.1)

    # ── Entry ─────────────────────────────────────────────────────────────────
    def run(self):
        self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
        threading.Thread(target=self.client.loop_forever, daemon=True).start()
        curses.wrapper(self._run_tui)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting Jetson AI Dashboard...")
    print(f"Model : {LLM_MODEL}")
    print("Requires: ollama serve + mosquitto running")
    time.sleep(1)
    JetsonAIDashboard().run()
