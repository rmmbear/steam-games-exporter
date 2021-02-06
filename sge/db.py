import json
import uuid
import time

import sqlalchemy
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta

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


SESSIONMAKER = sessionmaker(autocommit=False, autoflush=False)
SESSION = scoped_session(SESSIONMAKER)

def init(url: str) -> None:
    engine = sqlalchemy.create_engine(url, echo=False)
    SESSIONMAKER.configure(bind=engine)
    ORM_BASE.metadata.create_all(bind=engine)
