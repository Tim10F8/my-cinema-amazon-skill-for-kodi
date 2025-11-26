# ==============================================================================
# FICHIER : app.py
# VERSION : 1.3.11
# DATE    : 2025-11-26 20:50:00 (CET)
# AUTEUR  : Richard Perez (richard@perez-mail.fr)
#
# DESCRIPTION : 
# Skill Alexa pour contrôle vocal de Kodi sur Nvidia Shield.
# FIX v1.3.11 : Correction critique de la détection de Kodi (is_kodi_responsive).
# Accepte désormais les retours 401/405 comme preuve de vie pour éviter
# les faux négatifs liés à l'authentification.
# ==============================================================================

from flask import Flask, request, jsonify
import requests
import threading
import time
import subprocess
import os
import sys
import logging
import json
from wakeonlan import send_magic_packet

# --- CONFIGURATION LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("KodiMiddleware")

# --- METADATA ---
APP_VERSION = "1.3.11"
APP_DATE = "2025-11-26"
APP_AUTHOR = "Richard Perez (richard@perez-mail.fr)"

app = Flask(__name__)

# ==========================================
# 1. CONFIGURATION
# ==========================================

# Mode Debug
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
if DEBUG_MODE:
    logger.setLevel(logging.DEBUG)
    logger.debug("MODE DEBUG ACTIVÉ")

# Réseau Shield
SHIELD_IP = os.getenv("SHIELD_IP")
SHIELD_MAC = os.getenv("SHIELD_MAC")

# Configuration Kodi
KODI_PORT = os.getenv("KODI_PORT")
KODI_USER = os.getenv("KODI_USER")
KODI_PASS = os.getenv("KODI_PASS")

# API TMDB & TRAKT
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID")
TRAKT_ACCESS_TOKEN = os.getenv("TRAKT_ACCESS_TOKEN")

# --- CONFIGURATION DES PLAYERS ---
PLAYER_DEFAULT = os.getenv("PLAYER_DEFAULT", "fenlight_auto.json")
PLAYER_SELECT = os.getenv("PLAYER_SELECT", "fenlight_select.json")

# --- CONFIGURATION AUTO-PATCHER ---
FENLIGHT_REMOTE_PATH = "/sdcard/Android/data/org.xbmc.kodi/files/.kodi/addons/plugin.video.fenlight/resources/lib/modules/sources.py"
FENLIGHT_LOCAL_TEMP = "/tmp/sources.py"
BLOCKING_CODE_SNIPPET = "return kodi_utils.notification('WARNING: External Playback Detected!')"
PATCH_CHECK_INTERVAL = 3600 

# URL de base Kodi
if SHIELD_IP and KODI_PORT:
    KODI_BASE_URL = f"http://{SHIELD_IP}:{KODI_PORT}/jsonrpc"
else:
    KODI_BASE_URL = None
    logger.critical("Configuration incomplète : SHIELD_IP ou KODI_PORT manquant.")

# ==========================================
# 2. AUTO-PATCHER & SCHEDULER
# ==========================================

