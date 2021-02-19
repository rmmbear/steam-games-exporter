import re
import json
import time
import uuid
import logging

import sqlalchemy
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta

LOGGER = logging.getLogger(__name__)

RE_SIMPLE_HTML = re.compile("<.*?>")
ORM_BASE: DeclarativeMeta = declarative_base()

#TODO: naming collision with all the networking/server stuff, find a better name
class Request(ORM_BASE):
    __tablename__ = "requests_queue"
    job_uuid = sqlalchemy.Column(sqlalchemy.String, primary_key=True)
    timestamp = sqlalchemy.Column(sqlalchemy.Integer)
    games_json = sqlalchemy.Column(sqlalchemy.String, nullable=True)
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


class Queue(ORM_BASE):
    __tablename__ = "games_queue"
    appid = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    job_uuid = sqlalchemy.Column(sqlalchemy.String)
    app_name = sqlalchemy.Column(sqlalchemy.String)
    timestamp = sqlalchemy.Column(sqlalchemy.Integer)
    scrape = sqlalchemy.Column(sqlalchemy.Boolean, default=False)
    #^ scrape the info from store instead of using the api


class GameInfo(ORM_BASE):
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
        #{'<appid>': {'success': <bool>, 'data': {'steam_appid':<steam_appid>, ...}}}
        # info_json = json['<appid>']['data']
        #NOTE: steam_appid and appid are not guaranteed to be the same
        # (this is mostly the case for 'bonus' apps bundled with purchase)
        # (for example, a single-player game can have a multi-player mode as a deparate app)
        # (this app's steam_appid will actually be the main app's appid)
        # (I assume this is to make it possible to have both apps have the same store page)
        #appid = appid
        name = info_json["name"]
        type = info_json["type"]
        if "developers" in info_json:
            developers = ",\n".join(info_json["developers"])
        else:
            developers = None
        publishers = ",\n".join(info_json["publishers"])
        is_free = info_json["is_free"]
        on_linux = info_json["platforms"]["linux"]
        on_mac = info_json["platforms"]["mac"]
        on_windows = info_json["platforms"]["windows"]
        supported_languages = info_json["supported_languages"].replace("<br>", "\n")
        supported_languages = re.sub(RE_SIMPLE_HTML, "", supported_languages)
        controller_support = info_json.get("controller_support", "")
        age_gate = info_json["required_age"]
        if "categories" in info_json:
            categories = ",\n".join(
                [category["description"] for category in info_json["categories"]])
        else:
            categories = None
        if "genres" in info_json:
            genres = ",\n".join(genre["description"] for genre in info_json["genres"])
        else:
            genres = None
        release_date = info_json["release_date"]["date"]
        timestamp = int(time.time())
        unavailable = False

        kwargs = locals()
        del kwargs["cls"], kwargs["info_json"]
        new_obj = cls(**kwargs)
        return new_obj


SESSIONMAKER = sessionmaker(autocommit=False, autoflush=False)
SESSION = scoped_session(SESSIONMAKER)

def init(path: str) -> None:
    LOGGER.info("Performing db init")
    if path in ("", ":memory:"):
        engine = sqlalchemy.create_engine(f"sqlite:///{path}", echo=False, connect_args={'check_same_thread':False}, poolclass=sqlalchemy.pool.StaticPool)
    else:
        engine = sqlalchemy.create_engine(f"sqlite:///{path}", echo=False, connect_args={'check_same_thread':False})
    SESSIONMAKER.configure(bind=engine)
    ORM_BASE.metadata.create_all(bind=engine)
