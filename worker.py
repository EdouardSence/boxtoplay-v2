#!/usr/bin/env python3
"""
================================================================================
BoxToPlay Worker - Version Cloud (GitHub Codespaces)
================================================================================

Script de migration automatique entre comptes BoxToPlay.
Conçu pour tourner toutes les 8h sur GitHub Codespaces.

ARCHITECTURE STATELESS:
- Aucun fichier local persistant (pas de id.json, cookies.json)
- État stocké dans un GitHub Gist (base de données)
- Transferts FTP via /tmp (dossier temporaire)

PRÉREQUIS:
    pip install selenium requests
    sudo apt-get install -y lftp firefox-esr

CONFIGURATION (Variables d'environnement):
    - GIST_ID: ID du Gist GitHub contenant l'état
    - GH_TOKEN: Token GitHub avec permission "gist"
    - IP_NEW_SERVER: DNS du serveur (optionnel, défaut: "orny")
    - FTP_PASSWORD: Mot de passe FTP partagé (optionnel si dans le Gist)

STRUCTURE DU GIST (boxtoplay.json):
{
    "active_account_index": 0,
    "current_server_id": "123456",
    "ftp_password": "motdepasse",
    "accounts": [
        {
            "email": "compte1@example.com",
            "password": "password1",
            "cookies": {"BOXTOPLAY_SESSION": "..."},
            "ftp_host": "ftp1.boxtoplay.com",
            "ftp_user": "user_xxx",
            "server_id": "123456"
        },
        {
            "email": "compte2@example.com",
            "password": "password2",
            "cookies": {},
            "ftp_host": null,
            "ftp_user": null,
            "server_id": null
        }
    ]
}

================================================================================
"""

import traceback
import shutil
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import json
import os
import subprocess
import logging
import requests
from datetime import datetime, timedelta


# =============================================================================
# CONFIGURATION VIA VARIABLES D'ENVIRONNEMENT
# =============================================================================

GIST_ID = os.environ.get("GIST_ID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
IP_NEW_SERVER = os.environ.get("IP_NEW_SERVER", "orny")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "")

# User-Agent pour Selenium
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"

# Dossier temporaire pour les transferts FTP (utilise /tmp pour être stateless)
TEMP_DIR = "/tmp/boxtoplay_transfer"


# =============================================================================
# CONFIGURATION DU LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Console
        logging.FileHandler("/tmp/worker.log", mode='w')  # Fichier temporaire
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# SÉLECTEURS CSS/XPATH POUR BOXTOPLAY
# =============================================================================

SELECTORS = {
    "email_input": '//*[@id="emailLogin"]',
    "password_input": '//*[@id="passwordLogin"]',
    "login_button": '/html/body/div[1]/div[2]/form[1]/div[4]/div[2]/button',
    "cookies_accept": '/html/body/div[1]/div/div[3]/button[1]',
    "checkout": '//*[@id="livraisonBt"]',
    "gcu": '/html/body/div[2]/div/div[10]/button[1]',
    "add_ftp_account": '/html/body/div/div[2]/div[2]/div/div[2]/div[2]/div/a',
    "ftp_password": '//*[@id="password"]',
    "ftp_host": '/html/body/div/div[2]/div[2]/div/div[2]/div[1]/div/table/tbody/tr[2]/td[2]',
    "ftp_submit": '/html/body/div[1]/div[2]/div[2]/div/div[3]/div/div/form/div[3]/button[2]',
}


# =============================================================================
# GESTION DE L'ÉTAT VIA GITHUB GIST
# =============================================================================

