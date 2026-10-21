"""Guided enrollment on the Enrollment page: steps, live camera guide (streamlit-webrtc), simple capture
fallback, review and finish. The logic lives in app/enroll_guide.py; this module is only the UI."""

import time

import cv2
import streamlit as st

from app import config, enroll_guide, enrollment
from dashboard.common import claim_camera, connection, face_models, flash, manager

LIVE, SIMPLE = "Live camera (guided)", "Simple capture"
BUSY = "Camera is busy. Stop the camera workers on the Cameras page while enrolling."


def thumb(img, size: int = 260) -> bytes | None:
    if img is None:
        return None
    scale = min(1.0, size / max(img.shape[:2]))
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
    ok, buf = cv2.imencode(".jpg", small)
    return buf.tobytes() if ok else None


def session(code: str) -> enroll_guide.GuidedSession:
    return st.session_state.setdefault(f"guided:{code}", enroll_guide.GuidedSession(code))


def live_guide(code: str, step: enroll_guide.Step) -> enroll_guide.LiveGuide:
    key = f"guided_live:{code}"
    if key not in st.session_state:
        st.session_state[key] = enroll_guide.LiveGuide(face_models()[0], step)
    guide = st.session_state[key]
    guide.set_step(step)
    return guide


def message(code: str, kind: str | None = None, text: str | None = None):
    """Set (or show and keep) the guided-capture message under the instruction."""
    key = f"guided_msg:{code}"
    if kind:
        st.session_state[key] = (kind, text)
    elif item := st.session_state.get(key):
        {"error": st.error, "success": st.success}.get(item[0], st.info)(item[1])


def capture(code: str, sess: enroll_guide.GuidedSession, step: enroll_guide.Step, frame) -> None:
    """Final check on the full-resolution frame; keep it and move to the next step, or say why not."""
    if frame is None:
        message(code, "error", "No picture from the camera yet. Wait for the video, then try again.")
        return
    detector, recognizer = face_models()
    cap, problem = enroll_guide.capture_photo(frame, step, detector, recognizer)
    if problem:
        message(code, "error", f"Not kept: {problem}")
        return
    sess.accept(cap)
    q = cap.quality
    message(code, "success", f"{step.title} photo kept: score {q.score:.0f}, "
                             f"yaw {enroll_guide.person_yaw(q.yaw):+.0f}°, pitch {q.pitch:+.0f}°.")


def render(code: str) -> None:
    sess = session(code)
    step = sess.current()
    n, total = sess.position()
    st.subheader("Guided photos")
    if step is not None:
        st.progress((n - 1) / total, text=f"Step {n} of {total}: {step.title}")
        lo, hi = step.yaw
        st.markdown(f"## {step.arrow} {step.instruction}")
        st.caption(f"Head angle {lo:+.0f}° to {hi:+.0f}° (+ = to your left), tilt within ±{step.pitch[1]:.0f}°. "
                   "The preview is mirrored like a selfie.")
    else:
        st.progress(1.0, text=f"All {total} photos taken")
        st.success("All photos taken. Check them below, then press Finish enrollment.")
    message(code)

    if step is not None:
        mode = st.radio("Capture with", [LIVE, SIMPLE], horizontal=True, key="enroll_mode",
                        help="Simple capture uses the browser's photo camera or a file, if the live camera "
                             "doesn't work here.")
        if mode == LIVE:
            live_mode(code, sess, step)
        else:
            simple_mode(code, sess, step)
    review_section(code, sess)


# --- Live camera guide -------------------------------------------------------------------------------------


def live_mode(code: str, sess, step) -> None:
    users = manager().webcam_users()
    if users:
        claim_camera(False)
        st.warning(f"{BUSY} (The {', '.join(users)} worker is using the webcam.)")
        return
    import av
    from streamlit_webrtc import WebRtcMode, webrtc_streamer

    guide = live_guide(code, step)

    def callback(frame: "av.VideoFrame") -> "av.VideoFrame":  # streamlit-webrtc's video thread
        out = guide.process(frame.to_ndarray(format="bgr24"))
        return av.VideoFrame.from_ndarray(out, format="bgr24")

    w, h = config.ENROLL_VIDEO_SIZE
    try:
        ctx = webrtc_streamer(
            key=f"enroll_live_{code}", mode=WebRtcMode.SENDRECV, video_frame_callback=callback,
            media_stream_constraints={"video": {"width": {"ideal": w}, "height": {"ideal": h}}, "audio": False},
            rtc_configuration={"iceServers": []},  # local connection only: no STUN servers on the internet
            async_processing=True,
        )
    except Exception as e:  # noqa: BLE001  WebRTC must never take the page down: fall back
        st.warning(f"The live camera isn't available here ({type(e).__name__}). Choose \"{SIMPLE}\" above.")
        return
    playing = bool(ctx.state.playing)
    claim_camera(playing)  # webcam workers can't start while the live guide has the camera
    since_key = f"guided_live_since:{code}"
    if playing:
        st.session_state.setdefault(since_key, time.monotonic())
    else:
        st.session_state.pop(since_key, None)
    live_status(code, step.key, ctx, since_key)


