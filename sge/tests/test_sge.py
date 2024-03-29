"""
Note about the fetcher thread: due to how pytest runs these tests, the
fetcher threads will quit only after the main pytest thread will, which
happens AFTER test environments have been cleaned up. This means that
logging facilities, and their underlying I/O objects will be closed and
will raise errors when the fetcher thread tries to signal its shutdown.
This issue does not occur when running through uWSGI or flask dev
server.
"""
import os
import json
import time
import logging
import tempfile

from typing import Dict
from urllib.parse import urlparse

import pytest
import requests
import sqlalchemy as sqla
import sqlalchemy.orm

import sge
from sge import db, views

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

JSON_TEMPLATE_PROFILE = "{\"response\": {\"game_count\": %d, \"games\": [%s]}}"
JSON_TEMPLATE_GAME = """{
    "appid": %d,
    "name": "%s",
    "img_icon_url": "adc83b19e793491b1c6ea0fd8b46cd9f32e592fc",
    "img_logo_url": "adc83b19e793491b1c6ea0fd8b46cd9f32e592fc",
    "has_community_visible_stats": true,
    "playtime_forever": 0,
    "playtime_windows_forever": 0,
    "playtime_mac_forever": 0,
    "playtime_linux_forever": 0
}"""
JSON_TEMPLATE_GAMEINFO = """{
    "%d":{
        "success": true,
        "data":{
            "type": "game",
            "name": "%s",
            "steam_appid": %d,
            "required_age": 0,
            "is_free": false,
            "controller_support": "full",
            "supported_languages": "English<strong>*</strong>, French, Spanish - Spain, Korean<br><strong>*</strong>languages with full audio support",
            "developers": ["developer 1", "developer2"],
            "publishers": ["publisher 1", "publisher 2"],
            "platforms": {"windows": true, "mac": true, "linux": true},
            "categories": [{"id": 2,"description": "Single-player"},
                           {"id": 22, "description": "Steam Achievements"},
                           {"id": 28, "description": "Full controller support"},
                           {"id": 29, "description": "Steam Trading Cards"},
                           {"id": 23, "description": "Steam Cloud"},
                           {"id": 43, "description": "Remote Play on TV"}],
            "genres": [{"id": "23", "description": "Indie"},
                       {"id": "3", "description": "RPG"}],
            "release_date": {"coming_soon": false, "date": "2 Oct, 2020"},
            "content_descriptors": {"ids": [], "notes": null}
        }
    }
}"""

def test_requires_env(*expected_vars):
    return pytest.mark.skipif(
        not all(x in os.environ for x in expected_vars),
        reason=f"Following env vars need to be defined for this test: {expected_vars}"
    )


class DummyAPISession(sge.APISession):
    GENERATE_GAMES_NUM = 2000
    GENERATE_GAMES_START_ID = 1
    USERS = {}

    def query(self, prepared_query: requests.PreparedRequest, *args, **kwargs) -> requests.Response:
        """Return dummy json as requests.Response."""
        url = urlparse(str(prepared_query.url))
        query = dict(pair.split("=") for pair in url.query.lower().split("&")) #type: Dict[str, str]
        if url.netloc in sge.APISession.API_STORE_URL:
            appid = int(query["appids"])
            LOGGER.debug("Querying store with appid=%s", appid)
            content = self.fetch_dummy_game_info(appid).encode()
        elif url.netloc in sge.APISession.API_GAMES_URL:
            steamid = int(query["steamid"])
            LOGGER.debug("Querying profile with steamid=%s", steamid)
            content = self.fetch_dummy_steam_profile(steamid).encode()
        else:
            raise ValueError(f"UNKNOWN ENDPOINT: {prepared_query.url}")

        # this is not the proper way of creating a new response
        # but it will work well enough for our purpose
        response = requests.Response()
        response.encoding = "utf-8"
        response._content = content #pylint: disable=protected-access
        response._content_consumed = True #pylint: disable=protected-access
        response.status_code = 200
        response.reason = "OK"
        return response


    def fetch_dummy_steam_profile(self, steamid: int) -> str:
        """Generate cls.GENERATE_GAMES_NUM fake game entries.
        Return those entries formatted as steam API json response.
        """
        if steamid in self.USERS:
            return self.USERS[steamid]

        games = []

        range_start = self.GENERATE_GAMES_START_ID
        assert range_start, "Positive integers only"
        range_end = self.GENERATE_GAMES_START_ID + self.GENERATE_GAMES_NUM
        for i in range(range_start, range_end):
            games.append(JSON_TEMPLATE_GAME % (i, f"App {i}"))

        self.USERS[steamid] = JSON_TEMPLATE_PROFILE % (len(games), ", ".join(games))
        return self.USERS[steamid]


    def fetch_dummy_game_info(self, appid: int) -> str:
        """"""
        return JSON_TEMPLATE_GAMEINFO % (appid, f"App {appid}", appid)



