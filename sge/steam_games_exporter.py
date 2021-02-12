""""""
import os
import csv
import json
import time
import logging
import tempfile
from typing import Any, IO, Optional, Union
from types import ModuleType
import flask
import flask_openid
import werkzeug

import requests

import pyexcel_xls as pyxls
import pyexcel_xlsx as pyxlsx
import pyexcel_ods3 as pyods

from sge import config, db

__VERSION__ = "0.1"

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

# This is set automatically by emperor (see vassal.ini), has to be set manually in dev env
# key.ini mentioned in vassal.ini is a one liner setting environment variable containing dev key
# not included in the repo for obvious reasons
STEAM_DEV_KEY = os.environ.get("STEAM_DEV_KEY")
SQLITE_DB_PATH = os.environ.get("FLASK_DB_PATH", default="")
# if path is not set, use in-memory sqlite db ("sqlite://")

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.ERROR)

FLASK_ENV = os.environ.get("FLASK_ENV")
if FLASK_ENV == "development":
    config.STATIC_URL_PATH = "/static"
    #del config.APPLICATION_ROOT
    del config.SERVER_NAME
    config.SESSION_COOKIE_SECURE = False
    config.DEBUG = True
    config.TESTING = True

FLASK_DEBUG_TOOLBAR = None

OID = flask_openid.OpenID()
PWD = os.path.realpath(__file__).rsplit("/", maxsplit=1)[0]
APP_BP = flask.Blueprint("sge", __name__, url_prefix="/tools/steam-games-exporter")


def create_app(app_config: ModuleType) -> flask.Flask:
    app = flask.Flask(
        __name__,
        static_url_path=config.STATIC_URL_PATH, #type: ignore
        static_folder=os.path.join(PWD, "../static"),
        template_folder=os.path.join(PWD, "../templates"),
    )
    app.config.from_object(app_config)
    app.register_blueprint(APP_BP)
    OID.init_app(app)

    # if available, and in debug mode, import and enable flask debug toolbar
    if app.debug:
        try:
            import flask_debugtoolbar
            debug_toolbar = flask_debugtoolbar.DebugToolbarExtension(app)
        except ImportError:
            pass

    return app


@APP_BP.before_app_first_request
def db_init() -> None:
    """Create engine, bind it to sessionmaker, and create tables"""
    db.init(f"sqlite://{SQLITE_DB_PATH}")


@APP_BP.before_request
def load_job() -> None:
    job = flask.request.cookies.get("job")
    if job:
        flask.g.job = db.SESSION().query(db.Request).filter(
            db.Request.job_uuid == job
        ).first()
    else:
        flask.g.job = None


@APP_BP.teardown_request
def close_db_session(exc: Any) -> None:
    """Close the scoped session during teardown"""
    db.SESSION().close()


@APP_BP.route("/")
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

@APP_BP.route("/login", methods=['GET', 'POST'])
@OID.loginhandler
def login() -> Union[werkzeug.wrappers.Response, str]:
    """Redirect to steam for authentication"""
    cookies = bool(flask.session)
    if "steamid" in flask.session:
        return flask.redirect(flask.url_for("sge.games_export_config"))
    if flask.request.method == 'POST' and cookies:
        return OID.try_login("https://steamcommunity.com/openid")

    # this should only be displayed in case of errors
    # lack of cookies is an error
    return flask.render_template("login.html", cookies=cookies, error=OID.fetch_error())


@OID.after_login
def create_session(resp: flask_openid.OpenIDResponse) -> werkzeug.wrappers.Response:
    """called automatically instead of login() after successful authentication"""
    flask.session["steamid"] = resp.identity_url.rsplit("/", maxsplit=1)[-1]
    return flask.redirect(flask.url_for("sge.games_export_config"))


@APP_BP.route("/export", methods=("GET", "POST"))
def games_export_config() -> Union[werkzeug.wrappers.Response, str]:
    """Display and handle export config"""
    if flask.g.job:
        return finalize_extended_export(flask.g.job)

    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("sge.index"))

    if flask.request.method == "POST":
        if flask.request.form["format"] not in ["ods", "xls", "xlsx", "csv"]:
            flask.abort(400)

        steamid = flask.session["steamid"]
        # we don't need steamid anymore, so throw it out
        flask.session.clear()

        if flask.request.form["include-gameinfo"]:
            exported = export_games_extended(steamid, flask.request.form["format"])
            # did not return file, game info still needs to be fetched
            if isinstance(exported, db.Request):
                db_session = db.SESSION()
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
                         ) -> Union[werkzeug.wrappers.Response, db.Request, str]:
    """Initiate export, create all necessary db rows, return control to finalize_
    Returns:
        str - error page
        Request - newly added db.Request row
        flask response - successfully exported and began sending the file
    """
    with APISession() as s:
        profile_json = s.query_profile(steamid)

    if not profile_json:
        return flask.render_template(
            "login.html",
            error="Cannot export data: this account does not own any games"
        )

    games_json = profile_json["games"]
    new_request = db.Request(games_json, file_format)

    games_ids = {row["appid"] for row in profile_json["games"]}
    db_session = db.SESSION()
    available_ids = db_session.query(db.GameInfo.appid).filter(
        db.GameInfo.appid.in_(games_ids)
    ).all()
    missing_ids = games_ids.difference(available_ids)
    if missing_ids:
        db_session.add(new_request)
        queue = []
        for appid in missing_ids:
            queue.append(db.Queue(new_request.job_uuid, appid))

        db_session.bulk_save_objects(queue)
        db_session.commit()
        db_session.close()
        return new_request
    #else: all necessary info already present in db, no need to persist the new request

    return finalize_extended_export(new_request)


