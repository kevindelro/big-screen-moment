"""
Seeds one demo event with four periods and a handful of clips (some
already 'approved', some still 'candidate') so you can open the gallery
and the admin review queue immediately - no venue, camera, or real
footage required.

Run after installing requirements:
    python seed_demo_data.py
"""

import os
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

import db

COLORS = [(160, 60, 196), (224, 52, 155), (237, 90, 66), (242, 131, 60)]  # purple -> pink -> coral -> orange, matches the logo


def make_thumbnail(path, line1, line2, color):
    img = Image.new("RGB", (320, 180), color)
    draw = ImageDraw.Draw(img)
    draw.text((16, 70), line1, fill="white")
    draw.text((16, 95), line2, fill="white")
    img.save(path)


def seed():
    db.init_db()
    conn = db.get_conn()
    now = datetime.now(timezone.utc)

    cur = conn.execute(
        "INSERT INTO events (name, venue, created_at) VALUES (?, ?, ?)",
        ("Demo Night - Home Arena", "Test Arena", now.isoformat()),
    )
    conn.commit()
    event_id = cur.lastrowid

    os.makedirs("thumbnails", exist_ok=True)

    labels = ["1st Quarter", "2nd Quarter", "3rd Quarter", "4th Quarter"]
    period_ids = []
    period_start = now - timedelta(minutes=40)
    for i, label in enumerate(labels):
        start = period_start + timedelta(minutes=i * 10)
        end = start + timedelta(minutes=10)
        cur = conn.execute(
            "INSERT INTO periods (event_id, label, start_time, end_time) VALUES (?, ?, ?, ?)",
            (event_id, label, start.isoformat(), end.isoformat()),
        )
        period_ids.append(cur.lastrowid)
    conn.commit()

    clip_num = 0
    for i, label in enumerate(labels):
        period_start_dt = period_start + timedelta(minutes=i * 10)
        for j in range(3):
            clip_num += 1
            ts = period_start_dt + timedelta(minutes=j * 3)
            thumb_rel = f"/thumbnails/clip_{clip_num}.jpg"
            make_thumbnail(f"thumbnails/clip_{clip_num}.jpg", label, f"Clip {clip_num}",
                            COLORS[i % len(COLORS)])
            # every 4th clip stays a 'candidate' so the admin queue isn't empty
            status = "candidate" if clip_num % 4 == 0 else "approved"
            conn.execute(
                "INSERT INTO clips (event_id, period_id, timestamp, duration, thumbnail_path, "
                "video_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, period_ids[i], ts.isoformat(), 4.5, thumb_rel, None, status,
                 now.isoformat()),
            )
    conn.commit()
    conn.close()
    print(f"Seeded demo event id={event_id}.")
    print(f"Gallery:  http://localhost:8000/app/gallery.html?event={event_id}")
    print(f"Admin:    http://localhost:8000/app/admin.html?event={event_id}")


if __name__ == "__main__":
    seed()
