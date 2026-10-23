"""Phase 3 data endpoints: overview, attendance, events and snapshots, corrections, people, cameras, sites, audit,
settings; paging/sorting/search/site filter; the OpenAPI schema; and camera passwords never in a response."""

import json
from datetime import date, datetime, time, timedelta

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import auth
from api.main import create_app
from api.workers import WorkerManager
from app import camera_settings, config, db, enrollment
from app.workers import WorkerManager as AppWorkerManager
from tests.fakes import FakeDetector, FakeRecognizer, photo
from tests.test_api import FAKE, wait_for

FIRST = "first-admin-password"
ADMIN = "admin-password-2026"
PW = "user-password-123"
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
CAM_PASSWORD = "S3cret-Cam-Pw!"  # typed in for a Hikvision camera: must never come back
ENV_PASSWORD = "Env-Secret-99"  # behind ${GATESENSE_CAM_YARD} for an RTSP camera
LITERAL_PASSWORD = "Literal-Pw-77"  # written into cameras.json by hand


@pytest.fixture(autouse=True)
def first_password(monkeypatch):
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, FIRST)


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm))


def seed() -> dict:
    """Four people, the two default cameras, a day of events today and one yesterday."""
    conn = db.connect()
    db.sync_persons(conn, ["1001", "1002", "1003", "1004"])
    for code, name, dept in (("1001", "Ayesha", "Stores"), ("1002", "Bilal", "Office"), ("1003", "Chand", "Stores"),
                             ("1004", "Dawood", "Gate")):
        conn.execute("UPDATE persons SET name = ?, department = ? WHERE employee_code = ?", (name, dept, code))
    conn.commit()
    ids = db.person_ids(conn)
    cin, cout = db.upsert_camera(conn, "entrance", 0, "IN"), db.upsert_camera(conn, "exit", 0, "OUT")
    events = {
        "a_in": db.add_event(conn, ids["1001"], cin, at(TODAY, "08:05"), 0.71, None),
        "a_out": db.add_event(conn, ids["1001"], cout, at(TODAY, "17:10"), 0.66, None),
        "b_in": db.add_event(conn, ids["1002"], cin, at(TODAY, "09:20"), 0.58, None),
        "c_out": db.add_event(conn, ids["1003"], cout, at(TODAY, "12:00"), 0.62, None),
        "a_in_y": db.add_event(conn, ids["1001"], cin, at(YESTERDAY, "08:00"), 0.70, None),
        "a_out_y": db.add_event(conn, ids["1001"], cout, at(YESTERDAY, "16:00"), 0.69, None),
    }
    conn.close()
    return {"people": ids, "cameras": {"entrance": cin, "exit": cout}, "events": events}


@pytest.fixture
def app():
    application = create_app(models=lambda: (FakeDetector(), FakeRecognizer()))
    with TestClient(application):  # tables + first admin
        application.state.seed = seed()
        yield application


def login(app, username: str, password: str) -> TestClient:
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return c


def admin(app) -> TestClient:
    c = TestClient(app)
    if c.post("/api/auth/login", json={"username": "admin", "password": ADMIN}).status_code == 200:
        return c  # the first-login password change is already done
    c = login(app, "admin", FIRST)
    assert c.post("/api/auth/password", json={"current_password": FIRST, "new_password": ADMIN}).status_code == 200
    return c


def as_role(app, role: str) -> TestClient:
    if role == "admin":
        return admin(app)
    app.state.db.create_user({"username": role, "full_name": role, "email": None, "role": role,
                              "password_hash": auth.hash_password(PW), "must_change_password": False,
                              "created_at": datetime.now()}, actor="test", reason="test setup")
    return login(app, role, PW)


def items(r) -> list[dict]:
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --- Attendance: the same day as the daily register report ---------------------------------------------


def test_attendance_matches_the_daily_register(app):
    viewer = as_role(app, "viewer")
    rows = items(viewer.get("/api/attendance", params={"from": TODAY.isoformat(), "to": TODAY.isoformat(),
                                                       "sort": "name"}))
    register = viewer.get("/api/reports/daily", params={"from": TODAY.isoformat(), "to": TODAY.isoformat()})

    def shape(r):
        return (r["employee_code"], r["name"], r["department"], r["first_in"], r["last_out"], r["worked_hours"],
                r["status"])

    assert sorted(map(shape, rows)) == sorted(map(shape, register.json()["rows"]))
    assert [r["status"] for r in rows] == ["present", "missing exit", "missing entry", "absent"]
    assert rows[0]["worked_minutes"] == 545 and rows[0]["worked_hours"] == 9.08


