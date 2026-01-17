# BoxToPlay Worker - GitHub Codespaces

Script d'automatisation **stateless** pour la migration de serveurs Minecraft sur BoxToPlay avec alternance de comptes.

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Codespaces                            │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐       │
│  │  worker.py  │────▶│   Selenium  │────▶│  BoxToPlay  │       │
│  │  (Python)   │     │  (Firefox)  │     │   (Web)     │       │
│  └─────────────┘     └─────────────┘     └─────────────┘       │
│         │                                                        │
│         │  ┌─────────────┐                                      │
│         └─▶│    LFTP     │──────▶ Transfert FTP                 │
│            │  (local)    │        (Old → /tmp → New)            │
│            └─────────────┘                                      │
└─────────────────────────────────────────────────────────────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │    GitHub Gist      │
         │  (Base de données)  │
         │                     │
         │  - Comptes          │
         │  - Cookies          │
         │  - Infos FTP        │
         │  - État actuel      │
         └─────────────────────┘
```

## 🚀 Prérequis

### Variables d'environnement (Secrets GitHub)

```bash
# OBLIGATOIRES
GIST_ID=<id_de_votre_gist>
GH_TOKEN=<token_github_avec_permission_gist>

# OPTIONNELS
IP_NEW_SERVER=orny              # DNS du serveur (défaut: orny)
FTP_PASSWORD=<mot_de_passe>     # Si non défini dans le Gist
```

### Installation dans Codespaces

```bash
# Installation des dépendances Python
pip install -r requirements.txt

# Installation de lftp pour les transferts FTP
sudo apt-get update && sudo apt-get install -y lftp

# Installation de Firefox (geckodriver inclus)
sudo apt-get install -y firefox-esr
```

## 📁 Structure du Gist

Créez un **Gist GitHub privé** avec un fichier `boxtoplay_state.json` :

```json
{
  "active_account_index": 0,
  "current_server_id": null,
  "ftp_password": "votre_mot_de_passe_ftp",
  "accounts": [
    {
      "email": "compte1@example.com",
      "password": "password1",
      "cookies": {},
      "ftp_host": null,
      "ftp_user": null,
      "server_id": null
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
```

## 🔄 Workflow de rotation

Le worker effectue les actions suivantes à chaque exécution (toutes les 8h) :

### Étape 1 - Lecture de l'état

- Récupère le JSON depuis le Gist GitHub (GET)
- Identifie le compte **actif** (index 0) et le compte **cible** (index 1)

### Étape 2 - Arrêt de l'ancien serveur

- Se connecte au compte actif (via cookie ou credentials)
- Vide le DNS du serveur (plus de connexions possibles)
- Arrête le serveur
- Récupère les infos FTP pour le transfert

### Étape 3 - Activation du nouveau serveur

- Se connecte au compte cible
- Achète/active le serveur gratuit Leviathan
- Configure le DNS personnalisé
- Crée un compte FTP
- Installe le modpack
- Récupère les cookies frais

### Étape 4 - Transfert du monde

- Télécharge `/world` depuis l'ancien serveur → `/tmp/`
- Upload `/tmp/world` vers le nouveau serveur
- Nettoie les fichiers temporaires

### Étape 5 - Démarrage

- Démarre le nouveau serveur

### Étape 6 - Sauvegarde

- Bascule `active_account_index` (0 → 1 ou 1 → 0)
- Sauvegarde les nouveaux cookies (pour le Bot Discord)
- Sauvegarde les nouvelles infos FTP
- Envoie le tout dans le Gist (PATCH)

## 🏃 Exécution

```bash
# Définir les variables d'environnement
export GIST_ID="abc123def456"
export GH_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"

# Lancer le worker
python worker.py
```

## 📂 Points clés de l'architecture stateless

| Avant (local)            | Après (Cloud)              |
| ------------------------ | -------------------------- |
| `id.json`                | Gist GitHub                |
| `cookies.json`           | Gist GitHub                |
| `/home/uxy/boxtoplay/`   | `/tmp/boxtoplay_transfer/` |
| `gcloud cloud-shell ssh` | Exécution locale `lftp`    |
| Geckodriver manuel       | Firefox-esr + auto         |

## 🔒 Sécurité

- ✅ Gist **privé** recommandé
- ✅ Token GitHub avec permission `gist` uniquement
- ✅ Cookies automatiquement rafraîchis
- ✅ Pas de secrets dans le code
- ✅ Mode headless (pas d'écran)

## ⚙️ Configuration Selenium

Le driver Firefox est configuré avec :

```python
--headless           # Pas d'interface graphique
--no-sandbox         # Compatible conteneur
--disable-dev-shm-usage  # Évite problèmes mémoire
--disable-gpu        # Pas de GPU sur Codespaces
--window-size=1920,1080  # Taille virtuelle
```

## 📋 Logs

Les logs sont affichés dans la console ET écrits dans `/tmp/worker.log`.

Format : `2026-01-17 14:30:00 - INFO - 🚀 Message`

## ❓ Troubleshooting

### Erreur "GIST_ID et GH_TOKEN doivent être définis"

→ Vérifiez que les variables d'environnement sont exportées

### Timeout FTP

→ Vérifiez que `lftp` est installé : `sudo apt-get install lftp`

### Firefox ne démarre pas

→ Installez firefox-esr : `sudo apt-get install firefox-esr`
