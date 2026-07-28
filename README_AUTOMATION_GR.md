# Αυτόματη ροή BetAnalytic

```text
Fixtur.es calendar
       +
OpenFootball CC0
       +
Football-Data CSV
       +
API-Football Free (προαιρετικό)
       ↓
διασταύρωση ομάδων / ημερομηνίας / ώρας
       ↓
SQLite + fixture_statistics.json
       ↓
μοντέλα πρόβλεψης με χρονικό cutoff
       ↓
feed.json + manifest.json
       ↓
GitHub Pages
       ↓
Android εφαρμογή
```

- Κύριο workflow: ανά δύο ώρες.
- Ιστορικό workflow: καθημερινά.
- Χωρίς Super League scraping.
- Χωρίς υποχρεωτικό πληρωμένο API.
- Αν οι πηγές διαφωνούν, η ώρα δεν επιβεβαιώνεται.