def get_state() -> dict:
    """
    [GIST GET] Récupère l'état actuel depuis le Gist GitHub.
    
    L'état contient:
    - active_account_index: Index du compte actuellement actif (0 ou 1)
    - accounts: Liste des deux comptes avec leurs infos
    - current_server_id: ID du serveur en cours d'utilisation
    
    Returns:
        dict: État complet depuis le Gist
        
    Raises:
        ValueError: Si GIST_ID ou GH_TOKEN non définis
        requests.HTTPError: Si erreur API GitHub
    """
    if not GIST_ID or not GH_TOKEN:
        raise ValueError("❌ GIST_ID et GH_TOKEN doivent être définis dans les variables d'environnement!")
    
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    response = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers=headers,
        timeout=30
    )
    response.raise_for_status()
    
    gist_data = response.json()
    state_content = gist_data["files"]["boxtoplay.json"]["content"]
    state = json.loads(state_content)
    
    logger.info(f"📥 État récupéré - Compte actif: {state.get('active_account_index', 0)}")
    return state


def update_state(new_state: dict) -> None:
    """
    [GIST PATCH] Sauvegarde le nouvel état dans le Gist GitHub.
    
    Cette fonction est appelée à la fin du script pour persister:
    - Le nouvel active_account_index
    - Les nouveaux cookies de session
    - Les nouvelles infos FTP
    - L'ID du nouveau serveur
    
    Args:
        new_state: État complet à sauvegarder
        
    Raises:
        ValueError: Si GIST_ID ou GH_TOKEN non définis
        requests.HTTPError: Si erreur API GitHub
    """
    if not GIST_ID or not GH_TOKEN:
        raise ValueError("❌ GIST_ID et GH_TOKEN doivent être définis!")
    
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    payload = {
        "files": {
            "boxtoplay.json": {
                "content": json.dumps(new_state, indent=4, ensure_ascii=False)
            }
        }
    }
    
    response = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers=headers,
        json=payload,
        timeout=30
    )
    response.raise_for_status()
    
    logger.info("📤 État sauvegardé dans le Gist avec succès")


# =============================================================================
# DRIVER SELENIUM - MODE HEADLESS OBLIGATOIRE
# =============================================================================

def create_headless_driver() -> webdriver.Firefox:
    """
    Crée un driver Firefox en mode HEADLESS (obligatoire pour Codespaces).
    
    Options configurées:
    - --headless: Pas d'interface graphique
    - --no-sandbox: Nécessaire pour environnement conteneurisé
    - --disable-dev-shm-usage: Évite les problèmes de mémoire partagée
    
    Returns:
        webdriver.Firefox: Instance du driver configuré
    """
    options = Options()
    
    # MODE HEADLESS OBLIGATOIRE (pas d'écran sur Codespaces)
    options.add_argument("--headless")
    
    # Options pour environnement serveur Linux
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    # User-Agent personnalisé
    options.set_preference("general.useragent.override", USER_AGENT)
    
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(5)
    
    logger.info("🌐 Driver Firefox (headless) initialisé")
    return driver


def close_driver(driver: webdriver.Firefox) -> None:
    """Ferme proprement le driver Selenium."""
    try:
        driver.quit()
        logger.info("🔒 Driver fermé")
    except Exception as e:
        logger.warning(f"Erreur fermeture driver: {e}")


# =============================================================================
# FONCTIONS UTILITAIRES SELENIUM
# =============================================================================

def click_xpath(driver: webdriver.Firefox, xpath: str, timeout: int = 10) -> None:
    """Clique sur un élément identifié par son XPATH."""
    WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xpath))
    ).click()


def send_keys_xpath(driver: webdriver.Firefox, xpath: str, text: str, timeout: int = 10) -> None:
    """Saisit du texte dans un champ identifié par son XPATH."""
    element = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.XPATH, xpath))
    )
    element.clear()
    element.send_keys(text)


def get_text_xpath(driver: webdriver.Firefox, xpath: str, timeout: int = 10) -> str:
    """Récupère le texte d'un élément identifié par son XPATH."""
    element = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.XPATH, xpath))
    )
    return element.text


def safe_click(driver: webdriver.Firefox, xpath: str) -> bool:
    """Tente de cliquer sur un élément, retourne False si échec."""
    try:
        click_xpath(driver, xpath, timeout=3)
        return True
    except Exception:
        return False


