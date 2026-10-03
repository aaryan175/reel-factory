"""Clip grader: queue order, append-only grade lines, latest-wins folding,
validation, proxies through /media, the page, and the /audit mount."""

import json
import os
from pathlib import Path

import pytest

from server import settings
from tests.test_app import client, login  # noqa: F401  (fixture + helper, same login pattern)

# Real-corpus checks run only when the configured volume holds a library (tests/conftest.py points
# every setting at a temp dir, so by default they skip).
REAL_PROXIES = settings.FOOTAGE_LIBRARY_ROOT / "proxies"
AUDIT_PROXIES = Path(os.environ.get("REEL_DECK_AUDIT_DIR") or settings.WORKBENCH / "footage-intake" / "audit") / "proxies"


def _clip(clip_id, name, intake=None, proxy=True):
    return {"id": clip_id, "name": name, "duration": 5.0, "intake": intake,
            "proxy": f"proxies/{clip_id}.mp4" if proxy else None,
            "tags": {"subject": "person", "notes": f"note {name}"}}


@pytest.fixture()
def library(tmp_path, monkeypatch):
    """A synthetic library touching every queue group, plus a tmp grades file."""
    from server import grader

    clips = {
        "id-empty": _clip("id-empty", "A_EMPTY.MP4"),
        "id-noproxy": _clip("id-noproxy", "A_NOPROXY.MP4", proxy=False),
        "id-faint": _clip("id-faint", "A_FAINT.MP4"),
        "id-unscanned": _clip("id-unscanned", "A_UNSCANNED.MP4"),
        "id-ruled": _clip("id-ruled", "A_RULED.MP4"),
        "id-unruled": _clip("id-unruled", "Z_UNRULED.MP4"),
        "id-new-b": _clip("id-new-b", "Z_NEW_B.MP4", intake=grader.NEW_INTAKE),
        "id-new-a": _clip("id-new-a", "Z_NEW_A.MP4", intake=grader.NEW_INTAKE),
    }
    scores = {
        "id-empty": {"verdict": "EMPTY", "frames": 12, "frames_with_face": 0},
        "id-faint": {"verdict": "FAINT", "frames": 12, "frames_with_face": 2},
        "id-ruled": {"verdict": "PERSON", "frames": 12, "frames_with_face": 9},
        "id-unruled": {"verdict": "PERSON", "frames": 12, "frames_with_face": 12},
        "id-new-a": {"verdict": "EMPTY", "frames": 12, "frames_with_face": 0},
        "id-new-b": {"verdict": "PERSON", "frames": 12, "frames_with_face": 4},
    }
    files = {
        "LIBRARY_PATH": {"clips": clips},
        "PERSON_SCORES_PATH": scores,
        "USAGE_PATH": {"A_RULED": {"uses": 7, "rows": ["reel07"]}},
        "IDENTITY_POOL_PATH": {"settled_pool": ["A_RULED"], "not_operator": [],
                               "blacklist_full": [{"stem": "A_EMPTY", "kind": "FULL_BAN"}]},
    }
    for const, data in files.items():
        path = tmp_path / f"{const.lower()}.json"
        path.write_text(json.dumps(data))
        monkeypatch.setattr(grader, const, path)
    grades = tmp_path / "clip-grades" / "grades.jsonl"   # dir does not exist yet: append must create it
    monkeypatch.setattr(grader, "GRADES_PATH", grades)
    return grades


def test_items_come_in_queue_order(client, library):
    test_client, _ = client
    cookies = login(test_client)
    items = test_client.get("/api/grader/items", cookies=cookies).json()
    assert [i["stem"] for i in items] == [
        "Z_NEW_A", "Z_NEW_B",          # new first, stable by stem
        "Z_UNRULED",                   # PERSON, identity unruled
        "A_RULED",                     # PERSON, ruled
        "A_FAINT", "A_UNSCANNED",      # FAINT / not scanned
        "A_EMPTY",                     # EMPTY
        "A_NOPROXY",                   # no proxy last
    ]
    by_stem = {i["stem"]: i for i in items}
    ruled = by_stem["A_RULED"]
    assert ruled["identity"] == "settled" and ruled["uses"] == 7 and ruled["face_frames"] == "9/12"
    assert ruled["proxy_url"].startswith("/media?p=") and ruled["proxy_url"].endswith("/proxies/id-ruled.mp4")
    assert ruled["note"] == "note A_RULED.MP4" and ruled["subject"] == "person"
    assert by_stem["A_EMPTY"]["identity"] == "blacklisted"
    assert by_stem["A_UNSCANNED"]["verdict"] == "UNSCANNED" and by_stem["A_UNSCANNED"]["face_frames"] == ""
    assert by_stem["A_NOPROXY"]["verdict"] == "NO_PROXY" and by_stem["A_NOPROXY"]["proxy_url"] is None
    assert by_stem["Z_NEW_A"]["new"] and not ruled["new"]
    assert all(i["grade"] is None and i["ident"] is None for i in items)


