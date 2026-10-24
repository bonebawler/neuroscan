# ResponSMS

A simulated SMS emergency hospital referral system for Quezon City. A person texts their emergency (for example `STROKE near NIP`), and the system replies with the nearest suitable hospitals ranked by service level, reported capacity, road distance, and data freshness. Messages work in English and Tagalog.

> **Prototype for research and testing only.** Bed counts come from DOH license records and are **not** live availability. In a real emergency, call **911**.

## Requirements

- Python 3.9 or newer
- An internet connection for the first-time data build and for place lookups
- The DOH facility CSV, `QCFacis - Sheet1.csv`

## Project layout

```
.
├── sms_hospital_finder.py    # core logic: data build, ranking, SMS sessions
├── app.py                    # Flask page that looks like a phone
├── test.py                   # test suite
├── README.md
├── QCFacis - Sheet1.csv      # can be any data related to our structure
└── qc_hospital_sms/data/     # created by the build step
```

## 1. Install

Create a virtual environment and install the dependencies.

**Linux / macOS**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Add the data file

Put `QCFacis - Sheet1.csv` in the same folder as `sms_hospital_finder.py`.

## 3. Build the data (one time)

```bash
python sms_hospital_finder.py build
```

This step:

1. Keeps only the hospital rows from the CSV and cleans names and addresses.
2. Downloads Quezon City barangay boundaries.
3. Geocodes every hospital. This takes a few minutes, and the results are cached.
4. Writes `hospitals_clean.csv`, `barangays.csv`, and `qc_barangays.geojson` to `qc_hospital_sms/data/`.

If the CSV is somewhere else, pass its path:

```bash
python sms_hospital_finder.py build "/path/to/QCFacis - Sheet1.csv"
```

To re-geocode from scratch, delete `qc_hospital_sms/data/hospitals_geocoded.csv` and run the build again.

## 4. Run it

### Phone-style web page

```bash
python app.py
```

Open <http://127.0.0.1:5000>. Each browser gets its own conversation. The 📍 button fills in your current coordinates, and every reply shows its processing time.

### Terminal chat

```bash
python sms_hospital_finder.py
```

Press `Ctrl+C` to quit.

## 5. Using it

Send any of these:

| You send | What happens |
|---|---|
| `help` | Shows the menu |
| `1` to `18` | Picks an emergency or non-emergency from the menu, then asks for your place |
| `STROKE near NIP` | Detects the emergency and the place in one message |
| `nip` then `stroke` | Saves the place, then asks for the emergency |
| `buntis po ako, Pinyahan` | Tagalog works too |
| `14.6760,121.0437` | Coordinates work as a place |
| `reset` | Starts a new conversation |

Replies include up to three hospitals with level, road distance, estimated minutes, and a capacity label:

| Label | Meaning |
|---|---|
| `cap reported` | Bed count from a valid license |
| `cap OUTDATED` | The license has expired |
| `cap UNKNOWN` | No bed data |
| `cap SIMULATED` | Simulated data, never presented as verified |

A second message follows with map links.

## 6. Run the tests

```bash
python test.py
```

This runs offline against a small built-in fixture, so it needs no data files and no internet.

```bash
python test.py --live
```

This also tests Photon geocoding and your real data. Live tests are skipped if the network or the built data is missing.

## Optional settings

### Google Maps key

Without a key, place lookups use Photon and then Nominatim, which are free. To use Google Places first, create a file named `.env` next to the script:

```
GOOGLE_MAPS_API_KEY=your_key_here
```

You can also set it as an environment variable instead.

### Data folder

By default the data lives in `qc_hospital_sms/` next to the script. To change it:

```bash
export QC_SMS_ROOT=/some/other/folder     # Windows: set QC_SMS_ROOT=C:\some\folder
```

### Real SMS with Twilio

The web page is a simulation. The app also exposes a `/sms` route that speaks Twilio's webhook format.

1. Get a Twilio number. Trial accounts can only text verified numbers.
2. Make your local server reachable, for example `ngrok http 5000`.
3. In the Twilio console, set the number's "A message comes in" webhook to `https://<your-public-url>/sms`.

Real texts are billed per segment, and replies longer than 160 characters are split into several segments.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not find the DOH facility CSV` | Put `QCFacis - Sheet1.csv` next to the script, or pass its path to `build` |
| `No data in ...` when starting `app.py` | Run `python sms_hospital_finder.py build` first |
| `ModuleNotFoundError` | Activate your virtual environment, then run `pip install -r requirements.txt` |
| Place lookups return nothing | Check your connection. Barangay names and hospital names are matched locally without internet |
| Distances look larger than expected | If the routing service is unreachable, road distance is estimated as straight-line distance × 1.35 |
| Port 5000 already in use | Change the port in the last line of `app.py`, for example `app.run(debug=True, port=5001)` |
