""""""
import os
import csv
import json
import time
import logging
import tempfile
import threading

from types import ModuleType
from typing import Any, IO, Optional, Union
from urllib.parse import urlparse

import flask
import flask_openid
import werkzeug

import requests

import pyexcel_xls as pyxls
import pyexcel_xlsx as pyxlsx
import pyexcel_ods3 as pyods

from sge import config, db

__VERSION__ = "0.1"

COOKIE_MAX_AGE = 172800 # 2 days
# https://partner.steamgames.com/doc/webapi_overview/responses
KNOWN_API_RESPONSES = [200, 400, 401, 403, 404, 405, 429, 500, 503]
# https://wiki.teamfortress.com/wiki/User:RJackson/StorefrontAPI#appdetails
API_STORE_URL = "https://store.steampowered.com/api/appdetails/?appids={appid}"
# https://developer.valvesoftware.com/wiki/Steam_Web_API
API_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/" \
                "?format=json&include_appinfo=1&include_played_free_games=1" \
                "&key={key}&steamid={steamid}"
# despite what the resources on this endpoint say, free games are included in the response
# even with include_played_free_games=0
# so instead let's pretend this is what we wanted

PROFILE_RELEVANT_FIELDS = [
    "appid", "name", "playtime_forever", "playtime_windows_forever",
    "playtime_mac_forever", "playtime_linux_forever"
]
GAMEINFO_RELEVANT_FIELDS = [
    c.key for c in db.GameInfo.__table__.columns if c.key not in ["name", "appid",
                                                                  "timestamp", "unavailable"]
]


# This is set automatically by emperor (see vassal.ini), has to be set manually in dev env
# key.ini mentioned in vassal.ini is a one liner which sets the dev key as env var
# not included in the repo for obvious reasons
STEAM_DEV_KEY = os.environ.get("STEAM_DEV_KEY")
SQLITE_DB_PATH = os.environ.get("FLASK_DB_PATH", default="")
# if path is not set, use in-memory sqlite db ("sqlite:///")