def test_grade_post_appends_one_well_formed_line(client, library):
    test_client, _ = client
    cookies = login(test_client)
    r = test_client.post("/api/grader/grade", cookies=cookies,
                         json={"stem": "A_RULED", "clip_id": "id-ruled", "grade": "HERO"})
    assert r.status_code == 200 and r.json()["ok"]
    lines = library.read_text().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert set(line) == {"at", "stem", "clip_id", "t0", "t1", "grade", "ident", "by", "via"}
    assert line["t0"] is None and line["t1"] is None   # no window = a whole-clip grade
    assert line["stem"] == "A_RULED" and line["clip_id"] == "id-ruled" and line["grade"] == "HERO"
    assert line["ident"] is None and line["by"] == "op" and line["via"] == "deck-grader"
    assert line["at"].endswith("Z") and "T" in line["at"]
    assert r.json()["line"] == line


def test_latest_line_wins_per_field_and_null_never_erases(client, library):
    test_client, _ = client
    cookies = login(test_client)
    for body in ({"grade": "HERO"}, {"ident": "ME"}, {"grade": "NEVER"}, {"ident": "NOTME", "grade": None}):
        r = test_client.post("/api/grader/grade", cookies=cookies,
                             json={"stem": "Z_UNRULED", "clip_id": "id-unruled", **body})
        assert r.status_code == 200, r.text
    assert len(library.read_text().splitlines()) == 4
    item = next(i for i in test_client.get("/api/grader/items", cookies=cookies).json() if i["stem"] == "Z_UNRULED")
    assert item["grade"] == "NEVER" and item["ident"] == "NOTME"

    summary = test_client.get("/api/grader/summary", cookies=cookies).json()
    assert summary["total"] == 8 and summary["graded"] == 1 and summary["remaining"] == 7
    assert summary["counts"]["NEVER"] == 1 and summary["counts"]["NOTME"] == 1 and summary["counts"]["HERO"] == 0
    assert summary["first_ungraded_index"] == 0   # Z_NEW_A, still ungraded


def test_grade_many_writes_one_line_each_and_resume_skips_them(client, library):
    test_client, _ = client
    cookies = login(test_client)
    r = test_client.post("/api/grader/grade-many", cookies=cookies, json={"items": [
        {"stem": "Z_NEW_A", "clip_id": "id-new-a", "grade": "BROLL"},
        {"stem": "A_EMPTY", "clip_id": "id-empty", "grade": "BROLL"},
    ]})
    assert r.status_code == 200 and r.json()["written"] == 2
    assert [json.loads(x)["stem"] for x in library.read_text().splitlines()] == ["Z_NEW_A", "A_EMPTY"]
    summary = test_client.get("/api/grader/summary", cookies=cookies).json()
    assert summary["counts"]["BROLL"] == 2 and summary["first_ungraded_index"] == 1


def test_grader_validation_is_422_and_writes_nothing(client, library):
    test_client, _ = client
    cookies = login(test_client)
    bad = [
        {"stem": "A_RULED", "clip_id": "id-ruled", "grade": "GREAT"},
        {"stem": "A_RULED", "clip_id": "id-ruled", "ident": "MAYBE"},
        {"stem": "A_RULED", "clip_id": "id-ruled"},                       # neither field
        {"stem": "A_RULED", "clip_id": "id-ruled", "grade": None, "ident": None},
        {"stem": "A_RULED", "clip_id": "id-faint", "grade": "HERO"},      # clip_id of another stem
        {"stem": "NOPE", "clip_id": "nope", "grade": "HERO"},
        {"clip_id": "id-ruled", "grade": "HERO"},
    ]
    for body in bad:
        assert test_client.post("/api/grader/grade", cookies=cookies, json=body).status_code == 422, body
    assert test_client.post("/api/grader/grade", cookies=cookies, content=b"not json",
                            headers={"content-type": "application/json"}).status_code == 422
    assert test_client.post("/api/grader/grade-many", cookies=cookies, json={"items": []}).status_code == 422
    # one bad item and none of the batch lands
    assert test_client.post("/api/grader/grade-many", cookies=cookies, json={"items": [
        {"stem": "A_RULED", "clip_id": "id-ruled", "grade": "BROLL"}, {"stem": "A_RULED", "clip_id": "id-ruled", "grade": "x"},
    ]}).status_code == 422
    assert not library.exists()


