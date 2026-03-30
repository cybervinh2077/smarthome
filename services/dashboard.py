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

# ── IR Topics ─────────────────────────────────────────────────────────────────
TOPIC_IR_LEARN_ON  = f"{ROOM_TOPIC}/ir/learn_on"
TOPIC_IR_LEARN_OFF = f"{ROOM_TOPIC}/ir/learn_off"
TOPIC_IR_SEND_ON   = f"{ROOM_TOPIC}/ir/send_on"
TOPIC_IR_SEND_OFF  = f"{ROOM_TOPIC}/ir/send_off"
TOPIC_IR_ACK       = f"{ROOM_TOPIC}/ir/ack"
TOPIC_IR_RX        = f"{ROOM_TOPIC}/ir/received"

SYSTEM_PROMPT = """Bạn là trợ lý AI nhà thông minh. Nhiệm vụ:
1. Trả lời câu hỏi về cảm biến phòng ngắn gọn bằng tiếng Việt.
2. Tư vấn bật/tắt điều hòa theo rule:
   - Nhiệt > 28C + có người → bật 26C
   - Không có người > 15 phút → tắt
   - Độ ẩm > 70% → thông gió
3. Nếu người dùng ra lệnh (bật/tắt/timer điều hòa), trả về JSON:
   {{"action": "bat_ac|tat_ac|timer_ac", "temp": 26, "duration": 60}}
   kèm giải thích ngắn.
4. IR ON code  : {ir_on}
   IR OFF code : {ir_off}
   IR vừa nhận : {ir_code}
   - Nếu nhận mã IR remote điều hòa, đối chiếu ON/OFF code đã học để xác nhận lệnh.
   - Các mã NEC thông dụng: Power=0x20DF10EF, Cool=0x20DF906F

Dữ liệu hiện tại: Nhiệt={temp}C, Độ ẩm={hum}%, Motion={motion}, AC={ac}"""


