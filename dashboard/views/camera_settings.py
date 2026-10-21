import pandas as pd
import streamlit as st

from app import camera_settings as cs
from app import config
from app.camera import is_webcam, mask_source
from dashboard.common import camera_claimed, connection, flash, manager, show_flash

st.title("Camera settings")
st.caption("Add, change, test or remove cameras. They are saved to cameras.json in the project folder; "
           "camera passwords go to .env, never into cameras.json. A running camera must be stopped first, "
           "and changes apply the next time a camera starts.")
show_flash()

USER = config.DASHBOARD_USER
ADD = "➕ Add a camera"
statuses = {s.camera: s for s in manager().statuses()}
running = {name for name, s in statuses.items() if s.state != "stopped"}
cams = cs.entries()
by_name = {c["name"]: c for c in cams}


def password_label(cam: dict) -> str:
    if cam["type"] != "hikvision":
        return ""
    where = cs.password_status(cs.password_var(cam["name"]))
    return {"environment": "set (environment)", ".env": "set (.env)"}.get(where, "NOT SET")


def error(e: Exception) -> None:
    st.error(str(e)[:1].upper() + str(e)[1:])


st.dataframe(
    pd.DataFrame([{
        "Name": c["name"], "Direction": c["direction"], "Type": cs.TYPES.get(c["type"], c["type"]),
        "Source": mask_source(c["source"]), "Mirror": bool(c.get("mirror")), "Password": password_label(c),
        "Status": "running" if c["name"] in running else "stopped",
    } for c in cams]),
    hide_index=True,
)
if not config.CAMERAS_FILE.exists():
    st.caption("Showing the defaults from app/config.py; the first save creates cameras.json.")

if "cs_next_choice" in st.session_state:  # set after Save/Delete; must be applied before the widget exists
    st.session_state["cs_choice"] = st.session_state.pop("cs_next_choice")
if st.session_state.get("cs_choice") not in [ADD, *by_name]:
    st.session_state["cs_choice"] = ADD
choice = st.selectbox("Camera", [ADD] + list(by_name), key="cs_choice")
old = by_name.get(choice)
k = f"cs_{choice}_"  # widget keys per camera, so switching cameras shows that camera's values
locked = old is not None and choice in running
if locked:
    st.info(f"The {choice} camera is running. Stop it on the Cameras page to change, test or delete it.")

