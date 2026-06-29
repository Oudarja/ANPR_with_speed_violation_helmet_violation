

# import cv2
# import time
# import os
# import re
# from pathlib import Path
# from datetime import date, datetime
# import numpy as np
# from ultralytics import YOLO
# from sort import Sort
# from clip import ClipManager
# from datetime import datetime
# from collections import deque,Counter
# from ocr import find_date_time
# from config import (
#     MONITOR_WIDTH, MONITOR_HEIGHT,
#     PLATE_CONF_TH, SPEED_LIMIT_KMPH, PIXELS_PER_METER,
#     VEHICLE_MODEL, PLATE_MODEL, VEHICLE_CLASSES,
#     UPLOAD_ROOT, CAMERA_FOLDER_MAP
# )
# from helpers import update_plate_buffer, clear_vote_store, VehicleColorDetector
# from database import (
#     insert_detected_plate,
#     insert_speed_violation, set_camera_status,
#     get_camera_signal
# )

# _color_detector = VehicleColorDetector()

# _SIGNAL_MAP = {
#     "red":     ((0,   0,   255), True,    "RED"),
#     "green":   ((0,   255, 0),   False,   "GREEN"),
#     "orange":  ((0,   165, 255), False,   "ORANGE"),
#     "unknown": ((128, 128, 128), False,   "NO SIGNAL"),
# }

# _SIGNAL_POLL_INTERVAL = 2
# _SIGNAL_DEBOUNCE_SEC  = 3.0


# # ─────────────────────────────────────────────────────
# # RTSP/file stream open
# # ─────────────────────────────────────────────────────
# def _open_stream(stream_url: str) -> cv2.VideoCapture:
#     """
#     RTSP এ:
#       - FFmpeg backend
#       - Buffer = 1 frame  (real-time, lag নেই)
#       - Timeout 5s
#     File/MJPEG এও কাজ করে।
#     """
#     is_rtsp = stream_url.lower().startswith("rtsp://")

#     if is_rtsp:
#         os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
#             "rtsp_transport;udp|"
#             "stimeout;5000000"
#         )
#         cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
#         cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
#     else:
#         cap = cv2.VideoCapture(stream_url)
#         cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

#     return cap


# def _is_file_stream(stream_url: str) -> bool:
#     """RTSP নয়, local file কিনা check করে।"""
#     lower = stream_url.lower()
#     return not lower.startswith("rtsp://") and not lower.startswith("http")


# def _normalize_db_path(path: str) -> str:
#     """Store DB paths with an uploads-relative prefix."""
#     try:
#         rel = Path(path).relative_to(UPLOAD_ROOT)
#         return str(Path("uploads") / rel)
#     except Exception:
#         try:
#             return os.path.relpath(path)
#         except Exception:
#             return path


# # ─────────────────────────────────────────────────────
# # Main camera worker
# # ─────────────────────────────────────────────────────
# def run_camera(camera_info: dict, roi_polygon: np.ndarray,
#                preview_queue=None):
#     """
#     camera_info  : DB cameras row (dict)
#     roi_polygon  : np.array shape (N,2) int32, or empty array to disable ROI filtering
#     preview_queue: multiprocessing.Queue — preview frame পাঠানোর জন্য
#                    None হলে preview skip

#     Return:
#         True  — video সম্পূর্ণ process হয়েছে (file stream শেষ হয়েছে স্বাভাবিকভাবে)
#         False — কোনো error হয়েছে বা stream খোলা যায়নি
#     """
#     cam_id   = camera_info["id"]
#     cam_name = camera_info.get("name", f"Camera-{cam_id}")
#     stream   = camera_info["stream_url"]
#     TAG      = f"[CAM-{cam_id} | {cam_name}]"

#     map_folder = CAMERA_FOLDER_MAP.get(cam_id, str(cam_id))
#     try:
#         db_camera_id = int(map_folder)
#     except ValueError:
#         db_camera_id = cam_id

#     is_file  = _is_file_stream(stream)   # ← file নাকি RTSP

#     print(f"{TAG} Starting... ({'file' if is_file else 'RTSP'})")

#     # Determine validations save directory under uploads for this camera.
#     # Prefer finding a date folder in the stream path (when processing a file),
#     # otherwise use today's compact date format `YYYYMMDD`.
#     folder_name = CAMERA_FOLDER_MAP.get(cam_id, str(cam_id))
#     base_upload = Path(UPLOAD_ROOT) / folder_name
#     base_upload.mkdir(parents=True, exist_ok=True)

#     save_dir = None
#     if is_file:
#         try:
#             stream_path = Path(stream)
#             # Search parents for a date-like folder (YYYYMMDD or YYYY-MM-DD)
#             for anc in [stream_path.parent] + list(stream_path.parents):
#                 if re.match(r'^\d{8}$', anc.name) or re.match(r'^\d{4}-\d{2}-\d{2}$', anc.name):
#                     # if anc is the `validations` folder, go up one to get date folder
#                     if anc.name == 'validations':
#                         continue
#                     date_folder = anc
#                     save_dir = date_folder / 'validations'
#                     break
#         except Exception:
#             save_dir = None

#     if save_dir is None:
#         today_compact = date.today().strftime("%Y%m%d")
#         save_dir = base_upload / today_compact / 'validations'

#     save_dir.mkdir(parents=True, exist_ok=True)
#     clip_dir_path = save_dir / 'clips'
#     clip_dir_path.mkdir(parents=True, exist_ok=True)

#     # Keep legacy `vio_dir` name (string) used throughout the codebase
#     vio_dir = str(save_dir)
#     clip_dir = str(clip_dir_path)

