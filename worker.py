#!/usr/bin/env python3
"""
================================================================================
BoxToPlay Worker - Version HTTP (Requests + BeautifulSoup)
================================================================================
"""

import os
import json
import time
import shutil
import logging
import requests
import subprocess
import re
from bs4 import BeautifulSoup
from datetime import datetime

# =============================================================================
# CONFIGURATION
# =============================================================================

GIST_ID = os.environ.get("GIST_ID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
IP_NEW_SERVER = os.environ.get("IP_NEW_SERVER", "orny")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "")
TEMP_DIR = "/tmp/boxtoplay_transfer"

# Headers pour imiter un vrai navigateur
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.boxtoplay.com",
    "Referer": "https://www.boxtoplay.com/panel"
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# SESSION HTTP
# =============================================================================

class BoxSession:
    def __init__(self, cookies_dict=None):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        if cookies_dict:
            # On nettoie le cookie s'il contient "BOXTOPLAY_SESSION="
            if "BOXTOPLAY_SESSION" in cookies_dict:
                val = cookies_dict["BOXTOPLAY_SESSION"]
                if val.startswith("BOXTOPLAY_SESSION="):
                    val = val.split("=")[1]
                self.session.cookies.set("BOXTOPLAY_SESSION", val, domain="www.boxtoplay.com")

    def is_logged_in(self):
        """Vérifie si la session est active en tentant d'accéder au panel"""
        r = self.session.get("https://www.boxtoplay.com/panel", allow_redirects=False)
        # Si redirection (302) vers /login, c'est qu'on est déco
        if r.status_code == 302 or "login" in r.headers.get("Location", ""):
            return False
        return r.status_code == 200

# =============================================================================
# GIST UTILS
# =============================================================================

def get_state():
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers)
    files = r.json()["files"]
    filename = list(files.keys())[0]
    return json.loads(files[filename]["content"])

def update_state(new_state):
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers)
    filename = list(r.json()["files"].keys())[0]
    payload = {"files": {filename: {"content": json.dumps(new_state, indent=4)}}}
    requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload)
    logger.info("💾 État sauvegardé dans le Gist.")

# =============================================================================
# ACTIONS BOXTOPLAY
# =============================================================================

def get_current_server_id(box: BoxSession):
    """Récupère l'ID du serveur depuis le dashboard"""
    r = box.session.get("https://www.boxtoplay.com/panel")
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Cherche le lien qui ressemble à /minecraft/dashboard/12345
    # Basé sur votre selecteur Selenium: .block h2 a strong
    try:
        blocks = soup.select('.block')
        if not blocks: return None
        
        # On prend le dernier bloc (le plus récent)
        last_block = blocks[-1]
        link = last_block.select_one('h2 a strong')
        if link:
            # Le texte est "#12345", on retire le #
            return link.text.replace('#', '').strip()
    except Exception as e:
        logger.error(f"Erreur parsing ID serveur: {e}")
    return None

def stop_server(box: BoxSession, server_id):
    logger.info(f"⏹️ Arrêt du serveur {server_id}...")
    # C'est un simple GET d'après votre script Selenium
    box.session.get(f"https://www.boxtoplay.com/minecraft/stop/{server_id}")

def change_dns(box: BoxSession, server_id, dns_name):
    logger.info(f"🔗 Changement DNS vers '{dns_name}'...")
    url = "https://www.boxtoplay.com/minecraft/setServerDNS"
    payload = {
        "name": "",
        "value": dns_name,
        "pk": server_id
    }
    # Headers AJAX obligatoires pour cette requête
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    }
    box.session.post(url, data=payload, headers=headers)

