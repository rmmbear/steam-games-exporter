import os
import sys
import time
import logging
import tempfile

import flask
import requests
import flask_openid
import pyexcel_ods3 as ods

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


@APP.route('/tools/steam-games-exporter/')
def index():
    """landing page"""
    #cookie check
    if "c" not in flask.session:
        flask.session["c"] = None

    return flask.render_template("index.html")


@APP.route('/tools/steam-games-exporter/login', methods=['GET', 'POST'])
@OID.loginhandler
def login():
    """Redirect to steam for authentication.
    Successful
    """
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
    """display and handle export config"""
    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("index"))
    if flask.request.method == "POST":
        return games_export_simple()

    return flask.render_template("export-config.html")

#XXX: sometimes games might be unavailable in our region
# in that case, querying the store api will result in following response:
#    {"<appid>": {"success": False}}
# afaik there is no workaround for this (without using a proxy of some kind)
# these titles must be queried from regions in which they are available
#TODO: offer xlsx and csv export

def games_export_extended():
    raise NotImplementedError()


def games_export_simple():
    """Simple export without game info"""
    api_session = APISession()
    with api_session as s:
        games_json = s.query_profile(flask.session["steamid"])

    if not games_json:
        return flask.render_template(
            "login.html",
            error="Cannot export data: this account does not own any games"
        )
    # we don't need steamid anymore, so throw it out
    del flask.session["steamid"]
    games_json = games_json["games"]

    games = [list(PROFILE_RELEVANT_FIELDS)]
    games[0][0] = "store_url"
    # iterate over the games, extract only relevant fields, replace appid with store link
    for raw_row in games_json:
        game_row = [raw_row[field] for field in PROFILE_RELEVANT_FIELDS]
        game_row[0] = "https://store.steampowered.com/app/{}".format(game_row[0])
        games.append(game_row)

    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        ods.save_data(tmp, {"GAMES":games})
        tmp.close()
        return flask.send_file(tmp.name, as_attachment=True, attachment_filename="games.ods")
    finally:
        os.unlink(tmp.name)


class APISession():
    """Simple context manager taking advantage of connection pooling"""
    user_agent = f"SteamGamesFetcher/{__VERSION__} (+https://github.com/rmmbear)"

    def __init__(self):
        self.requests_session = requests.Session()
        self.requests_session.headers["User-Agent"] = self.user_agent


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_val, exc_tb):
        self.requests_session.close()
        return False


    def query_store(self, appid: int) -> "json":
        raise NotImplementedError()
        query = requests.Request("GET", STORE_API.format(appid=appid))
        query = self.requests_session.prepare_request(query)


    def query_profile(self, steamid: int) -> "json":
        _query = requests.Request("GET", GAMES_API.format(key=STEAM_DEV_KEY, steamid=steamid), 0)
        _query = self.requests_session.prepare_request(_query)

        games_json = self.query(_query).json()["response"]
        if not games_json:
            return None
        return games_json


    def query(self, prepared_query: requests.PreparedRequest, max_retries: int = 2
             ) -> requests.Response:
        """Error handling helper"""
        exp_delay = (2**x for x in range(max_retries))
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
                LOGGER.error("request body = %s", prepared_query.body)
                raise err

            retry_count += 1
            delay = exp_delay[retry_count-1]
            LOGGER.info("Retrying (%s/%s) in %ss", retry_count, max_retries, delay)
            time.sleep(delay)