#     cap = None
#     clip_manager = None
#     counts = {}

#     # ── return value ──
#     # file stream → শেষ পর্যন্ত পড়লে True
#     # RTSP       → loop ভাঙলে (KeyboardInterrupt / ESC) True
#     # error      → False
#     completed_successfully = False

#     try:
#         # ── Stream open ──
#         cap = _open_stream(stream)
#         if not cap.isOpened():
#             print(f"{TAG} ✗ Cannot open: {stream}")
#             set_camera_status(cam_id, "offline")
#             return False

#         set_camera_status(cam_id, "active")

#         fps = cap.get(cv2.CAP_PROP_FPS)
#         if fps <= 0 or np.isnan(fps):
#             fps = float(camera_info.get("frame_rate", 25))

#         total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if is_file else -1
#         if total_frames > 0:
#             print(f"{TAG} Video: {total_frames} frames @ {fps:.1f} fps")

#         # ── Models ──
#         print(f"{TAG} Loading models...")
#         model        = YOLO(VEHICLE_MODEL)
#         plate_model  = YOLO(PLATE_MODEL)
#         tracker      = Sort(max_age=20, min_hits=3, iou_threshold=0.2)
#         clip_manager = ClipManager(fps, (MONITOR_WIDTH, MONITOR_HEIGHT), clip_dir)

#         # ── State ──
#         counts              = {v: 0 for v in VEHICLE_CLASSES.values()}
#         counted_ids         = set()
#         speed_violation_ids = set()
#         track_plate_buffer  = {}
#         track_last_position = {}
#         track_speed_history = {}
#         track_color_buffer  = {}
#         db_logged_tracks    = set()
#         active_tracks       = {}

#         # ── Signal state ──
#         _displayed_signal = "unknown"
#         _pending_signal   = None
#         _pending_since    = 0.0
#         last_signal_poll  = 0.0

#         # ── RTSP reconnect (file এ প্রযোজ্য নয়) ──
#         MAX_RECONNECT     = 10
#         RECONNECT_DELAY   = 3.0
#         reconnect_count   = 0
#         consecutive_fails = 0

#         # file stream: কতবার পরপর fail হলে EOF ধরবে
#         MAX_CONSEC_FAILS_FILE = 5
#         # RTSP: কতবার fail হলে reconnect করবে
#         MAX_CONSEC_FAILS_RTSP = 30

#         # ── Preview interval ──
#         PREVIEW_EVERY = 10
#         frame_count   = 0

#         print(f"{TAG} ✅ Ready. Processing...")
        
#         ocr_frame_buffer = deque(maxlen=5)

#         while True:
#             ret, frame = cap.read()

#             # ── Stream fail / EOF handling ──────────────────────
#             if not ret:
#                 consecutive_fails += 1

#                 if is_file:
#                     # File stream: কয়েকবার fail মানেই EOF
#                     if consecutive_fails >= MAX_CONSEC_FAILS_FILE:
#                         print(f"{TAG} ✅ Video ended (EOF). Frames processed: {frame_count}")
#                         completed_successfully = True
#                         break
#                     time.sleep(0.01)
#                     continue

#                 else:
#                     # RTSP: reconnect logic
#                     if consecutive_fails >= MAX_CONSEC_FAILS_RTSP:
#                         if reconnect_count >= MAX_RECONNECT:
#                             print(f"{TAG} ✗ Max reconnects reached. Stopping.")
#                             break
#                         reconnect_count  += 1
#                         consecutive_fails = 0
#                         print(f"{TAG} 🔄 Reconnecting ({reconnect_count}/{MAX_RECONNECT})...")
#                         cap.release()
#                         time.sleep(RECONNECT_DELAY)
#                         cap = _open_stream(stream)
#                         if not cap.isOpened():
#                             set_camera_status(cam_id, "offline")
#                             continue
#                         print(f"{TAG} ✅ Reconnected.")
#                         set_camera_status(cam_id, "active")
#                     else:
#                         time.sleep(0.03)
#                     continue

#             consecutive_fails = 0
#             if not is_file:
#                 reconnect_count = 0

#             frame_count += 1

#             # ── Progress log (file stream, প্রতি 500 frame) ──
#             if is_file and total_frames > 0 and frame_count % 500 == 0:
#                 pct = frame_count / total_frames * 100
#                 print(f"{TAG} Progress: {frame_count}/{total_frames} ({pct:.1f}%)")

#             frame_resized = cv2.resize(frame, (MONITOR_WIDTH, MONITOR_HEIGHT))

#             # ── Signal poll ──
#             now = time.time()
#             if now - last_signal_poll >= _SIGNAL_POLL_INTERVAL:
#                 raw_signal       = get_camera_signal(cam_id)
#                 last_signal_poll = now
#                 clip_is_active   = clip_manager.has_active_clips()

#                 if not clip_is_active:
#                     if raw_signal == _displayed_signal:
#                         _pending_signal = None
#                     else:
#                         if _pending_signal != raw_signal:
#                             _pending_signal = raw_signal
#                             _pending_since  = now
#                         elif now - _pending_since >= _SIGNAL_DEBOUNCE_SEC:
#                             print(f"{TAG} Signal: {_displayed_signal} → {_pending_signal}")
#                             _displayed_signal = _pending_signal
#                             _pending_signal   = None

#             sig_color, is_red, sig_text = _SIGNAL_MAP[_displayed_signal]

#             # ── Draw ROI + label ──
#             if roi_polygon is not None and roi_polygon.size >= 3:
#                 cv2.polylines(frame_resized, [roi_polygon], isClosed=True,
#                               color=sig_color, thickness=2)
#             # cv2.putText(frame_resized, f"{cam_name} | {sig_text}",
#             #             (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, sig_color, 2)

