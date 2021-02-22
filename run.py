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

FLASK_ENV = os.environ.get("FLASK_ENV", default="production")
MAILX = shutil.which("mailx")

APP = sge.create_app(sge.ENV_TO_CONFIG[FLASK_ENV])
if "uwsgi" in locals():
    uwsgi.register_signal(10, "", sge.cleanup)
    uwsgi.add_cron(10, 0, 1, -1, -1, -1)

    #FIXME: smtp logger WILL fail in event of network errors
    #       or when networking has not yet been initialized on the server
    #       so, for example, right after boot
    LOGGER = logging.getLogger(__name__)
    LOGGER.setLevel(logging.INFO)
    MAIL_HANDLER = logging.handlers.SMTPHandler(
        "localhost", fromaddr="root", toaddrs="root",
        subject="[Steam Games Exporter]", secure=tuple())
    MAIL_HANDLER.setLevel(logging.INFO)
    LOGGER.addHandler(MAIL_HANDLER)
    LOGGER.info("Steam Games Exporter started successfully in uwsgi mode")
    MAIL_HANDLER.setLevel(logging.ERROR)
    sge.LOGGER.addHandler(MAIL_HANDLER)
