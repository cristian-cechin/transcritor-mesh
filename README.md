# Transcritor Mesh 🎙️

Sistema de transcrição de vídeo da Agência Mesh.
Suporta: YouTube, Instagram, TikTok, links diretos, MP4/MOV.
API: Groq Whisper Large v3 (gratuito).

---

## ARQUIVOS

```
transcritor-mesh/
├── app.py            → Backend Python (API)
├── index.html        → Frontend (vai pro seu site)
├── requirements.txt  → Dependências Python
├── Procfile          → Start do servidor (Railway)
├── runtime.txt       → Versão Python
└── nixpacks.toml     → Config Railway (instala ffmpeg)
```

---

## OPÇÃO 1: RAILWAY (recomendado — grátis)

### 1. Criar conta
- Acesse https://railway.app e entre com GitHub

### 2. Deploy do backend
1. Crie um novo projeto → "Deploy from GitHub repo"
2. Suba esta pasta como repositório no GitHub
3. Railway detecta automaticamente o Python
4. Vá em **Variables** e adicione:
   ```
   GROQ_API_KEY = sua_chave_aqui
   ```
5. Aguarde o deploy. Você receberá uma URL tipo:
   `https://transcritor-mesh-production.up.railway.app`

### 3. Chave Groq (gratuita)
- Acesse https://console.groq.com
- Crie conta → API Keys → Create API Key
- Copie e cole na variável do Railway

### 4. Configurar o frontend
- Abra `index.html`
- Encontre esta linha (perto do final):
  ```js
  const API_BASE = "https://SEU-BACKEND-AQUI.com";
  ```
- Substitua pela URL do Railway

### 5. Subir o frontend na KingHost
- Acesse o painel da KingHost
- Vá em Gerenciador de Arquivos → public_html
- Crie uma pasta: `transcritor/`
- Faça upload do `index.html` para dentro dela
- Acesse: `https://seusite.com.br/transcritor/`

---

## OPÇÃO 2: VPS KINGHOST

Se tiver VPS na KingHost, pode hospedar tudo lá:

```bash
# 1. Instalar dependências
sudo apt update
sudo apt install python3-pip ffmpeg -y

# 2. Instalar pacotes Python
pip install -r requirements.txt

# 3. Configurar variável de ambiente
export GROQ_API_KEY="sua_chave_aqui"

# 4. Rodar em produção
gunicorn app:app --bind 0.0.0.0:5000 --workers 2 --timeout 300 --daemon

# 5. Configurar nginx para apontar o domínio para a porta 5000
```

---

## CORS

O backend já está configurado com CORS liberado para qualquer origem.
Se quiser restringir ao seu domínio, edite `app.py`:

```python
CORS(app, origins=["https://seusite.com.br"])
```

---

## LIMITES GROQ (plano gratuito)

- 100 horas de áudio por mês
- Arquivos até 25MB por requisição (o sistema divide automaticamente)
- Suporta português nativamente

---

## SUPORTE

Dúvidas? Fala com a Agência Mesh.
