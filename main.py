"""
Chat de Clientes - Servidor principal
--------------------------------------
Este archivo levanta un servidor web que permite:
  - Que tus clientes entren a un link y chateen con vos.
  - Que vos veas todas las conversaciones y respondas desde un panel.
  - Que todo quede guardado en un archivo de base de datos (chat.db).

No hace falta que entiendas el código. Solo seguí las instrucciones
del archivo INSTRUCCIONES.txt para instalarlo y correrlo.
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "chat.db"

app = FastAPI(title="Chat de Clientes")


# ---------------------------------------------------------------------
# Base de datos: guarda conversaciones y mensajes para siempre
# ---------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversaciones (
            id TEXT PRIMARY KEY,
            nombre_cliente TEXT DEFAULT 'Cliente',
            creado_en TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversacion_id TEXT NOT NULL,
            remitente TEXT NOT NULL,  -- 'cliente' o 'admin'
            texto TEXT NOT NULL,
            enviado_en TEXT NOT NULL,
            FOREIGN KEY (conversacion_id) REFERENCES conversaciones (id)
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def crear_conversacion_si_no_existe(session_id: str):
    conn = get_db()
    fila = conn.execute(
        "SELECT id FROM conversaciones WHERE id = ?", (session_id,)
    ).fetchone()
    if fila is None:
        conn.execute(
            "INSERT INTO conversaciones (id, creado_en) VALUES (?, ?)",
            (session_id, datetime.now().isoformat()),
        )
        conn.commit()
    conn.close()


def guardar_mensaje(session_id: str, remitente: str, texto: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO mensajes (conversacion_id, remitente, texto, enviado_en) VALUES (?, ?, ?, ?)",
        (session_id, remitente, texto, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def obtener_mensajes(session_id: str):
    conn = get_db()
    filas = conn.execute(
        "SELECT remitente, texto, enviado_en FROM mensajes WHERE conversacion_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(f) for f in filas]


def obtener_conversaciones():
    conn = get_db()
    filas = conn.execute(
        "SELECT id, nombre_cliente, creado_en FROM conversaciones ORDER BY creado_en DESC"
    ).fetchall()
    conn.close()
    return [dict(f) for f in filas]


def poner_nombre_cliente(session_id: str, nombre: str):
    conn = get_db()
    conn.execute(
        "UPDATE conversaciones SET nombre_cliente = ? WHERE id = ?", (nombre, session_id)
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Conexiones en vivo (quién está conectado ahora mismo)
# ---------------------------------------------------------------------
class GestorConexiones:
    def __init__(self):
        self.clientes: dict[str, WebSocket] = {}   # session_id -> websocket
        self.admins: list[WebSocket] = []           # todos los paneles de admin abiertos

    async def conectar_cliente(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self.clientes[session_id] = ws

    def desconectar_cliente(self, session_id: str):
        self.clientes.pop(session_id, None)

    async def conectar_admin(self, ws: WebSocket):
        await ws.accept()
        self.admins.append(ws)

    def desconectar_admin(self, ws: WebSocket):
        if ws in self.admins:
            self.admins.remove(ws)

    async def enviar_a_cliente(self, session_id: str, mensaje: dict):
        ws = self.clientes.get(session_id)
        if ws is not None:
            await ws.send_json(mensaje)

    async def avisar_a_admins(self, mensaje: dict):
        for ws in list(self.admins):
            try:
                await ws.send_json(mensaje)
            except Exception:
                self.desconectar_admin(ws)


gestor = GestorConexiones()


# ---------------------------------------------------------------------
# Páginas web
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def pagina_cliente():
    return FileResponse(APP_DIR / "static" / "cliente.html")


@app.get("/panel", response_class=HTMLResponse)
async def pagina_admin():
    return FileResponse(APP_DIR / "static" / "admin.html")


# ---------------------------------------------------------------------
# WebSocket del cliente (la persona que escribe desde el link público)
# ---------------------------------------------------------------------
@app.websocket("/ws/cliente/{session_id}")
async def ws_cliente(websocket: WebSocket, session_id: str):
    crear_conversacion_si_no_existe(session_id)
    await gestor.conectar_cliente(session_id, websocket)

    # Al conectar, le mandamos el historial previo (si volvió a entrar)
    historial = obtener_mensajes(session_id)
    await websocket.send_json({"tipo": "historial", "mensajes": historial})

    try:
        while True:
            data = await websocket.receive_json()
            texto = data.get("texto", "").strip()
            nombre = data.get("nombre")

            if nombre:
                poner_nombre_cliente(session_id, nombre)

            if texto:
                guardar_mensaje(session_id, "cliente", texto)
                await gestor.avisar_a_admins(
                    {
                        "tipo": "mensaje_nuevo",
                        "session_id": session_id,
                        "remitente": "cliente",
                        "texto": texto,
                    }
                )
    except WebSocketDisconnect:
        gestor.desconectar_cliente(session_id)


# ---------------------------------------------------------------------
# WebSocket del admin (vos, desde /panel)
# ---------------------------------------------------------------------
@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    await gestor.conectar_admin(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            session_id = data.get("session_id")
            texto = data.get("texto", "").strip()
            if session_id and texto:
                guardar_mensaje(session_id, "admin", texto)
                await gestor.enviar_a_cliente(
                    session_id, {"tipo": "mensaje_nuevo", "remitente": "admin", "texto": texto}
                )
    except WebSocketDisconnect:
        gestor.desconectar_admin(websocket)


# ---------------------------------------------------------------------
# API simple para que el panel liste conversaciones y su historial
# ---------------------------------------------------------------------
@app.get("/api/conversaciones")
async def api_conversaciones():
    return obtener_conversaciones()


@app.get("/api/conversaciones/{session_id}/mensajes")
async def api_mensajes(session_id: str):
    return obtener_mensajes(session_id)


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
