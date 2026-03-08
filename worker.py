#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BoxToPlay Server Rotation Worker

Utilise Playwright pour bypass Cloudflare et effectuer la rotation
des serveurs Minecraft entre deux comptes BoxToPlay.

Workflow:
  1. Arreter l'ancien serveur (compte actif)
  2. Acheter un nouveau serveur (compte cible)
  3. Configurer DNS, FTP, modpack
  4. Transferer le monde via lftp
  5. Demarrer le nouveau serveur
  6. Sauvegarder le state dans le Gist
"""
import os
import json
import time
import shutil
import logging
import asyncio
import subprocess
import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

# =============================================================================
# CONFIGURATION
# =============================================================================

GIST_ID = os.environ.get("GIST_ID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
IP_NEW_SERVER = os.environ.get("IP_NEW_SERVER", "orny")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "Password123")
MODPACK_VERSION_ID = os.environ.get("MODPACK_VERSION_ID", "1521")
MODPACK_NAME = os.environ.get("MODPACK_NAME", "Star Technology")
TEMP_DIR = "/tmp/boxtoplay_transfer"
SCREENSHOT_DIR = "/tmp/boxtoplay_screenshots"

URLS = {
    "login": "https://www.boxtoplay.com/fr/login",
    "panel": "https://www.boxtoplay.com/panel",
    "stop": "https://www.boxtoplay.com/minecraft/stop/{server_id}",
    "start": "https://www.boxtoplay.com/minecraft/start/{server_id}",
    "dns": "https://www.boxtoplay.com/minecraft/setServerDNS",
    "cart_add": "https://www.boxtoplay.com/fr/cart/ajoutPanier/12?forceDuree=0",
    "cart_basket": "https://www.boxtoplay.com/fr/cart/basket",
    "cart_checkout": "https://www.boxtoplay.com/fr/cart/livraison",
    "cart_remove": "https://www.boxtoplay.com/fr/cart/retirerPanier/0",
    "ftp": "https://www.boxtoplay.com/minecraft/ftp/{server_id}",
    "modpack": "https://www.boxtoplay.com/minecraft/modpacks/cursemodpacks/install/{server_id}?packVersionId={pack_id}&mapReset=true&pluginsReset=true",
    "gist": "https://api.github.com/gists/{gist_id}",
}

CLOUDFLARE_TITLE = "Just a moment"
CLOUDFLARE_TIMEOUT = 30000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

stealth = Stealth()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# =============================================================================
# GIST STATE MANAGEMENT
# =============================================================================

def get_state():
    """Charge le state depuis le Gist GitHub. Retourne (state, filename)."""
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(URLS["gist"].format(gist_id=GIST_ID), headers=headers)
    r.raise_for_status()
    files = r.json()["files"]
    filename = list(files.keys())[0]
    state = json.loads(files[filename]["content"])
    return state, filename


def update_state(new_state, filename):
    """Sauvegarde le state dans le Gist GitHub."""
    headers = {"Authorization": f"token {GH_TOKEN}"}
    payload = {"files": {filename: {"content": json.dumps(new_state, indent=4)}}}
    r = requests.patch(URLS["gist"].format(gist_id=GIST_ID), headers=headers, json=payload)
    r.raise_for_status()
    logger.info("State sauvegarde dans le Gist.")

# =============================================================================
# BOXTOPLAY WORKER (Playwright)
# =============================================================================

class BoxToPlayWorker:
    """Automatise les interactions avec BoxToPlay via Playwright."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        """Lance le navigateur Playwright avec stealth."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=USER_AGENT,
        )
        self.page = await self.context.new_page()
        await stealth.apply_stealth_async(self.page)
        logger.info("Navigateur Playwright lance.")

    async def close(self):
        """Ferme le navigateur proprement."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Navigateur ferme.")

    async def _solve_cloudflare(self):
        """Attend que le challenge Cloudflare soit resolu."""
        title = await self.page.title()
        if CLOUDFLARE_TITLE not in title:
            return

        logger.info("Challenge Cloudflare detecte, attente...")
        try:
            await self.page.wait_for_function(
                "() => !document.title.includes('Just a moment')",
                timeout=CLOUDFLARE_TIMEOUT,
            )
            await self.page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            await self._screenshot("cloudflare_timeout")
            raise Exception("Cloudflare challenge non resolu (timeout 30s)")

        final_title = await self.page.title()
        if CLOUDFLARE_TITLE in final_title:
            await self._screenshot("cloudflare_stuck")
            raise Exception(f"Cloudflare toujours present: {final_title}")
        logger.info(f"Cloudflare resolu. Page: {final_title}")

    async def _new_session(self):
        """Cree une nouvelle page (utile pour changer de compte)."""
        if self.page:
            await self.page.close()
        # Vider tous les cookies pour une session propre
        await self.context.clear_cookies()
        self.page = await self.context.new_page()
        await stealth.apply_stealth_async(self.page)

    async def _screenshot(self, name):
        """Sauvegarde un screenshot pour debug."""
        try:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
            await self.page.screenshot(path=path, full_page=True)
            logger.info(f"Screenshot sauve: {path}")
        except Exception as e:
            logger.warning(f"Screenshot echoue: {e}")

    # ----- Actions BoxToPlay -----

    async def login(self, email, password):
        """Se connecte a BoxToPlay avec email/mot de passe."""
        logger.info(f"Connexion: {email}...")

        await self.page.goto(URLS["login"], wait_until="networkidle", timeout=60000)
        await self._solve_cloudflare()

        # Screenshot apres chargement pour debug
        await self._screenshot(f"login_loaded_{email.split('@')[0]}")

        # Fermer les overlays potentiels (cookie consent, popups)
        for selector in [
            'button:has-text("Accepter")',
            'button:has-text("Accept")',
            'button:has-text("OK")',
            '.cookie-consent-accept',
            '[data-dismiss="modal"]',
        ]:
            try:
                btn = self.page.locator(selector).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    logger.info(f"Overlay ferme: {selector}")
                    await self.page.wait_for_timeout(1000)
            except Exception:
                pass

        # Remplir le formulaire de login
        # BoxToPlay utilise _username/_password ou email/password selon la version
        email_input = self.page.locator(
            'input[name="_username"], input[name="email"], input[type="email"]'
        ).first
        password_input = self.page.locator(
            'input[name="_password"], input[name="password"], input[type="password"]'
        ).first

        # Attendre que l'input soit visible (max 10s)
        try:
            await email_input.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeout:
            await self._screenshot(f"login_input_not_visible_{email.split('@')[0]}")
            # Essayer de scroller vers l'element
            try:
                await email_input.scroll_into_view_if_needed()
                await self.page.wait_for_timeout(1000)
            except Exception:
                pass
            # Tenter quand meme si ca echoue
            logger.warning("Input email non visible apres 10s, tentative quand meme...")

        await email_input.fill(email)
        await password_input.fill(password)

        # Soumettre
        submit_btn = self.page.locator(
            'button[type="submit"], input[type="submit"]'
        ).first
        await submit_btn.click()

        # Attendre la redirection vers le panel
        try:
            await self.page.wait_for_url("**/panel**", timeout=15000)
        except PlaywrightTimeout:
            current_url = self.page.url
            page_text = await self.page.text_content("body") or ""
            await self._screenshot(f"login_failed_{email.split('@')[0]}")
            if "login" in current_url or "Se connecter" in page_text:
                raise Exception(f"Echec connexion pour {email} (identifiants invalides ?)")
            logger.warning(f"Redirect inattendu apres login: {current_url}")

        logger.info(f"Connecte: {email}")

    async def get_server_id(self):
        """Recupere l'ID du serveur depuis le panel."""
        await self.page.goto(URLS["panel"], wait_until="networkidle", timeout=30000)
        await self._solve_cloudflare()

        server_id = await self.page.evaluate("""() => {
            // Methode 1: blocs .block avec h2 > a > strong (#ID)
            const blocks = document.querySelectorAll('.block');
            if (blocks.length > 0) {
                const lastBlock = blocks[blocks.length - 1];
                const strong = lastBlock.querySelector('h2 a strong');
                if (strong) {
                    return strong.textContent.replace('#', '').trim();
                }
            }
            // Methode 2: liens contenant /minecraft/
            const links = document.querySelectorAll('a[href*="/minecraft/"]');
            for (const link of links) {
                const match = link.href.match(/\\/minecraft\\/\\w+\\/(\\d+)/);
                if (match) return match[1];
            }
            // Methode 3: texte "Serveur #XXXX"
            const body = document.body.textContent;
            const match = body.match(/Serveur\\s*#(\\d+)/i);
            if (match) return match[1];
            return null;
        }""")

        if server_id:
            logger.info(f"Server ID: {server_id}")
        else:
            logger.warning("Aucun serveur trouve sur le panel.")
        return server_id

    async def stop_server(self, server_id):
        """Arrete le serveur."""
        logger.info(f"Arret serveur {server_id}...")
        response = await self.page.goto(
            URLS["stop"].format(server_id=server_id),
            wait_until="networkidle",
            timeout=30000,
        )
        status = response.status if response else 0
        if status == 200:
            logger.info(f"Serveur {server_id} arrete.")
        else:
            logger.warning(f"Arret serveur: HTTP {status}")

    async def change_dns(self, server_id, dns_name):
        """Change le DNS du serveur via appel AJAX."""
        logger.info(f"Changement DNS -> '{dns_name}'...")

        # S'assurer qu'on est sur le bon domaine pour le fetch
        if "boxtoplay.com" not in self.page.url:
            await self.page.goto(URLS["panel"], wait_until="networkidle", timeout=15000)

        result = await self.page.evaluate(
            """async ({server_id, dns_name}) => {
            try {
                const response = await fetch('/minecraft/setServerDNS', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: 'name=&value=' + encodeURIComponent(dns_name) + '&pk=' + server_id
                });
                return {status: response.status, ok: response.ok};
            } catch (e) {
                return {status: 0, error: e.message};
            }
        }""",
            {"server_id": str(server_id), "dns_name": dns_name},
        )

        if result.get("ok"):
            logger.info(f"DNS mis a jour: '{dns_name}'")
        else:
            logger.warning(f"DNS: HTTP {result.get('status')} - {result.get('error', '')}")

    async def empty_cart(self):
        """Vide le panier."""
        logger.info("Vidage du panier...")
        await self.page.goto(URLS["cart_remove"], wait_until="networkidle", timeout=15000)

    async def buy_server(self):
        """Achete un serveur gratuit avec verification stricte du prix."""
        logger.info("--- Achat serveur ---")

        # 1. Ajouter au panier
        logger.info("Ajout au panier...")
        await self.page.goto(URLS["cart_add"], wait_until="networkidle", timeout=30000)
        await self._solve_cloudflare()

        # 2. Verifier le panier
        logger.info("Verification du panier...")
        await self.page.goto(URLS["cart_basket"], wait_until="networkidle", timeout=30000)

        page_text = await self.page.text_content("body") or ""

        # Verifier rupture de stock
        if "Rupture de stock" in page_text:
            logger.error("RUPTURE DE STOCK. Abandon.")
            await self.empty_cart()
            return False

        # Verifier le prix
        price_text = await self.page.evaluate("""() => {
            const values = document.querySelectorAll('.panier-summary-value');
            if (values.length > 0) {
                return values[values.length - 1].textContent.trim();
            }
            return null;
        }""")

        if not price_text:
            logger.error("Impossible de lire le prix (structure HTML changee ?). Abandon.")
            await self.empty_cart()
            return False

        logger.info(f"Montant detecte: {price_text}")

        price_clean = price_text.replace("€", "").replace(" ", "").replace(",", ".").strip()
        try:
            price = float(price_clean)
            if price > 0.00:
                logger.error(f"PANIER PAYANT ({price} EUR). ABANDON IMMEDIAT.")
                await self.empty_cart()
                return False
        except ValueError:
            logger.error(f"Erreur conversion prix '{price_clean}'. Abandon.")
            await self.empty_cart()
            return False

        # 3. Valider la commande
        logger.info("Panier gratuit confirme. Validation de la commande...")

        result = await self.page.evaluate("""async () => {
            try {
                const response = await fetch('/fr/cart/livraison', {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                });
                return {status: response.status, ok: response.ok};
            } catch (e) {
                return {status: 0, error: e.message};
            }
        }""")

        if result.get("status") == 200:
            logger.info("Commande validee ! Attente de creation du serveur (15s)...")
            await self.page.wait_for_timeout(15000)
            return True
        else:
            logger.error(f"Erreur validation: {result}")
            return False

    async def create_ftp_account(self, server_id, password):
        """Cree un compte FTP sur le serveur."""
        logger.info(f"Creation compte FTP (serveur {server_id})...")

        ftp_user = f"user_{int(time.time())}"
        url = URLS["ftp"].format(server_id=server_id)

        await self.page.goto(url, wait_until="networkidle", timeout=30000)
        await self._solve_cloudflare()

        # Remplir le formulaire FTP
        try:
            username_input = self.page.locator('input[name="username"]').first
            password_input = self.page.locator('input[name="password"]').first

            await username_input.fill(ftp_user)
            await password_input.fill(password)

            submit = self.page.locator(
                'button[type="submit"], input[type="submit"]'
            ).first
            await submit.click()
            await self.page.wait_for_load_state("networkidle")
        except Exception as e:
            logger.warning(f"Formulaire FTP non trouve, tentative POST direct: {e}")
            await self.page.evaluate(
                """async ({server_id, user, password}) => {
                const formData = new URLSearchParams();
                formData.append('username', user);
                formData.append('password', password);
                formData.append('action', 'create');
                await fetch('/minecraft/ftp/' + server_id, {
                    method: 'POST',
                    body: formData
                });
            }""",
                {"server_id": str(server_id), "user": ftp_user, "password": password},
            )
            await self.page.wait_for_timeout(3000)

        # Recharger la page pour recuperer le host FTP
        await self.page.goto(url, wait_until="networkidle", timeout=15000)

        host = await self.page.evaluate("""() => {
            const cells = document.querySelectorAll('table td');
            for (const cell of cells) {
                const text = cell.textContent.trim();
                if (text.includes('ftp.') || text.includes('mc-')) {
                    return text;
                }
            }
            return null;
        }""")

        if not host:
            host = "ftp.boxtoplay.com"
            logger.warning(f"Host FTP non detecte, fallback: {host}")

        logger.info(f"Compte FTP cree: {ftp_user}@{host}")
        return {"host": host, "user": ftp_user, "password": password}

    async def install_modpack(self, server_id, pack_version_id=None):
        """Installe un modpack CurseForge sur le serveur."""
        pack_id = pack_version_id or MODPACK_VERSION_ID
        logger.info(f"Installation modpack (packVersionId={pack_id})...")

        url = URLS["modpack"].format(server_id=server_id, pack_id=pack_id)
        response = await self.page.goto(url, wait_until="networkidle", timeout=60000)

        status = response.status if response else 0
        if status == 200:
            logger.info("Modpack installe.")
        else:
            logger.warning(f"Installation modpack: HTTP {status}")

        # Laisser le temps a l'installation de se propager
        await self.page.wait_for_timeout(5000)

    async def start_server(self, server_id):
        """Demarre le serveur."""
        logger.info(f"Demarrage serveur {server_id}...")
        response = await self.page.goto(
            URLS["start"].format(server_id=server_id),
            wait_until="networkidle",
            timeout=30000,
        )
        status = response.status if response else 0
        if status == 200:
            logger.info(f"Serveur {server_id} demarre.")
        else:
            logger.warning(f"Demarrage serveur: HTTP {status}")

    async def get_cookies_string(self):
        """Recupere les cookies du navigateur au format chaine pour le Gist."""
        cookies = await self.context.cookies("https://www.boxtoplay.com")
        relevant_names = [
            "BOXTOPLAY_SESSION",
            "BOXTOPLAY_LANG",
            "cf_clearance",
            "cookie_consent_level",
            "cookie_consent_user_accepted",
            "cookie_consent_user_consent_token",
        ]
        relevant = [c for c in cookies if c["name"] in relevant_names]
        return "; ".join(f'{c["name"]}={c["value"]}' for c in relevant)


