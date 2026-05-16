from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import logging
from logging.handlers import RotatingFileHandler

from config import num_to_plate, roomid_to_label

log = logging.getLogger("ztmy-fc")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = RotatingFileHandler("flask.log", maxBytes=5_000_000, backupCount=3)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.DEBUG)
_console_handler.setFormatter(_fmt)

log.addHandler(_file_handler)
log.addHandler(_console_handler)

logging.getLogger("werkzeug").addHandler(_file_handler)

REFRESH_TOKEN = os.environ["CSCGO_REFRESH_TOKEN"]
CLIENT_ID     = os.environ["CSCGO_CLIENT_ID"]
AUTH0_CLIENT  = os.environ["CSCGO_AUTH0_CLIENT"]
ACC           = os.environ["CSCGO_ACCOUNT_ID"]
KEYWORD       = os.environ["CSCGO_KEYWORD"]

AUTH_ENDPOINT          = "https://auth.mycscgo.com/oauth/token"
API_BASE_URL           = "https://mycscgo.com"
TOKEN_REFRESH_INTERVAL = timedelta(hours=6)

REFRESH_HEADERS = {
    "content-type": "application/json",
    "auth0-client": AUTH0_CLIENT,
}

REFRESH_PAYLOAD = {
    "refresh_token": REFRESH_TOKEN,
    "scope": "openid profile offline_access email",
    "client_id": CLIENT_ID,
    "grant_type": "refresh_token",
}

_API_HEADERS_BASE = {
    "accept": "*/*",
    "content-type": "application/json",
    "x-bai-correlation-id": "C683AC9F-4645-48F2-9314-B8D35CC62B6E",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "CSC Go/1.17.2/2023102401 (iOS; 17.2.1; iPhone11,8)",
}

http      = requests.Session()
auth_http = requests.Session()

http.headers.update(_API_HEADERS_BASE)

app = Flask(__name__)
CORS(app)
app.logger.addHandler(_file_handler)
app.logger.setLevel(logging.INFO)


TOKEN_STATE = {
    "id_token": None,
    "expires_at": datetime.min,
}

def refresh_auth_token() -> bool:
    """
    Refresh token if missing or older than TOKEN_REFRESH_INTERVAL.
    Returns True on success
    """
    global TOKEN_STATE

    age = datetime.now() - TOKEN_STATE["expires_at"]
    if TOKEN_STATE["id_token"] and age < TOKEN_REFRESH_INTERVAL:
        return True  # still fresh

    log.info("Auth token expired or missing  refreshing...")
    try:
        resp = auth_http.post(AUTH_ENDPOINT, headers=REFRESH_HEADERS, json=REFRESH_PAYLOAD)
        resp.raise_for_status()
        new_tokens = resp.json()

        if "id_token" not in new_tokens:
            log.error("Auth refresh response missing 'id_token': %s", new_tokens)
            return False

        TOKEN_STATE["id_token"] = new_tokens["id_token"]
        TOKEN_STATE["expires_at"] = datetime.now()
        log.info("Auth token refreshed successfully.")
        return True

    except requests.exceptions.HTTPError as e:
        log.error("Auth refresh HTTP %s error: %s", e.response.status_code, e)
    except requests.exceptions.RequestException as e:
        log.error("Auth refresh network error: %s", e)
    except Exception:
        log.exception("Unexpected error during auth refresh")

    return False


def _api_headers() -> dict:
    """Inject the current bearer token into the per-request headers."""
    return {"authorization": f"Bearer {TOKEN_STATE['id_token']}"}

def deposit_rpc(amount: int) -> dict:
    log.info("Depositing %d cents...", amount)
    resp = http.post(
        API_BASE_URL + "/api/v3/account/wallet/deposits",
        headers=_api_headers(),
        json={
            "amount": amount,
            "paymentSuccessful": True,
            "currency": "usd",
            "paymentId": "string",
            "method": "stored-value",
            "paymentProcessor": "STRIPE",
        },
    )
    resp.raise_for_status()
    log.info("Deposit of %d cents accepted.", amount)
    return resp.json()