def buy_server_safe(box: BoxSession):
    """
    Tente d'acheter le serveur gratuit.
    SECURITÉ : Vérifie le prix avant de valider.
    """
    logger.info("🛒 Tentative ajout au panier (Leviathan)...")
    
    # 1. Ajout au panier (GET avec forceDuree=0 pour tenter le gratuit)
    # URL récupérée de vos logs : /fr/cart/ajoutPanier/12?forceDuree=0
    box.session.get("https://www.boxtoplay.com/fr/cart/ajoutPanier/12?forceDuree=0")
    
    # 2. Vérification du Panier (Parsing HTML)
    logger.info("🔍 Vérification du prix...")
    r = box.session.get("https://www.boxtoplay.com/fr/cart/basket")
    
    if "Rupture de stock" in r.text:
        logger.error("❌ OFFRE GRATUITE EN RUPTURE DE STOCK. Abandon.")
        empty_cart(box)
        return False

    soup = BeautifulSoup(r.text, 'html.parser')
    
    # On cherche le total. Selecteur basé sur votre HTML : .panier-summary-value (le dernier est le total)
    prices = soup.select(".panier-summary-value")
    if not prices:
        logger.error("❌ Impossible de lire le prix. Abandon par sécurité.")
        empty_cart(box)
        return False
        
    # Le dernier élément est le "Reste à payer" en rouge/vert
    total_str = prices[-1].text.strip().replace('€', '').replace(' ', '')
    
    try:
        total = float(total_str)
        if total > 0.00:
            logger.error(f"❌ DANGER : Le panier n'est pas gratuit ({total} €). Abandon immédiat.")
            empty_cart(box)
            return False
    except:
        logger.error(f"❌ Erreur lecture prix ({total_str}). Abandon.")
        empty_cart(box)
        return False
        
    # 3. Validation (Uniquement si gratuit)
    logger.info("✅ Panier gratuit (0.00 €). Validation...")
    
    # La validation est un POST sur /fr/cart/livraison
    # Pas de payload complexe d'après votre JS : $.ajax({url: "/fr/cart/livraison", type: "POST"})
    res = box.session.post("https://www.boxtoplay.com/fr/cart/livraison")
    
    if "Félicitations" in res.text or res.status_code == 200:
        logger.info("🎉 Serveur commandé avec succès !")
        time.sleep(10) # Temps de création
        return True
    else:
        logger.error("❌ Erreur lors de la validation finale.")
        return False

def empty_cart(box: BoxSession):
    """Vide le panier pour ne pas laisser de trucs payants"""
    logger.info("🗑️ Vidage du panier...")
    # URL de suppression trouvée dans votre HTML: /fr/cart/retirerPanier/0
    box.session.get("https://www.boxtoplay.com/fr/cart/retirerPanier/0")

def create_ftp_account(box: BoxSession, server_id, password):
    logger.info("📁 Création compte FTP...")
    url_page = f"https://www.boxtoplay.com/minecraft/ftp/{server_id}"
    
    # On génère un user unique
    ftp_user = f"user_{int(time.time())}"
    
    # Note : Je déduis ici les champs standards. 
    # SI ÇA PLANTE : Il faudra vérifier les 'name' des inputs sur la page FTP
    payload = {
        "username": ftp_user,     # Probable name="username" ou "login"
        "password": password,     # name="password" (vu dans votre xpath)
        "action": "create"        # Souvent un champ caché pour l'action
    }
    
    # On tente le POST sur la même URL (classique)
    box.session.post(url_page, data=payload)
    
    # On recharge la page pour scraper le HOST attribué
    r = box.session.get(url_page)
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Selecteur basé sur votre XPath : /html/body/div/div[2]/div[2]/div/div[2]/div[1]/div/table/tbody/tr[2]/td[2]
    # En CSS : table tr:nth-child(2) td:nth-child(2)
    try:
        host = soup.select_one('table tbody tr:nth-of-type(2) td:nth-of-type(2)').text.strip()
    except:
        logger.warning("⚠️ Impossible de lire le Host FTP. Utilisation du défaut.")
        host = "ftp.boxtoplay.com"
        
    return {"host": host, "user": ftp_user, "password": password}

def install_modpack(box: BoxSession, server_id):
    logger.info("📦 Installation Modpack...")
    # URL fournie dans votre script Selenium
    url = f"https://www.boxtoplay.com/minecraft/modpacks/cursemodpacks/install/{server_id}?packVersionId=10517&mapReset=true&pluginsReset=true"
    box.session.get(url)