def test_attendance_filters_paging_and_sort(app):
    v = as_role(app, "viewer")
    ids = app.state.seed["people"]
    r = v.get("/api/attendance", params={"from": YESTERDAY.isoformat(), "to": TODAY.isoformat()})
    body = r.json()
    assert body["total"] == 8 and body["page"] == 1 and body["page_size"] == 50
    assert [x["date"] for x in body["items"]][:4] == [TODAY.isoformat()] * 4  # default -date, then name
    assert items(v.get("/api/attendance", params={"from": YESTERDAY.isoformat(), "person": ids["1001"]})) and all(
        x["employee_code"] == "1001" for x in items(v.get("/api/attendance", params={
            "from": YESTERDAY.isoformat(), "person": ids["1001"]})))
    assert [x["name"] for x in items(v.get("/api/attendance", params={"status": "missing exit"}))] == ["Bilal"]
    assert [x["name"] for x in items(v.get("/api/attendance", params={"q": "stor", "sort": "-name"}))] == [
        "Chand", "Ayesha"]
    page2 = v.get("/api/attendance", params={"sort": "name", "page": 2, "page_size": 3}).json()
    assert page2["total"] == 4 and [x["name"] for x in page2["items"]] == ["Dawood"]
    assert v.get("/api/attendance", params={"sort": "salary"}).status_code == 422
    assert v.get("/api/attendance", params={"from": TODAY.isoformat(), "to": YESTERDAY.isoformat()}).status_code == 422
    assert v.get("/api/attendance", params={"page_size": 501}).status_code == 422
    assert v.get("/api/attendance", params={"site_id": 999}).json()["total"] == 0
    assert v.get("/api/attendance", params={"site_id": 1}).json()["total"] == 4


# --- Overview ----------------------------------------------------------------------------------


def test_overview(app):
    o = as_role(app, "guard").get("/api/overview").json()
    assert (o["people"], o["present"], o["inside_now"], o["left"], o["absent"], o["missing_exit"],
            o["missing_entry"]) == (4, 2, 1, 1, 1, 1, 1)
    hours = {h["hour"]: h["arrivals"] for h in o["arrivals_by_hour"]}
    assert len(hours) == 24 and hours[8] == 1 and hours[9] == 1 and sum(hours.values()) == 2
    cams = {c["camera"]: c for c in o["cameras"]}
    assert cams["entrance"]["events"] == 2 and cams["exit"]["events"] == 2 and cams["entrance"]["state"] == "stopped"
    past = as_role(app, "viewer").get("/api/overview", params={"date": YESTERDAY.isoformat()}).json()
    assert past["inside_now"] is None and past["left"] == 1 and past["absent"] == 3


# --- Events and snapshots ------------------------------------------------------------------------


def test_events_filters(app):
    v = as_role(app, "viewer")
    s = app.state.seed
    today = items(v.get("/api/events"))
    assert [e["name"] for e in today] == ["Ayesha", "Chand", "Bilal", "Ayesha"]  # newest first
    first = today[-1]
    assert (first["direction"], first["camera"], first["score"], first["manual"], first["voided"],
            first["snapshot_url"], first["original_timestamp"]) == ("IN", "entrance", 0.71, False, False, None, None)
    assert v.get("/api/events", params={"from": YESTERDAY.isoformat()}).json()["total"] == 6
    assert len(items(v.get("/api/events", params={"direction": "OUT"}))) == 2
    assert len(items(v.get("/api/events", params={"camera": s["cameras"]["entrance"]}))) == 2
    assert [e["name"] for e in items(v.get("/api/events", params={"q": "bil"}))] == ["Bilal"]
    assert [e["name"] for e in items(v.get("/api/events", params={"sort": "name", "person": s["people"]["1001"]}))
            ] == ["Ayesha", "Ayesha"]
    assert v.get("/api/events", params={"site_id": 2}).json()["total"] == 0
    assert v.get("/api/events", params={"direction": "SIDEWAYS"}).status_code == 422


