"""This is only for the benefit of uwsgi emperor
"""
import os
import sys
import shutil
import logging
import logging.handlers

try:
    # https://uwsgi-docs.readthedocs.io/en/latest/PythonModule.html
    import uwsgi
except ImportError:
    # we're not running in uwsgi mode
    pass

# uwsgi emperor launches the app from within the venv
# so path from which sge can be imported must be added manually
sys.path.append(os.path.realpath(__file__).rsplit("/", maxsplit=1)[0])
import sge

# env vars are set automatically by emperor (see vassal.ini), have to be set manually in dev env
# key.ini mentioned in vassal.ini is a one liner which sets the dev key as env var
# not included in the repo for obvious reasons
STEAM_KEY = os.environ.get("STEAM_DEV_KEY")
FLASK_ENV = os.environ.get("FLASK_ENV", default="production")
DB_PATH = os.environ.get("FLASK_DB_PATH", default="")
# if path is not set, use in-memory sqlite db ("sqlite:///")
MAILX = shutil.which("mailx")

APP = sge.create_app(sge.ENV_TO_CONFIG[FLASK_ENV], steam_key=STEAM_KEY, db_path=DB_PATH)
if "uwsgi" in locals():
    uwsgi.register_signal(10, "", sge.cleanup)
    uwsgi.add_cron(10, 0, 1, -1, -1, -1)

    #FIXME: smtp logger WILL fail in event of network errors
    #       or when networking has not yet been initialized on the server
    #       so, for example, right after boot
    LOGGER = logging.getLogger(__name__)
    LOGGER.setLevel(logging.INFO)
    MAIL_HANDLER = logging.handlers.SMTPHandler(
        "localhost", fromaddr="root", toaddrs=["root"],
        subject="[Steam Games Exporter]", secure=None)
    MAIL_HANDLER.setLevel(logging.INFO)
    LOGGER.addHandler(MAIL_HANDLER)
    LOGGER.info("Steam Games Exporter started successfully in uwsgi mode")
    MAIL_HANDLER.setLevel(logging.ERROR)
    sge.LOGGER.addHandler(MAIL_HANDLER)
