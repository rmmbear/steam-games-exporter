""""""
import os
import csv
import json
import time
import logging
import tempfile

from typing import Any, IO, List, Union

import flask
import flask_openid
import werkzeug

import pyexcel_xls as pyxls
import pyexcel_xlsx as pyxlsx
import pyexcel_ods3 as pyods

import sge
from sge import db

LOG_FORMAT = logging.Formatter("[%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


COOKIE_MAX_AGE = 172800 # 2 days
# https://partner.steamgames.com/doc/webapi_overview/responses

# despite what the resources on this endpoint say, free games are included in the response
# even with include_played_free_games=0
# so instead let's pretend this is what we wanted

MSG_MISSING_GAMES = "Cannot export data, could not find any games! " \
                    "Please make sure 'game details' in your " \
                    "<a href=\"https://steamcommunity.com/my/edit/settings\">" \
                    "profile's privacy settings</a> is set to 'public'."
MSG_NO_COOKIES = "If your browser is rejecting cookies (which are necessary for this app), " \
                 "please allow cookies from https://misc.untextured.space in your browser's " \
                 "settings for this session."
MSG_PROCESSING_QUEUE = "Your request is still being processed. " \
                       "Still fetching game info for {missing_ids} games. " \
                       "This page will refresh automatically every 10 seconds."
MSG_QUEUE_CREATED = "{missing_ids} items added to the queue. " \
                    "Fetching them take at minimum {delay} minutes. " \
                    "This page will automatically refresh every 10 seconds."
MSG_RATE_LIMITED = "The server is currently rate limited and your request will take longer " \
                   "to complete."


PROFILE_RELEVANT_FIELDS = [
    "appid", "name", "playtime_forever", "playtime_windows_forever",
    "playtime_mac_forever", "playtime_linux_forever"
]
GAMEINFO_RELEVANT_FIELDS = [
    c.key for c in db.GameInfo.__table__.columns if c.key not in ["name", "appid",
                                                                  "timestamp", "unavailable"]
]

# env vars are set automatically by emperor (see vassal.ini), have to be set manually in dev env
# key.ini mentioned in vassal.ini is a one liner which sets the dev key as env var
# not included in the repo for obvious reasons
FLASK_ENV = os.environ.get("FLASK_ENV", default="production")
SQLITE_DB_PATH = os.environ.get("FLASK_DB_PATH", default="")
# if path is not set, use in-memory sqlite db ("sqlite:///")

OID = flask_openid.OpenID()
APP_BP = flask.Blueprint("sge", __name__, url_prefix="/tools/steam-games-exporter")
GAME_INFO_FETCHER = None

@APP_BP.before_app_first_request
def db_init() -> None:
    """Create engine, bind it to sessionmaker, and create tables"""
    LOGGER.info("Received first request, Initializing db")
    db.init(SQLITE_DB_PATH)
    if GAME_INFO_FETCHER:
        GAME_INFO_FETCHER.start()


@APP_BP.before_request
def load_job() -> None:
    LOGGER.debug("Received request")
    job_uuid = flask.request.cookies.get("job")
    if job_uuid:
        #FIXME: delay query until the resource is actually needed
        LOGGER.debug("Found job cookie %s", job_uuid)
        job_db_row = db.SESSION().query(db.Request).filter(
            db.Request.job_uuid == job_uuid
        ).first()
        if job_db_row:
            LOGGER.debug("Found job in db")
            flask.g.job_db_row = job_db_row
        else:
            LOGGER.info("Invalid job cookie found")
            flask.g.clear_job_cookie = True


@APP_BP.after_request
def finalize_request(resp: Any) -> None:
    if GAME_INFO_FETCHER and "queue_modified" in flask.g:
        LOGGER.info("Notifying fetcher thread of modified queue")
        GAME_INFO_FETCHER.notify()

    if "clear_job_cookie" in flask.g and "job_db_row" in flask.g:
        LOGGER.info("Clearing job cookie")
        resp.set_cookie(key="job", value="", expires=0, path="/tools/steam-games-exporter/",
                        secure=False, httponly=True, samesite="Lax")

    if "clear_session" in flask.g:
        LOGGER.info("Clearing session")
        flask.session.clear()

    return resp


@APP_BP.teardown_request
def close_db_session(exc: Any) -> None:
    """Close the scoped session during teardown"""
    LOGGER.debug("Request teardown")
    db.SESSION.remove()


@APP_BP.route("/")
def index() -> str:
    """landing page"""
    LOGGER.debug("Entering index view")
    #cookie check
    if "c" not in flask.session:
        flask.session["c"] = None

    return flask.render_template("index.html")

#FIXME: flask-openid encounters issues related to missing fields in steam's response
# KeyError: ('http://specs.openid.net/auth/2.0', 'assoc_type')
# this does not seem to cause any issues down the line
# this exact same issue: https://github.com/mitsuhiko/flask-openid/issues/48

@APP_BP.route("/login", methods=['GET', 'POST'])
def login_router() -> werkzeug.wrappers.Response:
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
    messages = []
    if not cookies:
        messages.append(("Missing cookies", MSG_NO_COOKIES))
    oid_error = OID.fetch_error()
    if oid_error:
        messages.append(("OpenID Error", oid_error))

    return flask.make_response(flask.render_template("error.html", messages=messages), 404)


@OID.loginhandler
def login() -> werkzeug.wrappers.Response:
    """Redirect to steam for authentication"""
    LOGGER.debug("In OID login")
    return OID.try_login("https://steamcommunity.com/openid")


@OID.after_login
def create_session(resp: flask_openid.OpenIDResponse) -> werkzeug.wrappers.Response:
    """called automatically instead of login() after successful authentication"""
    LOGGER.debug("creating new session")
    flask.session["steamid"] = resp.identity_url.rsplit("/", maxsplit=1)[-1]
    return flask.redirect(flask.url_for("sge.games_export_config"))


@APP_BP.route("/export", methods=("GET", "POST"))
def games_export_config() -> werkzeug.wrappers.Response:
    """Display and handle export config"""
    LOGGER.debug("Entering export config view")
    if "job_db_row" in flask.g:
        return finalize_extended_export(flask.g.job_db_row)

    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("sge.index"))

    if flask.request.method == "POST":
        if flask.request.form["format"] not in ["ods", "xls", "xlsx", "csv"]:
            flask.abort(400)

        steamid = flask.session["steamid"]
        # we don't need steamid anymore, so throw it out
        flask.g.clear_session = True

        if flask.request.form.get("include-gameinfo"):
            return export_games_extended(steamid, flask.request.form["format"])

        return export_games_simple(steamid, flask.request.form["format"])

    return flask.make_response(flask.render_template("export-config.html"), 200)


