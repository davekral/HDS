"""
Centrální konfigurace hlasového kuchyňského asistenta.

Vše, co je závislé na prostředí (přihlašovací údaje, modely, hlasy, adresy),
je soustředěno zde, aby se to dalo snadno změnit na jednom místě.
"""

# --------------------------------------------------------------------------- #
#  Ollama (LLM + embeddingy)
# --------------------------------------------------------------------------- #
OLLAMA_HOST = "https://ollama.kky.zcu.cz"
OLLAMA_USER = "hds2026"
OLLAMA_PASSWORD = "waikootiogiojaepee4hebungeajiech"

# Hlavní konverzační / rozhodovací model.
# Ověřené funkční volby na serveru KKY: "gemma3:12b", "phi4:latest",
# "qwen3.5:9b", "gemma3:4b" (rychlejší, méně přesný).
LLM_MODEL = "gemma3:12b"

# Model pro embeddingy (RAG). Dedikovaný embedding model na serveru.
EMBED_MODEL = "qwen3-embedding:latest"

# Velikost kontextového okna pro LLM.
NUM_CTX = 1024 * 16

# --------------------------------------------------------------------------- #
#  SpeechCloud (ASR – poslouchání, TTS – mluvení)
# --------------------------------------------------------------------------- #
# Adresu používá frontend (static/app.js) přes proměnnou SPEECHCLOUD_URI.
SPEECHCLOUD_URI = "https://speechcloud.kky.zcu.cz:9443/v1/speechcloud/edu-hds-all"

# Výchozí český hlas pro syntézu řeči (viz přehlídka hlasů v example_cviceni.py).
TTS_VOICE = "Iva210"

# Časový limit (v sekundách) pro jedno naslouchání uživateli v hlasovém režimu.
ASR_TIMEOUT = 20.0

# --------------------------------------------------------------------------- #
#  Webový server (Tornado)
# --------------------------------------------------------------------------- #
SERVER_ADDRESS = "0.0.0.0"
SERVER_PORT = 8888