def finalize_extended_export(request_job: db.Request) -> Union[werkzeug.wrappers.Response, str]:
    """Combine profile json with stored game info.
    Returns:
        str             -> error page / notification about ongoing export
        flask response  -> successfully exported and began sending the file
    """
    db_session = db.SESSION()
    missing_ids = db_session.query(db.Queue).filter(
        db.Queue.job_uuid == request_job.job_uuid
    ).count()

    if missing_ids:
        return flask.render_template(
            "login.html",
            error="Your request is still being processed. " \
                 f"Still fetching game info for {missing_ids} games"
        )

    games_json = json.loads(request_job.games_json)
    requested_appids = [row["appid"] for row in games_json]
    _games_info = db_session.request(db.GameInfo).filter(
        db.GameInfo.appid in requested_appids
    ).all()
    del requested_appids
    #associate each db.GameInfo object with its appid in a dict for easier and quicker lookup
    games_info = {row.appid:row for row in _games_info}
    # header
    combined_games_data = [
        ["app_id", "name", "developers", "publishers", "on_linux", "on_mac", "on_windows",
         "categories", "genres", "release_date", "playtime_forever", "playtime_windows_forever",
         "playtime_mac_forever", "playtime_linux_forever"]
    ]
    for json_row in games_json:
        info = games_info[json_row["appid"]]
        combined_games_data.append([
            json_row["appid"], info.name, info.developers, info.publishers, info.on_linux,
            info.on_mac, info.on_windows, info.categories, info.genres, info.release_date,
            json_row["playtime_forever"], json_row["playtime_windows_forever"],
            json_row["playtime_mac_forever"], json_row["playtime_linux_forever"]
        ])

    file_format = request_job.export_format
    try:
        #csv requires file in write mode, rest in binary write
        tmp: Union[IO[str], IO[bytes]]
        if file_format == "ods":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyods.save_data(tmp, {"GAMES":combined_games_data})
        elif file_format == "xls":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyxls.save_data(tmp, {"GAMES":combined_games_data})
        elif file_format == "xlsx":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyxlsx.save_data(tmp, {"GAMES":combined_games_data})
        elif file_format == "csv":
            tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
            csv_writer = csv.writer(tmp)
            for row in combined_games_data:
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


def export_games_simple(steamid: int, file_format: str
                       ) -> Union[werkzeug.wrappers.Response, str]:
    """Simple export without game info"""
    with APISession() as s:
        profile_json = s.query_profile(steamid)

    if not profile_json:
        return flask.render_template(
            "login.html",
            error="Cannot export data: this account does not own any games"
        )

    games_json = profile_json["games"]
    games = [list(PROFILE_RELEVANT_FIELDS)]
    games[0][0] = "store_url"
    # iterate over the games, extract only relevant fields, replace appid with store link
    for raw_row in games_json:
        game_row = [raw_row[field] for field in PROFILE_RELEVANT_FIELDS]
        game_row[0] = "https://store.steampowered.com/app/{}".format(game_row[0])
        games.append(game_row)

    try:
        #csv requires file in write mode, rest in binary write
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


    #type literals available in python 3.8+, we're targeting 3.6+
    def __exit__(self, *args: Any, **kwargs: Any) -> False:
        self.requests_session.close()
        return False


    def query_store(self, appid: int) -> Optional[dict]:
        _query = requests.Request("GET", API_STORE_URL.format(appid=appid))
        prepared_query = self.requests_session.prepare_request(_query)
        store_json = self.query(prepared_query, max_retries=2).json()["response"]
        if not store_json:
            return None
        return store_json


    def query_profile(self, steamid: int) -> Optional[dict]:
        _query = requests.Request(
            "GET", API_GAMES_URL.format(key=STEAM_DEV_KEY, steamid=steamid))
        prepared_query = self.requests_session.prepare_request(_query)

        games_json = self.query(prepared_query, max_retries=0).json()["response"]
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