def generate_fake_game_info(maxid: int, db_session):
    LOGGER.debug("Generating %s fake game entries", maxid)
    gameinfo = []
    for i in range(1, maxid+1):
        gameinfo.append(db.GameInfo(appid=i, timestamp=int(time.time()), unavailable=True))

    db_session.bulk_save_objects(gameinfo)
    db_session.commit()


@pytest.fixture(name="test_api_session")
def api_session_fixture(monkeypatch):
    """Prevent app from making any requests"""
    monkeypatch.setattr(sge, "APISession", DummyAPISession)
    yield DummyAPISession


@pytest.fixture(name="test_app_client")
def app_client_fixture():
    """Create new app instance"""
    try: #pylint: disable=consider-using-with
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        app = sge.create_app(sge.ConfigDevelopment, steam_key="", db_path=tmp.name)
        with app.test_client() as client:
            yield client, app
    finally:
        sqlalchemy.orm.close_all_sessions()
        tmp.close()
        os.unlink(tmp.name)


@pytest.fixture
def db_session_fixture():
    """Create new scoped session independent from the app"""
    try: #pylint: disable=consider-using-with
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        scoped_session = db.init(tmp.name)
        yield scoped_session
    finally:
        sqlalchemy.orm.close_all_sessions()
        tmp.close()
        os.unlink(tmp.name)


def test_gameinfo_init():
    """"""
    appid = 1
    parsed_json = json.loads(JSON_TEMPLATE_GAMEINFO % (appid, f"App {appid}", appid))
    parsed_json = parsed_json[str(appid)]["data"]
    start_time = time.time()
    gameinfo_obj = db.GameInfo.from_json(appid, parsed_json)

    ### All values are derived as expected
    # values derived manually from JSON_TEMPLATE_GAMEINFO
    derived_values = {
        "appid": appid,
        "name": f"App {appid}",
        "type": "game",
        "developers": "developer 1,\ndeveloper2",
        "publishers": "publisher 1,\npublisher 2",
        "is_free": False,
        "on_linux": True,
        "on_mac": True,
        "on_windows": True,
        "supported_languages": \
            "English*, French, Spanish - Spain, Korean\n*languages with full audio support",
        "controller_support": "full",
        "age_gate": 0,
        "categories": "Single-player,\nSteam Achievements,\nFull controller support,\nSteam Trading Cards,\nSteam Cloud,\nRemote Play on TV",
        "genres": "Indie,\nRPG",
        "release_date": "2020/10/02",
        "unavailable": False,
    }

    for column in db.GameInfo.__table__.columns:
        if column.key == "timestamp":
            assert gameinfo_obj.timestamp >= int(start_time)
            continue

        assert column.key in derived_values, f"'{column.key}' not found in derived values"
        assert getattr(gameinfo_obj, column.key) == derived_values[column.key]

    ### Test the different date formats, list[(source data, expected result)]
    date_formats = [
        ("2 Oct, 2020", "2020/10/02"),
        ("26 MAR 2018", "2018/03/26"),
        ("7. Aug. 2020", "2020/08/07"),
        ("May 22, 2017", "2017/05/22"),
        ("Nov 2014", "2014/11/01"),
        #("20 берез. 2007", "2007/03/20"), # appid 4500
        # strptime works based on current locale - this last one will be harder to fix
    ]
    for src_date, expected_date in date_formats:
        gameinfo_date = db.GameInfo.from_json(
            appid, {"appid": appid, "release_date": {"date":src_date}}
        )
        assert gameinfo_date.release_date == expected_date, \
            f"Expected [{src_date}]->[{expected_date}]"

    ### Make sure minimal responses are processed correctly
    minimal_json = {"appid": appid}
    gameinfo_minimal = db.GameInfo.from_json(appid, minimal_json)
    assert gameinfo_minimal


