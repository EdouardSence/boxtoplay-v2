#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

# Headers copiés de vos logs et fichiers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
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
            # Nettoyage et injection du cookie
            if "BOXTOPLAY_SESSION" in cookies_dict:
                val = cookies_dict["BOXTOPLAY_SESSION"]
                # Si le json contient toute la chaîne "BOXTOPLAY_SESSION=...", on coupe
                if "=" in val:
                    val = val.split("=")[-1]
                self.session.cookies.set("BOXTOPLAY_SESSION", val, domain="www.boxtoplay.com")

    def is_logged_in(self):
        """Vérifie si la session est active en regardant le contenu de la page"""
        try:
            r = self.session.get("https://www.boxtoplay.com/panel", allow_redirects=False)
            # Si redirection 302 vers login, c'est mort
            if r.status_code == 302:
                return False
            # Vérification supplémentaire : Si on est redirigé vers /fr/login
            if "login" in r.headers.get("Location", ""):
                return False
            # Si on a un code 200, on vérifie qu'on n'est pas sur la page de login déguisée
            if "Se connecter" in r.text and "Mot de passe oublié" in r.text:
                return False
            return True
        except Exception:
            return False

# =============================================================================
# GIST UTILS
# =============================================================================

def get_state():
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers)
    r.raise_for_status()
    files = r.json()["files"]
    # On prend le premier fichier peu importe son nom
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
    """Scrape l'ID du serveur depuis le panel"""
    r = box.session.get("https://www.boxtoplay.com/panel")
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Sélecteur robuste basé sur votre historique
    try:
        # Cherche tous les blocs serveurs
        blocks = soup.select('.block')
        if not blocks: return None
        
        # Le dernier bloc est généralement le plus récent
        last_block = blocks[-1]
        link = last_block.select_one('h2 a strong')
        if link:
            return link.text.replace('#', '').strip()
    except Exception:
        pass
    return None

def stop_server(box: BoxSession, server_id):
    logger.info(f"⏹️ Arrêt du serveur {server_id}...")
    # GET simple suffisant
    box.session.get(f"https://www.boxtoplay.com/minecraft/stop/{server_id}")

def change_dns(box: BoxSession, server_id, dns_name):
    logger.info(f"🔗 Changement DNS vers '{dns_name}'...")
    url = "https://www.boxtoplay.com/minecraft/setServerDNS"
    payload = {"name": "", "value": dns_name, "pk": server_id}
    headers = {"X-Requested-With": "XMLHttpRequest"} # Important pour Ajax
    box.session.post(url, data=payload, headers=headers)

def buy_server_safe(box: BoxSession):
    """
    Achète le serveur en vérifiant scrupuleusement le HTML du panier.
    Basé sur le fichier boxtoplay.html fourni.
    """
    logger.info("🛒 Tentative ajout au panier...")
    
    # 1. Ajout au panier (GET)
    box.session.get("https://www.boxtoplay.com/fr/cart/ajoutPanier/12?forceDuree=0")
    
    # 2. Analyse du Panier
    logger.info("🔍 Analyse du panier...")
    r = box.session.get("https://www.boxtoplay.com/fr/cart/basket")
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Vérification Rupture de stock (Texte présent dans boxtoplay.html)
    if "Rupture de stock" in r.text:
        logger.error("❌ RUPTURE DE STOCK DÉTECTÉE. Abandon.")
        empty_cart(box)
        return False

    # Vérification du Prix
    # Dans boxtoplay.html, le total est dans le dernier div avec la classe .panier-summary-value
    summary_values = soup.select(".panier-summary-value")
    if not summary_values:
        logger.error("❌ Impossible de lire le prix (Structure HTML changée ?). Abandon.")
        empty_cart(box)
        return False
    
    # Le dernier élément est le "Reste à payer"
    total_text = summary_values[-1].text.strip() # ex: "29.99 €" ou "0.00 €"
    logger.info(f"💰 Montant détecté : {total_text}")
    
    # Nettoyage du prix pour conversion float
    price_clean = total_text.replace('€', '').replace(' ', '').replace(',', '.')
    
    try:
        price = float(price_clean)
        if price > 0.00:
            logger.error(f"❌ PANIER PAYANT ({price} €) ! ABANDON IMMÉDIAT.")
            empty_cart(box)
            return False
    except ValueError:
        logger.error(f"❌ Erreur conversion prix. Sécurité activée. Abandon.")
        empty_cart(box)
        return False
        
    # 3. Validation (Si gratuit uniquement)
    logger.info("✅ Panier gratuit valide. Confirmation...")
    
    # URL AJAX trouvée dans le script de boxtoplay.html : $.ajax({ url: "/fr/cart/livraison", type: "POST" })
    # Headers Ajax nécessaires
    headers = {"X-Requested-With": "XMLHttpRequest"}
    res = box.session.post("https://www.boxtoplay.com/fr/cart/livraison", headers=headers)
    
    # Si succès, boxtoplay renvoie souvent du HTML partiel ou redirige
    if res.status_code == 200:
        logger.info("🎉 Commande envoyée ! Attente de création...")
        time.sleep(15) 
        return True
    else:
        logger.error(f"❌ Erreur validation: {res.status_code}")
        return False

