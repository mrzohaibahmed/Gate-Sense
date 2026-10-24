# GateSense: Build Plan

Oct 20, 2026 · @Zohaib Ahmed

## How it works

Build it as a pretrained face-recognition pipeline running on one local server: no model training is needed, and adding a person means adding photos, not retraining.

Each gate camera streams video to the server. The server finds faces in each frame, turns every face into a 512-number fingerprint (an "embedding"), and compares it with the fingerprints of enrolled people. A confident match at the entrance camera writes an IN time; at the exit camera it writes an OUT time. Faces that match nobody are ignored, so only known people get attendance.

Start with a pilot: one entrance camera, one exit camera and 20 to 50 enrolled people, then scale out.

Pipeline: entrance/exit camera -> detect faces (SCRFD) -> track (ByteTrack) -> quality + liveness -> embed (ArcFace) -> match gallery + vote -> known person? yes: attendance database -> dashboard; no: not recorded. Enrollment tool -> face gallery -> match step.

Both cameras feed the same pipeline; the camera a face was seen on decides whether the record is IN or OUT.

## Recommended stack

Use Python with InsightFace and OpenCV on an on-premise PC with an NVIDIA GPU, PostgreSQL for records, and a FastAPI web dashboard.

| Layer | Recommended | Why / alternative |
| --- | --- | --- |
| Cameras | 2 to 4 MP IP cameras with RTSP and WDR (one per gate direction) | Any brand that exposes an RTSP stream works; WDR handles bright doorways |
| Processing server | PC with NVIDIA GPU (RTX 3060 or better) | NVIDIA Jetson Orin for a small box at the gate; CPU-only handles 1 to 2 cameras at low frame rate |
| Language | Python 3.11 | Best library support for vision work |
| Video capture | OpenCV (FFmpeg backend) | NVIDIA DeepStream or GStreamer later for many cameras |
| Face detection and recognition | InsightFace (SCRFD + ArcFace) on ONNX Runtime GPU | OpenCV YuNet + SFace if you need permissive licenses |
| Face matching | NumPy cosine similarity | FAISS or pgvector beyond about 10,000 people |
| Database | PostgreSQL | SQLite is fine for the pilot |
| Backend API | FastAPI | Django if you prefer a built-in admin panel |
| Dashboard | React, or Streamlit for the pilot | Shows live events, daily reports, Excel export |
| Deployment | Docker Compose on the local server | Keeps video and biometrics on site |

## Models

Use four pretrained models in a chain: a detector, a recognizer, a tracker and a liveness check. You do not train any of them; recognition works from 5 to 10 enrollment photos per person.

| Job | Recommended | Alternatives | What it does |
| --- | --- | --- | --- |
| Face detection | SCRFD (InsightFace buffalo\_l pack) | RetinaFace; YuNet (OpenCV, lightweight) | Finds each face and 5 landmarks (eyes, nose, mouth) used to straighten it |
| Face recognition | ArcFace ResNet-50 or ResNet-100 (InsightFace) | AdaFace; SFace (OpenCV Zoo); FaceNet | Turns an aligned 112x112 face into a 512-number embedding compared by cosine similarity |
| Tracking | ByteTrack | DeepSORT; a simple IoU tracker for the pilot | Follows one person across frames so you vote over several frames and log them once |
| Liveness (anti-spoofing) | Silent-Face-Anti-Spoofing (MiniVision) | IR or depth camera hardware | Rejects a printed photo or a phone screen held up to the camera |

Before embedding a face, apply a simple quality filter: face at least 80 pixels wide, not blurred (variance of Laplacian), and roughly facing the camera (judged from the landmarks). This removes most false matches.

**License check:** InsightFace's pretrained ArcFace weights are released for non-commercial research use. They are ideal for the prototype. For a production system at a business, or one you sell, license them from InsightFace or switch to YuNet + SFace (permissively licensed; verify each model's license file before shipping).

## Enrolling known people

Every known person gets a record plus 5 to 10 face photos, and the system stores their embeddings, not just the photos.

1. Collect per person: employee code, name, department, shift, and signed consent.
2. Capture 5 to 10 photos: straight on, slightly left and right, with and without glasses. Capturing them with the gate cameras themselves gives the best accuracy, because lighting and angle match.
3. For each photo: detect, align, run the quality filter, compute the embedding, and save it. Reject photos with no face or two faces.
4. Keep all embeddings per person; match against each and take the best score.
5. Refresh over time: when someone is matched with very high confidence, optionally add that capture as a new embedding so the gallery tracks haircuts, beards and ageing.

Core database tables:

| Table | Key columns |
| --- | --- |
| persons | id, employee\_code, name, department, shift, active |
| face\_embeddings | id, person\_id, vector (512 floats), source\_photo, created\_at |
| cameras | id, name, rtsp\_url, direction (IN or OUT) |
| attendance\_events | id, person\_id, camera\_id, timestamp, match\_score, snapshot\_path |
| daily\_attendance | person\_id, date, first\_in, last\_out, worked\_hours, status |

## Attendance rules

Mark attendance only when several frames of one tracked face agree on the same enrolled person above a tuned threshold; everything else is treated as unknown and not recorded.

