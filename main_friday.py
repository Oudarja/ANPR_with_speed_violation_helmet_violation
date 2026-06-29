# =====================================================
# main.py — Multi-camera launcher
# Features:
#   - DB থেকে camera + ROI load
#   - RTSP stream support (reconnect সহ)
#   - Live preview window (সব camera একসাথে)
#   - Ctrl+C দিয়ে graceful shutdown
#   - Midnight এ automatically নতুন date folder এ shift
# =====================================================
import cv2
import time
import os
import numpy as np
import multiprocessing as mp
import threading
from pathlib import Path
from datetime import date, datetime, timedelta
from database      import init_db, get_active_cameras, print_summary
from camera_worker import run_camera
from config        import (
    MONITOR_WIDTH, MONITOR_HEIGHT,
    UPLOAD_ROOT, UPLOAD_POLL_INTERVAL,
    VIDEO_RETRY_SECONDS, VIDEO_STABLE_CHECK_SECONDS,
    VIDEO_EXTENSIONS, CAMERA_FOLDER_MAP, PROCESS_TODAY_VIDEOS,
)


# ─────────────────────────────────────────────────────
# Midnight folder creator — background thread
# রাত ১২টা বাজলেই পরের দিনের folder তৈরি করে দেয়
# ─────────────────────────────────────────────────────
def _midnight_folder_creator(camera_rois: list, upload_root: Path, stop_event: threading.Event):
    """
    প্রতি রাত ১২টায় (00:00:00) সব camera র জন্য
    নতুন দিনের date folder + validations subfolder তৈরি করে।
    stop_event set হলে thread বন্ধ হয়।
    """
    while not stop_event.is_set():
        now = datetime.now()
        tomorrow_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seconds_until_midnight = (tomorrow_midnight - now).total_seconds()

        print(f"[FolderCreator] পরের folder তৈরি হবে "
              f"{tomorrow_midnight.strftime('%Y-%m-%d %H:%M:%S')} তে "
              f"({seconds_until_midnight:.0f}s পরে)")

        # midnight পর্যন্ত wait; stop_event set হলে আগেই বেরিয়ে আসো
        stop_event.wait(timeout=seconds_until_midnight)

        if stop_event.is_set():
            break

        # Midnight হয়ে গেছে — নতুন দিনের folder বানাও
        new_date_compact = date.today().strftime("%Y%m%d")

        for cam, _ in camera_rois:
            folder_name = CAMERA_FOLDER_MAP.get(cam["id"], str(cam["id"]))
            folder      = upload_root / folder_name
            validations = folder / new_date_compact / "validations"
            validations.mkdir(parents=True, exist_ok=True)
            print(f"[FolderCreator] ✓ CAM-{cam['id']} → {validations}")

        # ১ সেকেন্ড pause — একই midnight এ double-fire এড়াতে
        stop_event.wait(timeout=1.0)


# ─────────────────────────────────────────────────────
# Worker process entry
# ─────────────────────────────────────────────────────
def _worker_entry(camera_info: dict, roi_list, preview_queue):
    roi_polygon = np.array(roi_list, dtype=np.int32)
    try:
        _camera_folder_worker(camera_info, roi_polygon, preview_queue)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        print(f"[CAM-{camera_info['id']}] Worker crashed: {e}")
        traceback.print_exc()


def _video_is_pending(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and not path.stem.endswith("_done")
    )


