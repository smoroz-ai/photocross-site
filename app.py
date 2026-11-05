import os
import re
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
        # Таблица для фото
        conn.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team TEXT NOT NULL,
                task_num INTEGER NOT NULL,
                yadisk_path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
        """)
        # Новая таблица для команд (чтобы присваивать им номера)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)


init_db()


def sanitize_name(name):
    """Очищает название команды от запрещенных символов для файловой системы"""
    clean = re.sub(r'[^a-zA-Zа-яА-Я0-9\s\-_]', '', name)
    clean = " ".join(clean.split())
    return clean.replace(" ", "_")[:50]


def get_team_id(team_name):
    """Возвращает номер команды. Если команды нет в базе, создает её и выдает новый номер."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM teams WHERE name = ?", (team_name,)).fetchone()
        if row:
            return row["id"]
        
        # Если команды нет — добавляем
        cur = conn.execute("INSERT INTO teams (name) VALUES (?)", (team_name,))
        return cur.lastrowid


def upload_to_yadisk(file_obj, team_name, task_num):
    if not YADISK_TOKEN:
        return None

    # Получаем номер команды (01, 02, 03...)
    team_id = get_team_id(team_name)
    formatted_team = f"{team_id:02d}"

    safe_team = sanitize_name(team_name)
    if not safe_team:
        safe_team = "Team_" + uuid.uuid4().hex[:6]

    # Папка всё еще называется по имени, чтобы людям было понятно
    folder_path = f"{YADISK_FOLDER}/{safe_team}"
    
    ext = os.path.splitext(file_obj.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
        ext = ".jpg"
    
    # Форматируем номер задания (01, 02...)
    formatted_task = f"{task_num:02d}"
    
    # Имя файла: "0101_Фотоны_удачи.jpg" (Номер команды + номер задания)
    filename = f"{formatted_team}{formatted_task}_{safe_team}{ext}"
    file_path = f"{folder_path}/{filename}"

    # Создаем папку команды (если её нет)
    requests.put(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers={"Authorization": f"OAuth {YADISK_TOKEN}"},
        params={"path": folder_path},
        timeout=30,
    )

    # Получаем ссылку для загрузки файла (overwrite=true перезапишет старый файл)
    resp = requests.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers={"Authorization": f"OAuth {YADISK_TOKEN}"},
        params={"path": file_path, "overwrite": "true"},
        timeout=30,
    )
    if resp.status_code != 200:
        app.logger.error(f"Yandex get upload url failed: {resp.status_code} {resp.text}")
        return None

    upload_url = resp.json().get("href")
    if not upload_url:
        return None

    # Отправляем файл на Диск
    put_resp = requests.put(upload_url, data=file_obj, timeout=120)
    if put_resp.status_code not in (200, 201, 202):
        app.logger.error(f"Yandex upload failed: {put_resp.status_code}")
        return None

    return file_path


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

        yadisk_path = upload_to_yadisk(photo, team, task_num)
        if not yadisk_path:
            flash("Не удалось загрузить фото на Яндекс.Диск. Проверьте токен или попробуйте ещё раз.", "error")
            return redirect(url_for("index"))

        with get_db() as conn:
            # Удаляем старую запись для этого же задания этой же команды, чтобы в базе не было дублей
            conn.execute(
                "DELETE FROM photos WHERE team = ? AND task_num = ?",
                (team, task_num)
            )
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