#             # ── Vehicle detection ──
#             detections = []
#             for r in model(frame_resized, verbose=False):
#                 for box in r.boxes:
#                     cls = int(box.cls[0])
#                     if cls in VEHICLE_CLASSES:
#                         x1, y1, x2, y2 = map(int, box.xyxy[0])
#                         detections.append([x1, y1, x2, y2, float(box.conf[0]), cls])

#             # ── Plate detection ──
#             current_plate_boxes = []
#             for pr in plate_model(frame_resized, verbose=False):
#                 for pbox in pr.boxes:
#                     px1, py1, px2, py2 = map(int, pbox.xyxy[0])
#                     pconf = float(pbox.conf[0])
#                     if pconf < PLATE_CONF_TH:
#                         continue
#                     if frame_resized[py1:py2, px1:px2].size == 0:
#                         continue
#                     current_plate_boxes.append((px1, py1, px2, py2, pconf))
#                     cv2.rectangle(frame_resized, (px1, py1), (px2, py2), (255, 0, 255), 2)

#             # ── SORT tracking ──
#             tracks = tracker.update(
#                 np.array([d[:5] for d in detections])
#             ) if detections else []

#             current_track_ids = set()

#             for track in tracks:
#                 x1, y1, x2, y2, track_id = map(int, track)

#                 cls_name = None
#                 for d in detections:
#                     if abs(x1 - d[0]) < 50 and abs(y1 - d[1]) < 50:
#                         cls_name = VEHICLE_CLASSES[d[5]]; break

#                 if cls_name is None and track_id not in active_tracks:
#                     continue

#                 center_pt  = ((x1 + x2) // 2, (y1 + y2) // 2)
#                 corners    = [(x1,y1),(x2,y1),(x2,y2),(x1,y2), center_pt]
#                 if roi_polygon is not None and roi_polygon.size >= 3:
#                     inside_roi = any(
#                         cv2.pointPolygonTest(
#                             roi_polygon, (float(p[0]), float(p[1])), False) >= 0
#                         for p in corners
#                     )
#                 else:
#                     inside_roi = True

#                 buf = update_plate_buffer(
#                     track_id, (x1, y1, x2, y2),
#                     current_plate_boxes, frame_resized,
#                     plate_model, track_plate_buffer,
#                     max_age_seconds=3.0
#                 )

#                 # ── Color (একবার per track) ──
#                 if track_id not in track_color_buffer:
#                     color_name, _ = _color_detector.detect(frame_resized[y1:y2, x1:x2])
#                     if color_name != "UNKNOWN":
#                         track_color_buffer[track_id] = color_name
#                 vehicle_color = track_color_buffer.get(track_id, "UNKNOWN")

#                 # ── Frame label ──
#                 if buf and buf["number"]:
#                     display = buf["number"]
#                     if buf["vtype"] not in ("unknown", ""):
#                         display = f"{buf['vtype']} {buf['number']}"
#                     display = f"{vehicle_color} | {display}"
#                     cv2.putText(frame_resized, display,
#                                 (x1, y2 + 18),
#                                 cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2)

#                 # ── Speed ──
#                 speed_kmph = 0.0
#                 now_t = time.time()
#                 if track_id in track_last_position:
#                     lx, ly, lt = track_last_position[track_id]
#                     dt     = max(now_t - lt, 1e-3)
#                     dist_m = np.sqrt(
#                         (center_pt[0]-lx)**2 + (center_pt[1]-ly)**2
#                     ) / PIXELS_PER_METER
#                     speed_kmph = (dist_m / dt) * 3.6
#                 track_last_position[track_id] = (center_pt[0], center_pt[1], now_t)

#                 hist = track_speed_history.setdefault(track_id, [])
#                 hist.append(speed_kmph)
#                 if len(hist) > 5: hist.pop(0)
#                 speed_kmph = min(sum(hist) / len(hist), 260.0)

#                 # cv2.putText(frame_resized, f"{speed_kmph:.1f} km/h",
#                 #             (x1, y2 + 36),
#                 #             cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
                
#                 cv2.putText(frame_resized, f"{speed_kmph:.1f} km/h",
#                             (x2 - 80, y1 - 10),                     # ← উপরে ডান পাশে
#                             cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2)    # ← green color

#                 if inside_roi:
#                     if track_id not in counted_ids:
#                         counted_ids.add(track_id)
#                         if cls_name: counts[cls_name] += 1

#                     stored_cls = cls_name or (
#                         active_tracks[track_id][1]
#                         if track_id in active_tracks else "vehicle"
#                     )

#                     # detected_plates
#                     if buf and track_id not in db_logged_tracks:
#                         db_logged_tracks.add(track_id)
#                         v_path = os.path.join(vio_dir, f"detected_{track_id}_vehicle.jpg")
#                         p_path = os.path.join(vio_dir, f"detected_{track_id}_plate.jpg")
#                         vcrop  = frame_resized[y1:y2, x1:x2]
#                         if vcrop.size > 0:
#                             cv2.imwrite(v_path, vcrop)
#                         cv2.imwrite(p_path, buf["crop"])
#                         insert_detected_plate(
#                             camera_id=db_camera_id, track_id=track_id,
#                             plate_number=buf["number"], vehicle_type=buf["vtype"],
#                             vehicle_class=stored_cls,
#                             plate_img_path=_normalize_db_path(p_path),
#                             vehicle_img_path=_normalize_db_path(v_path),
#                             confidence=buf["score"], vehicle_color=vehicle_color
#                         )
#                     ocr_frame_buffer.append(frame_resized.copy())
#                     # Speed violation
#                     if speed_kmph > SPEED_LIMIT_KMPH and track_id not in speed_violation_ids and buf:
#                         speed_violation_ids.add(track_id)
#                         vcrop     = frame_resized[y1:y2, x1:x2]
#                         v_path    = os.path.join(vio_dir, f"s{track_id}_vehicle.jpg")
#                         p_path    = os.path.join(vio_dir, f"s{track_id}_plate.jpg")
#                         clip_path = clip_manager.start_clip(track_id + 100000)
#                         clip_path = _normalize_db_path(clip_path)
#                         print(f"{TAG} 🎬 SPEED | track:{track_id} | {speed_kmph:.1f}")
#                         if vcrop.size > 0: cv2.imwrite(v_path, vcrop)
#                         cv2.imwrite(p_path, buf["crop"])


