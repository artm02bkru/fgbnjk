"""RTSP Viewer + Map.

Flask-сервер, который забирает RTSP/HTTP/файловые видеопотоки через OpenCV,
раздаёт их в браузер как MJPEG и показывает камеры на карте (Leaflet).

Запуск:  python app.py            (конфиг: cameras.json или cameras.example.json)
         CAMERAS_FILE=my.json PORT=8080 python app.py
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

# Таймауты FFMPEG задаются до импорта cv2: иначе недоступная камера
# блокирует открытие/чтение потока на ~30 секунд.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|timeout;5000000|stimeout;5000000",
)

import cv2  # noqa: E402
from flask import Flask, Response, abort, jsonify, render_template  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger("rtsp-viewer")

# ================= НАСТРОЙКИ =================
DEFAULTS = {
    "map_center": [55.7558, 37.6173],
    "map_zoom": 13,
    "port": 5000,
    "jpeg_quality": 80,
    "max_width": 960,        # кадры шире ужимаются перед кодированием (0 = не ужимать)
    "max_fps": 15,           # ограничение FPS раздачи
    "idle_timeout": 30,      # сек. без зрителей -> поток останавливается
    "open_timeout_ms": 5000,
    "read_timeout_ms": 5000,
    "reconnect_min": 1,      # сек., начальная пауза переподключения
    "reconnect_max": 30,     # сек., максимальная пауза переподключения
    "cameras": [],
}
# =============================================


def load_config():
    """Читает конфиг: $CAMERAS_FILE -> cameras.json -> cameras.example.json."""
    candidates = [os.environ.get("CAMERAS_FILE"), BASE_DIR / "cameras.json",
                  BASE_DIR / "cameras.example.json"]
    for path in candidates:
        if path and Path(path).is_file():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            log.info("Конфиг загружен: %s", path)
            break
    else:
        log.warning("Конфиг не найден, камер нет")
        data = {}

    cfg = {**DEFAULTS, **data}
    if os.environ.get("PORT"):
        cfg["port"] = int(os.environ["PORT"])

    ids = set()
    for cam in cfg["cameras"]:
        for key in ("id", "name", "url", "lat", "lng"):
            if key not in cam:
                raise ValueError(f"У камеры {cam} нет поля '{key}'")
        if cam["id"] in ids:
            raise ValueError(f"Повторяющийся id камеры: {cam['id']}")
        ids.add(cam["id"])
        url = str(cam["url"])
        # Локальный файл относительно папки проекта (удобно для демо)
        if "://" not in url and not Path(url).is_absolute():
            cam["url"] = str(BASE_DIR / url)
    return cfg


def make_placeholder(text="NO SIGNAL", size=(640, 360)):
    """JPEG-заглушка, которую получают зрители, пока камера недоступна."""
    import numpy as np
    w, h = size
    img = np.full((h, w, 3), 40, dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), _ = cv2.getTextSize(text, font, 1.4, 3)
    cv2.putText(img, text, ((w - tw) // 2, (h + th) // 2), font, 1.4,
                (80, 80, 240), 3, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


def public_camera(cam):
    """Данные камеры, безопасные для отдачи в браузер (без URL с паролями)."""
    return {k: cam[k] for k in ("id", "name", "lat", "lng")}


class CameraStream:
    """Читает поток в фоновом потоке и хранит последний JPEG-кадр.

    Все зрители одной камеры получают один и тот же кадр, поэтому камера
    открывается один раз, сколько бы вкладок/превью ни было открыто.
    Поток запускается при первом зрителе и останавливается после
    `idle_timeout` секунд без зрителей.
    """

    def __init__(self, cam, cfg):
        self.cam = cam
        self.url = cam["url"]
        self.cfg = cfg
        self.is_file = "://" not in self.url
        self.cond = threading.Condition()
        self.frame = None
        self.frame_id = 0
        self.frame_time = 0.0
        self.state = "idle"      # idle | connecting | online | offline
        self.error = None
        self.fps = 0.0
        self.last_access = 0.0
        self.thread = None
        self.stop_event = threading.Event()

    # ---------- жизненный цикл ----------
    def touch(self):
        """Отметить зрителя и при необходимости запустить поток."""
        with self.cond:
            self.last_access = time.monotonic()
            if self.thread is None or not self.thread.is_alive():
                self.stop_event.clear()
                self.state = "connecting"
                self.thread = threading.Thread(
                    target=self._run, name=f"cam-{self.cam['id']}", daemon=True)
                self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _open(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.cfg["open_timeout_ms"],
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.cfg["read_timeout_ms"],
        ])
        if not cap.isOpened():
            cap.release()
            return None
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass
        return cap

    def _run(self):
        backoff = self.cfg["reconnect_min"]
        while not self.stop_event.is_set():
            if self._idle():
                break
            self._set_state("connecting")
            cap = self._open()
            if cap is None:
                self._set_state("offline", "не удалось открыть поток")
                log.warning("[%s] не удалось открыть поток, повтор через %ss",
                            self.cam["name"], backoff)
                if self.stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, self.cfg["reconnect_max"])
                continue

            log.info("[%s] подключено", self.cam["name"])
            backoff = self.cfg["reconnect_min"]
            try:
                self._read_loop(cap)
            finally:
                cap.release()
        self._set_state("idle")
        log.info("[%s] поток остановлен", self.cam["name"])

    def _read_loop(self, cap):
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0
        max_fps = self.cfg["max_fps"] or 0
        # Файлы читаются мгновенно — ограничиваем их родным FPS
        pace = src_fps if self.is_file and 0 < src_fps < 120 else 0
        if max_fps and (not pace or pace > max_fps):
            pace = max_fps
        min_interval = 1.0 / pace if pace else 0
        encode_interval = 1.0 / max_fps if max_fps else 0

        last_encode = 0.0
        fps_count, fps_start = 0, time.monotonic()
        while not self.stop_event.is_set():
            if self._idle():
                return
            t0 = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                if self.is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)   # зацикливаем файл
                    ok, frame = cap.read()
                if not ok:
                    self._set_state("offline", "поток прервался")
                    log.warning("[%s] поток прервался, переподключение",
                                self.cam["name"])
                    return

            now = time.monotonic()
            if now - last_encode >= encode_interval:
                last_encode = now
                jpeg = self._encode(frame)
                if jpeg is not None:
                    self._publish(jpeg)
                    fps_count += 1

            if now - fps_start >= 2:
                self.fps = round(fps_count / (now - fps_start), 1)
                fps_count, fps_start = 0, now

            if min_interval:
                delay = min_interval - (time.monotonic() - t0)
                if delay > 0:
                    self.stop_event.wait(delay)

    def _encode(self, frame):
        max_w = self.cfg["max_width"]
        if max_w and frame.shape[1] > max_w:
            h = int(frame.shape[0] * max_w / frame.shape[1])
            frame = cv2.resize(frame, (max_w, h), interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg["jpeg_quality"]])
        return jpeg.tobytes() if ok else None

    def _publish(self, jpeg):
        with self.cond:
            self.frame = jpeg
            self.frame_id += 1
            self.frame_time = time.time()
            self.state = "online"
            self.error = None
            self.cond.notify_all()

    def _set_state(self, state, error=None):
        with self.cond:
            self.state = state
            self.error = error
            if state != "online":
                self.fps = 0.0
            self.cond.notify_all()

    def _idle(self):
        return time.monotonic() - self.last_access > self.cfg["idle_timeout"]

    # ---------- для клиентов ----------
    def wait_frame(self, last_id, timeout=1.0):
        """Ждёт кадр новее last_id. Возвращает (frame_id, jpeg) или (last_id, None)."""
        self.touch()
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id != last_id, timeout=timeout)
            if self.frame_id != last_id and self.frame is not None:
                return self.frame_id, self.frame
            return last_id, None

    def status(self):
        with self.cond:
            return {
                "state": self.state,
                "error": self.error,
                "fps": self.fps,
                "last_frame": self.frame_time or None,
            }


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["VIEWER"] = cfg

    cameras = {cam["id"]: cam for cam in cfg["cameras"]}
    streams = {cid: CameraStream(cam, cfg) for cid, cam in cameras.items()}
    app.extensions["streams"] = streams
    placeholder = make_placeholder()

    def get_stream(cam_id):
        stream = streams.get(cam_id)
        if stream is None:
            abort(404, description=f"Камера {cam_id} не найдена")
        return stream

    @app.route("/")
    def index():
        public = [public_camera(c) for c in cfg["cameras"]]
        if public:
            center = [sum(c["lat"] for c in public) / len(public),
                      sum(c["lng"] for c in public) / len(public)]
        else:
            center = cfg["map_center"]
        return render_template("index.html", cameras=public,
                               map_center=center, map_zoom=cfg["map_zoom"])

    @app.route("/video/<int:cam_id>")
    def video_feed(cam_id):
        stream = get_stream(cam_id)

        def generate():
            last_id, last_sent = 0, time.monotonic()
            while True:
                last_id, frame = stream.wait_frame(last_id)
                if frame is None:
                    # Пока кадров нет, периодически шлём заглушку: так браузер
                    # видит статус, а сервер замечает отключившегося клиента.
                    if time.monotonic() - last_sent < 2:
                        continue
                    frame = placeholder
                last_sent = time.monotonic()
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() +
                       b"\r\n\r\n" + frame + b"\r\n")

        return Response(generate(),
                        mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-cache, no-store"})

    @app.route("/snapshot/<int:cam_id>")
    def snapshot(cam_id):
        stream = get_stream(cam_id)
        deadline = time.monotonic() + cfg["open_timeout_ms"] / 1000 + 1
        frame = stream.frame
        while frame is None and time.monotonic() < deadline:
            _, frame = stream.wait_frame(0, timeout=0.5)
        if frame is None:
            abort(503, description="Кадр недоступен")
        return Response(frame, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store"})

    @app.route("/api/cameras")
    def api_cameras():
        return jsonify([{**public_camera(cameras[cid]), **s.status()}
                        for cid, s in streams.items()])

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    application = create_app()
    # host='0.0.0.0' позволяет подключиться с других устройств в сети
    application.run(host=os.environ.get("HOST", "0.0.0.0"),
                    port=application.config["VIEWER"]["port"],
                    threaded=True, debug=False)