def test_grader_needs_login_and_editors_cannot_grade(client, library):
    test_client, _ = client
    assert test_client.get("/grader", follow_redirects=False).status_code == 303
    assert test_client.get("/api/grader/items").status_code == 401
    assert test_client.post("/api/grader/grade", json={"stem": "A_RULED", "clip_id": "id-ruled", "grade": "HERO"}).status_code == 401
    ed = login(test_client, "ed", "editor-pass-123")
    r = test_client.post("/api/grader/grade", cookies=ed, json={"stem": "A_RULED", "clip_id": "id-ruled", "grade": "HERO"})
    assert r.status_code == 403
    assert not library.exists()


def test_grader_page_has_the_legend(client, library):
    test_client, _ = client
    cookies = login(test_client)
    r = test_client.get("/grader", cookies=cookies)
    assert r.status_code == 200
    for text in ("HERO", "B-ROLL", "NEVER", "This is me", "Not me", "Skip video", "Prev video", "Next section", "Prev section",
                 "Jump to NEW", "Jump to FACE", "Jump to NO-FACE", "First ungraded",
                 "Mark all remaining NO-FACE as B-ROLL", "Never cast again", "/static/js/grader.js"):
        assert text in r.text, text
    assert 'href="/grader"' in r.text   # nav link
    script = (Path(__file__).resolve().parent.parent / "web" / "static" / "js" / "grader.js").read_text()
    assert "confirm(" not in script.replace("g-confirm", "") and "localStorage.setItem(\"deck.grader.pos\"" in script


def test_real_library_and_proxy_served_through_media(client):
    from server import grader
    if not grader.LIBRARY_PATH.exists() or not REAL_PROXIES.is_dir():
        pytest.skip("footage library or the work volume is not mounted")
    test_client, _ = client
    cookies = login(test_client)
    items = test_client.get("/api/grader/items", cookies=cookies).json()
    clips = json.loads(grader.LIBRARY_PATH.read_text())["clips"]
    assert len(items) == len(clips)
    assert items[0]["new"]
    playable = next(i for i in items if i["proxy_url"])
    r = test_client.get(playable["proxy_url"], cookies=cookies, headers={"range": "bytes=0-1023"})
    assert r.status_code == 206 and r.headers["content-type"] == "video/mp4"
    # the confinement did not loosen: the same root still refuses non-reel files
    assert test_client.get("/media", params={"p": str(grader.FOOTAGE_ROOT / "person_scores.json")}, cookies=cookies).status_code == 403


def test_audit_mount_serves_through_the_proxies_symlink(client):
    if not AUDIT_PROXIES.is_symlink() or not REAL_PROXIES.is_dir():
        pytest.skip("audit dir or the work volume is not mounted")
    assert os.path.realpath(AUDIT_PROXIES) == os.path.realpath(REAL_PROXIES)
    test_client, _ = client
    assert test_client.get("/audit/index.html", follow_redirects=False).status_code == 303   # behind the login
    cookies = login(test_client)
    assert test_client.get("/audit/index.html", cookies=cookies).status_code == 200
    name = next(n for n in sorted(os.listdir(REAL_PROXIES)) if n.endswith(".mp4"))
    r = test_client.get(f"/audit/proxies/{name}", cookies=cookies, headers={"range": "bytes=0-1023"})
    assert r.status_code == 206
    assert test_client.get("/audit/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hosts", cookies=cookies).status_code == 404


# ------------------------------------------------------------ per-segment grading


