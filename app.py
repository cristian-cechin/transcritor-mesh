import os
import re
import uuid
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
from groq import Groq
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

app = Flask(__name__)
CORS(app)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "SUA_CHAVE_GROQ_AQUI")
client = Groq(api_key=GROQ_API_KEY)

UPLOAD_FOLDER = "/tmp/transcritor"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

COOKIES_FILE = "/tmp/yt_cookies.txt"

def get_yt_dlp_base_opts():
    """Opções base do yt-dlp, com cookies do YouTube se disponíveis."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "web"]}},
    }
    yt_cookies = os.environ.get("YT_COOKIES", "")
    if yt_cookies:
        with open(COOKIES_FILE, "w") as f:
            f.write(yt_cookies)
        opts["cookiefile"] = COOKIES_FILE
    return opts

# ─── JOB STATUS ───────────────────────────────────────────────────────────────
jobs = {}

def update_job(job_id, **kwargs):
    jobs[job_id].update(kwargs)

# ─── YOUTUBE: TRANSCRIÇÃO DIRETA VIA API ──────────────────────────────────────
def extract_youtube_id(url):
    patterns = [
        r"(?:v=|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def try_youtube_transcript(url, job_id):
    """Tenta buscar transcrição direto do YouTube. Retorna texto ou None."""
    video_id = extract_youtube_id(url)
    if not video_id:
        return None

    update_job(job_id, progress=20, message="Buscando transcrição do YouTube...")

    try:
        # Passa cookies se disponíveis
        kwargs = {}
        yt_cookies = os.environ.get("YT_COOKIES", "")
        if yt_cookies:
            with open(COOKIES_FILE, "w") as f:
                f.write(yt_cookies)
            kwargs["cookies"] = COOKIES_FILE

        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id, **kwargs)

        # Tenta: pt manual → pt gerado → qualquer manual → qualquer gerado
        transcript = None
        try:
            transcript = transcript_list.find_transcript(["pt", "pt-BR", "pt-PT"])
        except Exception:
            pass
        if not transcript:
            try:
                transcript = transcript_list.find_generated_transcript(["pt", "pt-BR", "pt-PT"])
            except Exception:
                pass
        if not transcript:
            # Pega qualquer transcrição disponível
            all_transcripts = list(transcript_list)
            if all_transcripts:
                transcript = all_transcripts[0]

        if not transcript:
            return None

        update_job(job_id, progress=70, message="Processando transcrição...")
        entries = transcript.fetch(**kwargs)
        text = " ".join(e["text"] for e in entries)
        return text.strip()

    except (NoTranscriptFound, TranscriptsDisabled):
        return None
    except Exception:
        return None

# ─── EXTRAÇÃO DE ÁUDIO VIA YT-DLP ────────────────────────────────────────────
def extract_audio_from_url(url, output_path, job_id):
    update_job(job_id, progress=15, message="Identificando fonte do vídeo...")

    # Baixa o vídeo/áudio sem pós-processamento para evitar problemas de codec
    ydl_opts = {
        **get_yt_dlp_base_opts(),
        "format": "bestaudio/best/worstaudio/worst",
        "outtmpl": output_path + ".%(ext)s",
    }

    update_job(job_id, progress=25, message="Baixando áudio do vídeo...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloaded_ext = info.get("ext", "mp4")

    # Encontrar arquivo baixado
    downloaded_file = output_path + "." + downloaded_ext
    if not os.path.exists(downloaded_file):
        for ext in ["mp4", "webm", "m4a", "mp3", "ogg", "wav"]:
            candidate = output_path + "." + ext
            if os.path.exists(candidate):
                downloaded_file = candidate
                break

    if not os.path.exists(downloaded_file):
        raise FileNotFoundError("Não foi possível baixar o vídeo.")

    # Converter para mp3 via ffmpeg
    audio_file = output_path + ".mp3"
    result = subprocess.run([
        "ffmpeg", "-i", downloaded_file,
        "-vn", "-ar", "16000", "-ac", "1", "-b:a", "128k",
        audio_file, "-y"
    ], capture_output=True, text=True)

    if downloaded_file != audio_file and os.path.exists(downloaded_file):
        os.remove(downloaded_file)

    if not os.path.exists(audio_file):
        raise RuntimeError(f"Erro ao converter áudio: {result.stderr[-300:]}")

    return audio_file


def extract_audio_from_file(file_path, output_path, job_id):
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
    file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    if file_size_mb <= 24:
        return [audio_path]

    update_job(job_id, progress=40, message="Dividindo áudio em partes...")

    chunk_duration = 600
    chunks = []
    base = os.path.splitext(audio_path)[0]

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
def is_youtube_url(url):
    return "youtube.com" in url or "youtu.be" in url

def process_job(job_id, source_type, url=None, file_path=None):
    try:
        output_base = os.path.join(UPLOAD_FOLDER, job_id)
        update_job(job_id, status="processing", progress=10, message="Iniciando processamento...")

        if source_type == "url" and is_youtube_url(url):
            # Tenta transcrição direta do YouTube primeiro
            text = try_youtube_transcript(url, job_id)
            if text:
                update_job(job_id, status="done", progress=100,
                           message="Transcrição concluída!", result=text)
                return
            # Fallback: baixar áudio e usar Groq
            update_job(job_id, progress=30, message="Sem legenda disponível, baixando áudio...")

        # Para não-YouTube ou fallback
        if source_type == "url":
            audio_path = extract_audio_from_url(url, output_base, job_id)
        elif source_type == "audio_file":
            # Arquivo de áudio enviado direto — manda pro Groq sem converter
            audio_path = file_path
        else:
            audio_path = extract_audio_from_file(file_path, output_base, job_id)

        update_job(job_id, progress=45, message="Áudio extraído. Preparando transcrição...")

        chunks = split_audio_if_needed(audio_path, job_id)

        full_text = ""
        for i, chunk in enumerate(chunks):
            text = transcribe_audio(chunk, job_id, i, len(chunks))
            full_text += text + " "

        update_job(job_id, progress=95, message="Finalizando...")

        for f in [audio_path, file_path] + ([c for c in chunks if c != audio_path]):
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except:
                pass

        update_job(job_id, status="done", progress=100,
                   message="Transcrição concluída!", result=full_text.strip())

    except Exception as e:
        update_job(job_id, status="error", progress=0, message=str(e), error=str(e))


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
    allowed_video = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
    allowed_audio = {"mp3", "m4a", "wav", "aac", "ogg", "flac"}
    allowed = allowed_video | allowed_audio
    ext = file.filename.rsplit(".", 1)[-1].lower()

    if ext not in allowed:
        return jsonify({"error": f"Formato não suportado. Use: {', '.join(sorted(allowed))}"}), 400

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_input.{ext}")
    file.save(file_path)

    jobs[job_id] = {"status": "queued", "progress": 0, "message": "Na fila...", "result": None, "error": None}

    source = "audio_file" if ext in allowed_audio else "file"
    thread = threading.Thread(target=process_job, args=(job_id, source), kwargs={"file_path": file_path})
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)


@app.route("/api/download", methods=["POST"])
def download_video():
    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "720")  # "1080", "720", "480", "mp3"

    if not url:
        return jsonify({"error": "URL não informada"}), 400

    job_id = str(uuid.uuid4())
    output_base = os.path.join(UPLOAD_FOLDER, job_id)

    try:
        if quality == "mp3":
            ydl_opts = {
                **get_yt_dlp_base_opts(),
                "format": "bestaudio/best",
                "outtmpl": output_base + ".%(ext)s",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
            ext = "mp3"
            mime = "audio/mpeg"
        else:
            height = quality
            ydl_opts = {
                **get_yt_dlp_base_opts(),
                "format": f"best[height<={height}]/best",
                "outtmpl": output_base + ".%(ext)s",
            }
            ext = "mp4"
            mime = "video/mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = re.sub(r'[^\w\s-]', '', info.get("title", "video"))[:60].strip()

        # Encontrar arquivo baixado
        output_file = output_base + "." + ext
        if not os.path.exists(output_file):
            for candidate_ext in ["mp4", "mkv", "webm", "mp3", "m4a"]:
                candidate = output_base + "." + candidate_ext
                if os.path.exists(candidate):
                    output_file = candidate
                    ext = candidate_ext
                    mime = "video/mp4" if candidate_ext in ["mp4", "mkv", "webm"] else "audio/mpeg"
                    break

        if not os.path.exists(output_file):
            return jsonify({"error": "Não foi possível baixar o vídeo."}), 500

        filename = f"{title}.{ext}"

        @after_this_request
        def cleanup(response):
            try:
                os.remove(output_file)
            except Exception:
                pass
            return response

        return send_file(
            output_file,
            mimetype=mime,
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
