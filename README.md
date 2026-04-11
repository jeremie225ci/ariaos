# AriaOS - AI Operating System for Agents

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.x-blue?style=for-the-badge&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/VM-Debian%2012%20%2F%20XFCE-red?style=for-the-badge&logo=debian" alt="Debian VM">
  <img src="https://img.shields.io/badge/AI-OpenAI%20GPT--5.4-green?style=for-the-badge&logo=openai" alt="OpenAI">
</p>

Un système d'exploitation orienté agents qui s'exécute dans une machine virtuelle dédiée, avec Terminal Aria, mémoire locale, fichiers locaux, et une configuration locale par clé OpenAI.

## 🚀 Fonctionnalités

- 🤖 **OS orienté agents** - Un environnement dédié pour exécuter et piloter un agent dans une VM
- 💻 **Terminal Aria** - Une interface de travail centrée sur les tâches agentiques
- 🔐 **BYOK OpenAI** - L'utilisateur fournit sa clé OpenAI au premier démarrage
- 🗂️ **Workspace local** - Fichiers, historique et mémoire restent dans la VM
- 🧠 **Mémoire long terme** - Préférences, playbooks et résumés utiles conservés localement
- 🖥️ **Desktop intégré** - Browser, files et shell tournent dans la même machine

## ⚡ Installation Rapide

```bash
# Cloner le repo
git clone git@github.com:jeremie225ci/ariaos.git
cd ariaos

# Lancer le shell GTK en local
cd app/aria-shell-gtk
PYTHONPATH=src python3 -m aria_shell_gtk
```

Pour le vrai produit, le chemin visé reste la VM AriaOS préconfigurée. Le dépôt public contient la base du shell, les fichiers guest VM et les outils de release.

## 📖 Utilisation

### Premier démarrage

1. Démarrer la VM AriaOS
2. Ouvrir AriaOS / Aria Home
3. Passer l'onboarding
4. Entrer une clé OpenAI
5. Lancer Terminal Aria

### Flux principal

- **Aria Home** - Point d'entrée principal
- **Terminal Aria** - Exécution des tâches
- **OpenAI Key** - Configuration locale de la clé et du modèle
- **Files** - Accès au workspace local
- **Browser** - Navigation depuis la VM

### État actuel

Le nouveau flux public est **local-first** :

- pas de création de compte obligatoire
- pas de dépendance obligatoire à un serveur distant pour le premier usage
- clé OpenAI stockée localement dans la VM
- runtime local comme chemin principal

## 🔧 Configuration

Les principaux fichiers locaux utilisés par AriaOS sont :

- `~/.config/ariaos/local_agent_secrets.json` - clé OpenAI locale
- `~/.config/ariaos/runtime_config.json` - configuration runtime
- `~/.local/state/ariaos/onboarding_state.json` - état de l'onboarding
- `~/.ariaos/data/` - historique local et données

## 🏗️ Structure du Projet

```text
ariaos/
├── README.md
├── app/
│   └── aria-shell-gtk/          # Shell GTK, onboarding, home, terminal
├── vm/
│   ├── guest/                   # Launchers, desktop files, firstboot, greeter
│   └── release/                 # Outils d'export et de release VM
├── docs/
│   ├── INSTALL_VM.md            # Flux de setup VM
│   └── LOCAL_FIRST_ARCHITECTURE.md
└── assets/                      # Assets partagés
```

## 🛡️ Sécurité

AriaOS isole l'expérience agentique dans une VM dédiée :

- 🧱 **Périmètre séparé** - l'agent travaille dans la VM, pas sur l'hôte directement
- 🔐 **Clé locale** - la clé OpenAI reste stockée localement
- 🗃️ **Mémoire visible** - préférences et résumés utiles restent inspectables
- 🧭 **Flux simplifié** - moins de dépendances distantes dans le chemin principal

## 📋 Prérequis

- Python 3
- Linux / VM Debian-based
- GTK4 + libadwaita pour le shell GTK
- clé API OpenAI

Paquets généralement nécessaires :

- `python3-gi`
- `gir1.2-gtk-4.0`
- `gir1.2-adw-1`

## 📄 Licence

Licence publique pas encore finalisée.
