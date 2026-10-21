import cv2
import streamlit as st

from app import config, db, enrollment
from dashboard import guided_capture
from dashboard.common import connection, face_models, flash, person_label, show_flash

st.title("Enrollment")
st.caption("Take the guided photos (front, slightly left, slightly right); each is checked with the same rules as "
           "scripts\\enroll.py and saved to known/<code>/. Running camera workers pick up gallery changes within "
           "a few seconds.")
show_flash()
USER = config.DASHBOARD_USER
MIN = config.ENROLL_MIN_PHOTOS
st.session_state.setdefault("enroll_widget", 0)  # bumped to reset the uploader / camera after each use


def thumb(img, size: int = 200) -> bytes | None:
    """Small JPEG of a checked image, for showing rejected photos that aren't saved."""
    if img is None:
        return None
    scale = min(1.0, size / max(img.shape[:2]))
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
    ok, buf = cv2.imencode(".jpg", small)
    return buf.tobytes() if ok else None


def angles(info: dict) -> str:
    return f"yaw {info['yaw']:+.0f}°, pitch {info['pitch']:+.0f}°" if info.get("yaw") is not None else ""


def process(code: str, images: list[tuple[str, object]]) -> None:
    """Check, save accepted photos, rebuild the person's embeddings, remember the results for display.

    Every photo gets a visible result: a summary at the top of the page and a box per photo under the tabs.
    """
    detector, recognizer = face_models()
    with st.spinner("Checking photos..."), connection() as conn:
        results, _person = enrollment.add_photos(code, images, detector, recognizer, conn)
    checked = [(label, thumb(img), check.quality.to_json() if check.quality else {"ok": False,
                "reason": check.message or "Couldn't check this photo."}, path.name if path else None)
               for (label, check, path), (_, img) in zip(results, images)]
    st.session_state["enroll_last_check"] = (code, checked)
    saved = [c for c in checked if c[3]]
    refused = [c for c in checked if not c[3]]
    if len(checked) == 1:
        label, _, info, name = checked[0]
        if name:
            flash(f"Photo saved as {name}. Score {info['score']:.0f}, {angles(info)}.")
        else:
            flash(f"Photo not saved: {info['reason']}", "error")
    else:
        text = f"{len(saved)} photo(s) saved, {len(refused)} refused"
        flash(text + (": see the reasons below." if refused else "."),
              "success" if not refused else "error" if not saved else "warning")
    st.session_state.enroll_widget += 1


# --- Add a person ---------------------------------------------------------------

with connection() as conn:
    people = {p["employee_code"]: dict(p) for p in db.list_persons(conn)}

with st.expander("Add a person", icon=":material/person_add:", expanded=not people):
    with st.form("add_person", clear_on_submit=True):
        c1, c2 = st.columns(2)
        code = c1.text_input("Employee code", help="Letters, digits, '_', '-' or '.'; also the photo folder name")
        name = c2.text_input("Name")
        c3, c4 = st.columns(2)
        department = c3.text_input("Department")
        shift = c4.text_input("Shift")
        if st.form_submit_button("Add person"):
            try:
                code = enrollment.validate_code(code)
                with connection() as conn:
                    db.add_person(conn, code, name, department, shift, "added on the Enrollment page", USER)
                enrollment.person_dir(code).mkdir(parents=True, exist_ok=True)
            except ValueError as e:
                st.error(str(e).capitalize())
            else:
                st.session_state["enroll_person"] = code
                flash(f"Added {name.strip()} ({code}). Now add {MIN} to 10 photos.")
                st.rerun()

if not people:
    st.stop()

# --- One person's photos ----------------------------------------------------------

if st.session_state.get("enroll_person") not in people:
    st.session_state["enroll_person"] = next(iter(people))
code = st.selectbox("Person", list(people), key="enroll_person", format_func=lambda c: person_label(people[c]))
try:
    folder = enrollment.person_dir(code)
except ValueError as e:
    st.error(f"{code!r} can't be managed here: {e}. Rename its known/ folder and run scripts\\enroll.py.")
    st.stop()