def empty_cart(box: BoxSession):
    logger.info("🗑️ Vidage panier...")
    # URL de suppression standard
    box.session.get("https://www.boxtoplay.com/fr/cart/retirerPanier/0")

def create_ftp_account(box: BoxSession, server_id, password):
    logger.info("📁 Création compte FTP...")
    url = f"https://www.boxtoplay.com/minecraft/ftp/{server_id}"
    
    ftp_user = f"user_{int(time.time())}"
    # Payload standard pour ce formulaire
    payload = {
        "username": ftp_user,
        "password": password,
        "action": "create" # Souvent requis
    }
    
    box.session.post(url, data=payload)
    
    # Récupération du host
    r = box.session.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')
    try:
        # Tableau FTP, 2ème colonne
        host = soup.select_one('table tbody tr:nth-of-type(2) td:nth-of-type(2)').text.strip()
    except:
        host = "ftp.boxtoplay.com"
        
    return {"host": host, "user": ftp_user, "password": password}

def install_modpack(box: BoxSession, server_id):
    logger.info("📦 Installation Modpack...")
    url = f"https://www.boxtoplay.com/minecraft/modpacks/cursemodpacks/install/{server_id}?packVersionId=10517&mapReset=true&pluginsReset=true"
    box.session.get(url)

def start_server(box: BoxSession, server_id):
    logger.info("▶️ Démarrage...")
    box.session.get(f"https://www.boxtoplay.com/minecraft/start/{server_id}")

# =============================================================================
# TRANSFERT
# =============================================================================

def run_lftp(args):
    try:
        subprocess.run(['lftp'] + args, check=True, timeout=900)
    except Exception as e:
        logger.error(f"Erreur LFTP: {e}")

def transfer_world(source, target):
    if not source or not source.get('host'): return
    os.makedirs(TEMP_DIR, exist_ok=True)
    local = os.path.join(TEMP_DIR, "world")
    
    logger.info("📥 Téléchargement...")
    # --delete permet de nettoyer le dossier local si besoin, mirror simple suffit ici
    run_lftp(['-u', f"{source['user']},{source['password']}", f"ftp://{source['host']}", 
              '-e', f"mirror --verbose --parallel=10 /world {local}; quit"])
              
    logger.info("📤 Envoi...")
    run_lftp(['-u', f"{target['user']},{target['password']}", f"ftp://{target['host']}",
              '-e', f"mirror --reverse --verbose --parallel=10 {local} /world; quit"])
              
    shutil.rmtree(TEMP_DIR, ignore_errors=True)

# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("🚀 WORKER START")
    state = get_state()
    
    current_idx = state.get("active_account_index", 0)
    next_idx = 1 if current_idx == 0 else 0
    
    acc_active = state["accounts"][current_idx]
    acc_target = state["accounts"][next_idx]
    common_pass = state.get("ftp_password", "Password123")
    
    # 1. Check Active Account
    logger.info(f"🔄 Check compte actif : {acc_active['email']}")
    box_active = BoxSession(acc_active.get("cookies"))
    
    ftp_source = {
        "host": acc_active.get("ftp_host"),
        "user": acc_active.get("ftp_user"),
        "password": common_pass
    }
    
    if box_active.is_logged_in():
        srv_id = get_current_server_id(box_active)
        if srv_id:
            change_dns(box_active, srv_id, "")
            stop_server(box_active, srv_id)
    else:
        logger.warning("⚠️ Session active expirée. Impossible d'arrêter proprement.")

    # 2. Activate Target Account
    logger.info(f"🎯 Activation compte cible : {acc_target['email']}")
    box_target = BoxSession(acc_target.get("cookies"))
    
    if not box_target.is_logged_in():
        # C'est ici que ça plante. Le bot doit empêcher ça.
        raise Exception("❌ CRITIQUE : Session cible expirée. Mettez à jour le Gist manuellement !")
        
    if not buy_server_safe(box_target):
        logger.warning("⚠️ Achat non réalisé (Pas de stock ou déjà présent).")
        
    srv_id_target = None
    for _ in range(5):
        srv_id_target = get_current_server_id(box_target)
        if srv_id_target: break
        time.sleep(2)
        
    if not srv_id_target:
        raise Exception("❌ Serveur introuvable après achat.")
        
    change_dns(box_target, srv_id_target, IP_NEW_SERVER)
    ftp_target = create_ftp_account(box_target, srv_id_target, common_pass)
    install_modpack(box_target, srv_id_target)
    
    # 3. Transfer & Start
    transfer_world(ftp_source, ftp_target)
    start_server(box_target, srv_id_target)
    
    # 4. Save
    state["active_account_index"] = next_idx
    state["current_server_id"] = srv_id_target
    state["accounts"][next_idx]["ftp_host"] = ftp_target["host"]
    state["accounts"][next_idx]["ftp_user"] = ftp_target["user"]
    # On ne met à jour les cookies QUE si le serveur en a renvoyé de nouveaux dans les headers
    # Requests le gère automatiquement via session.cookies
    state["accounts"][next_idx]["cookies"] = box_target.session.cookies.get_dict()
    
    update_state(state)
    logger.info("✅ SUCCESS")

if __name__ == "__main__":
    main()