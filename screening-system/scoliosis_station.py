"""
Spine-scan laptop station (MQTT version, upright back only)
-----------------------------------------------------------
Pairs with the ESP32 RFID reader (same STATION_ID, e.g. station02).

Flow:
  1. IDLE: no frames are read or processed (no pose model, no drawing), so CPU/GPU
     use is near zero. The window is black before the first scan, and afterwards
     shows the last captured frame, frozen. The camera itself is started once at
     launch and left running, exactly like the original standalone script.
  2. ESP32 publishes a card UID on  screening/<STATION_ID>/scan
  3. SCANNING: frames are processed and the live feed with skeleton overlay
     is shown. Each frame is classified as "upright" (or not) by fitting a plane to
     the back's 3D depth points and measuring its angle to the camera's viewing
     axis. MediaPipe is only used to confirm the person is facing away and to get a
     rough torso region to crop. When "upright" is held continuously for
     HOLD_SECONDS (wall-clock time), the 7 spine points are captured.
  4. Processing stops, the captured frame stays frozen on screen, and the result
     (a list of rough [x, y] pixel coords) is published on
     screening/<STATION_ID>/result, resent every 5 s until the hub acks.
     The ESP32 sees the result/ack and then opens its gate for the next card.

Networking: there is NO wifi name/password here. The laptop just uses whatever
network the OS is already connected to. Only MQTT_HOST matters (see CONFIG).

Setup:  pip install paho-mqtt numpy opencv-python mediapipe primesense
"""

import os
import json
import time
import uuid
import queue
import threading
import urllib.request
from datetime import datetime, timezone

import numpy as np
import cv2
import mediapipe as mp
import paho.mqtt.client as mqtt
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from primesense import openni2

# ============================== CONFIG ==============================
MQTT_HOST = "127.0.0.1"   # 127.0.0.1 if the broker runs on THIS laptop; otherwise the hub's
                          # reserved IP (e.g. "192.168.1.3") and be on the same router/network
MQTT_PORT = 1883
STATION_ID = "station02"  # = MQTT username; MUST match the ESP32's STATION_ID
MQTT_PASS = os.environ.get("MQTT_PASS", "help")
TOPIC_ROOT = "screening"

OPENNI2_REDIST_PATH = "C:/Program Files/OpenNI2/Redist"  # confirm this matches your working setup

SCAN_TIMEOUT_S = 100      # give up on a visit after this long (keep BELOW the ESP32's
                          # RESULT_WAIT_TIMEOUT_MS, which is 120 s by default)
PUBLISH_RESULTS = True    # False = just print results (for solo testing)
TEST_UID = "TESTCARD01"   # press 't' in the idle window to simulate a card scan
# ====================================================================

POSE_MODEL_VARIANT = "full"  # "lite" (fastest, least robust), "full" (recommended), or "heavy" (most robust, slowest)
POSE_MODEL_URLS = {
    "lite": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "full": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
    "heavy": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
}
POSE_MODEL_PATH = f"pose_landmarker_{POSE_MODEL_VARIANT}.task"
POSE_MODEL_URL = POSE_MODEL_URLS[POSE_MODEL_VARIANT]

try:
    import winsound
    def beep(freq=1000, dur=150):
        try:
            winsound.Beep(freq, dur)
        except Exception:
            pass
except ImportError:
    def beep(freq=1000, dur=150):
        pass  # non-Windows: no-op, since winsound is Windows-only


LEFT_SHOULDER, RIGHT_SHOULDER = 11, 12
LEFT_HIP, RIGHT_HIP = 23, 24

FX, FY = 570.3, 570.3
CX, CY = 320.0, 240.0

MIN_DEPTH_MM = 800
MAX_DEPTH_MM = 1600

DISPLAY_SCALE = 1.6

BODY_VISIBILITY_THRESHOLD = 0.5
FACING_AWAY_MARGIN = 0.3

UPRIGHT_MAX_ANGLE = 20        # plane angle (deg) below this = "upright"

HOLD_SECONDS = 5.0            # how long upright must be held before auto-capture
MISMATCH_GRACE_FRAMES = 5     # a pose read can disagree with the current hold for this many
                              # consecutive frames before we treat it as a real change and reset --
                              # absorbs single noisy depth frames instead of restarting the hold

