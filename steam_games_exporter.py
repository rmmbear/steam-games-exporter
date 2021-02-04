import os
import csv
import sys
import time
import logging
import tempfile

from typing import Any, Optional

import flask
import flask_openid

import requests

import pyexcel_xls as pyxls
import pyexcel_xlsx as pyxlsx
import pyexcel_ods3 as pyods

import sqlalchemy
from sqlalchemy.ext.declarative import declarative_base

#TODO: probably should structure this as a package
# but this requires less thinking
sys.path.insert(0, os.path.realpath(__file__).rsplit("/", maxsplit=1)[0])
import config

__VERSION__ = "0.1"

# This is set automatically by emperor (see vassal.ini), has to be set manually in dev env
# key.ini mentioned in vassal.ini is a one liner setting environment variable containing dev key
# not included in the repo for obvious reasons
STEAM_DEV_KEY = os.environ.get("STEAM_DEV_KEY")

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.ERROR)
DEBUG_TOOLBAR = None

FLASK_ENV = os.environ.get("FLASK_ENV")
if FLASK_ENV == "development":
    config.STATIC_URL_PATH = "/static"
    del config.APPLICATION_ROOT
    del config.SERVER_NAME
    config.SESSION_COOKIE_SECURE = False
    config.DEBUG = True
    config.TESTING = True

APP = flask.Flask(__name__, static_url_path=config.STATIC_URL_PATH)
APP.config.from_object(config)
OID = flask_openid.OpenID(APP)
# if available, and in debug mode, import and enable flask debug toolbar
if APP.debug:
    try:
        import flask_debugtoolbar
        DEBUG_TOOLBAR = flask_debugtoolbar.DebugToolbarExtension(APP)
        del flask_debugtoolbar
    except ImportError:
        pass


# https://partner.steamgames.com/doc/webapi_overview/responses
KNOWN_API_RESPONSES = [200, 400, 401, 403, 404, 405, 429, 500, 503]
# https://partner.steamgames.com/doc/webapi_overview/responses
STORE_API = "https://store.steampowered.com/api/appdetails/?appids={appid}"
# https://developer.valvesoftware.com/wiki/Steam_Web_API
# https://wiki.teamfortress.com/wiki/User:RJackson/StorefrontAPI#appdetails
GAMES_API = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?" \
            "key={key}&steamid={steamid}&format=json&include_appinfo=1"

PROFILE_RELEVANT_FIELDS = (
    "appid", "name", "playtime_forever", "playtime_windows_forever",
    "playtime_mac_forever", "playtime_linux_forever"
)

DBOrmBase = declarative_base()

class Request(DBOrmBase):
    __tablename__ = "requests_queue"
    job_uuid = sqlalchemy.Column(sqlalchemy.String, primary_key=True)
    timestamp = sqlalchemy.Column(sqlalchemy.Integer)
    games_json = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    generated_file = sqlalchemy.Column(sqlalchemy.String, nullable=True)

class Queue(DBOrmBase):
    __tablename__ = "games_queue"
    job_uuid = sqlalchemy.Column(sqlalchemy.String, primary_key=True)
    appid = sqlalchemy.Column(sqlalchemy.Integer)
    job_type = sqlalchemy.Column(sqlalchemy.String) #api_store / scrape_store

class GameInfo(DBOrmBase):
    __tablename__ = "games_info"
    appid = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    name = sqlalchemy.Column(sqlalchemy.String)
    developers = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    publishers = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    on_linux = sqlalchemy.Column(sqlalchemy.Boolean)
    on_mac = sqlalchemy.Column(sqlalchemy.Boolean)
    on_windows = sqlalchemy.Column(sqlalchemy.Boolean)
    categories = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    genres = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    release_date = sqlalchemy.Column(sqlalchemy.String)


@APP.route('/tools/steam-games-exporter/')
def index() -> str:
    """landing page"""
    #cookie check
    if "c" not in flask.session:
        flask.session["c"] = None

    return flask.render_template("index.html")

#FIXME: flask-openid encounters issues related to missing fields in steam's response
# KeyError: ('http://specs.openid.net/auth/2.0', 'assoc_type')
# this does not seem to cause any issues down the line
# this exact same issue: https://github.com/mitsuhiko/flask-openid/issues/48
#FIXME: openid discovery performed in login regardless of context
# i.e. a request to https://steamcommunity.com/openid/login is made each time login() is called
# with these two issues in mind, I think it would make the most sense to ditch flask-openid
# and interact with python-openid directly, as much as I hate the concept of
# "code is documentation" that they're using

@APP.route('/tools/steam-games-exporter/login', methods=['GET', 'POST'])
@OID.loginhandler
def login():
    """Redirect to steam for authentication"""
    cookies = bool(flask.session)
    if "steamid" in flask.session:
        return flask.redirect(flask.url_for("games_export_config"))
    if flask.request.method == 'POST' and cookies:
        return OID.try_login("https://steamcommunity.com/openid")

    # this should only be displayed in case of errors
    # lack of cookies is an error
    return flask.render_template("login.html", cookies=cookies, error=OID.fetch_error())


@OID.after_login
def create_session(resp):
    """called automatically instead of login() after successful authentication"""
    flask.session["steamid"] = resp.identity_url.rsplit("/", maxsplit=1)[-1]
    return flask.redirect(flask.url_for("games_export_config"))