1. **Detect and track** every face in each frame, giving each person a track ID while they walk past.
2. **Pick the best 3 to 5 frames** of each track by the quality filter, and embed those.
3. **Match** each embedding against the gallery. Accept a frame when the top cosine score clears the threshold (start around 0.45 for ArcFace and tune on your own footage) and beats the second-best person by a clear margin.
4. **Vote** across the track's frames; the person must win the majority, otherwise the track is unknown.
5. **Debounce:** ignore the same person in the same direction (any camera of that direction) for a cool-down window (for example 5 minutes, reset at midnight) so standing near the gate does not log 50 events.
6. **Write the event** with camera direction, timestamp, score and a small face snapshot for audit.
7. **Daily summary:** first IN of the day is the in-time and last OUT is the out-time; all raw events are kept so mid-day exits and returns are visible.

Edge cases to handle from day one:

- **No OUT recorded:** flag the day as "missing exit" for an admin to fix, rather than guessing.
- **Night shifts:** assign events to a shift window instead of the calendar date.
- **Unknown faces:** not recorded by default. Optionally keep a snapshot in a review queue so an admin can enroll a new employee, only if your privacy policy allows it.
- **Manual corrections:** admins can add or edit a record with a reason, and the change is logged.

## Build steps

Build in nine steps, proving recognition on recorded video before wiring up live cameras.

1. **Scope the pilot.** Pick one entrance and one exit, 20 to 50 people, and decide on-premise hosting. Collect consent.
2. **Install the cameras.** Mount both, confirm their RTSP streams open in VLC and OpenCV, and record sample walk-throughs at different times of day.
3. **Prototype recognition offline.** Run InsightFace on the recorded clips with a handful of enrolled colleagues. Measure how often the right person is found and how often a stranger is wrongly matched.
4. **Build the enrollment tool.** A small web form or script that adds a person, takes photos, and saves embeddings to the database.
5. **Build the live pipeline.** One worker per camera: read stream, detect, track, quality filter, embed, match, vote, write event. Auto-reconnect when a stream drops.
6. **Add attendance rules.** Debounce, daily first-in and last-out, shift windows, missing-exit flags.
7. **Build the dashboard.** Live event feed, daily and monthly reports, Excel or CSV export, manual corrections, person management.
8. **Harden it.** Add liveness checks, tune the threshold on real pilot data, run as Docker services with auto-restart, add health monitoring and nightly database backups.
9. **Run the pilot in parallel** with your current register for 2 to 4 weeks, compare the two daily, fix misses, then roll out to more gates.

## Camera placement

Camera placement decides accuracy more than the model does: aim for a clear, front-facing face at least 80 pixels wide, ideally 150 or more.

- **Height and angle:** mount at about 2 to 2.5 m, tilted down no more than about 15 degrees, so faces are seen nearly straight on.
- **One camera per direction:** the entrance camera faces people walking in; the exit camera faces people walking out. Never use one camera for both.
- **Funnel people:** a doorway, turnstile or narrow passage 1 to 3 m from the camera makes people pass one at a time and face forward.
- **Lighting:** avoid pointing a camera at a bright outdoor doorway (faces turn into silhouettes). Use WDR cameras, add front lighting, and use IR night mode where needed.
- **Settings:** 2 to 4 MP, 15 frames per second is enough, fast shutter to reduce motion blur.
- **Network:** wired PoE cameras on a separate network or VLAN, connected to the server by switch.

## Privacy and security

Face data is biometric data, so treat it as the most sensitive data in the system.

- Get written consent from each enrolled person and offer an alternative (card or PIN) for anyone who declines.
- Do not record continuous video; process frames live and keep only embeddings plus small event snapshots.
- Set a retention period for snapshots and delete embeddings when someone leaves.
- Encrypt the database and backups, keep the server on site, and require logins with roles (admin, HR, viewer).
- Check your country's data-protection law before rollout, and confirm the model licenses (see Models).

## Open questions

These answers change the hardware and model choices:

- [ ] How many people, and how many entrance and exit gates?
- [ ] Are the gates indoors or facing outdoor light?
- [ ] Is this for your own organization, or a product you will sell (decides model licensing)?
- [ ] Do you already have IP cameras, or will you buy new ones?
- [ ] Should it connect to an existing HR or payroll system?
- [ ] Single day shift, or night shifts too?

## Decisions since the plan

- Build with Claude Code on the developer's own PC (webcam now, gate cameras later).
- CPU first: for a 2-camera pilot, use InsightFace buffalo_s (SCRFD-500M + MobileFaceNet) on onnxruntime or OpenVINO, process 5 to 10 fps, detect on ~640 px frames, recognize only the best 3 to 5 frames per track. Add an NVIDIA GPU only if measured speed falls short or past ~4 cameras.
- YOLO is optional: it can replace SCRFD for detection and Ultralytics' built-in ByteTrack for tracking, but ArcFace still does identification. Ultralytics is AGPL-3.0 (commercial use needs their license).
- First build step: webcam prototype (enroll from known/<name>/*.jpg, recognize live, threshold 0.45), before buying gate cameras.