def activate_rpc(plate: str, amount: int) -> dict:
    log.info("Activating machine %s...", plate)
    resp = http.post(
        API_BASE_URL + "/api/v3/commands/startMachineWithStoredValue",
        headers=_api_headers(),
        json={
            "currency": "usd",
            "additionalBlocks": 0,
            "licensePlate": str(plate),
            "subject": ACC,
            "amount": amount,
        },
    )
    resp.raise_for_status()
    log.info("Machine %s activated successfully.", plate)
    return resp.json()

def activate(plate: str, amount: int) -> bool:
    try:
        activate_rpc(plate, amount)
        return True
    except requests.exceptions.HTTPError as e:
        log.error("Activate HTTP %s: %s", e.response.status_code, e)
        return False
    except requests.exceptions.RequestException as e:
        log.error("Activate network error: %s", e)
        return False

def deposit(amount: int) -> bool:
    try:
        deposit_rpc(amount)
        return True
    except requests.exceptions.HTTPError as e:
        log.error("Deposit HTTP %s: %s", e.response.status_code, e)
    except requests.exceptions.RequestException as e: 
        log.error("Deposit network error: %s", e)
    return False

def start_machine(plate: str, amount: int = 125):
    """
    Deposit money then activate machine. (reordered to make more responsive) Returns (response_dict, http_status_code).
    """

    if activate(plate, amount) and deposit(amount):
        return {
            "success": True,
            "message": f"activated machine: {plate}",
        }, 200

    return {"success": False, "message": "failure, contact maple"}

_ET = ZoneInfo("America/New_York")

def parse_utc(utc: str) -> datetime:
    return datetime.fromisoformat(utc).replace(tzinfo=ZoneInfo("UTC"))

def is_more_than_5_hours_ago(utc: str) -> bool:
    now = datetime.now(tz=ZoneInfo("UTC"))
    return now - parse_utc(utc) > timedelta(hours=5)

@app.route("/", methods=["GET"])
def serve_homepage():
    return app.send_static_file("index.html")

@app.route("/ls", methods=["GET"])
def list_laundry():
    """List machines currently associated with the account."""
    if not TOKEN_STATE["id_token"]:
        log.warning("/ls called with no valid auth token")
        return jsonify({"success": False, "error": "Authentication token is missing."}), 401

    try:
        resp = http.get(
            API_BASE_URL + "/api/v3/account/laundry",
            headers=_api_headers(),
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("Failed to fetch laundry list: %s", e)
        return jsonify({"success": False, "error": str(e)}), 502

    machines = []
    for machine in resp.json():
        started_at = machine.get("startedAt")
        room_id = machine.get("roomId")
        if not started_at:
            log.warning("Machine %s has no startedAt, skipping", machine.get("stickerNumber"))
            continue
        if not is_more_than_5_hours_ago(started_at) or machine["timeRemaining"]:
            machines.append({
                "stickerNumber": machine["stickerNumber"],
                "timeRemaining": machine["timeRemaining"],
                "type": machine["type"],
                "startedAt": parse_utc(started_at).astimezone(_ET).strftime("%m/%d %H:%M:%S"),
                "label": roomid_to_label.get(room_id, room_id),
            })
    return jsonify(machines)


@app.route("/start-machine", methods=["POST"])
def start_machine_endpoint():
    """Trigger a machine start by sticker number."""
    try:
        data = request.get_json(force=True)

        if data.get("keyword") != KEYWORD:
            log.warning("Rejected /start-machine  bad keyword")
            return jsonify({"error": "Access Denied"}), 403

        number = data.get("plate")
        if number is None:
            return jsonify({"error": "Missing 'plate' parameter."}), 400

        plate = num_to_plate.get(int(number))
        if not plate:
            log.warning("Unknown plate number: %s", number)
            return jsonify({"error": f"Unknown plate number: {number}"}), 400

        if not refresh_auth_token():
            return jsonify({"error": "Failed to authenticate. Check server logs."}), 500

        result, status_code = start_machine(plate)
        return jsonify(result), status_code

    except Exception:
        log.exception("Unhandled error in /start-machine")
        return jsonify({"error": "Internal Server Error"}), 500

with app.app_context():
    refresh_auth_token()

if __name__ == "__main__":
    app.run(
        debug=False,
        host="zutomayo-fanclub.mit.edu",
        port=443,
        ssl_context=("ssl/zutomayo-fanclub.mit.edu.crt", "ssl/zutomayo-fanclub.mit.edu.key"),
    )

