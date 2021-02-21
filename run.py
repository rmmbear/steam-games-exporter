"""This is only for the benefit of uwsgi emperor
"""
import os
import sys

try:
    # https://uwsgi-docs.readthedocs.io/en/latest/PythonModule.html
    import uwsgi
except ImportError:
    # we're not running in uwsgi mode
    pass

# uwsgi emperor launches the app from within the venv
# so path from which sge can be imported must be added manually
sys.path.append(os.path.realpath(__file__).rsplit("/", maxsplit=1)[0])
from sge.steam_games_exporter import create_app

APP = create_app()
if "uwsgi" in locals():
    uwsgi.register_signal(10, "", SGE.cleanup)
    uwsgi.add_cron(10, 0, 1, -1, -1, -1)