def test_segments_grid():
    from server.grader import segments
    assert segments(5.0) == [[0.0, 5.0]]
    assert segments(7.0) == [[0.0, 7.0]]                      # <= 7 s is one piece
    forty = segments(40.0)
    assert len(forty) == 8 and all(abs((b - a) - 5.0) < 1e-9 for a, b in forty)
    assert forty[0][0] == 0.0 and forty[-1][1] == 40.0
    assert segments(12.0) == [[0.0, 6.0], [6.0, 12.0]]       # max(2, round(2.4)) = 2 pieces of 6 s
    assert segments(7.5) == [[0.0, 3.75], [3.75, 7.5]]       # never fewer than 2 above 7 s
    odd = segments(40.04)
    assert odd[-1][1] == 40.04 and all(a == round(a, 2) for a, _ in odd)
    assert all(odd[i][1] == odd[i + 1][0] for i in range(len(odd) - 1))   # contiguous
    long = segments(639.1385)
    assert long[-1][1] == 639.1385 and len(long) == 128
    assert segments(None) == [] and segments(0) == [] and segments("x") == []


@pytest.fixture()
def long_library(library):
    """The synthetic library plus a 40 s and a 12 s clip (both unscanned, so they sort as group 3)."""
    from server import grader
    data = json.loads(grader.LIBRARY_PATH.read_text())
    for clip_id, name, dur in (("id-long", "B_LONG.MP4", 40.0), ("id-twelve", "B_TWELVE.MP4", 12.0)):
        clip = _clip(clip_id, name)
        clip["duration"] = dur
        data["clips"][clip_id] = clip
    grader.LIBRARY_PATH.write_text(json.dumps(data))
    return library


def _post(test_client, cookies, body, many=False):
    url = "/api/grader/grade-many" if many else "/api/grader/grade"
    return test_client.post(url, cookies=cookies, json={"items": body} if many else body)


def _items(test_client, cookies):
    return {i["stem"]: i for i in test_client.get("/api/grader/items", cookies=cookies).json()}


def test_items_carry_segments_grades_whole(client, long_library):
    test_client, _ = client
    cookies = login(test_client)
    by = _items(test_client, cookies)
    assert len(by["B_LONG"]["segments"]) == 8 and by["B_LONG"]["segments"][-1] == [35.0, 40.0]
    assert by["B_LONG"]["segment_keys"][0] == "0.00-5.00"
    assert by["B_TWELVE"]["segments"] == [[0.0, 6.0], [6.0, 12.0]]
    assert by["A_RULED"]["segments"] == [[0.0, 5.0]]
    assert by["B_LONG"]["grades"] == {} and by["B_LONG"]["whole"] is None and by["B_LONG"]["ident"] is None


def test_segment_grade_line_and_off_grid_is_422(client, long_library):
    test_client, _ = client
    cookies = login(test_client)
    r = _post(test_client, cookies, {"stem": "B_LONG", "clip_id": "id-long", "grade": "HERO", "t0": 5.0, "t1": 10.0})
    assert r.status_code == 200, r.text
    line = r.json()["line"]
    assert line["t0"] == 5.0 and line["t1"] == 10.0 and line["grade"] == "HERO" and line["ident"] is None
    bad = [
        {"t0": 5.0, "t1": 9.0},            # not a boundary pair
        {"t0": 0.0, "t1": 40.0},           # the whole span is not a segment of a 40 s clip
        {"t0": 5.0, "t1": None},           # half a window
        {"t0": None, "t1": 10.0},
        {"t0": "five", "t1": 10.0},
        {"t0": True, "t1": 10.0},
        {"t0": 0.0, "t1": 6.0},            # B_TWELVE's window, not B_LONG's
    ]
    for window in bad:
        body = {"stem": "B_LONG", "clip_id": "id-long", "grade": "HERO", **window}
        assert _post(test_client, cookies, body).status_code == 422, window
    # ident is whole-clip only
    assert _post(test_client, cookies, {"stem": "B_LONG", "clip_id": "id-long", "ident": "ME",
                                        "t0": 0.0, "t1": 5.0}).status_code == 422
    assert _post(test_client, cookies, {"stem": "B_LONG", "clip_id": "id-long", "ident": "ME",
                                        "t0": None, "t1": None}).status_code == 200
    # an off-grid window inside a batch sinks the batch
    assert _post(test_client, cookies, [
        {"stem": "B_LONG", "clip_id": "id-long", "grade": "BROLL", "t0": 0.0, "t1": 5.0},
        {"stem": "B_LONG", "clip_id": "id-long", "grade": "BROLL", "t0": 1.0, "t1": 5.0},
    ], many=True).status_code == 422
    assert len(long_library.read_text().splitlines()) == 2