def get_all_cookies(driver: webdriver.Firefox) -> dict:
    """Récupère tous les cookies de la session sous forme de dict."""
    cookies = driver.get_cookies()
    return {c['name']: c['value'] for c in cookies}


def get_session_cookie(driver: webdriver.Firefox) -> str:
    """Récupère uniquement le cookie BOXTOPLAY_SESSION."""
    cookies = driver.get_cookies()
    for c in cookies:
        if c['name'] == 'BOXTOPLAY_SESSION':
            return c['value']
    return ""


# =============================================================================
# AUTHENTIFICATION BOXTOPLAY
# =============================================================================

def login_with_cookie(driver: webdriver.Firefox, session_cookie: str) -> bool:
    """
    Tente de se connecter en utilisant un cookie de session existant.
    
    Args:
        driver: Instance Selenium
        session_cookie: Valeur du cookie BOXTOPLAY_SESSION
        
    Returns:
        bool: True si connexion réussie
    """
    if not session_cookie:
        return False
    
    try:
        # D'abord naviguer sur le domaine
        driver.get("https://www.boxtoplay.com/")
        time.sleep(1)
        
        # Injecter le cookie
        driver.add_cookie({
            "name": "BOXTOPLAY_SESSION",
            "value": session_cookie,
            "domain": "www.boxtoplay.com",
            "path": "/"
        })
        
        # Vérifier l'accès au panel
        driver.get("https://www.boxtoplay.com/panel")
        time.sleep(2)
        
        if "panel" in driver.current_url:
            logger.info("🔐 Connexion via cookie réussie")
            return True
        
        return False
        
    except Exception as e:
        logger.warning(f"Échec connexion cookie: {e}")
        return False


def login_with_credentials(driver: webdriver.Firefox, email: str, password: str) -> bool:
    """
    Connexion avec email et mot de passe.
    
    Args:
        driver: Instance Selenium
        email: Email du compte BoxToPlay
        password: Mot de passe du compte
        
    Returns:
        bool: True si connexion réussie
    """
    try:
        driver.get("https://www.boxtoplay.com/fr/login")
        time.sleep(2)
        
        # Accepter les cookies si présent
        safe_click(driver, SELECTORS["cookies_accept"])
        
        # Remplir le formulaire
        send_keys_xpath(driver, SELECTORS["email_input"], email)
        send_keys_xpath(driver, SELECTORS["password_input"], password)
        
        # Soumettre
        click_xpath(driver, SELECTORS["login_button"])
        time.sleep(3)
        
        # Vérifier la connexion
        driver.get("https://www.boxtoplay.com/panel")
        time.sleep(2)
        
        if "panel" in driver.current_url:
            logger.info(f"🔑 Connexion credentials réussie pour: {email}")
            return True
        
        logger.warning(f"⚠️ Échec connexion pour: {email}")
        return False
        
    except Exception as e:
        logger.error(f"Erreur connexion: {e}")
        return False


def logout(driver: webdriver.Firefox) -> None:
    """Déconnexion du compte BoxToPlay."""
    try:
        driver.get("https://www.boxtoplay.com/fr/login/logout")
        driver.delete_all_cookies()
        time.sleep(1)
        logger.info("🚪 Déconnexion effectuée")
    except Exception as e:
        logger.warning(f"Erreur déconnexion: {e}")


# =============================================================================
# GESTION DES SERVEURS BOXTOPLAY
# =============================================================================

def get_current_server_id(driver: webdriver.Firefox) -> str:
    """
    Récupère l'ID du serveur actuel depuis le panel.
    
    Returns:
        str: ID du serveur ou None si aucun serveur
    """
    try:
        driver.get("https://www.boxtoplay.com/panel")
        time.sleep(2)
        
        blocks = driver.find_elements(By.CSS_SELECTOR, 'body .block')
        nb_server = len(blocks)
        
        if nb_server == 0:
            logger.warning("Aucun serveur trouvé")
            return None
        
        # Récupère l'ID du serveur le plus récent
        xpath = f'/html/body/div/div[2]/div[2]/div/div[{nb_server*2}]/div[1]/h2/a/strong'
        server_text = get_text_xpath(driver, xpath)
        
        # Enlève le # devant l'ID
        server_id = server_text.lstrip('#')
        logger.info(f"🖥️ Serveur trouvé: #{server_id}")
        return server_id
        
    except Exception as e:
        logger.error(f"Erreur récupération serveur: {e}")
        return None


