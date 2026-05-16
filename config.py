from concurrent.futures import ThreadPoolExecutor
import requests

# handle CSC's hella chopped typos
def sanitize_label(label: str) -> str:
    label = label.split('-', 1)[0].rstrip() if '-' in label else label
    if "BURTON" in label:
        return "BURTON CONNER HOUSE"
    elif label == "EAST CAMPUS  BLDG 62":
        return "EAST CAMPUS  BLDG 62"
    elif "PI BETA PHI" in label:
        return "PI BETA PHI"
    return label

def create_laundry_mappings() -> dict:
    num_to_plate = dict()
    roomid_to_label = dict()
    s = requests.Session()

    MIT_ID = "d4478fbf-bead-444f-9d24-708d3a405d43"
    base = "https://mycscgo.com/api/v3/location/" + MIT_ID
    list_machines_api = lambda x: base + f"/room/{x}/machines"
    location = s.get(base).json()
    rooms = location["rooms"]

    for room in rooms:
        roomid, label = room["roomId"], room["label"]
        label = sanitize_label(label)
        roomid_to_label[roomid] = label
        machines = s.get(list_machines_api(roomid)).json()
        for machine in machines:
            plate, num = machine["licensePlate"], machine["stickerNumber"]
            num_to_plate[num] = plate

    return num_to_plate, roomid_to_label