def test_latest_wins_per_window_and_whole_never_overwrites_a_segment(client, long_library):
    test_client, _ = client
    cookies = login(test_client)
    seg = {"stem": "B_LONG", "clip_id": "id-long", "t0": 5.0, "t1": 10.0}
    whole = {"stem": "B_LONG", "clip_id": "id-long", "t0": None, "t1": None}
    for body in ({**seg, "grade": "HERO"}, {**seg, "grade": "NEVER"},
                 {"stem": "B_LONG", "clip_id": "id-long", "t0": 0.0, "t1": 5.0, "grade": "BROLL"},
                 {**whole, "grade": "HERO"}, {**whole, "ident": "NOTME"}):
        assert _post(test_client, cookies, body).status_code == 200, body
    item = _items(test_client, cookies)["B_LONG"]
    assert item["grades"] == {"5.00-10.00": "NEVER", "0.00-5.00": "BROLL"}   # latest per window
    assert item["whole"] == "HERO" and item["ident"] == "NOTME"              # null ident line did not erase grade

    summary = test_client.get("/api/grader/summary", cookies=cookies).json()
    # every piece of B_LONG is covered (2 by their own grade, 6 by the whole-clip HERO)
    assert summary["segment_counts"]["NEVER"] == 1 and summary["segment_counts"]["BROLL"] == 1
    assert summary["segment_counts"]["HERO"] == 6
    assert summary["segments_total"] == 8 + 8 + 2 and summary["segments_graded"] == 8
    assert summary["counts"]["HERO"] == 1 and summary["counts"]["NOTME"] == 1 and summary["graded"] == 1


def test_resume_is_first_uncovered_piece(client, long_library):
    test_client, _ = client
    cookies = login(test_client)
    items = test_client.get("/api/grader/items", cookies=cookies).json()
    stems = [i["stem"] for i in items]
    long_at = stems.index("B_LONG")
    # whole-grade every clip before B_LONG, and grade B_LONG's first two pieces
    _post(test_client, cookies, [{"stem": i["stem"], "clip_id": i["clip_id"], "grade": "BROLL"}
                                 for i in items[:long_at]], many=True)
    _post(test_client, cookies, [{"stem": "B_LONG", "clip_id": "id-long", "grade": "HERO", "t0": a, "t1": b}
                                 for a, b in ([0.0, 5.0], [5.0, 10.0])], many=True)
    summary = test_client.get("/api/grader/summary", cookies=cookies).json()
    pieces_before = sum(max(1, len(i["segments"])) for i in items[:long_at])
    assert summary["resume"] == {"queue_index": pieces_before + 2, "clip_index": long_at, "segment_index": 2}
    assert summary["first_ungraded_index"] == long_at
    # a whole-clip grade on B_LONG covers its remaining pieces: resume moves past it
    _post(test_client, cookies, {"stem": "B_LONG", "clip_id": "id-long", "grade": "NEVER"})
    resumed = test_client.get("/api/grader/summary", cookies=cookies).json()["resume"]
    assert resumed["clip_index"] == long_at + 1 and resumed["segment_index"] == 0


def test_rest_of_clip_writes_one_line_per_remaining_segment(client, long_library):
    test_client, _ = client
    cookies = login(test_client)
    grid = _items(test_client, cookies)["B_LONG"]["segments"]
    rest = grid[3:]                                  # on part 4/8, "NEVER rest of clip"
    r = _post(test_client, cookies, [{"stem": "B_LONG", "clip_id": "id-long", "grade": "NEVER", "t0": a, "t1": b}
                                     for a, b in rest], many=True)
    assert r.status_code == 200 and r.json()["written"] == 5
    lines = [json.loads(x) for x in long_library.read_text().splitlines()]
    assert [[x["t0"], x["t1"]] for x in lines] == rest and all(x["grade"] == "NEVER" for x in lines)
    item = _items(test_client, cookies)["B_LONG"]
    assert sorted(item["grades"]) == sorted(item["segment_keys"][3:]) and item["whole"] is None


