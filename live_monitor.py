#!/usr/bin/env python3
"""
Big Screen Moment - live camera monitor (continuous auto-detection)

This is the live counterpart to detect_big_screen_moments.py. That script
analyzes a FINISHED video file, start to finish. This one watches a
CONTINUOUS feed - a real webcam, an IP/RTSP camera, or a video file played
back as if it were live - and detects candidate moments as they actually
happen, with no waiting for a file to finish.

How it works:
  - Every incoming frame is added to a rolling buffer (kept in memory as
    small JPEGs, not raw frames, so a 90-second buffer doesn't eat all
    your RAM).
  - A lightweight frame-to-frame difference score flags hard cuts - the
    same "how long did the picture hold before the next cut" idea as the
    file-based script, just computed live instead of after the fact.
  - When a shot's hold-time lands in the candidate window, the clip is
    pulled straight out of the buffer (with padding before/after), cropped
    vertical, branded, and - if --ingest is set - pushed directly into
    your running app's fan gallery. No human, no manual step.

Known limitation: a live webcam/RTSP feed has no audio track available to
OpenCV, so clips from a real camera come out silent. If you point this at
a video FILE to simulate a live feed (great for testing), that file's own
audio is extracted and attached automatically, since it's actually
available in that case.

Setup:
    pip install opencv-python imageio-ffmpeg pillow

Usage:
    # Simulate a live camera using a video file you already have -
    # this is the easiest way to test the whole live pipeline right now:
    python live_monitor.py --source "C:\\Users\\kevdel\\Desktop\\kisscam3.mp4" --ingest

    # A real, physically-connected webcam (0 is usually the built-in one):
    python live_monitor.py --source 0 --ingest

    # An IP / RTSP camera:
    python live_monitor.py --source "rtsp://192.168.1.50:554/stream" --ingest

Press Ctrl+C to stop.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque

import cv2
import csv
import numpy as np

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

# Reuse the brand-overlay generator and platform-ingest helper already
# built for the file-based script, instead of duplicating them.
import detect_big_screen_moments as filedet


def open_source(source_arg):
    """--source can be a webcam index ('0'), a file path, or an RTSP/HTTP URL."""
    if source_arg.isdigit():
        return cv2.VideoCapture(int(source_arg)), True  # True = "live" (no built-in audio/timeline)
    is_file = os.path.exists(source_arg)
    cap = cv2.VideoCapture(source_arg)
    return cap, not is_file  # file sources have real audio + a real timeline to throttle against


def small_color(frame, max_dim=240):
    """
    Downscaled HSV representation used for the frame-to-frame difference
    score. HSV (rather than raw BGR) is what real scene-cut detectors use,
    since it separates color/brightness in a way that's more sensitive to
    an actual camera cut than comparing raw pixel values.

    Resizes to a moderate size while PRESERVING the source's actual aspect
    ratio - squashing a portrait phone-shaped frame into a fixed small
    landscape shape (an earlier version of this function did that) distorts
    the image and buries real differences before they're even measured.
    """
    h, w = frame.shape[:2]
    scale = max_dim / max(h, w)
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
    small = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    return hsv.astype("float32")


def export_clip(buffer, start_t, end_t, out_path, thumb_path, vertical, overlay_img, source_path_for_audio, is_live):
    """
    Pull frames from the buffer in [start_t, end_t], crop/brand them, and
    write a clip plus a matching thumbnail. If a source video FILE (not a
    live camera) is available, its real audio for this exact window is
    extracted and attached.
    """
    frames_in_range = [(t, jpg) for (t, jpg) in buffer if start_t <= t <= end_t]
    if len(frames_in_range) < 2:
        return False, "not enough buffered frames for this window"

    first = cv2.imdecode(np.frombuffer(frames_in_range[0][1], np.uint8), cv2.IMREAD_COLOR)
    h, w = first.shape[:2]
    if vertical:
        crop_w = min(w, int(h * 9 / 16))
        crop_w -= crop_w % 2
        crop_h = min(h, int(w * 16 / 9))
        crop_h -= crop_h % 2
        x0 = (w - crop_w) // 2
        y0 = (h - crop_h) // 2
        out_w, out_h = 1080, 1920
    else:
        crop_w, crop_h, x0, y0 = w, h, 0, 0
        out_w, out_h = (w // 2) * 2, (h // 2) * 2

    duration = frames_in_range[-1][0] - frames_in_range[0][0]
    fps = max(1.0, len(frames_in_range) / max(duration, 0.1))

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (out_w, out_h))
    if not writer.isOpened():
        return False, "could not open video writer"

    mid_index = len(frames_in_range) // 2
    for i, (_, jpg) in enumerate(frames_in_range):
        frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        resized = cv2.resize(cropped, (out_w, out_h))
        if overlay_img is not None and Image is not None:
            pil_frame = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)).convert("RGBA")
            pil_frame.alpha_composite(overlay_img)
            resized = cv2.cvtColor(np.array(pil_frame.convert("RGB")), cv2.COLOR_RGB2BGR)
        writer.write(resized)
        if i == mid_index:
            cv2.imwrite(thumb_path, resized)
    writer.release()

    if is_live or imageio_ffmpeg is None:
        # No real audio available from a live camera - ship the silent clip.
        shutil.move(silent_path, out_path)
        return True, None

    # Source is a file being used to simulate "live" - it really does have
    # audio for this time window, so pull it in with a plain, filter-free
    # ffmpeg mux (the simple, reliable kind of ffmpeg call).
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y",
        "-i", silent_path,
        "-ss", str(frames_in_range[0][0]), "-t", str(duration), "-i", source_path_for_audio,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", "-movflags", "+faststart", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    try:
        os.remove(silent_path)
    except OSError:
        pass
    if result.returncode != 0 or not os.path.exists(out_path):
        return False, filedet._ffmpeg_error(result)
    return True, None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Webcam index ('0'), video file path, or RTSP/HTTP URL")
    parser.add_argument("--buffer-seconds", type=float, default=90.0, help="How much rolling footage to keep available")
    parser.add_argument("--threshold", type=float, default=9.0,
                         help="Cut-detection sensitivity (lower = more cuts flagged). "
                              "This is NOT the same scale as detect_big_screen_moments.py's --threshold - "
                              "start around 9 and adjust from there; try lower (4-6) if real cuts are being missed.")
    parser.add_argument("--min-hold", type=float, default=3.0)
    parser.add_argument("--max-hold", type=float, default=30.0)
    parser.add_argument("--pad-before", type=float, default=1.0)
    parser.add_argument("--pad-after", type=float, default=1.0)
    parser.add_argument("--out", default="live_candidates")
    parser.add_argument("--logo", default="static/logo.png")
    parser.add_argument("--overlay", default="static/brand_overlay.png")
    parser.add_argument("--no-vertical", action="store_true")
    parser.add_argument("--no-brand", action="store_true")
    parser.add_argument("--ingest", action="store_true", help="Push every detected clip straight into the running app, already approved")
    parser.add_argument("--event-id", type=int, default=1)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--realtime", dest="realtime", action="store_true", default=True,
                         help="Throttle a file source to play at real speed, so it behaves like a live feed (default: on)")
    parser.add_argument("--no-realtime", dest="realtime", action="store_false")
    parser.add_argument("--log-diffs", default="diff_log.csv",
                         help="Every diff score gets written here with its timestamp, so a threshold can be "
                              "picked from real data instead of trial and error. Set to '' to skip logging.")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    overlay_img = None
    if not args.no_brand:
        overlay_path = args.overlay
        filedet.ensure_brand_overlay(overlay_path, args.logo, width=1080, height=1920)
        if os.path.exists(overlay_path) and Image is not None:
            overlay_img = Image.open(overlay_path).convert("RGBA")

    cap, is_live = open_source(args.source)
    if not cap.isOpened():
        print(f"Could not open source: {args.source}")
        sys.exit(1)

    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = 1.0 / source_fps if source_fps > 0 else 1.0 / 30.0

    kind = "LIVE camera (no audio available)" if is_live else f"file, simulating live (realtime={args.realtime}, has real audio)"
    print(f"Watching: {args.source}  [{kind}]")
    print(f"Buffer: {args.buffer_seconds}s   Candidate window: {args.min_hold}-{args.max_hold}s hold, "
          f"+{args.pad_before}s/+{args.pad_after}s padding")
    print("Press Ctrl+C to stop.\n")

    buffer = deque()  # list of (timestamp, jpeg_bytes)
    prev_frame = None
    recent_diffs = deque(maxlen=300)
    shot_start = 0.0

    log_file = open(args.log_diffs, "w", newline="") if args.log_diffs else None
    log_writer = csv.writer(log_file) if log_file else None
    if log_writer:
        log_writer.writerow(["time_seconds", "diff_score"])
    clip_index = 0
    pending = []  # candidates waiting for enough "after" padding to be buffered
    start_wall = time.time()
    last_heartbeat = 0

    try:
        while True:
            loop_start = time.time()
            ok, frame = cap.read()
            if not ok:
                if is_live:
                    print("Lost the camera feed - stopping.")
                    break
                else:
                    print("Reached the end of the file.")
                    break

            if is_live:
                now = time.time() - start_wall
            else:
                # Use the file's own embedded timeline, not wall-clock time -
                # otherwise a file processed at full CPU speed (no --realtime
                # throttling) finishes in a fraction of a second and every
                # shot looks like it lasted almost no time at all.
                now = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

            ok_enc, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok_enc:
                buffer.append((now, jpg.tobytes()))
            while buffer and buffer[0][0] < now - args.buffer_seconds:
                buffer.popleft()

            small = small_color(frame)
            if prev_frame is not None:
                diff = float(np.abs(small - prev_frame).mean())
                recent_diffs.append(diff)
                if log_writer:
                    log_writer.writerow([round(now, 3), round(diff, 3)])
                if diff > args.threshold:
                    shot_duration = now - shot_start
                    if args.min_hold <= shot_duration <= args.max_hold:
                        pending.append({"start": shot_start, "end": now, "export_at": now + args.pad_after})
                        print(f"[{now:6.1f}s] Candidate moment detected - held {shot_duration:.1f}s "
                              f"(exporting once {args.pad_after}s of trailing footage is buffered)")
                    shot_start = now
            prev_frame = small

            still_pending = []
            for cand in pending:
                if now >= cand["export_at"]:
                    clip_index += 1
                    clip_path = os.path.join(args.out, f"live_{clip_index:04d}.mp4")
                    thumb_path = os.path.join(args.out, f"live_{clip_index:04d}.jpg")
                    got, err = export_clip(
                        list(buffer), cand["start"] - args.pad_before, cand["end"] + args.pad_after,
                        clip_path, thumb_path, not args.no_vertical, overlay_img, args.source, is_live,
                    )
                    if got:
                        msg = f"  -> saved {clip_path}"
                        if args.ingest:
                            ingested, info = filedet.ingest_clip_to_platform(
                                args.api_url, args.event_id, thumb_path, clip_path,
                                cand["end"] - cand["start"], clip_index,
                            )
                            msg += " -> live on platform" if ingested else f" -> ingest failed: {info}"
                        print(msg)
                    else:
                        print(f"  -> export failed: {err}")
                else:
                    still_pending.append(cand)
            pending = still_pending

            if now - last_heartbeat > 5:
                if recent_diffs:
                    print(f"[{now:6.1f}s] watching... diff scores recently: "
                          f"avg {sum(recent_diffs)/len(recent_diffs):.1f}, max {max(recent_diffs):.1f} "
                          f"(threshold is {args.threshold})")
                else:
                    print(f"[{now:6.1f}s] watching...")
                last_heartbeat = now

            if not is_live and args.realtime:
                elapsed = time.time() - loop_start
                sleep_for = frame_interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cap.release()
        if log_file:
            log_file.close()
            print(f"\nWrote every diff score (with timestamps) to {args.log_diffs} - "
                  f"send this file over so the right --threshold can be picked from real data.")


if __name__ == "__main__":
    main()
