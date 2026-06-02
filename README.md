# Audio Encoder

A self-hosted audio conversion service. Upload audio files, set encoding options, preview them in the browser, and download the result. Files are automatically deleted after 5 minutes.

## Features

- **Format conversion** — MP3, WAV, FLAC, OGG, AAC, M4A, WMA, OPUS and more → FLAC, WAV, MP3, OGG, AAC
- **Audio settings** — configurable channels (mono/stereo), sample rate, and bit rate
- **Batch processing** — convert up to 10 files at once, concurrently
- **In-browser player** — play converted files before downloading; Space bar to play/pause
- **Auto-delete** — files are deleted immediately after download, or after 5 minutes
- **Magic-byte validation** — files are rejected by content, not just extension
- **Responsive UI** — works on desktop and mobile

## Requirements

- Python 3.11+ and ffmpeg (for local development)
- Docker and Docker Compose (for production)

## Local development

```bash
# Install ffmpeg (macOS)
brew install ffmpeg

# Create a virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run the server
.venv/bin/uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000/static/` in your browser.

## Docker

```bash
docker compose up -d --build
```

The service binds to `127.0.0.1:8000` by default. Put nginx in front for TLS and public access.

### Deploying to a Raspberry Pi

```bash
# Copy the project to the Pi
rsync -av --exclude='.venv' --exclude='storage' . pi@<PI_IP>:~/audio-encoder/

# On the Pi
cd ~/audio-encoder
docker compose up -d --build
```

See the nginx reverse proxy and local DNS setup notes below for a custom local domain.

#### nginx reverse proxy

```nginx
server {
    listen 80;
    server_name audio.home;

    client_max_body_size 160M;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

#### Local domain

Add a DNS record in your router (or Pi-hole) pointing your chosen domain to the Pi's reserved IP, or add an entry to `/etc/hosts` on each device:

```
192.168.1.50    audio.home
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/convert` | Upload files and convert. Form fields: `files`, `target_format`, `channels`, `sample_rate`, `bit_rate` |
| `GET` | `/stream/{job_id}` | Stream a converted file for in-browser playback (no delete) |
| `GET` | `/download/{job_id}` | Download a converted file (deleted after response) |
| `GET` | `/formats` | List supported formats and valid parameter values |
| `GET` | `/health` | Service health, active job count, and uptime |

## Security

- Files are validated by magic bytes, not file extension
- All ffmpeg invocations use argument lists — no shell injection surface
- Converted files are served by opaque job ID, with strict regex validation
- Security headers (`X-Content-Type-Options`, `X-Frame-Options`, `CSP`, `Referrer-Policy`) on every response
- Docker container runs as a non-root user with a read-only root filesystem, all Linux capabilities dropped

## License

MIT