def start_server(box: BoxSession, server_id):
    logger.info("▶️ Démarrage serveur...")
    box.session.get(f"https://www.boxtoplay.com/minecraft/start/{server_id}")

# =============================================================================
# TRANSFERT LFTP
# =============================================================================

def run_lftp(command_args):
    try:
        subprocess.run(['lftp'] + command_args, check=True, timeout=600)
    except Exception as e:
        logger.error(f"Erreur LFTP: {e}")

def transfer_world(source, target):
    if not source or not source.get('host'): return
    
    os.makedirs(TEMP_DIR, exist_ok=True)
    local_world = os.path.join(TEMP_DIR, "world")
    
    # 1. Download
    logger.info("📥 Téléchargement (Source -> Worker)...")
    cmd_down = [
        '-u', f"{source['user']},{source['password']}", 
        f"ftp://{source['host']}",
        '-e', f"mirror --verbose --parallel=10 --delete /world {local_world}; quit"
    ]
    run_lftp(cmd_down)
    
    # 2. Upload
    logger.info("📤 Envoi (Worker -> Cible)...")
    cmd_up = [
        '-u', f"{target['user']},{target['password']}", 
        f"ftp://{target['host']}",
        '-e', f"mirror --reverse --verbose --parallel=10 {local_world} /world; quit"
    ]
    run_lftp(cmd_up)
    
    shutil.rmtree(TEMP_DIR, ignore_errors=True)

# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("🚀 WORKER HTTP START")
    state = get_state()
    
    current_idx = state.get("active_account_index", 0)
    next_idx = 1 if current_idx == 0 else 0
    
    acc_active = state["accounts"][current_idx]
    acc_target = state["accounts"][next_idx]
    common_pass = state.get("ftp_password", "Password123")
    
    # --- 1. GESTION COMPTE ACTIF (Désactivation) ---
    logger.info(f"🔄 Désactivation du compte : {acc_active['email']}")
    box_active = BoxSession(acc_active.get("cookies"))
    
    if box_active.is_logged_in():
        srv_id = get_current_server_id(box_active)
        if srv_id:
            change_dns(box_active, srv_id, "") # Libération DNS
            stop_server(box_active, srv_id)
    else:
        logger.warning("⚠️ Session active expirée. Impossible d'arrêter proprement.")

    # Infos FTP pour le transfert (depuis le Gist)
    ftp_source = {
        "host": acc_active.get("ftp_host"),
        "user": acc_active.get("ftp_user"),
        "password": common_pass
    }
    
    # --- 2. GESTION COMPTE CIBLE (Activation) ---
    logger.info(f"🎯 Activation du compte : {acc_target['email']}")
    box_target = BoxSession(acc_target.get("cookies"))
    
    if not box_target.is_logged_in():
        raise Exception("❌ CRITIQUE : La session du compte cible est expirée. Le Bot Discord n'a pas fait son travail.")
    
    # Achat sécurisé
    if not buy_server_safe(box_target):
        logger.warning("⚠️ Achat non réalisé (Serveur déjà là ou rupture de stock).")
    
    # Récupération ID
    srv_id_target = None
    for _ in range(5):
        srv_id_target = get_current_server_id(box_target)
        if srv_id_target: break
        time.sleep(2)
        
    if not srv_id_target:
        raise Exception("❌ Impossible de trouver le nouveau serveur.")
    
    # Config
    change_dns(box_target, srv_id_target, IP_NEW_SERVER)
    ftp_target = create_ftp_account(box_target, srv_id_target, common_pass)
    install_modpack(box_target, srv_id_target)
    
    # --- 3. TRANSFERT & START ---
    transfer_world(ftp_source, ftp_target)
    start_server(box_target, srv_id_target)
    
    # --- 4. SAUVEGARDE ---
    state["active_account_index"] = next_idx
    state["current_server_id"] = srv_id_target
    state["accounts"][next_idx]["ftp_host"] = ftp_target["host"]
    state["accounts"][next_idx]["ftp_user"] = ftp_target["user"]
    
    update_state(state)
    logger.info("✅ MIGRATION TERMINÉE")

if __name__ == "__main__":
    main()