photos = enrollment.photo_paths(folder)
stored = enrollment.read_sidecar(folder)  # known/<code>/photos.json: score, yaw, pitch, result per photo
if photos and not all(p.name in stored for p in photos):  # photos added by hand, or checked before photos.json
    detector, recognizer = face_models()
    with st.spinner("Checking photos..."):
        enrollment.write_sidecar(folder, enrollment.enroll_folder(folder, detector, recognizer))
    stored = enrollment.read_sidecar(folder)
usable = sum(1 for p in photos if stored.get(p.name, {}).get("ok"))
embeddings = enrollment.load_gallery().counts().get(code, 0)

if not people[code]["active"]:
    st.info("This person is inactive: the cameras won't record them. Activate them on the People page.")
c1, c2, c3 = st.columns(3)
c1.metric("Photos", len(photos))
c2.metric("Usable", usable)
c3.metric("Embeddings in gallery", embeddings)
if usable < MIN:
    st.warning(f"Only {usable} usable photo(s); {MIN} to 10 are recommended for reliable recognition.")

if photos:
    cols = st.columns(5)
    for i, photo in enumerate(photos):
        with cols[i % 5]:
            info = stored.get(photo.name, {})
            if info.get("ok"):
                caption = f"✅ {photo.name} · score {info['score']:.0f} · {angles(info)}"
            else:
                caption = f"❌ {photo.name}: {info.get('reason') or 'not checked'}"
            st.image(str(photo), caption=caption)
            if st.button("Delete", key=f"del_{code}_{photo.name}", icon=":material/delete:"):
                detector, recognizer = face_models()
                with st.spinner("Updating embeddings..."), connection() as conn:
                    enrollment.delete_photo(code, photo.name, detector, recognizer, conn)
                flash(f"Deleted {photo.name}; {code}'s embeddings rebuilt.")
                st.rerun()

# --- Guided photos (front, slightly left, slightly right) -------------------------------------

guided_capture.render(code)

# --- More photos by upload (optional) --------------------------------------------------------------

last = st.session_state.get("enroll_last_check")
last = last if last and last[0] == code else None
with st.expander("Upload more photos (optional)", icon=":material/upload:", expanded=last is not None):
    st.caption(f"Extra photos straight on or turned a little (up to {config.ENROLL_MAX_YAW:.0f}°), with and without "
               "glasses. One face per photo, in focus and well lit. Each one gets the same checks.")
    widget = st.session_state.enroll_widget
    files = st.file_uploader("Photos", type=sorted(e.lstrip(".") for e in config.IMAGE_EXTS),
                             accept_multiple_files=True, key=f"upload_{widget}")
    if st.button("Check and add", disabled=not files, type="primary"):
        process(code, [(f.name, enrollment.decode_image(f.getvalue())) for f in files])
        st.rerun()
    if last:
        st.markdown("**Last check**")
        cols = st.columns(3)
        for i, (label, image, info, saved) in enumerate(last[1]):
            info = info if isinstance(info, dict) else {"ok": info is None, "reason": info}  # older session format
            with cols[i % 3]:
                if image:
                    st.image(image, caption=label)
                if saved:
                    st.success(f"Saved as {saved}. Score {info.get('score', 0):.0f}" +
                               (f", {angles(info)}." if angles(info) else "."))
                else:
                    st.error(info.get("reason") or "Not saved.")

# --- Delete face data -------------------------------------------------------------------

with st.expander("Delete face data", icon=":material/delete_forever:"):
    st.warning(f"Permanently deletes {len(photos)} photo(s) in known/{code}/ and {embeddings} embedding(s) for "
               f"{people[code]['name']}. Their attendance history is kept. This is written to the audit log.")
    with st.form(f"delete_face_{code}"):
        reason = st.text_input("Reason (required)")
        deactivate = st.checkbox("Also deactivate this person (they stop showing as absent)", value=True)
        confirm = st.checkbox("I understand this cannot be undone")
        if st.form_submit_button("Delete face data", type="primary"):
            if not confirm:
                st.error("Tick the confirmation box first.")
            else:
                try:
                    with connection() as conn:
                        details = enrollment.delete_face_data(code, reason, USER, conn, deactivate)
                except ValueError as e:
                    st.error(str(e).capitalize())
                else:
                    flash(f"Deleted {details['photos']} photo(s) and {details['embeddings']} embedding(s) for {code}.")
                    st.rerun()
