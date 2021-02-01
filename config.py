"""Flask app config"""
import os

APPLICATION_ROOT = "/tools/steam-games-exporter/"
MAX_CONTENT_LENGTH = 512*1024
SECRET_KEY = os.urandom(16)
SERVER_NAME = "misc.untextured.space"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = True
STATIC_URL_PATH = "/tools/steam-games-exporter/static"
