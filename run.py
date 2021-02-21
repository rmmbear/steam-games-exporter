"""This is only for the benefit of uwsgi emperor
"""
import os
import sys
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
from sge.steam_games_exporter import create_app, cleanup

APP = create_app()
if "uwsgi" in locals():
    LOGGER = logging.getLogger(__name__)
    LOGGER.setLevel(logging.INFO)
    MAIL_HANDLER = logging.handlers.SMTPHandler(
        "localhost", fromaddr="root@untextured.space", toaddrs="root@localhost",
        subject="[Steam Games Exporter]", secure=tuple())
    MAIL_HANDLER.setLevel(logging.INFO)
    LOGGER.addHandler(MAIL_HANDLER)

    uwsgi.register_signal(10, "", cleanup)
    uwsgi.add_cron(10, 0, 1, -1, -1, -1)
    LOGGER.info("Steam Games Exporter started")
