from flask import Flask, render_template, jsonify, request, Response
import serial
import serial.tools.list_ports
import json
import time
import threading
import os
import math
import sqlite3
import csv
import io
import smtplib
from datetime import datetime, timezone
from collections import deque
from email.message import EmailMessage
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Application State ---
latest_data = {}
data_lock = threading.Lock()
stop_event = threading.Event()
event_log = deque(maxlen=500)
event_lock = threading.Lock()
emergency_state_lock = threading.Lock()

# --- Configuration ---
BAUD_RATE = int(os.getenv("BAUD_RATE", "9600"))
MANUAL_PORT = os.getenv("SERIAL_PORT", "COM18").strip()
PREFERRED_PORT = os.getenv("PREFERRED_PORT", "COM18").strip()
DB_PATH = os.getenv("DB_PATH", "solar_tracker.db")
DB_SAVE_INTERVAL_SECONDS = int(os.getenv("DB_SAVE_INTERVAL_SECONDS", "30"))
DATA_STALE_SECONDS = int(os.getenv("DATA_STALE_SECONDS", "10"))

EMAIL_SENDER = os.getenv("EMAIL_SENDER", "").strip()
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").strip()
EMAIL_RECEIVERS = [x.strip() for x in os.getenv("EMAIL_RECEIVERS", "").split(",") if x.strip()]
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "120"))
AUTO_ALERT_ENABLED = os.getenv("AUTO_ALERT_ENABLED", "true").strip().lower() == "true"
HEALTH_CHECK_INTERVAL_SECONDS = int(os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "5"))

ser = None
serial_lock = threading.Lock()
serial_error = "No serial connection yet."
last_auto_alert_at = 0.0
emergency_active = False


def log_event(level, message):
    """Store in-memory logs for dashboard visibility."""
    entry = {
        "ts": time.time(),
        "level": level,
        "message": str(message),
    }
    with event_lock:
        event_log.append(entry)
    print(f"[{level}] {message}")