def buy_free_server(driver: webdriver.Firefox) -> bool:
    """
    Achète/active le serveur gratuit Leviathan.
    
    Returns:
        bool: True si achat réussi
    """
    try:
        # Page de location
        driver.get("https://www.boxtoplay.com/fr/serveur-minecraft/location-minecraft")
        time.sleep(2)
        
        # Accepter cookies si nécessaire
        safe_click(driver, SELECTORS["cookies_accept"])
        
        # Ajouter le serveur Leviathan (ID 12) au panier
        driver.get("https://www.boxtoplay.com/fr/cart/ajoutPanier/12")
        time.sleep(2)
        logger.info("🛒 Serveur Leviathan ajouté au panier")
        
        # Checkout
        click_xpath(driver, SELECTORS["checkout"])
        time.sleep(1)
        
        # Accepter CGU
        click_xpath(driver, SELECTORS["gcu"])
        time.sleep(1)
        
        logger.info("💳 Commande validée")
        
        # Attendre la création du serveur
        time.sleep(10)
        
        driver.get("https://www.boxtoplay.com/panel")
        return True
        
    except Exception as e:
        logger.error(f"Erreur achat serveur: {e}")
        return False


def change_server_dns(driver: webdriver.Firefox, server_id: str, dns_name: str) -> None:
    """
    Change le DNS/sous-domaine du serveur.
    
    Args:
        driver: Instance Selenium
        server_id: ID du serveur
        dns_name: Nouveau nom DNS (ex: "orny" pour orny.boxtoplay.com)
    """
    script = f'''
    fetch("https://www.boxtoplay.com/minecraft/setServerDNS", {{
        "credentials": "include",
        "headers": {{
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        }},
        "body": "name=&value={dns_name}&pk={server_id}",
        "method": "POST"
    }});
    '''
    driver.execute_script(script)
    logger.info(f"🔗 DNS changé vers: {dns_name}.boxtoplay.com")


def start_server(driver: webdriver.Firefox, server_id: str) -> None:
    """Démarre le serveur Minecraft."""
    driver.execute_script(f"window.open('https://www.boxtoplay.com/minecraft/start/{server_id}')")
    time.sleep(2)
    driver.switch_to.window(driver.window_handles[-1])
    driver.close()
    driver.switch_to.window(driver.window_handles[0])
    logger.info(f"▶️ Serveur #{server_id} démarré")


def stop_server(driver: webdriver.Firefox, server_id: str) -> None:
    """Arrête le serveur Minecraft."""
    driver.execute_script(f"window.open('https://www.boxtoplay.com/minecraft/stop/{server_id}')")
    time.sleep(2)
    driver.switch_to.window(driver.window_handles[-1])
    driver.close()
    driver.switch_to.window(driver.window_handles[0])
    logger.info(f"⏹️ Serveur #{server_id} arrêté")


def install_modpack(driver: webdriver.Firefox, server_id: str) -> None:
    """Installe le modpack par défaut sur le serveur."""
    url = f"https://www.boxtoplay.com/minecraft/modpacks/cursemodpacks/install/{server_id}?packVersionId=10517&mapReset=true&pluginsReset=true"
    driver.execute_script(f"window.open('{url}')")
    time.sleep(2)
    driver.switch_to.window(driver.window_handles[-1])
    driver.close()
    driver.switch_to.window(driver.window_handles[0])
    logger.info(f"📦 Modpack installé sur #{server_id}")


# =============================================================================
# GESTION FTP
# =============================================================================

