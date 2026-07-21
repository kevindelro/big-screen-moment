#!/usr/bin/env python3
"""
Big Screen Moment - automatic candidate detector (proof of concept)

Finds likely "fan on the big screen" moments in a video by detecting hard
scene cuts (the moment the video board switches to a new camera feed) and
keeping only the cuts that are followed by a sustained hold - the visual
signature of a Kiss Cam / crowd cam shot, as opposed to the fast cutting
of live game action, replays, or graphics.

For each candidate, this saves a thumbnail AND an actual short video clip
(with audio), cropped to vertical 9:16 (Instagram Stories format) with a
single pre-composited brand overlay ("skin B": a soft bottom scrim with
the tagline on the left and the logo on the right) burned in - this is
the file a fan would actually download and share.

This still analyzes a FINISHED video file, start to finish - it's a proof
of concept for the detection + clip logic, not the live/continuous version
that will eventually run against a real-time capture feed.

Setup:
    pip install scenedetect[opencv] imageio-ffmpeg

(imageio-ffmpeg bundles its own ffmpeg binary - nothing extra to install
or add to PATH on Windows.)

Usage:
    python detect_big_screen_moments.py my_video.mp4
    python detect_big_screen_moments.py my_video.mp4 --min-hold 2.5 --max-hold 12
    python detect_big_screen_moments.py my_video.mp4 --no-vertical --no-brand
    python detect_big_screen_moments.py my_video.mp4 --threshold 12
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
import uuid

from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def ensure_brand_overlay(path, logo_path, width=1080, height=1920):
    """
    Pre-composite the whole "skin B" look into ONE transparent PNG: a soft
    dark gradient fade at the bottom holding the tagline (bottom-left) and
    the logo (bottom-right). Generated once and reused for every clip.

    Everything is combined into a single image on purpose - overlaying two
    separate transparent images onto a video in one ffmpeg command has
    caused encoder errors on some ffmpeg builds. A single overlay avoids
    that entirely.
    """
    if os.path.exists(path) or Image is None:
        return

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    scrim_h = 260
    fade_alpha = Image.new("L", (1, 2))
    fade_alpha.putpixel((0, 0), 0)
    fade_alpha.putpixel((0, 1), 190)
    fade_alpha = fade_alpha.resize((width, scrim_h), Image.BILINEAR)
    fade = Image.new("RGBA", (width, scrim_h), (0, 0, 0, 0))
    fade.putalpha(fade_alpha)
    overlay.paste(fade, (0, height - scrim_h), fade)

    d = ImageDraw.Draw(overlay)
    try:
        font_main = ImageFont.truetype(FONT_BOLD, 30)
        font_sub = ImageFont.truetype(FONT_REG, 26)
    except Exception:
        font_main = font_sub = ImageFont.load_default()
    d.text((52, height - 92), "You were there.", font=font_main, fill=(255, 255, 255, 255))
    d.text((52, height - 52), "We captured it.", font=font_sub, fill=(230, 230, 230, 255))

    if logo_path and os.path.exists(logo_path):
        logo_size = 160
        logo = Image.open(logo_path).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
        margin = 40
        overlay.alpha_composite(logo, (width - logo_size - margin, height - logo_size - 60))

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    overlay.save(path)


def detect_cuts(video_path, threshold):
    """Return a list of (start_seconds, end_seconds) for every detected shot."""
    video = open_video(video_path)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    scene_manager.detect_scenes(video=video)
    scene_list = scene_manager.get_scene_list()

    shots = []
    for start, end in scene_list:
        shots.append((start.get_seconds(), end.get_seconds()))
    return shots


def filter_candidates(shots, min_hold, max_hold):
    """
    Keep only shots whose duration falls in [min_hold, max_hold].

    Fast cuts (live game action, replay montages, graphics) tend to be
    short. A "fan on the board" shot is usually held steady for a few
    seconds so the crowd and the fan can react - that held duration is
    the stand-in signal this proof of concept uses instead of a trained
    classifier.
    """
    candidates = []
    for start, end in shots:
        duration = end - start
        if min_hold <= duration <= max_hold:
            candidates.append({"start": start, "end": end, "duration": duration})
    return candidates


def save_thumbnail(video_path, timestamp, out_path):
    if cv2 is None:
        return False
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    ok, frame = cap.read()
    cap.release()
    if ok:
        cv2.imwrite(out_path, frame)
    return ok


def _ffmpeg_error(result):
    stderr = result.stderr.decode(errors="replace") if result.stderr else ""
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    useful = [l for l in lines if any(k in l.lower() for k in ("error", "invalid", "no such", "unable", "not found", "unrecognized"))]
    snippet = useful[-3:] if useful else lines[-5:]
    return " | ".join(snippet) if snippet else f"ffmpeg exited with code {result.returncode}"


def save_clip(video_path, start, duration, out_path, vertical=True, overlay_path=None):
    """
    Cut an actual mobile-friendly video clip (with audio) using ffmpeg.

    vertical=True crops the center 9:16 slice and scales to 1080x1920
    (Instagram Stories format).

    Branding is applied in two stages rather than via ffmpeg's overlay
    filter: (1) ffmpeg cuts a plain clip - the simple, reliable path with
    no filters beyond crop/scale - then (2) if overlay_path is set, Python
    (Pillow/OpenCV) alpha-blends the brand overlay onto every frame and a
    final plain ffmpeg mux re-attaches the audio. Some ffmpeg builds
    mishandle real semi-transparent overlay images inside a filtergraph;
    doing the blending in Python sidesteps that entirely.
    """
    if imageio_ffmpeg is None:
        return False, "imageio_ffmpeg not installed"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    if vertical:
        # Works regardless of whether the SOURCE video is landscape or
        # already portrait/near-portrait (like a phone-shaped screen
        # recording) - picks whichever dimension is the real constraint,
        # instead of assuming a wide landscape source. trunc(...)*2 keeps
        # both dimensions even, which libx264 requires.
        video_chain = (
            r"crop=trunc(min(iw\,ih*9/16)/2)*2:trunc(min(ih\,iw*16/9)/2)*2,"
            "scale=1080:1920"
        )
    else:
        video_chain = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    plain_path = out_path + ".plain.mp4"
    cmd = [
        ffmpeg_exe, "-y", "-ss", str(start), "-i", video_path, "-t", str(duration),
        "-vf", video_chain,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart", plain_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not os.path.exists(plain_path):
        return False, _ffmpeg_error(result)

    use_overlay = bool(overlay_path and os.path.exists(overlay_path) and cv2 is not None and Image is not None)
    if not use_overlay:
        os.replace(plain_path, out_path)
        return True, None

    try:
        import numpy as np
    except ImportError:
        os.replace(plain_path, out_path)
        return True, None

    cap = cv2.VideoCapture(plain_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not w or not h:
        cap.release()
        os.replace(plain_path, out_path)
        return True, None

    overlay_img = Image.open(overlay_path).convert("RGBA").resize((w, h))

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (w, h))
    if not writer.isOpened():
        cap.release()
        os.replace(plain_path, out_path)
        return True, None

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGBA")
        frame_pil.alpha_composite(overlay_img)
        out_frame = cv2.cvtColor(np.array(frame_pil.convert("RGB")), cv2.COLOR_RGB2BGR)
        writer.write(out_frame)
    cap.release()
    writer.release()

    cmd2 = [
        ffmpeg_exe, "-y", "-i", silent_path, "-i", plain_path,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", "-movflags", "+faststart", out_path,
    ]
    result2 = subprocess.run(cmd2, capture_output=True)

    for p in (plain_path, silent_path):
        try:
            os.remove(p)
        except OSError:
            pass

    if result2.returncode != 0 or not os.path.exists(out_path):
        return False, _ffmpeg_error(result2)
    return True, None


def _build_multipart(fields, files):
    """Builds a multipart/form-data body using only the standard library -
    avoids adding the 'requests' package as a dependency just for this."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode())
    for key, (filename, filedata, content_type) in files.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"'.encode())
        parts.append(f"Content-Type: {content_type}".encode())
        parts.append(b"")
        parts.append(filedata)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    body = b"\r\n".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def ingest_clip_to_platform(api_url, event_id, thumb_src, clip_src, duration, index):
    """
    Uploads the actual thumbnail + clip FILES to the running app (not just
    a local path - the app may be running on a different machine entirely,
    e.g. hosted on Render) and registers the clip as already-approved -
    no admin review step. This is what makes a detected moment show up
    straight in the fan gallery, from anywhere.
    """
    try:
        with open(thumb_src, "rb") as f:
            thumb_bytes = f.read()
        with open(clip_src, "rb") as f:
            clip_bytes = f.read()
    except OSError as e:
        return False, f"couldn't read local file: {e}"

    body, content_type = _build_multipart(
        fields={"duration": duration, "auto_approve": "true"},
        files={
            "thumbnail": (f"clip_{index:04d}.jpg", thumb_bytes, "image/jpeg"),
            "clip": (f"clip_{index:04d}.mp4", clip_bytes, "video/mp4"),
        },
    )

    url = f"{api_url}/api/events/{event_id}/clips/upload"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")[:1500]
        except Exception:
            detail = str(e)
        return False, detail
    except urllib.error.URLError as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", help="Path to a video file to analyze")
    parser.add_argument("--out", default="candidates", help="Output folder for clips/thumbnails/report")
    parser.add_argument("--threshold", type=float, default=27.0,
                         help="Scene-cut sensitivity (lower = more cuts detected). Default 27.0")
    parser.add_argument("--min-hold", type=float, default=3.0,
                         help="Minimum seconds a shot must hold to count as a candidate moment")
    parser.add_argument("--max-hold", type=float, default=30.0,
                         help="Maximum seconds - filters out long segments like halftime shows")
    parser.add_argument("--logo", default="static/logo.png",
                         help="Path to a logo image to bake into the brand overlay")
    parser.add_argument("--overlay", default="static/brand_overlay.png",
                         help="Path to the combined brand overlay (auto-generated on first run if missing)")
    parser.add_argument("--pad-before", type=float, default=1.0,
                         help="Extra seconds to include before the detected cut, so the clip doesn't feel abrupt")
    parser.add_argument("--pad-after", type=float, default=1.0,
                         help="Extra seconds to include after the detected moment ends")
    parser.add_argument("--no-vertical", action="store_true", help="Keep original aspect ratio instead of cropping to 9:16")
    parser.add_argument("--no-brand", action="store_true", help="Skip the brand overlay entirely")
    parser.add_argument("--ingest", action="store_true",
                         help="Push every successfully-clipped candidate straight into the running app, "
                              "already approved - no manual review step")
    parser.add_argument("--event-id", type=int, default=1,
                         help="Which event (from the app) to attach ingested clips to. Default 1")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000",
                         help="Base URL of the running app (must be started with 'uvicorn main:app' first)")
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"Video not found: {args.video}")
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)

    overlay_path = None if args.no_brand else args.overlay
    if overlay_path:
        ensure_brand_overlay(overlay_path, args.logo)
        if not os.path.exists(overlay_path):
            print(f"(Couldn't create brand overlay at {overlay_path} - clips will be unbranded.)")
            overlay_path = None

    print(f"Analyzing {args.video} for scene cuts...")
    shots = detect_cuts(args.video, args.threshold)
    print(f"Found {len(shots)} total shots.")

    candidates = filter_candidates(shots, args.min_hold, args.max_hold)
    print(f"{len(candidates)} look like candidate 'big screen moments' "
          f"(held {args.min_hold}-{args.max_hold}s).")

    report = []
    for i, c in enumerate(candidates):
        mid = (c["start"] + c["end"]) / 2
        thumb_path = os.path.join(args.out, f"candidate_{i:03d}.jpg")
        clip_path = os.path.join(args.out, f"candidate_{i:03d}.mp4")

        padded_start = max(0.0, c["start"] - args.pad_before)
        padded_duration = (c["end"] + args.pad_after) - padded_start

        got_thumb = save_thumbnail(args.video, mid, thumb_path)
        got_clip, clip_error = save_clip(
            args.video, padded_start, padded_duration, clip_path,
            vertical=not args.no_vertical, overlay_path=overlay_path,
        )

        entry = {
            "index": i,
            "start_seconds": round(padded_start, 2),
            "end_seconds": round(padded_start + padded_duration, 2),
            "duration_seconds": round(padded_duration, 2),
            "thumbnail": thumb_path if got_thumb else None,
            "clip": clip_path if got_clip else None,
        }
        report.append(entry)
        status = "clip saved" if got_clip else f"CLIP FAILED - {clip_error}"

        if got_clip and args.ingest:
            ingested, info = ingest_clip_to_platform(
                args.api_url, args.event_id, thumb_path, clip_path,
                entry["duration_seconds"], i,
            )
            status += " -> live on platform" if ingested else f" -> ingest failed: {info}"

        print(f"  #{i}: {entry['start_seconds']}s - {entry['end_seconds']}s "
              f"({entry['duration_seconds']}s hold) - {status}")

    report_path = os.path.join(args.out, "candidates.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nWrote {report_path}, thumbnails, and clips to {args.out}/")
    print("Open the .mp4 clips - vertical, branded, ready to compare against "
          "how it'd actually look shared to a fan's Instagram Story.")


if __name__ == "__main__":
    main()
