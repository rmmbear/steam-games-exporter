import os
import time
import logging
import tempfile

from typing import Dict
from urllib.parse import urlparse

import pytest
import requests
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
            "publishers": ["publisher 1"],
            "platforms": {"windows": true, "mac": true, "linux": true},
            "categories": [{"id": 2,"description": "Single-player"},
                           {"id": 22, "description": "Steam Achievements"},
                           {"id": 28, "description": "Full controller support"},
                           {"id": 29, "description": "Steam Trading Cards"},
                           {"id": 23, "description": "Steam Cloud"},
                           {"id": 43, "description": "Remote Play on TV"}],
            "genres": [{"id": "23", "description": "Indie"},
                       {"id": "3", "description": "RPG"}],
            "release_date": {"coming_soon": false, "date": "00 Month, Year"},
            "content_descriptors": {"ids": [], "notes": null}
        }
    }
}"""


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
            raise ValueError("UNKNOWN ENDPOINT: %s", prepared_query.url)

        # this is not the proper way of creating a new response
        # but it will work well enough for our purpose
        response = requests.Response()
        response.encoding = "utf-8"
        response._content = content
        response._content_consumed = True
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


@pytest.fixture
def api_session_fixture(monkeypatch):
    """Prevent app from making any requests"""
    monkeypatch.setattr(sge, "APISession", DummyAPISession)
    yield


@pytest.fixture
def app_client_fixture():
    """Create new app instance"""
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        app = sge.create_app(sge.ConfigDevelopment, steam_key="", db_path=tmp.name)
        with app.test_client() as client:
            yield client, app

        sqlalchemy.orm.close_all_sessions()
    finally:
        tmp.close()
        os.unlink(tmp.name)


@pytest.fixture
def db_session_fixture(monkeypatch):
    """Initialize db and prevent the app from doing so again"""
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        db_session = db.init(tmp.name)
        monkeypatch.setattr(views.db, "init", lambda url: db_session)
        yield db_session()
        sqlalchemy.orm.close_all_sessions()
    finally:
        tmp.close()
        os.unlink(tmp.name)


def test_routing(app_client_fixture, monkeypatch):
    """"""
    client, app = app_client_fixture

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


def test_extended_export(api_session_fixture, app_client_fixture):
    client, app = app_client_fixture
    db_session = app.config["SGE_SCOPED_SESSION"]()

    # disable fetcher thread by overriding its start method
    # we're manually adding all the entries and don't want fetcher to interfere
    app.config["SGE_FETCHER_THREAD"].start = lambda: None

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
    assert not resp.headers.get("Location")
    resp_msg = views.MSG_QUEUE_CREATED.format(
        missing_ids=DummyAPISession.GENERATE_GAMES_NUM,
        delay=DummyAPISession.GENERATE_GAMES_NUM*1.5 // 60 + 1)
    assert resp_msg in resp.get_data().decode()
    assert "job" in [cookie.name for cookie in client.cookie_jar]
    job_cookie = [cookie for cookie in client.cookie_jar if cookie.name == "job"][0]
    assert "session" not in [cookie.name for cookie in client.cookie_jar]
    assert db_session.query(db.Queue).count() == DummyAPISession.GENERATE_GAMES_NUM
    assert db_session.query(db.Request).count() == 1
    assert db_session.query(db.Request).first().job_uuid == job_cookie.value
    assert resp.status_code == 202

    generate_fake_game_info(DummyAPISession.GENERATE_GAMES_NUM, db_session)
    assert db_session.query(db.GameInfo).count() == DummyAPISession.GENERATE_GAMES_NUM
    db_session.query(db.Queue).delete() #clear the queue manually
    db_session.commit()
    assert db_session.query(db.Queue).count() == 0

    ### GET: job cookie present from last request, game info available for export
    resp = client.get("/tools/steam-games-exporter/export")
    assert not resp.headers.get("Location")
    assert not client.cookie_jar
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert db_session.query(db.Queue).count() == 0
    assert db_session.query(db.Request).count() == 0
    assert resp.status_code == 200

    ### POST: game info already available, do not queue anything, export in one step
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "xlsx", "include-gameinfo": True})
    assert not resp.headers.get("Location")
    assert not client.cookie_jar
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert db_session.query(db.Queue).count() == 0
    assert db_session.query(db.Request).count() == 0
    assert resp.status_code == 200


def test_gameinfo_fetcher(api_session_fixture, app_client_fixture, monkeypatch):
    client, app = app_client_fixture
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
                       data={"format": "ods", "include-gameinfo": True})
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
    assert db_session.query(db.Request).count() == 4
    # wait until the thread terminates
    gameinfo_fetcher._terminate.set()
    gameinfo_fetcher.notify()
    gameinfo_fetcher.join()

    queue_length = db_session.query(db.Queue).count()
    gameinfo_length = db_session.query(db.GameInfo).count()
    # how much will be added to gameinfo depends on how much these tests take
    # but they should take long enough to have at least a couple in there
    # on the other hand, because we're adding ids as fetcher is processing the queue,
    # we're going to wind up with duplicate ids, (no more than 20, size of fetcher's batch)

    assert queue_length + gameinfo_length >= DummyAPISession.GENERATE_GAMES_NUM
    assert queue_length + gameinfo_length <= DummyAPISession.GENERATE_GAMES_NUM + 20


def test_cleanup(api_session_fixture, app_client_fixture, monkeypatch):
    _ = api_session_fixture
    client, app = app_client_fixture
    db_session = app.config["SGE_SCOPED_SESSION"]()

    #prevent fetcher thread from interfering
    app.config["SGE_FETCHER_THREAD"]._terminate.set()

    ### cleaner runs without issues in empty db
    assert db_session.query(db.GameInfo).count() == 0
    assert db_session.query(db.Queue).count() == 0
    assert db_session.query(db.Request).count() == 0
    with app.app_context():
        sge.cleanup(-1)

    monkeypatch.setattr(DummyAPISession, "GENERATE_GAMES_NUM", 1)

    # POST 4 times, each time with different steamid to create 4 requests
    requests = []
    for i in range(1, 5):
        with client.session_transaction() as app_session:
            app_session["steamid"] = i
        resp = client.post("/tools/steam-games-exporter/export?export",
                           data={"format": "xlsx", "include-gameinfo": True})
        assert resp.status_code == 202

        job_uuid = [cookie for cookie in client.cookie_jar if cookie.name == "job"][0].value
        requests.append(job_uuid)
        client.cookie_jar.clear()

    client.cookie_jar.clear()
    assert db_session.query(db.Request).count() == 4
    assert db_session.query(db.Request).filter(
        db.Request.timestamp <= time.time() - sge.COOKIE_MAX_AGE + 60 * 60
        # being extra careful for no reason:
        # ensue none of the cookies are going to expire in the next hour
    ).count() == 0

    # set timestamp of three request to a moment in the past
    db_session.query(db.Request).filter(
        db.Request.job_uuid.in_([uuid for uuid in requests[:3]])
        ).update(
            {db.Request.timestamp: int(time.time()) - sge.COOKIE_MAX_AGE},
            synchronize_session=False
        )
    db_session.commit()

    with app.app_context():
        sge.cleanup(-1)
    assert db_session.query(db.Request).count() == 1
    assert db_session.query(db.Request).first().job_uuid == requests[3]
    # clients whose requests got deleted can make further requests without issues (redirect to /)
    for uuid in requests[:3]:
        client.cookie_jar.clear()
        client.set_cookie(
            key="job", value=uuid, path="/tools/steam-games-exporter/",
            server_name="localhost")
        resp = client.get("/tools/steam-games-exporter/export")
        assert not [cookie for cookie in client.cookie_jar if cookie.name == "job"]
        assert resp.status_code == 302

    # client whose request was not cleared can make further requests without issues
    # their cookies are not cleared, they are not redirected
    client.set_cookie(
        key="job", value=requests[3], path="/tools/steam-games-exporter/",
        server_name="localhost")
    resp = client.get("/tools/steam-games-exporter/export")
    assert [cookie for cookie in client.cookie_jar if cookie.name == "job"][0]. \
        value == requests[3]
    assert resp.status_code == 202

    # expire the remaining request
    db_session.query(db.Request).update(
        {db.Request.timestamp: int(time.time()) - sge.COOKIE_MAX_AGE},
        synchronize_session=False
    )
    db_session.commit()

    with app.app_context():
        sge.cleanup(-1)
    assert db_session.query(db.Request).count() == 0