def setup_ftp_account(driver: webdriver.Firefox, server_id: str, ftp_password: str) -> dict:
    """
    Configure un compte FTP sur le nouveau serveur.
    
    Args:
        driver: Instance Selenium
        server_id: ID du serveur
        ftp_password: Mot de passe pour le compte FTP
        
    Returns:
        dict: {"host": "...", "user": "...", "password": "..."}
    """
    try:
        driver.get(f"https://www.boxtoplay.com/minecraft/ftp/{server_id}")
        time.sleep(2)
        
        # Clic sur "Ajouter un compte"
        click_xpath(driver, SELECTORS["add_ftp_account"])
        time.sleep(1)
        
        # Récupère le host FTP
        ftp_host = get_text_xpath(driver, SELECTORS["ftp_host"])
        
        # Génère un nom d'utilisateur unique
        ftp_user = f"user_{int(time.time())}"
        
        # Remplit le formulaire
        WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, "div.form-group:nth-child(2) > div:nth-child(2) > input:nth-child(1)")
            )
        ).send_keys(ftp_user)
        
        send_keys_xpath(driver, SELECTORS["ftp_password"], ftp_password)
        click_xpath(driver, SELECTORS["ftp_submit"])
        
        time.sleep(2)
        
        result = {
            "host": ftp_host,
            "user": ftp_user,
            "password": ftp_password
        }
        
        logger.info(f"📁 Compte FTP créé: {ftp_user}@{ftp_host}")
        return result
        
    except Exception as e:
        logger.error(f"Erreur création FTP: {e}")
        raise


# =============================================================================
# TRANSFERT FTP VIA LFTP (Exécution locale sur Codespaces)
# =============================================================================

