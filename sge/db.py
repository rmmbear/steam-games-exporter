"""Database model."""
import re
import copy
import json
import time
import uuid
import sqlite3
import logging

from typing import Any, List, Optional

import sqlalchemy
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.orm.decl_api import DeclarativeMeta

LOGGER = logging.getLogger(__name__)

# https://sqlite.org/limits.html
SQLITE_MAX_VARIABLE_NUMBER = 999
if sqlite3.sqlite_version_info[0] > 3 or \
   (sqlite3.sqlite_version_info[0] == 3 and sqlite3.sqlite_version_info[1] >= 32):
    SQLITE_MAX_VARIABLE_NUMBER = 32766

RE_SIMPLE_HTML = re.compile(r"<.*?>")
ORM_BASE: DeclarativeMeta = sqlalchemy.orm.declarative_base()

#TODO: naming collision with all the networking/server stuff, find a better name
class Request(ORM_BASE):
    """Table containing all requests which could not be fulfilled
    immediately.
    """
    __tablename__ = "requests_queue"
    job_uuid = sqlalchemy.Column(sqlalchemy.String, primary_key=True)
    timestamp = sqlalchemy.Column(sqlalchemy.Integer, nullable=False)
    games_json = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    export_format = sqlalchemy.Column(sqlalchemy.String)
    generated_file = sqlalchemy.Column(sqlalchemy.String, nullable=True)

    def __init__(self, games_json: dict, export_format: str) -> None:
        if export_format not in ["ods", "xls", "xlsx", "csv"]:
            raise ValueError(f"Export format not recognized {export_format}")

        self.job_uuid = str(uuid.uuid4())
        self.timestamp = int(time.time())
        self.games_json = json.dumps(games_json)
        self.export_format = export_format
        self.generated_file = None


    def __repr__(self) -> str:
        return "<Request({} queued at {})>".format(
            self.job_uuid, self.timestamp
        )



class Queue(ORM_BASE):
    """Table serving as a queue for the game info fetcher."""
    __tablename__ = "games_queue"
    appid = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    job_uuid = sqlalchemy.Column(sqlalchemy.String)
    app_name = sqlalchemy.Column(sqlalchemy.String)
    timestamp = sqlalchemy.Column(sqlalchemy.Integer)
    #regenerate = sqlalchemy.Column(sqlalchemy.Boolean, default=False)
    #^ if true and gameinfo for this appid exists, regenerate it

    def __repr__(self) -> str:
        return "<Queue(app {} for request {})>".format(
            self.appid, self.job_uuid
        )



