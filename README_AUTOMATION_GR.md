# BetAnalytic — αυτόματη λειτουργία χωρίς μόνιμο server

Η Python δεν μένει ανοιχτή σε server. Το GitHub Actions εκτελείται ανά δύο
ώρες, συγχρονίζει τους αγώνες, επανεκπαιδεύει το ίδιο ensemble και ανεβάζει
δύο στατικά αρχεία στο Cloudflare R2:

- `manifest.json`: μικρό αρχείο ελέγχου έκδοσης.
- `feed.json`: επερχόμενοι αγώνες και έτοιμες προβλέψεις.

## Απαράβατος χρονικός κανόνας

Για κάθε εκτέλεση χρησιμοποιούνται μόνο εγγραφές που ικανοποιούν ταυτόχρονα:

```text
status = FT
home_goals και away_goals διαθέσιμα
fixture_date < prediction_calculated_at
```

Ο ίδιος ο αγώνας πρόβλεψης αποκλείεται ρητά. Έτσι ένας αγώνας που τελείωσε
την Πέμπτη θα συμπεριληφθεί στην επόμενη αυτόματη εκτέλεση και θα επηρεάσει
την πρόβλεψη ενός αγώνα της Παρασκευής. Κανένα μελλοντικό αποτέλεσμα δεν
μπορεί να χρησιμοποιηθεί.

## Τοπική δοκιμή χωρίς API και χωρίς upload

```cmd
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe automatic_update.py --skip-sync --skip-upload
```

## GitHub Secrets

Στο repository: `Settings → Secrets and variables → Actions → Secrets`:

```text
API_FOOTBALL_KEY
R2_ACCOUNT_ID
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
```

## GitHub Variables

Στην καρτέλα `Variables`:

```text
R2_BUCKET_NAME = betanalytic-data
R2_PUBLIC_BASE_URL = το δημόσιο URL του bucket
R2_PREFIX = betanalytic
BETANALYTIC_SYNC_SEASONS = auto
BETANALYTIC_INCLUDE_NEXT_SEASON = true
BETANALYTIC_LOOKAHEAD_DAYS = 45
```

Το `BETANALYTIC_SYNC_SEASONS=auto` ζητά από το API-Football την τρέχουσα
σεζόν και δοκιμάζει επίσης την επόμενη. Αν το πλάνο API δεν παρέχει τη
τρέχουσα σεζόν, το feed θα καταγράψει το συγκεκριμένο σφάλμα χωρίς να
χρησιμοποιήσει ψεύτικα δεδομένα.

## Cloudflare R2

1. Δημιουργία bucket, π.χ. `betanalytic-data`.
2. Δημιουργία R2 API token με Object Read & Write μόνο για αυτό το bucket.
3. Ενεργοποίηση Public Development URL για αρχικές δοκιμές ή σύνδεση
   custom domain για κανονική κυκλοφορία.
4. Αντιγραφή των credentials στα GitHub Secrets.

Το API key του API-Football δεν μπαίνει ποτέ στην Android εφαρμογή ούτε στα
JSON αρχεία.

## Αρχεία αυτοματοποίησης

```text
automatic_update.py
static_feed_generator.py
automation_config.py
time_utils.py
r2_storage.py
.github/workflows/update-betanalytic.yml
```

Το υπάρχον FastAPI backend παραμένει στο repository μόνο για τοπικό έλεγχο.
Η Android εφαρμογή θα αλλάξει στο επόμενο στάδιο ώστε να διαβάζει το
`manifest.json` και το `feed.json` αντί για `http://10.0.2.2:8000`.
