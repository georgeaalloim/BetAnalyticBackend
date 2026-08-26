# HISTORY DEDUP V2

Διορθώνει το διπλό PAOK - Levadiakos που εμφανιζόταν ως δύο αγώνες επειδή δύο πηγές χρησιμοποίησαν διαφορετική γραφή/ID για τον Λεβαδειακό (`Levadiakos` / `Levadeiakos`).

Αλλαγές:
- `Levadeiakos` και `Levadiakos` αντιστοιχίζονται στο ίδιο canonical team id 957.
- Η αποδιπλοποίηση ιστορικού και training δεν εμπιστεύεται μόνο provider team IDs· επαναλύει και τα ονόματα ομάδων.
- Το Football-Data reconciliation ταιριάζει υπάρχοντα fixtures με canonical team identity.
- Το history feed επιστρέφει canonical ονόματα/IDs και μία ομάδα/έναν αγώνα.
- Οι νέοι ολοκληρωμένοι αγώνες εξακολουθούν να μπαίνουν μία φορά στο μοντέλο.

Scorers:
- Το feed/app υποστηρίζει `goal_scorers`.
- Οι τρέχουσες δωρεάν πηγές του backend δεν παρέχουν ονόματα σκόρερ για την ενεργή σεζόν, άρα δεν κατασκευάζονται δεδομένα.
