# Διόρθωση SUSPENDED / POSTPONED fixtures

Η πηγή Fixtur.es μπορεί να επιστρέψει κατάσταση αγώνα μέσα στο SUMMARY, π.χ.:

`⚠️ SUSPENDED: Panathinaikos - Kifisia`

Πριν τη διόρθωση ο parser μπορούσε να θεωρήσει το `⚠️ SUSPENDED:` μέρος του ονόματος της γηπεδούχου ομάδας.

Η νέα πολιτική είναι:

1. Τα status labels αφαιρούνται από το κείμενο πριν γίνει αναγνώριση των ομάδων.
2. `SUSPENDED`, `POSTPONED`, `CANCELLED`, `ABANDONED`, `INTERRUPTED` και αντίστοιχα ελληνικά markers χαρτογραφούνται σε `PST` για το εσωτερικό schedule.
3. Το `Panathinaikos` επιλύεται ξανά στο canonical team id `617` και η `Kifisia` στο `5050`.
4. Το prediction feed δέχεται μόνο `NS` και `TBD`. `PST` δεν μπορεί να πάρει πρόβλεψη ακόμη και αν προστεθεί κατά λάθος στις μεταβλητές περιβάλλοντος.
5. Στο επόμενο schedule sync, τα synthetic fixtures της ενεργής σεζόν αντικαθίστανται, άρα η παλιά κακοσχηματισμένη εγγραφή καθαρίζεται αυτόματα.

Δεν απαιτείται αλλαγή Android για αυτή τη διόρθωση.