def ensure_temp_dir() -> str:
    """Crée et retourne le chemin du dossier temporaire."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    return TEMP_DIR


def cleanup_temp_dir() -> None:
    """Supprime le dossier temporaire et son contenu."""
    try:
        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)
            logger.info("🧹 Dossier temporaire nettoyé")
    except Exception as e:
        logger.warning(f"Erreur nettoyage: {e}")


def lftp_download(ftp_host: str, ftp_user: str, ftp_pass: str, 
                  remote_path: str, local_path: str) -> bool:
    """
    Télécharge des fichiers depuis un serveur FTP via LFTP.
    
    Exécute LFTP localement sur le Codespace (pas de gcloud).
    
    Args:
        ftp_host: Hôte FTP source
        ftp_user: Utilisateur FTP
        ftp_pass: Mot de passe FTP
        remote_path: Chemin distant à télécharger
        local_path: Chemin local de destination
        
    Returns:
        bool: True si succès
    """
    os.makedirs(local_path, exist_ok=True)
    
    # Commande LFTP avec mirror pour téléchargement récursif
    command = [
        'lftp',
        '-u', f'{ftp_user},{ftp_pass}',
        f'ftp://{ftp_host}',
        '-e', f'mirror --verbose --parallel=5 {remote_path} {local_path}; quit'
    ]
    
    try:
        logger.info(f"⬇️ Téléchargement: {ftp_host}:{remote_path} → {local_path}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        
        if result.returncode == 0:
            logger.info("✅ Téléchargement terminé")
            return True
        else:
            logger.error(f"Erreur LFTP: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("❌ Timeout téléchargement FTP")
        return False
    except Exception as e:
        logger.error(f"Erreur téléchargement: {e}")
        return False


def lftp_upload(ftp_host: str, ftp_user: str, ftp_pass: str,
                local_path: str, remote_path: str) -> bool:
    """
    Upload des fichiers vers un serveur FTP via LFTP.
    
    Args:
        ftp_host: Hôte FTP destination
        ftp_user: Utilisateur FTP
        ftp_pass: Mot de passe FTP
        local_path: Chemin local source
        remote_path: Chemin distant destination
        
    Returns:
        bool: True si succès
    """
    if not os.path.exists(local_path):
        logger.warning(f"⚠️ Chemin local inexistant: {local_path}")
        return False
    
    # Commande LFTP avec mirror --reverse pour upload
    command = [
        'lftp',
        '-u', f'{ftp_user},{ftp_pass}',
        f'ftp://{ftp_host}',
        '-e', f'mirror --reverse --verbose --parallel=5 {local_path} {remote_path}; quit'
    ]
    
    try:
        logger.info(f"⬆️ Upload: {local_path} → {ftp_host}:{remote_path}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        
        if result.returncode == 0:
            logger.info("✅ Upload terminé")
            return True
        else:
            logger.error(f"Erreur LFTP: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("❌ Timeout upload FTP")
        return False
    except Exception as e:
        logger.error(f"Erreur upload: {e}")
        return False


def transfer_world_data(source_ftp: dict, target_ftp: dict) -> bool:
    """
    Transfère les données du monde de l'ancien serveur vers le nouveau.
    
    Processus:
    1. Télécharge /world depuis le serveur source vers /tmp
    2. Upload /world vers le serveur cible
    3. Nettoie les fichiers temporaires
    
    Args:
        source_ftp: {"host": "...", "user": "...", "password": "..."}
        target_ftp: {"host": "...", "user": "...", "password": "..."}
        
    Returns:
        bool: True si transfert réussi
    """
    if not source_ftp or not source_ftp.get("host"):
        logger.info("⏭️ Pas de serveur source, skip du transfert")
        return True
    
    temp_dir = ensure_temp_dir()
    world_local = os.path.join(temp_dir, "world")
    
    try:
        # 1. Téléchargement depuis source
        logger.info("📥 Étape 1/3: Téléchargement du monde...")
        download_ok = lftp_download(
            source_ftp["host"],
            source_ftp["user"],
            source_ftp["password"],
            "/world",
            world_local
        )
        
        if not download_ok:
            logger.warning("⚠️ Échec téléchargement, le monde sera vierge")
            return True  # Continue quand même
        
        # 2. Upload vers cible
        logger.info("📤 Étape 2/3: Upload du monde...")
        upload_ok = lftp_upload(
            target_ftp["host"],
            target_ftp["user"],
            target_ftp["password"],
            world_local,
            "/world"
        )
        
        if not upload_ok:
            logger.error("❌ Échec upload du monde")
            return False
        
        # 3. Nettoyage
        logger.info("🧹 Étape 3/3: Nettoyage...")
        cleanup_temp_dir()
        
        logger.info("✅ Transfert du monde terminé avec succès")
        return True
        
    except Exception as e:
        logger.error(f"Erreur transfert monde: {e}")
        cleanup_temp_dir()
        return False


# =============================================================================
# LOGIQUE PRINCIPALE DE ROTATION
# =============================================================================

def process_active_account(driver: webdriver.Firefox, account: dict) -> dict:
    """
    Traite le compte ACTIF (celui qui va expirer).
    
    Actions:
    - Se connecter
    - Récupérer l'ID du serveur actuel
    - Vider le DNS du serveur
    - Arrêter le serveur
    - Retourner les infos FTP pour le transfert
    
    Args:
        driver: Instance Selenium
        account: Données du compte actif depuis le Gist
        
    Returns:
        dict: Infos FTP du serveur actif pour le transfert
    """
    logger.info(f"🔄 Traitement du compte ACTIF: {account['email']}")
    
    # Connexion (essaye d'abord avec le cookie)
    cookie = account.get("cookies", {}).get("BOXTOPLAY_SESSION", "")
    if not login_with_cookie(driver, cookie):
        if not login_with_credentials(driver, account["email"], account["password"]):
            logger.error("❌ Impossible de se connecter au compte actif")
            return None
    
    # Récupère l'ID du serveur
    server_id = get_current_server_id(driver)
    
    if server_id:
        # Vide le DNS (plus personne ne peut se connecter)
        change_server_dns(driver, server_id, "")
        time.sleep(1)
        
        # Arrête le serveur
        stop_server(driver, server_id)
    
    # Récupère les infos FTP
    ftp_info = {
        "host": account.get("ftp_host"),
        "user": account.get("ftp_user"),
        "password": FTP_PASSWORD or account.get("ftp_password", "")
    }
    
    logout(driver)
    return ftp_info


def process_target_account(driver: webdriver.Firefox, account: dict, ftp_password: str) -> dict:
    """
    Traite le compte CIBLE (celui qu'on active).
    
    Actions:
    - Se connecter
    - Acheter/activer le serveur gratuit
    - Configurer le DNS
    - Créer le compte FTP
    - Récupérer les cookies frais
    - Installer le modpack
    
    Args:
        driver: Instance Selenium
        account: Données du compte cible depuis le Gist
        ftp_password: Mot de passe FTP à utiliser
        
    Returns:
        dict: Nouvelles infos à sauvegarder dans le Gist
    """
    logger.info(f"🎯 Traitement du compte CIBLE: {account['email']}")
    
    # Connexion
    cookie = account.get("cookies", {}).get("BOXTOPLAY_SESSION", "")
    if not login_with_cookie(driver, cookie):
        if not login_with_credentials(driver, account["email"], account["password"]):
            raise Exception("Impossible de se connecter au compte cible")
    
    # Achat du serveur
    if not buy_free_server(driver):
        raise Exception("Échec de l'achat du serveur")
    
    # Récupère l'ID du nouveau serveur
    server_id = None
    for attempt in range(10):
        server_id = get_current_server_id(driver)
        if server_id:
            break
        logger.info(f"⏳ Attente du serveur... ({attempt+1}/10)")
        time.sleep(3)
    
    if not server_id:
        raise Exception("Serveur non trouvé après l'achat")
    
    # Configure le DNS
    change_server_dns(driver, server_id, IP_NEW_SERVER)
    time.sleep(1)
    
    # Configure le FTP
    ftp_info = setup_ftp_account(driver, server_id, ftp_password)
    
    # Installe le modpack
    install_modpack(driver, server_id)
    
    # Récupère les cookies frais (pour le bot Discord)
    fresh_cookies = get_all_cookies(driver)
    
    result = {
        "server_id": server_id,
        "ftp_host": ftp_info["host"],
        "ftp_user": ftp_info["user"],
        "ftp_password": ftp_password,
        "cookies": fresh_cookies
    }
    
    return result


def finalize_server(driver: webdriver.Firefox, account: dict, server_id: str) -> None:
    """
    Finalise le serveur (démarrage après transfert).
    
    Args:
        driver: Instance Selenium
        account: Données du compte avec cookies
        server_id: ID du serveur à démarrer
    """
    logger.info("🏁 Finalisation du serveur...")
    
    cookie = account.get("cookies", {}).get("BOXTOPLAY_SESSION", "")
    if login_with_cookie(driver, cookie):
        start_server(driver, server_id)
        logout(driver)


# =============================================================================
# MAIN - POINT D'ENTRÉE
# =============================================================================

def main():
    """
    Point d'entrée principal du worker.
    
    Workflow:
    1. Récupère l'état depuis le Gist
    2. Identifie le compte actif (qui expire) et le compte cible (à activer)
    3. Traite le compte actif (arrêt, récupération FTP)
    4. Traite le compte cible (achat, config FTP, cookies)
    5. Transfère le monde de l'ancien vers le nouveau serveur
    6. Démarre le nouveau serveur
    7. Sauvegarde le nouvel état dans le Gist (bascule d'index)
    """
    logger.info("=" * 70)
    logger.info("🚀 BOXTOPLAY WORKER - Démarrage")
    logger.info(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)
    
    # Vérification des variables d'environnement
    if not GIST_ID or not GH_TOKEN:
        logger.error("❌ Variables d'environnement manquantes!")
        logger.error("   Définissez: GIST_ID, GH_TOKEN")
        return 1
    
    driver = None
    
    try:
        # =================================================================
        # ÉTAPE 1: Récupération de l'état depuis le Gist
        # =================================================================
        logger.info("\n📋 ÉTAPE 1: Récupération de l'état")
        state = get_state()
        
        current_index = state.get("active_account_index", 0)
        next_index = 1 if current_index == 0 else 0
        
        active_account = state["accounts"][current_index]  # Celui qui expire
        target_account = state["accounts"][next_index]     # Celui qu'on active
        
        ftp_password = FTP_PASSWORD or state.get("ftp_password", "defaultpass")
        
        logger.info(f"🔄 Rotation: {active_account['email']} → {target_account['email']}")
        
        # =================================================================
        # ÉTAPE 2: Traitement du compte actif (arrêt de l'ancien serveur)
        # =================================================================
        logger.info("\n⏹️ ÉTAPE 2: Arrêt de l'ancien serveur")
        driver = create_headless_driver()
        
        source_ftp = None
        if active_account.get("ftp_host"):
            source_ftp = process_active_account(driver, active_account)
        else:
            logger.info("⏭️ Pas de serveur actif précédent")
        
        close_driver(driver)
        driver = None
        
        # =================================================================
        # ÉTAPE 3: Traitement du compte cible (achat nouveau serveur)
        # =================================================================
        logger.info("\n🎯 ÉTAPE 3: Activation du nouveau serveur")
        driver = create_headless_driver()
        
        target_result = process_target_account(driver, target_account, ftp_password)
        
        close_driver(driver)
        driver = None
        
        # =================================================================
        # ÉTAPE 4: Transfert FTP (ancien → nouveau)
        # =================================================================
        logger.info("\n📦 ÉTAPE 4: Transfert du monde")
        
        target_ftp = {
            "host": target_result["ftp_host"],
            "user": target_result["ftp_user"],
            "password": ftp_password
        }
        
        transfer_world_data(source_ftp, target_ftp)
        
        # =================================================================
        # ÉTAPE 5: Démarrage du nouveau serveur
        # =================================================================
        logger.info("\n▶️ ÉTAPE 5: Démarrage du serveur")
        driver = create_headless_driver()
        
        # Met à jour le compte avec les nouvelles infos pour la connexion
        target_account_updated = {**target_account, **target_result}
        finalize_server(driver, target_account_updated, target_result["server_id"])
        
        close_driver(driver)
        driver = None
        
        # =================================================================
        # ÉTAPE 6: Sauvegarde de l'état dans le Gist
        # =================================================================
        logger.info("\n💾 ÉTAPE 6: Sauvegarde de l'état")
        
        # Mise à jour de l'état
        state["active_account_index"] = next_index
        state["current_server_id"] = target_result["server_id"]
        
        # Mise à jour du compte cible
        state["accounts"][next_index].update({
            "cookies": target_result["cookies"],
            "ftp_host": target_result["ftp_host"],
            "ftp_user": target_result["ftp_user"],
            "server_id": target_result["server_id"]
        })
        
        update_state(state)
        
        # =================================================================
        # TERMINÉ
        # =================================================================
        logger.info("\n" + "=" * 70)
        logger.info("✅ MIGRATION TERMINÉE AVEC SUCCÈS!")
        logger.info(f"🖥️ Nouveau serveur actif: #{target_result['server_id']}")
        logger.info(f"🌐 DNS: {IP_NEW_SERVER}.boxtoplay.com")
        logger.info(f"👤 Compte actif: {target_account['email']}")
        logger.info("=" * 70)
        
        return 0
        
    except Exception as e:
        logger.error(f"\n❌ ERREUR FATALE: {e}")
        logger.error(traceback.format_exc())
        return 1
        
    finally:
        # Nettoyage
        if driver:
            close_driver(driver)
        cleanup_temp_dir()


# =============================================================================
# EXÉCUTION
# =============================================================================

if __name__ == "__main__":
    # Kill les processus Firefox orphelins (nettoyage)
    try:
        subprocess.run(["pkill", "-f", "firefox"], capture_output=True)
    except Exception:
        pass
    
    exit_code = main()
    exit(exit_code)