# =============================================================================
# FTP TRANSFER
# =============================================================================

def run_lftp(args, timeout=900):
    """Execute une commande lftp avec timeout."""
    try:
        result = subprocess.run(
            ["lftp"] + args,
            check=True,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            logger.info(f"lftp stdout: {result.stdout[:500]}")
        return True
    except subprocess.TimeoutExpired:
        logger.error("lftp: timeout depasse (15 min)")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"lftp erreur: {e.stderr[:500] if e.stderr else str(e)}")
        return False
    except FileNotFoundError:
        logger.error("lftp non installe. Transfert impossible.")
        return False


def transfer_world(source_ftp, target_ftp):
    """Transfere le dossier /world de l'ancien serveur vers le nouveau via lftp."""
    if not source_ftp or not source_ftp.get("host"):
        logger.warning("Pas d'infos FTP source. Transfert ignore.")
        return False

    if not target_ftp or not target_ftp.get("host"):
        logger.warning("Pas d'infos FTP cible. Transfert ignore.")
        return False

    os.makedirs(TEMP_DIR, exist_ok=True)
    local_world = os.path.join(TEMP_DIR, "world")

    # Telecharger le monde depuis l'ancien serveur
    logger.info(f"Telechargement /world depuis {source_ftp['host']}...")
    dl_ok = run_lftp([
        "-u", f"{source_ftp['user']},{source_ftp['password']}",
        f"ftp://{source_ftp['host']}",
        "-e", f"mirror --verbose --parallel=10 /world {local_world}; quit",
    ])

    if not dl_ok or not os.path.exists(local_world):
        logger.warning("Telechargement echoue ou /world vide.")
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        return False

    # Uploader le monde vers le nouveau serveur
    logger.info(f"Upload /world vers {target_ftp['host']}...")
    ul_ok = run_lftp([
        "-u", f"{target_ftp['user']},{target_ftp['password']}",
        f"ftp://{target_ftp['host']}",
        "-e", f"mirror --reverse --verbose --parallel=10 {local_world} /world; quit",
    ])

    shutil.rmtree(TEMP_DIR, ignore_errors=True)

    if ul_ok:
        logger.info("Transfert de monde termine.")
    else:
        logger.warning("Upload du monde echoue.")
    return ul_ok


