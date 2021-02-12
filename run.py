"""This is only for the benefit of uwsgi emperor
"""
import os
import sys

# uwsgi emperor launches the app from within the venv
# so path from which sge can be imported must be added manually
sys.path.append(os.path.realpath(__file__).rsplit("/", maxsplit=1)[0])
from sge import config
from sge.steam_games_exporter import create_app

app = create_app(config)
