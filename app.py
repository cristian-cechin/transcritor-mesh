import os
import uuid
import tempfile
import subprocess
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from groq import Groq
import yt_dlp

app = Flask(__name__)
CORS(app)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "SUA_CHAVE_GROQ_AQUI")
client = Groq(api_key=GROQ_API_KEY)

UPLOAD_FOLDER = "/tmp/transcritor"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MAX_FILE_MB = 200  # limite de upload

# ─── JOB STATUS ───────────────────────────────────────────────────────────────
jobs = {}  # job_id -> { status, progress, message, result, error }

def update_job(job_id, **kwargs):
    jobs[job_id].update(kwargs)

# ─── EXTRAÇÃO DE ÁUDIO ────────────────────────────────────────────────────────
def extract_audio_from_url(url, output_path, job_id):
    """Baixa e extrai áudio de YouTube, Instagram, TikTok, link direto, etc."""
    update_job(job_id, progress=15, message="Identificando fonte do vídeo...")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path + ".%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android", "web"]}},
    }

    update_job(job_id, progress=25, message="Baixando áudio do vídeo...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    audio_file = output_path + ".mp3"
    if not os.path.exists(audio_file):
        # tenta encontrar o arquivo com qualquer extensão
        for ext in ["mp3", "m4a", "webm", "ogg", "wav"]:
            candidate = output_path + "." + ext
            if os.path.exists(candidate):
                audio_file = candidate
                break

    if not os.path.exists(audio_file):
        raise FileNotFoundError("Não foi possível extrair o áudio do vídeo.")

    return audio_file


def extract_audio_from_file(file_path, output_path, job_id):
    """Extrai áudio de um arquivo de vídeo enviado (mp4, mov, etc.)"""
    update_job(job_id, progress=20, message="Extraindo áudio do vídeo...")

    audio_file = output_path + ".mp3"
    result = subprocess.run([
        "ffmpeg", "-i", file_path,
        "-vn", "-ar", "16000", "-ac", "1",
        "-b:a", "128k", audio_file, "-y"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"Erro ao extrair áudio: {result.stderr}")

    return audio_file


def split_audio_if_needed(audio_path, job_id):
    """Divide o áudio em chunks de 24MB se necessário (limite da API Groq)."""
    file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)

    if file_size_mb <= 24:
        return [audio_path]

    update_job(job_id, progress=40, message="Dividindo áudio em partes...")

    chunk_duration = 600  # 10 minutos por chunk
    chunks = []
    chunk_dir = os.path.dirname(audio_path)
    base = os.path.splitext(audio_path)[0]

    # descobrir duração total
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path
    ], capture_output=True, text=True)

    total_duration = float(result.stdout.strip())
    start = 0
    idx = 0

    while start < total_duration:
        chunk_path = f"{base}_chunk{idx:03d}.mp3"
        subprocess.run([
            "ffmpeg", "-i", audio_path,
            "-ss", str(start), "-t", str(chunk_duration),
            "-ar", "16000", "-ac", "1", chunk_path, "-y"
        ], capture_output=True)
        chunks.append(chunk_path)
        start += chunk_duration
        idx += 1

    return chunks


def transcribe_audio(audio_path, job_id, chunk_index=0, total_chunks=1):
    """Envia áudio para a API Groq Whisper e retorna texto."""
    pct_base = 55 + (chunk_index / total_chunks) * 35
    update_job(job_id, progress=int(pct_base),
               message=f"Transcrevendo{'...' if total_chunks == 1 else f' parte {chunk_index+1} de {total_chunks}...'}")

    with open(audio_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(audio_path), audio_file),
            model="whisper-large-v3",
            response_format="text",
            language="pt",
        )

    return transcription


# ─── WORKER THREAD ────────────────────────────────────────────────────────────
def process_job(job_id, source_type, url=None, file_path=None):
    try:
        output_base = os.path.join(UPLOAD_FOLDER, job_id)

        update_job(job_id, status="processing", progress=10, message="Iniciando processamento...")

        # 1. Obter áudio
        if source_type == "url":
            audio_path = extract_audio_from_url(url, output_base, job_id)
        else:
            audio_path = extract_audio_from_file(file_path, output_base, job_id)

        update_job(job_id, progress=45, message="Áudio extraído. Preparando transcrição...")

        # 2. Dividir se necessário
        chunks = split_audio_if_needed(audio_path, job_id)

        # 3. Transcrever
        full_text = ""
        for i, chunk in enumerate(chunks):
            text = transcribe_audio(chunk, job_id, i, len(chunks))
            full_text += text + " "

        # 4. Limpeza
        update_job(job_id, progress=95, message="Finalizando...")

        # Remover arquivos temporários
        for f in [audio_path, file_path] + ([c for c in chunks if c != audio_path]):
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except:
                pass

        update_job(job_id,
                   status="done",
                   progress=100,
                   message="Transcrição concluída!",
                   result=full_text.strip())

    except Exception as e:
        update_job(job_id,
                   status="error",
                   progress=0,
                   message=str(e),
                   error=str(e))


# ─── ROTAS ────────────────────────────────────────────────────────────────────
@app.route("/api/transcribe/url", methods=["POST"])
def transcribe_url():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL não informada"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "progress": 0, "message": "Na fila...", "result": None, "error": None}

    thread = threading.Thread(target=process_job, args=(job_id, "url"), kwargs={"url": url})
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/transcribe/file", methods=["POST"])
def transcribe_file():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files["file"]
    allowed = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
    ext = file.filename.rsplit(".", 1)[-1].lower()

    if ext not in allowed:
        return jsonify({"error": f"Formato não suportado. Use: {', '.join(allowed)}"}), 400

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_input.{ext}")
    file.save(file_path)

    jobs[job_id] = {"status": "queued", "progress": 0, "message": "Na fila...", "result": None, "error": None}

    thread = threading.Thread(target=process_job, args=(job_id, "file"), kwargs={"file_path": file_path})
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