def check_and_patch_fenlight():
    if not SHIELD_IP: return

    # Log réduit pour ne pas polluer toutes les heures, sauf en debug
    if DEBUG_MODE: logger.info(f"[PATCHER] Vérification intégrité Fen Light...")
    
    try:
        subprocess.run(["adb", "disconnect", SHIELD_IP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Timeout ajouté pour ne pas bloquer si ADB plante
        res_con = subprocess.run(["adb", "connect", SHIELD_IP], capture_output=True, text=True, timeout=5)
    except Exception as e:
        if DEBUG_MODE: logger.error(f"[PATCHER] Erreur connexion ADB: {e}")
        return

    if os.path.exists(FENLIGHT_LOCAL_TEMP):
        os.remove(FENLIGHT_LOCAL_TEMP)
        
    try:
        result = subprocess.run(["adb", "pull", FENLIGHT_REMOTE_PATH, FENLIGHT_LOCAL_TEMP], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return # Échec silencieux (Kodi peut être éteint)
    except Exception:
        return

    try:
        with open(FENLIGHT_LOCAL_TEMP, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        patched = False
        already_patched = False

        for line in lines:
            if BLOCKING_CODE_SNIPPET in line:
                if line.strip().startswith("#"):
                    already_patched = True
                    new_lines.append(line)
                else:
                    logger.info("[PATCHER] Protection détectée ! Application du patch...")
                    new_lines.append("# " + line.lstrip()) 
                    patched = True
            else:
                new_lines.append(line)

        if patched:
            with open(FENLIGHT_LOCAL_TEMP, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            
            push_res = subprocess.run(["adb", "push", FENLIGHT_LOCAL_TEMP, FENLIGHT_REMOTE_PATH], capture_output=True)
            if push_res.returncode == 0:
                logger.info("[PATCHER] SUCCÈS : Fichier patché envoyé sur la Shield.")
            else:
                logger.error("[PATCHER] ÉCHEC : Impossible d'écrire sur la Shield.")
        elif already_patched and DEBUG_MODE:
            logger.info("[PATCHER] OK : Fichier déjà propre.")

    except Exception as e:
        logger.error(f"[PATCHER] Erreur traitement fichier: {e}")

def patcher_scheduler():
    while True:
        check_and_patch_fenlight()
        time.sleep(PATCH_CHECK_INTERVAL)

# ==========================================
# 3. GESTION PUISSANCE (FIX v1.3.11)
# ==========================================

def is_kodi_responsive():
    """
    Vérifie si Kodi répond au ping HTTP.
    Accepte 200 (OK), 401 (Unauthorized) et 405 (Method Not Allowed).
    Si Kodi répond 401, c'est qu'il est vivant mais protégé par mot de passe.
    """
    if not KODI_BASE_URL: return False
    try:
        # On ne passe pas d'auth ici pour le ping rapide
        r = requests.get(KODI_BASE_URL, timeout=2)
        if r.status_code in [200, 401, 405]:
            return True
        if DEBUG_MODE:
            logger.debug(f"[PING] Code inattendu de Kodi : {r.status_code}")
    except Exception as e:
        if DEBUG_MODE: logger.debug(f"[PING] Échec connexion : {e}")
        pass
    return False

def wake_and_start_kodi():
    if not SHIELD_IP or not SHIELD_MAC:
        logger.error("Impossible de réveiller : IP ou MAC manquante.")
        return False

    logger.info(f"[POWER] Vérification de l'état de Kodi sur {SHIELD_IP}...")

    # Check rapide avant de tout lancer
    if is_kodi_responsive(): 
        logger.info("[POWER] Kodi est déjà actif.")
        return True

    logger.info("[POWER] Kodi inactif. Lancement de la procédure de réveil...")

    # 1. WoL
    try:
        send_magic_packet(SHIELD_MAC)
    except Exception as e:
        logger.error(f"[POWER] Erreur WoL : {e}")

    # 2. ADB Wake
    try:
        subprocess.run(["adb", "connect", SHIELD_IP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        subprocess.run(["adb", "shell", "input", "keyevent", "WAKEUP"], stdout=subprocess.DEVNULL, timeout=2)
        time.sleep(0.5) 
        subprocess.run(["adb", "shell", "input", "keyevent", "WAKEUP"], stdout=subprocess.DEVNULL, timeout=2)
    except Exception as e:
        logger.error(f"[POWER] Erreur ADB Wakeup : {e}")

    if is_kodi_responsive(): return True

    # 3. Launch Kodi
    logger.info("[POWER] Envoi commande de démarrage Kodi via ADB...")
    try:
        subprocess.run(["adb", "shell", "am", "start", "-n", "org.xbmc.kodi/.Splash"], stdout=subprocess.DEVNULL, timeout=3)
    except Exception as e:
        logger.error(f"[POWER] Erreur Launch : {e}")

    # 4. Wait Loop
    logger.info("[POWER] Attente disponibilité Kodi (max 45s)...")
    for i in range(45): 
        if is_kodi_responsive():
            logger.info(f"[POWER] Kodi opérationnel après {i+1} secondes.")
            time.sleep(4) # Tempo de chargement UI
            return True
        time.sleep(1)
    
    logger.error("[POWER] Echec critique : Kodi ne répond pas.")
    return False

# ==========================================
# 4. HELPERS
# ==========================================

def search_tmdb_movie(query, year=None):
    if not TMDB_API_KEY: return None, None, None
    base_url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": query, "language": "fr-FR"}
    if year: params['year'] = year
    try:
        logger.debug(f"[TMDB] Recherche Film : '{query}' (Year: {year})")
        r = requests.get(base_url, params=params, timeout=2)
        data = r.json()
        if data.get('results'):
            res = data['results'][0]
            logger.info(f"[TMDB] Film trouvé : {res['title']} ({res['id']})")
            return res['id'], res['title'], res.get('release_date', '')[:4]
        else:
            logger.warning(f"[TMDB] Aucun film trouvé pour : {query}")
    except Exception as e:
        logger.error(f"[TMDB] Erreur API : {e}")
    return None, None, None

def search_tmdb_show(query):
    if not TMDB_API_KEY: return None, None
    base_url = "https://api.themoviedb.org/3/search/tv"
    params = {"api_key": TMDB_API_KEY, "query": query, "language": "fr-FR"}
    try:
        logger.debug(f"[TMDB] Recherche Série : '{query}'")
        r = requests.get(base_url, params=params, timeout=2)
        data = r.json()
        if data.get('results'):
            res = data['results'][0]
            logger.info(f"[TMDB] Série trouvée : {res['name']} ({res['id']})")
            return res['id'], res['name']
        else:
            logger.warning(f"[TMDB] Aucune série trouvée pour : {query}")
    except Exception as e:
        logger.error(f"[TMDB] Erreur API : {e}")
    return None, None

def check_episode_exists(tmdb_id, season, episode):
    if not TMDB_API_KEY: return False
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/episode/{episode}"
    try:
        r = requests.get(url, params={"api_key": TMDB_API_KEY}, timeout=2)
        return r.status_code == 200
    except: return True

def get_tmdb_last_aired(tmdb_id):
    if not TMDB_API_KEY: return None, None
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
    try:
        r = requests.get(url, params={"api_key": TMDB_API_KEY, "language": "fr-FR"}, timeout=2)
        data = r.json()
        last_ep = data.get('last_episode_to_air')
        if last_ep:
            return last_ep['season_number'], last_ep['episode_number']
    except: pass
    return None, None

def get_trakt_next_episode(tmdb_show_id):
    if not TRAKT_CLIENT_ID or not TRAKT_ACCESS_TOKEN:
        logger.warning("[TRAKT] Credentials manquants.")
        return None, None
    
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID,
        'Authorization': f'Bearer {TRAKT_ACCESS_TOKEN}'
    }
    try:
        # 1. Convert TMDB -> Trakt
        search_url = f"https://api.trakt.tv/search/tmdb/{tmdb_show_id}?type=show"
        r = requests.get(search_url, headers=headers, timeout=2)
        results = r.json()
        if not results: return None, None
        trakt_id = results[0]['show']['ids']['trakt']

        # 2. Get Progress
        progress_url = f"https://api.trakt.tv/shows/{trakt_id}/progress/watched"
        r = requests.get(progress_url, headers=headers, timeout=2)
        data = r.json()
        next_ep = data.get('next_episode')
        
        if next_ep:
            logger.info(f"[TRAKT] Next Up pour {tmdb_show_id} : S{next_ep['season']} E{next_ep['number']}")
            return next_ep['season'], next_ep['number']
        else:
            logger.info(f"[TRAKT] Pas de progression trouvée.")
    except Exception as e:
        logger.error(f"[TRAKT] Erreur API : {e}")
    return None, None

# --- URL BUILDER ---
def get_playback_url(tmdb_id, media_type, season=None, episode=None, force_select=False):
    p_def = PLAYER_DEFAULT if PLAYER_DEFAULT else "fenlight_auto.json"
    p_sel = PLAYER_SELECT if PLAYER_SELECT else "fenlight_select.json"
    target_player = p_sel if force_select else p_def
    
    base = "plugin://plugin.video.themoviedb.helper/?info=play"
    url = f"{base}&player={target_player}"
    
    if media_type == "movie":
        return f"{url}&tmdb_id={tmdb_id}&type=movie"
    elif media_type == "episode":
        return f"{url}&tmdb_id={tmdb_id}&season={season}&episode={episode}&type=episode"
    return None

def worker_process(plugin_url):
    """Exécute la lecture (Lancé dans un thread)."""
    logger.info(">>> DÉBUT PROCESSUS DE LECTURE")
    
    if not wake_and_start_kodi():
        logger.error(">>> ABANDON : Kodi injoignable.")
        return

    logger.info(f"[KODI] Envoi URL : {plugin_url}")
    
    payload = {"jsonrpc": "2.0", "method": "Player.Open", "params": {"item": {"file": plugin_url}}, "id": 1}
    try:
        auth = (KODI_USER, KODI_PASS) if KODI_USER and KODI_PASS else None
        r = requests.post(KODI_BASE_URL, json=payload, auth=auth, timeout=5)
        
        if r.status_code == 200:
            rpc_res = r.json()
            if 'error' in rpc_res:
                logger.error(f"[KODI] Erreur RPC : {rpc_res['error']}")
            else:
                logger.info(f"[KODI] Commande acceptée (Result: {rpc_res.get('result')})")
        else:
            logger.error(f"[KODI] Erreur HTTP : {r.status_code}")
            
    except Exception as e:
        logger.error(f"[KODI] Exception fatale lors de l'envoi : {e}")
        
    logger.info(">>> FIN PROCESSUS DE LECTURE")

# ==========================================
# 5. ROUTE FLASK
# ==========================================

@app.route('/alexa-webhook', methods=['POST'])
def alexa_handler():
    req_data = request.get_json()
    if not req_data or 'request' not in req_data:
        logger.error("Requête invalide reçue.")
        return jsonify({"error": "Invalid Request"}), 400

    req_type = req_data['request']['type']
    session = req_data.get('session', {})
    attributes = session.get('attributes', {})
    
    # Log d'entrée simple
    logger.info(f"Alexa Intent reçu : {req_type}")
    if DEBUG_MODE:
        logger.debug(f"PAYLOAD : {json.dumps(req_data)}")

    if req_type == "LaunchRequest":
        return jsonify(build_response("Votre Cinéma est ouvert. Que voulez-vous voir ?", end_session=False))

    if req_type == "IntentRequest":
        intent = req_data['request']['intent']
        intent_name = intent['name']
        slots = intent.get('slots', {})
        
        logger.info(f"Traitement Intent : {intent_name}")

        slot_source_mode = slots.get('SourceMode', {}).get('value')
        has_slot_force = True if slot_source_mode else False
        has_session_force = attributes.get('force_select', False)
        force_select = has_slot_force or has_session_force
        
        manual_msg = " avec sélection manuelle de la source" if force_select else ""
        
        if force_select: logger.info("Mode : SÉLECTION MANUELLE")

        # --- RESUME SHOW ---
        if intent_name == "ResumeTVShowIntent":
            query = slots.get('ShowName', {}).get('value')
            logger.info(f"Demande Reprise Série : {query}")
            
            if not query: return jsonify(build_response("Quelle série voulez-vous reprendre ?", end_session=False))

            tmdb_id, title = search_tmdb_show(query)
            if not tmdb_id: return jsonify(build_response(f"La série {query} est introuvable."))

            s, e = get_trakt_next_episode(tmdb_id)
            if s and e:
                url = get_playback_url(tmdb_id, "episode", s, e, force_select)
                threading.Thread(target=worker_process, args=(url,)).start()
                return jsonify(build_response(f"Reprise de la série {title} : saison {s}, épisode {e}{manual_msg}."))
            else:
                return jsonify(build_response(f"Pas de progression trouvée pour la série {title}.", end_session=False))

        # --- PLAY MOVIE ---
        elif intent_name == "PlayMovieIntent":
            query = slots.get('MovieName', {}).get('value')
            year_query = slots.get('MovieYear', {}).get('value')
            logger.info(f"Demande Film : {query} (Année: {year_query})")
            
            if not query: return jsonify(build_response("Quel film ?", end_session=False))
            
            movie_id, movie_title, movie_year = search_tmdb_movie(query, year=year_query)
            
            if movie_id:
                url = get_playback_url(movie_id, "movie", force_select=force_select)
                threading.Thread(target=worker_process, args=(url,)).start()
                txt = f"Je lance le film {movie_title}"
                if movie_year: txt += f" de {movie_year}"
                return jsonify(build_response(txt + f"{manual_msg}."))
            else:
                return jsonify(build_response(f"Je ne trouve pas le film {query}."))

        # --- PLAY SHOW ---
        elif intent_name == "PlayTVShowIntent":
            query = slots.get('ShowName', {}).get('value')
            season = slots.get('Season', {}).get('value')
            episode = slots.get('Episode', {}).get('value')
            logger.info(f"Demande Série : {query} (S{season}E{episode})")

            if not query and attributes.get('pending_show_id'):
                tmdb_id = attributes['pending_show_id']
                title = attributes['pending_show_name']
            elif query:
                tmdb_id, title = search_tmdb_show(query)
            else:
                return jsonify(build_response("Quelle série ?", end_session=False))

            if not tmdb_id: return jsonify(build_response(f"La série {query} est introuvable."))

            if season and episode:
                if check_episode_exists(tmdb_id, season, episode):
                    url = get_playback_url(tmdb_id, "episode", season, episode, force_select)
                    threading.Thread(target=worker_process, args=(url,)).start()
                    return jsonify(build_response(f"Lancement de la série {title}, saison {season}, Épisode {episode}{manual_msg}."))
                else:
                    return jsonify(build_response(f"Cet épisode n'existe pas.", end_session=False))
            else:
                trakt_s, trakt_e = get_trakt_next_episode(tmdb_id)
                tmdb_last_s, tmdb_last_e = get_tmdb_last_aired(tmdb_id)

                new_attr = {
                    "pending_show_id": tmdb_id, 
                    "pending_show_name": title,
                    "step": "ask_playback_method",
                    "force_select": force_select,
                    "trakt_next_s": trakt_s, "trakt_next_e": trakt_e,
                    "tmdb_last_s": tmdb_last_s, "tmdb_last_e": tmdb_last_e
                }

                if trakt_s:
                    msg = f"Pour la série {title}, voulez-vous reprendre à la saison {trakt_s} épisode {trakt_e} ? Voir le tout dernier épisode diffusé ? ou choisir un épisode ?"
                else:
                    msg = f"Je lance la série {title}. Voulez-vous voir le dernier épisode diffusé ou choisir un épisode ?"

                return jsonify(build_response(msg, end_session=False, attributes=new_attr))

        # --- RESPONSES ---
        elif intent_name in ["AMAZON.YesIntent", "ResumeIntent", "ReprendreIntent"]: 
            if attributes.get('step') == 'ask_playback_method':
                if attributes.get('trakt_next_s'):
                    s = attributes['trakt_next_s']
                    e = attributes['trakt_next_e']
                    title = attributes['pending_show_name']
                    url = get_playback_url(attributes['pending_show_id'], "episode", s, e, force_select)
                    threading.Thread(target=worker_process, args=(url,)).start()
                    manual_txt = " avec sélection manuelle de la source" if force_select else ""
                    return jsonify(build_response(f"Reprise de la série {title}, lecture de l'épisode {e} de la saison {s} {manual_txt}."))
                else:
                    return jsonify(build_response("Pas d'historique trouvé.", end_session=False))
            else:
                return jsonify(build_response("Je n'ai rien en attente."))

        elif intent_name == "LatestEpisodeIntent":
            if attributes.get('step') == 'ask_playback_method':
                if attributes.get('tmdb_last_s'):
                    s = attributes['tmdb_last_s']
                    e = attributes['tmdb_last_e']
                    title = attributes.get('pending_show_name', 'la série')
                    url = get_playback_url(attributes['pending_show_id'], "episode", s, e, force_select)
                    threading.Thread(target=worker_process, args=(url,)).start()
                    return jsonify(build_response(f"Je lance le dernier épisode de {title}."))
            return jsonify(build_response("Désolé, information indisponible."))

        elif intent_name in ["AMAZON.NoIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"]:
            return jsonify(build_response("Annulé."))

    return jsonify(build_response("Je n'ai pas compris."))

def build_response(text, end_session=True, attributes={}):
    response = {
        "version": "1.0",
        "sessionAttributes": attributes,
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end_session
        }
    }
    return response

# --- BANNER LOGGING ---
def print_startup_banner():
    masked_key = f"{TMDB_API_KEY[:4]}...{TMDB_API_KEY[-4:]}" if TMDB_API_KEY else "MISSING"
    masked_trakt = "Configured" if TRAKT_ACCESS_TOKEN else "MISSING"
    
    print("\n" + "="*50)
    print(f" KODI ALEXA CONTROLLER")
    print(f" Version : {APP_VERSION}")
    print(f" Debug   : {'ON' if DEBUG_MODE else 'OFF'}")
    print("="*50)
    print(f" [NET] Shield IP      : {SHIELD_IP if SHIELD_IP else 'MISSING'}")
    print(f" [NET] Kodi Endpoint  : {KODI_BASE_URL if KODI_BASE_URL else 'INVALID'}")
    print(f" [CFG] Player Auto    : {PLAYER_DEFAULT if PLAYER_DEFAULT else 'MISSING'}")
    print(f" [CFG] Player Select  : {PLAYER_SELECT if PLAYER_SELECT else 'MISSING'}")
    print(f" [API] TMDB Key       : {masked_key}")
    print(f" [API] Trakt Token    : {masked_trakt}")
    print(f" [SYS] Auto-Patcher   : ACTIVE (Interval: {PATCH_CHECK_INTERVAL}s)")
    print("="*50 + "\n")
    sys.stdout.flush()

if __name__ == '__main__':
    print_startup_banner()
    
    # Lancement du Patcher
    patcher_thread = threading.Thread(target=patcher_scheduler, daemon=True)
    patcher_thread.start()
    
    app.run(host='0.0.0.0', port=5000)