# =============================================================================
# MAIN
# =============================================================================

async def main():
    logger.info("=" * 50)
    logger.info("BOXTOPLAY WORKER START")
    logger.info("=" * 50)

    # Validation
    if not GIST_ID or not GH_TOKEN:
        raise Exception("GIST_ID et GH_TOKEN requis (variables d'environnement).")

    # 1. Charger le state
    state, gist_filename = get_state()
    current_idx = state.get("active_account_index", 0)
    next_idx = 1 if current_idx == 0 else 0

    acc_active = state["accounts"][current_idx]
    acc_target = state["accounts"][next_idx]
    common_pass = state.get("ftp_password", FTP_PASSWORD)

    logger.info(f"Compte actif:  [{current_idx}] {acc_active['email']}")
    logger.info(f"Compte cible:  [{next_idx}] {acc_target['email']}")

    # Infos FTP du serveur actuel (pour le transfert de monde)
    ftp_source = {
        "host": acc_active.get("ftp_host"),
        "user": acc_active.get("ftp_user"),
        "password": common_pass,
    }

    worker = BoxToPlayWorker()

    try:
        await worker.start()

        # =============================================
        # Phase 1: Arreter l'ancien serveur
        # =============================================
        logger.info("--- Phase 1: Arret de l'ancien serveur ---")

        try:
            await worker.login(acc_active["email"], acc_active["password"])

            server_id = await worker.get_server_id()
            if server_id:
                await worker.change_dns(server_id, "")
                await worker.stop_server(server_id)
                logger.info(f"Ancien serveur {server_id} arrete, DNS libere.")
            else:
                logger.warning("Aucun serveur actif (probablement deja expire).")
        except Exception as e:
            logger.warning(f"Phase 1 echouee (non bloquant): {e}")

        # =============================================
        # Phase 2: Creer le nouveau serveur
        # =============================================
        logger.info("--- Phase 2: Creation du nouveau serveur ---")

        # Nouvelle session navigateur pour le second compte
        await worker._new_session()
        await worker.login(acc_target["email"], acc_target["password"])

        # Acheter le serveur gratuit
        if not await worker.buy_server():
            raise Exception("Achat du serveur echoue (rupture de stock ou panier payant).")

        # Recuperer l'ID du nouveau serveur (avec retries)
        new_server_id = None
        for attempt in range(6):
            new_server_id = await worker.get_server_id()
            if new_server_id:
                break
            logger.info(f"Serveur pas encore disponible, retry {attempt + 1}/6...")
            await worker.page.wait_for_timeout(5000)

        if not new_server_id:
            raise Exception("Serveur introuvable apres achat.")

        logger.info(f"Nouveau serveur: #{new_server_id}")

        # Configurer le serveur
        await worker.change_dns(new_server_id, IP_NEW_SERVER)
        ftp_target = await worker.create_ftp_account(new_server_id, common_pass)
        await worker.install_modpack(new_server_id)

        # =============================================
        # Phase 3: Transfert de monde
        # =============================================
        logger.info("--- Phase 3: Transfert de monde ---")
        transfer_world(ftp_source, ftp_target)

        # =============================================
        # Phase 4: Demarrer et sauvegarder
        # =============================================
        logger.info("--- Phase 4: Demarrage et sauvegarde ---")

        await worker.start_server(new_server_id)

        # Recuperer les cookies frais pour le bot Discord
        cookie_string = await worker.get_cookies_string()

        # Mettre a jour le state
        state["active_account_index"] = next_idx
        state["current_server_id"] = new_server_id
        state["modpack"] = MODPACK_NAME
        state["accounts"][next_idx]["server_id"] = new_server_id
        state["accounts"][next_idx]["ftp_host"] = ftp_target["host"]
        state["accounts"][next_idx]["ftp_user"] = ftp_target["user"]
        state["accounts"][next_idx]["cookies"] = {"BOXTOPLAY_SESSION": cookie_string}

        update_state(state, gist_filename)

        logger.info("=" * 50)
        logger.info("BOXTOPLAY WORKER SUCCESS")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"WORKER FAILED: {e}")
        raise
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