def db_rows_to_xml(rows):
    """Serialize DB records to XML text."""
    root = ET.Element("sensor_readings")
    root.set("count", str(len(rows)))

    for row in rows:
        item = ET.SubElement(root, "reading")
        for key, value in row.items():
            node = ET.SubElement(item, key)
            node.text = "" if value is None else str(value)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def fetch_history_rows(limit=2000, newest_first=True):
    """Read rows from SQLite for history, export, and alert attachments."""
    order = "DESC" if newest_first else "ASC"
    safe_limit = max(1, min(int(limit), 10000))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT id, recorded_at, n, e, s, w, temp, hum, volt, direction, angle_ns, angle_ew, mode
            FROM sensor_readings
            ORDER BY id {order}
            LIMIT ?
            """,
            (safe_limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


def generate_csv_bytes(rows):
    """Generate CSV content from DB rows."""
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["id", "recorded_at", "n", "e", "s", "w", "temp", "hum", "volt", "direction", "angle_ns", "angle_ew", "mode"]
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(h) for h in header])
    return output.getvalue().encode("utf-8")


def send_email_notification(subject, body, xml_attachment_bytes=None, attachment_name="sensor_data.xml"):
    """Send notification email to configured receivers."""
    if not EMAIL_SENDER or not EMAIL_APP_PASSWORD or not EMAIL_RECEIVERS:
        return False, "Email is not configured. Check .env values."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(EMAIL_RECEIVERS)
    msg.set_content(body)

    if xml_attachment_bytes:
        msg.add_attachment(
            xml_attachment_bytes,
            maintype="application",
            subtype="xml",
            filename=attachment_name,
        )

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        return True, f"Notification sent to {len(EMAIL_RECEIVERS)} receiver(s)."
    except Exception as e:
        return False, f"Email send failed: {e}"


def send_emergency_alert(reason):
    """Build and send emergency email with latest data and XML history attachment."""
    with data_lock:
        snapshot = dict(latest_data)

    history = fetch_history_rows(limit=500, newest_first=True)
    xml_bytes = db_rows_to_xml(history)
    now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = (
        f"Emergency triggered at {now_text}\n"
        f"Reason: {reason}\n\n"
        f"Current Dashboard Data:\n{json.dumps(snapshot, indent=2)}\n\n"
        f"Attached: latest DB history in XML format."
    )
    return send_email_notification(
        subject="HelioBloom Emergency Alert",
        body=body,
        xml_attachment_bytes=xml_bytes,
        attachment_name="heliobloom_history.xml",
    )


def init_database():
    """Create SQLite table for periodic sensor snapshots."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at REAL NOT NULL,
                n INTEGER NOT NULL,
                e INTEGER NOT NULL,
                s INTEGER NOT NULL,
                w INTEGER NOT NULL,
                temp REAL,
                hum REAL,
                volt REAL NOT NULL,
                direction TEXT,
                angle_ns INTEGER,
                angle_ew INTEGER,
                mode TEXT
            )
            """
        )
        conn.commit()
    log_event("INFO", f"Database initialized at {DB_PATH}")


def save_latest_data_snapshot():
    """Persist latest dashboard payload to local SQLite database."""
    with data_lock:
        snapshot = dict(latest_data)

    if not snapshot:
        return False, "No data yet; skipping DB save."

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sensor_readings (
                    recorded_at, n, e, s, w, temp, hum, volt, direction, angle_ns, angle_ew, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(snapshot.get("timestamp", time.time())),
                    int(snapshot.get("N", 0)),
                    int(snapshot.get("E", 0)),
                    int(snapshot.get("S", 0)),
                    int(snapshot.get("W", 0)),
                    snapshot.get("temp"),
                    snapshot.get("hum"),
                    float(snapshot.get("volt", 0.0)),
                    str(snapshot.get("dir", "-")),
                    int(snapshot.get("angle_ns", 90)),
                    int(snapshot.get("angle_ew", 90)),
                    str(snapshot.get("mode", "AUTO")),
                ),
            )
            conn.commit()
        return True, "Saved sensor snapshot to DB."
    except (sqlite3.Error, TypeError, ValueError) as e:
        return False, f"DB save failed: {e}"


def database_writer_thread():
    """Write a sensor snapshot to SQLite every configured interval."""
    while not stop_event.is_set():
        stop_event.wait(DB_SAVE_INTERVAL_SECONDS)
        if stop_event.is_set():
            break

        ok, message = save_latest_data_snapshot()
        if ok:
            log_event("DB", message)
        else:
            log_event("WARN", message)


def health_monitor_thread():
    """Watch health and auto-send emergency alerts when data flow fails."""
    global emergency_active, last_auto_alert_at

    while not stop_event.is_set():
        current_issue = None
        now = time.time()

        with data_lock:
            has_data = bool(latest_data)
            last_seen = float(latest_data.get("timestamp", 0)) if has_data else 0.0

        if serial_error and serial_error != "No serial connection yet.":
            current_issue = serial_error
        elif not has_data:
            current_issue = "No data received yet."
        elif now - last_seen > DATA_STALE_SECONDS:
            current_issue = f"Data stale for {int(now - last_seen)}s."

        with emergency_state_lock:
            if current_issue:
                if not emergency_active:
                    log_event("ERROR", f"Emergency detected: {current_issue}")
                emergency_active = True

                should_alert = AUTO_ALERT_ENABLED and (now - last_auto_alert_at >= ALERT_COOLDOWN_SECONDS)
                if should_alert:
                    ok, msg = send_emergency_alert(current_issue)
                    if ok:
                        last_auto_alert_at = now
                        log_event("ALERT", msg)
                    else:
                        log_event("ERROR", msg)
            else:
                if emergency_active:
                    log_event("INFO", "Emergency cleared. Data flow restored.")
                emergency_active = False

        stop_event.wait(HEALTH_CHECK_INTERVAL_SECONDS)

def find_arduino_port():
    """Finds a plausible port for the Arduino."""
    print("Searching for Arduino port...")
    if MANUAL_PORT:
        print(f"Using manual serial port from SERIAL_PORT: {MANUAL_PORT}")
        return MANUAL_PORT

    ports = serial.tools.list_ports.comports()
    if not ports:
        print("No serial ports found.")
        return None

    available_devices = [p.device for p in ports]
    if PREFERRED_PORT and PREFERRED_PORT in available_devices:
        print(f"Using preferred serial port: {PREFERRED_PORT}")
        return PREFERRED_PORT

    for port in ports:
        if "arduino" in port.description.lower() or "ch340" in port.description.lower() or "usb-serial" in port.description.lower():
            print(f"Found plausible Arduino on {port.device}")
            return port.device

    print("No Arduino-like port found. Will try the first available port.")
    return ports[0].device


def open_serial_connection():
    """Open serial connection if available."""
    global ser, serial_error
    port = find_arduino_port()
    if not port:
        serial_error = "No COM port found. Connect Arduino and retry."
        return False

    try:
        with serial_lock:
            ser = serial.Serial(port, BAUD_RATE, timeout=1)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        serial_error = ""
        log_event("INFO", f"Serial connected on {port} @ {BAUD_RATE}")
        time.sleep(2)
        return True
    except serial.SerialException as e:
        serial_error = f"Failed to open {port}: {e}"
        log_event("ERROR", serial_error)
        return False


def to_number(value, default=None):
    """Safely convert incoming JSON values to float/int-like numbers."""
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default

def serial_reader_thread():
    """Continuously read JSON lines from Arduino serial output."""
    global latest_data, ser, serial_error

    while not stop_event.is_set():
        if ser is None or not ser.is_open:
            open_serial_connection()
            time.sleep(1)
            continue

        try:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
            if not raw:
                continue

            if not (raw.startswith("{") and raw.endswith("}")):
                continue

            data = json.loads(raw)

            required = ["N", "E", "S", "W", "volt", "dir"]
            if not all(k in data for k in required):
                continue

            n = to_number(data.get("N"))
            e = to_number(data.get("E"))
            s = to_number(data.get("S"))
            w = to_number(data.get("W"))
            volt = to_number(data.get("volt"), 0.0)

            if n is None or e is None or s is None or w is None:
                continue

            if n == 0 or e == 0 or s == 0 or w == 0:
                continue

            temp = to_number(data.get("temp"), None)
            hum = to_number(data.get("hum"), None)

            # Normalize payload so frontend always receives stable types.
            data["N"] = int(n)
            data["E"] = int(e)
            data["S"] = int(s)
            data["W"] = int(w)
            data["volt"] = float(volt)
            data["temp"] = temp
            data["hum"] = hum
            data.setdefault("angle_ns", 90)
            data.setdefault("angle_ew", 90)
            data.setdefault("mode", "AUTO")
            data.setdefault("check_stage", "-")
            data.setdefault("check_best_dir", "-")
            data.setdefault("check_best_light", 0)

            data["timestamp"] = time.time()

            with data_lock:
                latest_data = data

            serial_error = ""
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        except serial.SerialException as e:
            serial_error = f"Serial disconnected: {e}"
            log_event("ERROR", serial_error)
            with serial_lock:
                try:
                    if ser and ser.is_open:
                        ser.close()
                except Exception:
                    pass
                ser = None
            time.sleep(1)
        except Exception as e:
            serial_error = f"Reader error: {e}"
            log_event("ERROR", serial_error)
            time.sleep(0.2)


@app.route('/')
def index():
    """Serves the main dashboard page."""
    return render_template('index.html')

@app.route('/data')
def get_data():
    """API endpoint to get the latest sensor data."""
    with data_lock:
        if not latest_data:
            return jsonify({"error": serial_error or "No data received yet."}), 500
        
        if time.time() - latest_data.get('timestamp', 0) > DATA_STALE_SECONDS:
             return jsonify({"error": "Data is stale. Check Arduino connection."}), 500

        return jsonify(latest_data)


@app.route('/history')
def get_history():
    """Returns latest saved records from local SQLite DB."""
    limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(limit, 500))

    try:
        rows = fetch_history_rows(limit=limit, newest_first=True)
        return jsonify({"count": len(rows), "records": rows}), 200
    except sqlite3.Error as e:
        return jsonify({"error": f"DB read failed: {e}"}), 500


@app.route('/logs')
def get_logs():
    """Expose recent server logs for the dashboard."""
    limit = request.args.get("limit", default=120, type=int)
    limit = max(1, min(limit, 500))
    with event_lock:
        logs = list(event_log)[-limit:]
    return jsonify({"count": len(logs), "logs": logs}), 200


@app.route('/download/csv')
def download_csv():
    """Download historical sensor records as CSV."""
    try:
        rows = fetch_history_rows(limit=10000, newest_first=False)
        csv_bytes = generate_csv_bytes(rows)
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=heliobloom_history.csv"},
        )
    except sqlite3.Error as e:
        return jsonify({"error": f"CSV export failed: {e}"}), 500


@app.route('/download/xml')
def download_xml():
    """Download historical sensor records as XML."""
    try:
        rows = fetch_history_rows(limit=10000, newest_first=False)
        xml_bytes = db_rows_to_xml(rows)
        return Response(
            xml_bytes,
            mimetype="application/xml",
            headers={"Content-Disposition": "attachment; filename=heliobloom_history.xml"},
        )
    except sqlite3.Error as e:
        return jsonify({"error": f"XML export failed: {e}"}), 500


@app.route('/send-alert', methods=['POST'])
def send_alert_now():
    """Send alert email with current data and XML attachment on demand."""
    try:
        reason = "Manual alert from dashboard"
        ok, message = send_emergency_alert(reason)
        if ok:
            log_event("ALERT", "Manual alert sent from dashboard.")
            return jsonify({"status": "success", "message": message}), 200
        log_event("ERROR", message)
        return jsonify({"status": "error", "message": message}), 500
    except Exception as e:
        log_event("ERROR", f"Manual alert failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def send_serial_command(command):
    global ser
    with serial_lock:
        if ser is None or not ser.is_open:
            return False, "Serial port not available."
        try:
            ser.write(f"{command}\n".encode("utf-8"))
            return True, f"{command} command sent."
        except serial.SerialException as e:
            return False, str(e)

@app.route('/reset', methods=['POST'])
def reset_tracker():
    """API endpoint to send RESET command."""
    ok, msg = send_serial_command("RESET")
    if ok:
        return jsonify({"status": "success", "message": msg}), 200
    return jsonify({"status": "error", "message": msg}), 500

@app.route('/check', methods=['POST'])
def check_tracker():
    """API endpoint to send CHECK command."""
    ok, msg = send_serial_command("CHECK")
    if ok:
        return jsonify({"status": "success", "message": msg}), 200
    return jsonify({"status": "error", "message": msg}), 500


if __name__ == '__main__':
    print(f"Initializing local database at {DB_PATH}...")
    init_database()

    print("Starting background data reader thread...")
    reader_thread = threading.Thread(target=serial_reader_thread)
    reader_thread.daemon = True
    reader_thread.start()

    print(f"Starting DB writer thread (interval: {DB_SAVE_INTERVAL_SECONDS}s)...")
    db_thread = threading.Thread(target=database_writer_thread)
    db_thread.daemon = True
    db_thread.start()

    print("Starting health monitor thread...")
    monitor_thread = threading.Thread(target=health_monitor_thread)
    monitor_thread.daemon = True
    monitor_thread.start()

    print("\n" + "="*50)
    print(">>> HelioBloom Server is starting! <<<")
    print(f"Open your browser and go to: http://127.0.0.1:5000")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=5000, debug=False)

    print("Shutting down...")
    stop_event.set()
    with serial_lock:
        if ser and ser.is_open:
            ser.close()
    reader_thread.join()
    db_thread.join()
    monitor_thread.join()
    print("Goodbye!")
