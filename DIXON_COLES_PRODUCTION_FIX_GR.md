# Dixon–Coles production fix

Η παραγωγική πρόβλεψη 1Χ2 ενεργοποιεί πλέον την τιμή `rho = -0.10`, η οποία είχε ήδη επιλεγεί από το υπάρχον tuning (`dixon_coles_tuning_results.json`).

Η διόρθωση εφαρμόζεται πλέον και στα δύο σκέλη του ensemble:

- 60% Bayesian-smoothed Poisson
- 40% Poisson MLE

Έτσι τα χαμηλά σκορ `0-0`, `1-0`, `0-1`, `1-1` δεν υπολογίζονται πλέον ως δύο πλήρως ανεξάρτητες Poisson κατανομές. Η αρνητική τιμή rho αυξάνει κυρίως τις πιθανότητες `0-0` και `1-1`, άρα αντιμετωπίζει το προηγούμενο systematic underprediction των ισοπαλιών.

Δεν υπάρχει τεχνητό bonus στο Χ και δεν αλλάζει το `predicted_result = max(HOME, DRAW, AWAY)`. Αλλάζει η ίδια η πιθανότητα του score grid μέσω Dixon–Coles.