def test_grader_page_has_segment_controls(client, library):
    test_client, _ = client
    cookies = login(test_client)
    text = test_client.get("/grader", cookies=cookies).text
    for needle in ("HERO rest of video", "B-ROLL rest of video", "NEVER rest of video", "about 5 seconds"):
        assert needle in text, needle
    script = (Path(__file__).resolve().parent.parent / "web" / "static" / "js" / "grader.js").read_text()
    assert "timeupdate" in script and "currentTime" in script and "#t=" not in script
    assert "alert(" not in script
    # live playhead: timeline, speed toggle, hold-to-grade, next clip, untagged pill
    for needle in ('id="g-timeline"', "Next video", "Speed 1.5×", "Jump to UNTAGGED", "then next section", "Hold the key to blast"):
        assert needle in text, needle
    for needle in ("playbackRate", "pointerdown", "gradeAndNext", "goSection", "nextOpen", "e.repeat", '"ended"', "UNTAGGED", "g-next-clip"):
        assert needle in script, needle
    assert " loop " not in text.split('id="g-video"')[1].split(">")[0]   # it must reach the end to auto-advance


def test_rest_of_clip_from_the_playheads_segment_writes_only_at_and_after_it(client, long_library):
    test_client, _ = client
    cookies = login(test_client)
    item = _items(test_client, cookies)["B_LONG"]
    grid, keys = item["segments"], item["segment_keys"]
    # earlier, a tap graded part 2 HERO; the playhead now sits at 17.3 s, inside part 4 (15–20 s)
    _post(test_client, cookies, {"stem": "B_LONG", "clip_id": "id-long", "grade": "HERO", "t0": 5.0, "t1": 10.0})
    playhead = 17.3
    at = next(i for i, (a, b) in enumerate(grid) if a <= playhead < b)
    assert at == 3
    r = _post(test_client, cookies, [{"stem": "B_LONG", "clip_id": "id-long", "grade": "BROLL", "t0": a, "t1": b}
                                     for a, b in grid[at:]], many=True)
    assert r.status_code == 200 and r.json()["written"] == len(grid) - at == 5
    written = [json.loads(x) for x in long_library.read_text().splitlines()][1:]
    assert [[x["t0"], x["t1"]] for x in written] == grid[at:]
    grades = _items(test_client, cookies)["B_LONG"]["grades"]
    assert grades == {keys[1]: "HERO", **{k: "BROLL" for k in keys[at:]}}
    assert keys[0] not in grades and keys[2] not in grades     # untouched pieces stay ungraded


@pytest.fixture()
def untagged_library(library):
    """Adds clips with no tag notes: one playable, one with no proxy."""
    from server import grader
    data = json.loads(grader.LIBRARY_PATH.read_text())
    for clip_id, name, proxy in (("id-untagged", "M_UNTAGGED.MP4", True), ("id-untagged-np", "M_UNTAGGED_NP.MP4", False)):
        clip = _clip(clip_id, name, proxy=proxy)
        clip["tags"] = {"subject": "", "notes": ""}
        data["clips"][clip_id] = clip
    data["clips"]["id-untagged-none"] = {**_clip("id-untagged-none", "M_NOTAGS.MP4"), "tags": None}
    grader.LIBRARY_PATH.write_text(json.dumps(data))
    return library


def test_untagged_clips_come_right_after_new(client, untagged_library):
    test_client, _ = client
    cookies = login(test_client)
    items = test_client.get("/api/grader/items", cookies=cookies).json()
    assert [i["stem"] for i in items] == [
        "Z_NEW_A", "Z_NEW_B",
        "M_NOTAGS", "M_UNTAGGED",      # no tag notes, before the PERSON-unruled group
        "Z_UNRULED", "A_RULED", "A_FAINT", "A_UNSCANNED", "A_EMPTY",
        "A_NOPROXY", "M_UNTAGGED_NP",  # nothing to play stays last, tagged or not
    ]
    by = {i["stem"]: i for i in items}
    assert by["M_UNTAGGED"]["untagged"] and by["M_NOTAGS"]["untagged"] and by["M_UNTAGGED_NP"]["untagged"]
    assert not by["A_RULED"]["untagged"]


def test_library_is_read_per_request(client, library):
    from server import grader
    test_client, _ = client
    cookies = login(test_client)
    assert test_client.get("/api/grader/items", cookies=cookies).json()[2]["stem"] == "Z_UNRULED"
    data = json.loads(grader.LIBRARY_PATH.read_text())
    data["clips"]["id-unruled"]["tags"]["notes"] = ""          # the tagging agent rewrites the library
    grader.LIBRARY_PATH.write_text(json.dumps(data))
    item = next(i for i in test_client.get("/api/grader/items", cookies=cookies).json() if i["stem"] == "Z_UNRULED")
    assert item["untagged"]
