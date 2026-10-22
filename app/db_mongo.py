"""PyMongo client helper. MongoClient is thread-safe, so one client per process."""
from flask import current_app
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.config import Config
from app.query_benchmarks.mongo import CommandListener

_clients = {}
_listener = CommandListener()   # records commands for the query panel


def client(uri=None):
    uri = uri or Config.MONGO_URI
    if uri not in _clients:
        _clients[uri] = MongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True,
                                    event_listeners=[_listener])
    return _clients[uri]


def get_mongo():
    """The app's Mongo database."""
    return client(current_app.config["MONGO_URI"])[current_app.config["MONGO_DB"]]


def optional(fn, *args, default=None, what="data", **kwargs):
    """fn(*args, **kwargs), or `default` if MongoDB is unavailable: pages that mix both stores still render
    their MariaDB parts (the template shows "unavailable" for the rest)."""
    try:
        return fn(*args, **kwargs)
    except PyMongoError:
        current_app.logger.warning("MongoDB unavailable for %s", what, exc_info=True)
        return default