MIN_PLANE_FIT_POINTS = 50     # below this, the ROI didn't contain enough valid back surface to trust a fit
OUTLIER_RESIDUAL_MM = 25      # after the first fit, drop points further than this from the plane and refit

WINDOW_NAME = "Spine scan station - 'q' to quit"


# =====================================================================
# Vision code (same logic as the original standalone script)
# =====================================================================

def ensure_pose_model():
    if not os.path.exists(POSE_MODEL_PATH):
        print(f"Downloading MediaPipe pose model ('{POSE_MODEL_VARIANT}')...")
        urllib.request.urlretrieve(POSE_MODEL_URL, POSE_MODEL_PATH)
        print("Done.")


def create_pose_landmarker():
    base_options = mp_python.BaseOptions(model_asset_path=POSE_MODEL_PATH)
    options = mp_vision.PoseLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.3,
        min_tracking_confidence=0.3,
    )
    return mp_vision.PoseLandmarker.create_from_options(options)


def is_facing_away(landmarks):
    """Facing-away check based on shoulder ordering only. MediaPipe labels
    landmarks by the subject's own anatomical side: facing the camera, the
    subject's LEFT shoulder projects onto the image's right side (like a
    mirror). Facing away, that flips."""
    def vis(idx):
        return landmarks[idx].visibility

    if vis(LEFT_SHOULDER) < BODY_VISIBILITY_THRESHOLD or vis(RIGHT_SHOULDER) < BODY_VISIBILITY_THRESHOLD:
        return False

    lsx = landmarks[LEFT_SHOULDER].x
    rsx = landmarks[RIGHT_SHOULDER].x
    shoulder_width = abs(rsx - lsx)
    if shoulder_width < 0.02:
        return False

    normalized_diff = (rsx - lsx) / shoulder_width
    return normalized_diff > FACING_AWAY_MARGIN


def get_hip_px(landmarks, frame_w, frame_h):
    """(lhx, lhy, rhx, rhy) in pixel coords."""
    return (landmarks[LEFT_HIP].x * frame_w, landmarks[LEFT_HIP].y * frame_h,
            landmarks[RIGHT_HIP].x * frame_w, landmarks[RIGHT_HIP].y * frame_h)


def get_torso_roi(landmarks, frame_w, frame_h, depth_shape):
    """Rough torso bounding box in pixel coords, used only to crop depth
    data for the plane fit -- doesn't need to be precise."""
    lsx, lsy = landmarks[LEFT_SHOULDER].x * frame_w, landmarks[LEFT_SHOULDER].y * frame_h
    rsx, rsy = landmarks[RIGHT_SHOULDER].x * frame_w, landmarks[RIGHT_SHOULDER].y * frame_h
    lhx, lhy, rhx, rhy = get_hip_px(landmarks, frame_w, frame_h)

    shoulder_width = abs(rsx - lsx)
    margin_x = max(20, shoulder_width * 0.3)
    margin_y = max(20, shoulder_width * 0.3)

    x0 = min(lsx, rsx, lhx, rhx) - margin_x
    x1 = max(lsx, rsx, lhx, rhx) + margin_x
    y0 = min(lsy, rsy, lhy, rhy) - margin_y
    y1 = max(lsy, rsy, lhy, rhy) + margin_y

    h, w = depth_shape
    x0, x1 = int(np.clip(x0, 0, w)), int(np.clip(x1, 0, w))
    y0, y1 = int(np.clip(y0, 0, h)), int(np.clip(y1, 0, h))
    return x0, x1, y0, y1


