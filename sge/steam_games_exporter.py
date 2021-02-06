import os
import csv
import sys
import json
import time
import uuid
import logging
import tempfile

from typing import Any, IO, Optional, Union

import flask
import flask_openid
import werkzeug

import requests

import pyexcel_xls as pyxls
import pyexcel_xlsx as pyxlsx
import pyexcel_ods3 as pyods

import sqlalchemy
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta

#TODO: probably should structure this as a package
# but this requires less thinking
sys.path.insert(0, os.path.realpath(__file__).rsplit("/", maxsplit=1)[0])
import config

__VERSION__ = "0.1"

# This is set automatically by emperor (see vassal.ini), has to be set manually in dev env
# key.ini mentioned in vassal.ini is a one liner setting environment variable containing dev key
# not included in the repo for obvious reasons
STEAM_DEV_KEY = os.environ.get("STEAM_DEV_KEY")
SQLITE_DB_PATH = os.environ.get("FLASK_DB_PATH", default="")
# if path is not set, use in-memory sqlite db ("sqlite://")

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

COOKIE_MAX_AGE = 86400 # 1 day

# https://partner.steamgames.com/doc/webapi_overview/responses
KNOWN_API_RESPONSES = [200, 400, 401, 403, 404, 405, 429, 500, 503]
# https://wiki.teamfortress.com/wiki/User:RJackson/StorefrontAPI#appdetails
API_STORE_URL = "https://store.steampowered.com/api/appdetails/?appids={appid}"
# https://developer.valvesoftware.com/wiki/Steam_Web_API
API_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/" \
                "?key={key}&steamid={steamid}&format=json&include_appinfo=1"
API_GAMES_NOINFO_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/" \
                       "?key={key}&steamid={steamid}&format=json"
PROFILE_RELEVANT_FIELDS = (
    "appid", "name", "playtime_forever", "playtime_windows_forever",
    "playtime_mac_forever", "playtime_linux_forever"
)

ORM_BASE: DeclarativeMeta = declarative_base()

#TODO: naming collision with all the networking/server stuff, find a better name
class Request(ORM_BASE):
    __tablename__ = "requests_queue"
    job_uuid = sqlalchemy.Column(sqlalchemy.String, primary_key=True)
    timestamp = sqlalchemy.Column(sqlalchemy.Integer)
    games_json = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    export_format = sqlalchemy.Column(sqlalchemy.String)
    generated_file = sqlalchemy.Column(sqlalchemy.String, nullable=True)

    def __init__(self, games_json: dict, export_format: str):
        if export_format not in ["ods", "xls", "xlsx", "csv"]:
            raise ValueError(f"Export format not recognized {export_format}")

        self.job_uuid = uuid.uuid4()
        self.timestamp = int(time.time())
        self.games_json = json.dumps(games_json)
        self.export_format = export_format
        self.generated_file = None


class Queue(ORM_BASE):
    __tablename__ = "games_queue"
    job_uuid = sqlalchemy.Column(sqlalchemy.String, primary_key=True)
    appid = sqlalchemy.Column(sqlalchemy.Integer)
    job_type = sqlalchemy.Column(sqlalchemy.String) #api_store / scrape_store


class GameInfo(ORM_BASE):
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


DB_SESSIONMAKER = sessionmaker(autocommit=False, autoflush=False)
DB_SESSION = scoped_session(DB_SESSIONMAKER)

@APP.before_first_request
def db_init() -> None:
    """Create engine, bind it to sessionmaker, and create tables"""
    engine = sqlalchemy.create_engine(f"sqlite://{SQLITE_DB_PATH}")
    DB_SESSIONMAKER.configure(bind=engine)
    ORM_BASE.metadata.create_all(bind=engine)


@APP.before_request
def load_job() -> None:
    job = flask.request.cookies.get("job")
    if job:
        flask.g.job = DB_SESSION().query(Request).filter(
            Request.job_uuid == job
        ).first()
    else:
        flask.g.job = None


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
def login() -> Union[werkzeug.wrappers.Response, str]:
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
def create_session(resp: flask_openid.OpenIDResponse) -> werkzeug.wrappers.Response:
    """called automatically instead of login() after successful authentication"""
    flask.session["steamid"] = resp.identity_url.rsplit("/", maxsplit=1)[-1]
    return flask.redirect(flask.url_for("games_export_config"))


@APP.route("/tools/steam-games-exporter/export", methods=("GET", "POST"))
def games_export_config() -> Union[werkzeug.wrappers.Response, str]:
    """Display and handle export config"""
    if flask.g.job:
        return finalize_extended_export(flask.g.job)

    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("index"))

    if flask.request.method == "POST":
        if flask.request.form["format"] not in ["ods", "xls", "xlsx", "csv"]:
            flask.abort(400)

        steamid = flask.session["steamid"]
        # we don't need steamid anymore, so throw it out
        flask.session.clear()

        if flask.request.form["include-gameinfo"]:
            exported = export_games_extended(steamid, flask.request.form["format"])
            # did not return file, game info still needs to be fetched
            if isinstance(exported, Request):
                db_session = DB_SESSION()
                db_session.add(exported)
                db_session.commit()
                resp = flask.make_response(exported)
                resp.set_cookie(
                    "job", value=exported.job_uuid, max_age=COOKIE_MAX_AGE,
                    path="/tools/steam-games-exporter/",
                    secure=False, httponly=True, samesite="Lax"
                )
                return resp

            return exported

        return export_games_simple(steamid, flask.request.form["format"])

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

def export_games_extended(steamid: int, file_format: str
                         ) -> Union[werkzeug.wrappers.Response, Request, str]:
    """Initiate export, create all necessary db rows, return control to finalize_
    Returns:
        str - error page
        Request - request object to be committed by caller
        flask response - successfully exported and began sending the file
    """
    raise NotImplementedError()
    #return finalize_extended_export()


def finalize_extended_export(request_job: Request)-> Union[werkzeug.wrappers.Response, str]:
    """Combine profile json with stored game info.
    Returns:
        str             -> error page / notification about ongoing export
        flask response  -> successfully exported and began sending the file
    """
    raise NotImplementedError()


def export_games_simple(steamid: int, file_format: str
                       ) -> Union[werkzeug.wrappers.Response, str]:
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
        tmp: Union[IO[str], IO[bytes]]
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
        return flask.send_file(
            tmp.name, as_attachment=True, attachment_filename=f"games.{file_format}")
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


    def query_store(self, appid: int) -> Optional[dict]:
        raise NotImplementedError()
        query = requests.Request("GET", API_STORE_URL.format(appid=appid))
        query = self.requests_session.prepare_request(query)


    def query_profile(self, steamid: int) -> Optional[dict]:
        _query = requests.Request("GET", API_GAMES_URL.format(key=STEAM_DEV_KEY, steamid=steamid), 0)
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