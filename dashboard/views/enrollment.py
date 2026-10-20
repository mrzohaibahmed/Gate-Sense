import cv2
import streamlit as st

from app import config, db, enrollment
from dashboard.common import connection, face_models, flash, manager, person_label, show_flash

st.title("Enrollment")
st.caption("Photos are checked with the same quality filter as scripts\\enroll.py and saved to known/<code>/. "
           "Running camera workers pick up gallery changes within a few seconds.")
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


def process(code: str, images: list[tuple[str, object]]) -> None:
    """Check, save accepted photos, rebuild the person's embeddings, remember the results for display."""
    detector, recognizer = face_models()
    with st.spinner("Checking photos..."), connection() as conn:
        results, person = enrollment.add_photos(code, images, detector, recognizer, conn)
    st.session_state["enroll_last_check"] = (code, [
        (label, thumb(img), check.reason, path.name if path else None)
        for (label, check, path), (_, img) in zip(results, images)
    ])
    if person is not None:
        st.session_state[f"enroll_result:{code}"] = person
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
result_key = f"enroll_result:{code}"
if photos and result_key not in st.session_state:
    detector, recognizer = face_models()
    with st.spinner("Checking photos..."):
        st.session_state[result_key] = enrollment.enroll_folder(folder, detector, recognizer)
result = st.session_state.get(result_key)
rejected = dict(result.rejected) if result else {}
usable = len(result.accepted) if result else 0
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
            reason = rejected.get(photo.name)
            st.image(str(photo), caption=f"✅ {photo.name}" if reason is None else f"❌ {photo.name}: {reason}")
            if st.button("Delete", key=f"del_{code}_{photo.name}", icon=":material/delete:"):
                detector, recognizer = face_models()
                with st.spinner("Updating embeddings..."), connection() as conn:
                    st.session_state[result_key] = enrollment.delete_photo(code, photo.name, detector, recognizer,
                                                                           conn)
                flash(f"Deleted {photo.name}; {code}'s embeddings rebuilt.")
                st.rerun()

# --- Add photos -----------------------------------------------------------------------

st.subheader("Add photos")
st.caption("Straight on, slightly left and right, with and without glasses. One face per photo, in focus.")
upload_tab, camera_tab = st.tabs(["Upload files", "Camera"])
widget = st.session_state.enroll_widget

with upload_tab:
    files = st.file_uploader("Photos", type=sorted(e.lstrip(".") for e in config.IMAGE_EXTS),
                             accept_multiple_files=True, key=f"upload_{widget}")
    if st.button("Check and add", disabled=not files, type="primary"):
        process(code, [(f.name, enrollment.decode_image(f.getvalue())) for f in files])
        st.rerun()

with camera_tab:
    users = manager().webcam_users()
    if users:
        st.warning(f"The webcam is in use by the {', '.join(users)} worker. Stop it on the Cameras page to "
                   "take photos here.")
    else:
        shot = st.camera_input("Take a photo (one photo per click)", key=f"camera_{widget}")
        if shot is not None:
            process(code, [("camera photo", enrollment.decode_image(shot.getvalue()))])
            st.rerun()

last = st.session_state.get("enroll_last_check")
if last and last[0] == code:
    st.markdown("**Last check**")
    cols = st.columns(5)
    for i, (label, image, reason, saved) in enumerate(last[1]):
        with cols[i % 5]:
            caption = f"✅ {label}: saved as {saved}" if reason is None else f"❌ {label}: {reason}"
            if image:
                st.image(image, caption=caption)
            else:
                st.markdown(caption)

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
                    st.session_state.pop(result_key, None)
                    flash(f"Deleted {details['photos']} photo(s) and {details['embeddings']} embedding(s) for {code}.")
                    st.rerun()