def test_routing(test_app_client, monkeypatch):
    """"""
    client, app = test_app_client

    # monkeypatch the login function to stop OID from making any requests
    # we're assuming a correct OID config and that call to login will result in a redirect to steam
    login_return = "Unit test: login function triggered"
    monkeypatch.setattr(views, "login", lambda: login_return)

    # POST / not allowed
    resp = client.post("/tools/steam-games-exporter/")
    assert resp.status_code == 405
    assert not client.cookie_jar

    # GET / loads correctly, cookies are set
    resp = client.get("/tools/steam-games-exporter/")
    assert resp.status_code == 200
    assert client.cookie_jar

    # POST /login without cookies -> error page, no redirect
    client.cookie_jar.clear()
    resp = client.post("/tools/steam-games-exporter/login")
    assert resp.status_code == 404
    assert not client.cookie_jar
    assert not resp.headers.get("Location")

    # POST /login with cookies set -> redirect to steam login page (trigger monkeypatched lambda)
    with client.session_transaction() as app_session:
        app_session["c"] = ""
    resp = client.post("/tools/steam-games-exporter/login")
    assert resp.status_code == 200 # this will be a 302 normally
    assert client.cookie_jar #cookies have not been cleared
    assert resp.get_data().decode() == login_return

    # POST /login with steamid session cookie set -> redirect to export config
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890
    resp = client.post("/tools/steam-games-exporter/login")
    assert resp.status_code == 302
    assert client.cookie_jar
    assert urlparse(resp.headers.get("Location")).path == "/tools/steam-games-exporter/export"

    # POST /export without cookies -> redirect to index
    client.cookie_jar.clear()
    resp = client.post("/tools/steam-games-exporter/export")
    assert resp.status_code == 302
    assert not client.cookie_jar
    assert urlparse(resp.headers.get("Location")).path == "/tools/steam-games-exporter/"

    # GET: /export without cookies -> redirect to index
    client.cookie_jar.clear()
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 302
    assert not client.cookie_jar
    assert urlparse(resp.headers.get("Location")).path == "/tools/steam-games-exporter/"

    # GET: /export with steamid session cookie set -> display export config
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 200
    assert client.cookie_jar
    assert not resp.headers.get("Location")

    # termminate fetcher manually to prevent writing to closed I/O objects in pytext context
    # see this module's docstring for details
    fetcher = app.config["SGE_FETCHER_THREAD"]
    fetcher._terminate.set() #pylint: disable=protected-access
    fetcher.notify()
    fetcher.join()