def fit_back_plane_angle(depth_mm, x0, x1, y0, y1):
    """Fits a plane to the 3D points of the back surface within the ROI via
    SVD (total least squares) and returns the angle (degrees) between that
    plane's normal and the camera's viewing axis. ~0 = surface facing the
    camera (upright). Returns (None, point_count) if there isn't enough valid
    depth data. Does one round of outlier trimming, since depth sensors are
    noisy at silhouette edges and in shadowed regions."""
    if x1 <= x0 or y1 <= y0:
        return None, 0

    region = depth_mm[y0:y1, x0:x1].astype(np.float32)
    ys, xs = np.indices(region.shape)
    xs = xs + x0
    ys = ys + y0
    zs = region

    valid = (zs > MIN_DEPTH_MM) & (zs < MAX_DEPTH_MM)
    count = int(np.sum(valid))
    if count < MIN_PLANE_FIT_POINTS:
        return None, count

    X = (xs[valid] - CX) * zs[valid] / FX
    Y = (ys[valid] - CY) * zs[valid] / FY
    Z = zs[valid]
    points = np.column_stack([X, Y, Z])

    def fit_normal(pts):
        centroid = pts.mean(axis=0)
        centered = pts - centroid
        # The plane normal is the least-variance direction of the point cloud.
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
        return vh[-1], centroid, centered

    normal, centroid, centered = fit_normal(points)
    residuals = np.abs(centered @ normal)  # perpendicular distance to the fitted plane
    inliers = residuals < OUTLIER_RESIDUAL_MM
    if np.sum(inliers) >= MIN_PLANE_FIT_POINTS:
        normal, _, _ = fit_normal(points[inliers])
        count = int(np.sum(inliers))

    # Angle between the plane normal and the camera's viewing axis (0, 0, 1).
    cos_angle = abs(normal[2]) / np.linalg.norm(normal)
    angle = np.degrees(np.arccos(np.clip(cos_angle, -1.0, 1.0)))
    return angle, count


def classify_pose(depth_mm, landmarks, frame_w, frame_h):
    """Returns (pose_type, angle_degrees). pose_type is 'upright' or None
    (not upright / not enough data)."""
    x0, x1, y0, y1 = get_torso_roi(landmarks, frame_w, frame_h, depth_mm.shape)
    angle, _ = fit_back_plane_angle(depth_mm, x0, x1, y0, y1)

    if angle is None:
        return None, None
    if angle < UPRIGHT_MAX_ANGLE:
        return "upright", angle
    return None, angle


def find_groove_x(depth_mm, y, x_center, search_half_width):
    h, w = depth_mm.shape
    y = int(np.clip(y, 0, h - 1))
    x0 = int(np.clip(x_center - search_half_width, 0, w - 1))
    x1 = int(np.clip(x_center + search_half_width, 0, w - 1))
    if x1 <= x0:
        return None
    row = depth_mm[y, x0:x1].astype(np.float32)
    valid = (row > MIN_DEPTH_MM) & (row < MAX_DEPTH_MM)
    if not np.any(valid):
        return None
    row_masked = np.where(valid, row, -1)
    best_idx = np.argmax(row_masked)
    return x0 + best_idx


def find_silhouette_edge(depth_mm, y, x_start, direction, max_search):
    h, w = depth_mm.shape
    y = int(np.clip(y, 0, h - 1))
    x = int(x_start)
    last_valid = x
    for _ in range(max_search):
        x += direction
        if x < 0 or x >= w:
            break
        d = depth_mm[y, x]
        if MIN_DEPTH_MM < d < MAX_DEPTH_MM:
            last_valid = x
        else:
            break
    return last_valid


def compute_spine_points(depth_mm, landmarks, frame_w, frame_h):
    def px(idx):
        lm = landmarks[idx]
        return lm.x * frame_w, lm.y * frame_h

    lsx, lsy = px(LEFT_SHOULDER)
    rsx, rsy = px(RIGHT_SHOULDER)
    lhx, lhy, rhx, rhy = get_hip_px(landmarks, frame_w, frame_h)

    shoulder_y = (lsy + rsy) / 2
    hip_y = (lhy + rhy) / 2
    shoulder_width = abs(rsx - lsx)
    torso_center_x = (lsx + rsx + lhx + rhx) / 4

    if shoulder_width < 10 or abs(hip_y - shoulder_y) < 10:
        return None

    heights = np.linspace(shoulder_y, hip_y, 5)
    search_half_width = max(15, shoulder_width * 0.25)

    points = {}
    for i, y in enumerate(heights):
        gx = find_groove_x(depth_mm, y, torso_center_x, search_half_width)
        if gx is None:
            gx = torso_center_x
        points[i] = (float(gx), float(y))
        torso_center_x = gx

    blade_y = heights[1]
    left_edge = find_silhouette_edge(depth_mm, blade_y, lsx, direction=-1, max_search=int(shoulder_width))
    right_edge = find_silhouette_edge(depth_mm, blade_y, rsx, direction=+1, max_search=int(shoulder_width))
    points[6] = (float(left_edge), float(blade_y))
    points[5] = (float(right_edge), float(blade_y))

    return points


# =====================================================================
# MQTT layer
# =====================================================================

BASE = f"{TOPIC_ROOT}/{STATION_ID}"
PENDING_FILE = f"pending_{STATION_ID}.json"