@APP.route("/tools/steam-games-exporter/export", methods=("GET", "POST"))
def games_export_config():
    """Display and handle export config"""
    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("index"))

    if flask.request.method == "POST":
        if flask.request.form["format"] not in ["ods", "xls", "xlsx", "csv"]:
            flask.abort(400)

        steamid = flask.session["steamid"]
        # we don't need steamid anymore, so throw it out
        del flask.session["steamid"]
        return games_export_simple(steamid, flask.request.form["format"])

    return flask.render_template("export-config.html")

#XXX: sometimes games might be unavailable in our region
# in that case, querying the store api will result in following response:
#    {"<appid>": {"success": False}}
# afaik there is no workaround for this (without using a proxy of some kind)
# these titles must be queried from regions in which they are available

#Notes on formats:
# based on artificial tests with random and static data
# export time, lowest -> highest
# csv -> xls -> xlsx -> ods
# file size, smallest -> biggest
# ods -> xlsx -> xls -> csv
#
# based on this, my conclusion is that saving generated files
# is not necessary - majority of users will most likely have
# between 100 - 1000 items in their steam library, export times
# for this range are reasonable even for ods. Exception could be
# made fot larger collections, but only for xlsx and ods
# xls and csv should not be retained because of their short export times
# csv doubly so because of its big file sizes

def games_export_extended(steamid: int, file_format: str):
    raise NotImplementedError()


def games_export_simple(steamid: int, file_format: str):
    """Simple export without game info"""
    api_session = APISession()
    with api_session as s:
        games_json = s.query_profile(steamid)

    if not games_json:
        return flask.render_template(
            "login.html",
            error="Cannot export data: this account does not own any games"
        )

    games_json = games_json["games"]
    games = [list(PROFILE_RELEVANT_FIELDS)]
    games[0][0] = "store_url"
    # iterate over the games, extract only relevant fields, replace appid with store link
    for raw_row in games_json:
        game_row = [raw_row[field] for field in PROFILE_RELEVANT_FIELDS]
        game_row[0] = "https://store.steampowered.com/app/{}".format(game_row[0])
        games.append(game_row)

    del games_json

    try:
        if file_format == "ods":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyods.save_data(tmp, {"GAMES":games})
        elif file_format == "xls":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyxls.save_data(tmp, {"GAMES":games})
        elif file_format == "xlsx":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyxlsx.save_data(tmp, {"GAMES":games})
        elif file_format == "csv":
            tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
            csv_writer = csv.writer(tmp)
            for row in games:
                csv_writer.writerow(row)
        else:
            # this should be caught earlier in the flow, but _just in case_
            raise ValueError(f"Unknown file format: {file_format}")

        tmp.close()
        return flask.send_file(tmp.name, as_attachment=True,
                               attachment_filename=f"games.{file_format}")
    finally:
        tmp.close()
        os.unlink(tmp.name)


class APISession():
    """Simple context manager taking advantage of connection pooling"""
    user_agent = f"SteamGamesFetcher/{__VERSION__} (+https://github.com/rmmbear)"

    def __init__(self) -> None:
        self.requests_session = requests.Session()
        self.requests_session.headers["User-Agent"] = self.user_agent


    def __enter__(self) -> "APISession":
        return self


    def __exit__(self, *args: Any, **kwargs: Any) -> False:
        self.requests_session.close()
        return False


    def query_store(self, appid: int) -> Optional[str]:
        raise NotImplementedError()
        query = requests.Request("GET", STORE_API.format(appid=appid))
        query = self.requests_session.prepare_request(query)


    def query_profile(self, steamid: int) -> Optional[str]:
        _query = requests.Request("GET", GAMES_API.format(key=STEAM_DEV_KEY, steamid=steamid), 0)
        _query = self.requests_session.prepare_request(_query)

        games_json = self.query(_query).json()["response"]
        if not games_json:
            return None
        return games_json


    def query(self, prepared_query: requests.PreparedRequest, max_retries: int = 2
             ) -> requests.Response:
        """Error handling helper"""
        exp_delay = [2**x for x in range(max_retries)]
        retry_count = 0
        while True:
            try:
                response = self.requests_session.send(prepared_query, stream=True, timeout=15)
                response.raise_for_status()
                return response
            except requests.HTTPError:
                LOGGER.info("Received HTTP error code %s", response.status_code)
                if response.status_code not in KNOWN_API_RESPONSES:
                    LOGGER.error("Unexpected API response (%s), contents:\n%s",
                                 response.status_code, response.content)
                    raise
                if response.status_code in range(400, 500) or \
                   retry_count >= max_retries:
                    raise
            except requests.Timeout:
                LOGGER.warning("Connection timed out")
                if retry_count >= max_retries:
                    raise
            except requests.ConnectionError:
                LOGGER.error("Could not establish a new connection")
                raise
            except Exception as err:
                LOGGER.error("Unexpected request exception")
                LOGGER.error("request url = %s", prepared_query.url)
                LOGGER.error("request method = %s", prepared_query.method)
                LOGGER.error("request headers = %s", prepared_query.headers)
                raise err

            retry_count += 1
            delay = exp_delay[retry_count-1]
            LOGGER.info("Retrying (%s/%s) in %ss", retry_count, max_retries, delay)
            time.sleep(delay)