with st.container(border=True):
    # Name and type are outside the form: they decide which fields are shown below.
    c1, c2 = st.columns(2)
    name = c1.text_input("Name", value=old["name"] if old else "", key=k + "name", disabled=old is not None,
                         help="Used on every page and in file names; can't be changed later. E.g. gate1")
    types = list(cs.TYPES)
    kind = c2.selectbox("Type", types, format_func=cs.TYPES.get, key=k + "type",
                        index=types.index(old["type"]) if old else types.index("hikvision"))
    entry = {"name": name if not old else old["name"], "type": kind}
    password = None

    # A form sends every field's current value when a button is pressed. Without it, a password typed and
    # left with Tab (focus moves to the field's eye icon) was never sent to the app.
    # enter_to_submit=False: Enter in a field would otherwise press the first button and save the camera.
    with st.form(k + kind + "_form", border=False, enter_to_submit=False):
        entry["direction"] = st.radio("Direction", ["IN", "OUT"], horizontal=True, key=k + "direction",
                                      index=["IN", "OUT"].index(old["direction"]) if old else 0,
                                      help="IN = entrance camera, OUT = exit camera")
        if kind == "webcam":
            entry["index"] = st.number_input("Webcam number", 0, 9, value=int((old or {}).get("index", 0)),
                                             key=k + "index", help="0 = the first webcam, 1 = a second USB webcam")
        elif kind == "hikvision":
            c1, c2 = st.columns([3, 1])
            entry["ip"] = c1.text_input("IP address", value=(old or {}).get("ip", ""), key=k + "ip",
                                        placeholder="192.168.1.101")
            entry["port"] = c2.number_input("RTSP port", 1, 65535, value=int((old or {}).get("port", 554)),
                                            key=k + "port")
            c1, c2 = st.columns(2)
            entry["user"] = c1.text_input("User name", value=(old or {}).get("user", ""), key=k + "user",
                                          help="Best: a separate user for GateSense on the camera, with view rights only")
            var = cs.password_var(entry["name"] or "NAME")
            status = cs.password_status(var) if entry["name"] else None
            password = c2.text_input(
                "Password", type="password", key=k + "password",
                placeholder="leave empty to keep the current password" if status else "camera password",
                help=f"Saved to .env as {var}, not to cameras.json. Special characters are fine.")
            c1, c2 = st.columns(2)
            entry["channel"] = c1.number_input("Channel", 1, 64, value=int((old or {}).get("channel", 1)),
                                               key=k + "channel", help="1 for a single camera; NVR channel otherwise")
            entry["stream"] = c2.radio("Stream", ["main", "sub"], horizontal=True, key=k + "stream",
                                       index=["main", "sub"].index((old or {}).get("stream", "main")),
                                       help="main = full resolution (best for recognition), sub = lower resolution")
            if entry["name"]:
                st.caption(f"Password: {'saved in ' + status if status else 'not set yet'} ({var}). "
                           "Leave the field empty to keep it.")
                if status == "environment":
                    st.caption(f"Note: an environment variable {var} is set and wins over .env.")
        elif kind == "rtsp":
            entry["url"] = st.text_input(
                "RTSP URL", value=(old or {}).get("url", ""), key=k + "url",
                placeholder="rtsp://user:${GATESENSE_CAM_PASS}@192.168.1.101:554/stream",
                help="Write the password as ${VARIABLE} and set that variable in .env; a typed password is refused.")
        else:
            entry["path"] = st.text_input("Video file", value=(old or {}).get("path", ""), key=k + "path",
                                          placeholder=r"D:\clips\exit.mp4")
        entry["mirror"] = st.checkbox("Mirror the live view (like a mirror; display only)", key=k + kind + "_mirror",
                                      value=bool((old or {}).get("mirror", kind == "webcam")))
        reason = st.text_input("Note for the audit log (optional)", key=k + "reason")

        # Preview of the source the worker will use (masked), as of the last button press.
        try:
            preview = cs.build(entry, [c for c in cams if c is not old])
            st.caption(f"Source: `{mask_source(preview['source'])}`")
        except ValueError:
            pass  # shown when Save is pressed

        b1, b2 = st.columns(2)
        save = b1.form_submit_button("Save" if old else "Add camera", type="primary", disabled=locked)
        test = b2.form_submit_button("Test connection", disabled=locked)

    if save:
        try:
            with connection() as conn:
                saved = cs.upsert(entry, USER, conn, running, password=password or None, reason=reason)
        except ValueError as e:
            error(e)
        else:
            st.session_state["cs_next_choice"] = saved["name"]
            flash(f"Saved camera {saved['name']}. Start it on the Cameras page.")
            st.rerun()

    if test:
        try:
            built = cs.build(entry, [c for c in cams if c is not old])
        except ValueError as e:
            error(e)
        else:
            src = built["source"]
            busy = [n for n in running if is_webcam(statuses[n].source) and statuses[n].source == src]
            if is_webcam(src) and (busy or camera_claimed()):
                st.error("That webcam is in use (" + (f"by the {busy[0]} worker" if busy else
                         "by the Enrollment page camera") + "); stop it first.")
            else:
                extra = {cs.password_var(built["name"]): password} if password else None
                with st.spinner("Connecting (up to 12 s)..."):
                    result = cs.test_connection(src, extra=extra)
                if result["ok"]:
                    st.success(f"Connected: {result['width']}×{result['height']}"
                               + (f", codec {result['codec']}" if result["codec"] not in (None, "unknown") else ""))
                    st.image(result["frame"], channels="BGR", caption="Test picture (not saved)")
                else:
                    st.error(f"Could not get a picture: {result['error']}")

    if old:
        with st.popover("Delete", disabled=locked):
            st.write(f"Remove **{old['name']}** from the camera list? Its attendance history is kept.")
            if st.button("Delete camera", type="primary", key=k + "delete"):
                try:
                    with connection() as conn:
                        cs.delete(old["name"], USER, conn, running, reason=reason)
                except ValueError as e:
                    error(e)
                else:
                    st.session_state["cs_next_choice"] = ADD
                    flash(f"Deleted camera {old['name']}.")
                    st.rerun()

if old and len(cams) > 1:
    c1, c2, _ = st.columns([1, 1, 4])
    if c1.button("Move up", disabled=cams[0]["name"] == old["name"], key=k + "up"):
        cs.move(old["name"], -1)
        st.rerun()
    if c2.button("Move down", disabled=cams[-1]["name"] == old["name"], key=k + "down"):
        cs.move(old["name"], +1)
        st.rerun()
    st.caption("The first camera is the default for scripts\\recognize.py without --camera.")