scan_q = queue.Queue()
pending = {}                      # session_id -> JSON string, resent until acked
pending_lock = threading.Lock()
busy = False                      # True while a visit is being scanned
last_resend = 0.0
frozen_frame = None               # last captured frame, shown while waiting for the next card


def topic(kind):
    return f"{BASE}/{kind}"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def save_pending():
    with pending_lock:
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f)


def load_pending():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE) as f:
                pending.update(json.load(f))
            if pending:
                print(f"Loaded {len(pending)} unconfirmed result(s) from disk; will resend.")
        except Exception as e:
            print("Could not read pending file:", e)


try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{STATION_ID}-py")
except AttributeError:               # paho-mqtt 1.x
    client = mqtt.Client(client_id=f"{STATION_ID}-py")
client.username_pw_set(STATION_ID, MQTT_PASS)
client.will_set(topic("status"), "offline", qos=1, retain=True)
client.reconnect_delay_set(min_delay=1, max_delay=10)


def set_status(s):
    client.publish(topic("status"), s, qos=1, retain=True)


def send_event(etype, uid):
    client.publish(topic("event"), json.dumps({"type": etype, "uid": uid, "ts": now_iso()}))


def on_connect(c, userdata, flags, rc, *extra):
    if rc == 0:
        print("MQTT connected")
        c.subscribe(topic("scan"), 1)
        c.subscribe(topic("ack"), 1)
        set_status("busy" if busy else "online")
    else:
        print(f"MQTT connect failed rc={rc} (check username/password)")


def on_disconnect(c, userdata, *args):
    print("MQTT disconnected (will auto-reconnect)")


def on_message(c, userdata, msg):
    payload = msg.payload.decode("utf-8", errors="replace")
    if msg.topic.endswith("/scan"):
        try:
            uid = str(json.loads(payload)["uid"])
        except Exception:
            uid = payload
        uid = uid.strip().upper()
        if not uid:
            return
        if busy or not scan_q.empty():
            send_event("scan_ignored_busy", uid)
        else:
            scan_q.put(uid)
    elif msg.topic.endswith("/ack"):
        try:
            doc = json.loads(payload)
        except Exception:
            return
        sid = doc.get("session_id", "")
        with pending_lock:
            known = sid in pending
        if known:
            if doc.get("ok"):
                with pending_lock:
                    pending.pop(sid, None)
                save_pending()
                print("Hub saved result.")
            else:
                print("Hub REJECTED result (will keep resending).")


client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


def resend_pending():
    global last_resend
    if time.time() - last_resend < 5 or not client.is_connected():
        return
    last_resend = time.time()
    with pending_lock:
        msgs = list(pending.values())
    for m in msgs:
        client.publish(topic("result"), m)


def points_to_list(points):
    """Rough pixel coords as a list ordered by point index:
    0-4 = spine groove from shoulder level down to hip level,
    5 = right back edge, 6 = left back edge (at shoulder-blade height)."""
    return [[int(round(points[i][0])), int(round(points[i][1]))] for i in sorted(points)]


def submit_result(uid, data):
    sid = uuid.uuid4().hex
    doc = {"uid": uid, "session_id": sid, "ts": now_iso(), "data": data}
    out = json.dumps(doc)

    if not PUBLISH_RESULTS:
        print(f"\nRESULT (not sent, PUBLISH_RESULTS=False) for {uid}:")
        print(json.dumps(data, indent=2))
        return

    with pending_lock:
        pending[sid] = out
    save_pending()
    client.publish(topic("result"), out)
    print(f"Result published for {uid} (session {sid[:8]}...), waiting for hub ack.")


# =====================================================================
# Drawing helpers
# =====================================================================

def print_debug_info(frame_count, landmarks, pose_type, plane_angle):
    if frame_count % 30 != 0 or landmarks is None:
        return
    def vis(idx):
        return round(landmarks[idx].visibility, 2)
    angle_str = f"{plane_angle:.1f}" if plane_angle is not None else "N/A"
    print(
        f"[frame {frame_count}] shoulders(L/R) vis={vis(LEFT_SHOULDER)}/{vis(RIGHT_SHOULDER)} "
        f"hips(L/R) vis={vis(LEFT_HIP)}/{vis(RIGHT_HIP)} plane_angle={angle_str} pose_type={pose_type}"
    )


