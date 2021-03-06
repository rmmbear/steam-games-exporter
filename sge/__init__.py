"""Steam Games Exporter
This package is meant to be run by uwsgi emperor.
See vassal.ini for details on uwsgi configuration.
"""
__VERSION__ = "0.2"

import os
import time
import logging
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# enable 2.0 removal warnings
# https://docs.sqlalchemy.org/en/14/core/exceptions.html#sqlalchemy.exc.RemovedIn20Warning
os.environ["SQLALCHEMY_WARN_20"] = '1'

import flask
import requests
import sqlalchemy
import sqlalchemy.orm

from sge import db
from sge import views

LOG_FORMAT = logging.Formatter("%(asctime)s [SGE][%(levelname)s]: %(message)s",
                               "%Y-%m-%d %H:%M:%S")
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
TH = logging.StreamHandler()
TH.setLevel(logging.INFO)
TH.setFormatter(LOG_FORMAT)
LOGGER.addHandler(TH)


class ConfigProduction():
    """Object holding config variables for production environment."""
    MAX_CONTENT_LENGTH = 512*1024
    # key is generated each time app is launched
    # sessions are short-lived and app state does not depend on them
    # so losing a session after reload is not a concern
    SECRET_KEY = os.urandom(16)
    SERVER_NAME = "misc.untextured.space"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = True
    STATIC_URL_PATH = "/tools/steam-games-exporter/static"


class ConfigDevelopment():
    """Object holding config variables for development environment."""
    DEBUG = True
    MAX_CONTENT_LENGTH = ConfigProduction.MAX_CONTENT_LENGTH
    SECRET_KEY = "devkey"
    SESSION_COOKIE_HTTPONLY = ConfigProduction.SESSION_COOKIE_HTTPONLY
    SESSION_COOKIE_SAMESITE = ConfigProduction.SESSION_COOKIE_SAMESITE
    SESSION_COOKIE_SECURE = False
    STATIC_URL_PATH = "/static"


ENV_TO_CONFIG = {
    "production": ConfigProduction,
    "development": ConfigDevelopment,
}
COOKIE_MAX_AGE = 172800 # 2 days, chosen arbitrarily


def create_app(app_config: object, steam_key: str, db_path: str,
               page_refresh: int = 5) -> flask.Flask:
    """Create flask app, configure it, and register blueprint from
    sge/views.py
    """
    cwd = os.path.realpath(__file__).rsplit("/", maxsplit=1)[0]
    app = flask.Flask(
        __name__,
        static_url_path=app_config.STATIC_URL_PATH, #type: ignore
        static_folder=os.path.join(cwd, "../static"),
        template_folder=os.path.join(cwd, "../templates"),
    )
    # apply basic config to the app
    app.config.from_object(app_config)
    # extend config with instance-specific variables
    app.config["SGE_DB_PATH"] = db_path
    app.config["SGE_SCOPED_SESSION"] = db.init(db_path)
    app.config["SGE_FETCHER_THREAD"] = GameInfoFetcher(app.config["SGE_SCOPED_SESSION"])
    app.config["SGE_STEAM_DEV_KEY"] = steam_key
    app.config["SGE_PAGE_REFRESH"] = page_refresh

    app.register_blueprint(views.APP_BP)
    views.OID.init_app(app)

    if app.env == "production" and not app.config["SGE_DB_PATH"]:
        raise RuntimeError("Running in prod without db path specified")

    if app.debug or app.testing:
        TH.setLevel(logging.DEBUG)
        LOGGER.setLevel(logging.DEBUG)

    return app


def cleanup(signal: int, app: flask.Flask) -> None:
    """Remove old requests and vacuum the database.
    This command is intended to be called by uwsgi cron every day
    (see run.py).
    """
    LOGGER.debug("Received uwsgi signal %s", signal)
    LOGGER.info("Cleaning old requests")
    cutoff = int(time.time()) - COOKIE_MAX_AGE
    db_session = app.config["SGE_SCOPED_SESSION"]()
    db_session.execute(sqlalchemy.delete(db.Request).where(db.Request.timestamp <= cutoff))
    db_session.commit()

    LOGGER.info("Vacuuming sqlite db")
    db_session.execute(sqlalchemy.text("VACUUM"))
    db_session = app.config["SGE_SCOPED_SESSION"].remove()


