import cv2
import threading
import time
from flask import Flask, Response, render_template_string

# ================= НАСТРОЙКИ =================
# Укажите здесь ваши камеры: имя, URL и координаты (широта, долгота)
CAMERAS = [
    {
        "id": 1,
        "name": "Камера 1",
        "url": "rtsp://user:pass@192.168.1.10:554/stream1",
        "lat": 55.7558,
        "lng": 37.6173
    },
    {
        "id": 2,
        "name": "Камера 2",
        "url": "rtsp://user:pass@192.168.1.11:554/stream1",
        "lat": 55.7600,
        "lng": 37.6200
    }
    # Добавляйте свои камеры по аналогии
]

# Начальный центр карты (если не хотите вычислять автоматически)
MAP_CENTER = [55.7558, 37.6173]
MAP_ZOOM = 13

# Порт веб-сервера
PORT = 5000
# =============================================


app = Flask(__name__)

# Класс для захвата и раздачи потока
class CameraStream:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.cap = None
        self.lock = threading.Lock()
        self.frame = None
        self.running = True
        self.connect()

    def connect(self):
        """Подключение к RTSP"""
        try:
            self.cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            if not self.cap.isOpened():
                print(f"Не удалось открыть поток: {self.rtsp_url}")
                self.cap = None
        except Exception as e:
            print(f"Ошибка подключения к {self.rtsp_url}: {e}")
            self.cap = None

    def get_frame(self):
        """Получение кадра (MJPEG)"""
        if self.cap is None:
            # Попытка переподключения
            self.connect()
            return None

        with self.lock:
            ret, frame = self.cap.read()
            if not ret:
                # Переподключение при потере кадра
                self.cap.release()
                self.connect()
                return None

            # Кодирование в JPEG для MJPEG
            ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ret:
                return None
            return jpeg.tobytes()

    def release(self):
        self.running = False
        if self.cap:
            self.cap.release()


# Хранилище потоков
streams = {}


def get_stream(cam_id):
    """Получить или создать поток для камеры"""
    if cam_id not in streams:
        for cam in CAMERAS:
            if cam["id"] == cam_id:
                streams[cam_id] = CameraStream(cam["url"])
                break
    return streams.get(cam_id)


def generate_frames(cam_id):
    """Генератор MJPEG кадров для Flask"""
    stream = get_stream(cam_id)
    if not stream:
        return

    while True:
        frame = stream.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        else:
            time.sleep(0.5)  # Пауза при ошибке


@app.route('/video/<int:cam_id>')
def video_feed(cam_id):
    """Эндпоинт для MJPEG потока"""
    return Response(generate_frames(cam_id),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# HTML шаблон (карта + видео)
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>RTSP Viewer + Map</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <!-- Leaflet CSS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #1a1a1a; color: #fff; }
        #container { display: flex; height: 100vh; }
        #sidebar {
            width: 400px;
            background: #2a2a2a;
            padding: 15px;
            overflow-y: auto;
            border-right: 1px solid #444;
        }
        #map-container { flex: 1; position: relative; }
        #map { width: 100%; height: 100%; }
        .camera-card {
            background: #333;
            border-radius: 8px;
            margin-bottom: 15px;
            padding: 10px;
            border: 1px solid #555;
        }
        .camera-card h3 {
            margin-bottom: 8px;
            font-size: 14px;
            color: #4fc3f7;
        }
        .camera-card img {
            width: 100%;
            border-radius: 4px;
            background: #000;
            min-height: 150px;
        }
        .camera-card .coords {
            font-size: 11px;
            color: #999;
            margin-top: 5px;
        }
        .camera-card .status {
            font-size: 11px;
            color: #4caf50;
        }
    </style>
</head>
<body>
    <div id="container">
        <div id="sidebar">
            <h2 style="margin-bottom: 15px;">📹 Камеры</h2>
            {% for cam in cameras %}
            <div class="camera-card" id="card-{{ cam.id }}">
                <h3>{{ cam.name }} (ID: {{ cam.id }})</h3>
                <img src="/video/{{ cam.id }}" alt="Поток {{ cam.name }}" 
                     onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22300%22 height=%22200%22><rect fill=%22%23333%22 width=%22300%22 height=%22200%22/><text fill=%22%23f44%22 x=%2250%25%22 y=%2250%25%22 text-anchor=%22middle%22>Ошибка потока</text></svg>'">
                <div class="coords">📍 {{ cam.lat }}, {{ cam.lng }}</div>
                <div class="status">● Подключено</div>
            </div>
            {% endfor %}
        </div>
        <div id="map-container">
            <div id="map"></div>
        </div>
    </div>

    <!-- Leaflet JS -->
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        // Данные камер из Python
        const cameras = {{ cameras_json | safe }};
        const mapCenter = {{ map_center_json | safe }};
        const mapZoom = {{ map_zoom }};

        // Инициализация карты
        const map = L.map('map').setView(mapCenter, mapZoom);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors',
            maxZoom: 19
        }).addTo(map);

        // Маркеры камер
        cameras.forEach(cam => {
            const marker = L.marker([cam.lat, cam.lng]).addTo(map);
            
            // Всплывающее окно с превью
            const popupContent = `
                <div style="width: 280px;">
                    <b>${cam.name}</b><br>
                    <img src="/video/${cam.id}" style="width: 100%; margin-top: 5px; border-radius: 4px;">
                </div>
            `;
            marker.bindPopup(popupContent, { maxWidth: 300 });

            // Подсветка карточки при клике на маркер
            marker.on('click', () => {
                document.querySelectorAll('.camera-card').forEach(c => c.style.borderColor = '#555');
                const card = document.getElementById(`card-${cam.id}`);
                if (card) card.style.borderColor = '#4fc3f7';
            });
        });

        // Автоматическое построение границ по всем камерам (если их больше одной)
        if (cameras.length > 1) {
            const bounds = L.latLngBounds(cameras.map(c => [c.lat, c.lng]));
            map.fitBounds(bounds, { padding: [50, 50] });
        }
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    """Главная страница с картой и видео"""
    import json
    cameras_json = json.dumps(CAMERAS)
    # Автоматическое определение центра карты, если камер несколько
    if len(CAMERAS) > 1:
        center = [sum(c['lat'] for c in CAMERAS)/len(CAMERAS),
                  sum(c['lng'] for c in CAMERAS)/len(CAMERAS)]
    else:
        center = MAP_CENTER

    return render_template_string(
        HTML_TEMPLATE,
        cameras=CAMERAS,
        cameras_json=cameras_json,
        map_center_json=json.dumps(center),
        map_zoom=MAP_ZOOM
    )


if __name__ == '__main__':
    # Запуск сервера
    # host='0.0.0.0' позволяет подключиться с других устройств в сети
    app.run(host='0.0.0.0', port=PORT, threaded=True, debug=False)
