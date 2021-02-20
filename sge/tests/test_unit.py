import os
import time
import logging
# ~ import http.cookiejar

from typing import Dict
from urllib.parse import urlparse

import pytest
import requests
import sqlalchemy.orm
#from sqlalchemy import Session
os.environ["FLASK_ENV"] = "testing"

from sge import db
from sge import steam_games_exporter as SGE
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

TH = logging.StreamHandler()
TH.setLevel(logging.DEBUG)
TH.setFormatter(SGE.LOG_FORMAT)
LOGGER.addHandler(TH)


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
    '%d':{
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
            "content_descriptors": {"ids": [], "notes": None}
        }
    }
}"""


class DummyAPISession(SGE.APISession):
    GENERATE_GAMES_NUM = 999

    def query(self, prepared_query: requests.PreparedRequest, *args, **kwargs) -> requests.Response:
        """Return dummy json as requests.Response."""
        url = urlparse(str(prepared_query.url))
        query = dict(pair.split("=") for pair in url.query.lower().split("&")) #type: Dict[str, str]
        if url.netloc in SGE.API_STORE_URL:
            appid = int(query["appids"])
            LOGGER.debug("Querying store with appid=%s", appid)
            content = self.fetch_dummy_game_info(appid).encode()
        elif url.netloc in SGE.API_GAMES_URL:
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
        games = []
        for i in range(1, self.GENERATE_GAMES_NUM + 1):
            games.append(JSON_TEMPLATE_GAME % (i, f"App {i}"))

        return JSON_TEMPLATE_PROFILE % (len(games), ", ".join(games))


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
    monkeypatch.setattr(SGE, "APISession", DummyAPISession)
    yield


@pytest.fixture
def app_client_fixture():
    """Create new app instance"""
    app = SGE.create_app(SGE.ConfigTesting)
    with app.test_client() as client:
        yield client, app


@pytest.fixture
def db_session_fixture(monkeypatch):
    """Initialize db and prevent the app from doing so again"""
    db.init(":memory:")
    monkeypatch.setattr(SGE.db, "init", lambda url: None)
    yield db.SESSION()
    sqlalchemy.orm.close_all_sessions()


def test_routing(app_client_fixture, db_session_fixture, monkeypatch):
    """"""
    client, _ = app_client_fixture

    # monkeypatch the login function to stop OID from making any requests
    # we're assuming a correct OID config and that call to login will result in a redirect to steam
    login_return = "Unit test: login function triggered"
    monkeypatch.setattr(SGE, "login", lambda: login_return)

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


def test_extended_export(api_session_fixture, app_client_fixture, db_session_fixture):
    client, _ = app_client_fixture
    db_session = db_session_fixture

    # remove a variable from this equation; fetcher thread will be tested separately
    SGE.GAME_INFO_FETCHER = None

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
    assert resp.status_code == 202
    assert not resp.headers.get("Location")
    assert "Items added to the queue, return later" in resp.get_data().decode()
    assert "job" in [cookie.name for cookie in client.cookie_jar]
    job_cookie = [cookie for cookie in client.cookie_jar if cookie.name == "job"][0]
    assert "session" not in [cookie.name for cookie in client.cookie_jar]
    assert db_session.query(db.Queue).count() == DummyAPISession.GENERATE_GAMES_NUM
    assert db_session.query(db.Request).count() == 1
    assert db_session.query(db.Request).first().job_uuid == job_cookie.value

    generate_fake_game_info(999, db_session)
    assert db_session.query(db.GameInfo).count() == 999
    db_session.query(db.Queue).delete() #clear the queue manually
    db_session.commit()
    assert db_session.query(db.Queue).count() == 0

    ### GET: job cookie present from last request, game info available for export
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 200
    assert not resp.headers.get("Location")
    assert not client.cookie_jar
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert db_session.query(db.Queue).count() == 0
    assert db_session.query(db.Request).count() == 0

    ### POST: game info already available, do not queue anything, export in one step
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890
    resp = client.post("/tools/steam-games-exporter/export?export",
                       data={"format": "xlsx", "include-gameinfo": True})
    assert resp.status_code == 200
    assert not resp.headers.get("Location")
    assert not client.cookie_jar
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert db_session.query(db.Queue).count() == 0
    assert db_session.query(db.Request).count() == 0

    # ~ fake_request = db.Request(dict(), "xlsx")
    # ~ job_cookie = http.cookiejar.Cookie(
        # ~ version=1, name="job", value=fake_request.job_uuid, port=80, port_specified=False,
        # ~ domain="", domain_specified=False, domain_initial_dot=False,
        # ~ path="/tools/steam-games-exporter/", path_specified=True, secure=False,
        # ~ expires=int(time.time() + 60*60*24), discard=False, comment=None, comment_url=None,
        # ~ rest={'HttpOnly': None, 'SameSite': 'Lax'}
    # ~ )