# ── Dashboard Class ───────────────────────────────────────────────────────────
class JetsonAIDashboard:
    def __init__(self):
        self.temp           = 0.0
        self.hum            = 0.0
        self.motion         = "OFF"
        self.ac_status      = "OFF"
        self.last_ir        = "Chưa có"
        self.ir_on_code     = None        # Mã lệnh ON đã học
        self.ir_off_code    = None        # Mã lệnh OFF đã học
        self.ir_setup_mode  = None        # "waiting_on" | "waiting_off" | None
        self.ai_history     = []
        self.lock           = threading.Lock()

        # Callback gán trong run() để đảm bảo thứ tự đúng
        self.client = mqtt.Client(client_id="JetsonDashboard", clean_session=True)

    # ── MQTT ─────────────────────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc == 0:
            client.subscribe(f"{ROOM_TOPIC}/sensor/+")
            client.subscribe(f"{ROOM_TOPIC}/ac/status")
            client.subscribe(TOPIC_IR_RX)
            client.subscribe(TOPIC_IR_ACK)
        else:
            print(f"[MQTT] Connect failed, rc={rc}")

    def publish_ir_status(self, status: str):
        self.client.publish(f"{ROOM_TOPIC}/ir/status", status)
        print(f"[IR STATUS] → {status}")

    def _on_message(self, client, userdata, msg):
        topic             = msg.topic
        payload           = msg.payload.decode().strip()
        status_to_publish = None

        with self.lock:
            try:
                if "temp" in topic:
                    self.temp = float(payload)
                elif "hum" in topic:
                    self.hum = float(payload)
                elif "motion" in topic:
                    self.motion = "ON" if payload == "1" else "OFF"
                elif "ac/status" in topic:
                    self.ac_status = payload.upper()
                elif topic == TOPIC_IR_RX:
                    self.last_ir = payload
                    if self.ir_setup_mode == "waiting_on":
                        self.ir_on_code    = payload
                        self.ir_setup_mode = "waiting_off"
                        status_to_publish  = "waiting_off"
                    elif self.ir_setup_mode == "waiting_off":
                        self.ir_off_code   = payload
                        self.ir_setup_mode = None
                        status_to_publish  = "done"
                elif topic == TOPIC_IR_ACK:
                    pass  # xử lý ngoài lock bên dưới
            except ValueError:
                pass

        # Xử lý ir/ack và publish NGOÀI lock
        if topic == TOPIC_IR_ACK:
            ack_map = {
                "learning_on":      "[IR] Đang chờ học mã BẬT... bấm remote vào IR RX",
                "learning_off":     "[IR] Đang chờ học mã TẮT... bấm remote vào IR RX",
                "learned_on":       "✅ Đã học xong mã BẬT điều hòa!",
                "learned_off":      "✅ Đã học xong mã TẮT điều hòa!",
                "sent_on":          "✅ Đã gửi lệnh BẬT điều hòa!",
                "sent_off":         "✅ Đã gửi lệnh TẮT điều hòa!",
                "error_no_raw_on":  "❌ Chưa học mã BẬT! Vào Setup IR trước.",
                "error_no_raw_off": "❌ Chưa học mã TẮT! Vào Setup IR trước.",
            }
            print(ack_map.get(payload, f"[IR ACK] {payload}"))
            # Khi ESP32 xác nhận đã học ON xong → tự động chuyển sang học OFF
            if payload == "learned_on":
                with self.lock:
                    self.ir_setup_mode = "waiting_off"
                self.client.publish(TOPIC_IR_LEARN_OFF, "1")
                print("[IR Setup] Gửi lệnh học mã TẮT...")

        if status_to_publish:
            self.publish_ir_status(status_to_publish)

    # ── AI ───────────────────────────────────────────────────────────────────
    def ai_query(self, user_query: str) -> str:
        with self.lock:
            sys_prompt = SYSTEM_PROMPT.format(
                temp=self.temp, hum=self.hum,
                motion=self.motion, ac=self.ac_status,
                ir_on=self.ir_on_code   or "Chưa học",
                ir_off=self.ir_off_code or "Chưa học",
                ir_code=self.last_ir,
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

        def safe(row, col, text, attr=curses.A_NORMAL):
            if row < H - 1 and col < W - 1:
                try:
                    stdscr.addstr(row, col, text[:W - col - 1], attr)
                except curses.error:
                    pass

        # Header
        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        safe(0, 0, "=" * (W - 1))
        safe(1, 2, f"JETSON NANO AI DASHBOARD  |  {LLM_MODEL}  |  {datetime.now().strftime('%H:%M:%S')}")
        safe(2, 0, "=" * (W - 1))
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        # Sensor data
        stdscr.attron(curses.color_pair(2))
        safe(4, 2, "[ SENSOR DATA ]")
        stdscr.attroff(curses.color_pair(2))

        with self.lock:
            t      = self.temp
            h      = self.hum
            m      = self.motion
            ac     = self.ac_status
            ir     = self.last_ir
            ir_on  = self.ir_on_code
            ir_off = self.ir_off_code
            ir_mode = self.ir_setup_mode

        safe(5, 4, f"Temperature : {t:.1f} C")
        safe(6, 4, f"Humidity    : {h:.0f} %")

        stdscr.attron(curses.color_pair(5) if m == "ON" else curses.color_pair(1))
        safe(7, 4, f"Motion      : {m}")
        stdscr.attroff(curses.color_pair(5) if m == "ON" else curses.color_pair(1))

        stdscr.attron(curses.color_pair(2) if ac == "ON" else curses.color_pair(3))
        safe(8, 4, f"AC Status   : {ac}")
        stdscr.attroff(curses.color_pair(2) if ac == "ON" else curses.color_pair(3))

        stdscr.attron(curses.color_pair(3))
        safe(9, 4, f"IR RX       : {ir}")
        stdscr.attroff(curses.color_pair(3))

        # IR Setup section
        safe(11, 0, "-" * (W - 1))
        stdscr.attron(curses.color_pair(2) | curses.A_BOLD)
        safe(12, 2, "[ IR SETUP ]")
        stdscr.attroff(curses.color_pair(2) | curses.A_BOLD)

        # ON Code
        if ir_on:
            stdscr.attron(curses.color_pair(1))
            safe(13, 4, f"ON Code  : {ir_on} (đã học)")
            stdscr.attroff(curses.color_pair(1))
        else:
            stdscr.attron(curses.color_pair(3))
            safe(13, 4, "ON Code  : Chưa học")
            stdscr.attroff(curses.color_pair(3))

        # OFF Code
        if ir_off:
            stdscr.attron(curses.color_pair(1))
            safe(14, 4, f"OFF Code : {ir_off} (đã học)")
            stdscr.attroff(curses.color_pair(1))
        else:
            stdscr.attron(curses.color_pair(5))
            safe(14, 4, "OFF Code : Chưa học")
            stdscr.attroff(curses.color_pair(5))

        # Mode status
        blink = int(time.time()) % 2 == 0
        if ir_mode == "waiting_on":
            mode_str = "Hãy bấm nút ON trên remote..." if blink else ">>> CHỜ MÃ ON <<<   "
            stdscr.attron(curses.color_pair(2) | curses.A_BOLD)
            safe(15, 4, f"Mode     : {mode_str}")
            stdscr.attroff(curses.color_pair(2) | curses.A_BOLD)
        elif ir_mode == "waiting_off":
            mode_str = "ON đã lưu! Hãy bấm nút OFF trên remote..." if blink else ">>> CHỜ MÃ OFF <<<  "
            stdscr.attron(curses.color_pair(2) | curses.A_BOLD)
            safe(15, 4, f"Mode     : {mode_str}")
            stdscr.attroff(curses.color_pair(2) | curses.A_BOLD)
        elif ir_on and ir_off:
            stdscr.attron(curses.color_pair(1))
            safe(15, 4, "Mode     : Setup hoàn tất!")
            stdscr.attroff(curses.color_pair(1))
        else:
            safe(15, 4, "Mode     : Nhấn [r] để setup remote")

        # AI response
        safe(17, 0, "-" * (W - 1))
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        safe(18, 2, "[ AI RESPONSE ]")
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)

        max_len = W - 6
        lines   = []
        for chunk in (ai_reply or "Chưa có câu trả lời. Nhấn phím bên dưới.").split("\n"):
            while len(chunk) > max_len:
                lines.append(chunk[:max_len])
                chunk = chunk[max_len:]
            lines.append(chunk)
        for i, line in enumerate(lines[:6]):
            safe(19 + i, 4, line)

        # Commands
        safe(26, 0, "-" * (W - 1))
        stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
        safe(27, 2, "[ COMMANDS ]")
        stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

        cmds = [
            "[t] Nhiệt độ?    [h] Độ ẩm?      [a] Tình trạng AC?  [c] Tư vấn AC?",
            "[b] Bật AC       [x] Tắt AC       [r] Setup IR remote",
            "[o] Gửi IR ON    [f] Gửi IR OFF   [i] Nhận dạng IR    [q] Thoát",
        ]
        for i, cmd in enumerate(cmds):
            safe(28 + i, 4, cmd)

        stdscr.refresh()

    def _input_ir_code(self, stdscr) -> str:
        """Hiển thị prompt nhập hex IR code, trả về string nhập vào."""
        H, W = stdscr.getmaxyx()
        prompt = "Nhập mã IR hex (vd: 0x20DF10EF): "
        row = H - 2
        try:
            stdscr.addstr(row, 0, " " * (W - 1))
            stdscr.addstr(row, 2, prompt[:W - 3])
        except curses.error:
            pass
        stdscr.refresh()
        curses.echo()
        curses.curs_set(1)
        try:
            code = stdscr.getstr(row, 2 + len(prompt), 20).decode().strip()
        except Exception:
            code = ""
        curses.noecho()
        curses.curs_set(0)
        return code

    def _run_tui(self, stdscr):
        stdscr.nodelay(True)
        stdscr.timeout(100)   # refresh mỗi 100ms dù không có keypress
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
            elif key == ord('i') and not busy:
                with self.lock:
                    ir = self.last_ir
                ai_thread = threading.Thread(
                    target=ask,
                    args=(f"Mã IR vừa nhận được là {ir}, đây là lệnh gì của remote?",),
                    daemon=True,
                )
                ai_thread.start()
            elif key == ord('r'):
                with self.lock:
                    self.ir_on_code    = None
                    self.ir_off_code   = None
                    self.ir_setup_mode = "waiting_on"
                self.client.publish(TOPIC_IR_LEARN_ON, "1")
                print("[IR Setup] Gửi lệnh học mã BẬT...")
                self.publish_ir_status("waiting_on")
                ai_reply = "[IR SETUP] Bắt đầu học remote. Bấm nút ON..."
            elif key == ord('o'):
                self.client.publish(TOPIC_IR_SEND_ON, "1")
                ai_reply = "[IR ON] Đã gửi lệnh BẬT → ESP32"
            elif key == ord('f'):
                self.client.publish(TOPIC_IR_SEND_OFF, "1")
                ai_reply = "[IR OFF] Đã gửi lệnh TẮT → ESP32"
            elif key == ord('s'):
                stdscr.nodelay(False)
                code = self._input_ir_code(stdscr)
                stdscr.nodelay(True)
                if code:
                    self.client.publish(TOPIC_IR_SEND_ON, code)
                    ai_reply = f"[IR SENT] {code} → ESP32"

    # ── Entry ─────────────────────────────────────────────────────────────────
    def run(self):
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
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