def test_snapshots_only_for_logged_in_users(app):
    s = app.state.seed
    snap = config.SNAPSHOT_DIR / TODAY.isoformat() / "081500_1001_entrance.jpg"
    snap.parent.mkdir(parents=True)
    cv2.imwrite(str(snap), np.full((40, 30, 3), 128, np.uint8))
    conn = db.connect()
    with_snap = db.add_event(conn, s["people"]["1001"], s["cameras"]["entrance"], at(TODAY, "08:15"), 0.8,
                             snap.relative_to(config.DATA_DIR).as_posix())
    escape = db.add_event(conn, s["people"]["1001"], s["cameras"]["entrance"], at(TODAY, "08:16"), 0.8,
                          "../../no-such/outside.jpg")
    conn.close()
    v = as_role(app, "viewer")
    event = next(e for e in items(v.get("/api/events")) if e["id"] == with_snap)
    assert event["snapshot_url"] == f"/api/snapshots/{with_snap}"
    r = v.get(event["snapshot_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == snap.read_bytes()
    assert TestClient(app).get(event["snapshot_url"]).status_code == 401
    assert v.get(f"/api/snapshots/{escape}").status_code == 404
    assert v.get(f"/api/snapshots/{s['events']['a_in']}").status_code == 404  # no snapshot


# --- Corrections -----------------------------------------------------------------------------------


def test_corrections_add_change_void_restore(app):
    hr = as_role(app, "hr")
    s = app.state.seed
    bilal = s["people"]["1002"]
    r = hr.post("/api/attendance/corrections", json={"action": "add", "person_id": bilal, "direction": "OUT",
                                                     "timestamp": at(YESTERDAY, "18:00").isoformat(),
                                                     "reason": "forgot to look at the exit camera"})
    assert r.status_code == 201
    added = r.json()
    assert (added["direction"], added["camera"], added["manual"], added["score"]) == ("OUT", "exit", True, None)

    a_in = s["events"]["a_in_y"]
    r = hr.post("/api/attendance/corrections", json={"action": "change", "event_id": a_in,
                                                     "timestamp": at(YESTERDAY, "07:45").isoformat(),
                                                     "reason": "badge log shows 07:45"})
    assert r.status_code == 201
    changed = r.json()
    assert changed["timestamp"].endswith("07:45:00") and changed["original_timestamp"].endswith("08:00:00")
    assert hr.post("/api/attendance/corrections", json={"action": "void", "event_id": a_in,
                                                        "reason": "duplicate"}).json()["voided"] is True
    assert hr.post("/api/attendance/corrections", json={"action": "restore", "event_id": a_in,
                                                        "reason": "not a duplicate"}).json()["voided"] is False

    day = items(hr.get("/api/attendance", params={"from": YESTERDAY.isoformat(), "to": YESTERDAY.isoformat(),
                                                  "sort": "name"}))
    assert day[0]["first_in"].endswith("07:45:00") and day[0]["status"] == "present"
    entries = items(as_role(app, "admin").get("/api/audit", params={"user": "hr"}))[:4]  # then hr's login
    assert [e["action"] for e in entries] == ["restore_event", "void_event", "edit_event", "add_event"]
    assert entries[2]["details"]["timestamp"] == [f"{YESTERDAY} 08:00:00", f"{YESTERDAY} 07:45:00"]
    assert entries[2]["reason"] == "badge log shows 07:45"


def test_correction_rules(app):
    hr = as_role(app, "hr")
    s = app.state.seed
    base = {"action": "add", "person_id": s["people"]["1004"], "timestamp": at(YESTERDAY, "09:00").isoformat(),
            "direction": "IN", "reason": "manual entry"}
    post = lambda body: hr.post("/api/attendance/corrections", json=body)  # noqa: E731
    assert post({**base, "reason": " "}).status_code == 422
    assert post({**base, "timestamp": (datetime.now() + timedelta(hours=1)).isoformat()}).status_code == 422
    assert post({**base, "camera_id": s["cameras"]["exit"]}).status_code == 422  # IN on an OUT camera
    assert post({**base, "person_id": 999}).status_code == 404
    assert post({"action": "change", "event_id": 999, "timestamp": base["timestamp"], "reason": "x"}).status_code == 404
    assert post({"action": "change", "event_id": s["events"]["a_in"], "reason": "x"}).status_code == 422
    for role in ("viewer", "guard"):
        r = as_role(app, role).post("/api/attendance/corrections", json=base)
        assert r.status_code == 403
    assert post(base).status_code == 201


# --- People ---------------------------------------------------------------------------------------


def add_photos(code: str, n: int = 3) -> None:
    folder = config.KNOWN_DIR / code
    folder.mkdir(parents=True)
    for i in range(n):
        cv2.imwrite(str(folder / f"{i + 1}.jpg"), photo("good", seed=i))


def test_people_list_and_profile(app):
    v = as_role(app, "viewer")
    add_photos("1001")
    people = items(v.get("/api/people"))
    assert [p["name"] for p in people] == ["Ayesha", "Bilal", "Chand", "Dawood"]
    assert people[0]["photos"] == 3 and people[0]["embeddings"] == 0 and people[0]["site"] == config.DEFAULT_SITE_NAME
    assert [p["name"] for p in items(v.get("/api/people", params={"q": "stores", "sort": "-name"}))] == [
        "Chand", "Ayesha"]
    detail = v.get(f"/api/people/{people[0]['id']}").json()
    assert [ph["file"] for ph in detail["photo_list"]] == ["1.jpg", "2.jpg", "3.jpg"]
    assert detail["photo_list"][0]["accepted"] is None  # not checked yet
    assert [d["status"] for d in detail["recent_attendance"][:2]] == ["present", "present"]
    assert len(detail["recent_events"]) == 4
    img = v.get(detail["photo_list"][0]["url"])
    assert img.status_code == 200 and img.content == (config.KNOWN_DIR / "1001" / "1.jpg").read_bytes()
    assert v.get(f"/api/people/{people[0]['id']}/photos/..%2F..%2Fattendance.db").status_code == 404
    assert v.get("/api/people/999").status_code == 404


def test_people_changes_need_a_reason_and_are_audited(app):
    hr = as_role(app, "hr")
    r = hr.post("/api/people", json={"employee_code": "2001", "name": "Erum", "department": "HR", "reason": "joined"})
    assert r.status_code == 201 and r.json()["active"] is True and r.json()["site_id"] == 1
    pid = r.json()["id"]
    assert hr.post("/api/people", json={"employee_code": "2001", "name": "X", "reason": "x"}).status_code == 409
    assert hr.post("/api/people", json={"employee_code": "bad code!", "name": "X", "reason": "x"}).status_code == 422
    added = hr.post("/api/people", json={"employee_code": "2002", "name": "X"})  # enrollment: the reason is optional
    assert added.status_code == 201, added.text
    assert items(as_role(app, "admin").get("/api/audit", params={"action": "add_person"}))[0]["reason"] ==         "added for enrollment"

    site = as_role(app, "admin").post("/api/sites", json={"name": "Warehouse", "reason": "second gate"}).json()
    r = hr.patch(f"/api/people/{pid}", json={"shift": "night", "site_id": site["id"], "reason": "moved"})
    assert r.status_code == 200 and (r.json()["shift"], r.json()["site_id"], r.json()["department"]) == (
        "night", site["id"], "HR")
    assert hr.patch(f"/api/people/{pid}", json={"site_id": 999, "reason": "x"}).status_code == 422
    assert hr.post(f"/api/people/{pid}/deactivate", json={"reason": "left"}).json()["active"] is False
    assert hr.get("/api/people", params={"active": "false"}).json()["total"] == 1
    assert hr.post(f"/api/people/{pid}/activate", json={"reason": "back"}).json()["active"] is True
    actions = [e["action"] for e in items(as_role(app, "admin").get("/api/audit", params={"user": "hr"}))][:4]
    assert actions == ["activate_person", "deactivate_person", "edit_person", "add_person"]
    assert as_role(app, "viewer").post("/api/people", json={"employee_code": "3", "name": "Y",
                                                            "reason": "x"}).status_code == 403


def test_reenroll(app):
    hr = as_role(app, "hr")
    pid = app.state.seed["people"]["1001"]
    assert hr.post(f"/api/people/{pid}/reenroll", json={"reason": "new photos"}).status_code == 409  # no photos
    add_photos("1001")
    r = hr.post(f"/api/people/{pid}/reenroll", json={"reason": "new photos"})
    assert r.status_code == 200 and r.json()["accepted"] == ["1.jpg", "2.jpg", "3.jpg"] and r.json()["embeddings"] == 3
    assert enrollment.load_gallery().counts() == {"1001": 3}
    detail = hr.get(f"/api/people/{pid}").json()
    assert detail["embeddings"] == 3 and detail["photo_list"][0]["accepted"] is True
    assert items(as_role(app, "admin").get("/api/audit", params={"action": "reenroll_person"}))[0]["details"] == {
        "accepted": 3, "rejected": 0}


# --- Sites ------------------------------------------------------------------------------------------


def test_sites(app):
    a, hr = as_role(app, "admin"), as_role(app, "hr")
    assert [(s["name"], s["cameras"], s["people"]) for s in items(hr.get("/api/sites"))] == [
        (config.DEFAULT_SITE_NAME, 2, 4)]
    assert hr.post("/api/sites", json={"name": "Yard", "reason": "x"}).status_code == 403
    assert a.post("/api/sites", json={"name": "Yard", "timezone": "Mars/Olympus", "reason": "x"}).status_code == 422
    yard = a.post("/api/sites", json={"name": "Yard", "timezone": "Asia/Karachi", "reason": "new gate"})
    assert yard.status_code == 201 and yard.json()["timezone"] == "Asia/Karachi"
    assert a.post("/api/sites", json={"name": "yard", "reason": "x"}).status_code == 409
    sid = yard.json()["id"]
    assert a.patch(f"/api/sites/{sid}", json={"name": "Back yard", "reason": "renamed"}).json()["name"] == "Back yard"
    assert a.delete("/api/sites/1", params={"reason": "x"}).status_code == 409  # has cameras and people
    assert a.delete(f"/api/sites/{sid}").status_code == 422  # no reason
    assert a.delete(f"/api/sites/{sid}", params={"reason": "not needed"}).status_code == 204
    assert a.get(f"/api/sites/{sid}").status_code == 404
    assert [e["action"] for e in items(a.get("/api/audit", params={"q": "site:"}))][:3] == [
        "delete_site", "edit_site", "add_site"]


# --- Audit ---------------------------------------------------------------------------------------


def test_audit_filters_and_access(app):
    a = as_role(app, "admin")
    for i in range(3):
        a.post("/api/sites", json={"name": f"S{i}", "reason": f"reason {i}"})
    page = a.get("/api/audit", params={"action": "add_site", "page_size": 2}).json()
    assert page["total"] == 3 and [e["target"] for e in page["items"]] == ["site:S2", "site:S1"]
    assert items(a.get("/api/audit", params={"q": "reason 1"}))[0]["target"] == "site:S1"
    assert a.get("/api/audit", params={"from": TODAY.isoformat(), "to": TODAY.isoformat()}).json()["total"] >= 3
    assert a.get("/api/audit", params={"to": YESTERDAY.isoformat()}).json()["total"] == 0
    assert a.get("/api/audit", params={"sort": "-details"}).status_code == 422
    assert as_role(app, "hr").get("/api/audit").status_code == 200
    assert as_role(app, "viewer").get("/api/audit").status_code == 403


# --- Settings ----------------------------------------------------------------------------------------


def test_settings(app):
    a = as_role(app, "admin")
    s = {x["key"]: x for x in a.get("/api/settings").json()["settings"]}
    assert s["match_threshold"]["value"] == config.DEFAULT_SETTINGS["MATCH_THRESHOLD"] and not s["match_threshold"][
        "changed"]
    assert s["cooldown_seconds"]["type"] == "int" and "debounce" in s["cooldown_seconds"]["description"]
    r = a.patch("/api/settings", json={"values": {"match_threshold": 0.5, "cooldown_seconds": 600},
                                       "reason": "too many false matches"})
    assert r.status_code == 200
    s = {x["key"]: x for x in r.json()["settings"]}
    assert s["match_threshold"]["value"] == 0.5 and s["match_threshold"]["changed"] and s["cooldown_seconds"]["value"] == 600
    assert config.MATCH_THRESHOLD == 0.5 and config.COOLDOWN_SECONDS == 600
    assert json.loads(config.SETTINGS_FILE.read_text())["settings"] == {"MATCH_THRESHOLD": 0.5, "COOLDOWN_SECONDS": 600}
    entry = items(a.get("/api/audit", params={"action": "change_settings"}))[0]
    assert entry["details"] == {"match_threshold": [0.45, 0.5], "cooldown_seconds": [300, 600]}
    assert entry["reason"] == "too many false matches"

    assert a.patch("/api/settings", json={"values": {"match_threshold": 2}, "reason": "x"}).status_code == 422
    assert a.patch("/api/settings", json={"values": {"model_pack": 1}, "reason": "x"}).status_code == 422
    assert a.patch("/api/settings", json={"values": {"vote_min_agree": 9}, "reason": "x"}).status_code == 422
    assert a.patch("/api/settings", json={"values": {"cooldown_seconds": 1.5}, "reason": "x"}).status_code == 422
    assert a.patch("/api/settings", json={"values": {"match_threshold": 0.45}}).status_code == 422  # no reason
    back = a.patch("/api/settings", json={"values": {"match_threshold": 0.45}, "reason": "back to default"})
    assert json.loads(config.SETTINGS_FILE.read_text())["settings"] == {"COOLDOWN_SECONDS": 600}
    assert back.status_code == 200 and as_role(app, "hr").get("/api/settings").status_code == 403


def test_settings_file_is_applied_on_load(tmp_path):
    config.SETTINGS_FILE.write_text(json.dumps({"settings": {"MIN_FACE_PX": 120, "MATCH_THRESHOLD": 7}}))
    assert config.load_settings() == {"MIN_FACE_PX": 120}  # the out-of-range value is skipped
    assert config.MIN_FACE_PX == 120 and config.MATCH_THRESHOLD == config.DEFAULT_SETTINGS["MATCH_THRESHOLD"]
    config.SETTINGS_FILE.unlink()
    assert config.refresh_settings() and config.MIN_FACE_PX == config.DEFAULT_SETTINGS["MIN_FACE_PX"]


# --- Cameras ---------------------------------------------------------------------------------------


def clip(path, frames: int = 20, size=(320, 240), fps: float = 15.0):
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    for i in range(frames):
        w.write(np.full((size[1], size[0], 3), i * 10 % 255, np.uint8))
    w.release()
    return path


def test_cameras_list_create_and_patch(app, tmp_path):
    a = as_role(app, "admin")
    cams = items(a.get("/api/cameras"))
    assert [(c["name"], c["direction"], c["type"], c["source"], c["worker"]["state"]) for c in cams] == [
        ("entrance", "IN", "webcam", 0, "stopped"), ("exit", "OUT", "webcam", 0, "stopped")]
    assert cams[0]["id"] == app.state.seed["cameras"]["entrance"]

    r = a.post("/api/cameras", json={"name": "gate1", "direction": "IN", "type": "hikvision", "ip": "192.168.1.101",
                                     "user": "gatesense", "password": CAM_PASSWORD, "roi": {"x": 0.1, "y": 0.2, "w": 0.5,
                                                                                           "h": 0.6}})
    assert r.status_code == 201, r.text
    gate = r.json()
    assert gate["source"] == "rtsp://gatesense:${GATESENSE_CAM_GATE1_PASS}@192.168.1.101:554/Streaming/Channels/101"
    assert gate["password_variable"] == "GATESENSE_CAM_GATE1_PASS" and gate["password_set"] is True
    assert gate["roi"] == {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.6} and gate["missing_variables"] == []
    assert a.post("/api/cameras", json={"name": "GATE1", "direction": "IN", "type": "webcam"}).status_code == 409
    literal = {"name": "yard", "direction": "OUT", "type": "rtsp", "url": f"rtsp://u:{LITERAL_PASSWORD}@10.0.0.2/1"}
    assert a.post("/api/cameras", json=literal).status_code == 422  # a typed-in password is refused

    r = a.patch(f"/api/cameras/{gate['id']}", json={"mirror": True, "roi": None, "stream": "sub"})
    assert r.status_code == 200 and r.json()["mirror"] and r.json()["roi"] is None
    assert r.json()["source"].endswith("/Streaming/Channels/102")
    site = a.post("/api/sites", json={"name": "North", "reason": "x"}).json()
    r = a.patch(f"/api/cameras/{gate['id']}", json={"site_id": site["id"], "reason": "moved north"})
    assert r.json()["site_id"] == site["id"]
    assert [c["name"] for c in items(a.get("/api/cameras", params={"site_id": site["id"]}))] == ["gate1"]
    assert a.patch(f"/api/cameras/{gate['id']}", json={"direction": "UP"}).status_code == 422
    assert a.get("/api/cameras/999").status_code == 404
    assert as_role(app, "guard").post("/api/cameras", json={"name": "x", "direction": "IN",
                                                            "type": "webcam"}).status_code == 403
    actions = [e["action"] for e in items(a.get("/api/audit", params={"q": "camera:gate1"}))]
    assert actions == ["set_camera_site", "edit_camera", "add_camera"]


def test_camera_test_endpoint(app, tmp_path):
    a = as_role(app, "admin")
    video = clip(tmp_path / "walk.avi")
    r = a.post("/api/cameras/test", json={"type": "file", "path": str(video), "direction": "IN", "timeout_seconds": 5})
    assert r.status_code == 200 and r.json()["ok"]
    assert (r.json()["width"], r.json()["height"], r.json()["fps"]) == (320, 240, 15.0)
    r = a.post("/api/cameras/test", json={"type": "file", "path": str(tmp_path / "none.avi"), "direction": "IN"})
    assert r.json()["ok"] is False and "video file not found" in r.json()["error"]
    r = a.post("/api/cameras/test", json={"type": "hikvision", "ip": "10.0.0.5", "user": "u", "direction": "IN"})
    assert r.json()["ok"] is False and "GATESENSE_CAM_CONNECTION_TEST_PASS" in r.json()["error"]
    assert a.post("/api/cameras/test", json={"type": "teleport", "direction": "IN"}).status_code == 422
    assert as_role(app, "viewer").post("/api/cameras/test", json={"type": "webcam"}).status_code == 403


@pytest.fixture
def worker_app(tmp_path):
    """The API with fake camera workers; cameras: a (webcam 0) and c (a video file)."""
    video = clip(tmp_path / "c.avi")
    camera_settings.save([
        {"name": "a", "direction": "IN", "mirror": False, "type": "webcam", "index": 0, "source": 0},
        {"name": "c", "direction": "OUT", "mirror": False, "type": "file", "path": str(video), "source": str(video)},
    ])
    run, logs, live_dir = tmp_path / "run", tmp_path / "logs", tmp_path / "live"
    inner = AppWorkerManager(None, script=FAKE, run_dir=run, log_dir=logs, live_dir=live_dir,
                             extra_args=("--no-window", "--run-dir", str(run), "--frames", "--live-dir", str(live_dir)))
    application = create_app(workers=WorkerManager(inner))
    try:
        with TestClient(application):
            yield application
    finally:
        for proc in inner._procs.values():
            proc.kill()


def test_running_cameras(worker_app):
    a = as_role(worker_app, "admin")
    ids = {c["name"]: c["id"] for c in items(a.get("/api/cameras"))}
    assert a.post(f"/api/cameras/{ids['a']}/start").status_code == 200
    wait_for(lambda: a.get(f"/api/cameras/{ids['a']}").json()["worker"]["state"] == "running")
    cam = a.get(f"/api/cameras/{ids['a']}").json()
    assert cam["worker"]["held_by"] == "api" and cam["worker"]["last_frame_at"]
    r = a.patch(f"/api/cameras/{ids['a']}", json={"mirror": True})
    assert r.status_code == 409 and "running" in r.json()["detail"]
    assert a.post("/api/cameras/test", json={"camera_id": ids["a"]}).status_code == 409  # never opened twice
    assert a.post("/api/cameras/test", json={"type": "webcam", "index": 0, "direction": "IN"}).status_code == 409
    assert a.post("/api/cameras/test", json={"camera_id": ids["c"], "timeout_seconds": 5}).json()["ok"] is True
    overview = {c["camera"]: c for c in a.get("/api/overview").json()["cameras"]}
    assert overview["a"]["state"] == "running" and overview["a"]["fps"] is not None
    assert a.post(f"/api/cameras/{ids['a']}/stop").json()["result"] == "stopped cleanly"


# --- Passwords never in a response ------------------------------------------------------------------


def test_camera_passwords_never_appear_in_responses(app, monkeypatch, tmp_path):
    a = as_role(app, "admin")
    monkeypatch.setenv("GATESENSE_CAM_YARD", ENV_PASSWORD)
    bodies = [a.post("/api/cameras", json={"name": "gate1", "direction": "IN", "type": "hikvision",
                                           "ip": "192.168.1.101", "user": "gatesense", "password": CAM_PASSWORD}),
              a.post("/api/cameras", json={"name": "yard", "direction": "OUT", "type": "rtsp",
                                           "url": "rtsp://cam:${GATESENSE_CAM_YARD}@127.0.0.1:9/stream"})]
    gate = bodies[0].json()["id"]
    bodies.append(a.patch(f"/api/cameras/{gate}", json={"password": CAM_PASSWORD + "2", "port": 8554}))
    # A cameras.json edited by hand with the password typed into the URL: masked on the way out.
    entries = camera_settings.entries() + [{"name": "legacy", "direction": "IN", "mirror": False, "type": "rtsp",
                                            "url": f"rtsp://admin:{LITERAL_PASSWORD}@127.0.0.1:9/x",
                                            "source": f"rtsp://admin:{LITERAL_PASSWORD}@127.0.0.1:9/x"}]
    camera_settings.save(entries)
    cams = {c["name"]: c for c in items(a.get("/api/cameras"))}
    assert cams["legacy"]["source"] == "rtsp://admin:***@127.0.0.1:9/x"
    assert cams["yard"]["source"] == "rtsp://cam:${GATESENSE_CAM_YARD}@127.0.0.1:9/stream"  # the placeholder only
    bodies += [a.post("/api/cameras/test", json={"camera_id": cams["legacy"]["id"], "timeout_seconds": 1}),
               a.post("/api/cameras/test", json={"camera_id": cams["yard"]["id"], "timeout_seconds": 1}),
               a.post("/api/cameras/test", json={"type": "hikvision", "ip": "127.0.0.1", "port": 9, "user": "u",
                                                 "password": CAM_PASSWORD, "direction": "IN", "timeout_seconds": 1})]
    for path in ("/api/cameras", f"/api/cameras/{gate}", "/api/overview", "/api/audit?page_size=500", "/api/settings",
                 "/api/events", "/api/attendance", "/api/people", "/api/sites", "/api/users", "/api/auth/me",
                 "/openapi.json"):
        bodies.append(a.get(path))
    assert config.ENV_PATH.read_text().count(CAM_PASSWORD) == 1  # it was saved, to .env only
    for r in bodies:
        assert r.status_code < 500, r.text
        for secret in (CAM_PASSWORD, ENV_PASSWORD, LITERAL_PASSWORD):
            assert secret not in r.text, (r.request.url, secret)


# --- The OpenAPI schema --------------------------------------------------------------------------------


def test_openapi_lists_every_endpoint_with_a_response_model(app):
    spec = TestClient(app).get("/openapi.json").json()
    ops = [(m, p, o) for p, ms in spec["paths"].items() for m, o in ms.items()]
    assert len(ops) == 65 and len({o["operationId"] for _, _, o in ops}) == 65  # + live, enrollment (IP cameras), system
    expected = {("get", "/api/overview"), ("get", "/api/attendance"), ("get", "/api/events"),
                ("get", "/api/snapshots/{event_id}"), ("post", "/api/attendance/corrections"), ("get", "/api/people"),
                ("post", "/api/people"), ("get", "/api/people/{person_id}"), ("patch", "/api/people/{person_id}"),
                ("post", "/api/people/{person_id}/deactivate"), ("post", "/api/people/{person_id}/reenroll"),
                ("get", "/api/cameras"), ("post", "/api/cameras"), ("patch", "/api/cameras/{camera_id}"),
                ("post", "/api/cameras/{camera_id}/start"), ("post", "/api/cameras/{camera_id}/stop"),
                ("post", "/api/cameras/test"), ("get", "/api/sites"), ("post", "/api/sites"),
                ("patch", "/api/sites/{site_id}"), ("delete", "/api/sites/{site_id}"), ("get", "/api/audit"),
                ("get", "/api/settings"), ("patch", "/api/settings")}
    assert expected <= {(m, p) for m, p, _ in ops}
    for method, path, op in ops:
        ok = next(code for code in op["responses"] if code.startswith("2"))
        if ok == "204":
            continue
        content = op["responses"][ok]["content"]
        if "application/json" not in content:  # files: images, the MJPEG stream, log.txt, report exports
            assert any(t.startswith(("image/", "multipart/", "text/", "application/vnd.")) for t in content), path
            continue
        schema = content["application/json"]["schema"]
        assert schema and schema != {}, (method, path)  # a declared model, not "any"
    for name in ("Page_Event_", "Page_AttendanceRow_", "Overview", "Camera", "PersonDetail", "Settings",
                 "AuditEntry", "Site"):
        assert name in spec["components"]["schemas"], name
