"""
channel_assignments jadvalini tozalab qayta taqsimlaydi.
Bot TO'XTATILGAN holda ishlatilsin.
"""
import sqlite3, os, sys

db_path = "cyber_station.db"
if not os.path.exists(db_path):
    print(f"XATO: {db_path} topilmadi. Bot papkasida ishlatilsin.")
    sys.exit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Barcha kanallarni ol
cur.execute("SELECT COUNT(*) FROM channel_assignments")
total = cur.fetchone()[0]
print(f"Jami biriktirilgan kanallar: {total}")

# Tozala
cur.execute("DELETE FROM channel_assignments")
conn.commit()
print("channel_assignments tozalandi. Bot ishga tushganda qayta teng taqsimlaydi.")
conn.close()
print("Tayyor! Botni ishga tushiring.")