#                         results_ocr = []

#                         for frm in ocr_frame_buffer:
#                             d, t = find_date_time(frm)
#                             if d is not None and t is not None:
#                                 results_ocr.append(f"{d} {t}")
                        
#                         if results_ocr:
#                             vote = Counter(results_ocr)
#                             best_timestamp, count = vote.most_common(1)[0]
#                         else:
#                             best_timestamp=None
                      
#                         insert_speed_violation(
#                             camera_id=db_camera_id, track_id=track_id,
#                             plate_number=buf["number"], vehicle_type=buf["vtype"],
#                             vehicle_class=stored_cls,
#                             plate_img_path=_normalize_db_path(p_path),
#                             vehicle_img_path=_normalize_db_path(v_path),
#                             confidence=buf["score"], speed_kmph=speed_kmph,
#                             speed_limit=SPEED_LIMIT_KMPH, clip_path=clip_path,
#                             vehicle_color=vehicle_color,created_at=best_timestamp
#                         )
#                         cv2.putText(frame_resized, "SPEED!",
#                                     (x1, y1-28), cv2.FONT_HERSHEY_SIMPLEX,
#                                     0.6, (0, 140, 255), 2)

#                     active_tracks[track_id] = [(x1, y1, x2, y2), stored_cls]
#                 else:
#                     if track_id in active_tracks:
#                         active_tracks[track_id][0] = (x1, y1, x2, y2)

#                 current_track_ids.add(track_id)

#             # ── Stale track cleanup ──
#             for tid in list(active_tracks.keys()):
#                 if tid not in current_track_ids:
#                     del active_tracks[tid]
#                     track_plate_buffer.pop(tid, None)
#                     track_last_position.pop(tid, None)
#                     track_speed_history.pop(tid, None)
#                     track_color_buffer.pop(tid, None)
#                     clear_vote_store(tid)

#             # ── Draw boxes ──
#             for tid, (bbox, cname) in active_tracks.items():
#                 bx1, by1, bx2, by2 = bbox
#                 pk = "+" if tid in track_plate_buffer else ""
#                 cv2.rectangle(frame_resized, (bx1, by1), (bx2, by2), (0, 255, 255), 2)
#                 # cv2.putText(frame_resized, f"{cname} #{tid}{pk}",
#                 #             (bx1, by1-16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0,255,255), 1)

#             # ── Clip ──
#             clip_manager.push_frame(frame_resized)

#             # ── Preview → main process ──
#             if preview_queue is not None and frame_count % PREVIEW_EVERY == 0:
#                 preview = cv2.resize(frame_resized, (640, 360))
#                 try:
#                     while not preview_queue.empty():
#                         try: preview_queue.get_nowait()
#                         except Exception: break
#                     preview_queue.put_nowait((cam_id, cam_name, preview))
#                 except Exception:
#                     pass

#             if cv2.waitKey(1) & 0xFF == 27:
#                 # ESC চাপলে — file stream হলে incomplete, RTSP হলে graceful stop
#                 if is_file:
#                     print(f"{TAG} ESC pressed — video interrupted.")
#                     completed_successfully = False
#                 else:
#                     completed_successfully = True
#                 break

#     except KeyboardInterrupt:
#         # file stream interrupt → incomplete
#         completed_successfully = not is_file

#     except Exception as e:
#         import traceback
#         print(f"{TAG} Error: {e}")
#         traceback.print_exc()
#         completed_successfully = False

#     finally:
#         if cap is not None:
#             cap.release()
#         cv2.destroyAllWindows()
#         if clip_manager is not None:
#             clip_manager.release_all()
#         set_camera_status(cam_id, "inactive")
#         print(f"{TAG} Stopped. Counts: {counts}")

#     return completed_successfully



import cv2
import time
import os
import re
import threading  # <-- মাল্টি-থ্রেডিং এর জন্য যুক্ত করা হয়েছে
import cvzone
import math
from pathlib import Path
from datetime import date, datetime
import numpy as np
from ultralytics import YOLO
from sort import Sort
from clip import ClipManager
from collections import deque, Counter
from ocr import find_date_time
from config import (
    MONITOR_WIDTH, MONITOR_HEIGHT,
    PLATE_CONF_TH, SPEED_LIMIT_KMPH, PIXELS_PER_METER,
    VEHICLE_MODEL, PLATE_MODEL, VEHICLE_CLASSES,
    UPLOAD_ROOT, CAMERA_FOLDER_MAP
)
from helpers import update_plate_buffer, clear_vote_store, VehicleColorDetector

from helmet_detector.helmet_detector_vide import detect_helmet
from database import (
    insert_detected_plate,
    insert_speed_violation, insert_helmet_violation,
    set_camera_status, get_camera_signal
)

_color_detector = VehicleColorDetector()