def _wait_for_stable_file(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return False
    # ──────────────────────────────────────────────────────────
    # ফাইলের নাম থেকে টাইম বের করে চেক করা (OS mtime বাগ বাইপাস)
    # ──────────────────────────────────────────────────────────
    try:
        filename = path.name
        if filename.startswith('S') and len(filename) >= 15:
            time_str  = filename[1:15]
            file_time = datetime.strptime(time_str, "%Y%m%d%H%M%S")
            file_age_seconds = (datetime.now() - file_time).total_seconds()
            if file_age_seconds > 120:
                return True
    except Exception:
        pass
    # Fallback to standard check
    checks     = 2
    prev_size  = st.st_size
    prev_mtime = st.st_mtime
    for _ in range(checks):
        time.sleep(VIDEO_STABLE_CHECK_SECONDS)
        try:
            st2 = path.stat()
        except OSError:
            return False
        if st2.st_size == 0:
            return False
        if st2.st_size == prev_size and st2.st_mtime == prev_mtime:
            return True
        prev_size  = st2.st_size
        prev_mtime = st2.st_mtime
    try:
        age = time.time() - path.stat().st_mtime
        return age >= VIDEO_STABLE_CHECK_SECONDS
    except OSError:
        return False


def _probe_video_file(path: Path) -> bool:
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            cap.release()
            print(f"    Cannot open video: {path.name}")
            return False

        width       = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps         = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if width <= 0 or height <= 0:
            cap.release()
            print(f"    Invalid video dimensions: {width}x{height}")
            return False

        if fps <= 0:
            cap.release()
            print(f"    Invalid FPS: {fps}")
            return False

        if frame_count <= 0:
            cap.release()
            print(f"    No frames detected in video")
            return False

        frames_read = 0
        for _ in range(min(5, frame_count)):
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.release()
                print(f"    Failed to read frame {frames_read+1}")
                return False
            frames_read += 1

        cap.release()
        print(f"    ✓ Video OK: {width}x{height} @{fps}fps, {frame_count} frames")
        return True

    except Exception as e:
        print(f"    Error probing video: {e}")
        return False


def _mark_done(path: Path) -> Path:
    dest = path.with_name(f"{path.stem}_done{path.suffix}")
    path.rename(dest)
    return dest


def _parse_date_folder(folder: Path):
    name = folder.name
    try:
        return date.fromisoformat(name)
    except ValueError:
        pass
    try:
        return date.fromisoformat(f"{name[:4]}-{name[4:6]}-{name[6:8]}")
    except Exception:
        return None


def _find_pending_videos(upload_folder: Path):
    """
    প্রতিবার call এ date.today() fresh — রাত ১২টা পার হলে
    automatically নতুন দিনের folder এ shift হয়।
    """
    current_today = date.today()
    pending = []

    if not upload_folder.exists():
        return pending

    for child in sorted(upload_folder.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            folder_date = _parse_date_folder(child)

            # সরাসরি date folder (যেমন: uploads/20260617)
            if folder_date is not None and folder_date == current_today:
                validations_dir = child / "validations"
                sources = [validations_dir, child] if validations_dir.exists() else [child]
                for src in sources:
                    try:
                        pending.extend(
                            sorted(
                                [p for p in src.iterdir() if _video_is_pending(p)],
                                key=lambda p: p.name.lower()
                            )
                        )
                    except (OSError, PermissionError):
                        pass

            # camera id folder এর ভেতর date folder (যেমন: uploads/201/20260617)
            elif folder_date is None:
                for grandchild in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                    if grandchild.is_dir():
                        subfolder_date = _parse_date_folder(grandchild)
                        if subfolder_date is not None and subfolder_date == current_today:
                            validations_dir = grandchild / "validations"
                            sources = [validations_dir, grandchild] if validations_dir.exists() else [grandchild]
                            for src in sources:
                                try:
                                    pending.extend(
                                        sorted(
                                            [p for p in src.iterdir() if _video_is_pending(p)],
                                            key=lambda p: p.name.lower()
                                        )
                                    )
                                except (OSError, PermissionError):
                                    print(f"    Warning: Cannot read {src}")

        elif _video_is_pending(child):
            pending.append(child)

    return pending


def _camera_folder_worker(camera_info: dict, roi_polygon: np.ndarray, preview_queue):
    cam_id      = camera_info["id"]
    folder_name = CAMERA_FOLDER_MAP.get(cam_id, str(cam_id))
    upload_folder = Path(UPLOAD_ROOT) / folder_name
    upload_folder.mkdir(parents=True, exist_ok=True)

    print(f"[CAM-{cam_id}] Watching Live (Autoshift at Midnight): {upload_folder}")

    failed_videos = {}  # path -> retry_after_time

    while True:
        try:
            now = time.time()

            all_pending = _find_pending_videos(upload_folder)

            if not all_pending:
                time.sleep(UPLOAD_POLL_INTERVAL)
                continue

            # cooldown এ নেই এমন প্রথম video নাও
            next_video = None
            for p in all_pending:
                if failed_videos.get(p, 0) <= now:
                    next_video = p
                    break

            if next_video is None:
                next_retry = min(failed_videos[p] for p in all_pending if p in failed_videos)
                wait = max(1, min(next_retry - now, UPLOAD_POLL_INTERVAL))
                print(f"[CAM-{cam_id}] all video cooldown  {wait:.0f}s পরে check করব...")
                time.sleep(wait)
                continue

            if not _wait_for_stable_file(next_video):
                print(f"[CAM-{cam_id}] File appears to be still writing: {next_video.name}; will retry shortly")
                failed_videos[next_video] = now + 10
                continue

            print(f"[CAM-{cam_id}] Checking: {next_video.name}")
            if not _probe_video_file(next_video):
                print(f"[CAM-{cam_id}] ✗ Bad video: {next_video.name} — 5 মিনিট পরে retry")
                failed_videos[next_video] = now + VIDEO_RETRY_SECONDS
                continue

            print(f"[CAM-{cam_id}] ▶ Processing: {next_video.name}")
            proc_info = dict(camera_info)
            proc_info["stream_url"] = str(next_video)

            success = run_camera(proc_info, roi_polygon, preview_queue=preview_queue)
            failed_videos.pop(next_video, None)

            if success:
                done_path = _mark_done(next_video)
                print(f"[CAM-{cam_id}] ✓ Done: {done_path.name}")
            else:
                print(f"[CAM-{cam_id}] ✗ Failed: {next_video.name} — after 5 min retry")
                failed_videos[next_video] = now + VIDEO_RETRY_SECONDS

        except KeyboardInterrupt:
            break
        except Exception as e:
            import traceback
            print(f"[CAM-{cam_id}] Exception: {e}")
            traceback.print_exc()
            time.sleep(5)


# ─────────────────────────────────────────────────────
# Preview window — সব camera এর frame একসাথে দেখায়
# ─────────────────────────────────────────────────────
def _build_preview_grid(frames_by_cam: dict, grid_cols: int = 2) -> np.ndarray:
    items = list(frames_by_cam.values())
    if not items:
        return np.zeros((360, 640, 3), dtype=np.uint8)

    CELL_W, CELL_H = 640, 360
    n    = len(items)
    cols = min(grid_cols, n)
    rows = (n + cols - 1) // cols
    grid = np.zeros((rows * CELL_H, cols * CELL_W, 3), dtype=np.uint8)

    for idx, (name, frame) in enumerate(items):
        r  = idx // cols
        c  = idx % cols
        y0, y1 = r * CELL_H, (r+1) * CELL_H
        x0, x1 = c * CELL_W, (c+1) * CELL_W

        if frame is not None:
            cell = cv2.resize(frame, (CELL_W, CELL_H))
        else:
            cell = np.zeros((CELL_H, CELL_W, 3), dtype=np.uint8)
            cv2.putText(cell, f"{name} — No Frame",
                        (20, CELL_H//2), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (100, 100, 100), 2)

        grid[y0:y1, x0:x1] = cell

    for r in range(1, rows):
        cv2.line(grid, (0, r*CELL_H), (cols*CELL_W, r*CELL_H), (60, 60, 60), 1)
    for c in range(1, cols):
        cv2.line(grid, (c*CELL_W, 0), (c*CELL_W, rows*CELL_H), (60, 60, 60), 1)

    return grid


# ─────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────
def main():
    init_db()

    all_cameras = get_active_cameras()
    if not all_cameras:
        print(" No active cameras in DB.")
        return

    print(f"\n {len(all_cameras)} active camera(s) found.")
    print(f" Upload root: {UPLOAD_ROOT}\n")

    upload_root = Path(UPLOAD_ROOT)
    upload_root.mkdir(parents=True, exist_ok=True)

    camera_rois = [(cam, []) for cam in all_cameras]

    # আজকের folder তৈরি করো
    today_compact = date.today().strftime("%Y%m%d")
    for cam, _ in camera_rois:
        folder_name         = CAMERA_FOLDER_MAP.get(cam["id"], str(cam["id"]))
        folder              = upload_root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        validations_compact = folder / today_compact / "validations"
        validations_compact.mkdir(parents=True, exist_ok=True)
        print(f"  [CAM-{cam['id']}] Upload folder: {folder} (today: {validations_compact})")

    # ── Midnight folder creator thread ──
    stop_event = threading.Event()
    folder_creator_thread = threading.Thread(
        target=_midnight_folder_creator,
        args=(camera_rois, upload_root, stop_event),
        name="midnight-folder-creator",
        daemon=True,
    )
    folder_creator_thread.start()
    print("\n[Main] Midnight folder creator thread চালু হয়েছে.")

    # ── Shared preview queue ──
    preview_queue = mp.Queue(maxsize=len(camera_rois) * 3)

    # ── Spawn processes ──
    print(f"\n Starting {len(camera_rois)} camera process(es)...\n")
    processes = []

    for cam, roi_list in camera_rois:
        p = mp.Process(
            target=_worker_entry,
            args=(cam, roi_list, preview_queue),
            name=f"cam-{cam['id']}"
        )
        p.start()
        processes.append((p, cam))
        print(f"  ▶ PID={p.pid} | Camera {cam['id']} — {cam['name']}")
        time.sleep(0.3)

    print("\n[Main] Preview window খুলছে... ESC বা Q চাপলে বন্ধ হবে.\n")

    frames_by_cam = {
        cam["id"]: (cam["name"], None)
        for cam, _ in camera_rois
    }

    cv2.namedWindow("ANPR — Live Preview", cv2.WINDOW_NORMAL)

    try:
        while True:
            drained = 0
            while drained < 20:
                try:
                    cam_id, cam_name, preview = preview_queue.get_nowait()
                    frames_by_cam[cam_id] = (cam_name, preview)
                    drained += 1
                except Exception:
                    break

            grid = _build_preview_grid(frames_by_cam, grid_cols=2)
            cv2.imshow("ANPR — Live Preview", grid)

            key = cv2.waitKey(30) & 0xFF
            if key in (27, ord('q')):
                print("\n[Main] Preview বন্ধ করা হচ্ছে...")
                break

            alive = []
            for p, cam in processes:
                if p.is_alive():
                    alive.append((p, cam))
                else:
                    print(f" {p.name} exited (code={p.exitcode})")
            processes[:] = alive

            if not processes:
                print("[Main] সব camera process শেষ হয়ে গেছে.")
                break

    except KeyboardInterrupt:
        print("\n[Main] Ctrl+C — সব camera বন্ধ করা হচ্ছে...")

    finally:
        stop_event.set()
        folder_creator_thread.join(timeout=2)

        cv2.destroyAllWindows()

        for p, cam in processes:
            if p.is_alive():
                print(f"  Terminating {p.name}...")
                p.terminate()

        for p, cam in processes:
            p.join(timeout=5)

    print_summary()
    print("\n Done.")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()