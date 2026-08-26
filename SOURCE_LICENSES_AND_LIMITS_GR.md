# Πηγές, δωρεάν χρήση και περιορισμοί

## OpenFootball

- Repository: `https://github.com/openfootball/europe`
- Άδεια: CC0-1.0 / public domain.
- Η ίδια η σελίδα δηλώνει ότι τα δεδομένα μπορούν να χρησιμοποιηθούν χωρίς περιορισμούς.
- Καλύπτει τη Greece Super League, αλλά η νέα σεζόν μπορεί να δημοσιευτεί με καθυστέρηση.

## API-Football Free — προαιρετικό

- Pricing: `https://www.api-football.com/pricing`
- Δωρεάν plan: $0, 100 requests/ημέρα.
- Χρειάζεται δωρεάν λογαριασμός και secret key.
- Στο BetAnalytic χρησιμοποιείται μόνο ως επιπλέον πηγή διασταύρωσης και όχι ως μοναδική αλήθεια.
- Το key δεν αποθηκεύεται στον κώδικα ή στο ZIP· μπαίνει μόνο στα GitHub Secrets.

## Fixtur.es

- Σελίδα calendar: `https://fixtur.es/en/matches/super-league-greece`
- Η υπηρεσία δηλώνει δωρεάν calendar feed, χωρίς εγγραφή, με αυτόματες ενημερώσεις ώρας και αποτελέσματος.
- Δεν βρέθηκε ρητή CC0 ή αντίστοιχη άδεια αναδημοσίευσης δεδομένων σε εφαρμογή.
- Η χρήση στον κώδικα γίνεται μέσω calendar feed και όχι μέσω του site της Super League.

## Football-Data.co.uk

- Greece data: `https://www.football-data.co.uk/greecem.php`
- Τα CSV παρέχονται δωρεάν και προορίζονται για ανάλυση σε spreadsheet/συστήματα αξιολόγησης.
- Η σελίδα αναφέρει ότι τα δεδομένα ενημερώνονται περιοδικά και ότι δεν εγγυάται την ορθότητά τους.
- Η αρχική σελίδα αναφέρει «All Rights Reserved». Δεν βρέθηκε ρητή CC0 άδεια για αναδημοσίευση σε app store.

## TheSportsDB API v1 — αυτόματοι σκόρερ

- Documentation: `https://www.thesportsdb.com/documentation`
- Η τρέχουσα τεκμηρίωση δηλώνει free API, current free v1 key `123` και όριο 30 requests/λεπτό για free users.
- Το BetAnalytic χρησιμοποιεί μόνο `searchevents.php` και `lookuptimeline.php` για ολοκληρωμένους αγώνες με ελλείποντες σκόρερ.
- Ο collector περιορίζεται σε 8 αγώνες ανά run (έως 24 requests στο χειρότερο σενάριο) και απορρίπτει ελλιπή/ασύμφωνα timelines.
- Μπορεί να δοθεί `THESPORTSDB_KEY` ως GitHub Secret για άλλο/premium key.
- Πριν από δημόσια ή εμπορική κυκλοφορία πρέπει να επιβεβαιωθούν οι τρέχοντες όροι χρήσης του provider και, όπου απαιτείται, να χρησιμοποιηθεί το κατάλληλο πλάνο/key.

## Super League official website

- Δεν γίνεται κανένα request στο `slgr.gr`.
- Δεν υπάρχει scraper ή κρυφή εναλλακτική ενεργοποίησης.
- Δεν απαιτείται ούτε υποτίθεται άδεια από τη Super League για τον παρόντα κώδικα, επειδή δεν αντλεί δεδομένα από το site της.

## Συμπέρασμα

Το repository δεν απαιτεί πληρωμή. Η μόνη πηγή με απολύτως σαφή άδεια public-domain είναι το OpenFootball. Για εμπορική δημοσίευση, χρειάζεται ξεχωριστός έλεγχος των όρων Fixtur.es και Football-Data ή αντικατάστασή τους με πάροχο που δίνει ρητή άδεια διανομής.