_SIGNAL_MAP = {
    "red":      ((0,   0,   255), True,    "RED"),
    "green":   ((0,   255, 0),   False,   "GREEN"),
    "orange":  ((0,   165, 255), False,   "ORANGE"),
    "unknown": ((128, 128, 128), False,   "NO SIGNAL"),
}

_SIGNAL_POLL_INTERVAL = 2
_SIGNAL_DEBOUNCE_SEC  = 3.0
HELEMET_CONF_THRESHOLD = 0.5 

classNames = ['With Helmet', 'Without Helmet']

# ─────────────────────────────────────────────────────
# RTSP/file stream open
# ─────────────────────────────────────────────────────
def _open_stream(stream_url: str) -> cv2.VideoCapture:
    is_rtsp = stream_url.lower().startswith("rtsp://")

    if is_rtsp:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            "rtsp_transport;udp|"
            "stimeout;5000000"
        )
        cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    else:
        cap = cv2.VideoCapture(stream_url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    return cap


def _is_file_stream(stream_url: str) -> bool:
    lower = stream_url.lower()
    return not lower.startswith("rtsp://") and not lower.startswith("http")


def _normalize_db_path(path: str) -> str:
    try:
        rel = Path(path).relative_to(UPLOAD_ROOT)
        return str(Path("uploads") / rel)
    except Exception:
        try:
            return os.path.relpath(path)
        except Exception:
            return path


# ─────────────────────────────────────────────────────
# Asynchronous OCR & DB Logger Worker
# ─────────────────────────────────────────────────────
def _bg_ocr_and_insert_violation(ocr_frames, db_camera_id, track_id, plate_number, 
                                 vehicle_type, stored_cls, p_path, v_path, 
                                 confidence, speed_kmph, speed_limit, clip_path, 
                                 vehicle_color, TAG):
    """
    ব্যাকগ্রাউন্ড থ্রেড ফাংশন: এটি মূল ভিডিও প্রসেসিং লুপের বাইরে সমান্তরালভাবে (parallel) 
    OCR প্রসেস করবে এবং ডাটাবেজে ডাটা পুশ করবে, যার ফলে মেইন লুপে কোনো ল্যাগ হবে না।
    """
    try:
        results_ocr = []
        # বাফারের ফ্রেমগুলোর ওপর OCR চালানো হচ্ছে
        for frm in ocr_frames:
            d, t = find_date_time(frm)
            if d is not None and t is not None:
                results_ocr.append(f"{d} {t}")
        
        if results_ocr:
            vote = Counter(results_ocr)
            best_timestamp, count = vote.most_common(1)[0]
        else:
            best_timestamp = None

        # ডাটাবেজে স্পিড ভায়োলেশন এন্ট্রি ইনসার্ট
        insert_speed_violation(
            camera_id=db_camera_id, track_id=track_id,
            plate_number=plate_number, vehicle_type=vehicle_type,
            vehicle_class=stored_cls,
            plate_img_path=_normalize_db_path(p_path),
            vehicle_img_path=_normalize_db_path(v_path),
            confidence=confidence, speed_kmph=speed_kmph,
            speed_limit=speed_limit, clip_path=clip_path,
            vehicle_color=vehicle_color, created_at=best_timestamp
        )
        print(f"{TAG} [BG-THREAD] Async OCR & Speed Violation Logged Successfully for Track {track_id}")
    except Exception as e:
        print(f"{TAG} [BG-THREAD] Error in background OCR thread: {e}")


# ─────────────────────────────────────────────────────
# Main camera worker
# ─────────────────────────────────────────────────────
def run_camera(camera_info: dict, roi_polygon: np.ndarray, preview_queue=None):
    cam_id   = camera_info["id"]
    cam_name = camera_info.get("name", f"Camera-{cam_id}")
    stream   = camera_info["stream_url"]
    TAG      = f"[CAM-{cam_id} | {cam_name}]"

    map_folder = CAMERA_FOLDER_MAP.get(cam_id, str(cam_id))
    try:
        db_camera_id = int(map_folder)
    except ValueError:
        db_camera_id = cam_id

    is_file  = _is_file_stream(stream)

    print(f"{TAG} Starting... ({'file' if is_file else 'RTSP'})")

    folder_name = CAMERA_FOLDER_MAP.get(cam_id, str(cam_id))
    base_upload = Path(UPLOAD_ROOT) / folder_name
    base_upload.mkdir(parents=True, exist_ok=True)

    save_dir = None
    if is_file:
        try:
            stream_path = Path(stream)
            for anc in [stream_path.parent] + list(stream_path.parents):
                if re.match(r'^\d{8}$', anc.name) or re.match(r'^\d{4}-\d{2}-\d{2}$', anc.name):
                    if anc.name == 'validations':
                        continue
                    date_folder = anc
                    save_dir = date_folder / 'validations'
                    break
        except Exception:
            save_dir = None

    if save_dir is None:
        today_compact = date.today().strftime("%Y%m%d")
        save_dir = base_upload / today_compact / 'validations'

    save_dir.mkdir(parents=True, exist_ok=True)
    clip_dir_path = save_dir / 'clips'
    clip_dir_path.mkdir(parents=True, exist_ok=True)

    vio_dir = str(save_dir)
    clip_dir = str(clip_dir_path)

    cap = None
    clip_manager = None
    counts = {}
    completed_successfully = False

    try:
        cap = _open_stream(stream)
        if not cap.isOpened():
            print(f"{TAG} ✗ Cannot open: {stream}")
            set_camera_status(cam_id, "offline")
            return False

        set_camera_status(cam_id, "active")

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0 or np.isnan(fps):
            fps = float(camera_info.get("frame_rate", 25))

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if is_file else -1
        if total_frames > 0:
            print(f"{TAG} Video: {total_frames} frames @ {fps:.1f} fps")

        print(f"{TAG} Loading models...")
        model        = YOLO(VEHICLE_MODEL)
        plate_model  = YOLO(PLATE_MODEL)
        tracker      = Sort(max_age=20, min_hits=3, iou_threshold=0.2)
        clip_manager = ClipManager(fps, (MONITOR_WIDTH, MONITOR_HEIGHT), clip_dir)

        counts              = {v: 0 for v in VEHICLE_CLASSES.values()}
        counted_ids         = set()
        speed_violation_ids = set()
        helmet_violation_ids = set()
        track_plate_buffer  = {}
        track_last_position = {}
        track_speed_history = {}
        track_color_buffer  = {}
        db_logged_tracks    = set()
        active_tracks       = {}

        _displayed_signal = "unknown"
        _pending_signal   = None
        _pending_since    = 0.0
        last_signal_poll  = 0.0

        MAX_RECONNECT     = 10
        RECONNECT_DELAY   = 3.0
        reconnect_count   = 0
        consecutive_fails = 0

        MAX_CONSEC_FAILS_FILE = 5
        MAX_CONSEC_FAILS_RTSP = 30

        PREVIEW_EVERY = 10
        frame_count   = 0

        print(f"{TAG} ✅ Ready. Processing...")
        
        ocr_frame_buffer = deque(maxlen=5)

        while True:
            ret, frame = cap.read()

            if not ret:
                consecutive_fails += 1
                if is_file:
                    if consecutive_fails >= MAX_CONSEC_FAILS_FILE:
                        print(f"{TAG} ✅ Video ended (EOF). Frames processed: {frame_count}")
                        completed_successfully = True
                        break
                    time.sleep(0.01)
                    continue
                else:
                    if consecutive_fails >= MAX_CONSEC_FAILS_RTSP:
                        if reconnect_count >= MAX_RECONNECT:
                            print(f"{TAG} ✗ Max reconnects reached. Stopping.")
                            break
                        reconnect_count  += 1
                        consecutive_fails = 0
                        print(f"{TAG} 🔄 Reconnecting ({reconnect_count}/{MAX_RECONNECT})...")
                        cap.release()
                        time.sleep(RECONNECT_DELAY)
                        cap = _open_stream(stream)
                        if not cap.isOpened():
                            set_camera_status(cam_id, "offline")
                            continue
                        print(f"{TAG} ✅ Reconnected.")
                        set_camera_status(cam_id, "active")
                    else:
                        time.sleep(0.03)
                    continue

            consecutive_fails = 0
            if not is_file:
                reconnect_count = 0

            frame_count += 1

            if is_file and total_frames > 0 and frame_count % 500 == 0:
                pct = frame_count / total_frames * 100
                print(f"{TAG} Progress: {frame_count}/{total_frames} ({pct:.1f}%)")

            frame_resized = cv2.resize(frame, (MONITOR_WIDTH, MONITOR_HEIGHT))

            # ── Signal poll ──
            now = time.time()
            if now - last_signal_poll >= _SIGNAL_POLL_INTERVAL:
                raw_signal       = get_camera_signal(cam_id)
                last_signal_poll = now
                clip_is_active   = clip_manager.has_active_clips()

                if not clip_is_active:
                    if raw_signal == _displayed_signal:
                        _pending_signal = None
                    else:
                        if _pending_signal != raw_signal:
                            _pending_signal = raw_signal
                            _pending_since  = now
                        elif now - _pending_since >= _SIGNAL_DEBOUNCE_SEC:
                            print(f"{TAG} Signal: {_displayed_signal} → {_pending_signal}")
                            _displayed_signal = _pending_signal
                            _pending_signal   = None

            sig_color, is_red, sig_text = _SIGNAL_MAP[_displayed_signal]

            if roi_polygon is not None and roi_polygon.size >= 3:
                cv2.polylines(frame_resized, [roi_polygon], isClosed=True,
                              color=sig_color, thickness=2)

            # ── Vehicle detection ──
            detections = []
            for r in model(frame_resized, verbose=False):
                for box in r.boxes:
                    cls = int(box.cls[0])
                    if cls in VEHICLE_CLASSES:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        detections.append([x1, y1, x2, y2, float(box.conf[0]), cls])

            # ── Plate detection ──
            current_plate_boxes = []
            for pr in plate_model(frame_resized, verbose=False):
                for pbox in pr.boxes:
                    px1, py1, px2, py2 = map(int, pbox.xyxy[0])
                    pconf = float(pbox.conf[0])
                    if pconf < PLATE_CONF_TH:
                        continue
                    if frame_resized[py1:py2, px1:px2].size == 0:
                        continue
                    current_plate_boxes.append((px1, py1, px2, py2, pconf))
                    cv2.rectangle(frame_resized, (px1, py1), (px2, py2), (255, 0, 255), 2)

            # ── SORT tracking ──
            tracks = tracker.update(
                np.array([d[:5] for d in detections])
            ) if detections else []

            current_track_ids = set()

            for track in tracks:
                x1, y1, x2, y2, track_id = map(int, track)

                cls_name = None
                for d in detections:
                    if abs(x1 - d[0]) < 50 and abs(y1 - d[1]) < 50:
                        cls_name = VEHICLE_CLASSES[d[5]]; break

                if cls_name is None and track_id not in active_tracks:
                    continue

                center_pt  = ((x1 + x2) // 2, (y1 + y2) // 2)
                corners    = [(x1,y1),(x2,y1),(x2,y2),(x1,y2), center_pt]
                if roi_polygon is not None and roi_polygon.size >= 3:
                    inside_roi = any(
                        cv2.pointPolygonTest(
                            roi_polygon, (float(p[0]), float(p[1])), False) >= 0
                        for p in corners
                    )
                else:
                    inside_roi = True

                buf = update_plate_buffer(
                    track_id, (x1, y1, x2, y2),
                    current_plate_boxes, frame_resized,
                    plate_model, track_plate_buffer,
                    max_age_seconds=3.0
                )

                if track_id not in track_color_buffer:
                    color_name, _ = _color_detector.detect(frame_resized[y1:y2, x1:x2])
                    if color_name != "UNKNOWN":
                        track_color_buffer[track_id] = color_name
                vehicle_color = track_color_buffer.get(track_id, "UNKNOWN")

                if buf and buf["number"]:
                    display = buf["number"]
                    if buf["vtype"] not in ("unknown", ""):
                        display = f"{buf['vtype']} {buf['number']}"
                    display = f"{vehicle_color} | {display}"
                    cv2.putText(frame_resized, display,
                                (x1, y2 + 18),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2)

                # ── Speed calculation ──
                speed_kmph = 0.0
                now_t = time.time()
                if track_id in track_last_position:
                    lx, ly, lt = track_last_position[track_id]
                    dt     = max(now_t - lt, 1e-3)
                    dist_m = np.sqrt(
                        (center_pt[0]-lx)**2 + (center_pt[1]-ly)**2
                    ) / PIXELS_PER_METER
                    speed_kmph = (dist_m / dt) * 3.6
                track_last_position[track_id] = (center_pt[0], center_pt[1], now_t)

                hist = track_speed_history.setdefault(track_id, [])
                hist.append(speed_kmph)
                if len(hist) > 5: hist.pop(0)
                speed_kmph = min(sum(hist) / len(hist), 260.0)
                
                cv2.putText(frame_resized, f"{speed_kmph:.1f} km/h",
                            (x2 - 80, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2)

                if inside_roi:
                    if track_id not in counted_ids:
                        counted_ids.add(track_id)
                        if cls_name: counts[cls_name] += 1

                    stored_cls = cls_name or (
                        active_tracks[track_id][1]
                        if track_id in active_tracks else "vehicle"
                    )

                    # ── Log detected plates ──
                    if buf and track_id not in db_logged_tracks:
                        db_logged_tracks.add(track_id)
                        v_path = os.path.join(vio_dir, f"detected_{track_id}_vehicle.jpg")
                        p_path = os.path.join(vio_dir, f"detected_{track_id}_plate.jpg")
                        vcrop  = frame_resized[y1:y2, x1:x2]
                        if vcrop.size > 0:
                            cv2.imwrite(v_path, vcrop)
                        cv2.imwrite(p_path, buf["crop"])
                        insert_detected_plate(
                            camera_id=db_camera_id, track_id=track_id,
                            plate_number=buf["number"], vehicle_type=buf["vtype"],
                            vehicle_class=stored_cls,
                            plate_img_path=_normalize_db_path(p_path),
                            vehicle_img_path=_normalize_db_path(v_path),
                            confidence=buf["score"], vehicle_color=vehicle_color
                        )

                    # ফ্রেম বাফার ক্রপ সংরক্ষণ (OCR এর ব্যাকলগের জন্য)
                    ocr_frame_buffer.append(frame_resized.copy())

                    # ── Speed violation logic (Multi-threaded OCR) ──
                    if speed_kmph > SPEED_LIMIT_KMPH and track_id not in speed_violation_ids and buf:
                        speed_violation_ids.add(track_id)
                        vcrop     = frame_resized[y1:y2, x1:x2]
                        v_path    = os.path.join(vio_dir, f"s{track_id}_vehicle.jpg")
                        p_path    = os.path.join(vio_dir, f"s{track_id}_plate.jpg")
                        
                        # ক্লিপ তৈরি শুরু
                        clip_path = clip_manager.start_clip(track_id + 100000)
                        clip_path = _normalize_db_path(clip_path)
                        
                        print(f"{TAG} 🎬 SPEED VIOLATION | track:{track_id} | Speed: {speed_kmph:.1f} km/h")
                        if vcrop.size > 0: 
                            cv2.imwrite(v_path, vcrop)
                        cv2.imwrite(p_path, buf["crop"])

                        # প্রধান ফিক্স: এখানে ডিপ কপি করে ফ্রেম বাফার আলাদা থ্রেডে পাঠিয়ে দেওয়া হচ্ছে
                        # যার কারণে লুপে কোনো বাধা ছাড়াই ফ্রেম প্রসেসিং ও ক্লিপ মেকিং চলতে থাকবে।
                        frames_to_process = list(ocr_frame_buffer)
                        
                        ocr_thread = threading.Thread(
                            target=_bg_ocr_and_insert_violation,
                            args=(
                                frames_to_process, db_camera_id, track_id, buf["number"],
                                buf["vtype"], stored_cls, p_path, v_path, buf["score"],
                                speed_kmph, SPEED_LIMIT_KMPH, clip_path, vehicle_color, TAG
                            ),
                            daemon=True # মেইন স্ক্রিপ্ট বন্ধ হলে থ্রেডও নিজে থেকে বন্ধ হবে
                        )
                        ocr_thread.start() # থ্রেড স্টার্ট

                        cv2.putText(frame_resized, "SPEED!",
                                    (x1, y1-28), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.6, (0, 140, 255), 2)
                    
                    # ---------------- Helmet violation for motorbikes only----------------
                    if stored_cls == "motorbike" and track_id not in helmet_violation_ids:
    
                        # 1. Crop the motorbike region from the full 1280×720 frame
                        # vcrop = frame_resized[y1:y2, x1:x2]

                        expand_top = int((y2 - y1) * 0.6)   # 60% extra height
                        ny1 = max(0, y1 - expand_top)
                        ny2 = y2
                        nx1 = max(0, x1)
                        nx2 = min(frame_resized.shape[1], x2)

                        vcrop = frame_resized[ny1:ny2, nx1:nx2]
                                                
                        if vcrop.size > 0:
                            
                            # 2. Run best.pt on the crop
                            #    Model finds helmet/no-helmet boxes INSIDE the crop
                            #    Coordinates returned are relative to vcrop, NOT frame_resized
                            #     vcrop = cv2.resize(
                            #     vcrop,
                            #     None,
                            #     fx=2,
                            #     fy=2,
                            #     interpolation=cv2.INTER_CUBIC
                            # )

                            cv2.imwrite(f"uploads/test_motorbike/debug_crop_{track_id}.jpg", vcrop)
                            helmet_results = detect_helmet(vcrop)

                            for hr in helmet_results:
                                for hbox in hr.boxes:
                                    
                                    hconf = float(hbox.conf[0])
                                    print("hconf result: ", hconf)
                                    # if hconf < HELEMET_CONF_THRESHOLD:  # 0.5 — skip low confidence
                                    #     continue

                                    hcls = int(hbox.cls[0])
                                    print("HCLS value",hcls)
                                    if hcls == 0:   # 0=With Helmet (safe), 1=Without Helmet (violation)
                                        continue

                                    # 3. Box coords are relative to vcrop
                                    hx1, hy1, hx2, hy2 = map(int, hbox.xyxy[0])
                                    hw, hh = hx2 - hx1, hy2 - hy1

                                    # 4. Remap to full frame coordinates
                                    #    vcrop started at (x1, y1) in frame_resized
                                    global_x1 = nx1 + hx1   # ✅ correct
                                    global_y1 = ny1 + hy1   # ✅ correct

                                    # 5. Draw on full frame (using global coords)
                                    cvzone.cornerRect(frame_resized,
                                                    (global_x1, global_y1, hw, hh))
                                    cvzone.putTextRect(
                                        frame_resized,
                                        f'No Helmet {math.ceil(hconf * 100) / 100}',
                                        (max(0, global_x1), max(35, global_y1)),
                                        scale=1, thickness=1
                                    )
                                    hv_path = os.path.join(vio_dir, f"h{track_id}_vehicle.jpg")
                                    hp_path = os.path.join(vio_dir, f"h{track_id}_plate.jpg")
                                    if vcrop.size > 0:
                                        cv2.imwrite(hv_path, vcrop)
                                    if buf and buf["crop"] is not None:
                                        cv2.imwrite(hp_path, buf["crop"])

                                    clip_path = clip_manager.start_clip(track_id + 200000)
                                    clip_path = _normalize_db_path(clip_path)

                                    insert_helmet_violation(
                                        camera_id=db_camera_id,
                                        track_id=track_id,
                                        plate_number=buf["number"] if buf else None,
                                        vehicle_type=buf["vtype"] if buf else None,
                                        vehicle_class=stored_cls,
                                        plate_img_path=_normalize_db_path(hp_path) if buf else None,
                                        vehicle_img_path=_normalize_db_path(hv_path),
                                        confidence=hconf,
                                        clip_path=clip_path,
                                        vehicle_color=vehicle_color
                                    )
                                    helmet_violation_ids.add(track_id)
                                    # break
                                if track_id in helmet_violation_ids:
                                    break

                    active_tracks[track_id] = [(x1, y1, x2, y2), stored_cls]
                else:
                    if track_id in active_tracks:
                        active_tracks[track_id][0] = (x1, y1, x2, y2)

                current_track_ids.add(track_id)

            # ── Stale track cleanup ──
            for tid in list(active_tracks.keys()):
                if tid not in current_track_ids:
                    del active_tracks[tid]
                    track_plate_buffer.pop(tid, None)
                    track_last_position.pop(tid, None)
                    track_speed_history.pop(tid, None)
                    track_color_buffer.pop(tid, None)
                    clear_vote_store(tid)

            # ── Draw boxes ──
            for tid, (bbox, cname) in active_tracks.items():
                bx1, by1, bx2, by2 = bbox
                cv2.rectangle(frame_resized, (bx1, by1), (bx2, by2), (0, 255, 255), 2)

            # ── Push frame into Clip Manager ──
            clip_manager.push_frame(frame_resized)

            # ── Preview → main process ──
            if preview_queue is not None and frame_count % PREVIEW_EVERY == 0:
                preview = cv2.resize(frame_resized, (640, 360))
                try:
                    while not preview_queue.empty():
                        try: preview_queue.get_nowait()
                        except Exception: break
                    preview_queue.put_nowait((cam_id, cam_name, preview))
                except Exception:
                    pass

            if cv2.waitKey(1) & 0xFF == 27:
                if is_file:
                    print(f"{TAG} ESC pressed — video interrupted.")
                    completed_successfully = False
                else:
                    completed_successfully = True
                break

    except KeyboardInterrupt:
        completed_successfully = not is_file

    except Exception as e:
        import traceback
        print(f"{TAG} Error: {e}")
        traceback.print_exc()
        completed_successfully = False

    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
        if clip_manager is not None:
            clip_manager.release_all()
        set_camera_status(cam_id, "inactive")
        print(f"{TAG} Stopped. Counts: {counts}")

    return completed_successfully



