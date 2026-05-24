import os
from datetime import datetime

try:
    from pymongo import MongoClient
except ImportError:  # Optional until Mongo variables are configured in production.
    MongoClient = None


_client = None


def get_mongo_database():
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    if not uri or MongoClient is None:
        return None

    global _client
    if _client is None:
        _client = MongoClient(uri, serverSelectionTimeoutMS=3000)

    db_name = os.getenv("MONGO_DB_NAME", "betmundial")
    return _client[db_name]


def mirror_document(collection, document, key_fields=None):
    db = get_mongo_database()
    if db is None:
        return False

    payload = {**document, "synced_at": datetime.utcnow()}
    key_fields = key_fields or ["id"]
    query = {field: payload.get(field) for field in key_fields if payload.get(field) is not None}

    if query:
        db[collection].update_one(query, {"$set": payload}, upsert=True)
    else:
        db[collection].insert_one(payload)
    return True
