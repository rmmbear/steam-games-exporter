import os
import sys
import logging

from urllib.parse import urlparse

import pytest

os.environ["FLASK_ENV"] = "development"
ONE_DIR_UP = os.path.join(os.path.realpath(__file__).rsplit("/", maxsplit=1)[0], "..")
sys.path.append(ONE_DIR_UP)
import steam_games_exporter as SGE
SGE.APP.config.update(SECRET_KEY="devkey")

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

@pytest.fixture
def app_client_fixture():
    SGE.APP.testing = True
    SGE.APP.debug = True
    with SGE.APP.test_client() as client:
        yield client


def test_routing(app_client_fixture):
    """"""
    client = app_client_fixture
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

    # ensure user is not redirected (so an error message can be shown) when cookies are missing
    client.cookie_jar.clear()
    resp = client.post("/tools/steam-games-exporter/login")
    assert resp.status_code == 200
    assert not client.cookie_jar
    assert not resp.headers.get("Location")

    # ensure user is redirected back to index if cookies are missing
    client.cookie_jar.clear()
    resp = client.post("/tools/steam-games-exporter/export")
    assert resp.status_code == 302
    assert not client.cookie_jar
    assert urlparse(resp.headers.get("Location")).path == "/tools/steam-games-exporter/"

    #TODO: test /tools/steam-games-exporter/export with cookies set
    #TODO: test /tools/steam-games-exporter/export with an invalid steamid
    #TODO: test /tools/steam-games-exporter/export with an invalid export format