class GameInfo(ORM_BASE):
    """Table containing all relevant information about requested games.
    """
    __tablename__ = "games_info"
    appid = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    name = sqlalchemy.Column(sqlalchemy.String)
    type = sqlalchemy.Column(sqlalchemy.String)
    developers = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    publishers = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    is_free = sqlalchemy.Column(sqlalchemy.Boolean)
    on_linux = sqlalchemy.Column(sqlalchemy.Boolean)
    on_mac = sqlalchemy.Column(sqlalchemy.Boolean)
    on_windows = sqlalchemy.Column(sqlalchemy.Boolean)
    supported_languages = sqlalchemy.Column(sqlalchemy.String)
    controller_support = sqlalchemy.Column(sqlalchemy.String)
    age_gate = sqlalchemy.Column(sqlalchemy.Integer)
    categories = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    genres = sqlalchemy.Column(sqlalchemy.String) #(csv, sep=",\n")
    release_date = sqlalchemy.Column(sqlalchemy.String)
    #content_descriptors
    #^this field seems to always be set to null,
    # even when that information is normally present on the store page
    timestamp = sqlalchemy.Column(sqlalchemy.Integer, nullable=False)
    unavailable = sqlalchemy.Column(sqlalchemy.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return "<GameInfo(appid='{}', name='{}', ... timestamp='{}', unavailable='{}')>".format(
            self.appid, self.name, self.timestamp, self.unavailable
        )


    @classmethod
    def from_json(cls, appid: int, info_json: dict) -> "GameInfo":
        """Create new row from dumped json, as returned by
        sge.APISession.query().
        """
        #{'<appid>': {'success': <bool>, 'data': {'steam_appid':<steam_appid>, ...}}}
        # info_json = json['<appid>']['data']
        #NOTE: steam_appid and appid are not guaranteed to be the same
        # (this is mostly the case for 'bonus' apps bundled with purchase)
        # (for example, a single-player game can have a multi-player mode as a separate app)
        # (this app's steam_appid will actually be the main app's appid)
        # (I assume this is to make it possible to have both apps have the same store page)
        #appid = appid
        name = info_json.get("name")
        type = info_json.get("type")
        developers: Optional[str]
        if "developers" in info_json:
            developers = ",\n".join(info_json["developers"])
        else:
            developers = None
        publishers = ",\n".join(info_json.get("publishers", ""))
        is_free = info_json.get("is_free", False)
        # platforms should always be available, but I thought the same was true of other fields
        # and since some other fields aren't always present I'm just playing it safe
        platforms = info_json.get("platforms", {"linux": None, "mac": None, "windows": None})
        on_linux = platforms["linux"]
        on_mac = platforms["mac"]
        on_windows = platforms["windows"]
        supported_languages = info_json.get("supported_languages", "").replace("<br>", "\n")
        supported_languages = re.sub(RE_SIMPLE_HTML, "", supported_languages)
        controller_support = info_json.get("controller_support")
        age_gate = info_json.get("required_age")
        categories: Optional[str]
        if "categories" in info_json:
            categories = ",\n".join(
                [category["description"] for category in info_json["categories"]])
        else:
            categories = None
        genres: Optional[str]
        if "genres" in info_json:
            genres = ",\n".join(genre["description"] for genre in info_json["genres"])
        else:
            genres = None
        release_date = info_json.get("release_date", {}).get("date")
        try:
            if release_date:
                release_date = time.strftime("%Y/%m/%d", time.strptime(release_date, "%d %b, %Y"))
        except ValueError:
            LOGGER.error(
                "release date does not match known date format: %s (expected '%%d %%b, %%Y')",
                release_date
            )
            # allow inconsistent dates
            # having to manually correct these later is preferrable to sge crashing and burning

        timestamp = int(time.time())
        unavailable = False

        # instead of tediously copying and pasting all variables into the contstructor call,
        # dump the local namespace instead (after removing redundant variables first)
        kwargs = copy.copy(locals())
        del kwargs["cls"], kwargs["info_json"], kwargs["platforms"]
        new_obj = cls(**kwargs)
        return new_obj



def init(path: str) -> sqlalchemy.orm.scoped_session:
    """Configure the engine, bind it to the sessionmaker, create tables.
    """
    LOGGER.info("Performing db init")

    if path in ("", ":memory:"):
        LOGGER.info("Initializing an in-memory database")
        engine = sqlalchemy.create_engine(
            f"sqlite:///{path}", echo=False, poolclass=sqlalchemy.pool.StaticPool,
            connect_args={"check_same_thread":False}
        )
    else:
        engine = sqlalchemy.create_engine(
            f"sqlite:///{path}", echo=False, connect_args={"check_same_thread":False}
        )

    configured_sessionmaker = sessionmaker(engine, autocommit=False, autoflush=False)
    scoped_session_proxy = scoped_session(configured_sessionmaker)
    ORM_BASE.metadata.create_all(bind=engine)

    return scoped_session_proxy


def in_query_chunked(db_session: sqlalchemy.orm.Session, query_target: ORM_BASE,
                     filter_from: ORM_BASE, in_value: list,
                     batch_size: int = SQLITE_MAX_VARIABLE_NUMBER) -> list:
    """Perform sqlalchemy in_() operation on the query, but in chunks to
    avoid triggering the 'too many SQLite variables' error.
    """
    query_return: List[Any] = []
    loop_num = 0
    last_batch_size = batch_size
    while in_value:
        batch = in_value[:batch_size]
        query_return.extend(
            db_session.execute(sqlalchemy.select(query_target).\
            where(filter_from.in_(batch))).scalars().all()
        )
        in_value = in_value[batch_size:]

    return query_return