def test_extended_export(test_api_session, test_app_client):
    _ = test_api_session
    client, app = test_app_client
    db_session = app.config["SGE_SCOPED_SESSION"]()
    page_refresh = app.config["SGE_PAGE_REFRESH"]
    # disable fetcher thread
    # we're manually adding all the entries and don't want fetcher to interfere
    gameinfo_fetcher = app.config["SGE_FETCHER_THREAD"]
    gameinfo_fetcher._terminate.set() #pylint: disable=protected-access

    ### POST: invalid export format
    with client.session_transaction() as app_session:
        # set dummy steamid to prevent redirecting to index
        app_session["steamid"] = 1234567890
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "not a format", "include-gameinfo": True})
    assert resp.status_code == 400

    ### POST: valid request, missing game info -> user shown info about pending export
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "csv", "include-gameinfo": True})
    assert not resp.headers.get("Location") #user is not redirected
    resp_msg = views.MSG_QUEUE_CREATED.format(
        missing_ids=DummyAPISession.GENERATE_GAMES_NUM,
        delay=DummyAPISession.GENERATE_GAMES_NUM*1.5 // 60 + 1,
        refresh=page_refresh)
    assert resp_msg in resp.get_data().decode()
    assert "job" in [cookie.name for cookie in client.cookie_jar]
    job_cookie = [cookie for cookie in client.cookie_jar if cookie.name == "job"][0]
    assert "session" not in [cookie.name for cookie in client.cookie_jar]
    assert db_session.execute(
        sqla.select(sqla.func.count()).\
        select_from(db.Queue)).scalar() == DummyAPISession.GENERATE_GAMES_NUM
    assert db_session.execute(
        sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 1
    assert db_session.execute(
        sqla.select(db.Request.job_uuid)).scalars().first() == job_cookie.value
    assert resp.status_code == 202

    generate_fake_game_info(DummyAPISession.GENERATE_GAMES_NUM, db_session)
    assert db_session.execute(
        sqla.select(sqla.func.count()).\
        select_from(db.GameInfo)).scalar() == DummyAPISession.GENERATE_GAMES_NUM

    db_session.execute(sqla.delete(db.Queue)) #clear the queue manually
    db_session.commit()
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Queue)).scalar() == 0

    ### GET: job cookie present from last request, game info available for export
    resp = client.get("/tools/steam-games-exporter/export")
    assert not resp.headers.get("Location")
    assert not client.cookie_jar
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Queue)).scalar() == 0
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 0
    assert resp.status_code == 200

    ### POST: game info already available, do not queue anything, export in one step
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "xlsx", "include-gameinfo": True})
    assert not resp.headers.get("Location")
    assert not client.cookie_jar
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Queue)).scalar() == 0
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 0
    assert resp.status_code == 200


def test_gameinfo_fetcher(test_api_session, test_app_client):
    _ = test_api_session
    client, app = test_app_client
    db_session = app.config["SGE_SCOPED_SESSION"]()
    gameinfo_fetcher = app.config["SGE_FETCHER_THREAD"]

    ### Simulate client sending multiple duplicate requests after losing job cookies
    # also send a get after each post, to confirm the queue is not skipped
    client.cookie_jar.clear()
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "xlsx", "include-gameinfo": True})
    assert resp.status_code == 202
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 202
    assert gameinfo_fetcher.is_alive()

    client.cookie_jar.clear()
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "xls", "include-gameinfo": True})
    assert resp.status_code == 202
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 202

    client.cookie_jar.clear()
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "csv", "include-gameinfo": True})
    assert resp.status_code == 202
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 202
    assert gameinfo_fetcher.is_alive()
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 3

    # wait until the thread terminates
    gameinfo_fetcher._terminate.set() #pylint: disable=protected-access
    gameinfo_fetcher.notify()
    gameinfo_fetcher.join()

    queue_length = db_session.execute(sqla.select(sqla.func.count()).select_from(db.Queue)).scalar()
    gameinfo_length = db_session.execute(
        sqla.select(sqla.func.count()).select_from(db.GameInfo)).scalar()
    # how much will be added to gameinfo depends on how much time these tests take
    # but they should take long enough to have at least a couple in there
    # on the other hand, because we're adding ids as fetcher is processing the queue,
    # we're going to wind up with duplicate ids, (no more than 20, size of fetcher's batch)
    assert queue_length + gameinfo_length >= DummyAPISession.GENERATE_GAMES_NUM
    assert queue_length + gameinfo_length <= DummyAPISession.GENERATE_GAMES_NUM + 20