def draw_debug_skeleton(display_frame, landmarks, frame_w, frame_h, scale):
    """Draws EVERY detected landmark in yellow, regardless of gating."""
    if landmarks is None:
        return
    for lm in landmarks:
        dx, dy = int(lm.x * frame_w * scale), int(lm.y * frame_h * scale)
        cv2.circle(display_frame, (dx, dy), 3, (0, 255, 255), -1)


def draw_overlay(display_frame, points, scale):
    if points is None:
        return
    for idx, (x, y) in points.items():
        dx, dy = int(x * scale), int(y * scale)
        cv2.circle(display_frame, (dx, dy), 5, (0, 0, 255), -1)
        cv2.putText(display_frame, str(idx), (dx + 8, dy - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)


def draw_status(display_frame, uid, v, plane_angle, elapsed, reason=""):
    y0, line_h = 30, 30
    cv2.putText(display_frame, f"Card: {uid}", (20, y0),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.putText(display_frame, "Stand upright, back to camera", (20, y0 + line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
    if plane_angle is not None:
        cv2.putText(display_frame, f"Back plane angle: {plane_angle:.1f} deg", (20, y0 + 2 * line_h),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
    if v["hold_type"] is not None and v["hold_start"] is not None:
        held_for = time.time() - v["hold_start"]
        cv2.putText(display_frame, f"Holding '{v['hold_type']}': {held_for:.1f}s / {HOLD_SECONDS}s",
                    (20, y0 + 3 * line_h), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
    cv2.putText(display_frame, f"Time left: {max(0, SCAN_TIMEOUT_S - elapsed):.0f}s", (20, y0 + 4 * line_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)
    if reason:
        cv2.putText(display_frame, reason, (20, display_frame.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)


def show_idle(connected):
    if frozen_frame is not None:          # keep showing the last capture (no processing)
        cv2.imshow(WINDOW_NAME, frozen_frame)
        return
    img = np.zeros((240, 560, 3), dtype=np.uint8)
    msg = "Waiting for card..." if connected else "Connecting to MQTT..."
    cv2.putText(img, msg, (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 165, 255), 2)
    cv2.putText(img, "(idle)  q = quit,  t = test scan", (20, 160),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 1)
    cv2.imshow(WINDOW_NAME, img)


# =====================================================================
# One visit: stream camera until the upright pose is captured
# =====================================================================

def run_visit(uid, landmarker, depth_stream, color_stream):
    """Streams the camera until upright is held for HOLD_SECONDS.
    Returns (points_dict_or_None, abort_reason_or_None)."""
    global frozen_frame
    frozen_frame = None
    v = {"hold_type": None, "hold_start": None, "mismatch_streak": 0}
    captured = None
    frame_count = 0
    t_start = time.time()
    abort = None

    # Streams stay running (as in the original script). Drop frames that queued up while idle.
    for _ in range(5):
        depth_stream.read_frame()
        color_stream.read_frame()
    print(f"Scanning started for {uid}.")

    try:
        while True:
            depth_frame = depth_stream.read_frame()
            depth_mm = np.frombuffer(depth_frame.get_buffer_as_uint16(), dtype=np.uint16).reshape(
                depth_frame.height, depth_frame.width)

            color_frame = color_stream.read_frame()
            color_bgr = np.frombuffer(color_frame.get_buffer_as_uint8(), dtype=np.uint8).reshape(
                color_frame.height, color_frame.width, 3)
            color_bgr = cv2.cvtColor(color_bgr, cv2.COLOR_RGB2BGR)

            frame_h, frame_w = color_bgr.shape[:2]
            rgb_for_mp = cv2.cvtColor(color_bgr, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_for_mp)
            result = landmarker.detect_for_video(mp_image, int(time.time() * 1000))
            landmarks = result.pose_landmarks[0] if result.pose_landmarks else None

            points, pose_type, plane_angle = None, None, None
            reason = "No person detected"
            frame_count += 1

            if landmarks and is_facing_away(landmarks):
                pose_type, plane_angle = classify_pose(depth_mm, landmarks, frame_w, frame_h)
                points = compute_spine_points(depth_mm, landmarks, frame_w, frame_h)
                if plane_angle is None:
                    reason = "Not enough depth data on back (stand 0.8-1.6 m from camera)"
                elif pose_type is None:
                    reason = f"Back angle {plane_angle:.0f} deg - stand straighter (need < {UPRIGHT_MAX_ANGLE})"
                else:
                    reason = "Upright OK - hold still"

                if pose_type == v["hold_type"]:
                    v["mismatch_streak"] = 0
                else:
                    v["mismatch_streak"] += 1
                    if v["mismatch_streak"] >= MISMATCH_GRACE_FRAMES:
                        # Sustained disagreement -- a real pose change, not noise.
                        v["hold_type"] = pose_type
                        v["hold_start"] = time.time() if pose_type is not None else None
                        v["mismatch_streak"] = 0

                if pose_type is not None and v["hold_type"] == pose_type and v["hold_start"] is not None:
                    held_for = time.time() - v["hold_start"]
                    if held_for >= HOLD_SECONDS and points is not None:
                        captured = points

            if landmarks and not is_facing_away(landmarks):
                reason = "Person seen but not facing away - turn your back to the camera"
            print_debug_info(frame_count, landmarks, pose_type, plane_angle)
            if frame_count % 30 == 0 and (landmarks is None or pose_type is None):
                print(f"[frame {frame_count}] {reason}")

            elapsed = time.time() - t_start
            display = cv2.resize(color_bgr, None, fx=DISPLAY_SCALE, fy=DISPLAY_SCALE)
            draw_debug_skeleton(display, landmarks, frame_w, frame_h, DISPLAY_SCALE)
            draw_overlay(display, points, DISPLAY_SCALE)

            if captured is not None:
                cv2.putText(display, f"CAPTURED ({uid}) - waiting for next card", (20, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 220, 0), 2)
                frozen_frame = display.copy()
                cv2.imshow(WINDOW_NAME, frozen_frame)
                cv2.waitKey(1)
                print(f"Captured upright pose for {uid}.")
                beep(880, 100)
                beep(1200, 200)
                break

            if elapsed > SCAN_TIMEOUT_S:
                abort = "timeout"
                break

            draw_status(display, uid, v, plane_angle, elapsed, reason)
            cv2.imshow(WINDOW_NAME, display)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                abort = "quit"
                break
    finally:
        pass  # streams are intentionally left running; idle simply stops reading them

    return (None if abort else captured), abort


# =====================================================================
# Main
# =====================================================================

openni2_dev = None


def main():
    global busy, openni2_dev

    load_pending()
    ensure_pose_model()
    landmarker = create_pose_landmarker()

    openni2.initialize(OPENNI2_REDIST_PATH)
    openni2_dev = openni2.Device.open_any()

    # Identical to the original script: start both streams once and leave them running.
    depth_stream = openni2_dev.create_depth_stream()
    depth_stream.start()
    color_stream = openni2_dev.create_color_stream()
    color_stream.start()

    # Align depth to color so MediaPipe (RGB) landmark coordinates line up with the
    # depth image. Must be done AFTER both streams exist and are started.
    try:
        openni2_dev.set_image_registration_mode(openni2.IMAGE_REGISTRATION_DEPTH_TO_COLOR)
        print("Depth/color registration enabled.")
    except Exception as e:
        print("Warning: could not enable depth/color registration:", e)
        print("Spine point positions may be offset by a few pixels without it.")

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)

    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=15)
    client.loop_start()
    print(f"Station '{STATION_ID}' running; broker {MQTT_HOST}:{MQTT_PORT}. Waiting for card scans.")

    try:
        while True:
            resend_pending()
            show_idle(client.is_connected())
            key = cv2.waitKey(100) & 0xFF      # idle: ~10 loops/s, nothing processed
            if key == ord('q'):
                break
            if key == ord('t') and scan_q.empty():
                scan_q.put(TEST_UID)

            try:
                uid = scan_q.get_nowait()
            except queue.Empty:
                continue

            busy = True
            set_status("busy")
            captured, abort = run_visit(uid, landmarker, depth_stream, color_stream)
            if captured is not None:
                submit_result(uid, {
                    "upright_points_px": points_to_list(captured),
                    "n_points": len(captured),
                })
            else:
                print(f"Visit for {uid} aborted ({abort}).")
                send_event("measure_failed", uid)
            busy = False
            set_status("online")
            if abort == "quit":
                break
    finally:
        try:
            set_status("offline")
        except Exception:
            pass
        client.loop_stop()
        client.disconnect()
        depth_stream.stop()
        color_stream.stop()
        openni2_dev.close()
        openni2.unload()
        landmarker.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()