LOG_FORMAT = logging.Formatter("[%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.WARNING)


FLASK_ENV = os.environ.get("FLASK_ENV")
if FLASK_ENV != "development" and not SQLITE_DB_PATH:
    raise RuntimeError("Running in prod without db path specified")
if FLASK_ENV == "development":
    config.STATIC_URL_PATH = "/static"
    #del config.APPLICATION_ROOT
    del config.SERVER_NAME
    config.SESSION_COOKIE_SECURE = False
    config.DEBUG = True
    config.TESTING = True
    LOGGER.setLevel(logging.DEBUG)
    TH = logging.StreamHandler()
    TH.setLevel(logging.DEBUG)
    TH.setFormatter(LOG_FORMAT)
    LOGGER.addHandler(TH)

FLASK_DEBUG_TOOLBAR = None

OID = flask_openid.OpenID()
PWD = os.path.realpath(__file__).rsplit("/", maxsplit=1)[0]
APP_BP = flask.Blueprint("sge", __name__, url_prefix="/tools/steam-games-exporter")
GAME_INFO_FETCHER = None

#FIXME: remove old jobs from db.Requests (older than COOKIE_MAX_AGE)

class GameInfoFetcher(threading.Thread):
    """Background thread for processing db.Queue.
    """
    def __init__(self) -> None:
        super().__init__(target=None, name="store_info_fetcher", daemon=False)
        self.condition = threading.Condition()
        self._terminate = threading.Event()
        self.rate_limited = False

        def shutdown_notify(fetcher_thread: "GameInfoFetcher") -> None:
            # wait until main thread stops execution
            threading.main_thread().join()
            # trigger termination event and wake our thread
            LOGGER.info("Terminating fetcher thread")
            fetcher_thread._terminate.set()
            fetcher_thread.notify(force=True)

        self.shutdown_notifier = threading.Thread(
            target=shutdown_notify, args=(self,), daemon=False
        )


    def _wait(self, timeout: Optional[int] = None, rate_limited: bool = False) -> None:
        LOGGER.debug("Fetcher thread waiting. timeout=%s, rate_limited=%s", timeout, rate_limited)
        if not timeout and rate_limited:
            raise ValueError("Timeout must be specified when rate_limit is True")
        try:
            self.condition.acquire()
            self.rate_limited = rate_limited
            self.condition.wait(timeout)
        finally:
            self.condition.release()
            self.rate_limited = False
            LOGGER.debug("Fetcher thread waking up")


    def notify(self, force: bool = False) -> None:
        """Wake the thread to resume queue processing
        """
        if not self.rate_limited or (force and self.rate_limited):
            try:
                self.condition.acquire()
                self.condition.notify_all()
            finally:
                self.condition.release()
        #else: we're rate limited, do not wake up, will wake up automatically later


    def run(self) -> None:
        """Continuously fetch game info until main thread stops."""
        LOGGER.info("Fetcher thread started")
        db_session = db.SESSION()
        # 20 items = 30 seconds (at minimum) at 1.5s delay between requests
        queue_query = db_session.query(db.Queue).order_by(db.Queue.timestamp).limit(20)
        LOGGER.info("Starting shutdown notifier")
        self.shutdown_notifier.start()
        with APISession() as api_session:
            while True:
                if self._terminate.is_set():
                    db.SESSION.remove()
                    LOGGER.info("Terminating fetcher thread (idle)")
                    return

                queue_batch = queue_query.all()
                if not queue_batch:
                    LOGGER.debug("Nothing in the queue, waiting")
                    self._wait()
                    continue

                LOGGER.debug("Processing batch")
                for queue_item in queue_batch:
                    if self._terminate.is_set():
                        db.SESSION.remove()
                        LOGGER.info("Terminating fetcher thread (processing)")
                        return

                    app_already_known = db_session.query(db.GameInfo).filter(
                        db.GameInfo.appid == int(queue_item.appid)
                    ).exists()
                    if not db_session.query(app_already_known).scalar():
                        try:
                            game_info = api_session.query_store(queue_item.appid)
                            LOGGER.info("Adding app to db: %s", game_info)
                            db_session.add(game_info)
                        except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as exc:
                            LOGGER.warning("Network error: %s", exc)
                            #FIXME: detect when we're getting rate limited
                            # if isinstance(exc, requests.HTTPError) and exc.status_code == 429:
                            #   self.wait(timeout=rate_limited_until, rate_limited=True)
                            # else:
                            self._wait(10, rate_limited=True)
                            continue
                        except Exception as exc:
                            LOGGER.exception("Ignoring exception:")
                            db_session.rollback()
                            # move item to the bottom of the stack
                            queue_item.timestamp = int(time.time())
                            db_session.commit()
                            self._wait(10, rate_limited=True)
                            continue
                    else:
                        LOGGER.warning("encountered queue item for an already known app (%s)",
                                       queue_item.appid)

                    db_session.delete(queue_item)
                    db_session.commit()


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
            global FLASK_DEBUG_TOOLBAR
            FLASK_DEBUG_TOOLBAR = flask_debugtoolbar.DebugToolbarExtension(app)
        except ImportError:
            pass

    db.init(SQLITE_DB_PATH)
    global GAME_INFO_FETCHER
    GAME_INFO_FETCHER = GameInfoFetcher()

    return app


@APP_BP.before_app_first_request
def db_init() -> None:
    """Create engine, bind it to sessionmaker, and create tables"""
    LOGGER.debug("Received first request")
    LOGGER.info("Initializing db")
    db.init(SQLITE_DB_PATH)
    if GAME_INFO_FETCHER:
        GAME_INFO_FETCHER.start()


@APP_BP.before_request
def load_job() -> None:
    LOGGER.debug("Received request")
    job_uuid = flask.request.cookies.get("job")
    if job_uuid:
        job_db_row = db.SESSION().query(db.Request).filter(
            db.Request.job_uuid == job_uuid
        ).first()
        if job_db_row:
            flask.g.job_db_row = job_db_row
        #FIXME: clear invalid job cookies

@APP_BP.after_request
def notify_fetcher(resp: Any) -> None:
    if "queue_modified" in flask.g:
        if GAME_INFO_FETCHER:
            LOGGER.info("Notifying fetcher thread of modified queue ")
            GAME_INFO_FETCHER.notify()

    return resp


@APP_BP.teardown_request
def close_db_session(exc: Any) -> None:
    """Close the scoped session during teardown"""
    LOGGER.info("Request teardown")
    db.SESSION.remove()


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

@APP_BP.route("/login", methods=['GET', 'POST'])
def login_router() -> Union[werkzeug.wrappers.Response, str]:
    LOGGER.debug("In login router")
    openid_complete = flask.request.args.get("openid_complete")
    if openid_complete:
        return login()

    if "job_db_row" in flask.g:
        return finalize_extended_export(flask.g.job_db_row)
    if "steamid" in flask.session:
        return flask.redirect(flask.url_for("sge.games_export_config"))

    cookies = bool(flask.session)
    if flask.request.method == 'POST' and cookies:
        return login()

    # this should only be displayed in case of errors
    # lack of cookies is an error
    return flask.render_template("login.html", cookies=cookies, error=OID.fetch_error())


@OID.loginhandler
def login() -> Union[werkzeug.wrappers.Response, str]:
    """Redirect to steam for authentication"""
    LOGGER.debug("In OID login")
    return OID.try_login("https://steamcommunity.com/openid")


@OID.after_login
def create_session(resp: flask_openid.OpenIDResponse) -> werkzeug.wrappers.Response:
    """called automatically instead of login() after successful authentication"""
    flask.session["steamid"] = resp.identity_url.rsplit("/", maxsplit=1)[-1]
    return flask.redirect(flask.url_for("sge.games_export_config"))


@APP_BP.route("/export", methods=("GET", "POST"))
def games_export_config() -> Union[werkzeug.wrappers.Response, str]:
    """Display and handle export config"""
    if "job_db_row" in flask.g:
        return finalize_extended_export(flask.g.job_db_row)

    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("sge.index"))

    if flask.request.method == "POST":
        if flask.request.form["format"] not in ["ods", "xls", "xlsx", "csv"]:
            flask.abort(400)

        steamid = flask.session["steamid"]
        # we don't need steamid anymore, so throw it out
        flask.session.clear()

        if flask.request.form.get("include-gameinfo"):
            return export_games_extended(steamid, flask.request.form["format"])

        return export_games_simple(steamid, flask.request.form["format"])

    return flask.render_template("export-config.html")