class GameInfoFetcher(threading.Thread):
    """Background thread for processing db.Queue."""
    def __init__(self, db_session_proxy: sqlalchemy.orm.scoped_session) -> None:
        super().__init__(target=None, name="store_info_fetcher", daemon=False)
        self.condition = threading.Condition()
        self._terminate = threading.Event()
        self.rate_limited = False
        self.scoped_session = db_session_proxy

        def shutdown_notify(fetcher_thread: "GameInfoFetcher") -> None:
            """Tell fetcher to terminate when main thread stops."""
            # wait until main thread stops execution
            threading.main_thread().join()
            # trigger termination event and wake fetcher thread
            #LOGGER.info("Requesting fetcher thread termination")
            fetcher_thread._terminate.set()
            fetcher_thread.notify(force=True)

        self.shutdown_notifier = threading.Thread(
            target=shutdown_notify, name="shutdown_notify", args=(self,), daemon=False
        )


    def _wait(self, timeout: Optional[int] = None, rate_limited: bool = False) -> None:
        """Put the thread to sleep.
        Thread will sleep until notified with notify(), or timeout is
        reached. Do _not_ call from outside this thread.
        """
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
        """Wake the thread to resume queue processing."""
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
        db_session = self.scoped_session()
        # 20 items = 30 seconds (at minimum) at 1.5s delay between requests
        queue_query = sqlalchemy.select(db.Queue).order_by(db.Queue.timestamp.asc()).limit(20)
        LOGGER.info("Starting shutdown notifier")
        self.shutdown_notifier.start()
        with APISession() as api_session:
            while True:
                if self._terminate.is_set():
                    self.scoped_session.remove()
                    LOGGER.info("Terminating fetcher thread (idle)")
                    return

                queue_batch = db_session.execute(queue_query).scalars().all()
                if not queue_batch:
                    LOGGER.info("Nothing in the queue for fetcher, waiting")
                    self.scoped_session.remove()
                    self._wait()
                    db_session = self.scoped_session()
                    continue

                LOGGER.debug("Processing batch")
                for queue_item in queue_batch:
                    if self._terminate.is_set():
                        self.scoped_session.remove()
                        LOGGER.info("Terminating fetcher thread (processing)")
                        return

                    if db_session.get(db.GameInfo, int(queue_item.appid)):
                        LOGGER.warning("encountered queue item for an already known app (%s)",
                                       queue_item.appid)
                        db_session.delete(queue_item)
                        db_session.commit()
                        continue

                    try:
                        game_info = api_session.query_store(queue_item.appid)
                        db_session.add(game_info)
                        db_session.delete(queue_item)
                        db_session.commit()
                    except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as exc:
                        LOGGER.warning("Network error: %s", exc)
                        if exc.response and exc.response.status_code == 429:
                            # wait longer if we're rate limited,
                            # store api does not tell us how long we have to wait
                            self._wait(timeout=60, rate_limited=True)
                        else:
                            self._wait(10, rate_limited=True)

                        continue
                    except Exception as exc:
                        LOGGER.exception("Ignoring unexpected exception:")
                        db_session.rollback()
                        # move item to the bottom of the stack
                        queue_item.timestamp = int(time.time())
                        db_session.commit()
                        self._wait(10, rate_limited=True)
                        break



class APISession():
    """Simple context manager taking advantage of connection pooling"""
    user_agent = f"SteamGamesFetcher/{__VERSION__} (+https://github.com/rmmbear)"
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

    def __init__(self) -> None:
        self.requests_session = requests.Session()
        self.requests_session.headers["User-Agent"] = self.user_agent
        self.access_times: Dict[str, float] = {}


    def __enter__(self) -> "APISession":
        return self


    #type literals from typing available in python 3.8+, we're targeting 3.6+
    def __exit__(self, *args: Any, **kwargs: Any) -> False:
        self.requests_session.close()
        return False


    def query_store(self, appid: int) -> db.GameInfo:
        """Fetch information about app from steam store api.
        Will attempt to retry the request in case of recoverable errors.
        Caller should expect at least HTTP errors (see query()).
        """
        _query = requests.Request("GET", self.API_STORE_URL.format(appid=appid))
        prepared_query = self.requests_session.prepare_request(_query)
        store_json = self.query(prepared_query, max_retries=2, min_delay=1.5).json()[str(appid)]

        if not store_json["success"]:
            LOGGER.warning("Invalid appid or app not available from our region: %s", appid)
            return db.GameInfo(appid=appid, timestamp=int(time.time()), unavailable=True)

        return db.GameInfo.from_json(appid=appid, info_json=store_json["data"])


    def query_profile(self, steamid: int) -> Optional[dict]:
        """Fetch information about app from steam store api.
        Errors are raised immediately, no retries even in case of
        recoverable HTTP errs. Caller should expect at least HTTP errors
        (see query()).
        """
        steam_key = flask.current_app.config["SGE_STEAM_DEV_KEY"]
        _query = requests.Request("GET", self.API_GAMES_URL.format(key=steam_key, steamid=steamid))
        prepared_query = self.requests_session.prepare_request(_query)

        games_json = self.query(prepared_query, max_retries=0, min_delay=0).json()["response"]
        if not games_json:
            return None
        return games_json


    def query(self, prepared_query: requests.PreparedRequest, max_retries: int = 2,
              min_delay: float = 0, timeout: int = 15) -> requests.Response:
        """Error handling helper.
        max_retries - how many times to re-attempt the query after encountering errors
        min_delay   - minimum amount of time that needs to pass before making subsequent
                      requests to given address. Last access time stored in self.access_times,
                      a dict whose keys are made from netloc part of urlparse(prepared_query.url)
        timeout     - max time in seconds to spend waiting for request to finish
        """
        netloc = str(urlparse(prepared_query.url).netloc)
        exp_delay = [2**x for x in range(max_retries)]
        retry_count = 0
        while True:
            try:
                time.sleep(max(0, self.access_times.get(netloc, .0) + min_delay - time.time()))
                self.access_times[netloc] = time.time()
                response = self.requests_session.send(prepared_query, stream=True, timeout=timeout)
                response.raise_for_status()
                return response
            except requests.HTTPError:
                LOGGER.warning("Received HTTP error code %s", response.status_code)
                if response.status_code not in self.KNOWN_API_RESPONSES:
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
                    err, netloc, prepared_query.headers
                )
                raise err

            retry_count += 1
            delay = exp_delay[retry_count-1]
            LOGGER.info("Retrying (%s/%s) in %ss", retry_count, max_retries, delay)
            time.sleep(delay)