def test_cleanup(test_api_session, test_app_client, monkeypatch):
    _ = test_api_session
    client, app = test_app_client
    db_session = app.config["SGE_SCOPED_SESSION"]()

    #prevent fetcher thread from interfering
    app.config["SGE_FETCHER_THREAD"]._terminate.set() #pylint: disable=protected-access

    ### cleaner runs without issues in empty db
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.GameInfo)).scalar() == 0
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Queue)).scalar() == 0
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 0
    sge.cleanup(-1, app)

    monkeypatch.setattr(DummyAPISession, "GENERATE_GAMES_NUM", 1)
    # POST 4 times, each time with different steamid to create 4 requests
    api_requests = []
    for i in range(1, 5):
        with client.session_transaction() as app_session:
            app_session["steamid"] = i
        resp = client.post("/tools/steam-games-exporter/export?export",
                           data={"format": "xlsx", "include-gameinfo": True})
        assert resp.status_code == 202

        job_uuid = [cookie for cookie in client.cookie_jar if cookie.name == "job"][0].value
        api_requests.append(job_uuid)
        client.cookie_jar.clear()

    client.cookie_jar.clear()
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 4
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request).where(
        db.Request.timestamp <= time.time() - sge.COOKIE_MAX_AGE + 60 * 60
        # being extra careful for no reason:
        # ensue none of the cookies are going to expire in the next hour
    )).scalar() == 0

    # set timestamp of three request to a moment in the past
    db_session.execute(
        sqla.update(db.Request).\
        where(db.Request.job_uuid.in_([uuid for uuid in api_requests[:3]])).\
        values(timestamp=int(time.time()) - sge.COOKIE_MAX_AGE).\
        execution_options(synchronize_session=False)
    )
    db_session.commit()

    sge.cleanup(-1, app)
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 1
    assert db_session.execute(sqla.select(db.Request.job_uuid)).scalars().first() == api_requests[3]
    ### clients whose requests got deleted can make further requests without issues (redirect to /)
    for uuid in api_requests[:3]:
        client.cookie_jar.clear()
        client.set_cookie(
            key="job", value=uuid, path="/tools/steam-games-exporter/",
            server_name="localhost")
        resp = client.get("/tools/steam-games-exporter/export")
        assert not [cookie for cookie in client.cookie_jar if cookie.name == "job"]
        assert resp.status_code == 302

    ### client whose request was not cleared can make further requests without issues
    # their cookies are not cleared, they are not redirected
    client.set_cookie(
        key="job", value=api_requests[3], path="/tools/steam-games-exporter/",
        server_name="localhost")
    resp = client.get("/tools/steam-games-exporter/export")
    assert [cookie for cookie in client.cookie_jar if cookie.name == "job"][0]. \
        value == api_requests[3]
    assert resp.status_code == 202

    # expire the remaining requests
    db_session.execute(
        sqla.update(db.Request).\
        values(timestamp=int(time.time()) - sge.COOKIE_MAX_AGE).\
        execution_options(synchronize_session=False)
    )
    db_session.commit()

    sge.cleanup(-1, app)
    assert db_session.execute(sqla.select(sqla.func.count()).select_from(db.Request)).scalar() == 0


@test_requires_env("SGE_STEAM_DEV_KEY", "SGE_REAL_IDS_LIST")
def test_real_ids(test_app_client, monkeypatch):
    """Test whether processing for real IDs in env var can be completed.
    Note that fetcher might not terminate if any of the IDs cannot be fetched
    Note that graceful thread termination is not implemented for fetcher tests, So
    this test will generate some errors after the pytest process finishes.
    Some IDs found in the wild:

    No supported languages field:
    12230,12250,21110,21120,29720,94500,94510,94520,94530,212910
    """
    client, app = test_app_client
    app.config["SGE_STEAM_DEV_KEY"] = os.environ["SGE_STEAM_DEV_KEY"]

    db_session = app.config["SGE_SCOPED_SESSION"]()
    real_ids = [int(x.strip()) for x in os.environ["SGE_REAL_IDS_LIST"].split(",")]
    LOGGER.debug("Fetching")
    real_data = []
    for game_id in real_ids:
        real_data.append(
            json.loads(JSON_TEMPLATE_GAME % (game_id, f"name for game id {game_id}"))
        )
    real_data = {"games":real_data}
    monkeypatch.setattr(sge.APISession, "query_profile", lambda *args, **kwargs: real_data)

    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "csv", "include-gameinfo": True})
    assert resp.status_code == 202 #request accepted
    assert "job" in [cookie.name for cookie in client.cookie_jar]
    assert "session" not in [cookie.name for cookie in client.cookie_jar]

    timeout_after = len(real_ids) * 10
    start_time = time.time()
    while db_session.execute(sqla.select(sqla.func.count()).select_from(db.Queue)).scalar():
        LOGGER.info("Waiting for fetcher to finish loading info...")
        if time.time() >= (start_time + timeout_after):
            LOGGER.error("TIMEOUT FOR REAL ID TEST REACHED")
            break

        time.sleep(1)

    assert db_session.execute(
        sqla.select(
            sqla.func.count()).select_from(db.GameInfo)
        ).scalar() == len(real_ids)