#XXX: sometimes games might be unavailable in our region
# in that case, querying the store api will result in following response:
#    {"<appid>": {"success": False}}
# afaik there is no workaround for this (without using a proxy of some kind)
# these titles must be queried from regions in which they are available
# this is also true for titles that have been removed from the store
# there is no way to distinguish between hidden and removed titles
#TODO: Figure out if this info can be scraped from somewhere else

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
                         ) -> werkzeug.wrappers.Response:
    """Initiate export, create all necessary db rows, return control to finalize_
    Returns:
        str - error page
        Request - newly added db.Request row
        flask response - successfully exported and began sending the file
    """
    LOGGER.debug("started extended export")
    with sge.APISession() as s:
        profile_json = s.query_profile(steamid)

    if not profile_json:
        messages = [("Error", MSG_MISSING_GAMES)]
        if GAME_INFO_FETCHER and GAME_INFO_FETCHER.rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(
            flask.render_template("error.html", messages=messages), 404
        )
        return resp

    games_json = profile_json["games"]
    new_request = db.Request(games_json, file_format)
    requested_ids = [row["appid"] for row in games_json]
    available_ids = []
    db_session = db.SESSION()

    batch_size = 999
    loop_num = 0
    last_batch_size = batch_size
    while last_batch_size == batch_size:
        loop_num += 1
        batch = requested_ids[batch_size*(loop_num-1):batch_size*loop_num]
        batch_result = db_session.query(db.GameInfo.appid).filter(
            db.GameInfo.appid.in_(batch)
        ).all()
        # query returns [(id1,), (id2,), ...], so we need to flatten the list
        available_ids.extend([row[0] for row in batch_result])
        last_batch_size = len(batch)

    missing_ids = set(requested_ids).difference(available_ids)
    if missing_ids:
        LOGGER.debug("Found %s missing ids in new request", len(missing_ids))
        queue = [new_request]
        for appid in missing_ids:
            queue.append(db.Queue(appid=appid, job_uuid=new_request.job_uuid,
                                  timestamp=int(time.time())))

        messages = [
            ("Processing",
             MSG_QUEUE_CREATED.format(
                 missing_ids=len(missing_ids), delay=(len(missing_ids) * 1.5) // 60 + 1)
            )
        ]
        if GAME_INFO_FETCHER and GAME_INFO_FETCHER.rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(
            flask.render_template("error.html", refresh=10, messages=messages), 202)
        resp.set_cookie(
            "job", value=new_request.job_uuid, max_age=COOKIE_MAX_AGE,
            path="/tools/steam-games-exporter/", secure=False, httponly=True, samesite="Lax"
        )
        db_session.bulk_save_objects(queue)
        db_session.commit()
        flask.g.queue_modified = True
        return resp
    #else: all necessary info already present in db, no need to persist the new request
    return finalize_extended_export(new_request)


def finalize_extended_export(request_job: db.Request) -> werkzeug.wrappers.Response:
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
        LOGGER.debug("There are %s missing ids for request %s", missing_ids, request_job.job_uuid)
        messages = [("Processing", MSG_PROCESSING_QUEUE.format(missing_ids=missing_ids))]
        if GAME_INFO_FETCHER and GAME_INFO_FETCHER.rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(
            flask.render_template("error.html", refresh=10, messages=messages), 200)
        return resp

    LOGGER.debug("Finalizing extended export")
    games_json = json.loads(request_job.games_json)
    _games_info = [] #type: List[db.GameInfo]

    batch_size = 999
    loop_num = 0
    last_batch_size = batch_size
    while last_batch_size == batch_size:
        loop_num += 1
        batch = [row["appid"] for row in games_json[batch_size*(loop_num-1):batch_size*loop_num]]
        _games_info.extend(db_session.query(db.GameInfo).filter(db.GameInfo.appid.in_(batch)).all())
        last_batch_size = len(batch)

    #associate each db.GameInfo object with its appid in a dict for easier and quicker lookup
    games_info = {row.appid:row for row in _games_info}

    # first row contains headers
    combined_games_data = [PROFILE_RELEVANT_FIELDS + GAMEINFO_RELEVANT_FIELDS]
    combined_games_data[0][0] = "store_url"
    for json_row in games_json:
        info = games_info[json_row["appid"]]
        data = [json_row[field] for field in PROFILE_RELEVANT_FIELDS]
        data.extend([getattr(info, field) for field in GAMEINFO_RELEVANT_FIELDS])
        data[0] = f"https://store.steampowered.com/app/{data[0]}"
        combined_games_data.append(data)

    del games_json, games_info

    file_format = request_job.export_format
    try:
        #TODO: figure out if pyexcel api supports chunked sequential write
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
            csv_writer = csv.writer(tmp, dialect="excel-tab")
            for row in combined_games_data:
                csv_writer.writerow(row)
        else:
            # this should be caught earlier in the flow, but _just in case_
            raise ValueError(f"Unknown file format: {file_format}")

        tmp.close()
        if "job_db_row" in flask.g:
            db_session.delete(flask.g.job_db_row)
            db_session.commit()

        flask.g.clear_job_cookie = True

        file_response = flask.send_file(
            tmp.name, as_attachment=True, attachment_filename=f"games.{file_format}")
        return file_response
    finally:
        tmp.close()
        os.unlink(tmp.name)


def export_games_simple(steamid: int, file_format: str
                       ) -> werkzeug.wrappers.Response:
    """Simple export without game info"""
    with sge.APISession() as s:
        profile_json = s.query_profile(steamid)

    if not profile_json:
        messages = [("Error", MSG_MISSING_GAMES)]
        if GAME_INFO_FETCHER and GAME_INFO_FETCHER.rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(flask.render_template("error.html", messages=messages), 404)
        return resp

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
            csv_writer = csv.writer(tmp, dialect="excel-tab")
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
