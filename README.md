# Big Screen Moment - pilot backend

Runs the full software loop with no venue, camera, or cloud account
required: event + period setup, candidate clip ingestion (what the
auto-detector will eventually call), an admin review queue, and the
fan-facing gallery.

## Run it

```
pip install -r requirements.txt
python seed_demo_data.py
uvicorn main:app --reload
```

Then open:
- Fan gallery: http://localhost:8000/app/gallery.html?event=1
- Admin review queue: http://localhost:8000/app/admin.html?event=1

The seed script fills in a demo event with four quarters and a dozen
clips (placeholder thumbnails, since there's no real footage yet) - most
already "approved" so the gallery isn't empty, a few left as
"candidate" so the admin queue has something to review.

## How this maps to the real system

- `POST /api/events/{id}/periods` is what a staffer would call a
  handful of times per game ("start of Q2") instead of marking every
  moment - see `detect_big_screen_moments.py` for the piece meant to
  replace manual moment-marking entirely.
- `POST /api/events/{id}/clips` is the endpoint the auto-detector (or a
  manual fallback button, for the very first pilot) calls the instant it
  flags a candidate.
- Everything lands as `status = 'candidate'` and only reaches the fan
  gallery after `POST /api/clips/{id}/approve` - real thumbnails/video
  files can be swapped in for the placeholder ones without changing this
  flow.

## Not yet wired up (next pieces)

- Real video storage/upload (currently just a file path column)
- SMS delivery (Twilio would hit the gallery URL as the text link)
- Auth on the admin routes
- Swapping SQLite for something that survives multiple app instances,
  once this needs to run at more than one venue
