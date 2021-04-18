"""Flask views, blueprint definition, and spreadsheet export functions.
"""
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

MSG_MISSING_GAMES = "Cannot export data, could not find any games! " \
                    "Please make sure 'game details' in your " \
                    "<a href=\"https://steamcommunity.com/my/edit/settings\"> " \
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

OID = flask_openid.OpenID()
APP_BP = flask.Blueprint("sge", __name__, url_prefix="/tools/steam-games-exporter")


@APP_BP.before_request
def load_job() -> None:
    """Check for job cookies, load corresponding job from db.
    Mark the cookie for deletion if no match found in db.
    """
    LOGGER.debug("Received request")
    job_uuid = flask.request.cookies.get("job")
    db_session = flask.current_app.config["SGE_SCOPED_SESSION"]()
    if job_uuid:
        #FIXME: delay query until the resource is actually needed
        LOGGER.debug("Found job cookie %s", job_uuid)
        job_db_row = db_session.query(db.Request).filter(
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
    """Notify fetcher thread of queue modifications (if any), and clear
    no longer needed/invalid cookies.
    """
    if "queue_modified" in flask.g:
        LOGGER.info("Notifying fetcher thread of modified queue")
        flask.current_app.config["SGE_FETCHER_THREAD"].notify()

    if "clear_job_cookie" in flask.g:
        LOGGER.info("Clearing job cookie")
        resp.set_cookie(key="job", value="", expires=0, path="/tools/steam-games-exporter/",
                        secure=False, httponly=True, samesite="Lax")

    if "clear_session" in flask.g:
        LOGGER.info("Clearing session")
        flask.session.clear()

    return resp


@APP_BP.teardown_request
def close_db_session(exc: Any) -> None:
    """Remove this thread's session."""
    LOGGER.debug("Request teardown")
    flask.current_app.config["SGE_SCOPED_SESSION"].remove()


@APP_BP.route("/")
def index() -> str:
    """Landing page.
    Sets a session cookie to allow for cookie check later.
    """
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
    """Routes the user to either login, export config or error page.
    Separated from actual login() to avoid making premature requests
    during OID provider discovery (this is done automatically in OID
    loginhandler and cannot be avoided).
    """
    LOGGER.debug("In login router")
    openid_complete = flask.request.args.get("openid_complete")
    if openid_complete:
        return login()

    if "job_db_row" in flask.g:
        return check_extended_export(flask.g.job_db_row)
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
    """Called automatically instead of login() after successful
    authentication.
    """
    LOGGER.debug("creating new session")
    flask.session["steamid"] = resp.identity_url.rsplit("/", maxsplit=1)[-1]
    return flask.redirect(flask.url_for("sge.games_export_config"))


@APP_BP.route("/export", methods=("GET", "POST"))
def games_export_config() -> werkzeug.wrappers.Response:
    """Display and handle export config."""
    LOGGER.debug("Entering export config view")
    if "job_db_row" in flask.g:
        return check_extended_export(flask.g.job_db_row)

    if "steamid" not in flask.session:
        return flask.redirect(flask.url_for("sge.index"))

    if flask.request.method == "POST":
        if flask.request.form["format"] not in ["ods", "xls", "xlsx", "csv"]:
            flask.abort(400)

        steamid = flask.session["steamid"]
        # we don't need steamid anymore, so throw it out
        flask.g.clear_session = True

        if flask.request.form.get("include-gameinfo"):
            return prepare_extended_export(steamid, flask.request.form["format"])

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


def check_for_missing_ids(requested_ids: List[int]) -> set:
    """Check if the request being handled has any appids still in the
    Queue. Returns list of integers (appids).
    """
    db_session = flask.current_app.config["SGE_SCOPED_SESSION"]()
    available_ids = db.in_query_chunked(
        db_session, db.GameInfo.appid, db.GameInfo.appid, requested_ids)
    # returned = [(<appid>,), (<appid>), ...], we need to flatten that list
    available_ids = [row[0] for row in available_ids]
    missing_ids = set(requested_ids).difference(available_ids)
    LOGGER.debug("Found %s missing ids in new request", len(missing_ids))

    return missing_ids


def prepare_extended_export(steamid: int, file_format: str) -> werkzeug.wrappers.Response:
    """Initiate export, create new request and queue items if necessary.
    If all info is available, then finalize the export immediately
    without persisting the request.
    """
    LOGGER.debug("started extended export")
    with sge.APISession() as s:
        profile_json = s.query_profile(steamid)

    if not profile_json:
        messages = [("Error", MSG_MISSING_GAMES)]
        if flask.current_app.config["SGE_FETCHER_THREAD"].rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(
            flask.render_template("error.html", messages=messages), 404
        )
        return resp

    games_json = profile_json["games"]

    requested_ids = [row["appid"] for row in games_json]
    missing_ids = check_for_missing_ids(requested_ids)
    db_session = flask.current_app.config["SGE_SCOPED_SESSION"]()
    if missing_ids:
        new_request = db.Request(games_json, file_format)
        queue = [new_request]
         # compare missing ids against currently queued ids
        queued_ids = db.in_query_chunked(
            db_session, db.Queue.appid, db.Queue.appid, list(missing_ids)
        )
        queued_ids = [row[0] for row in queued_ids]
        missing_ids = missing_ids.difference(queued_ids)
        LOGGER.debug("%s missing ids after comparing with queue", len(missing_ids))
        for appid in missing_ids:
            queue.append(db.Queue(appid=appid, job_uuid=new_request.job_uuid,
                                  timestamp=int(time.time())))
        messages = [
            ("Processing",
             MSG_QUEUE_CREATED.format(
                 missing_ids=len(missing_ids), delay=(len(missing_ids) * 1.5) // 60 + 1)
            )
        ]
        if flask.current_app.config["SGE_FETCHER_THREAD"].rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(
            flask.render_template("error.html", refresh=10, messages=messages), 202)
        resp.set_cookie(
            "job", value=new_request.job_uuid, max_age=sge.COOKIE_MAX_AGE,
            path="/tools/steam-games-exporter/", secure=False, httponly=True, samesite="Lax"
        )
        db_session = flask.current_app.config["SGE_SCOPED_SESSION"]()
        db_session.bulk_save_objects(queue)
        db_session.commit()
        flask.g.queue_modified = True
        return resp
    #else: all necessary info already present in db, no need to persist the new request
    return finalize_extended_export(games_json, requested_ids, file_format)


def check_extended_export(request_job: db.Request) -> werkzeug.wrappers.Response:
    """Check if we can proceed with export for previously recorded job.
    """
    db_session = flask.current_app.config["SGE_SCOPED_SESSION"]()
    profile_info = json.loads(request_job.games_json)
    requested_ids = [row["appid"] for row in profile_info]
    missing_ids = len(check_for_missing_ids(requested_ids))

    #FIXME: communicate properly that there might be other profiles in the queue
    if missing_ids:
        LOGGER.debug("There are %s missing ids for request %s", missing_ids, request_job.job_uuid)
        messages = [("Processing", MSG_PROCESSING_QUEUE.format(missing_ids=missing_ids))]
        if flask.current_app.config["SGE_FETCHER_THREAD"].rate_limited:
            messages.append(("Error", MSG_RATE_LIMITED))
        resp = flask.make_response(
            flask.render_template("error.html", refresh=10, messages=messages), 202)
        return resp

    if "job_db_row" in flask.g:
        #we're removing the request before it is finalized
        # in case of an app error, the user will have to re-submit their request
        db_session.delete(flask.g.job_db_row)
        db_session.commit()

    return finalize_extended_export(profile_info, requested_ids, request_job.export_format)


def finalize_extended_export(profile_info: dict, requested_ids: List[int], export_format: str
                            ) -> werkzeug.wrappers.Response:
    """Combine profile json with stored game info."""
    LOGGER.debug("Finalizing extended export")
    db_session = flask.current_app.config["SGE_SCOPED_SESSION"]()
    _games_info: List[db.GameInfo] = []
    _games_info = db.in_query_chunked(db_session, db.GameInfo, db.GameInfo.appid, requested_ids)
    #associate each db.GameInfo object with its appid in a dict for easier and quicker lookup
    games_info = {row.appid:row for row in _games_info}

    # first row contains headers
    combined_games_data = [PROFILE_RELEVANT_FIELDS + GAMEINFO_RELEVANT_FIELDS]
    combined_games_data[0][0] = "store_url"
    for json_row in profile_info:
        info = games_info[json_row["appid"]]
        data = [json_row[field] for field in PROFILE_RELEVANT_FIELDS]
        data.extend([getattr(info, field) for field in GAMEINFO_RELEVANT_FIELDS])
        data[0] = f"https://store.steampowered.com/app/{data[0]}"
        combined_games_data.append(data)

    del profile_info, games_info

    return send_exported_file(combined_games_data, export_format)


def export_games_simple(steamid: int, file_format: str
                       ) -> werkzeug.wrappers.Response:
    """Simple export without game info."""
    with sge.APISession() as s:
        profile_json = s.query_profile(steamid)

    if not profile_json:
        resp = flask.make_response(
            flask.render_template("error.html", messages=[("Error", MSG_MISSING_GAMES)]), 404)
        return resp

    games_json = profile_json["games"]
    games = [list(PROFILE_RELEVANT_FIELDS)]
    games[0][0] = "store_url"
    # iterate over the games, extract only relevant fields, replace appid with store link
    for raw_row in games_json:
        game_row = [raw_row[field] for field in PROFILE_RELEVANT_FIELDS]
        game_row[0] = "https://store.steampowered.com/app/{}".format(game_row[0])
        games.append(game_row)

    return send_exported_file(games, file_format)


def send_exported_file(export_data: List[List[Any]], export_format: str
                      ) -> werkzeug.wrappers.Response:
    """Export and save provided data into a temporary file and send that
    to the client.
    """
    try:
        #TODO: figure out if pyexcel api supports chunked sequential write
        #csv requires file in write mode, rest in binary write
        tmp: Union[IO[str], IO[bytes]]
        if export_format == "ods":
            #FIXME: ods chokes on Nones in GameInfo table
            # site-packages/pyexcel_ods3/odsw.py", line 38, in write_row
            #   value_type = service.ODS_WRITE_FORMAT_COVERSION[type(cell)]
            # KeyError: <class 'NoneType'>
            # so much for the "don't worry about the format" part, eh?
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyods.save_data(tmp, {"GAMES":export_data})
        elif export_format == "xls":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyxls.save_data(tmp, {"GAMES":export_data})
        elif export_format == "xlsx":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            pyxlsx.save_data(tmp, {"GAMES":export_data})
        elif export_format == "csv":
            tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
            csv_writer = csv.writer(tmp, dialect="excel-tab")
            for row in export_data:
                csv_writer.writerow(row)
        else:
            # this should be caught earlier in the flow, but _just in case_
            raise ValueError(f"Unknown file format: {export_format}")

        tmp.close()
        flask.g.clear_job_cookie = True
        file_response = flask.send_file(
            tmp.name, as_attachment=True, attachment_filename=f"games.{export_format}")
        return file_response
    finally:
        tmp.close()
        os.unlink(tmp.name)
