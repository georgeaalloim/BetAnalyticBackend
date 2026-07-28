# Κανόνας εμφάνισης ώρας στην Android εφαρμογή

Η εφαρμογή πρέπει να ελέγχει πάντα:

```kotlin
if (fixture.kickoffTimeConfirmed) {
    // Εμφάνισε την ώρα μετατρεμμένη σε Europe/Athens.
} else {
    // Εμφάνισε: «Ώρα προς επιβεβαίωση».
}
```

Μην εμφανίζεις το time component του `fixture_date` όταν:

```json
"kickoff_time_confirmed": false
```

Σε αυτή την περίπτωση η ώρα μέσα στο JSON είναι μόνο τεχνικό σημείο ταξινόμησης και όχι πραγματική ώρα έναρξης.