#XXX: sometimes games might be unavailable in our region
# in that case, querying the store api will result in following response:
#    {"<appid>": {"success": False}}
# afaik there is no workaround for this (without using a proxy of some kind)
# these titles must be queried from regions in which they are available
# this is also true for titles that have been removed from the store
# there is no way to distinguish between hidden and removed titles

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
# for this range are reasonable even for ods. Exceptions could be
# made for larger collections, but only for xlsx and ods
# xls and csv should not be retained because of their short export times
# csv doubly so because of its big file sizes

def export_games_extended(steamid: int, file_format: str
                         ) -> Union[werkzeug.wrappers.Response, str]:
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
            cookies=True,
            error="Cannot export data: this account does not own any games"
        )

    games_json = profile_json["games"]
    new_request = db.Request(games_json, file_format)

    games_ids = {row["appid"] for row in profile_json["games"]}
    db_session = db.SESSION()

    #FIXME: prevent "too many SQL variables" error by chunking this query (max=999)
    available_ids = db_session.query(db.GameInfo.appid).filter(
        db.GameInfo.appid.in_(games_ids)
    ).all()
    #FIXME: all ids are queried
    missing_ids = set(games_ids.difference(available_ids))
    if missing_ids:
        db_session.add(new_request)
        queue = []
        for appid in missing_ids:
            queue.append(db.Queue(appid=appid, job_uuid=new_request.job_uuid,
                                  timestamp=int(time.time())))

        resp = flask.make_response(
            flask.render_template(
                "login.html", cookies=True, error="Items added to the queue, return later"),
            202
        )
        resp.set_cookie(
            "job", value=new_request.job_uuid, max_age=COOKIE_MAX_AGE,
            path="/tools/steam-games-exporter/",
            secure=False, httponly=True, samesite="Lax"
        )
        db_session.bulk_save_objects(queue)
        db_session.commit()
        flask.g.queue_modified = True
        return resp
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
            cookies=True,
            error="Your request is still being processed. " \
                 f"Still fetching game info for {missing_ids} games"
        )

    games_json = json.loads(request_job.games_json)
    requested_appids = [row["appid"] for row in games_json]
    #FIXME: prevent "too many SQL variables" error by chunking this query (max=999)
    _games_info = db_session.query(db.GameInfo).filter(
        db.GameInfo.appid.in_(requested_appids)
    ).all()
    del requested_appids
    #associate each db.GameInfo object with its appid in a dict for easier and quicker lookup
    games_info = {row.appid:row for row in _games_info}

    # first row contains headers
    combined_games_data = [PROFILE_RELEVANT_FIELDS + GAMEINFO_RELEVANT_FIELDS]
    for json_row in games_json:
        info = games_info[json_row["appid"]]
        data = [json_row[field] for field in PROFILE_RELEVANT_FIELDS]
        data.extend([getattr(info, field) for field in GAMEINFO_RELEVANT_FIELDS])
        combined_games_data.append(data)

    del games_json, games_info

    file_format = request_job.export_format
    try:
        #csv requires file in write mode, rest in binary write
        tmp: Union[IO[str], IO[bytes]]
        if file_format == "ods":
            #FIXME: ods chokes on Nones in GameInfo table
            # site-packages/pyexcel_ods3/odsw.py", line 38, in write_row
            #   value_type = service.ODS_WRITE_FORMAT_COVERSION[type(cell)]
            # KeyError: <class 'NoneType'>
            # so much for the "don't worry about the format" part, eh?
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
        db_session.delete(flask.g.job_db_row)
        db_session.commit()

        file_response = flask.send_file(
            tmp.name, as_attachment=True, attachment_filename=f"games.{file_format}")
        file_response.set_cookie("job", "", expires=0, path="/tools/steam-games-exporter/",
                                 secure=False, httponly=True, samesite="Lax")
        return file_response
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
            cookies=True,
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
    STORE_DELAY = 1.5

    def __init__(self) -> None:
        self.requests_session = requests.Session()
        self.requests_session.headers["User-Agent"] = self.user_agent
        self.last_store_access = .0


    def __enter__(self) -> "APISession":
        return self


    #type literals available in python 3.8+, we're targeting 3.6+
    def __exit__(self, *args: Any, **kwargs: Any) -> False:
        self.requests_session.close()
        return False


    def query_store(self, appid: int) -> db.GameInfo:
        _query = requests.Request("GET", API_STORE_URL.format(appid=appid))
        prepared_query = self.requests_session.prepare_request(_query)
        store_json = self.query(prepared_query, max_retries=2).json()[str(appid)]

        if not store_json["success"]:
            LOGGER.warning("Invalid appid or app not available from our region: (%s)", appid)
            return db.GameInfo(appid=appid, timestamp=int(time.time()), unavailable=True)

        return db.GameInfo.from_json(appid=appid, info_json=store_json["data"])


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
                #FIXME: apply this only to requests to store api
                # this doesn't really matter because we only make one request to the profile
                # endpoint, but this should be fixed regardless
                time.sleep(max(0, self.STORE_DELAY + self.last_store_access - time.time()))
                self.last_store_access = time.time()
                response = self.requests_session.send(prepared_query, stream=True, timeout=15)
                response.raise_for_status()
                return response
            except requests.HTTPError:
                LOGGER.warning("Received HTTP error code %s", response.status_code)
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
                LOGGER.exception(
                    "Unexpected request exception: %s" \
                    "\nrequest url = %s" \
                    "\nrequest headers = %s",
                    err, urlparse(prepared_query.url).netloc, prepared_query.headers
                )
                raise err

            retry_count += 1
            delay = exp_delay[retry_count-1]
            LOGGER.info("Retrying (%s/%s) in %ss", retry_count, max_retries, delay)
            time.sleep(delay)
