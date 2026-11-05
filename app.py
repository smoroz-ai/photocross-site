import os
import uuid
import sqlite3
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, flash, send_file
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "photocross-secret-key-change-me")

YADISK_TOKEN = os.environ.get("YADISK_TOKEN", "")
YADISK_FOLDER = "/photocross"
DB_PATH = "photocross.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team TEXT NOT NULL,
                task_num INTEGER NOT NULL,
                yadisk_path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
        """)


def upload_to_yadisk(file_obj, filename):
    """Загружает файл на Яндекс.Диск. Возвращает путь на Диске или None."""
    if not YADISK_TOKEN:
        return None

    path = f"{YADISK_FOLDER}/{filename}"

    resp = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers={"Authorization": f"OAuth {YADISK_TOKEN}"},
        params={"path": path, "overwrite": "true"},
        timeout=30,
    )
    if resp.status_code != 200:
        app.logger.error(f"Yandex get upload url failed: {resp.status_code} {resp.text}")
        return None

    upload_url = resp.json().get("href")
    if not upload_url:
        return None

    put_resp = requests.put(upload_url, data=file_obj, timeout=120)
    if put_resp.status_code not in (200, 201, 202):
        app.logger.error(f"Yandex upload failed: {put_resp.status_code}")
        return None

    return path


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        team = request.form.get("team", "").strip()
        task_num_raw = request.form.get("task_num", "").strip()
        photo = request.files.get("photo")

        if not team:
            flash("Введите название команды", "error")
            return redirect(url_for("index"))

        try:
            task_num = int(task_num_raw)
            if not (1 <= task_num <= 26):
                raise ValueError
        except ValueError:
            flash("Номер задания — число от 1 до 26", "error")
            return redirect(url_for("index"))

        if not photo or not photo.filename:
            flash("Выберите фото", "error")
            return redirect(url_for("index"))

        ext = os.path.splitext(photo.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
            ext = ".jpg"

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_team = "".join(c for c in team if c.isalnum() or c in " _-")[:40].replace(" ", "_")
        filename = f"{safe_team}_zad{task_num}_{timestamp}_{uuid.uuid4().hex[:6]}{ext}"

        yadisk_path = upload_to_yadisk(photo, filename)
        if not yadisk_path:
            flash("Не удалось загрузить фото на Яндекс.Диск. Попробуйте ещё раз.", "error")
            return redirect(url_for("index"))

        with get_db() as conn:
            conn.execute(
                "INSERT INTO photos (team, task_num, yadisk_path, uploaded_at) VALUES (?, ?, ?, ?)",
                (team, task_num, yadisk_path, datetime.utcnow().isoformat()),
            )

        flash(f"Фото по заданию №{task_num} для команды «{team}» загружено!", "ok")
        return redirect(url_for("index"))

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM photos ORDER BY uploaded_at DESC").fetchall()
    return render_template("index.html", photos=rows)


@app.route("/export")
def export():
    with get_db() as conn:
        rows = conn.execute("SELECT team, task_num, yadisk_path, uploaded_at FROM photos ORDER BY team, task_num").fetchall()

    lines = ["team;task_num;yadisk_path;uploaded_at"]
    for r in rows:
        lines.append(f"{r['team']};{r['task_num']};{r['yadisk_path']};{r['uploaded_at']}")

    data = "\n".join(lines).encode("utf-8")
    return send_file(
        __import__("io").BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"photocross_export_{datetime.utcnow():%Y%m%d_%H%M}.csv",
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
