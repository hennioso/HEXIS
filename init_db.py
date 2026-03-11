"""
Initialisiert eine leere trades.db Datenbank.
Beim ersten Start von main.py oder web_dashboard.py passiert das automatisch.
Dieses Script kann manuell ausgeführt werden um die DB zurückzusetzen.

Verwendung:
    python init_db.py
"""

import database as db

db.init_db()
print("trades.db wurde erfolgreich erstellt (leere Datenbank).")
