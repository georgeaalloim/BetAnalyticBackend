# Σωστή εγκατάσταση BetAnalytic — δωρεάν έκδοση

## Αρχεία που εκτελούνται αυτόματα

- `.github/workflows/update-betanalytic.yml`: πρόγραμμα, διασταύρωση, προβλέψεις και GitHub Pages ανά δύο ώρες.
- `.github/workflows/collect-match-statistics.yml`: καθημερινός συγχρονισμός ιστορικών αποτελεσμάτων, κόρνερ και καρτών.
- `automatic_update.py`: κεντρική αυτόματη ροή.
- `free_schedule_source.py`: κανόνας διασταύρωσης ημερομηνίας και ώρας.
- `openfootball_source.py`: OpenFootball CC0.
- `api_football_free_source.py`: προαιρετική δωρεάν επιβεβαίωση.
- `football_data_source.py`: δωρεάν CSV αποτελεσμάτων και στατιστικών.
- `fixtur_es_source.py`: calendar feed.

## Εγκατάσταση

1. Ανέβασε όλα τα αρχεία του ZIP στο root του GitHub repository.
2. Κράτησε ενεργό το GitHub Pages με source `GitHub Actions`.
3. Δεν απαιτείται Secret.
4. Προαιρετικά πρόσθεσε δωρεάν `API_FOOTBALL_KEY` στα GitHub Actions Secrets.
5. Τρέξε πρώτα `Sync free match history`.
6. Τρέξε μετά `Update and publish BetAnalytic data`.

Η ώρα εμφανίζεται μόνο μετά από συμφωνία δύο πηγών. Αν δεν υπάρχει συμφωνία, το feed δηλώνει `kickoff_time_confirmed=false`.