@st.fragment(run_every=0.3)
def live_status(code: str, step_key: str, ctx, since_key: str) -> None:
    sess = session(code)
    step = sess.current()
    if step is None or step.key != step_key:
        st.rerun(scope="app")
    if not ctx.state.playing:
        st.info("Press START above to turn on the camera (allow camera access when the browser asks).")
        return
    guide = live_guide(code, step)
    snap = guide.snapshot()
    waited = time.monotonic() - st.session_state.get(since_key, time.monotonic())
    if snap["frames"] == 0:
        if waited > config.ENROLL_CAMERA_TIMEOUT_SECONDS:
            st.error(BUSY)
        else:
            st.info("Starting the camera...")
        return
    icon = {"good": "🟢", "bad": "🔴"}.get(snap["state"], "⚪")
    st.markdown(f"### {icon} {snap['hint']}")
    st.progress(min(1.0, snap["held"] / config.ENROLL_STABLE_SECONDS),
                text="Hold still..." if snap["state"] == "good" and not snap["stable"] else
                ("Ready: press Capture" if snap["stable"] else "Follow the hint on the video"))
    auto = (config.ENROLL_AUTO_CAPTURE and snap["state"] == "good"
            and snap["held"] >= config.ENROLL_AUTO_CAPTURE_SECONDS)
    pressed = st.button("Capture", type="primary", disabled=not snap["stable"], key=f"capture_{code}_{step.key}",
                        icon=":material/photo_camera:")
    st.caption(f"Video {snap['video_fps']:.0f} fps · face checks {snap['analysis_fps']:.1f}/s"
               + (" · auto-capture on" if config.ENROLL_AUTO_CAPTURE else ""))
    if pressed or auto:
        capture(code, sess, step, guide.take_frame())
        st.rerun(scope="app")


# --- Simple capture fallback ----------------------------------------------------------------------------------


def simple_mode(code: str, sess, step) -> None:
    st.caption("Same checks and the same steps, without the live guide: take the photo with the browser camera, "
               "or upload one.")
    counter = st.session_state.setdefault("guided_simple", 0)
    users = manager().webcam_users()
    if users:
        claim_camera(False)
        st.warning(f"{BUSY} (The {', '.join(users)} worker is using the webcam.) You can still upload a photo.")
    else:
        camera_on = st.toggle("Use the camera", key="enroll_camera_on",
                              help="Webcam camera workers can't start while this is on.")
        claim_camera(camera_on)
        if camera_on:
            shot = st.camera_input(f"{step.title}: {step.instruction}", key=f"simple_cam_{code}_{counter}")
            if shot is not None:
                capture(code, sess, step, enrollment.decode_image(shot.getvalue()))
                st.session_state["guided_simple"] = counter + 1
                st.rerun()
    upload = st.file_uploader(f"Or upload the \"{step.title}\" photo",
                              type=sorted(e.lstrip(".") for e in config.IMAGE_EXTS), key=f"simple_up_{code}_{counter}")
    if upload is not None and st.button("Use this photo", key=f"simple_use_{code}_{counter}"):
        capture(code, sess, step, enrollment.decode_image(upload.getvalue()))
        st.session_state["guided_simple"] = counter + 1
        st.rerun()


# --- Review and finish --------------------------------------------------------------------------------------------


def review_section(code: str, sess) -> None:
    if not sess.captures:
        return
    st.markdown("#### Review")
    warnings = enroll_guide.review(sess, enrollment.load_gallery())
    steps = sess.steps
    cols = st.columns(len(steps))
    for col, s in zip(cols, steps):
        with col:
            cap = sess.captures.get(s.key)
            if cap is None:
                st.info(f"{s.title}: not taken yet")
                continue
            q = cap.quality
            st.image(thumb(cap.image), caption=f"{s.title} · score {q.score:.0f} · "
                                               f"yaw {enroll_guide.person_yaw(q.yaw):+.0f}°")
            for w in warnings.get(s.key, []):
                st.warning(w)
            if st.button("Retake", key=f"retake_{code}_{s.key}", icon=":material/refresh:"):
                sess.retake(s.key)
                message(code, "info", f"Retake the \"{s.title}\" photo.")
                st.rerun()
    done = sum(1 for s in sess.required if s.key in sess.captures)
    if st.button("Finish enrollment", type="primary", disabled=not sess.complete, key=f"finish_{code}",
                 icon=":material/how_to_reg:"):
        detector, recognizer = face_models()
        with st.spinner("Saving photos and rebuilding the embeddings..."), connection() as conn:
            person = enroll_guide.finish(sess, detector, recognizer, conn)
        for key in (f"guided:{code}", f"guided_live:{code}", f"guided_msg:{code}"):
            st.session_state.pop(key, None)
        running = [s.camera for s in manager().statuses() if s.state != "stopped"]
        note = (f" Running cameras ({', '.join(running)}) pick up the new photos within "
                f"{config.GALLERY_RELOAD_SECONDS:.0f} s." if running else " Start the cameras to use them.")
        flash(f"Enrollment finished for {code}: {len(person.accepted)} usable photo(s), "
              f"{len(person.embeddings)} embedding(s) in the gallery.{note}")
        st.rerun()
    if not sess.complete:
        st.caption(f"Finish enrollment is available when all {len(sess.required)} photos are taken "
                   f"({done} so far).")
