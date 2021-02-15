import os
import time
import logging
import datetime

import http.cookiejar
from urllib.parse import urlparse

import pytest
import sqlalchemy.orm
#from sqlalchemy import Session
os.environ["FLASK_ENV"] = "development"
os.environ["FLASK_DB_PATH"] = ""

from sge import db
from sge import steam_games_exporter as SGE
LOGGER = logging.getLogger()
LOGGER.setLevel(logging.DEBUG)

@pytest.fixture
def app_client_fixture():
    app = SGE.create_app(SGE.config)
    app.config.update(SECRET_KEY="devkey")
    app.testing = True
    app.debug = True
    with app.test_client() as client:
        yield client, app


@pytest.fixture
def db_session_fixture(monkeypatch):
    monkeypatch.setattr(SGE.db.SESSION, "remove", lambda: None)
    monkeypatch.setattr(SGE, "db_init", lambda: None)
    db.init("sqlite:///:memory:")
    yield db.SESSION()
    sqlalchemy.orm.close_all_sessions()


def generate_fake_game_info(maxid: int, db_session):
    gameinfo = []
    for i in range(1, maxid+1):
        gameinfo.append(
            db.GameInfo(
                appid=i, name=f"dummy app {i}", developers=f"dev 1,\ndev 2 for app {i}",
                publishers=f"publisher 1,\npublisher 2 for app {i}", on_linux=True, on_mac=True,
                on_windows=False, categories=f"category 1,\ncategory 2 for app {i}",
                genres=f"genre 1,\ngenre 2 for app {i}",
                release_date=str(datetime.datetime.fromtimestamp(0)), timestamp=int(time.time())
            )
        )
    db_session.bulk_save_objects(gameinfo)
    db_session.commit()


def test_routing(app_client_fixture):
    """"""
    client, app = app_client_fixture
    # POST to index not allowed
    resp = client.post("/tools/steam-games-exporter/")
    assert resp.status_code == 405
    assert not client.cookie_jar

    # GET to index loads correctly, cookies are set
    resp = client.get("/tools/steam-games-exporter/")
    assert resp.status_code == 200
    assert client.cookie_jar

    #FIXME: this always makes a request to https://steamcommunity.com/openid/login
    # which makes the tests take much longer
    # POST with cookies set leads to a redirect to steam login page
    resp = client.post("/tools/steam-games-exporter/login")
    assert resp.status_code == 302
    assert client.cookie_jar
    assert resp.headers.get("Location").startswith("https://steamcommunity.com/openid/login")

    # POST: user is not redirected (so an error message can be shown) when cookies are missing
    client.cookie_jar.clear()
    resp = client.post("/tools/steam-games-exporter/login")
    assert resp.status_code == 200
    assert not client.cookie_jar
    assert not resp.headers.get("Location")

    # POST: user is redirected back to index if cookies are missing
    client.cookie_jar.clear()
    resp = client.post("/tools/steam-games-exporter/export")
    assert resp.status_code == 302
    assert not client.cookie_jar
    assert urlparse(resp.headers.get("Location")).path == "/tools/steam-games-exporter/"

    # GET: user is redirected back to index if cookies are missing
    client.cookie_jar.clear()
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 302
    assert not client.cookie_jar
    assert urlparse(resp.headers.get("Location")).path == "/tools/steam-games-exporter/"

    #TODO: test /tools/steam-games-exporter/export with cookies set
    #TODO: test /tools/steam-games-exporter/export with an invalid steamid
    #TODO: test /tools/steam-games-exporter/export with an invalid export format


def test_extended_export(app_client_fixture, db_session_fixture, monkeypatch):
    client, app = app_client_fixture
    db_session = db_session_fixture

    # Generate fake profile data, patch that to query_profile
    fake_user_data = [
        {"appid":x, "name":f"{x}", "playtime_forever":x, "playtime_windows_forever":x,
         "playtime_mac_forever":x, "playtime_linux_forever":x} for x in range(1, 1000)
    ]
    monkeypatch.setattr(
        SGE.APISession, "query_profile",
        lambda self, steamid: {"game_count": 1000, "games": fake_user_data}
    )
    # set dummy steamid to prevent redirecting to index
    with client.session_transaction() as app_session:
        app_session["steamid"] = 1234567890

    ### POST: valid request, missing game info -> user shown info about pending export
    resp = client.post(
        "/tools/steam-games-exporter/export?export", data={"format": "csv", "include-gameinfo": True}
    )
    assert resp.status_code == 202
    assert not resp.headers.get("Location")
    assert "Items added to the queue, return later" in resp.get_data().decode()


    generate_fake_game_info(1000, db_session)
    db_session.query(db.Queue).delete() #clear the queue manually

    ### GET: job cookie present from last request, game info available for export
    resp = client.get("/tools/steam-games-exporter/export")
    assert resp.status_code == 200
    assert not resp.headers.get("Location")
    assert "attachment" in resp.headers.get("Content-Disposition")


    ### POST: game info already available, do not queue anything, export in one step
    resp = client.post(
        "/tools/steam-games-exporter/export?export", data={"format": "xlsx", "include-gameinfo": True}
    )
    assert resp.status_code == 200
    assert not resp.headers.get("Location")
    assert "attachment" in resp.headers.get("Content-Disposition")

    # ~ cookie = http.cookiejar.Cookie(
        # ~ version=1, name="job", value=fake_user.job_uuid, port=80, port_specified=False, domain="",
        # ~ domain_specified=False, domain_initial_dot=False, path="/tools/steam-games-exporter",
        # ~ path_specified=True, secure=False, expires=int(time.time() + 60*60*24), discard=False,
        # ~ comment=None, comment_url=None, rest=None
    # ~ )
