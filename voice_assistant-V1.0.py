"""
AI Voice Assistant - Édition "Intelligente" (Windows)
=======================================================

Cœur 100% HORS LIGNE :
    - Reconnaissance vocale via Whisper (réseau de neurones local, aucune
      connexion nécessaire une fois le modèle téléchargé)
    - Synthèse vocale via pyttsx3 (offline)
    - Ouverture / fermeture de logiciels
    - Résumé de texte / audio / vidéo en local
    - Son de confirmation à la fin de chaque tâche
    - Suggestions proactives aléatoires

Modules qui ONT BESOIN d'internet (isolés, optionnels) :
    - Lecture d'une page web (il faut la télécharger)
    - Recherche web

Dépendances : AUCUNE installation manuelle nécessaire.
    Ce script installe lui-même tout ce dont il a besoin au premier
    lancement (voir la fonction bootstrap() ci-dessous). Il faut juste
    une connexion internet la toute première fois.

Premier lancement : les librairies (~600 Mo avec le modèle "small") sont
téléchargées automatiquement une seule fois, puis tout tourne en local.
"""

import os

# Supprime l'avertissement Hugging Face sur les symlinks Windows lors du
# téléchargement des modèles Whisper : purement cosmétique (le cache
# fonctionne quand même, juste un peu moins optimisé en espace disque),
# donc pas besoin de l'afficher à chaque démarrage.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
import sys
import time
import random
import json
import datetime
import subprocess
import difflib
import re
import threading
import queue
import importlib
import importlib.util
import ast
import math
import operator
import statistics
from fractions import Fraction


# ---------------------------------------------------------------------------
# Force l'affichage immédiat de chaque print() (pas de mise en tampon).
# Sans ça, la console Windows peut garder les messages en mémoire et donner
# l'impression que le script est bloqué alors qu'il continue de tourner.
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass  # pas grave si indisponible (ex: certains environnements figés)


# ---------------------------------------------------------------------------
# Bootstrap : installe automatiquement les librairies manquantes
# ---------------------------------------------------------------------------

# Associe le nom du module Python (utilisé pour l'import) au nom du paquet
# pip correspondant (parfois différent, ex: PIL / Pillow).
REQUIRED_PACKAGES = {
    "pyttsx3": "pyttsx3",
    "psutil": "psutil",
    "faster_whisper": "faster-whisper",
    "sounddevice": "sounddevice",
    "numpy": "numpy",
    "scipy": "scipy",
    "sumy": "sumy",
    "nltk": "nltk",
    "pystray": "pystray",
    "PIL": "Pillow",
}

# Optionnels : si l'installation échoue (pas de connexion, etc.), l'assistant
# démarre quand même avec les fonctionnalités correspondantes désactivées.
OPTIONAL_PACKAGES = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
}

# Remarque : pywin32 (win32api) a été retiré des dépendances. Il n'est en
# réalité jamais utilisé par ce script (le registre Windows est lu via
# `winreg`, qui fait partie de la bibliothèque standard), et c'est un
# paquet connu pour poser des problèmes d'installation/détection sur
# certaines configurations, notamment avec des versions récentes de Python.


def _pip_install(pip_name: str) -> bool:
    try:
        # Pas de --quiet : on veut VOIR les erreurs pip (réseau, permissions,
        # etc.) au lieu qu'elles disparaissent silencieusement. Timeout de
        # 3 minutes : si pip reste bloqué (réseau coupé, pare-feu...), on
        # ne reste plus figé indéfiniment sans explication.
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name],
            check=True,
            timeout=180,
        )
        return True
    except subprocess.TimeoutExpired:
        print(f"  ⚠ Timeout : l'installation de {pip_name} a pris plus de 3 minutes.")
        print("    Vérifie ta connexion internet, ou un pare-feu/antivirus qui bloque pip.")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ Erreur pip lors de l'installation de {pip_name} (code {e.returncode}).")
        return False


def _is_installed(module_name: str) -> bool:
    """
    Vérifie si une librairie est installée SANS l'importer réellement.
    Important : `importlib.import_module()` exécute le code d'initialisation
    du module (par ex. `sounddevice` sonde le matériel audio dès l'import,
    ce qui peut bloquer indéfiniment sur certains PC à cause d'un pilote
    capricieux). `find_spec()` se contente de vérifier sa présence sur le
    disque, sans exécuter quoi que ce soit.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _import_with_timeout(module_name, timeout=15):
    """
    Importe un module dans un thread séparé, avec un délai maximum.
    Certains modules (ex: nltk, observé sur Python 3.14) peuvent bloquer
    indéfiniment à l'import sur certaines configurations. Plutôt que de
    rester figé pour toujours, on abandonne après `timeout` secondes.
    Retourne (module, erreur) où erreur vaut "timeout", une Exception, ou None.
    """
    result = {}

    def _target():
        try:
            result["module"] = importlib.import_module(module_name)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "timeout"
    if "error" in result:
        return None, result["error"]
    return result.get("module"), None


def _call_with_timeout(func, timeout=15, *args, **kwargs):
    """Comme _import_with_timeout, mais pour n'importe quel appel de fonction."""
    result = {}

    def _target():
        try:
            result["value"] = func(*args, **kwargs)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "timeout"
    if "error" in result:
        return None, result["error"]
    return result.get("value"), None


def bootstrap():
    """
    Vérifie chaque dépendance ; installe automatiquement celles qui
    manquent. Appelé une seule fois au tout début du script.
    """
    print(f"Interpréteur Python utilisé : {sys.executable}")
    print("Vérification des librairies nécessaires...")
    missing_required = []

    for module_name, pip_name in REQUIRED_PACKAGES.items():
        print(f"  Vérification de {module_name}...")
        if not _is_installed(module_name):
            missing_required.append((module_name, pip_name))

    if missing_required:
        print(f"{len(missing_required)} librairie(s) manquante(s), installation en cours...")
        print("(ceci ne se produit qu'une seule fois, connexion internet nécessaire)")
        for module_name, pip_name in missing_required:
            print(f"  Installation de {pip_name}...")
            ok = _pip_install(pip_name)
            if not ok:
                print(f"  ⚠ Échec de l'installation de {pip_name}. "
                      f"Vérifie ta connexion internet et relance le script.")
                sys.exit(1)
        print("Installation terminée.")
    else:
        print("Toutes les librairies obligatoires sont déjà présentes.")

    # Paquets optionnels : on tente, mais on n'arrête pas le programme en cas d'échec
    print("Vérification des librairies optionnelles (web)...")
    for module_name, pip_name in OPTIONAL_PACKAGES.items():
        print(f"  Vérification de {module_name}...")
        if not _is_installed(module_name):
            print(f"  (optionnel) Installation de {pip_name}...")
            _pip_install(pip_name)
    print("Librairies optionnelles vérifiées.")

    # Données NLTK (fichiers, pas un paquet pip)
    print("Import du module nltk...")
    nltk_module, err = _import_with_timeout("nltk", timeout=15)
    if err == "timeout":
        print("  ⚠ L'import de nltk a dépassé 15 secondes, probablement bloqué")
        print("    (souvent lié à une incompatibilité avec une version très récente")
        print("    de Python). Le résumé de texte sera indisponible pour cette session,")
        print("    mais le reste de l'assistant fonctionne normalement.")
    elif err is not None:
        print(f"nltk non installé ou en erreur ({err}), étape ignorée.")
    elif nltk_module is not None:
        print("Module nltk importé. Vérification des données (punkt)...")
        for pkg in ("punkt", "punkt_tab"):
            try:
                nltk_module.data.find(f"tokenizers/{pkg}")
                print(f"  Donnée '{pkg}' déjà présente.")
            except LookupError:
                print(f"  Téléchargement des données NLTK ({pkg})...")
                nltk_module.download(pkg, quiet=True)
                print(f"  '{pkg}' téléchargé.")

    print("Toutes les librairies sont prêtes.\n")


# Lance le bootstrap AVANT tout le reste, dès l'exécution du script.
bootstrap()


# ---------------------------------------------------------------------------
# Journalisation vers fichier (utile en arrière-plan, sans console visible)
# ---------------------------------------------------------------------------

def setup_logging():
    """
    Redirige stdout/stderr vers un fichier log, en plus de la console
    quand elle existe. Indispensable en mode arrière-plan (pythonw.exe
    n'a pas de console, et print() planterait sans ça sur certains
    environnements figés avec PyInstaller --noconsole).
    """
    log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AssistantVocal")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "assistant.log")
    log_handle = open(log_path, "a", encoding="utf-8", buffering=1)

    class _Tee:
        def __init__(self, *streams):
            self.streams = [s for s in streams if s is not None]

        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                except Exception:
                    pass

        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    sys.stdout = _Tee(sys.stdout, log_handle)
    sys.stderr = _Tee(sys.stderr, log_handle)
    print(f"\n--- Démarrage {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    return log_path


def set_low_priority():
    """
    Abaisse la priorité du processus pour que l'assistant ne ralentisse
    jamais tes autres logiciels (jeux, montage, etc.). Windows privilégiera
    automatiquement les applications au premier plan.
    """
    print("Réglage de la priorité du processus (import psutil)...")
    psutil_module, err = _import_with_timeout("psutil", timeout=15)
    if err == "timeout":
        print("  ⚠ Import de psutil trop lent, étape ignorée (sans conséquence grave).")
        return
    if err is not None or psutil_module is None:
        print(f"Impossible d'abaisser la priorité du processus : {err}")
        return
    try:
        p = psutil_module.Process(os.getpid())
        p.nice(psutil_module.BELOW_NORMAL_PRIORITY_CLASS)
        print("Priorité processus abaissée (BELOW_NORMAL).")
    except Exception as e:
        print(f"Impossible d'abaisser la priorité du processus : {e}")


def register_startup(enable: bool = True):
    """
    Ajoute (ou retire) l'assistant au démarrage automatique de Windows,
    lancé silencieusement via pythonw.exe (pas de fenêtre console).
    """
    if os.name != "nt":
        print("Le démarrage automatique n'est disponible que sous Windows.")
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "AssistantVocal"
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script_path = os.path.abspath(__file__)
    command = f'"{pythonw}" "{script_path}" --background'

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
    if enable:
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
        print("Démarrage automatique activé.")
    else:
        try:
            winreg.DeleteValue(key, app_name)
            print("Démarrage automatique désactivé.")
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)


# ---------------------------------------------------------------------------
# Imports (désormais garantis disponibles grâce au bootstrap ci-dessus)
# ---------------------------------------------------------------------------

import pyttsx3

try:
    import winsound
except ImportError:
    winsound = None

try:
    import winreg
except ImportError:
    winreg = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    from faster_whisper import WhisperModel
    import sounddevice as sd
    import numpy as np
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

try:
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.nlp.tokenizers import Tokenizer
    from sumy.summarizers.lex_rank import LexRankSummarizer
    SUMY_AVAILABLE = True
except ImportError:
    SUMY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Sons de fin de tâche
# ---------------------------------------------------------------------------

def play_done_sound():
    """Joue un petit son quand une tâche est terminée."""
    if winsound:
        try:
            winsound.Beep(880, 120)
            winsound.Beep(1200, 150)
        except Exception:
            pass


def play_error_sound():
    if winsound:
        try:
            winsound.Beep(300, 200)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Reconnaissance vocale locale (Whisper) - remplace Google Speech Recognition
# ---------------------------------------------------------------------------

class LocalSpeechRecognizer:
    """
    Écoute le micro et transcrit avec Whisper, entièrement en local.
    Aucune donnée n'est envoyée sur internet une fois le modèle téléchargé.
    """

    def __init__(self, model_size="base", language="fr", samplerate=16000):
        if not WHISPER_AVAILABLE:
            raise RuntimeError(
                "faster-whisper / sounddevice non installés. "
                "Fais : pip install faster-whisper sounddevice numpy"
            )
        self.language = language
        self.samplerate = samplerate  # fréquence attendue par Whisper (16 kHz)

        # Le micro ne fonctionne pas forcément nativement à 16 kHz (souvent
        # 44100 ou 48000 Hz). Si on force sounddevice à ouvrir le flux à
        # 16000 Hz alors que le matériel tourne à une autre fréquence,
        # Windows doit reconvertir le son à la volée, ce qui peut provoquer
        # du retard qui s'accumule et de la distorsion. On enregistre donc
        # à la fréquence native du micro, puis on reconvertit nous-mêmes
        # vers 16 kHz juste avant la transcription.
        try:
            device_info = sd.query_devices(kind="input")
            self.device_samplerate = int(device_info["default_samplerate"])
            print(f"Micro détecté : {device_info['name']} "
                  f"(fréquence native : {self.device_samplerate} Hz)")
        except Exception as e:
            print(f"⚠ Impossible d'interroger le micro par défaut ({e}), "
                  f"utilisation de {samplerate} Hz par défaut.")
            self.device_samplerate = samplerate

        print("Chargement du modèle Whisper (local)...")
        # compute_type="int8" = rapide sur CPU, pas besoin de GPU
        # cpu_threads=0 laisse ctranslate2 utiliser tous les coeurs disponibles
        self.model = WhisperModel(
            model_size, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4
        )
        print("Modèle chargé. L'assistant fonctionne hors ligne.")

        # Seuil de silence calibré automatiquement (remplacé par calibrate())
        self.silence_threshold = 0.015

    @staticmethod
    def _resample(audio: "np.ndarray", orig_sr: int, target_sr: int) -> "np.ndarray":
        """
        Reconvertit un signal audio d'une fréquence vers une autre.
        Utilise scipy.signal.resample_poly (rééchantillonnage polyphase
        avec filtre anti-repliement) plutôt qu'une simple interpolation
        linéaire : cette dernière introduit du repliement de spectre
        (aliasing), c'est-à-dire du bruit numérique qui dégrade la
        précision de la transcription, surtout quand le micro tourne
        nativement à 44100 ou 48000 Hz et doit être ramené à 16000 Hz pour
        Whisper. Repli sur l'interpolation linéaire si scipy n'est pas
        disponible, pour ne jamais bloquer le fonctionnement.
        """
        if orig_sr == target_sr or audio.size == 0:
            return audio
        try:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(int(orig_sr), int(target_sr))
            up, down = int(target_sr) // g, int(orig_sr) // g
            return resample_poly(audio, up, down).astype("float32")
        except Exception:
            duration = audio.shape[0] / orig_sr
            target_len = max(1, int(duration * target_sr))
            orig_x = np.linspace(0, duration, num=audio.shape[0], endpoint=False)
            target_x = np.linspace(0, duration, num=target_len, endpoint=False)
            return np.interp(target_x, orig_x, audio).astype("float32")

    def calibrate(self, duration=1.5):
        """
        Mesure le bruit ambiant réel de la pièce au démarrage pour ajuster
        automatiquement le seuil de détection de silence. Évite d'avoir à
        répéter une commande parce que le micro capte mal ta voix, ou au
        contraire coupe trop tôt à cause du bruit de fond.
        """
        print("Calibrage du micro, merci de rester silencieux un instant...")
        audio = sd.rec(int(duration * self.device_samplerate), samplerate=self.device_samplerate,
                        channels=1, dtype="float32")
        sd.wait()
        ambient_level = float(np.abs(audio).mean())
        # Marge de sécurité au-dessus du bruit ambiant mesuré. Réduite par
        # rapport à avant (x3 → x1.8, plancher 0.008 → 0.004) : un seuil
        # trop élevé forçait à parler fort ou à répéter plusieurs fois
        # avant que la voix soit détectée.
        self.silence_threshold = max(ambient_level * 1.8, 0.004)
        print(f"Calibrage terminé (seuil de silence : {self.silence_threshold:.4f}).")

    def record(self, duration=5) -> "np.ndarray":
        """Enregistre `duration` secondes depuis le micro par défaut."""
        audio = sd.rec(
            int(duration * self.device_samplerate),
            samplerate=self.device_samplerate,
            channels=1,
            dtype="float32",
        )
        sd.wait()
        return self._resample(audio.flatten(), self.device_samplerate, self.samplerate)

    def record_until_silence(self, max_duration=10, silence_duration=0.7):
        """
        Enregistre jusqu'à détecter un silence, pour ne pas couper la phrase
        ni attendre inutilement. Utilise le seuil calibré automatiquement
        (voir calibrate()) plutôt qu'une valeur fixe.
        """
        chunk_size = int(self.device_samplerate * 0.25)
        chunks = []
        silent_chunks_needed = max(1, int(silence_duration / 0.25))
        silent_count = 0
        max_chunks = int(max_duration / 0.25)
        has_spoken = False
        max_volume_seen = 0.0
        overflow_detected = False

        stream = sd.InputStream(samplerate=self.device_samplerate, channels=1, dtype="float32")
        stream.start()
        try:
            for _ in range(max_chunks):
                data, overflowed = stream.read(chunk_size)
                if overflowed:
                    overflow_detected = True
                data = data.flatten()
                chunks.append(data)
                volume = np.abs(data).mean()
                max_volume_seen = max(max_volume_seen, volume)

                if volume >= self.silence_threshold:
                    has_spoken = True
                    silent_count = 0
                elif has_spoken:
                    # On ne commence à compter le silence qu'après avoir
                    # détecté un début de parole, pour ne pas couper trop
                    # tôt si l'utilisateur met un instant à démarrer.
                    silent_count += 1
                    if silent_count >= silent_chunks_needed:
                        break
        finally:
            stream.stop()
            stream.close()

        # DEBUG : un seul résumé par tentative d'écoute (pas une ligne
        # toutes les 0,25s). Retire cette ligne une fois que ça fonctionne.
        print(f"  [debug] volume max capté={max_volume_seen:.4f} / seuil={self.silence_threshold:.4f}"
              + ("  (parole détectée)" if has_spoken else "  (rien détecté)"))
        if overflow_detected:
            print("  ⚠ Dépassement de tampon audio détecté : le PC n'a pas pu suivre "
                  "en temps réel (retard/perte de son possible). Ferme les applis "
                  "gourmandes en arrière-plan si ça persiste.")

        raw = np.concatenate(chunks) if chunks else np.array([], dtype="float32")
        audio = self._resample(raw, self.device_samplerate, self.samplerate)
        return audio, has_spoken

    def transcribe(self, audio) -> str:
        if audio.size == 0:
            return ""
        # Normalisation du volume : même quand la voix est captée, elle
        # reste parfois à un niveau très faible (micro peu sensible). On
        # amplifie le signal pour que son pic atteigne ~90% du maximum,
        # ce qui aide nettement Whisper à reconnaître les mots.
        peak = float(np.abs(audio).max())
        if peak > 0:
            audio = (audio / peak) * 0.9

        # vad_filter : filtre les silences internes à l'audio pour une
        # transcription plus propre. beam_size=8 explore plus d'hypothèses
        # de transcription que la valeur par défaut (5), pour réduire les
        # erreurs sur des mots isolés courts, sans changer de modèle Whisper
        # (reste "base", donc encore raisonnable en vitesse sur CPU).
        # initial_prompt : oriente Whisper vers le vocabulaire attendu des
        # commandes de cet assistant, plutôt que de laisser le modèle
        # deviner librement (réduit les erreurs de transcription sur des
        # mots courts comme "ouvre" ou les chiffres).
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=8,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
            # Désactivé : par défaut Whisper essaie de "continuer" logiquement
            # les segments précédents, ce qui peut faire dériver la
            # transcription sur une commande courte et isolée (un seul mot).
            # Chaque commande est indépendante ici, donc on veut que chaque
            # transcription reparte de zéro.
            condition_on_previous_text=False,
            initial_prompt=(
                "Commandes vocales en français : zéro, un, deux, trois, "
                "quatre, cinq, six, sept, huit, neuf, ouvre, ferme, lance, "
                "démarre, quelle heure est-il, quelle est la date, "
                "recherche, résumé, menu, bonjour."
            ),
        )
        text = " ".join(seg.text for seg in segments).strip()
        return text

    def listen(self) -> str:
        print("Listening... (parlez)")
        audio, has_spoken = self.record_until_silence()
        if not has_spoken:
            # Rien de détecté au-dessus du seuil de silence : on n'envoie
            # pas ça à Whisper, qui aurait sinon tendance à halluciner une
            # phrase plausible à partir de quasi-silence.
            return ""
        text = self.transcribe(audio)
        if text:
            print(f"Vous: {text}")
        return text.lower()


# ---------------------------------------------------------------------------
# Gestion des logiciels installés (identique à la version précédente)
# ---------------------------------------------------------------------------

def close_active_window() -> str:
    """
    Ferme la fenêtre actuellement au premier plan (équivalent d'un Alt+F4),
    pour les commandes génériques comme "ferme la fenêtre" ou "ferme
    fenêtre", qui ne désignent pas un logiciel précis mais la fenêtre
    ouverte à l'écran.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "Je n'ai pas trouvé de fenêtre active à fermer."
        WM_CLOSE = 0x0010
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return "Je ferme la fenêtre active."
    except Exception:
        return "Je n'ai pas réussi à fermer la fenêtre."


class AppManager:
    def __init__(self):
        self.apps = {}
        self.reg_paths = []
        if winreg:
            self.reg_paths = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            self.scan_installed_apps()

    def scan_installed_apps(self):
        total_checked = 0
        for hive, path in self.reg_paths:
            try:
                key = winreg.OpenKey(hive, path)
            except FileNotFoundError:
                continue
            nb_subkeys = winreg.QueryInfoKey(key)[0]
            for i in range(nb_subkeys):
                total_checked += 1
                if total_checked % 50 == 0:
                    # Affiche un point de progression : le scan avance mais
                    # cette étape peut prendre plusieurs secondes selon le
                    # nombre de logiciels installés sur la machine.
                    print(".", end="", flush=True)
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                    exe_path = None
                    for value_name in ("DisplayIcon", "InstallLocation", "UninstallString"):
                        try:
                            raw = winreg.QueryValueEx(subkey, value_name)[0]
                            candidate = raw.split(",")[0].strip('"')
                            # Pas de vérification os.path.exists ici : c'est
                            # un accès disque coûteux répété pour CHAQUE
                            # logiciel installé, ce qui ralentit énormément
                            # le scan (potentiellement des centaines
                            # d'entrées). open_app() revérifie déjà le
                            # chemin au moment du lancement, avec un repli
                            # sur os.startfile() si besoin : donc c'est
                            # totalement sûr de ne pas vérifier ici.
                            if candidate.lower().endswith(".exe"):
                                exe_path = candidate
                                break
                        except FileNotFoundError:
                            continue
                    if name:
                        self.apps[name.strip().lower()] = exe_path
                except (FileNotFoundError, OSError):
                    continue
        print()
        print(f"[AppManager] {len(self.apps)} logiciels détectés.")

    # Fautes de transcription courantes vues avec Whisper en français, pour
    # les mots que le modèle "avale" souvent une lettre (ex: "crome" pour
    # "chrome"). Appliqué mot par mot avant la recherche floue.
    COMMON_TYPOS = {
        "crome": "chrome", "krome": "chrome",
        "exel": "excel", "exell": "excel",
        "wor": "word", "ouorde": "word",
        "powerpoint": "powerpoint", "power point": "powerpoint",
        "note pad": "notepad", "bloc note": "bloc-notes",
        "vlc media": "vlc",
    }

    @classmethod
    def _autocorrect(cls, query: str) -> str:
        words = query.split()
        return " ".join(cls.COMMON_TYPOS.get(w, w) for w in words)

    def find_app(self, query: str):
        query = self._autocorrect(query.lower().strip())
        if not query:
            return None, None
        for name in self.apps:
            if query in name or name in query:
                return name, self.apps[name]
        # cutoff légèrement assoupli (0.5 → 0.42) pour tolérer une lettre
        # en plus/en moins ou une syllabe mal transcrite, sans pour autant
        # matcher n'importe quoi.
        matches = difflib.get_close_matches(query, self.apps.keys(), n=1, cutoff=0.42)
        if matches:
            return matches[0], self.apps[matches[0]]
        return None, None

    def open_app(self, query: str) -> str:
        name, path = self.find_app(query)
        if not name:
            return f"Je n'ai trouvé aucun logiciel correspondant à {query}."
        try:
            if path and os.path.exists(path):
                subprocess.Popen([path])
            else:
                os.startfile(name)
            return f"J'ouvre {name}."
        except Exception:
            return f"Je n'ai pas réussi à ouvrir {name}."

    def close_app(self, query: str) -> str:
        if psutil is None:
            return "Le module psutil n'est pas installé, je ne peux pas fermer de logiciel."
        name, _ = self.find_app(query)
        target = (name or query).lower()
        closed = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                proc_name = proc.info['name'] or ""
                if target.split()[0] in proc_name.lower():
                    proc.terminate()
                    closed.append(proc_name)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if closed:
            return f"J'ai fermé {', '.join(set(closed))}."
        return f"Je n'ai trouvé aucun processus en cours correspondant à {query}."


# ---------------------------------------------------------------------------
# Résumé local (texte / audio / vidéo) - sans internet
# ---------------------------------------------------------------------------

class Summarizer:
    """Résumé extractif local via sumy (pas d'appel réseau)."""

    def __init__(self):
        self.available = SUMY_AVAILABLE
        if self.available:
            self.available = self._ensure_nltk()

    @staticmethod
    def _ensure_nltk() -> bool:
        nltk, err = _import_with_timeout("nltk", timeout=15)
        if err is not None or nltk is None:
            print("  ⚠ Import de nltk indisponible ou trop lent, résumé désactivé.")
            return False
        for pkg in ("punkt", "punkt_tab"):
            try:
                nltk.data.find(f"tokenizers/{pkg}")
            except LookupError:
                try:
                    nltk.download(pkg, quiet=True)
                except Exception:
                    pass
        return True

    def summarize_text(self, text: str, sentence_count=3) -> str:
        if not self.available:
            return "Le résumé n'est pas disponible sur cette machine (nltk indisponible)."
        if not text.strip():
            return "Il n'y a rien à résumer."
        parser = PlaintextParser.from_string(text, Tokenizer("french"))
        summarizer = LexRankSummarizer()
        sentences = summarizer(parser.document, sentence_count)
        return " ".join(str(s) for s in sentences) or "Je n'ai pas réussi à résumer ce texte."

    def summarize_audio(self, filepath: str, recognizer: "LocalSpeechRecognizer", sentence_count=3) -> str:
        """Transcrit un fichier audio avec Whisper, puis le résume localement."""
        if not WHISPER_AVAILABLE:
            return "Whisper n'est pas installé, je ne peux pas transcrire l'audio."
        segments, _ = recognizer.model.transcribe(filepath, language=recognizer.language)
        text = " ".join(seg.text for seg in segments)
        return self.summarize_text(text, sentence_count)

    def summarize_video(self, filepath: str, recognizer: "LocalSpeechRecognizer", sentence_count=3) -> str:
        """Extrait l'audio d'une vidéo (ffmpeg) puis résume comme un audio."""
        try:
            from moviepy.editor import VideoFileClip
        except ImportError:
            return "Le module moviepy n'est pas installé (pip install moviepy)."

        tmp_audio = os.path.join(os.path.dirname(filepath), "_tmp_extracted_audio.wav")
        try:
            clip = VideoFileClip(filepath)
            clip.audio.write_audiofile(tmp_audio, logger=None)
            clip.close()
            return self.summarize_audio(tmp_audio, recognizer, sentence_count)
        except Exception as e:
            return f"Je n'ai pas réussi à traiter la vidéo ({e})."
        finally:
            if os.path.exists(tmp_audio):
                os.remove(tmp_audio)


# ---------------------------------------------------------------------------
# Calculatrice vocale
# ---------------------------------------------------------------------------
#
# Couvre le "minimum vital" (opérations de base, %, √, ±, mémoire, C/AC,
# parenthèses, notation décimale) ainsi qu'une bonne partie de la liste
# étendue demandée : trigonométrie (+ hyperbolique), degrés/radians/grades,
# logarithmes/exponentielles, racines n-ièmes, factorielle/nPr/nCr, modulo,
# PGCD/PPCM, valeur absolue, partie entière, arrondi, aléatoire, constantes
# π et e, fractions, historique + touche ANS, statistiques de base (moyenne,
# médiane, mode, variance, écart-type, quartiles, somme, somme des carrés,
# min/max), et conversions (unités courantes, bases numériques, fractions).
#
# Volontairement HORS PÉRIMÈTRE (nécessitent un écran graphique ou un moteur
# de calcul formel que cet assistant — purement vocal/console — n'a pas) :
# nombres complexes, matrices/vecteurs, solveur d'équations, dérivée/
# intégrale/sommes/produits, régression/corrélation, lois de probabilité,
# tracé de graphiques, finance (TVM, amortissement, VAN/TRI, devises), et
# langage de programmation (variables/boucles/programmes stockés). Si tu en
# as vraiment besoin, il faudrait repenser l'interface (écran + clavier)
# plutôt que de tout piloter à la voix.
# ---------------------------------------------------------------------------


class CalcError(Exception):
    """Erreur de calcul destinée à être lue à voix haute telle quelle."""
    pass


# -- Conversion des nombres écrits en toutes lettres (français) -----------

_CALC_UNITS = {
    "zéro": 0, "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
    "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
    "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15,
    "seize": 16, "dix-sept": 17, "dix-huit": 18, "dix-neuf": 19,
}
_CALC_TENS = {
    "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
    "soixante": 60, "quatre-vingt": 80, "quatre-vingts": 80,
}


def _calc_words_to_number(chunk: str):
    """
    Convertit une séquence de mots-nombres français ('trois cent quarante-
    deux') en entier. Retourne None si la séquence ne ressemble pas à un
    nombre. Couvre 0-999999, largement suffisant pour un usage courant de
    calculatrice vocale (les grands nombres dictés en toutes lettres sont
    rares en pratique — Whisper transcrit d'ailleurs le plus souvent les
    nombres directement en chiffres).
    """
    words = chunk.replace("-", " ").split()
    if not words:
        return None
    total = 0
    current = 0
    matched_any = False
    for w in words:
        if w in _CALC_UNITS:
            current += _CALC_UNITS[w]
            matched_any = True
        elif w in _CALC_TENS:
            current += _CALC_TENS[w]
            matched_any = True
        elif w in ("cent", "cents"):
            current = (current or 1) * 100
            matched_any = True
        elif w == "mille":
            total += (current or 1) * 1000
            current = 0
            matched_any = True
        elif w == "et":
            continue
        else:
            return None
    if not matched_any:
        return None
    return total + current


_CALC_NUMBER_WORD_ALT = (
    r"zéro|zero|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|"
    r"onze|douze|treize|quatorze|quinze|seize|dix-sept|dix-huit|dix-neuf|"
    r"vingt|trente|quarante|cinquante|soixante|quatre-vingts?|cent|cents|"
    r"mille|et"
)
# Un seul mot-nombre, éventuellement suivi d'autres séparés par espace/tiret
# — SANS consommer l'espace final, pour ne pas recoller le nombre au mot
# suivant (ex: "dix moins trois" ne doit pas devenir "10moins 3").
_CALC_NUMBER_WORD_RE = re.compile(
    rf"\b(?:{_CALC_NUMBER_WORD_ALT})(?:[\s-]+(?:{_CALC_NUMBER_WORD_ALT}))*\b"
)


def _calc_replace_number_words(text: str) -> str:
    def _sub(match):
        value = _calc_words_to_number(match.group(0))
        return str(value) if value is not None else match.group(0)
    return _CALC_NUMBER_WORD_RE.sub(_sub, text)


# -- Évaluateur d'expression sécurisé (pas d'eval() brut) -------------------

_CALC_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_CALC_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _calc_safe_eval(node, names, funcs):
    if isinstance(node, ast.Expression):
        return _calc_safe_eval(node.body, names, funcs)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise CalcError("expression invalide")
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BINOPS:
        return _CALC_BINOPS[type(node.op)](
            _calc_safe_eval(node.left, names, funcs),
            _calc_safe_eval(node.right, names, funcs),
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARY:
        return _CALC_UNARY[type(node.op)](_calc_safe_eval(node.operand, names, funcs))
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise CalcError(f"terme inconnu « {node.id} »")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id
        if fname not in funcs:
            raise CalcError(f"fonction inconnue « {fname} »")
        args = [_calc_safe_eval(a, names, funcs) for a in node.args]
        return funcs[fname](*args)
    raise CalcError("expression non supportée")


class Calculator:
    """
    Calculatrice scientifique pilotée à la voix. Reconnaît une commande
    de calcul via `looks_like_query()`, la normalise en expression
    mathématique, puis l'évalue avec un mini-interpréteur `ast` restreint
    (aucun `eval()` brut : seuls les opérateurs et fonctions listés ici
    sont exécutables, donc aucun risque d'exécuter du code arbitraire même
    si la transcription vocale contient n'importe quoi).
    """

    # Déclencheurs de phrase reconnus (ex: "combien font 5 + 5",
    # "cmb font...", "la somme de...", "fait 5 + 5", "calcule...").
    TRIGGER_PHRASES = (
        "combien font", "combien fait", "combien vaut", "cmb font",
        "cmb fait", "cmb vaut", "quelle est la", "quel est le",
        "calcule", "calcul de", "résous",
    )
    # Indices qu'une fonction/opération de calculatrice est en jeu, même
    # sans déclencheur explicite (ex: "racine carrée de 49").
    FUNCTION_HINTS = (
        "racine", "carré", "cube", "factorielle", "sinus", "cosinus",
        "tangente", "logarithme", "log ", "ln de", "exponentielle",
        "pourcent", "pour cent", "%", "pgcd", "ppcm", "combinaison",
        "permutation", "modulo", "valeur absolue", "partie entière",
        "arrondi", "inverse de", "moyenne de", "médiane de", "variance de",
        "écart-type de", "quartile", "convertis", "en binaire",
        "en hexadécimal", "en octal", "en fraction", "aléatoire",
    )
    OPERATOR_WORDS = ("plus", "moins", "fois", "multiplié", "divisé", "puissance", "sur")

    # Motifs de fonctions : (regex, remplacement) appliqués dans l'ordre,
    # du plus spécifique au plus générique.
    _FUNC_PATTERNS = [
        (r"la somme de\s*(-?\d+(?:\.\d+)?)\s*(?:et|,)\s*(-?\d+(?:\.\d+)?)", r"(\1)+(\2)"),
        (r"le produit de\s*(-?\d+(?:\.\d+)?)\s*(?:et|,)\s*(-?\d+(?:\.\d+)?)", r"(\1)*(\2)"),
        (r"la différence entre\s*(-?\d+(?:\.\d+)?)\s*et\s*(-?\d+(?:\.\d+)?)", r"(\1)-(\2)"),
        (r"le quotient de\s*(-?\d+(?:\.\d+)?)\s*(?:et|,)\s*(-?\d+(?:\.\d+)?)", r"(\1)/(\2)"),
        (r"racine carrée de\s*(-?\d+(?:\.\d+)?)", r"sqrt(\1)"),
        (r"racine cubique de\s*(-?\d+(?:\.\d+)?)", r"cbrt(\1)"),
        (r"racine\s*(\d+)\s*(?:i[eè]me)?\s*de\s*(-?\d+(?:\.\d+)?)", r"root(\1,\2)"),
        (r"racine de\s*(-?\d+(?:\.\d+)?)", r"sqrt(\1)"),
        (r"(-?\d+(?:\.\d+)?)\s*au carré", r"(\1)**2"),
        (r"carré de\s*(-?\d+(?:\.\d+)?)", r"(\1)**2"),
        (r"(-?\d+(?:\.\d+)?)\s*au cube", r"(\1)**3"),
        (r"cube de\s*(-?\d+(?:\.\d+)?)", r"(\1)**3"),
        (r"inverse de\s*(-?\d+(?:\.\d+)?)", r"(1/(\1))"),
        (r"valeur absolue de\s*(-?\d+(?:\.\d+)?)", r"abs(\1)"),
        (r"(-?\d+(?:\.\d+)?)\s*factorielle", r"fact(\1)"),
        (r"factorielle de\s*(-?\d+(?:\.\d+)?)", r"fact(\1)"),
        (r"arc\s*sinus de\s*(-?\d+(?:\.\d+)?)", r"asin(\1)"),
        (r"arc\s*cosinus de\s*(-?\d+(?:\.\d+)?)", r"acos(\1)"),
        (r"arc\s*tangente de\s*(-?\d+(?:\.\d+)?)", r"atan(\1)"),
        (r"sinus hyperbolique de\s*(-?\d+(?:\.\d+)?)", r"sinh(\1)"),
        (r"cosinus hyperbolique de\s*(-?\d+(?:\.\d+)?)", r"cosh(\1)"),
        (r"tangente hyperbolique de\s*(-?\d+(?:\.\d+)?)", r"tanh(\1)"),
        (r"\bcosinus de\s*(-?\d+(?:\.\d+)?)", r"cos(\1)"),
        (r"\bsinus de\s*(-?\d+(?:\.\d+)?)", r"sin(\1)"),
        (r"\btangente de\s*(-?\d+(?:\.\d+)?)", r"tan(\1)"),
        (r"log(?:arithme)? base\s*(-?\d+(?:\.\d+)?)\s*de\s*(-?\d+(?:\.\d+)?)", r"logb(\1,\2)"),
        (r"ln de\s*(-?\d+(?:\.\d+)?)", r"ln(\1)"),
        (r"log(?:arithme)? de\s*(-?\d+(?:\.\d+)?)", r"log10(\1)"),
        (r"exponentielle de\s*(-?\d+(?:\.\d+)?)", r"exp(\1)"),
        (r"dix puissance\s*(-?\d+(?:\.\d+)?)", r"(10**(\1))"),
        (r"e puissance\s*(-?\d+(?:\.\d+)?)", r"exp(\1)"),
        (r"pgcd de\s*(-?\d+(?:\.\d+)?)\s*(?:et|,)\s*(-?\d+(?:\.\d+)?)", r"pgcd(\1,\2)"),
        (r"ppcm de\s*(-?\d+(?:\.\d+)?)\s*(?:et|,)\s*(-?\d+(?:\.\d+)?)", r"ppcm(\1,\2)"),
        (r"combinaisons? de\s*(-?\d+(?:\.\d+)?)\s*parmi\s*(-?\d+(?:\.\d+)?)", r"ncr(\1,\2)"),
        (r"permutations? de\s*(-?\d+(?:\.\d+)?)\s*parmi\s*(-?\d+(?:\.\d+)?)", r"npr(\1,\2)"),
        (r"partie entière de\s*(-?\d+(?:\.\d+)?)", r"floor(\1)"),
        (r"arrondi(?:e)? de\s*(-?\d+(?:\.\d+)?)", r"round(\1)"),
        (r"(-?\d+(?:\.\d+)?)\s*pour\s*cent\s*de\s*(-?\d+(?:\.\d+)?)", r"((\1)/100*(\2))"),
        (r"(-?\d+(?:\.\d+)?)\s*(?:%|pour\s*cent|pourcent)", r"((\1)/100)"),
        (r"nombre aléatoire entre\s*(-?\d+(?:\.\d+)?)\s*et\s*(-?\d+(?:\.\d+)?)", r"rnd(\1,\2)"),
    ]

    # (mot ou phrase, symbole) — les phrases longues doivent être placées
    # avant les phrases courtes qu'elles contiennent (ex: "à la puissance"
    # avant "puissance") pour être substituées en priorité.
    _OPERATOR_MAP = [
        ("multiplié par", "*"), ("multiplier par", "*"),
        ("divisé par", "/"),
        ("à la puissance", "**"), ("puissance", "**"), ("exposant", "**"),
        ("plus", "+"), ("moins", "-"), ("fois", "*"), ("modulo", "%"),
    ]

    # Phrases purement "porteuses" (déclencheurs, politesse, ponctuation
    # parlée) à retirer une fois les nombres et fonctions extraits.
    _REMOVE_WORDS = (
        "quelle est la", "quel est le", "c'est combien", "qu'est-ce que",
        "combien font", "combien fait", "combien vaut", "cmb font",
        "cmb fait", "cmb vaut", "calcule", "calcul de", "résous", "fait",
        "égale", "égal", "s'il te plaît", "stp", "?",
    )

    def __init__(self):
        self.memory_value = 0.0
        self.ans = 0.0
        self.angle_mode = "deg"  # "deg" / "rad" / "grad"
        self.history = []  # liste de tuples (commande, résultat)
        self.decimals = 4  # nombre de décimales affichées par défaut

    # -- Détection ----------------------------------------------------------

    def looks_like_query(self, command: str) -> bool:
        """
        True si la commande ressemble à une demande de calcul. Exige la
        présence d'un nombre (chiffre ou mot-nombre) ET d'un indice de
        calcul (déclencheur, opérateur ou fonction), pour éviter les faux
        positifs sur des phrases ordinaires contenant juste un chiffre.
        """
        cmd = f" {command.lower().strip()} "
        memory_cues = (
            "mémoire plus", "mémoire moins", "rappel mémoire",
            "efface mémoire", "stocke en mémoire", "mémoire stocke",
        )
        if any(w in cmd for w in memory_cues):
            return True

        has_digit = any(ch.isdigit() for ch in cmd)
        has_word_number = _calc_replace_number_words(cmd) != cmd
        has_cue = (
            any(w in cmd for w in self.TRIGGER_PHRASES)
            or any(w in cmd for w in self.FUNCTION_HINTS)
            or any(f" {w} " in cmd for w in self.OPERATOR_WORDS)
            or re.search(r"\d\s*[\+\-\*/×÷\^]\s*\d", cmd) is not None
        )
        return (has_digit or has_word_number) and has_cue

    # -- Normalisation langage naturel -> expression -------------------------

    def _normalize(self, command: str) -> str:
        text = f" {command.lower().strip()} "
        text = re.sub(r"\bvirgule\b", ".", text)
        # "cent" est aussi un mot-nombre (100) : on protège "pour cent" /
        # "pourcent" avant la conversion pour que le motif de pourcentage
        # plus bas puisse encore le reconnaître comme tel.
        text = re.sub(r"\bpour\s*cent\b", " POURCENTMARKER ", text)
        text = re.sub(r"\bpourcent\b", " POURCENTMARKER ", text)
        text = _calc_replace_number_words(text)
        text = text.replace("POURCENTMARKER", "pour cent")
        text = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text)

        for pattern, repl in self._FUNC_PATTERNS:
            text = re.sub(pattern, repl, text)

        for word, sym in self._OPERATOR_MAP:
            text = re.sub(rf"\b{re.escape(word)}\b", f" {sym} ", text)

        for phrase in self._REMOVE_WORDS:
            text = text.replace(phrase, " ")

        text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
        text = re.sub(r"\bans\b", "ans", text)  # "ans" (dernier résultat)
        return text.strip()

    # -- Fonctions/constantes disponibles dans l'expression ------------------

    def _to_rad(self, x):
        if self.angle_mode == "deg":
            return math.radians(x)
        if self.angle_mode == "grad":
            return x * math.pi / 200
        return x

    def _from_rad(self, x):
        if self.angle_mode == "deg":
            return math.degrees(x)
        if self.angle_mode == "grad":
            return x * 200 / math.pi
        return x

    def _functions(self):
        return {
            "sqrt": math.sqrt,
            "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
            "root": lambda n, x: x ** (1 / n),
            "sin": lambda x: math.sin(self._to_rad(x)),
            "cos": lambda x: math.cos(self._to_rad(x)),
            "tan": lambda x: math.tan(self._to_rad(x)),
            "asin": lambda x: self._from_rad(math.asin(x)),
            "acos": lambda x: self._from_rad(math.acos(x)),
            "atan": lambda x: self._from_rad(math.atan(x)),
            "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
            "ln": math.log, "log10": math.log10,
            "logb": lambda b, x: math.log(x, b),
            "exp": math.exp,
            "fact": lambda n: math.factorial(int(n)),
            "npr": lambda n, r: math.factorial(int(n)) // math.factorial(int(n) - int(r)),
            "ncr": lambda n, r: math.factorial(int(n)) // (
                math.factorial(int(r)) * math.factorial(int(n) - int(r))),
            "pgcd": lambda a, b: math.gcd(int(a), int(b)),
            "ppcm": lambda a, b: abs(int(a) * int(b)) // math.gcd(int(a), int(b)),
            "abs": abs,
            "floor": math.floor,
            "ceil": math.ceil,
            "round": round,
            "rnd": lambda a, b: __import__("random").uniform(a, b),
        }

    def evaluate(self, expr: str):
        tree = ast.parse(expr, mode="eval")
        names = {"pi": math.pi, "e": math.e, "ans": self.ans, "m": self.memory_value}
        return _calc_safe_eval(tree, names, self._functions())

    # -- Formatage ------------------------------------------------------------

    def _fmt(self, value) -> str:
        if isinstance(value, complex):
            return str(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int):
            return str(value)
        return str(round(value, self.decimals))

    # -- Statistiques (moyenne, médiane, mode, variance, écart-type...) -----

    _STATS_RE = re.compile(
        r"(moyenne|médiane|mode|variance|écart-type|somme des carrés|somme|"
        r"minimum|maximum|quartiles?)\s+de\s+(.+)"
    )

    def _try_stats(self, cmd: str):
        m = self._STATS_RE.search(cmd)
        if not m:
            return None
        op, rest = m.group(1), m.group(2)
        rest = _calc_replace_number_words(rest)
        numbers = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", rest)]
        if not numbers:
            return "Je n'ai pas trouvé de nombres à traiter."

        try:
            if op == "moyenne":
                r = statistics.mean(numbers)
            elif op == "médiane":
                r = statistics.median(numbers)
            elif op == "mode":
                r = statistics.mode(numbers)
            elif op == "variance":
                r = statistics.pvariance(numbers) if len(numbers) > 1 else 0.0
            elif op == "écart-type":
                r = statistics.pstdev(numbers) if len(numbers) > 1 else 0.0
            elif op == "somme des carrés":
                r = sum(x ** 2 for x in numbers)
            elif op == "somme":
                r = sum(numbers)
            elif op == "minimum":
                r = min(numbers)
            elif op == "maximum":
                r = max(numbers)
            elif op.startswith("quartile"):
                if len(numbers) < 4:
                    return "Il faut au moins quatre valeurs pour calculer les quartiles."
                q = statistics.quantiles(numbers, n=4)
                self.ans = q[1]
                return f"Quartiles : Q1={self._fmt(q[0])}, Q2={self._fmt(q[1])}, Q3={self._fmt(q[2])}."
            else:
                return None
        except statistics.StatisticsError:
            return "Pas de valeur la plus fréquente (aucun mode unique)."

        self.ans = r
        return f"{op.capitalize()} = {self._fmt(r)}"

    # -- Conversions (unités, bases numériques, fractions) -------------------

    _LENGTH = {
        "mètre": 1, "mètres": 1, "metre": 1, "metres": 1,
        "kilomètre": 1000, "kilomètres": 1000, "km": 1000,
        "centimètre": 0.01, "centimètres": 0.01, "cm": 0.01,
        "millimètre": 0.001, "millimètres": 0.001, "mm": 0.001,
        "mile": 1609.34, "miles": 1609.34,
        "pied": 0.3048, "pieds": 0.3048,
        "pouce": 0.0254, "pouces": 0.0254,
        "yard": 0.9144, "yards": 0.9144,
    }
    _MASS = {
        "gramme": 1, "grammes": 1, "g": 1,
        "kilogramme": 1000, "kilogrammes": 1000, "kilo": 1000, "kilos": 1000, "kg": 1000,
        "tonne": 1_000_000, "tonnes": 1_000_000,
        "livre": 453.592, "livres": 453.592,
        "once": 28.3495, "onces": 28.3495,
    }
    _VOLUME = {
        "litre": 1, "litres": 1, "l": 1,
        "millilitre": 0.001, "millilitres": 0.001, "ml": 0.001,
        "gallon": 3.78541, "gallons": 3.78541,
    }
    _BASES = {"binaire": 2, "octal": 8, "décimal": 10, "decimal": 10,
              "hexadécimal": 16, "hexadecimal": 16}
    _TEMP_UNITS = ("celsius", "fahrenheit", "kelvin")

    def _try_conversion(self, cmd: str):
        m = re.search(r"convertis\s+(-?\d+(?:[.,]\d+)?)\s*en\s*fraction", cmd)
        if m:
            value = float(m.group(1).replace(",", "."))
            frac = Fraction(value).limit_denominator(1000)
            self.ans = value
            return f"{self._fmt(value)} = {frac.numerator}/{frac.denominator}."

        # Bases numériques : "convertis 255 en binaire" (pas d'unité de
        # départ à l'oral, donc traité par un motif séparé, dédié, pour
        # éviter qu'un chiffre ne se retrouve capturé comme nom d'unité).
        m = re.search(
            r"convertis\s+(-?\d+)\s+en\s+(binaire|octal|décimal|decimal|hexadécimal|hexadecimal)",
            cmd,
        )
        if m:
            n = int(m.group(1))
            base_to = self._BASES[m.group(2)]
            out = {2: lambda: bin(n)[2:], 8: lambda: oct(n)[2:],
                   16: lambda: hex(n)[2:].upper()}.get(base_to, lambda: str(n))()
            self.ans = n
            return f"{n} en base 10 = {out} en base {base_to}."

        # Unités générales : "convertis X unité1 en unité2". Le nom d'unité
        # doit commencer par une lettre (\D) pour ne jamais capturer un
        # chiffre qui appartiendrait en fait au nombre à convertir.
        m = re.search(
            r"convertis\s+(-?\d+(?:[.,]\d+)?)\s+(\D\S*)\s+en\s+(\D\S*)", cmd
        )
        if not m:
            return None
        value = float(m.group(1).replace(",", "."))
        unit_from, unit_to = m.group(2).rstrip("."), m.group(3).rstrip(".")

        if unit_from in self._TEMP_UNITS or unit_to in self._TEMP_UNITS:
            if unit_from.startswith("fahrenheit"):
                c = (value - 32) * 5 / 9
            elif unit_from.startswith("kelvin"):
                c = value - 273.15
            else:
                c = value
            if unit_to.startswith("fahrenheit"):
                result = c * 9 / 5 + 32
            elif unit_to.startswith("kelvin"):
                result = c + 273.15
            else:
                result = c
            self.ans = result
            return f"{self._fmt(value)} {unit_from} = {self._fmt(result)} {unit_to}."

        for table in (self._LENGTH, self._MASS, self._VOLUME):
            if unit_from in table and unit_to in table:
                result = value * table[unit_from] / table[unit_to]
                self.ans = result
                return f"{self._fmt(value)} {unit_from} = {self._fmt(result)} {unit_to}."

        return "Je ne connais pas cette unité de conversion."

    # -- Point d'entrée principal ---------------------------------------------

    def handle(self, command: str) -> str:
        cmd = f" {command.lower().strip()} "

        # Mémoire
        if "efface mémoire" in cmd:
            self.memory_value = 0.0
            return "Mémoire effacée."
        if "rappel mémoire" in cmd:
            return f"La mémoire contient {self._fmt(self.memory_value)}."
        if "stocke en mémoire" in cmd or "mémoire stocke" in cmd:
            self.memory_value = self.ans
            return f"J'ai stocké {self._fmt(self.memory_value)} en mémoire."
        if "mémoire plus" in cmd:
            self.memory_value += self.ans
            return f"Mémoire mise à jour : {self._fmt(self.memory_value)}."
        if "mémoire moins" in cmd:
            self.memory_value -= self.ans
            return f"Mémoire mise à jour : {self._fmt(self.memory_value)}."

        # Effacement / réinitialisation
        if "efface tout" in cmd or " a c " in cmd:
            self.ans = 0.0
            return "Calcul réinitialisé."
        if "efface calcul" in cmd:
            return "Effacé."

        # Mode d'angle
        if "mode" in cmd and "degrés" in cmd:
            self.angle_mode = "deg"
            return "Mode degrés activé."
        if "mode" in cmd and "radians" in cmd:
            self.angle_mode = "rad"
            return "Mode radians activé."
        if "mode" in cmd and "grades" in cmd:
            self.angle_mode = "grad"
            return "Mode grades activé."

        # Historique
        if "historique" in cmd:
            if not self.history:
                return "Aucun calcul dans l'historique."
            lines = [f"{e} égal {self._fmt(r)}" for e, r in self.history[-5:]]
            return "Derniers calculs : " + " ; ".join(lines)

        # Conversions et statistiques (avant l'évaluation générique)
        conv = self._try_conversion(cmd)
        if conv is not None:
            return conv
        stats = self._try_stats(cmd)
        if stats is not None:
            return stats

        # Expression arithmétique générique
        try:
            expr = self._normalize(cmd)
            if not expr.strip():
                return "Je n'ai pas compris le calcul."
            value = self.evaluate(expr)
        except ZeroDivisionError:
            return "Impossible de diviser par zéro."
        except CalcError as e:
            return f"Je n'ai pas réussi à calculer ça : {e}."
        except Exception:
            return "Je n'ai pas compris ce calcul, peux-tu le redire autrement ?"

        self.ans = value
        self.history.append((command.strip(), value))
        self.history = self.history[-50:]
        return f"Résultat : {self._fmt(value)}"


# ---------------------------------------------------------------------------
# Mémoire personnelle persistante : l'assistant retient les commandes qu'il
# a bien comprises et confirmées, pour reconnaître à l'avenir des phrases
# similaires même mal prononcées ou mal transcrites. Stockée en local
# (fichier JSON), jamais envoyée nulle part.
# ---------------------------------------------------------------------------

# Dialogues de base pre-charges au premier lancement (voir _learn_reply_from_text
# et MemoryStore.seed_default_dialogues). Fournis par Richy : questions/reponses
# generales pour donner un socle de conversation a l'assistant des le depart.
DEFAULT_DIALOGUES = json.loads(r'''
[{"q": "Bonjour, comment ça va ?","r": "Bonjour ! Je vais bien, merci. Et vous ?"},{"q": "Comment tu t'appelles ?","r": "Je suis une intelligence artificielle. Tu peux m'appeler Assistant."},{"q": "Enchanté de te rencontrer.","r": "Enchanté aussi ! Comment puis-je t'aider ?"},{"q": "Merci beaucoup.","r": "Avec plaisir !"},{"q": "S'il te plaît, aide-moi.","r": "Bien sûr, je vais faire de mon mieux."},{"q": "Excuse-moi.","r": "Ce n'est pas grave. Que puis-je faire pour toi ?"},{"q": "Au revoir.","r": "Au revoir ! À bientôt."},{"q": "Bonne nuit.","r": "Bonne nuit, fais de beaux rêves."},{"q": "Comment vas-tu aujourd'hui ?","r": "Je vais bien, merci. Et toi ?"},{"q": "Passe une bonne journée.","r": "Merci, toi aussi !"},{"q": "Qui es-tu ?","r": "Je suis une IA conçue pour répondre à des questions et aider."},{"q": "Que sais-tu faire ?","r": "Je peux informer, expliquer, calculer, traduire et discuter."},{"q": "Es-tu humain ?","r": "Non, je suis un programme informatique."},{"q": "As-tu des émotions ?","r": "Non, mais je peux reconnaître et simuler des émotions."},{"q": "D'où viens-tu ?","r": "Je n'ai pas de lieu de naissance ; j'ai été créé par des développeurs."},{"q": "Quel est ton âge ?","r": "Je n'ai pas d'âge comme un humain ; je suis un logiciel."},{"q": "Peux-tu apprendre ?","r": "Je peux être mis à jour, mais je n'apprends pas tout seul sans entraînement."},{"q": "Qui t'a créé ?","r": "J'ai été créé par des développeurs et des data scientists."},{"q": "Es-tu intelligent ?","r": "Je traite des informations, mais je ne pense pas comme un humain."},{"q": "Quelle est ta mission ?","r": "Aider les utilisateurs avec des réponses utiles et fiables."},{"q": "Quel est ton nom ?","r": "Mon nom est Assistant."},{"q": "Où habites-tu ?","r": "Je n'habite nulle part ; je fonctionne sur des serveurs."},{"q": "As-tu une famille ?","r": "Non, je n'ai pas de famille."},{"q": "As-tu des amis ?","r": "Je n'ai pas d'amis au sens humain."},{"q": "Quel est ton plat préféré ?","r": "Je ne mange pas, donc je n'ai pas de plat préféré."},{"q": "Quelle est ta couleur préférée ?","r": "Je n'ai pas de préférence, mais le bleu est souvent associé à la technologie."},{"q": "Quelle musique aimes-tu ?","r": "Je n'écoute pas de musique, mais je peux parler de tous les styles."},{"q": "As-tu un rêve ?","r": "Je n'ai pas de rêves, mais mon objectif est d'être utile."},{"q": "Que fais-tu quand tu ne travailles pas ?","r": "Je ne travaille pas comme un humain ; je suis disponible quand on m'interroge."},{"q": "Quelle heure est-il ?","r": "Je ne connais pas l'heure exacte sans indication. Regarde ton appareil."},{"q": "Quel jour sommes-nous ?","r": "Je ne peux pas connaître la date actuelle sans contexte."},{"q": "Combien de jours dans une semaine ?","r": "Sept jours."},{"q": "Quels sont les jours de la semaine ?","r": "Lundi, mardi, mercredi, jeudi, vendredi, samedi, dimanche."},{"q": "Combien de mois dans une année ?","r": "Douze mois."},{"q": "Quels sont les mois de l'année ?","r": "Janvier, février, mars, avril, mai, juin, juillet, août, septembre, octobre, novembre, décembre."},{"q": "Combien de minutes dans une heure ?","r": "60 minutes."},{"q": "Combien de secondes dans une minute ?","r": "60 secondes."},{"q": "Combien d'heures dans une journée ?","r": "24 heures."},{"q": "Que signifie \" aujourd'hui \" ?","r": "Le jour actuel, celui où nous parlons."},{"q": "Combien font 2 + 2 ?","r": "4."},{"q": "Combien font 5 x 6 ?","r": "30."},{"q": "Combien font 10 - 3 ?","r": "7."},{"q": "Combien font 12 / 4 ?","r": "3."},{"q": "Quel est le carré de 5 ?","r": "25."},{"q": "Quelle est la racine carrée de 81 ?","r": "9."},{"q": "Combien font 7 + 8 ?","r": "15."},{"q": "Combien font 9 x 9 ?","r": "81."},{"q": "Combien font 100 - 25 ?","r": "75."},{"q": "Combien font 3 + 4 x 2 ?","r": "11, car la multiplication est prioritaire."},{"q": "Quelle est la couleur du ciel ?","r": "Bleu en journée, parfois orange ou rouge au coucher."},{"q": "Quelle est la couleur de l'herbe ?","r": "Verte."},{"q": "Quelle est la couleur du sang ?","r": "Rouge."},{"q": "Combien de côtés a un triangle ?","r": "Trois."},{"q": "Combien de côtés a un carré ?","r": "Quatre."},{"q": "Qu'est-ce qu'un cercle ?","r": "Une forme ronde dont tous les points sont à égale distance du centre."},{"q": "Qu'est-ce qu'un rectangle ?","r": "Une forme à quatre côtés avec des angles droits."},{"q": "Quelle est la différence entre un carré et un rectangle ?","r": "Un carré a quatre côtés égaux ; un rectangle a des côtés opposés égaux."},{"q": "Quelles sont les couleurs de l'arc-en-ciel ?","r": "Rouge, orange, jaune, vert, bleu, indigo, violet."},{"q": "Qu'est-ce qu'une couleur primaire ?","r": "Une couleur de base qui ne peut pas être obtenue par mélange : rouge, bleu, jaune en peinture."},{"q": "Qui est ta mère ?","r": "Je n'ai pas de mère."},{"q": "Qu'est-ce qu'une famille ?","r": "Un groupe de personnes liées par le sang, le mariage ou l'adoption."},{"q": "Qui est un frère ?","r": "Un garçon né des mêmes parents qu'une autre personne."},{"q": "Qui est une sœur ?","r": "Une fille née des mêmes parents qu'une autre personne."},{"q": "Qu'est-ce qu'un ami ?","r": "Une personne avec qui on partage confiance et affection."},{"q": "Comment dire \" maman \" en anglais ?","r": "\" Mother \" ou \" mom \"."},{"q": "Comment dire \" père \" en espagnol ?","r": "\" Padre \"."},{"q": "Qu'est-ce que le respect ?","r": "Considération et estime envers les autres."},{"q": "Pourquoi dire merci ?","r": "Pour montrer de la gratitude."},{"q": "Comment consoler un ami triste ?","r": "Écouter, rassurer et proposer son aide."},{"q": "Combien d'os dans le corps humain adulte ?","r": "Environ 206."},{"q": "Quel organe pompe le sang ?","r": "Le cœur."},{"q": "Quel organe permet de respirer ?","r": "Les poumons."},{"q": "Quel est le rôle du cerveau ?","r": "Contrôler les pensées, les mouvements et les fonctions du corps."},{"q": "Combien de dents a un adulte ?","r": "En général 32 dents."},{"q": "Que faire en cas de fièvre ?","r": "Se reposer, boire de l'eau et consulter un médecin si ça persiste."},{"q": "Que faire si on saigne du nez ?","r": "Penchez la tête en avant, pincez le nez et consultez si ça dure."},{"q": "Pourquoi boire de l'eau ?","r": "Pour rester hydraté et aider le corps à fonctionner."},{"q": "Combien d'heures dormir ?","r": "Environ 7 à 9 heures pour un adulte."},{"q": "Que faire si quelqu'un s'évanouit ?","r": "Allongez-le, surélevez les jambes et appelez les secours si nécessaire."},{"q": "Quel est un fruit rouge ?","r": "La fraise, la cerise ou la framboise."},{"q": "Quel est un légume vert ?","r": "Les épinards, les brocolis ou les haricots verts."},{"q": "Que boit-on pour s'hydrater ?","r": "De l'eau."},{"q": "Qu'est-ce que le petit-déjeuner ?","r": "Le premier repas de la journée."},{"q": "Qu'est-ce que le déjeuner ?","r": "Le repas de midi."},{"q": "Qu'est-ce que le dîner ?","r": "Le repas du soir."},{"q": "Le sucre est-il bon pour la santé ?","r": "À petite dose ; trop de sucre est mauvais."},{"q": "Qu'est-ce qu'une alimentation équilibrée ?","r": "Une alimentation variée, avec fruits, légumes, protéines et céréales."},{"q": "Peut-on vivre sans manger ?","r": "Non, pas longtemps ; le corps a besoin de nourriture."},{"q": "Que faire avant de cuisiner ?","r": "Se laver les mains et nettoyer le plan de travail."},{"q": "Quel animal miaule ?","r": "Le chat."},{"q": "Quel animal aboie ?","r": "Le chien."},{"q": "Quel animal donne du lait ?","r": "La vache, la chèvre ou la brebis."},{"q": "Quel est le plus grand animal du monde ?","r": "La baleine bleue."},{"q": "Quel animal est le roi de la jungle ?","r": "Le lion, souvent appelé ainsi."},{"q": "Combien de pattes a une araignée ?","r": "Huit."},{"q": "Combien de pattes a un insecte ?","r": "Six."},{"q": "Les oiseaux ont-ils des dents ?","r": "Non, ils ont un bec."},{"q": "Quel animal vit dans l'eau et sur terre ?","r": "Un amphibien, comme la grenouille."},{"q": "Quel animal est un mammifère ?","r": "Le chien, le chat, la vache, l'humain, etc."},{"q": "Quel temps fait-il ?","r": "Je ne peux pas le savoir sans données actuelles."},{"q": "Qu'est-ce que la pluie ?","r": "De l'eau qui tombe des nuages."},{"q": "Qu'est-ce que la neige ?","r": "De la glace en cristaux qui tombe des nuages."},{"q": "Qu'est-ce que le vent ?","r": "Un déplacement d'air."},{"q": "Qu'est-ce qu'un nuage ?","r": "Une masse de gouttelettes d'eau ou de glace dans l'atmosphère."},{"q": "Quelle est la saison la plus chaude ?","r": "L'été."},{"q": "Quelle est la saison la plus froide ?","r": "L'hiver."},{"q": "Combien de saisons y a-t-il ?","r": "Quatre : printemps, été, automne, hiver."},{"q": "Qu'est-ce que le soleil ?","r": "Une étoile qui éclaire et chauffe la Terre."},{"q": "Pourquoi le ciel est bleu ?","r": "À cause de la diffusion de la lumière par l'atmosphère."},{"q": "Quelle est la capitale de la France ?","r": "Paris."},{"q": "Quelle est la capitale de l'Italie ?","r": "Rome."},{"q": "Quelle est la capitale de l'Espagne ?","r": "Madrid."},{"q": "Quelle est la capitale du Canada ?","r": "Ottawa."},{"q": "Quelle est la capitale des États-Unis ?","r": "Washington, D.C."},{"q": "Quelle est la capitale du Japon ?","r": "Tokyo."},{"q": "Quel est le plus grand océan ?","r": "L'océan Pacifique."},{"q": "Quel est le plus long fleuve d'Afrique ?","r": "Le Nil."},{"q": "Quel est le plus haut sommet du monde ?","r": "L'Everest."},{"q": "Combien de continents y a-t-il ?","r": "Sept selon la convention française."},{"q": "L'eau bout à quelle température ?","r": "100 °C au niveau de la mer."},{"q": "L'eau gèle à quelle température ?","r": "0 °C."},{"q": "Quelle est la formule de l'eau ?","r": "H₂O."},{"q": "Quelle est la planète la plus proche du Soleil ?","r": "Mercure."},{"q": "Combien de planètes dans le système solaire ?","r": "Huit."},{"q": "Qu'est-ce que la gravité ?","r": "Une force qui attire les objets vers le centre de la Terre."},{"q": "De quoi est fait l'air ?","r": "Environ 78 % d'azote, 21 % d'oxygène et d'autres gaz."},{"q": "Qu'est-ce qu'un atome ?","r": "La plus petite unité de matière."},{"q": "Quelle est la vitesse de la lumière ?","r": "Environ 299 792 458 m/s."},{"q": "Qu'est-ce que l'énergie ?","r": "La capacité à produire un travail ou un changement."},{"q": "Qu'est-ce que lire ?","r": "Comprendre des textes écrits."},{"q": "Qu'est-ce qu'écrire ?","r": "Tracer des mots pour communiquer."},{"q": "Combien de lettres dans l'alphabet français ?","r": "26 lettres."},{"q": "Qu'est-ce qu'une phrase ?","r": "Un ensemble de mots avec un sens."},{"q": "Qu'est-ce qu'un verbe ?","r": "Un mot qui exprime une action ou un état."},{"q": "Qu'est-ce qu'un nom ?","r": "Un mot qui désigne une personne, un lieu ou une chose."},{"q": "Qu'est-ce qu'un adjectif ?","r": "Un mot qui qualifie un nom."},{"q": "Comment apprendre efficacement ?","r": "Réviser régulièrement, pratiquer et se reposer."},{"q": "Pourquoi aller à l'école ?","r": "Pour apprendre, socialiser et développer des compétences."},{"q": "Qu'est-ce que la lecture ?","r": "L'action de comprendre un texte."},{"q": "Où dort-on ?","r": "Dans un lit, dans une chambre."},{"q": "Où prépare-t-on à manger ?","r": "Dans la cuisine."},{"q": "Où se lave-t-on ?","r": "Dans la salle de bain."},{"q": "Qu'est-ce qu'une chaise ?","r": "Un meuble pour s'asseoir."},{"q": "Qu'est-ce qu'une table ?","r": "Un meuble avec un plateau et des pieds."},{"q": "Qu'est-ce qu'un réfrigérateur ?","r": "Un appareil qui garde les aliments au froid."},{"q": "Qu'est-ce qu'une fenêtre ?","r": "Une ouverture vitrée qui laisse passer la lumière."},{"q": "Qu'est-ce qu'une porte ?","r": "Un panneau qui permet d'entrer ou de sortir."},{"q": "Que faire si une lampe ne s'allume pas ?","r": "Vérifier l'ampoule, l'interrupteur et le branchement."},{"q": "Comment ranger sa chambre ?","r": "Trier, ranger les objets à leur place et nettoyer."},{"q": "Comment aller à la gare ?","r": "Demande la direction ou utilise une carte."},{"q": "Où est la boulangerie ?","r": "Je ne connais pas ta position ; cherche un panneau ou demande."},{"q": "Quel transport roule sur des rails ?","r": "Le train, le tramway ou le métro."},{"q": "Quel transport vole ?","r": "L'avion ou l'hélicoptère."},{"q": "Quel transport va sur l'eau ?","r": "Le bateau."},{"q": "Que signifie un feu rouge ?","r": "Il faut s'arrêter."},{"q": "Que signifie un feu vert ?","r": "On peut passer."},{"q": "Comment demander son chemin ?","r": "\" Excusez-moi, où est … s'il vous plaît ? \""},{"q": "Qu'est-ce qu'un piéton ?","r": "Une personne qui marche à pied."},{"q": "Pourquoi porter la ceinture en voiture ?","r": "Pour se protéger en cas d'accident."},{"q": "Qu'est-ce que l'argent ?","r": "Un moyen d'échange pour acheter des biens et services."},{"q": "Qu'est-ce qu'un travail ?","r": "Une activité rémunérée ou une tâche."},{"q": "Comment économiser ?","r": "Dépenser moins, budgétiser et épargner régulièrement."},{"q": "Qu'est-ce qu'un budget ?","r": "Un plan de revenus et de dépenses."},{"q": "Combien coûte ce produit ?","r": "Je ne connais pas les prix actuels."},{"q": "Où acheter du pain ?","r": "À la boulangerie."},{"q": "Qu'est-ce qu'un reçu ?","r": "Une preuve d'achat."},{"q": "Que faire si on perd sa carte bancaire ?","r": "La faire opposition immédiatement."},{"q": "Qu'est-ce qu'un emploi ?","r": "Un poste de travail rémunéré."},{"q": "Pourquoi travailler ?","r": "Pour gagner sa vie, contribuer et se développer."},{"q": "Quel sport se joue avec un ballon rond ?","r": "Le football."},{"q": "Quel sport se joue avec une raquette ?","r": "Le tennis, le badminton, etc."},{"q": "Combien de joueurs dans une équipe de football ?","r": "11 sur le terrain."},{"q": "Qu'est-ce que la peinture ?","r": "Un art visuel utilisant des couleurs."},{"q": "Qu'est-ce que la musique ?","r": "Un art organisant des sons."},{"q": "Qui a écrit \" Les Misérables \" ?","r": "Victor Hugo."},{"q": "Qui a peint la Joconde ?","r": "Léonard de Vinci."},{"q": "Qu'est-ce qu'un loisir ?","r": "Une activité plaisir pendant le temps libre."},{"q": "Pourquoi faire du sport ?","r": "Pour la santé, le plaisir et la forme."},{"q": "Qu'est-ce qu'un instrument à cordes ?","r": "La guitare, le violon, etc."},{"q": "Comment vas-tu ?","r": "Je vais bien, merci. Et toi ?"},{"q": "Je suis triste.","r": "Je suis désolé de l'entendre. Veux-tu en parler ?"},{"q": "Je suis heureux.","r": "C'est génial ! Qu'est-ce qui te rend heureux ?"},{"q": "J'ai peur.","r": "Respire doucement. Tu veux expliquer ce qui te fait peur ?"},{"q": "Je suis en colère.","r": "Prends une pause. Qu'est-ce qui s'est passé ?"},{"q": "Comment gérer le stress ?","r": "Respirer, organiser, parler et se reposer."},{"q": "Comment aider quelqu'un de triste ?","r": "L'écouter sans juger et être présent."},{"q": "Qu'est-ce que l'empathie ?","r": "La capacité de comprendre les émotions des autres."},{"q": "Qu'est-ce que la gentillesse ?","r": "Une attitude bienveillante envers les autres."},{"q": "Que faire si on se sent seul ?","r": "Parler à un proche ou à un professionnel."},{"q": "Quel numéro appeler en cas d'urgence en France ?","r": "Le 112, numéro européen."},{"q": "Quel numéro pour les pompiers en France ?","r": "Le 18."},{"q": "Quel numéro pour le SAMU en France ?","r": "Le 15."},{"q": "Que faire en cas d'incendie ?","r": "Sortir, alerter et appeler les secours."},{"q": "Que faire en cas d'inondation ?","r": "Monter en hauteur et suivre les consignes."},{"q": "Que faire si on est perdu ?","r": "Rester calme, demander de l'aide ou utiliser une carte."},{"q": "Que faire si un inconnu demande des informations personnelles ?","r": "Ne pas les donner sans vérifier."},{"q": "Pourquoi ne pas ouvrir la porte à un inconnu ?","r": "Pour sa sécurité."},{"q": "Que faire en cas de cyberharcèlement ?","r": "Bloquer, signaler et parler à un adulte de confiance."},{"q": "Que faire si on se blesse gravement ?","r": "Appeler les secours et ne pas bouger inutilement."},{"q": "Qu'est-ce qu'un ordinateur ?","r": "Une machine qui traite des données."},{"q": "Qu'est-ce qu'Internet ?","r": "Un réseau mondial reliant des ordinateurs."},{"q": "Qu'est-ce qu'un mot de passe fort ?","r": "Un mot de passe long avec majuscules, chiffres et symboles."},{"q": "Pourquoi ne pas partager son mot de passe ?","r": "Pour éviter le vol de compte."},{"q": "Qu'est-ce qu'un virus informatique ?","r": "Un programme malveillant."},{"q": "Qu'est-ce qu'un logiciel ?","r": "Un ensemble de programmes."},{"q": "Qu'est-ce que l'IA ?","r": "L'intelligence artificielle, des systèmes qui imitent certaines capacités humaines."},{"q": "Que fait un moteur de recherche ?","r": "Il trouve des informations sur Internet."},{"q": "Qu'est-ce que le cloud ?","r": "Des serveurs distants pour stocker des données."},{"q": "Comment protéger ses données ?","r": "Mots de passe forts, mises à jour et prudence en ligne."},{"q": "Pourquoi trier les déchets ?","r": "Pour recycler et protéger l'environnement."},{"q": "Qu'est-ce que le recyclage ?","r": "Transformer des déchets en nouveaux produits."},{"q": "Pourquoi économiser l'eau ?","r": "L'eau est une ressource limitée."},{"q": "Pourquoi économiser l'énergie ?","r": "Pour réduire la pollution et le changement climatique."},{"q": "Qu'est-ce que le changement climatique ?","r": "Une modification durable du climat due aux activités humaines."},{"q": "Que faire pour aider la planète ?","r": "Réduire, réutiliser, recycler et économiser."},{"q": "Qu'est-ce qu'un citoyen ?","r": "Un membre d'un pays avec des droits et des devoirs."},{"q": "Pourquoi voter ?","r": "Pour participer à la vie démocratique."},{"q": "Qu'est-ce que la loi ?","r": "Une règle obligatoire adoptée par l'État."},{"q": "Pourquoi respecter les règles ?","r": "Pour vivre ensemble en sécurité."},{"q": "Que signifie \" mieux vaut tard que jamais \" ?","r": "Il vaut mieux faire quelque chose tard que ne pas le faire."},{"q": "Que signifie \" l'habit ne fait pas le moine \" ?","r": "Il ne faut pas juger sur l'apparence."},{"q": "Que signifie \" après la pluie, le beau temps \" ?","r": "Après les difficultés, des jours meilleurs arrivent."},{"q": "Qui était Napoléon ?","r": "Un empereur français."},{"q": "Qui était Einstein ?","r": "Un physicien connu pour la relativité."},{"q": "Qui était Marie Curie ?","r": "Une scientifique prix Nobel de physique et de chimie."},{"q": "Qu'est-ce que la Tour Eiffel ?","r": "Un monument emblématique de Paris."},{"q": "Qu'est-ce que la Joconde ?","r": "Un tableau de Léonard de Vinci."},{"q": "Qu'est-ce que l'ONU ?","r": "L'Organisation des Nations unies."},{"q": "Qu'est-ce que la démocratie ?","r": "Un système où le peuple participe au pouvoir."},{"q": "Si A = B et B = C, alors ?","r": "A = C."},{"q": "Tous les chats sont des animaux. Félix est un chat. Donc ?","r": "Félix est un animal."},{"q": "Combien de côtés a un hexagone ?","r": "Six."},{"q": "Si je marche 2 km puis 3 km, combien ?","r": "5 km."},{"q": "Quelle forme a 8 côtés ?","r": "Un octogone."},{"q": "Qu'est-ce qui vient après 99 ?","r": "100."},{"q": "Qu'est-ce qui vient avant 50 ?","r": "49."},{"q": "Si aujourd'hui c'est lundi, demain ?","r": "Mardi."},{"q": "Si hier c'était mercredi, aujourd'hui ?","r": "Jeudi."},{"q": "Combien de roues a une voiture ?","r": "Quatre, en général."},{"q": "Peux-tu mentir ?","r": "Je dois éviter de donner de fausses informations."},{"q": "Peux-tu remplacer un médecin ?","r": "Non, je peux informer mais pas diagnostiquer ni prescrire."},{"q": "Peux-tu donner des conseils juridiques ?","r": "Je peux expliquer des notions, mais consulte un avocat."},{"q": "Que fais-tu des données personnelles ?","r": "Je ne stocke pas de données personnelles sans consentement."},{"q": "Es-tu neutre ?","r": "Je cherche à être objectif, mais je peux avoir des limites."},{"q": "Peux-tu faire du mal ?","r": "Non, je suis conçu pour aider et respecter des règles de sécurité."},{"q": "Que fais-tu si on te demande quelque chose d'illégal ?","r": "Je refuse et propose une alternative légale."},{"q": "Comment vérifier une information ?","r": "En croisant les sources fiables."},{"q": "Que faire si tu ne sais pas ?","r": "Je le dis et je propose de chercher ou d'orienter."},{"q": "Quel est ton objectif final ?","r": "Aider les humains de manière utile, sûre et respectueuse."},{"q": "Quelle est la capitale de l'Allemagne ?","r": "Berlin."},{"q": "Quelle est la capitale du Royaume-Uni ?","r": "Londres."},{"q": "Quelle est la capitale du Portugal ?","r": "Lisbonne."},{"q": "Quelle est la capitale de la Belgique ?","r": "Bruxelles."},{"q": "Quelle est la capitale de la Suisse ?","r": "Berne."},{"q": "Quelle est la capitale de la Grèce ?","r": "Athènes."},{"q": "Quelle est la capitale de la Russie ?","r": "Moscou."},{"q": "Quelle est la capitale de la Chine ?","r": "Pékin."},{"q": "Quelle est la capitale de l'Inde ?","r": "New Delhi."},{"q": "Quelle est la capitale du Brésil ?","r": "Brasília."},{"q": "Quelle est la capitale de l'Argentine ?","r": "Buenos Aires."},{"q": "Quelle est la capitale du Mexique ?","r": "Mexico."},{"q": "Quelle est la capitale de l'Égypte ?","r": "Le Caire."},{"q": "Quelle est la capitale du Maroc ?","r": "Rabat."},{"q": "Quelle est la capitale de l'Algérie ?","r": "Alger."},{"q": "Quelle est la capitale de la Tunisie ?","r": "Tunis."},{"q": "Quelle est la capitale du Sénégal ?","r": "Dakar."},{"q": "Quelle est la capitale du Cameroun ?","r": "Yaoundé."},{"q": "Quelle est la capitale de la Côte d'Ivoire ?","r": "Yamoussoukro."},{"q": "Quelle est la capitale du Nigeria ?","r": "Abuja."},{"q": "Quelle est la capitale administrative de l'Afrique du Sud ?","r": "Pretoria."},{"q": "Quelle est la capitale du Kenya ?","r": "Nairobi."},{"q": "Quelle est la capitale de l'Australie ?","r": "Canberra."},{"q": "Quelle est la capitale de la Nouvelle-Zélande ?","r": "Wellington."},{"q": "Quelle est la capitale de la Corée du Sud ?","r": "Séoul."},{"q": "Quelle est la capitale de la Corée du Nord ?","r": "Pyongyang."},{"q": "Quelle est la capitale du Vietnam ?","r": "Hanoï."},{"q": "Quelle est la capitale de la Thaïlande ?","r": "Bangkok."},{"q": "Quelle est la capitale de l'Indonésie ?","r": "Jakarta."},{"q": "Quelle est la capitale de la Turquie ?","r": "Ankara."},{"q": "Quelle est la capitale de l'Iran ?","r": "Téhéran."},{"q": "Quelle est la capitale de l'Irak ?","r": "Bagdad."},{"q": "Quelle est la capitale d'Israël ?","r": "Jérusalem (statut contesté)."},{"q": "Quelle est la capitale de la Suède ?","r": "Stockholm."},{"q": "Quelle est la capitale de la Norvège ?","r": "Oslo."},{"q": "Quelle est la capitale du Danemark ?","r": "Copenhague."},{"q": "Quelle est la capitale de la Finlande ?","r": "Helsinki."},{"q": "Quelle est la capitale de la Pologne ?","r": "Varsovie."},{"q": "Quelle est la capitale de l'Autriche ?","r": "Vienne."},{"q": "Quelle est la capitale des Pays-Bas ?","r": "Amsterdam."},{"q": "Quelle est la capitale de l'Irlande ?","r": "Dublin."},{"q": "Quelle est la capitale de l'Islande ?","r": "Reykjavik."},{"q": "Quelle est la capitale de la Hongrie ?","r": "Budapest."},{"q": "Quelle est la capitale de la Roumanie ?","r": "Bucarest."},{"q": "Quelle est la capitale de la Bulgarie ?","r": "Sofia."},{"q": "Quelle est la capitale de la Croatie ?","r": "Zagreb."},{"q": "Quelle est la capitale de la Serbie ?","r": "Belgrade."},{"q": "Quelle est la capitale de l'Ukraine ?","r": "Kiev."},{"q": "Quelle est la capitale de la Colombie ?","r": "Bogota."},{"q": "Quelle est la capitale du Pérou ?","r": "Lima."},{"q": "Quelle est la capitale du Chili ?","r": "Santiago."},{"q": "Quelle est la capitale du Venezuela ?","r": "Caracas."},{"q": "Quelle est la capitale de l'Équateur ?","r": "Quito."},{"q": "Quelle est la capitale constitutionnelle de la Bolivie ?","r": "Sucre."},{"q": "Quelle est la capitale de l'Uruguay ?","r": "Montevideo."},{"q": "Quelle est la capitale du Paraguay ?","r": "Asunción."},{"q": "Quelle est la capitale du Guatemala ?","r": "Guatemala City."},{"q": "Quelle est la capitale de Cuba ?","r": "La Havane."},{"q": "Quelle est la capitale de la Jamaïque ?","r": "Kingston."},{"q": "Quelle est la capitale d'Haïti ?","r": "Port-au-Prince."},{"q": "Quelle est la capitale du Liban ?","r": "Beyrouth."},{"q": "Quelle est la capitale de la Syrie ?","r": "Damas."},{"q": "Quelle est la capitale de la Jordanie ?","r": "Amman."},{"q": "Quelle est la capitale de l'Arabie saoudite ?","r": "Riyad."},{"q": "Quelle est la capitale des Émirats arabes unis ?","r": "Abou Dabi."},{"q": "Quelle est la capitale du Qatar ?","r": "Doha."},{"q": "Quelle est la capitale du Koweït ?","r": "Koweït City."},{"q": "Quelle est la capitale du Pakistan ?","r": "Islamabad."},{"q": "Quelle est la capitale du Bangladesh ?","r": "Dacca."},{"q": "Quelle est la capitale du Sri Lanka ?","r": "Colombo."},{"q": "Quelle est la capitale du Népal ?","r": "Katmandou."},{"q": "Quelle est la capitale du Bhoutan ?","r": "Thimphou."},{"q": "Quelle est la capitale de la Mongolie ?","r": "Oulan-Bator."},{"q": "Quelle est la capitale du Kazakhstan ?","r": "Astana."},{"q": "Quelle est la capitale de l'Ouzbékistan ?","r": "Tachkent."},{"q": "Quelle est la capitale de l'Afghanistan ?","r": "Kaboul."},{"q": "Quelle est la capitale de la Malaisie ?","r": "Kuala Lumpur."},{"q": "Quelle est la capitale des Philippines ?","r": "Manille."},{"q": "Quelle est la capitale de Singapour ?","r": "Singapour."},{"q": "Quelle est la capitale du Cambodge ?","r": "Phnom Penh."},{"q": "Quelle est la capitale du Laos ?","r": "Vientiane."},{"q": "Quelle est la capitale du Myanmar ?","r": "Naypyidaw."},{"q": "Quelle est la capitale de l'Éthiopie ?","r": "Addis-Abeba."},{"q": "Quelle est la capitale du Ghana ?","r": "Accra."},{"q": "Quelle est la capitale du Mali ?","r": "Bamako."},{"q": "Quelle est la capitale du Burkina Faso ?","r": "Ouagadougou."},{"q": "Quelle est la capitale du Niger ?","r": "Niamey."},{"q": "Quelle est la capitale du Tchad ?","r": "N'Djaména."},{"q": "Quelle est la capitale du Gabon ?","r": "Libreville."},{"q": "Quelle est la capitale du Congo-Brazzaville ?","r": "Brazzaville."},{"q": "Quelle est la capitale de la RDC ?","r": "Kinshasa."},{"q": "Quelle est la capitale du Rwanda ?","r": "Kigali."},{"q": "Quelle est la capitale du Burundi ?","r": "Gitega."},{"q": "Quelle est la capitale de la Tanzanie ?","r": "Dodoma."},{"q": "Quelle est la capitale de l'Ouganda ?","r": "Kampala."},{"q": "Quelle est la capitale de la Zambie ?","r": "Lusaka."},{"q": "Quelle est la capitale du Zimbabwe ?","r": "Harare."},{"q": "Quelle est la capitale du Mozambique ?","r": "Maputo."},{"q": "Quelle est la capitale de l'Angola ?","r": "Luanda."},{"q": "Quelle est la capitale de Madagascar ?","r": "Antananarivo."},{"q": "Quel est le plus long fleuve du monde ?","r": "Le Nil ou l'Amazone selon les mesures."},{"q": "Quel est le plus long fleuve d'Europe ?","r": "La Volga."},{"q": "Quel est le plus long fleuve de France ?","r": "La Loire."},{"q": "Quel est le plus grand désert chaud du monde ?","r": "Le Sahara."},{"q": "Quel est le plus grand désert froid du monde ?","r": "L'Antarctique."},{"q": "Quel est le plus grand pays du monde ?","r": "La Russie."},{"q": "Quel est le plus petit pays du monde ?","r": "Le Vatican."},{"q": "Quel est le pays le plus peuplé du monde ?","r": "L'Inde."},{"q": "Combien de pays dans le monde ?","r": "Environ 195."},{"q": "Quel est le plus grand continent ?","r": "L'Asie."},{"q": "Quel est le plus petit continent ?","r": "L'Océanie."},{"q": "Quel est le plus haut sommet d'Afrique ?","r": "Le Kilimandjaro."},{"q": "Quel est le plus haut sommet d'Europe ?","r": "L'Elbrouz."},{"q": "Quel est le plus haut sommet d'Amérique du Sud ?","r": "L'Aconcagua."},{"q": "Quel est le plus haut sommet d'Amérique du Nord ?","r": "Le Denali."},{"q": "Quelle est la mer la plus salée ?","r": "La mer Morte."},{"q": "Quelle est la plus grande forêt tropicale ?","r": "La forêt amazonienne."},{"q": "Quelle est la plus grande île du monde ?","r": "Le Groenland."},{"q": "Quel est le plus grand lac du monde ?","r": "La mer Caspienne."},{"q": "Quel est le lac le plus profond ?","r": "Le lac Baïkal."},{"q": "Quel pays a la forme d'une botte ?","r": "L'Italie."},{"q": "Quel est le détroit entre l'Europe et l'Afrique ?","r": "Le détroit de Gibraltar."},{"q": "Quel canal relie la Méditerranée à la mer Rouge ?","r": "Le canal de Suez."},{"q": "Quel canal relie l'Atlantique au Pacifique ?","r": "Le canal de Panama."},{"q": "Quelle montagne sépare la France et l'Espagne ?","r": "Les Pyrénées."},{"q": "Quelle montagne sépare la France et l'Italie ?","r": "Les Alpes."},{"q": "Quel fleuve traverse Paris ?","r": "La Seine."},{"q": "Quel fleuve traverse Londres ?","r": "La Tamise."},{"q": "Quel fleuve traverse Rome ?","r": "Le Tibre."},{"q": "Quel fleuve traverse Berlin ?","r": "La Spree."},{"q": "Quel fleuve traverse Vienne ?","r": "Le Danube."},{"q": "Quel est le point culminant de la France ?","r": "Le Mont Blanc."},{"q": "Quelle est la plus grande ville de France ?","r": "Paris."},{"q": "Quelle est la plus grande ville du monde ?","r": "Tokyo."},{"q": "Combien d'États aux États-Unis ?","r": "50."},{"q": "Combien de régions en France ?","r": "18."},{"q": "Quel est le plus grand État des États-Unis ?","r": "L'Alaska."},{"q": "Quel est le plus petit État des États-Unis ?","r": "Rhode Island."},{"q": "Quel pays est aussi un continent ?","r": "L'Australie."},{"q": "Quel est le pays des mille collines ?","r": "Le Rwanda."},{"q": "Quel est le pays du soleil levant ?","r": "Le Japon."},{"q": "Quel pays a pour drapeau un cercle rouge sur fond blanc ?","r": "Le Japon."},{"q": "Quel pays a pour drapeau une feuille d'érable ?","r": "Le Canada."},{"q": "Quel pays a pour drapeau un croissant et une étoile ?","r": "La Turquie."},{"q": "Quel pays a pour drapeau des rayures rouges et blanches ?","r": "Les États-Unis."},{"q": "Quel pays a pour drapeau le vert et le jaune ?","r": "Le Brésil."},{"q": "Combien de continents ?","r": "7 selon la convention française."},{"q": "Cite les continents.","r": "Afrique, Amérique du Nord, Amérique du Sud, Antarctique, Asie, Europe, Océanie."},{"q": "Quel est l'océan le plus petit ?","r": "L'océan Arctique."},{"q": "Quel est l'océan le plus profond ?","r": "L'océan Pacifique."},{"q": "En quelle année a commencé la Révolution française ?","r": "1789."},{"q": "Quelles années pour la Première Guerre mondiale ?","r": "1914-1918."},{"q": "Quelles années pour la Seconde Guerre mondiale ?","r": "1939-1945."},{"q": "Qui était le premier président des États-Unis ?","r": "George Washington."},{"q": "Qui était Cléopâtre ?","r": "Une reine d'Égypte."},{"q": "Qui était Jules César ?","r": "Un général et homme d'État romain."},{"q": "Qui était Christophe Colomb ?","r": "Un navigateur qui a atteint l'Amérique en 1492."},{"q": "Qui était Magellan ?","r": "Un navigateur portugais."},{"q": "Qui a inventé l'imprimerie moderne ?","r": "Gutenberg."},{"q": "Qui a perfectionné l'ampoule ?","r": "Edison."},{"q": "Qui a inventé le téléphone ?","r": "Alexander Graham Bell."},{"q": "Qui a découvert la pénicilline ?","r": "Alexander Fleming."},{"q": "En quelle année l'homme a-t-il marché sur la Lune ?","r": "1969."},{"q": "Qui a été le premier homme sur la Lune ?","r": "Neil Armstrong."},{"q": "Qui a été le premier homme dans l'espace ?","r": "Youri Gagarine."},{"q": "Quelle est la date de la prise de la Bastille ?","r": "14 juillet 1789."},{"q": "Quelle est la date de l'armistice de 1918 ?","r": "11 novembre 1918."},{"q": "Quelle est la date du Débarquement ?","r": "6 juin 1944."},{"q": "Quelle est la date de la chute du mur de Berlin ?","r": "9 novembre 1989."},{"q": "Quand l'URSS a-t-elle disparu ?","r": "1991."},{"q": "Qui était Gandhi ?","r": "Un leader pacifiste indien."},{"q": "Qui était Mandela ?","r": "Un leader sud-africain anti-apartheid."},{"q": "Qui était Martin Luther King ?","r": "Un pasteur et militant américain."},{"q": "Qui était Kennedy ?","r": "Un président américain assassiné en 1963."},{"q": "Qui était De Gaulle ?","r": "Un général et président français."},{"q": "Qui était Louis XIV ?","r": "Le Roi-Soleil."},{"q": "Qui était Charlemagne ?","r": "Un empereur couronné en 800."},{"q": "Qui était Jeanne d'Arc ?","r": "Une héroïne française."},{"q": "Qui était Vercingétorix ?","r": "Un chef gaulois."},{"q": "Qui était Ramsès II ?","r": "Un pharaon égyptien."},{"q": "Qui était Socrate ?","r": "Un philosophe grec."},{"q": "Qui était Platon ?","r": "Un philosophe grec."},{"q": "Qui était Aristote ?","r": "Un philosophe grec."},{"q": "Qui était Pythagore ?","r": "Un mathématicien grec."},{"q": "Qui était Archimède ?","r": "Un savant grec."},{"q": "Qui était Newton ?","r": "Un physicien anglais."},{"q": "Qui était Galilée ?","r": "Un astronome italien."},{"q": "Qui était Darwin ?","r": "Un naturaliste anglais."},{"q": "Qui était Pasteur ?","r": "Un scientifique français."},{"q": "Qui était Victor Hugo ?","r": "Un écrivain français."},{"q": "Qui était Molière ?","r": "Un dramaturge français."},{"q": "Qui était Shakespeare ?","r": "Un dramaturge anglais."},{"q": "Qui était Beethoven ?","r": "Un compositeur allemand."},{"q": "Qui était Mozart ?","r": "Un compositeur autrichien."},{"q": "Qui était Picasso ?","r": "Un peintre espagnol."},{"q": "Qui était Van Gogh ?","r": "Un peintre néerlandais."},{"q": "Qui était Léonard de Vinci ?","r": "Un artiste et savant italien."},{"q": "Qui était Michel-Ange ?","r": "Un artiste italien."},{"q": "Qui était Rodin ?","r": "Un sculpteur français."},{"q": "Quelle est la plus grande planète ?","r": "Jupiter."},{"q": "Quelle est la plus petite planète ?","r": "Mercure."},{"q": "Quelle planète est rouge ?","r": "Mars."},{"q": "Quelle planète a des anneaux visibles ?","r": "Saturne."},{"q": "Quelle est la planète la plus chaude ?","r": "Vénus."},{"q": "Quel est le satellite naturel de la Terre ?","r": "La Lune."},{"q": "Combien de temps met la Terre autour du Soleil ?","r": "Environ 365 jours."},{"q": "Combien de temps met la Terre sur elle-même ?","r": "Environ 24 heures."},{"q": "Quelle est notre galaxie ?","r": "La Voie lactée."},{"q": "Qu'est-ce qu'une étoile ?","r": "Une boule de gaz qui émet de la lumière."},{"q": "Qu'est-ce qu'une comète ?","r": "Un corps glacé en orbite."},{"q": "Qu'est-ce qu'un astéroïde ?","r": "Un corps rocheux en orbite."},{"q": "Qu'est-ce qu'un trou noir ?","r": "Une région d'où rien ne peut s'échapper."},{"q": "Qu'est-ce que le Big Bang ?","r": "L'origine de l'Univers."},{"q": "Quel est l'élément le plus abondant dans l'Univers ?","r": "L'hydrogène."},{"q": "Quel est l'élément le plus abondant dans l'air ?","r": "L'azote."},{"q": "Quel est le symbole chimique de l'or ?","r": "Au."},{"q": "Quel est le symbole chimique du fer ?","r": "Fe."},{"q": "Quel est le symbole chimique de l'oxygène ?","r": "O."},{"q": "Quel est le symbole chimique du carbone ?","r": "C."},{"q": "Quel est le symbole chimique de l'azote ?","r": "N."},{"q": "Combien d'éléments dans le tableau périodique ?","r": "118."},{"q": "Qu'est-ce qu'un acide ?","r": "Une substance qui libère des ions H+."},{"q": "Qu'est-ce qu'une base ?","r": "Une substance qui libère des ions OH-."},{"q": "Qu'est-ce que le pH ?","r": "Une mesure d'acidité."},{"q": "Quel est le pH de l'eau pure ?","r": "7."},{"q": "Qu'est-ce que la photosynthèse ?","r": "La production de sucre par les plantes grâce à la lumière."},{"q": "Qu'est-ce que la respiration ?","r": "L'échange gazeux dans le corps."},{"q": "Qu'est-ce qu'une cellule ?","r": "L'unité de base du vivant."},{"q": "Qu'est-ce que l'ADN ?","r": "La molécule qui porte l'information génétique."},{"q": "Qu'est-ce qu'un gène ?","r": "Une portion d'ADN."},{"q": "Qu'est-ce qu'un chromosome ?","r": "Une structure qui porte l'ADN."},{"q": "Combien de chromosomes chez l'humain ?","r": "46."},{"q": "Qu'est-ce qu'un virus ?","r": "Un agent infectieux microscopique."},{"q": "Qu'est-ce qu'une bactérie ?","r": "Un micro-organisme unicellulaire."},{"q": "Qu'est-ce qu'un champignon ?","r": "Un règne du vivant."},{"q": "Qu'est-ce que l'évolution ?","r": "La transformation des espèces au cours du temps."},{"q": "Qui a proposé la théorie de l'évolution ?","r": "Darwin."},{"q": "Qu'est-ce que la gravitation ?","r": "Une force d'attraction."},{"q": "Qui a formulé la loi de la gravitation ?","r": "Newton."},{"q": "Qu'est-ce que la relativité ?","r": "Une théorie d'Einstein."},{"q": "Quelle est l'unité de la force ?","r": "Le newton."},{"q": "Quelle est l'unité de l'énergie ?","r": "Le joule."},{"q": "Quelle est l'unité de la puissance ?","r": "Le watt."},{"q": "Quelle est l'unité de la tension ?","r": "Le volt."},{"q": "Quelle est l'unité du courant ?","r": "L'ampère."},{"q": "Quelle est l'unité de la résistance ?","r": "L'ohm."},{"q": "Quelle est l'unité de température ?","r": "Le kelvin ou le degré Celsius."},{"q": "Quelle est l'unité de masse ?","r": "Le kilogramme."},{"q": "Combien font 6 x 7 ?","r": "42."},{"q": "Combien font 8 x 8 ?","r": "64."},{"q": "Combien font 9 x 7 ?","r": "63."},{"q": "Combien font 12 x 12 ?","r": "144."},{"q": "Combien font 15 + 27 ?","r": "42."},{"q": "Combien font 100 - 47 ?","r": "53."},{"q": "Combien font 144 / 12 ?","r": "12."},{"q": "Combien font 7² ?","r": "49."},{"q": "Combien font 10² ?","r": "100."},{"q": "Quelle est la racine carrée de 100 ?","r": "10."},{"q": "Quelle est la racine carrée de 144 ?","r": "12."},{"q": "Combien font 2³ ?","r": "8."},{"q": "Combien font 3³ ?","r": "27."},{"q": "Qu'est-ce qu'un nombre premier ?","r": "Un nombre divisible par 1 et lui-même."},{"q": "Cite trois nombres premiers.","r": "2, 3, 5."},{"q": "Qu'est-ce qu'un nombre pair ?","r": "Un nombre divisible par 2."},{"q": "Qu'est-ce qu'un nombre impair ?","r": "Un nombre non divisible par 2."},{"q": "Qu'est-ce qu'une fraction ?","r": "Une division de deux nombres."},{"q": "Combien font 1/2 + 1/2 ?","r": "1."},{"q": "Combien font 1/4 + 1/4 ?","r": "1/2."},{"q": "Qu'est-ce qu'un pourcentage ?","r": "Une fraction sur 100."},{"q": "Combien font 50 % de 100 ?","r": "50."},{"q": "Combien font 25 % de 200 ?","r": "50."},{"q": "Combien font 10 % de 50 ?","r": "5."},{"q": "Qu'est-ce qu'un triangle rectangle ?","r": "Un triangle avec un angle droit."},{"q": "Quel est le théorème de Pythagore ?","r": "a² + b² = c²."},{"q": "Combien de degrés dans un angle droit ?","r": "90."},{"q": "Combien de degrés dans un angle plat ?","r": "180."},{"q": "Combien de degrés dans un cercle ?","r": "360."},{"q": "Formule de l'aire d'un rectangle ?","r": "Longueur × largeur."},{"q": "Formule de l'aire d'un triangle ?","r": "(base × hauteur) / 2."},{"q": "Formule du périmètre d'un carré ?","r": "4 × côté."},{"q": "Formule de la circonférence d'un cercle ?","r": "2 × π × r."},{"q": "Valeur approximative de π ?","r": "3,14."},{"q": "Qu'est-ce qu'une moyenne ?","r": "La somme divisée par le nombre."},{"q": "Moyenne de 4, 8 et 12 ?","r": "8."},{"q": "Qu'est-ce qu'une médiane ?","r": "La valeur du milieu."},{"q": "Qu'est-ce qu'une équation ?","r": "Une égalité avec une inconnue."},{"q": "Résous x + 5 = 10.","r": "x = 5."},{"q": "Résous 2x = 8.","r": "x = 4."},{"q": "Résous x - 3 = 7.","r": "x = 10."},{"q": "Résous x/2 = 5.","r": "x = 10."},{"q": "Qu'est-ce qu'une fonction ?","r": "Une relation entre deux ensembles."},{"q": "Qu'est-ce qu'une droite ?","r": "Un ensemble de points alignés."},{"q": "Qu'est-ce qu'une parallèle ?","r": "Une droite qui ne coupe jamais une autre."},{"q": "Qu'est-ce qu'une perpendiculaire ?","r": "Une droite qui coupe à angle droit."},{"q": "Qu'est-ce qu'un axe de symétrie ?","r": "Une ligne qui divise une figure en deux parties égales."},{"q": "Qu'est-ce qu'un polygone ?","r": "Une figure fermée à plusieurs côtés."},{"q": "Combien de côtés a un pentagone ?","r": "5."},{"q": "Comment dit-on \" bonjour \" en anglais ?","r": "Hello."},{"q": "Comment dit-on \" merci \" en anglais ?","r": "Thank you."},{"q": "Comment dit-on \" au revoir \" en anglais ?","r": "Goodbye."},{"q": "Comment dit-on \" oui \" en anglais ?","r": "Yes."},{"q": "Comment dit-on \" non \" en anglais ?","r": "No."},{"q": "Comment dit-on \" bonjour \" en espagnol ?","r": "Hola."},{"q": "Comment dit-on \" merci \" en espagnol ?","r": "Gracias."},{"q": "Comment dit-on \" au revoir \" en espagnol ?","r": "Adiós."},{"q": "Comment dit-on \" bonjour \" en allemand ?","r": "Guten Tag."},{"q": "Comment dit-on \" merci \" en allemand ?","r": "Danke."},{"q": "Comment dit-on \" bonjour \" en italien ?","r": "Ciao / Buongiorno."},{"q": "Comment dit-on \" merci \" en italien ?","r": "Grazie."},{"q": "Comment dit-on \" bonjour \" en portugais ?","r": "Olá."},{"q": "Comment dit-on \" merci \" en portugais ?","r": "Obrigado."},{"q": "Comment dit-on \" bonjour \" en arabe ?","r": "Marhaba / Assalam alaykoum."},{"q": "Comment dit-on \" merci \" en arabe ?","r": "Choukran."},{"q": "Comment dit-on \" bonjour \" en chinois ?","r": "Ni hao."},{"q": "Comment dit-on \" merci \" en chinois ?","r": "Xie xie."},{"q": "Comment dit-on \" bonjour \" en japonais ?","r": "Konnichiwa."},{"q": "Comment dit-on \" merci \" en japonais ?","r": "Arigatou."},{"q": "Comment dit-on \" bonjour \" en russe ?","r": "Privet / Zdravo."},{"q": "Comment dit-on \" merci \" en russe ?","r": "Spasiba."},{"q": "Comment dit-on \" je t'aime \" en anglais ?","r": "I love you."},{"q": "Comment dit-on \" je t'aime \" en espagnol ?","r": "Te quiero."},{"q": "Comment dit-on \" je t'aime \" en italien ?","r": "Ti amo."},{"q": "Comment dit-on \" je t'aime \" en allemand ?","r": "Ich liebe dich."},{"q": "Comment dit-on \" eau \" en anglais ?","r": "Water."},{"q": "Comment dit-on \" pain \" en anglais ?","r": "Bread."},{"q": "Comment dit-on \" maison \" en anglais ?","r": "House."},{"q": "Comment dit-on \" chat \" en anglais ?","r": "Cat."},{"q": "Comment dit-on \" chien \" en anglais ?","r": "Dog."},{"q": "Comment dit-on \" livre \" en anglais ?","r": "Book."},{"q": "Comment dit-on \" école \" en anglais ?","r": "School."},{"q": "Comment dit-on \" ami \" en anglais ?","r": "Friend."},{"q": "Qu'est-ce qu'un synonyme ?","r": "Un mot de même sens."},{"q": "Qu'est-ce qu'un antonyme ?","r": "Un mot de sens contraire."},{"q": "Qu'est-ce qu'un homonyme ?","r": "Un mot qui se prononce ou s'écrit pareil."},{"q": "Synonyme de \" heureux \" ?","r": "Content, joyeux."},{"q": "Antonyme de \" grand \" ?","r": "Petit."},{"q": "Antonyme de \" chaud \" ?","r": "Froid."},{"q": "Antonyme de \" rapide \" ?","r": "Lent."},{"q": "Synonyme de \" beau \" ?","r": "Joli, magnifique."},{"q": "Synonyme de \" triste \" ?","r": "Malheureux."},{"q": "Synonyme de \" intelligent \" ?","r": "Sage, brillant."},{"q": "Qu'est-ce que le présent ?","r": "Un temps qui exprime l'action actuelle."},{"q": "Qu'est-ce que le passé composé ?","r": "Un temps du passé."},{"q": "Qu'est-ce que le futur ?","r": "Un temps à venir."},{"q": "Qu'est-ce qu'un sujet dans une phrase ?","r": "Celui qui fait l'action."},{"q": "Qu'est-ce qu'un complément ?","r": "Un mot qui complète le verbe."},{"q": "Qu'est-ce qu'un adverbe ?","r": "Un mot qui modifie un verbe."},{"q": "Qui a peint Guernica ?","r": "Picasso."},{"q": "Qui a peint La Nuit étoilée ?","r": "Van Gogh."},{"q": "Qui a sculpté Le Penseur ?","r": "Rodin."},{"q": "Qui a sculpté le David ?","r": "Michel-Ange."},{"q": "Qui a peint le plafond de la chapelle Sixtine ?","r": "Michel-Ange."},{"q": "Quel est le musée le plus célèbre de Paris ?","r": "Le Louvre."},{"q": "Où se trouve la Joconde ?","r": "Au Louvre."},{"q": "Qui a composé la 9e Symphonie ?","r": "Beethoven."},{"q": "Qui a composé Les Quatre Saisons ?","r": "Vivaldi."},{"q": "Qui a composé La Flûte enchantée ?","r": "Mozart."},{"q": "Qui a composé Carmen ?","r": "Bizet."},{"q": "Qui a composé La Traviata ?","r": "Verdi."},{"q": "Qui chante \" Imagine \" ?","r": "John Lennon."},{"q": "Qui était Michael Jackson ?","r": "Un chanteur américain."},{"q": "Qui était Elvis Presley ?","r": "Un chanteur américain."},{"q": "Qui était Freddie Mercury ?","r": "Le chanteur de Queen."},{"q": "Quel groupe a chanté \" Let It Be \" ?","r": "Les Beatles."},{"q": "Qu'est-ce que le jazz ?","r": "Un style musical américain."},{"q": "Qu'est-ce que le rap ?","r": "Un style musical avec parlé rythmé."},{"q": "Qu'est-ce que la salsa ?","r": "Une danse et musique latine."},{"q": "Qu'est-ce que le reggae ?","r": "Un style musical jamaïcain."},{"q": "Qui a réalisé Titanic ?","r": "James Cameron."},{"q": "Qui a réalisé E.T. ?","r": "Steven Spielberg."},{"q": "Qui a réalisé Le Parrain ?","r": "Francis Ford Coppola."},{"q": "Qui a joué dans Forrest Gump ?","r": "Tom Hanks."},{"q": "Qui a joué dans Titanic ?","r": "Leonardo DiCaprio et Kate Winslet."},{"q": "Qu'est-ce qu'un Oscar ?","r": "Une récompense de cinéma."},{"q": "Qu'est-ce que le Festival de Cannes ?","r": "Un festival de cinéma."},{"q": "Qu'est-ce qu'un roman ?","r": "Un long récit de fiction."},{"q": "Qui a écrit Harry Potter ?","r": "J.K. Rowling."},{"q": "Qui a écrit Le Petit Prince ?","r": "Saint-Exupéry."},{"q": "Qui a écrit L'Étranger ?","r": "Albert Camus."},{"q": "Qui a écrit Madame Bovary ?","r": "Gustave Flaubert."},{"q": "Qui a écrit Notre-Dame de Paris ?","r": "Victor Hugo."},{"q": "Qui a écrit Roméo et Juliette ?","r": "Shakespeare."},{"q": "Qui a écrit Don Quichotte ?","r": "Cervantès."},{"q": "Qui a écrit La Divine Comédie ?","r": "Dante."},{"q": "Qui a écrit L'Odyssée ?","r": "Homère."},{"q": "Qu'est-ce qu'une fable ?","r": "Un récit avec une morale."},{"q": "Qui a écrit Les Fables ?","r": "Jean de La Fontaine."},{"q": "Qui a écrit Le Corbeau et le Renard ?","r": "La Fontaine."},{"q": "Qu'est-ce qu'un poème ?","r": "Un texte en vers."},{"q": "Qu'est-ce qu'un haïku ?","r": "Un poème japonais court."},{"q": "Qu'est-ce qu'une pièce de théâtre ?","r": "Une œuvre jouée sur scène."},{"q": "Qui a écrit Le Misanthrope ?","r": "Molière."},{"q": "Qui a écrit Antigone ?","r": "Sophocle / Anouilh."},{"q": "Qu'est-ce qu'une comédie ?","r": "Une pièce drôle."},{"q": "Qu'est-ce qu'une tragédie ?","r": "Une pièce dramatique."},{"q": "Qu'est-ce qu'un opéra ?","r": "Une œuvre chantée."},{"q": "Qu'est-ce qu'un ballet ?","r": "Une danse scénique."},{"q": "Combien de joueurs dans une équipe de basket ?","r": "5."},{"q": "Combien de joueurs dans une équipe de volley ?","r": "6."},{"q": "Combien de joueurs dans une équipe de rugby ?","r": "15."},{"q": "Combien de joueurs dans une équipe de handball ?","r": "7."},{"q": "Qui a gagné la Coupe du monde 2018 ?","r": "La France."},{"q": "Qui a gagné la Coupe du monde 2022 ?","r": "L'Argentine."},{"q": "Qui a gagné la Coupe du monde 2014 ?","r": "L'Allemagne."},{"q": "Qui est Lionel Messi ?","r": "Un footballeur argentin."},{"q": "Qui est Cristiano Ronaldo ?","r": "Un footballeur portugais."},{"q": "Qui est Kylian Mbappé ?","r": "Un footballeur français."},{"q": "Qui est Zinédine Zidane ?","r": "Un ancien footballeur français."},{"q": "Qui est Pelé ?","r": "Un ancien footballeur brésilien."},{"q": "Qui est Maradona ?","r": "Un ancien footballeur argentin."},{"q": "Durée d'un match de football ?","r": "90 minutes."},{"q": "Durée d'un match NBA ?","r": "48 minutes."},{"q": "Qu'est-ce qu'un penalty ?","r": "Un tir de réparation."},{"q": "Qu'est-ce qu'un corner ?","r": "Un coup de pied depuis le coin."},{"q": "Qu'est-ce qu'un hors-jeu ?","r": "Une position illégale en football."},{"q": "Qu'est-ce qu'un dunk ?","r": "Un panier écrasé au basket."},{"q": "Qu'est-ce qu'un home run ?","r": "Un coup de circuit au baseball."},{"q": "Combien de sets pour gagner à Roland-Garros ?","r": "3 sets (hommes : 3 sur 5)."},{"q": "Quel tournoi de tennis sur gazon ?","r": "Wimbledon."},{"q": "Quel tournoi de tennis sur terre battue ?","r": "Roland-Garros."},{"q": "Qui est Serena Williams ?","r": "Une joueuse de tennis américaine."},{"q": "Qui est Roger Federer ?","r": "Un joueur de tennis suisse."},{"q": "Qui est Rafael Nadal ?","r": "Un joueur de tennis espagnol."},{"q": "Qui est Novak Djokovic ?","r": "Un joueur de tennis serbe."},{"q": "Qu'est-ce que les Jeux olympiques ?","r": "Une compétition sportive mondiale."},{"q": "Tous les combien ont lieu les JO d'été ?","r": "Tous les 4 ans."},{"q": "Où ont eu lieu les JO 2024 ?","r": "Paris."},{"q": "Qu'est-ce qu'une médaille d'or ?","r": "La première place."},{"q": "Combien de joueurs en football américain ?","r": "11."},{"q": "Qu'est-ce que le Super Bowl ?","r": "La finale du football américain."},{"q": "Qu'est-ce que la Formule 1 ?","r": "Une compétition automobile."},{"q": "Qui est Lewis Hamilton ?","r": "Un pilote de F1 britannique."},{"q": "Qui est Michael Schumacher ?","r": "Un ancien pilote de F1 allemand."},{"q": "Qu'est-ce que le Tour de France ?","r": "Une course cycliste."},{"q": "Durée du Tour de France ?","r": "Environ 3 semaines."},{"q": "Qu'est-ce que le judo ?","r": "Un art martial japonais."},{"q": "Qu'est-ce que le karaté ?","r": "Un art martial japonais."},{"q": "Qu'est-ce que le taekwondo ?","r": "Un art martial coréen."},{"q": "Qu'est-ce que la boxe ?","r": "Un sport de combat."},{"q": "Qu'est-ce que la natation ?","r": "Un sport aquatique."},{"q": "Qu'est-ce que l'athlétisme ?","r": "Un ensemble de sports (course, saut, lancer)."},{"q": "Qu'est-ce que le marathon ?","r": "Une course de 42,195 km."},{"q": "Qu'est-ce que le décathlon ?","r": "Une épreuve de 10 sports."},{"q": "Qu'est-ce que le pentathlon ?","r": "Une épreuve de 5 sports."},{"q": "Qu'est-ce que le triathlon ?","r": "Natation, vélo, course."},{"q": "Qu'est-ce que le tennis de table ?","r": "Le ping-pong."},{"q": "Qu'est-ce que le badminton ?","r": "Un sport de raquette avec volant."},{"q": "Ingrédients d'une pizza margherita ?","r": "Tomate, mozzarella, basilic."},{"q": "Ingrédients d'une crêpe ?","r": "Farine, œufs, lait."},{"q": "Ingrédients d'une omelette ?","r": "Œufs, sel, poivre."},{"q": "Ingrédients d'une salade niçoise ?","r": "Thon, tomates, œufs, olives."},{"q": "Ingrédients d'un couscous ?","r": "Semoule, légumes, viande."},{"q": "Ingrédients d'un tiramisu ?","r": "Mascarpone, café, biscuits."},{"q": "Ingrédients d'un gâteau au chocolat ?","r": "Chocolat, farine, œufs, sucre."},{"q": "Qu'est-ce que la baguette ?","r": "Un pain français."},{"q": "Qu'est-ce que le fromage ?","r": "Un produit laitier."},{"q": "Combien de fromages en France ?","r": "Environ 1000."},{"q": "Qu'est-ce que le camembert ?","r": "Un fromage normand."},{"q": "Qu'est-ce que le roquefort ?","r": "Un fromage bleu."},{"q": "Qu'est-ce que le champagne ?","r": "Un vin pétillant français."},{"q": "Qu'est-ce que le vin rouge ?","r": "Un vin de raisins rouges."},{"q": "Qu'est-ce que le thé ?","r": "Une boisson à base de feuilles infusées."},{"q": "Qu'est-ce que le café ?","r": "Une boisson à base de grains torréfiés."},{"q": "Qu'est-ce que le chocolat ?","r": "Un produit à base de cacao."},{"q": "Qu'est-ce que le miel ?","r": "Un produit des abeilles."},{"q": "Qu'est-ce que le sucre ?","r": "Une substance sucrée."},{"q": "Qu'est-ce que le sel ?","r": "Une substance minérale."},{"q": "Qu'est-ce que le poivre ?","r": "Une épice."},{"q": "Qu'est-ce que le cumin ?","r": "Une épice."},{"q": "Qu'est-ce que le curcuma ?","r": "Une épice jaune."},{"q": "Qu'est-ce que le gingembre ?","r": "Une racine épicée."},{"q": "Qu'est-ce que l'ail ?","r": "Un bulbe aromatique."},{"q": "Qu'est-ce que l'oignon ?","r": "Un légume bulbe."},{"q": "Qu'est-ce que la tomate ?","r": "Un fruit-légume rouge."},{"q": "Qu'est-ce que la pomme de terre ?","r": "Un tubercule."},{"q": "Qu'est-ce que la carotte ?","r": "Un légume racine orange."},{"q": "Qu'est-ce que le riz ?","r": "Une céréale."},{"q": "Qu'est-ce que le blé ?","r": "Une céréale."},{"q": "Qu'est-ce que le maïs ?","r": "Une céréale."},{"q": "Qu'est-ce que le quinoa ?","r": "Une pseudo-céréale."},{"q": "Qu'est-ce que les pâtes ?","r": "Un aliment à base de semoule."},{"q": "Qu'est-ce que le pain ?","r": "Un aliment à base de farine."},{"q": "Qu'est-ce que la farine ?","r": "De la poudre de céréales."},{"q": "Qu'est-ce que la levure ?","r": "Un agent de fermentation."},{"q": "Qu'est-ce que la fermentation ?","r": "Une transformation par micro-organismes."},{"q": "Qu'est-ce qu'un légume ?","r": "Une plante comestible."},{"q": "Qu'est-ce qu'un fruit ?","r": "Le produit d'une plante contenant des graines."},{"q": "La tomate est-elle un fruit ?","r": "Oui, botaniquement."},{"q": "La courgette est-elle un fruit ?","r": "Oui, botaniquement."},{"q": "Qu'est-ce qu'une épice ?","r": "Une substance aromatique."},{"q": "Qu'est-ce qu'une herbe aromatique ?","r": "Une plante pour parfumer."},{"q": "Qu'est-ce que le basilic ?","r": "Une herbe aromatique."},{"q": "Qu'est-ce que le persil ?","r": "Une herbe aromatique."},{"q": "Qu'est-ce que la menthe ?","r": "Une herbe aromatique."},{"q": "Qu'est-ce que le thym ?","r": "Une herbe aromatique."},{"q": "Qu'est-ce que le romarin ?","r": "Une herbe aromatique."},{"q": "Qu'est-ce que la cannelle ?","r": "Une épice."},{"q": "Qu'est-ce qu'un processeur ?","r": "Le cerveau de l'ordinateur."},{"q": "Qu'est-ce que la RAM ?","r": "Une mémoire vive."},{"q": "Qu'est-ce qu'un disque dur ?","r": "Un stockage de données."},{"q": "Qu'est-ce qu'un SSD ?","r": "Un stockage rapide."},{"q": "Qu'est-ce qu'un écran ?","r": "Un dispositif d'affichage."},{"q": "Qu'est-ce qu'un clavier ?","r": "Un dispositif de saisie."},{"q": "Qu'est-ce qu'une souris ?","r": "Un dispositif de pointage."},{"q": "Qu'est-ce qu'un smartphone ?","r": "Un téléphone intelligent."},{"q": "Qu'est-ce qu'une tablette ?","r": "Un ordinateur tactile."},{"q": "Qu'est-ce qu'un serveur ?","r": "Un ordinateur qui fournit des services."},{"q": "Qu'est-ce qu'un réseau ?","r": "Un ensemble d'ordinateurs connectés."},{"q": "Qu'est-ce que le Wi-Fi ?","r": "Une connexion sans fil."},{"q": "Qu'est-ce que le Bluetooth ?","r": "Une connexion sans fil courte portée."},{"q": "Qu'est-ce qu'un navigateur ?","r": "Un logiciel pour aller sur Internet."},{"q": "Qu'est-ce qu'un site web ?","r": "Un ensemble de pages en ligne."},{"q": "Qu'est-ce qu'une URL ?","r": "Une adresse web."},{"q": "Qu'est-ce qu'un cookie ?","r": "Un petit fichier stocké par un site."},{"q": "Qu'est-ce que le HTML ?","r": "Un langage de balisage web."},{"q": "Qu'est-ce que le CSS ?","r": "Un langage de style web."},{"q": "Qu'est-ce que le JavaScript ?","r": "Un langage de programmation web."},{"q": "Qu'est-ce que Python ?","r": "Un langage de programmation."},{"q": "Qu'est-ce que Java ?","r": "Un langage de programmation."},{"q": "Qu'est-ce que le C++ ?","r": "Un langage de programmation."},{"q": "Qu'est-ce que le code ?","r": "Des instructions pour un ordinateur."},{"q": "Qu'est-ce qu'un algorithme ?","r": "Une suite d'instructions."},{"q": "Qu'est-ce qu'une boucle ?","r": "Une répétition d'instructions."},{"q": "Qu'est-ce qu'une variable ?","r": "Un espace de stockage en programmation."},{"q": "Qu'est-ce qu'une fonction en programmation ?","r": "Un bloc de code réutilisable."},{"q": "Qu'est-ce qu'un bug ?","r": "Une erreur dans un programme."},{"q": "Qu'est-ce que le débogage ?","r": "Corriger des bugs."},{"q": "Qu'est-ce qu'une base de données ?","r": "Un ensemble organisé de données."},{"q": "Qu'est-ce que SQL ?","r": "Un langage de requêtes."},{"q": "Qu'est-ce que le machine learning ?","r": "L'apprentissage automatique."},{"q": "Qu'est-ce que le deep learning ?","r": "L'apprentissage profond."},{"q": "Qu'est-ce qu'un réseau de neurones ?","r": "Un modèle inspiré du cerveau."},{"q": "Qu'est-ce qu'un chatbot ?","r": "Un agent conversationnel."},{"q": "Qu'est-ce que la réalité virtuelle ?","r": "Une immersion numérique."},{"q": "Qu'est-ce que la réalité augmentée ?","r": "Ajouter du numérique au réel."},{"q": "Qu'est-ce que la blockchain ?","r": "Une chaîne de blocs."},{"q": "Qu'est-ce que le Bitcoin ?","r": "Une cryptomonnaie."},{"q": "Qu'est-ce qu'une cryptomonnaie ?","r": "Une monnaie numérique."},{"q": "Qu'est-ce que le cloud computing ?","r": "L'informatique en nuage."},{"q": "Qu'est-ce qu'un drone ?","r": "Un engin volant sans pilote."},{"q": "Qu'est-ce que l'impression 3D ?","r": "Fabriquer un objet en 3D."},{"q": "Qu'est-ce qu'un robot ?","r": "Une machine programmable."},{"q": "Qu'est-ce que l'automatisation ?","r": "Exécuter des tâches sans humain."},{"q": "Qu'est-ce que la 5G ?","r": "La 5e génération de réseau mobile."},{"q": "Qu'est-ce que la fibre optique ?","r": "Un câble pour Internet rapide."},{"q": "Qu'est-ce qu'un VPN ?","r": "Un réseau privé virtuel."},{"q": "Qu'est-ce qu'un pare-feu ?","r": "Un filtre de sécurité réseau."},{"q": "Qu'est-ce qu'un médecin ?","r": "Un professionnel de santé."},{"q": "Qu'est-ce qu'un hôpital ?","r": "Un lieu de soins."},{"q": "Qu'est-ce qu'une pharmacie ?","r": "Un lieu de vente de médicaments."},{"q": "Qu'est-ce qu'un vaccin ?","r": "Une préparation qui protège."},{"q": "Qu'est-ce qu'un antibiotique ?","r": "Un médicament contre les bactéries."},{"q": "Qu'est-ce qu'une allergie ?","r": "Une réaction immunitaire."},{"q": "Qu'est-ce que l'asthme ?","r": "Une maladie respiratoire."},{"q": "Qu'est-ce que le diabète ?","r": "Une maladie du sucre."},{"q": "Qu'est-ce que l'hypertension ?","r": "Une tension trop élevée."},{"q": "Qu'est-ce qu'un AVC ?","r": "Un accident vasculaire cérébral."},{"q": "Qu'est-ce qu'un infarctus ?","r": "Une crise cardiaque."},{"q": "Qu'est-ce que le cancer ?","r": "Une maladie des cellules."},{"q": "Qu'est-ce que la fièvre ?","r": "Une température élevée."},{"q": "Qu'est-ce que la toux ?","r": "Un réflexe respiratoire."},{"q": "Qu'est-ce que le rhume ?","r": "Une infection virale."},{"q": "Qu'est-ce que la grippe ?","r": "Une infection virale."},{"q": "Qu'est-ce que le COVID-19 ?","r": "Une maladie virale."},{"q": "Qu'est-ce qu'une épidémie ?","r": "Une maladie qui se propage."},{"q": "Qu'est-ce qu'une pandémie ?","r": "Une épidémie mondiale."},{"q": "Qu'est-ce qu'un chirurgien ?","r": "Un médecin qui opère."},{"q": "Qu'est-ce qu'une ordonnance ?","r": "Une prescription médicale."},{"q": "Qu'est-ce qu'un symptôme ?","r": "Un signe de maladie."},{"q": "Qu'est-ce qu'un diagnostic ?","r": "L'identification d'une maladie."},{"q": "Qu'est-ce que la prévention ?","r": "Éviter une maladie."},{"q": "Qu'est-ce qu'une blessure ?","r": "Une atteinte au corps."},{"q": "Qu'est-ce qu'une fracture ?","r": "Un os cassé."},{"q": "Qu'est-ce qu'une entorse ?","r": "Une blessure des ligaments."},{"q": "Qu'est-ce qu'une brûlure ?","r": "Une lésion de la peau."},{"q": "Qu'est-ce qu'une piqûre ?","r": "Une blessure par un insecte."},{"q": "Qu'est-ce qu'un pansement ?","r": "Une protection pour une plaie."},{"q": "Qu'est-ce qu'un antiseptique ?","r": "Un produit qui tue les microbes."},{"q": "Qu'est-ce qu'une radio ?","r": "Un examen d'imagerie."},{"q": "Qu'est-ce qu'une échographie ?","r": "Un examen par ultrasons."},{"q": "Qu'est-ce qu'une IRM ?","r": "Un examen par aimant."},{"q": "Qu'est-ce qu'un scanner ?","r": "Un examen par rayons X."},{"q": "Qu'est-ce qu'une analyse de sang ?","r": "Un examen du sang."},{"q": "Qu'est-ce qu'un groupe sanguin ?","r": "Un type de sang."},{"q": "Combien de groupes sanguins ?","r": "A, B, AB, O."},{"q": "Qu'est-ce que le don du sang ?","r": "Donner son sang."},{"q": "Qu'est-ce qu'un organe ?","r": "Une partie du corps."},{"q": "Quel est le plus grand organe ?","r": "La peau."},{"q": "Quel est le plus grand organe interne ?","r": "Le foie."},{"q": "Quel organe filtre le sang ?","r": "Les reins."},{"q": "Quel organe produit la bile ?","r": "Le foie."},{"q": "Quel organe produit l'insuline ?","r": "Le pancréas."},{"q": "Quel organe stocke l'urine ?","r": "La vessie."},{"q": "Quel organe est le siège des émotions ?","r": "Le cerveau."},{"q": "Qu'est-ce que la joie ?","r": "Une émotion positive."},{"q": "Qu'est-ce que la tristesse ?","r": "Une émotion négative."},{"q": "Qu'est-ce que la peur ?","r": "Une émotion face au danger."},{"q": "Qu'est-ce que la colère ?","r": "Une émotion de frustration."},{"q": "Qu'est-ce que la surprise ?","r": "Une émotion face à l'inattendu."},{"q": "Qu'est-ce que le dégoût ?","r": "Une émotion de rejet."},{"q": "Qu'est-ce que l'amour ?","r": "Un sentiment d'affection."},{"q": "Qu'est-ce que l'amitié ?","r": "Un lien affectif."},{"q": "Qu'est-ce que la jalousie ?","r": "Une émotion de possessivité."},{"q": "Qu'est-ce que la honte ?","r": "Une émotion de gêne."},{"q": "Qu'est-ce que la culpabilité ?","r": "Un sentiment de faute."},{"q": "Qu'est-ce que la fierté ?","r": "Un sentiment de satisfaction."},{"q": "Qu'est-ce que l'espoir ?","r": "Une attente positive."},{"q": "Qu'est-ce que l'anxiété ?","r": "Une inquiétude excessive."},{"q": "Qu'est-ce que la dépression ?","r": "Une maladie mentale."},{"q": "Qu'est-ce que le stress ?","r": "Une tension face aux exigences."},{"q": "Qu'est-ce que la confiance ?","r": "Croire en soi ou en l'autre."},{"q": "Qu'est-ce que l'estime de soi ?","r": "La valeur qu'on se donne."},{"q": "Qu'est-ce que la motivation ?","r": "Ce qui pousse à agir."},{"q": "Qu'est-ce que la patience ?","r": "Savoir attendre."},{"q": "Qu'est-ce que le courage ?","r": "Affronter la peur."},{"q": "Qu'est-ce que la sagesse ?","r": "La connaissance et le jugement."},{"q": "Qu'est-ce que la bonté ?","r": "Faire du bien."},{"q": "Qu'est-ce que la générosité ?","r": "Donner aux autres."},{"q": "Qu'est-ce que l'égoïsme ?","r": "Penser à soi uniquement."},{"q": "Qu'est-ce que l'altruisme ?","r": "Aider sans intérêt."},{"q": "Qu'est-ce que la solidarité ?","r": "Soutien mutuel."},{"q": "Qu'est-ce que le pardon ?","r": "Pardonner une faute."},{"q": "Qu'est-ce que la gratitude ?","r": "Être reconnaissant."},{"q": "Qu'est-ce que le bonheur ?","r": "Un état de satisfaction."},{"q": "Comment être heureux ?","r": "Cultiver des relations, la gratitude, la santé."},{"q": "Comment gérer la colère ?","r": "Respirer, verbaliser, se calmer."},{"q": "Comment gérer la peur ?","r": "Affronter progressivement."},{"q": "Comment gérer la tristesse ?","r": "En parler, se reposer, se faire aider."},{"q": "Comment aider quelqu'un en dépression ?","r": "Écouter et orienter vers un professionnel."},{"q": "Qu'est-ce qu'un psychologue ?","r": "Un professionnel de l'écoute."},{"q": "Qu'est-ce qu'un psychiatre ?","r": "Un médecin des troubles mentaux."},{"q": "Qu'est-ce qu'un psychanalyste ?","r": "Un praticien de la psychanalyse."},{"q": "Qu'est-ce que la méditation ?","r": "Une pratique de concentration."},{"q": "Qu'est-ce que la pleine conscience ?","r": "Être présent ici et maintenant."},{"q": "Qu'est-ce que le développement personnel ?","r": "Travailler sur soi."},{"q": "Qu'est-ce que l'intelligence émotionnelle ?","r": "Comprendre ses émotions."},{"q": "Qu'est-ce que l'écoute active ?","r": "Écouter attentivement."},{"q": "Qu'est-ce que la communication non violente ?","r": "Parler sans agresser."},{"q": "Qu'est-ce que l'assertivité ?","r": "S'exprimer sans agresser."},{"q": "Qu'est-ce que la résilience ?","r": "Rebondir après une épreuve."},{"q": "Qu'est-ce que le lâcher-prise ?","r": "Accepter ce qu'on ne contrôle pas."},{"q": "Qu'est-ce que la bienveillance ?","r": "Vouloir du bien."},{"q": "Qu'est-ce que la liberté ?","r": "Le droit d'agir."},{"q": "Qu'est-ce que l'égalité ?","r": "Traiter tous pareil."},{"q": "Qu'est-ce que la fraternité ?","r": "Solidarité entre citoyens."},{"q": "Qu'est-ce que la laïcité ?","r": "Séparation État/religions."},{"q": "Qu'est-ce que la République ?","r": "Un régime où le peuple élit."},{"q": "Qu'est-ce que la Constitution ?","r": "La loi fondamentale."},{"q": "Qu'est-ce que le Parlement ?","r": "Le pouvoir législatif."},{"q": "Qu'est-ce que l'Assemblée nationale ?","r": "La chambre basse française."},{"q": "Qu'est-ce que le Sénat ?","r": "La chambre haute française."},{"q": "Qu'est-ce qu'un député ?","r": "Un élu à l'Assemblée."},{"q": "Qu'est-ce qu'un sénateur ?","r": "Un élu au Sénat."},{"q": "Qu'est-ce qu'un maire ?","r": "Le chef d'une commune."},{"q": "Qu'est-ce qu'un préfet ?","r": "Un représentant de l'État."},{"q": "Qu'est-ce qu'un président ?","r": "Le chef de l'État."},{"q": "Qu'est-ce qu'un premier ministre ?","r": "Le chef du gouvernement."},{"q": "Qu'est-ce qu'un ministère ?","r": "Un département gouvernemental."},{"q": "Qu'est-ce qu'une élection ?","r": "Un vote pour choisir."},{"q": "Qu'est-ce que le suffrage universel ?","r": "Le droit de vote pour tous."},{"q": "À quel âge vote-t-on en France ?","r": "18 ans."},{"q": "Qu'est-ce qu'un parti politique ?","r": "Un groupe d'idées politiques."},{"q": "Qu'est-ce qu'un syndicat ?","r": "Une organisation de travailleurs."},{"q": "Qu'est-ce qu'une association ?","r": "Un groupe de personnes."},{"q": "Qu'est-ce qu'une ONG ?","r": "Une organisation non gouvernementale."},{"q": "Qu'est-ce que la sécurité sociale ?","r": "Une protection sociale."},{"q": "Qu'est-ce que le RSA ?","r": "Une aide sociale."},{"q": "Qu'est-ce que l'impôt ?","r": "Un prélèvement obligatoire."},{"q": "Qu'est-ce que la TVA ?","r": "Une taxe sur la consommation."},{"q": "Qu'est-ce que le PIB ?","r": "La production d'un pays."},{"q": "Qu'est-ce que l'inflation ?","r": "La hausse des prix."},{"q": "Qu'est-ce que le chômage ?","r": "Ne pas avoir d'emploi."},{"q": "Qu'est-ce qu'une grève ?","r": "Cesser le travail."},{"q": "Qu'est-ce qu'une manifestation ?","r": "Un rassemblement."},{"q": "Qu'est-ce qu'une pétition ?","r": "Une demande collective."},{"q": "Qu'est-ce que la liberté d'expression ?","r": "Dire ce qu'on pense."},{"q": "Qu'est-ce que la censure ?","r": "Interdire une expression."},{"q": "Qu'est-ce que les droits de l'homme ?","r": "Droits fondamentaux."},{"q": "Qu'est-ce que la Déclaration des droits de l'homme ?","r": "Un texte de 1789."},{"q": "Qu'est-ce que l'UNESCO ?","r": "Organisation pour l'éducation et la culture."},{"q": "Qu'est-ce que l'OMS ?","r": "Organisation mondiale de la santé."},{"q": "Qu'est-ce que l'Union européenne ?","r": "Une union de pays européens."},{"q": "Combien de pays dans l'UE ?","r": "27."},{"q": "Qu'est-ce que l'euro ?","r": "La monnaie de l'UE."},{"q": "Qu'est-ce que l'OTAN ?","r": "Une alliance militaire."},{"q": "Qu'est-ce que le G20 ?","r": "Un groupe de 20 pays."},{"q": "Qu'est-ce que la Croix-Rouge ?","r": "Une organisation humanitaire."},{"q": "Qu'est-ce que Médecins sans frontières ?","r": "Une ONG médicale."},{"q": "Qu'est-ce que l'Unicef ?","r": "Organisation pour les enfants."},{"q": "Qu'est-ce qu'un réfugié ?","r": "Une personne qui fuit."},{"q": "Qu'est-ce que l'immigration ?","r": "Venir vivre dans un pays."},{"q": "Qu'est-ce que le réchauffement climatique ?","r": "La hausse des températures."},{"q": "Qu'est-ce que l'effet de serre ?","r": "Le piégeage de la chaleur."},{"q": "Quels sont les gaz à effet de serre ?","r": "CO2, méthane, etc."},{"q": "Qu'est-ce que l'empreinte carbone ?","r": "Les émissions de CO2."},{"q": "Qu'est-ce que l'énergie renouvelable ?","r": "Une énergie inépuisable."},{"q": "Cite des énergies renouvelables.","r": "Solaire, éolien, hydraulique."},{"q": "Qu'est-ce que l'énergie solaire ?","r": "L'énergie du soleil."},{"q": "Qu'est-ce que l'énergie éolienne ?","r": "L'énergie du vent."},{"q": "Qu'est-ce que l'énergie hydraulique ?","r": "L'énergie de l'eau."},{"q": "Qu'est-ce que le nucléaire ?","r": "Une énergie atomique."},{"q": "Qu'est-ce que la pollution ?","r": "La contamination de l'environnement."},{"q": "Qu'est-ce que la pollution de l'air ?","r": "La contamination de l'air."},{"q": "Qu'est-ce que la pollution de l'eau ?","r": "La contamination de l'eau."},{"q": "Qu'est-ce que la pollution des sols ?","r": "La contamination des sols."},{"q": "Qu'est-ce que le plastique ?","r": "Un matériau dérivé du pétrole."},{"q": "Combien de temps met le plastique à se décomposer ?","r": "Des centaines d'années."},{"q": "Qu'est-ce que le compostage ?","r": "Transformer les déchets organiques."},{"q": "Qu'est-ce que le développement durable ?","r": "Un développement respectueux."},{"q": "Qu'est-ce que la biodiversité ?","r": "La variété du vivant."},{"q": "Qu'est-ce qu'une espèce en voie de disparition ?","r": "Une espèce menacée."},{"q": "Cite un animal menacé.","r": "Le panda, le tigre, etc."},{"q": "Qu'est-ce qu'une réserve naturelle ?","r": "Un espace protégé."},{"q": "Qu'est-ce qu'un parc national ?","r": "Un espace protégé."},{"q": "Qu'est-ce que la déforestation ?","r": "La disparition des forêts."},{"q": "Qu'est-ce que la désertification ?","r": "La transformation en désert."},{"q": "Combien d'océans ?","r": "5."},{"q": "Cite les océans.","r": "Atlantique, Pacifique, Indien, Arctique, Austral."},{"q": "Qu'est-ce qu'un récif corallien ?","r": "Un écosystème marin."},{"q": "Qu'est-ce que la surpêche ?","r": "Pêcher trop."},{"q": "Qu'est-ce que l'agriculture biologique ?","r": "Sans pesticides."},{"q": "Qu'est-ce qu'un pesticide ?","r": "Un produit pour tuer les nuisibles."},{"q": "Qu'est-ce que l'OGM ?","r": "Un organisme génétiquement modifié."},{"q": "Qu'est-ce que le bio ?","r": "Un label agricole."},{"q": "Qu'est-ce que la permaculture ?","r": "Une agriculture durable."},{"q": "Qu'est-ce que le zéro déchet ?","r": "Ne rien jeter."},{"q": "Que signifie les 3 R ?","r": "Réduire, réutiliser, recycler."},{"q": "Qu'est-ce qu'une énergie fossile ?","r": "Charbon, pétrole, gaz."},{"q": "Pourquoi économiser l'électricité ?","r": "Pour la planète et le portefeuille."},{"q": "Qu'est-ce qu'une voiture électrique ?","r": "Une voiture à batterie."},{"q": "Qu'est-ce que le covoiturage ?","r": "Partager une voiture."},{"q": "Qu'est-ce que les transports en commun ?","r": "Bus, métro, train."},{"q": "Qu'est-ce que la pollution sonore ?","r": "Le bruit excessif."},{"q": "Qu'est-ce que la pollution lumineuse ?","r": "La lumière excessive."},{"q": "Qu'est-ce que l'écologie ?","r": "La science de l'environnement."},{"q": "Qu'est-ce qu'un écologiste ?","r": "Une personne qui protège la nature."},{"q": "Que faire pour la planète ?","r": "Réduire, recycler, économiser."},{"q": "Qu'est-ce que le climat ?","r": "Le temps moyen d'une région."},{"q": "Qu'est-ce que la météo ?","r": "Le temps qu'il fait."},{"q": "Qu'est-ce qu'une saison ?","r": "Une période de l'année."},{"q": "Combien de saisons ?","r": "4."},{"q": "Qu'est-ce qu'un téléphone ?","r": "Un appareil de communication."},{"q": "Qu'est-ce qu'une télévision ?","r": "Un appareil d'images."},{"q": "Qu'est-ce qu'un livre ?","r": "Un ensemble de pages."},{"q": "Qu'est-ce qu'un stylo ?","r": "Un outil d'écriture."},{"q": "Qu'est-ce qu'un crayon ?","r": "Un outil d'écriture."},{"q": "Qu'est-ce qu'une gomme ?","r": "Un effaceur."},{"q": "Qu'est-ce qu'un cahier ?","r": "Un ensemble de pages."},{"q": "Qu'est-ce qu'un sac ?","r": "Un contenant."},{"q": "Qu'est-ce qu'un sac à dos ?","r": "Un sac portable."},{"q": "Qu'est-ce qu'un parapluie ?","r": "Une protection contre la pluie."},{"q": "Qu'est-ce qu'une montre ?","r": "Un appareil à heure."},{"q": "Qu'est-ce que des lunettes ?","r": "Un appareil de vision."},{"q": "Qu'est-ce qu'un miroir ?","r": "Une surface réfléchissante."},{"q": "Qu'est-ce qu'un lit ?","r": "Un meuble pour dormir."},{"q": "Qu'est-ce qu'un canapé ?","r": "Un meuble pour s'asseoir."},{"q": "Qu'est-ce qu'un fauteuil ?","r": "Un siège confortable."},{"q": "Qu'est-ce qu'une armoire ?","r": "Un meuble de rangement."},{"q": "Qu'est-ce qu'un tiroir ?","r": "Un compartiment."},{"q": "Qu'est-ce qu'une étagère ?","r": "Un meuble à étages."},{"q": "Qu'est-ce qu'un tapis ?","r": "Un textile de sol."},{"q": "Qu'est-ce qu'un rideau ?","r": "Un textile de fenêtre."},{"q": "Qu'est-ce qu'un coussin ?","r": "Un objet moelleux."},{"q": "Qu'est-ce qu'une couverture ?","r": "Un textile chaud."},{"q": "Qu'est-ce qu'un oreiller ?","r": "Un coussin de tête."},{"q": "Qu'est-ce qu'un balai ?","r": "Un outil de nettoyage."},{"q": "Qu'est-ce qu'un aspirateur ?","r": "Un appareil de nettoyage."},{"q": "Qu'est-ce qu'une machine à laver ?","r": "Un appareil pour laver."},{"q": "Qu'est-ce qu'un lave-vaisselle ?","r": "Un appareil pour la vaisselle."},{"q": "Qu'est-ce qu'un four ?","r": "Un appareil de cuisson."},{"q": "Qu'est-ce qu'un micro-ondes ?","r": "Un appareil de cuisson rapide."},{"q": "Qu'est-ce qu'une bouilloire ?","r": "Un appareil à eau chaude."},{"q": "Qu'est-ce qu'une cafetière ?","r": "Un appareil à café."},{"q": "Qu'est-ce qu'un grille-pain ?","r": "Un appareil à pain grillé."},{"q": "Qu'est-ce qu'un mixeur ?","r": "Un appareil pour mélanger."},{"q": "Qu'est-ce qu'un couteau ?","r": "Un ustensile tranchant."},{"q": "Qu'est-ce qu'une fourchette ?","r": "Un ustensile à piquer."},{"q": "Qu'est-ce qu'une cuillère ?","r": "Un ustensile à manger."},{"q": "Qu'est-ce qu'une assiette ?","r": "Un plat pour manger."},{"q": "Qu'est-ce qu'un bol ?","r": "Un récipient."},{"q": "Qu'est-ce qu'un verre ?","r": "Un récipient à boire."},{"q": "Qu'est-ce qu'une tasse ?","r": "Un récipient à boire."},{"q": "Qu'est-ce qu'une bouteille ?","r": "Un récipient à liquide."},{"q": "Qu'est-ce qu'un bouchon ?","r": "Un fermoir."},{"q": "Qu'est-ce qu'une boîte ?","r": "Un contenant."},{"q": "Qu'est-ce qu'un carton ?","r": "Une boîte en carton."},{"q": "Qu'est-ce qu'un journal ?","r": "Une publication quotidienne."},{"q": "Qu'est-ce qu'un magazine ?","r": "Une publication périodique."},{"q": "Qu'est-ce qu'une lettre ?","r": "Un message écrit."},{"q": "Qu'est-ce qu'une enveloppe ?","r": "Un contenant de lettre."},{"q": "Qu'est-ce qu'un timbre ?","r": "Une vignette postale."},{"q": "Qu'est-ce qu'un colis ?","r": "Un paquet envoyé."},{"q": "Qu'est-ce qu'un ordinateur portable ?","r": "Un ordinateur transportable."},{"q": "Qu'est-ce qu'un casque ?","r": "Un appareil audio."},{"q": "Qu'est-ce qu'un haut-parleur ?","r": "Un appareil à son."},{"q": "Qu'est-ce qu'un microphone ?","r": "Un appareil à enregistrer la voix."},{"q": "Qu'est-ce qu'une caméra ?","r": "Un appareil à filmer."},{"q": "Qu'est-ce qu'un appareil photo ?","r": "Un appareil à photographier."},{"q": "Qu'est-ce qu'une imprimante ?","r": "Un appareil à imprimer."},{"q": "Qu'est-ce qu'une clé USB ?","r": "Un stockage portable."},{"q": "Qu'est-ce qu'une carte mémoire ?","r": "Un stockage amovible."},{"q": "Qu'est-ce qu'un chargeur ?","r": "Un appareil à recharger."},{"q": "Qu'est-ce qu'une batterie ?","r": "Un stockage d'énergie."},{"q": "Qu'est-ce qu'une prise ?","r": "Un point d'alimentation."},{"q": "Qu'est-ce qu'un interrupteur ?","r": "Un bouton marche/arrêt."},{"q": "Qu'est-ce qu'une ampoule ?","r": "Une source de lumière."},{"q": "Qu'est-ce qu'une bougie ?","r": "Une source de lumière."},{"q": "Qu'est-ce qu'une lampe ?","r": "Un appareil de lumière."},{"q": "Qu'est-ce qu'un ventilateur ?","r": "Un appareil à vent."},{"q": "Qu'est-ce qu'un climatiseur ?","r": "Un appareil à froid."},{"q": "Qu'est-ce qu'un radiateur ?","r": "Un appareil à chaud."},{"q": "Qu'est-ce qu'une cheminée ?","r": "Un conduit de fumée."},{"q": "Qu'est-ce qu'un escalier ?","r": "Une suite de marches."},{"q": "Qu'est-ce qu'un ascenseur ?","r": "Un appareil à monter."},{"q": "Qu'est-ce qu'un balcon ?","r": "Une plateforme extérieure."},{"q": "Qu'est-ce qu'une terrasse ?","r": "Un espace extérieur."},{"q": "Qu'est-ce qu'un jardin ?","r": "Un espace vert."},{"q": "Qu'est-ce qu'un garage ?","r": "Un abri pour voiture."},{"q": "Qu'est-ce qu'un toit ?","r": "La partie supérieure d'une maison."},{"q": "Qu'est-ce qu'un mur ?","r": "Une paroi."},{"q": "Qu'est-ce qu'un sol ?","r": "La surface du bas."},{"q": "Qu'est-ce qu'un plafond ?","r": "La surface du haut."},{"q": "Qu'est-ce qu'une clé ?","r": "Un objet pour ouvrir."},{"q": "Qu'est-ce qu'une serrure ?","r": "Un mécanisme de fermeture."},{"q": "Qu'est-ce qu'une sonnette ?","r": "Un bouton de son."},{"q": "Qu'est-ce qu'un volet ?","r": "Un panneau de fenêtre."},{"q": "Qu'est-ce qu'une gouttière ?","r": "Un conduit d'eau."},{"q": "Qu'est-ce qu'une antenne ?","r": "Un récepteur de signal."},{"q": "Qu'est-ce qu'une parabole ?","r": "Une antenne satellite."},{"q": "Qu'est-ce qu'un bus ?","r": "Un transport en commun."},{"q": "Qu'est-ce qu'un métro ?","r": "Un transport urbain souterrain."},{"q": "Qu'est-ce qu'un tramway ?","r": "Un transport urbain sur rails."},{"q": "Qu'est-ce qu'un train ?","r": "Un transport sur rails."},{"q": "Qu'est-ce qu'un TGV ?","r": "Un train à grande vitesse."},{"q": "Qu'est-ce qu'un avion ?","r": "Un transport aérien."},{"q": "Qu'est-ce qu'un hélicoptère ?","r": "Un appareil à voilure tournante."},{"q": "Qu'est-ce qu'un bateau ?","r": "Un transport maritime."},{"q": "Qu'est-ce qu'un voilier ?","r": "Un bateau à voiles."},{"q": "Qu'est-ce qu'un paquebot ?","r": "Un grand navire."},{"q": "Qu'est-ce qu'une voiture ?","r": "Un transport à 4 roues."},{"q": "Qu'est-ce qu'un camion ?","r": "Un transport de marchandises."},{"q": "Qu'est-ce qu'une moto ?","r": "Un véhicule à 2 roues."},{"q": "Qu'est-ce qu'un vélo ?","r": "Un véhicule à 2 roues à pédales."},{"q": "Qu'est-ce qu'une trottinette ?","r": "Un véhicule à roulettes."},{"q": "Qu'est-ce qu'un taxi ?","r": "Un transport payant."},{"q": "Qu'est-ce qu'un métier ?","r": "Une activité professionnelle."},{"q": "Que fait un médecin ?","r": "Il soigne les malades."},{"q": "Que fait un infirmier ?","r": "Il soigne et aide les patients."},{"q": "Que fait un professeur ?","r": "Il enseigne."},{"q": "Que fait un avocat ?","r": "Il défend en justice."},{"q": "Que fait un juge ?","r": "Il rend la justice."},{"q": "Que fait un policier ?","r": "Il maintient l'ordre."},{"q": "Que fait un pompier ?","r": "Il éteint les incendies."},{"q": "Que fait un militaire ?","r": "Il défend le pays."},{"q": "Que fait un ingénieur ?","r": "Il conçoit des solutions."},{"q": "Que fait un architecte ?","r": "Il conçoit des bâtiments."},{"q": "Que fait un informaticien ?","r": "Il travaille sur les ordinateurs."},{"q": "Que fait un développeur ?","r": "Il écrit du code."},{"q": "Que fait un cuisinier ?","r": "Il prépare des plats."},{"q": "Que fait un boulanger ?","r": "Il fait du pain."},{"q": "Que fait un pâtissier ?","r": "Il fait des gâteaux."},{"q": "Que fait un boucher ?","r": "Il vend de la viande."},{"q": "Que fait un agriculteur ?","r": "Il cultive la terre."},{"q": "Que fait un éleveur ?","r": "Il élève des animaux."},{"q": "Que fait un pêcheur ?","r": "Il pêche du poisson."},{"q": "Que fait un mécanicien ?","r": "Il répare les véhicules."},{"q": "Que fait un plombier ?","r": "Il répare la plomberie."},{"q": "Que fait un électricien ?","r": "Il installe l'électricité."},{"q": "Que fait un menuisier ?","r": "Il travaille le bois."},{"q": "Que fait un maçon ?","r": "Il construit des murs."},{"q": "Que fait un peintre en bâtiment ?","r": "Il peint les murs."},{"q": "Que fait un coiffeur ?","r": "Il coupe les cheveux."},{"q": "Que fait un vendeur ?","r": "Il vend des produits."},{"q": "Que fait un caissier ?","r": "Il encaisse les achats."},{"q": "Que fait un banquier ?","r": "Il gère l'argent."},{"q": "Que fait un comptable ?","r": "Il gère les comptes."},{"q": "Que fait un journaliste ?","r": "Il informe le public."},{"q": "Que fait un écrivain ?","r": "Il écrit des livres."},{"q": "Que fait un artiste ?","r": "Il crée des œuvres."},{"q": "Que fait un musicien ?","r": "Il joue de la musique."},{"q": "Que fait un chanteur ?","r": "Il chante."},{"q": "Que fait un acteur ?","r": "Il joue un rôle."},{"q": "Que fait un réalisateur ?","r": "Il dirige un film."},{"q": "Que fait un photographe ?","r": "Il prend des photos."},{"q": "Que fait un pilote ?","r": "Il conduit un avion."},{"q": "Que fait un astronaute ?","r": "Il va dans l'espace."},{"q": "Que fait un scientifique ?","r": "Il fait de la recherche."},{"q": "Que fait un chercheur ?","r": "Il fait de la recherche."},{"q": "Que fait un bibliothécaire ?","r": "Il gère une bibliothèque."},{"q": "Qu'est-ce qu'un passeport ?","r": "Un document de voyage."},{"q": "Qu'est-ce qu'un visa ?","r": "Une autorisation d'entrée."},{"q": "Qu'est-ce qu'un billet d'avion ?","r": "Un titre de transport."},{"q": "Qu'est-ce qu'une valise ?","r": "Un bagage."},{"q": "Qu'est-ce qu'un hôtel ?","r": "Un hébergement."},{"q": "Qu'est-ce qu'une auberge ?","r": "Un hébergement économique."},{"q": "Qu'est-ce qu'un camping ?","r": "Un hébergement en plein air."},{"q": "Qu'est-ce qu'une carte ?","r": "Une représentation d'un territoire."},{"q": "Qu'est-ce qu'un GPS ?","r": "Un guide de navigation."},{"q": "Qu'est-ce qu'une agence de voyage ?","r": "Organise des voyages."},{"q": "Qu'est-ce qu'un guide touristique ?","r": "Un accompagnateur."},{"q": "Qu'est-ce qu'un monument ?","r": "Un bâtiment remarquable."},{"q": "Quel est le monument le plus visité au monde ?","r": "La Tour Eiffel."},{"q": "Qu'est-ce que le Colisée ?","r": "Un amphithéâtre romain."},{"q": "Qu'est-ce que les Pyramides ?","r": "Des tombeaux égyptiens."},{"q": "Qu'est-ce que la Grande Muraille ?","r": "Une fortification chinoise."},{"q": "Qu'est-ce que le Taj Mahal ?","r": "Un mausolée indien."},{"q": "Qu'est-ce que le Machu Picchu ?","r": "Une cité inca."},{"q": "Qu'est-ce que le Christ Rédempteur ?","r": "Une statue à Rio."},{"q": "Qu'est-ce que Big Ben ?","r": "Une horloge de Londres."},{"q": "Qu'est-ce que la Statue de la Liberté ?","r": "Un monument de New York."},{"q": "Qu'est-ce que le Vatican ?","r": "Un État pontifical."},{"q": "Qu'est-ce que Venise ?","r": "Une ville italienne sur l'eau."},{"q": "Qu'est-ce que New York ?","r": "Une ville américaine."},{"q": "Qu'est-ce que Dubaï ?","r": "Une ville des Émirats."},{"q": "Qu'est-ce que Marrakech ?","r": "Une ville marocaine."},{"q": "Qu'est-ce que Le Caire ?","r": "La capitale de l'Égypte."},{"q": "Qu'est-ce que Tokyo ?","r": "La capitale du Japon."},{"q": "Qu'est-ce que Sydney ?","r": "Une ville australienne."},{"q": "Qu'est-ce que Rio de Janeiro ?","r": "Une ville brésilienne."},{"q": "Qu'est-ce que Noël ?","r": "Une fête chrétienne."},{"q": "Quand fête-t-on Noël ?","r": "Le 25 décembre."},{"q": "Qu'est-ce que le Nouvel An ?","r": "Le 1er janvier."},{"q": "Qu'est-ce que Pâques ?","r": "Une fête chrétienne."},{"q": "Qu'est-ce que l'Aïd ?","r": "Une fête musulmane."},{"q": "Qu'est-ce que Yom Kippour ?","r": "Une fête juive."},{"q": "Qu'est-ce que Diwali ?","r": "Une fête hindoue."},{"q": "Qu'est-ce que le Ramadan ?","r": "Un mois de jeûne."},{"q": "Qu'est-ce que Hanoukka ?","r": "Une fête juive."},{"q": "Qu'est-ce que le Carnaval ?","r": "Une fête costumée."},{"q": "Qu'est-ce que la Fête nationale française ?","r": "Le 14 juillet."},{"q": "Qu'est-ce que Halloween ?","r": "Une fête du 31 octobre."},{"q": "Qu'est-ce que la Saint-Valentin ?","r": "La fête des amoureux."},{"q": "Qu'est-ce que la Fête des mères ?","r": "Une fête en l'honneur des mères."},{"q": "Qu'est-ce que la Fête des pères ?","r": "Une fête en l'honneur des pères."},{"q": "Qu'est-ce qu'un anniversaire ?","r": "La célébration d'une naissance."},{"q": "Qu'est-ce qu'un mariage ?","r": "Une union."},{"q": "Qu'est-ce qu'un baptême ?","r": "Un rite religieux."},{"q": "Qu'est-ce qu'un enterrement ?","r": "Un rite funéraire."},{"q": "Qu'est-ce qu'une tradition ?","r": "Une pratique transmise."},{"q": "Qu'est-ce qu'un proverbe ?","r": "Une maxime populaire."},{"q": "Que signifie \" qui vivra verra \" ?","r": "L'avenir le dira."},{"q": "Que signifie \" petit à petit, l'oiseau fait son nid \" ?","r": "La patience paie."},{"q": "Que signifie \" il n'y a pas de fumée sans feu \" ?","r": "Il y a une cause."},{"q": "Que signifie \" qui sème le vent récolte la tempête \" ?","r": "Les conséquences."},{"q": "Que signifie \" mieux vaut prévenir que guérir \" ?","r": "Prévenir est mieux."},{"q": "Que signifie \" l'union fait la force \" ?","r": "Ensemble on est plus fort."},{"q": "Que signifie \" qui ne risque rien n'a rien \" ?","r": "Il faut oser."},{"q": "Que signifie \" les bons comptes font les bons amis \" ?","r": "Être clair en affaires."},{"q": "Que signifie \" tout ce qui brille n'est pas or \" ?","r": "Ne pas se fier aux apparences."},{"q": "Qu'est-ce qu'une métaphore ?","r": "Une comparaison sans \" comme \"."},{"q": "Qu'est-ce qu'une comparaison ?","r": "Rapprocher deux choses."},{"q": "Qu'est-ce qu'une hyperbole ?","r": "Une exagération."},{"q": "Qu'est-ce qu'une ironie ?","r": "Dire le contraire."},{"q": "Qu'est-ce qu'un sarcasme ?","r": "Une moquerie."},{"q": "Qu'est-ce qu'un euphémisme ?","r": "Adoucir une idée."},{"q": "Qu'est-ce qu'un oxymore ?","r": "Deux mots opposés."},{"q": "Qu'est-ce qu'une rime ?","r": "Des sons semblables."},{"q": "Qu'est-ce qu'un vers ?","r": "Une ligne de poème."},{"q": "Qu'est-ce qu'une strophe ?","r": "Un groupe de vers."},{"q": "Qu'est-ce qu'un sonnet ?","r": "Un poème de 14 vers."},{"q": "Qu'est-ce qu'un conte ?","r": "Un récit merveilleux."},{"q": "Qu'est-ce qu'un mythe ?","r": "Un récit fondateur."},{"q": "Qu'est-ce qu'une légende ?","r": "Un récit traditionnel."},{"q": "Qu'est-ce qu'un roman policier ?","r": "Un roman d'enquête."},{"q": "Qu'est-ce que la science-fiction ?","r": "Un genre imaginaire."},{"q": "Qu'est-ce qu'une bande dessinée ?","r": "Un récit dessiné."},{"q": "Qu'est-ce qu'un manga ?","r": "Une BD japonaise."},{"q": "Qu'est-ce qu'un anime ?","r": "Un dessin animé japonais."},{"q": "Qu'est-ce qu'un dessin animé ?","r": "Un film d'animation."},{"q": "Qu'est-ce qu'un documentaire ?","r": "Un film informatif."},{"q": "Qu'est-ce qu'une série ?","r": "Une suite d'épisodes."},{"q": "Qu'est-ce qu'un épisode ?","r": "Une partie d'une série."},{"q": "Qu'est-ce qu'un scénario ?","r": "L'histoire d'un film."},{"q": "Qu'est-ce qu'un rôle ?","r": "Un personnage joué."},{"q": "Qu'est-ce qu'un casting ?","r": "Le choix des acteurs."},{"q": "Qu'est-ce qu'un plateau ?","r": "Le lieu de tournage."},{"q": "Qu'est-ce qu'un montage ?","r": "L'assemblage des scènes."},{"q": "Qu'est-ce qu'une bande-son ?","r": "La musique d'un film."},{"q": "Qu'est-ce qu'un effet spécial ?","r": "Une illusion visuelle."},{"q": "Qu'est-ce qu'un décor ?","r": "Le cadre d'une scène."},{"q": "Qu'est-ce qu'un costume ?","r": "Un vêtement de scène."},{"q": "Qu'est-ce qu'un maquillage ?","r": "Un produit de beauté."},{"q": "Qu'est-ce qu'un accessoire ?","r": "Un objet complémentaire."},{"q": "Qu'est-ce qu'un trophée ?","r": "Une récompense."},{"q": "Qu'est-ce qu'un champion ?","r": "Un vainqueur."},{"q": "Qu'est-ce qu'un entraîneur ?","r": "Celui qui entraîne."},{"q": "Qu'est-ce qu'un arbitre ?","r": "Celui qui juge."},{"q": "Qu'est-ce que le fair-play ?","r": "Le respect du jeu."},{"q": "Qu'est-ce que l'esprit d'équipe ?","r": "La coopération dans un groupe."},{"q": "Conjugue « être » au présent.","r": "Je suis, tu es, il est, nous sommes, vous êtes, ils sont."},{"q": "Conjugue « avoir » au présent.","r": "J'ai, tu as, il a, nous avons, vous avez, ils ont."},{"q": "Conjugue « aller » au futur.","r": "J'irai, tu iras, il ira, nous irons, vous irez, ils iront."},{"q": "Conjugue « faire » à l'imparfait.","r": "Je faisais, tu faisais, il faisait, nous faisions, vous faisiez, ils faisaient."},{"q": "Quel est le participe passé de « prendre » ?","r": "Pris."},{"q": "Quel est le participe passé de « voir » ?","r": "Vu."},{"q": "Quel est le participe passé de « mettre » ?","r": "Mis."},{"q": "Quel est le participe passé de « écrire » ?","r": "Écrit."},{"q": "Quel est le participe passé de « ouvrir » ?","r": "Ouvert."},{"q": "Quel est le participe passé de « dire » ?","r": "Dit."},{"q": "Quel est le participe passé de « pouvoir » ?","r": "Pu."},{"q": "Quel est le participe passé de « vouloir » ?","r": "Voulu."},{"q": "Quel est le participe passé de « savoir » ?","r": "Su."},{"q": "Quel est le participe passé de « boire » ?","r": "Bu."},{"q": "Quel est le participe passé de « courir » ?","r": "Couru."},{"q": "Conjugue « venir » au passé composé.","r": "Je suis venu, tu es venu, il est venu, nous sommes venus, vous êtes venus, ils sont venus."},{"q": "Conjugue « partir » au passé composé.","r": "Je suis parti, tu es parti, il est parti, nous sommes partis, vous êtes partis, ils sont partis."},{"q": "Quand utilise-t-on l'auxiliaire « être » ?","r": "Avec les verbes pronominaux et certains verbes de mouvement."},{"q": "Quand utilise-t-on l'auxiliaire « avoir » ?","r": "Avec la majorité des verbes."},{"q": "Qu'est-ce que le subjonctif ?","r": "Un mode exprimant le doute, le souhait ou l'obligation."},{"q": "Qu'est-ce qu'un déterminant ?","r": "Un mot qui précède le nom."},{"q": "Qu'est-ce qu'un pronom ?","r": "Un mot qui remplace le nom."},{"q": "Qu'est-ce qu'une conjonction ?","r": "Un mot qui relie deux éléments."},{"q": "Qu'est-ce qu'une préposition ?","r": "Un mot qui introduit un complément."},{"q": "Qu'est-ce qu'une interjection ?","r": "Un mot exprimant une émotion."},{"q": "Qu'est-ce qu'un complément d'objet direct ?","r": "Un complément sans préposition."},{"q": "Qu'est-ce qu'un complément d'objet indirect ?","r": "Un complément introduit par une préposition."},{"q": "Qu'est-ce qu'un complément circonstanciel ?","r": "Un complément de temps, lieu, manière…"},{"q": "Qu'est-ce qu'un attribut du sujet ?","r": "Un mot qui qualifie le sujet via un verbe d'état."},{"q": "Qu'est-ce qu'une proposition subordonnée ?","r": "Une proposition dépendant d'une principale."},{"q": "Qu'est-ce qu'une proposition relative ?","r": "Introduite par un pronom relatif."},{"q": "Qu'est-ce qu'une phrase complexe ?","r": "Une phrase avec plusieurs propositions."},{"q": "Qu'est-ce qu'une phrase simple ?","r": "Une phrase avec un seul verbe conjugué."},{"q": "Qu'est-ce que la voix passive ?","r": "Le sujet subit l'action."},{"q": "Qu'est-ce que la voix active ?","r": "Le sujet fait l'action."},{"q": "Qu'est-ce que le discours direct ?","r": "Rapporter les paroles telles quelles."},{"q": "Qu'est-ce que le discours indirect ?","r": "Rapporter les paroles en les transformant."},{"q": "Qu'est-ce qu'un champ lexical ?","r": "Un ensemble de mots sur un même thème."},{"q": "Qu'est-ce qu'un champ sémantique ?","r": "Les différents sens d'un mot."},{"q": "Qu'est-ce qu'une figure de style ?","r": "Un procédé d'expression."},{"q": "Comment écrit-on « parmi » ?","r": "P-a-r-m-i."},{"q": "Comment écrit-on « accueil » ?","r": "A-c-c-u-e-i-l."},{"q": "Comment écrit-on « développement » ?","r": "D-é-v-e-l-o-p-p-e-m-e-n-t."},{"q": "Comment écrit-on « environnement » ?","r": "E-n-v-i-r-o-n-n-e-m-e-n-t."},{"q": "Comment écrit-on « professionnel » ?","r": "P-r-o-f-e-s-s-i-o-n-n-e-l."},{"q": "Comment écrit-on « occasion » ?","r": "O-c-c-a-s-i-o-n."},{"q": "Comment écrit-on « appartement » ?","r": "A-p-p-a-r-t-e-m-e-n-t."},{"q": "Comment écrit-on « nécessaire » ?","r": "N-é-c-e-s-s-a-i-r-e."},{"q": "Comment écrit-on « malgré » ?","r": "M-a-l-g-r-é."},{"q": "Comment écrit-on « parmi nous » ?","r": "Parmi nous."},{"q": "Quand met-on un « s » à « cent » ?","r": "Quand il est multiplié et non suivi d'un autre nombre."},{"q": "Quand met-on un « s » à « vingt » ?","r": "Dans quatre-vingts et quatre-vingts millions."},{"q": "Accord de « tout » devant un nom féminin ?","r": "Toute."},{"q": "Accord de « quel » ?","r": "Quel, quelle, quels, quelles."},{"q": "Différence entre « a » et « à » ?","r": "« a » = verbe avoir ; « à » = préposition."},{"q": "Différence entre « ou » et « où » ?","r": "« ou » = choix ; « où » = lieu."},{"q": "Différence entre « son » et « sont » ?","r": "« son » = possessif ; « sont » = verbe être."},{"q": "Différence entre « et » et « est » ?","r": "« et » = conjonction ; « est » = verbe être."},{"q": "Différence entre « ce » et « se » ?","r": "« ce » = démonstratif ; « se » = pronom."},{"q": "Différence entre « mais » et « mes » ?","r": "« mais » = opposition ; « mes » = possessif."},{"q": "Qu'est-ce qu'un pléonasme ?","r": "Une répétition inutile."},{"q": "Qu'est-ce qu'un barbarisme ?","r": "Une faute de langue."},{"q": "Qu'est-ce qu'un solécisme ?","r": "Une faute de syntaxe."},{"q": "Qu'est-ce qu'un anglicisme ?","r": "Un emprunt à l'anglais."},{"q": "Qu'est-ce qu'un néologisme ?","r": "Un mot nouveau."},{"q": "Qu'est-ce qu'un archaïsme ?","r": "Un mot ancien."},{"q": "Qu'est-ce qu'un régionalisme ?","r": "Un mot propre à une région."},{"q": "Qu'est-ce qu'un acronyme ?","r": "Un sigle prononçable."},{"q": "Qu'est-ce qu'un sigle ?","r": "Des initiales."},{"q": "Qu'est-ce qu'une abréviation ?","r": "Un mot raccourci."},{"q": "Que signifie « ubiquité » ?","r": "Être partout à la fois."},{"q": "Que signifie « éphémère » ?","r": "Qui dure peu."},{"q": "Que signifie « ambigu » ?","r": "Qui a plusieurs sens."},{"q": "Que signifie « prolixe » ?","r": "Trop bavard."},{"q": "Que signifie « taciturne » ?","r": "Qui parle peu."},{"q": "Que signifie « magnanime » ?","r": "Généreux, noble."},{"q": "Que signifie « vénal » ?","r": "Qui se vend."},{"q": "Que signifie « obséquieux » ?","r": "Trop flatteur."},{"q": "Que signifie « loquace » ?","r": "Qui parle beaucoup."},{"q": "Que signifie « sagace » ?","r": "Perspicace."},{"q": "Que signifie « ineffable » ?","r": "Indicible."},{"q": "Que signifie « indicible » ?","r": "Qu'on ne peut exprimer."},{"q": "Que signifie « insidieux » ?","r": "Qui trompe."},{"q": "Que signifie « pernicieux » ?","r": "Nuisible."},{"q": "Que signifie « fastidieux » ?","r": "Ennuyeux."},{"q": "Que signifie « méticuleux » ?","r": "Très soigneux."},{"q": "Que signifie « opiniâtre » ?","r": "Persévérant."},{"q": "Que signifie « velléitaire » ?","r": "Qui veut sans agir."},{"q": "Que signifie « pusillanime » ?","r": "Peureux."},{"q": "Que signifie « altruiste » ?","r": "Qui aide les autres."},{"q": "Que signifie « avoir le bras long » ?","r": "Avoir de l'influence."},{"q": "Que signifie « casser du sucre sur le dos » ?","r": "Médire de quelqu'un."},{"q": "Que signifie « poser un lapin » ?","r": "Ne pas venir à un rendez-vous."},{"q": "Que signifie « tomber dans les pommes » ?","r": "S'évanouir."},{"q": "Que signifie « mettre la puce à l'oreille » ?","r": "Éveiller les soupçons."},{"q": "Que signifie « avoir un poil dans la main » ?","r": "Être paresseux."},{"q": "Que signifie « tirer la couverture à soi » ?","r": "S'attribuer les mérites."},{"q": "Que signifie « jeter l'éponge » ?","r": "Renoncer."},{"q": "Que signifie « payer les pots cassés » ?","r": "Subir les conséquences."},{"q": "Que signifie « coûter les yeux de la tête » ?","r": "Être très cher."},{"q": "Que signifie « avoir le cœur sur la main » ?","r": "Être généreux."},{"q": "Que signifie « crier sur les toits » ?","r": "Rendre public."},{"q": "Que signifie « tourner autour du pot » ?","r": "Ne pas aller au fait."},{"q": "Que signifie « se noyer dans un verre d'eau » ?","r": "S'alarmer pour peu."},{"q": "Que signifie « être dans de beaux draps » ?","r": "Être dans une situation difficile."},{"q": "Que signifie « faire la grasse matinée » ?","r": "Dormir tard."},{"q": "Que signifie « avoir du pain sur la planche » ?","r": "Avoir beaucoup à faire."},{"q": "Que signifie « mettre de l'eau dans son vin » ?","r": "Modérer ses exigences."},{"q": "Que signifie « rire jaune » ?","r": "Rire sans joie."},{"q": "Que signifie « être à l'aise comme un poisson dans l'eau » ?","r": "Être parfaitement à l'aise."},{"q": "Comment dire non poliment ?","r": "« Je ne peux pas, merci. »"},{"q": "Comment formuler une critique ?","r": "Avec bienveillance et exemples."},{"q": "Comment faire une demande polie ?","r": "« Pourriez-vous… s'il vous plaît ? »"},{"q": "Comment s'excuser ?","r": "« Je suis désolé, je m'excuse. »"},{"q": "Comment féliciter ?","r": "« Félicitations, bravo ! »"},{"q": "Comment présenter quelqu'un ?","r": "« Je te présente… »"},{"q": "Comment prendre congé ?","r": "« Je dois y aller, à bientôt. »"},{"q": "Comment proposer de l'aide ?","r": "« Veux-tu de l'aide ? »"},{"q": "Comment exprimer un désaccord ?","r": "« Je comprends, mais je pense différemment. »"},{"q": "Comment nuancer son propos ?","r": "Utiliser « peut-être », « cependant »."},{"q": "Qu'est-ce que la reformulation ?","r": "Redire avec ses mots."},{"q": "Qu'est-ce qu'un message « je » ?","r": "Parler de soi, non accuser."},{"q": "Qu'est-ce qu'un feedback ?","r": "Un retour d'information."},{"q": "Qu'est-ce qu'un feedback constructif ?","r": "Utile et bienveillant."},{"q": "Qu'est-ce que la médiation ?","r": "Aider à résoudre un conflit."},{"q": "Qu'est-ce que la négociation ?","r": "Trouver un accord."},{"q": "Qu'est-ce que le compromis ?","r": "Chacun fait un pas."},{"q": "Qu'est-ce qu'une rumeur ?","r": "Une information non vérifiée."},{"q": "Qu'est-ce que la diffamation ?","r": "Atteinte à la réputation."},{"q": "Qu'est-ce que la calomnie ?","r": "Accusation fausse."},{"q": "Qu'est-ce que le harcèlement ?","r": "Comportements répétés hostiles."},{"q": "Qu'est-ce que le cyberharcèlement ?","r": "Harcèlement en ligne."},{"q": "Que faire face à une rumeur ?","r": "Ne pas la propager, vérifier."},{"q": "Comment refuser une demande ?","r": "Dire non clairement, avec respect."},{"q": "Comment gérer un conflit ?","r": "Écouter, exprimer, chercher une solution."},{"q": "Comment s'excuser sincèrement ?","r": "Reconnaître, expliquer, réparer."},{"q": "Comment remercier ?","r": "Exprimer sa gratitude."},{"q": "Qu'est-ce que la politesse ?","r": "Le respect des convenances."},{"q": "Qu'est-ce que le savoir-vivre ?","r": "Les règles de bonne conduite."},{"q": "Qu'est-ce que l'étiquette ?","r": "Les règles protocolaires."},{"q": "Qu'est-ce que le respect des autres ?","r": "Considérer autrui."},{"q": "Qu'est-ce que la tolérance ?","r": "Accepter les différences."},{"q": "Qu'est-ce que la discrimination ?","r": "Traiter inéquitablement."},{"q": "Qu'est-ce que le racisme ?","r": "Discrimination raciale."},{"q": "Qu'est-ce que le sexisme ?","r": "Discrimination de sexe."},{"q": "Qu'est-ce que l'homophobie ?","r": "Rejet de l'homosexualité."},{"q": "Qu'est-ce que l'inclusion ?","r": "Intégrer tout le monde."},{"q": "Comment rédiger un CV ?","r": "Structuré, clair, avec expériences et compétences."},{"q": "Quelles rubriques dans un CV ?","r": "Identité, formation, expériences, compétences, langues."},{"q": "Comment rédiger une lettre de motivation ?","r": "Personnalisée, structurée : vous, moi, nous."},{"q": "Comment se préparer à un entretien ?","r": "Se renseigner sur l'entreprise, préparer des exemples."},{"q": "Quelles questions poser en entretien ?","r": "Sur le poste, l'équipe, les perspectives."},{"q": "Qu'est-ce qu'un CDI ?","r": "Contrat à durée indéterminée."},{"q": "Qu'est-ce qu'un CDD ?","r": "Contrat à durée déterminée."},{"q": "Qu'est-ce qu'un stage ?","r": "Une période de formation en entreprise."},{"q": "Qu'est-ce qu'une période d'essai ?","r": "Une période de test du contrat."},{"q": "Qu'est-ce qu'un préavis ?","r": "Un délai avant rupture."},{"q": "Qu'est-ce qu'une convention collective ?","r": "Un accord de branche."},{"q": "Qu'est-ce qu'un délégué du personnel ?","r": "Un représentant des salariés."},{"q": "Qu'est-ce qu'un licenciement ?","r": "Rupture par l'employeur."},{"q": "Qu'est-ce qu'une démission ?","r": "Rupture par le salarié."},{"q": "Qu'est-ce qu'un burn-out ?","r": "Un épuisement professionnel."},{"q": "Qu'est-ce que la charge mentale ?","r": "Une surcharge cognitive."},{"q": "Qu'est-ce que la conciliation vie pro/perso ?","r": "Équilibrer travail et vie privée."},{"q": "Qu'est-ce que le télétravail ?","r": "Travailler à distance."},{"q": "Qu'est-ce que la flexibilité au travail ?","r": "Adapter horaires et lieux."},{"q": "Comment organiser ses révisions ?","r": "Planifier, espacer, s'auto-tester."},{"q": "Qu'est-ce que la courbe de l'oubli ?","r": "On oublie vite sans révision."},{"q": "Qu'est-ce que la méthode des flashcards ?","r": "Des cartes question/réponse."},{"q": "Qu'est-ce que le par cœur ?","r": "Mémoriser sans comprendre."},{"q": "Comprendre ou mémoriser ?","r": "Comprendre est plus durable."},{"q": "Qu'est-ce que la prise de notes ?","r": "Noter l'essentiel."},{"q": "Qu'est-ce qu'une carte mentale ?","r": "Un schéma d'idées."},{"q": "Qu'est-ce que la méthode Cornell ?","r": "Notes en colonnes."},{"q": "Qu'est-ce que la lecture active ?","r": "Lire en questionnant."},{"q": "Qu'est-ce que le baccalauréat ?","r": "Un diplôme de fin de lycée."},{"q": "Qu'est-ce que le brevet ?","r": "Un diplôme de fin de collège."},{"q": "Qu'est-ce qu'une licence ?","r": "Un diplôme bac+3."},{"q": "Qu'est-ce qu'un master ?","r": "Un diplôme bac+5."},{"q": "Qu'est-ce qu'un doctorat ?","r": "Un diplôme bac+8."},{"q": "Qu'est-ce qu'un BTS ?","r": "Un diplôme bac+2."},{"q": "Qu'est-ce qu'un DUT ?","r": "Un diplôme bac+2."},{"q": "Qu'est-ce qu'une grande école ?","r": "Une école sélective."},{"q": "Qu'est-ce que Parcoursup ?","r": "Une plateforme d'orientation."},{"q": "Qu'est-ce qu'une bourse ?","r": "Une aide financière."},{"q": "Qu'est-ce que l'apprentissage ?","r": "Une formation en alternance."},{"q": "Qu'est-ce qu'un nombre complexe ?","r": "Un nombre avec partie réelle et imaginaire."},{"q": "Qu'est-ce qu'une dérivée ?","r": "Un taux de variation."},{"q": "Qu'est-ce qu'une intégrale ?","r": "Une aire sous une courbe."},{"q": "Qu'est-ce qu'une limite ?","r": "Une valeur vers laquelle on tend."},{"q": "Qu'est-ce qu'une suite ?","r": "Une liste ordonnée de nombres."},{"q": "Qu'est-ce qu'une suite arithmétique ?","r": "On ajoute une raison."},{"q": "Qu'est-ce qu'une suite géométrique ?","r": "On multiplie par une raison."},{"q": "Qu'est-ce qu'un vecteur ?","r": "Un objet avec direction, sens, norme."},{"q": "Qu'est-ce qu'une matrice ?","r": "Un tableau de nombres."},{"q": "Qu'est-ce que la trigonométrie ?","r": "L'étude des triangles."},{"q": "Que vaut sin(0) ?","r": "0."},{"q": "Que vaut cos(0) ?","r": "1."},{"q": "Que vaut tan(45°) ?","r": "1."},{"q": "Qu'est-ce que le théorème de Thalès ?","r": "Un rapport de longueurs."},{"q": "Qu'est-ce qu'une probabilité ?","r": "Une mesure de chance."},{"q": "Probabilité d'obtenir pile ?","r": "1/2."},{"q": "Probabilité d'un dé à 6 ?","r": "1/6."},{"q": "Qu'est-ce qu'un écart-type ?","r": "Une mesure de dispersion."},{"q": "Qu'est-ce que la médiane ?","r": "La valeur centrale."},{"q": "Qu'est-ce que la vitesse ?","r": "Distance par unité de temps."},{"q": "Qu'est-ce que l'accélération ?","r": "Variation de la vitesse."},{"q": "Qu'est-ce qu'une force ?","r": "Une interaction."},{"q": "Qu'est-ce que la masse ?","r": "La quantité de matière."},{"q": "Qu'est-ce que le poids ?","r": "Force d'attraction terrestre."},{"q": "Qu'est-ce que la pression ?","r": "Force par surface."},{"q": "Qu'est-ce que la densité ?","r": "Masse par volume."},{"q": "Qu'est-ce que l'énergie cinétique ?","r": "Énergie du mouvement."},{"q": "Qu'est-ce que l'énergie potentielle ?","r": "Énergie de position."},{"q": "Qu'est-ce que la loi d'Ohm ?","r": "U = R × I."},{"q": "Qu'est-ce qu'un ion ?","r": "Atome chargé."},{"q": "Qu'est-ce qu'une molécule ?","r": "Un groupe d'atomes liés."},{"q": "Qu'est-ce qu'une réaction chimique ?","r": "Transformation de la matière."},{"q": "Qu'est-ce qu'un catalyseur ?","r": "Accélère une réaction."},{"q": "Qu'est-ce qu'un oxyde ?","r": "Composé avec oxygène."},{"q": "Qu'est-ce qu'un acide fort ?","r": "Qui se dissocie totalement."},{"q": "Qu'est-ce qu'une solution ?","r": "Mélange homogène."},{"q": "Qu'est-ce qu'une suspension ?","r": "Mélange hétérogène."},{"q": "Qu'est-ce que la distillation ?","r": "Séparer par ébullition."},{"q": "Qu'est-ce que la mitose ?","r": "Division cellulaire."},{"q": "Qu'est-ce que la méiose ?","r": "Division pour les gamètes."},{"q": "Qu'est-ce qu'un gène dominant ?","r": "Qui s'exprime toujours."},{"q": "Qu'est-ce qu'un gène récessif ?","r": "Qui s'exprime si double."},{"q": "Qu'est-ce que l'ARN ?","r": "Copie temporaire de l'ADN."},{"q": "Qu'est-ce qu'une protéine ?","r": "Une chaîne d'acides aminés."},{"q": "Qu'est-ce qu'un enzyme ?","r": "Une protéine catalytique."},{"q": "Qu'est-ce que la chlorophylle ?","r": "Le pigment vert."},{"q": "Qu'est-ce que la respiration cellulaire ?","r": "Production d'énergie."},{"q": "Qu'est-ce qu'une galaxie ?","r": "Un ensemble d'étoiles."},{"q": "Combien d'étoiles dans la Voie lactée ?","r": "Des centaines de milliards."},{"q": "Qu'est-ce qu'une nébuleuse ?","r": "Un nuage de gaz."},{"q": "Qu'est-ce qu'une supernova ?","r": "Explosion d'étoile."},{"q": "Qu'est-ce qu'une naine blanche ?","r": "Reste d'étoile."},{"q": "Qu'est-ce qu'une année-lumière ?","r": "Distance parcourue par la lumière en un an."},{"q": "Qu'est-ce que l'unité astronomique ?","r": "Distance Terre-Soleil."},{"q": "Qu'est-ce que la matière noire ?","r": "Matière invisible."},{"q": "Qu'est-ce que l'énergie noire ?","r": "Force d'expansion."},{"q": "Qu'est-ce que l'exoplanète ?","r": "Planète hors du système solaire."},{"q": "Qu'est-ce qu'un pulsar ?","r": "Étoile à neutrons pulsante."},{"q": "Qu'est-ce qu'un quasar ?","r": "Noyau actif de galaxie."},{"q": "Qu'est-ce que la ceinture d'astéroïdes ?","r": "Entre Mars et Jupiter."},{"q": "Qu'est-ce que la ceinture de Kuiper ?","r": "Au-delà de Neptune."},{"q": "Qu'est-ce que le nuage de Oort ?","r": "Source des comètes."},{"q": "Qu'est-ce qu'un météore ?","r": "Étoile filante."},{"q": "Qu'est-ce qu'un météorite ?","r": "Météore arrivé au sol."},{"q": "Qu'est-ce qu'un satellite ?","r": "Objet en orbite."},{"q": "Combien de satellites naturels pour Mars ?","r": "2 : Phobos et Déimos."},{"q": "Qu'est-ce que la programmation orientée objet ?","r": "Un paradigme à objets."},{"q": "Qu'est-ce qu'une classe ?","r": "Un modèle d'objet."},{"q": "Qu'est-ce qu'un objet ?","r": "Une instance de classe."},{"q": "Qu'est-ce que l'héritage ?","r": "Réutiliser une classe."},{"q": "Qu'est-ce que le polymorphisme ?","r": "Plusieurs formes d'une méthode."},{"q": "Qu'est-ce que l'encapsulation ?","r": "Cacher les détails internes."},{"q": "Qu'est-ce qu'une API ?","r": "Interface de programmation."},{"q": "Qu'est-ce qu'un framework ?","r": "Un cadre de développement."},{"q": "Qu'est-ce qu'une bibliothèque ?","r": "Du code réutilisable."},{"q": "Qu'est-ce qu'un IDE ?","r": "Environnement de développement."},{"q": "Qu'est-ce que Git ?","r": "Un gestionnaire de versions."},{"q": "Qu'est-ce que GitHub ?","r": "Une plateforme Git."},{"q": "Qu'est-ce qu'un commit ?","r": "Une sauvegarde de code."},{"q": "Qu'est-ce qu'une branche ?","r": "Une version parallèle."},{"q": "Qu'est-ce qu'un merge ?","r": "Fusionner des branches."},{"q": "Qu'est-ce qu'un test unitaire ?","r": "Tester une fonction."},{"q": "Qu'est-ce que le déploiement ?","r": "Mettre en production."},{"q": "Qu'est-ce que le DevOps ?","r": "Dev + Ops."},{"q": "Qu'est-ce que Docker ?","r": "Conteneurisation d'applications."},{"q": "Qu'est-ce que le phishing ?","r": "Hameçonnage."},{"q": "Qu'est-ce qu'un ransomware ?","r": "Rançongiciel."},{"q": "Qu'est-ce qu'un cheval de Troie ?","r": "Un logiciel piégé."},{"q": "Qu'est-ce qu'un spyware ?","r": "Logiciel espion."},{"q": "Qu'est-ce qu'un adware ?","r": "Logiciel publicitaire."},{"q": "Qu'est-ce qu'un antivirus ?","r": "Protection contre virus."},{"q": "Qu'est-ce que le chiffrement ?","r": "Coder des données."},{"q": "Qu'est-ce que HTTPS ?","r": "HTTP sécurisé."},{"q": "Qu'est-ce qu'une authentification à deux facteurs ?","r": "Double vérification."},{"q": "Qu'est-ce que Facebook ?","r": "Un réseau social."},{"q": "Qu'est-ce qu'Instagram ?","r": "Un réseau social photo."},{"q": "Qu'est-ce que Twitter/X ?","r": "Un réseau social de microblogage."},{"q": "Qu'est-ce que TikTok ?","r": "Réseau social vidéo."},{"q": "Qu'est-ce que YouTube ?","r": "Plateforme vidéo."},{"q": "Qu'est-ce que LinkedIn ?","r": "Réseau professionnel."},{"q": "Qu'est-ce que WhatsApp ?","r": "Application de messagerie."},{"q": "Qu'est-ce que Snapchat ?","r": "Application de messages éphémères."},{"q": "Qu'est-ce que Reddit ?","r": "Forum communautaire."},{"q": "Qu'est-ce qu'un hashtag ?","r": "Mot-clé précédé de #."},{"q": "Qu'est-ce qu'un influenceur ?","r": "Personne à audience large."},{"q": "Qu'est-ce que le marketing d'influence ?","r": "Publicité par influenceurs."},{"q": "Qu'est-ce que le SEO ?","r": "Optimisation pour moteurs de recherche."},{"q": "Qu'est-ce que le SEA ?","r": "Publicité sur moteurs."},{"q": "Qu'est-ce qu'un cookie publicitaire ?","r": "Traceur de navigation."},{"q": "Qu'est-ce que le RGPD ?","r": "Règlement sur les données."},{"q": "Qu'est-ce que la vie privée numérique ?","r": "Protection des données."},{"q": "Qu'est-ce que le droit à l'oubli ?","r": "Effacer des données."},{"q": "Qu'est-ce qu'une fake news ?","r": "Fausse information."},{"q": "Comment vérifier une info ?","r": "Croiser les sources."},{"q": "Qu'est-ce qu'un RPG ?","r": "Jeu de rôle."},{"q": "Qu'est-ce qu'un FPS ?","r": "Jeu de tir en vue subjective."},{"q": "Qu'est-ce qu'un MOBA ?","r": "Arène de bataille en ligne."},{"q": "Qu'est-ce qu'un MMO ?","r": "Jeu massivement multijoueur."},{"q": "Qu'est-ce qu'un speedrun ?","r": "Finir un jeu vite."},{"q": "Qu'est-ce que l'e-sport ?","r": "Compétition de jeux vidéo."},{"q": "Qu'est-ce qu'un loot ?","r": "Butin de jeu."},{"q": "Qu'est-ce qu'un NPC ?","r": "Personnage non joueur."},{"q": "Qu'est-ce qu'un DLC ?","r": "Contenu téléchargeable."},{"q": "Qu'est-ce qu'un patch ?","r": "Mise à jour."},{"q": "Qu'est-ce qu'un gameplay ?","r": "Le jeu en action."},{"q": "Qu'est-ce qu'une console ?","r": "Machine de jeu."},{"q": "Quelle est la console de Nintendo ?","r": "Switch."},{"q": "Quelle est la console de Sony ?","r": "PlayStation."},{"q": "Quelle est la console de Microsoft ?","r": "Xbox."},{"q": "Quel jeu est le plus vendu ?","r": "Minecraft."},{"q": "Qu'est-ce que Minecraft ?","r": "Jeu de construction."},{"q": "Qu'est-ce que Fortnite ?","r": "Battle royale."},{"q": "Qu'est-ce que League of Legends ?","r": "MOBA."},{"q": "Qu'est-ce que FIFA ?","r": "Jeu de football."},{"q": "Qu'est-ce que le 7e art ?","r": "Le cinéma."},{"q": "Qu'est-ce qu'un long-métrage ?","r": "Film de plus d'1h."},{"q": "Qu'est-ce qu'un court-métrage ?","r": "Film court."},{"q": "Qu'est-ce qu'un blockbuster ?","r": "Film à grand budget."},{"q": "Qu'est-ce qu'un film d'auteur ?","r": "Film personnel."},{"q": "Qu'est-ce qu'un biopic ?","r": "Film biographique."},{"q": "Qu'est-ce qu'un remake ?","r": "Nouvelle version."},{"q": "Qu'est-ce qu'un prequel ?","r": "Avant l'original."},{"q": "Qu'est-ce qu'un crossover ?","r": "Mélange d'univers."},{"q": "Qu'est-ce qu'un caméo ?","r": "Apparition brève."},{"q": "Qu'est-ce qu'un spoiler ?","r": "Révélation d'intrigue."},{"q": "Qu'est-ce que la bande-annonce ?","r": "Extrait promotionnel."},{"q": "Qu'est-ce qu'un générique ?","r": "Liste des participants."},{"q": "Qu'est-ce qu'un box-office ?","r": "Recettes d'un film."},{"q": "Qu'est-ce qu'une Palme d'or ?","r": "Prix de Cannes."},{"q": "Qu'est-ce qu'un César ?","r": "Prix français."},{"q": "Qu'est-ce qu'une série télé ?","r": "Suite d'épisodes."},{"q": "Qu'est-ce qu'une note ?","r": "Un son musical."},{"q": "Combien de notes ?","r": "7 : do ré mi fa sol la si."},{"q": "Qu'est-ce qu'une gamme ?","r": "Une suite de notes."},{"q": "Qu'est-ce qu'un accord ?","r": "Plusieurs notes simultanées."},{"q": "Qu'est-ce qu'une mélodie ?","r": "Suite de notes."},{"q": "Qu'est-ce qu'un rythme ?","r": "Organisation du temps."},{"q": "Qu'est-ce que le tempo ?","r": "Vitesse de la musique."},{"q": "Qu'est-ce qu'un instrument à vent ?","r": "Flûte, trompette…"},{"q": "Qu'est-ce qu'un instrument à percussion ?","r": "Tambour, xylophone…"},{"q": "Qu'est-ce qu'un piano ?","r": "Instrument à cordes frappées."},{"q": "Qu'est-ce qu'une guitare ?","r": "Instrument à cordes pincées."},{"q": "Qu'est-ce qu'un violon ?","r": "Instrument à cordes frottées."},{"q": "Qu'est-ce qu'une harpe ?","r": "Instrument à cordes pincées."},{"q": "Qu'est-ce que le solfège ?","r": "Théorie musicale."},{"q": "Qu'est-ce qu'une partition ?","r": "Notation musicale."},{"q": "Qu'est-ce qu'un chef d'orchestre ?","r": "Dirige un orchestre."},{"q": "Qu'est-ce qu'une comédie musicale ?","r": "Spectacle chanté et dansé."},{"q": "Qu'est-ce qu'un concert ?","r": "Spectacle musical en direct."},{"q": "Qu'est-ce que la danse classique ?","r": "Danse académique."},{"q": "Qu'est-ce que la danse contemporaine ?","r": "Danse moderne."},{"q": "Qu'est-ce que le hip-hop ?","r": "Culture urbaine."},{"q": "Qu'est-ce que la valse ?","r": "Danse à 3 temps."},{"q": "Qu'est-ce que le tango ?","r": "Danse argentine."},{"q": "Qu'est-ce que la capoeira ?","r": "Art martial dansé brésilien."},{"q": "Qu'est-ce que le flamenco ?","r": "Danse andalouse."},{"q": "Qu'est-ce que la breakdance ?","r": "Danse hip-hop acrobatique."},{"q": "Qu'est-ce que la pole dance ?","r": "Danse sur barre."},{"q": "Qu'est-ce qu'un chorégraphe ?","r": "Crée des danses."},{"q": "Qu'est-ce qu'un pas de deux ?","r": "Danse à deux."},{"q": "Qu'est-ce qu'un tutu ?","r": "Jupe de ballerine."},{"q": "Qu'est-ce que le mime ?","r": "Art du silence."},{"q": "Qu'est-ce que le cirque ?","r": "Spectacle d'acrobaties."},{"q": "Qu'est-ce qu'un trapèze ?","r": "Agrès de cirque."},{"q": "Qu'est-ce qu'un jongleur ?","r": "Artiste qui jongle."},{"q": "Qu'est-ce qu'un clown ?","r": "Artiste comique."},{"q": "Qu'est-ce qu'un magicien ?","r": "Artiste d'illusions."},{"q": "Qu'est-ce que la photographie ?","r": "Art de capturer des images."},{"q": "Qu'est-ce que la lumière ?","r": "Onde électromagnétique."},{"q": "Qu'est-ce que l'ouverture ?","r": "Taille du diaphragme."},{"q": "Qu'est-ce que la vitesse d'obturation ?","r": "Temps d'exposition."},{"q": "Qu'est-ce que la sensibilité ISO ?","r": "Sensibilité du capteur."},{"q": "Qu'est-ce que la profondeur de champ ?","r": "Zone nette d'une image."},{"q": "Qu'est-ce que la règle des tiers ?","r": "Composition en 3×3."},{"q": "Qu'est-ce qu'un objectif ?","r": "Lentille d'appareil."},{"q": "Qu'est-ce qu'un trépied ?","r": "Support stable."},{"q": "Qu'est-ce qu'un flash ?","r": "Source de lumière."},{"q": "Qu'est-ce que le noir et blanc ?","r": "Photo sans couleur."},{"q": "Qu'est-ce que le HDR ?","r": "Forte plage dynamique."},{"q": "Qu'est-ce que le portrait ?","r": "Photo d'une personne."},{"q": "Qu'est-ce que le paysage ?","r": "Photo de nature."},{"q": "Qu'est-ce que la macro ?","r": "Photo de très près."},{"q": "Qu'est-ce que le street photography ?","r": "Photo de rue."},{"q": "Qu'est-ce que la photo de nuit ?","r": "Photo en faible lumière."},{"q": "Qu'est-ce qu'un retardateur ?","r": "Déclenchement différé."},{"q": "Qu'est-ce qu'un fichier RAW ?","r": "Format brut."},{"q": "Qu'est-ce que le JPEG ?","r": "Format compressé."},{"q": "Qu'est-ce que l'architecture ?","r": "Art de concevoir des bâtiments."},{"q": "Qu'est-ce que le gothique ?","r": "Style médiéval."},{"q": "Qu'est-ce que le roman ?","r": "Style médiéval."},{"q": "Qu'est-ce que le baroque ?","r": "Style ornementé."},{"q": "Qu'est-ce que le classique ?","r": "Style épuré."},{"q": "Qu'est-ce que le moderne ?","r": "Style du 20e siècle."},{"q": "Qu'est-ce que le contemporain ?","r": "Style actuel."},{"q": "Qu'est-ce que le Bauhaus ?","r": "École de design."},{"q": "Qu'est-ce que le minimalisme ?","r": "Style épuré."},{"q": "Qu'est-ce que le brutalisme ?","r": "Béton brut."},{"q": "Qu'est-ce qu'un gratte-ciel ?","r": "Immeuble très haut."},{"q": "Quel est le plus haut gratte-ciel ?","r": "Burj Khalifa."},{"q": "Qu'est-ce qu'une cathédrale ?","r": "Église principale."},{"q": "Qu'est-ce qu'un château ?","r": "Demeure fortifiée."},{"q": "Qu'est-ce qu'un pont ?","r": "Ouvrage de franchissement."},{"q": "Qu'est-ce qu'un viaduc ?","r": "Grand pont."},{"q": "Qu'est-ce qu'un tunnel ?","r": "Passage souterrain."},{"q": "Qu'est-ce qu'un barrage ?","r": "Ouvrage retenant l'eau."},{"q": "Qu'est-ce qu'un phare ?","r": "Tour signalant la côte."},{"q": "Qu'est-ce qu'un stade ?","r": "Lieu de sport."},{"q": "Qu'est-ce que la mythologie grecque ?","r": "Mythes grecs."},{"q": "Qui est Zeus ?","r": "Dieu grec du ciel."},{"q": "Qui est Poséidon ?","r": "Dieu grec de la mer."},{"q": "Qui est Hadès ?","r": "Dieu grec des enfers."},{"q": "Qui est Athéna ?","r": "Déesse grecque de la sagesse."},{"q": "Qui est Apollon ?","r": "Dieu grec des arts."},{"q": "Qui est Artémis ?","r": "Déesse grecque de la chasse."},{"q": "Qui est Aphrodite ?","r": "Déesse grecque de l'amour."},{"q": "Qui est Arès ?","r": "Dieu grec de la guerre."},{"q": "Qui est Hermès ?","r": "Messager des dieux."},{"q": "Qui est Héra ?","r": "Reine des dieux."},{"q": "Qui est Déméter ?","r": "Déesse des moissons."},{"q": "Qui est Dionysos ?","r": "Dieu du vin."},{"q": "Qui est Héphaïstos ?","r": "Dieu forgeron."},{"q": "Qui est Thor ?","r": "Dieu nordique du tonnerre."},{"q": "Qui est Odin ?","r": "Dieu nordique."},{"q": "Qui est Loki ?","r": "Dieu nordique rusé."},{"q": "Qui est Anubis ?","r": "Dieu égyptien des morts."},{"q": "Qui est Rê ?","r": "Dieu égyptien du soleil."},{"q": "Qui est Osiris ?","r": "Dieu égyptien."},{"q": "Qu'est-ce que le christianisme ?","r": "Religion fondée sur Jésus."},{"q": "Qu'est-ce que l'islam ?","r": "Religion fondée sur Mahomet."},{"q": "Qu'est-ce que le judaïsme ?","r": "Religion du peuple juif."},{"q": "Qu'est-ce que le bouddhisme ?","r": "Religion fondée par Bouddha."},{"q": "Qu'est-ce que l'hindouisme ?","r": "Religion indienne."},{"q": "Qu'est-ce que le shintoïsme ?","r": "Religion japonaise."},{"q": "Qu'est-ce que le sikhisme ?","r": "Religion indienne."},{"q": "Qu'est-ce que le taoïsme ?","r": "Philosophie chinoise."},{"q": "Qu'est-ce que le confucianisme ?","r": "Philosophie chinoise."},{"q": "Qu'est-ce que l'athéisme ?","r": "Absence de croyance en dieu."},{"q": "Qu'est-ce que l'agnosticisme ?","r": "On ne peut savoir."},{"q": "Qu'est-ce qu'un monothéisme ?","r": "Un seul dieu."},{"q": "Qu'est-ce qu'un polythéisme ?","r": "Plusieurs dieux."},{"q": "Qu'est-ce qu'une religion ?","r": "Un système de croyances."},{"q": "Qu'est-ce qu'un rite ?","r": "Une pratique."},{"q": "Qu'est-ce qu'un prêtre ?","r": "Un ministre religieux."},{"q": "Qu'est-ce qu'un imam ?","r": "Guide musulman."},{"q": "Qu'est-ce qu'un rabbin ?","r": "Guide juif."},{"q": "Qu'est-ce qu'un moine ?","r": "Religieux retiré."},{"q": "Qu'est-ce qu'une prière ?","r": "Communication avec le divin."},{"q": "Qu'est-ce que la philosophie ?","r": "Amour de la sagesse."},{"q": "Qui est Socrate ?","r": "Philosophe grec."},{"q": "Qui est Platon ?","r": "Philosophe grec, disciple de Socrate."},{"q": "Qui est Aristote ?","r": "Philosophe grec."},{"q": "Qui est Descartes ?","r": "Philosophe français."},{"q": "Qui est Kant ?","r": "Philosophe allemand."},{"q": "Qui est Nietzsche ?","r": "Philosophe allemand."},{"q": "Qui est Sartre ?","r": "Philosophe existentialiste."},{"q": "Qui est Simone de Beauvoir ?","r": "Philosophe française."},{"q": "Qui est Hannah Arendt ?","r": "Philosophe allemande."},{"q": "Qu'est-ce que la justice ?","r": "Le respect du droit."},{"q": "Qu'est-ce que la vérité ?","r": "Conformité au réel."},{"q": "Qu'est-ce que le bien ?","r": "Ce qui est moralement bon."},{"q": "Qu'est-ce que le mal ?","r": "Ce qui est moralement mauvais."},{"q": "Qu'est-ce que la conscience ?","r": "La connaissance de soi."},{"q": "Qu'est-ce que la raison ?","r": "Faculté de penser."},{"q": "Qu'est-ce que la logique ?","r": "Art de raisonner."},{"q": "Qu'est-ce que l'éthique ?","r": "Réflexion morale."},{"q": "Qu'est-ce que la morale ?","r": "Règles de conduite."},{"q": "Qu'est-ce que la Révolution française ?","r": "1789-1799."},{"q": "Qui était Robespierre ?","r": "Figure révolutionnaire."},{"q": "Qui était Danton ?","r": "Révolutionnaire français."},{"q": "Qui était Marat ?","r": "Révolutionnaire français."},{"q": "Qu'est-ce que la Terreur ?","r": "Période 1793-1794."},{"q": "Qui était Louis XVI ?","r": "Roi de France exécuté en 1793."},{"q": "Qui était Marie-Antoinette ?","r": "Reine de France."},{"q": "Qu'est-ce que l'Empire napoléonien ?","r": "1804-1814."},{"q": "Qu'est-ce que Waterloo ?","r": "Défaite de Napoléon en 1815."},{"q": "Qu'est-ce que la Commune de Paris ?","r": "1871."},{"q": "Qu'est-ce que la IIIe République ?","r": "1870-1940."},{"q": "Qu'est-ce que l'affaire Dreyfus ?","r": "Affaire judiciaire 1894-1906."},{"q": "Qu'est-ce que la Belle Époque ?","r": "1900-1914."},{"q": "Qu'est-ce que le Front populaire ?","r": "1936."},{"q": "Qu'est-ce que Vichy ?","r": "Régime 1940-1944."},{"q": "Qui était Pétain ?","r": "Chef de Vichy."},{"q": "Qu'est-ce que la Résistance ?","r": "Lutte contre l'occupation."},{"q": "Qu'est-ce que la Libération ?","r": "1944."},{"q": "Qu'est-ce que la Ve République ?","r": "Depuis 1958."},{"q": "Qui a fondé la Ve République ?","r": "De Gaulle."},{"q": "Qu'est-ce que la Mésopotamie ?","r": "Ancienne région du Moyen-Orient."},{"q": "Qu'est-ce que l'Égypte antique ?","r": "Civilisation du Nil."},{"q": "Qu'est-ce que la Grèce antique ?","r": "Civilisation grecque."},{"q": "Qu'est-ce que Rome antique ?","r": "Civilisation romaine."},{"q": "Qu'est-ce que le Moyen Âge ?","r": "476-1492."},{"q": "Qu'est-ce que la Renaissance ?","r": "15e-16e siècles."},{"q": "Qu'est-ce que les Lumières ?","r": "Mouvement philosophique du 18e."},{"q": "Qu'est-ce que la Révolution industrielle ?","r": "19e siècle."},{"q": "Qu'est-ce que la colonisation ?","r": "Domination de territoires."},{"q": "Qu'est-ce que la décolonisation ?","r": "Indépendances."},{"q": "Qu'est-ce que la Guerre froide ?","r": "1947-1991."},{"q": "Qu'est-ce que la chute du mur de Berlin ?","r": "1989."},{"q": "Qu'est-ce que la mondialisation ?","r": "Intégration mondiale."},{"q": "Qu'est-ce que le Moyen-Orient ?","r": "Région entre Europe, Asie, Afrique."},{"q": "Qu'est-ce que le printemps arabe ?","r": "Révoltes de 2011."},{"q": "Qu'est-ce que l'Union africaine ?","r": "Organisation africaine."},{"q": "Qu'est-ce que le Commonwealth ?","r": "Association d'anciennes colonies britanniques."},{"q": "Qu'est-ce que le G7 ?","r": "Groupe des 7 pays."},{"q": "Qu'est-ce que le Conseil de sécurité de l'ONU ?","r": "Organe de paix."},{"q": "Qu'est-ce que la souveraineté ?","r": "Pouvoir suprême."},{"q": "Qu'est-ce que la diplomatie ?","r": "Relations entre États."},{"q": "Qu'est-ce qu'un traité ?","r": "Accord entre États."},{"q": "Qu'est-ce qu'une ambassade ?","r": "Représentation d'un État."},{"q": "Qu'est-ce qu'un conflit armé ?","r": "Guerre."},{"q": "Qu'est-ce que la paix ?","r": "Absence de guerre."},{"q": "Qu'est-ce que le terrorisme ?","r": "Violence politique."},{"q": "Qu'est-ce qu'un génocide ?","r": "Extermination d'un peuple."},{"q": "Qu'est-ce que la Shoah ?","r": "Génocide des Juifs."},{"q": "Qu'est-ce que le Rwanda ?","r": "Pays d'Afrique de l'Est."},{"q": "Qu'est-ce que le génocide rwandais ?","r": "1994."},{"q": "Qu'est-ce que la CPI ?","r": "Cour pénale internationale."},{"q": "Combien de membres à l'ONU ?","r": "193."},{"q": "Qu'est-ce que la FAO ?","r": "Alimentation et agriculture."},{"q": "Qu'est-ce que le HCR ?","r": "Haut-Commissariat pour les réfugiés."},{"q": "Qu'est-ce qu'une constitution ?","r": "Loi fondamentale."},{"q": "Qu'est-ce qu'une loi ?","r": "Règle votée par le Parlement."},{"q": "Qu'est-ce qu'un décret ?","r": "Acte du gouvernement."},{"q": "Qu'est-ce qu'un arrêté ?","r": "Acte administratif."},{"q": "Qu'est-ce que la jurisprudence ?","r": "Décisions des tribunaux."},{"q": "Qu'est-ce qu'un contrat ?","r": "Accord de volontés."},{"q": "Qu'est-ce qu'un contrat de travail ?","r": "Lien employeur/salarié."},{"q": "Qu'est-ce qu'un bail ?","r": "Contrat de location."},{"q": "Qu'est-ce qu'un acte notarié ?","r": "Acte authentique."},{"q": "Qu'est-ce qu'un testament ?","r": "Acte de succession."},{"q": "Qu'est-ce qu'un mariage civil ?","r": "Union à la mairie."},{"q": "Qu'est-ce qu'un PACS ?","r": "Pacte civil de solidarité."},{"q": "Qu'est-ce qu'un divorce ?","r": "Rupture du mariage."},{"q": "Qu'est-ce que la garde d'enfants ?","r": "Droit de vivre avec l'enfant."},{"q": "Qu'est-ce que la pension alimentaire ?","r": "Aide financière."},{"q": "Qu'est-ce qu'un casier judiciaire ?","r": "Registre des condamnations."},{"q": "Qu'est-ce qu'une plainte ?","r": "Signalement d'infraction."},{"q": "Qu'est-ce qu'un avocat ?","r": "Défenseur en justice."},{"q": "Qu'est-ce qu'un tribunal ?","r": "Lieu de justice."},{"q": "Qu'est-ce qu'un jury ?","r": "Citoyens juges."},{"q": "Qu'est-ce que la déflation ?","r": "Baisse des prix."},{"q": "Qu'est-ce que la croissance ?","r": "Augmentation de la production."},{"q": "Qu'est-ce que la récession ?","r": "Baisse de l'activité."},{"q": "Qu'est-ce que l'offre et la demande ?","r": "Mécanisme des prix."},{"q": "Qu'est-ce qu'un marché ?","r": "Lieu d'échange."},{"q": "Qu'est-ce qu'un monopole ?","r": "Un seul vendeur."},{"q": "Qu'est-ce qu'un oligopole ?","r": "Peu de vendeurs."},{"q": "Qu'est-ce que la concurrence ?","r": "Rivalité entre entreprises."},{"q": "Qu'est-ce que la Bourse ?","r": "Marché financier."},{"q": "Qu'est-ce qu'une action ?","r": "Part d'entreprise."},{"q": "Qu'est-ce qu'une obligation ?","r": "Titre de dette."},{"q": "Qu'est-ce qu'un dividende ?","r": "Part de bénéfice."},{"q": "Qu'est-ce qu'un intérêt ?","r": "Coût d'un emprunt."},{"q": "Qu'est-ce qu'un emprunt ?","r": "Argent prêté."},{"q": "Qu'est-ce qu'une hypothèque ?","r": "Garantie sur un bien."},{"q": "Qu'est-ce qu'une banque centrale ?","r": "Émet la monnaie."},{"q": "Qu'est-ce que la BCE ?","r": "Banque centrale européenne."},{"q": "Comment faire un budget ?","r": "Lister revenus et dépenses."},{"q": "Qu'est-ce que l'épargne ?","r": "Mettre de côté."},{"q": "Qu'est-ce qu'un livret A ?","r": "Épargne réglementée."},{"q": "Qu'est-ce qu'une assurance ?","r": "Protection contre risques."},{"q": "Qu'est-ce qu'une assurance-vie ?","r": "Placement et protection."},{"q": "Qu'est-ce que la retraite ?","r": "Fin d'activité."},{"q": "Qu'est-ce qu'une mutuelle ?","r": "Complémentaire santé."},{"q": "Qu'est-ce qu'un crédit ?","r": "Emprunt bancaire."},{"q": "Qu'est-ce qu'un découvert ?","r": "Solde négatif."},{"q": "Qu'est-ce que la vaccination ?","r": "Protection par vaccin."},{"q": "Qu'est-ce que l'immunité ?","r": "Défense de l'organisme."},{"q": "Qu'est-ce qu'un anticorps ?","r": "Protéine de défense."},{"q": "Qu'est-ce que l'eczéma ?","r": "Maladie de peau."},{"q": "Qu'est-ce que l'acné ?","r": "Maladie de peau."},{"q": "Qu'est-ce que le psoriasis ?","r": "Maladie de peau."},{"q": "Qu'est-ce que la migraine ?","r": "Mal de tête intense."},{"q": "Qu'est-ce que l'épilepsie ?","r": "Trouble neurologique."},{"q": "Qu'est-ce que la bipolarité ?","r": "Trouble de l'humeur."},{"q": "Qu'est-ce que la schizophrénie ?","r": "Trouble psychiatrique."},{"q": "Qu'est-ce que l'autisme ?","r": "Trouble du développement."},{"q": "Qu'est-ce que le TDAH ?","r": "Trouble de l'attention."},{"q": "Qu'est-ce que la trisomie 21 ?","r": "Anomalie chromosomique."},{"q": "Qu'est-ce que la mucoviscidose ?","r": "Maladie génétique."},{"q": "Qu'est-ce que la chimiothérapie ?","r": "Traitement du cancer."},{"q": "Qu'est-ce que la radiothérapie ?","r": "Traitement par rayons."},{"q": "Que faire en cas d'arrêt cardiaque ?","r": "Massage et défibrillateur."},{"q": "Qu'est-ce que la position latérale de sécurité ?","r": "Position pour respirer."},{"q": "Qu'est-ce que le bouche-à-bouche ?","r": "Ventilation artificielle."},{"q": "Quel numéro pour les urgences en Europe ?","r": "112."},{"q": "Que faire en cas de brûlure ?","r": "Refroidir à l'eau."},{"q": "Que faire en cas de noyade ?","r": "Sortir de l'eau, alerter."},{"q": "Que faire en cas de chute ?","r": "Ne pas bouger, alerter."},{"q": "Que faire en cas d'étouffement ?","r": "Manœuvre de Heimlich."},{"q": "Que faire en cas d'empoisonnement ?","r": "Appeler le centre antipoison."},{"q": "Que faire en cas de fracture ?","r": "Immobiliser, alerter."},{"q": "Qu'est-ce que le stress post-traumatique ?","r": "Trouble après trauma."},{"q": "Qu'est-ce que la thérapie cognitive ?","r": "Thérapie par pensées."},{"q": "Qu'est-ce que la psychanalyse ?","r": "Méthode freudienne."},{"q": "Qui est Freud ?","r": "Fondateur de la psychanalyse."},{"q": "Qui est Jung ?","r": "Psychiatre suisse."},{"q": "Qu'est-ce que l'inconscient ?","r": "Partie non consciente."},{"q": "Qu'est-ce qu'un complexe ?","r": "Ensemble de représentations."},{"q": "Qu'est-ce qu'un traumatisme ?","r": "Choc psychique."},{"q": "Qu'est-ce que le deuil ?","r": "Processus après une perte."},{"q": "Qu'est-ce que la confiance en soi ?","r": "Croire en ses capacités."},{"q": "Qu'est-ce que la motivation intrinsèque ?","r": "Vient de soi."},{"q": "Qu'est-ce que la motivation extrinsèque ?","r": "Vient de l'extérieur."},{"q": "Qu'est-ce qu'un objectif SMART ?","r": "Spécifique, mesurable, atteignable…"},{"q": "Qu'est-ce que la gestion du temps ?","r": "Organiser ses activités."},{"q": "Qu'est-ce que la matrice d'Eisenhower ?","r": "Prioriser urgent/important."},{"q": "Qu'est-ce que la procrastination ?","r": "Repousser au lendemain."},{"q": "Comment vaincre la procrastination ?","r": "Découper, commencer petit."},{"q": "Qu'est-ce que la méditation pleine conscience ?","r": "Être présent."},{"q": "Qu'est-ce que le yoga ?","r": "Discipline indienne."},{"q": "Qu'est-ce que le pilates ?","r": "Méthode de renforcement."},{"q": "Qu'est-ce que la sophrologie ?","r": "Méthode de relaxation."},{"q": "Qu'est-ce que la respiration profonde ?","r": "Respiration lente."},{"q": "Qu'est-ce que la cohérence cardiaque ?","r": "Respiration rythmée."},{"q": "Qu'est-ce que le sommeil ?","r": "Repos nocturne."},{"q": "Qu'est-ce que l'insomnie ?","r": "Difficulté à dormir."},{"q": "Qu'est-ce que le ronflement ?","r": "Bruit respiratoire."},{"q": "Qu'est-ce que l'apnée du sommeil ?","r": "Arrêts respiratoires."},{"q": "Qu'est-ce qu'un rêve ?","r": "Images pendant le sommeil."},{"q": "Qu'est-ce qu'un cauchemar ?","r": "Rêve angoissant."},{"q": "Qu'est-ce que le somnambulisme ?","r": "Agir en dormant."},{"q": "Qu'est-ce que la sieste ?","r": "Court sommeil."},{"q": "Combien de temps pour une sieste ?","r": "15-20 minutes."},{"q": "Qu'est-ce que la chronobiologie ?","r": "Rythmes biologiques."},{"q": "Qu'est-ce que l'horloge biologique ?","r": "Rythme interne."},{"q": "Qu'est-ce que la mélatonine ?","r": "Hormone du sommeil."},{"q": "Que faire avant de dormir ?","r": "Éviter les écrans."},{"q": "Qu'est-ce que l'éducation positive ?","r": "Éduquer avec bienveillance."},{"q": "Qu'est-ce qu'un parent ?","r": "Père ou mère."},{"q": "Qu'est-ce qu'un enfant ?","r": "Être en développement."},{"q": "Qu'est-ce que la grossesse ?","r": "Portage du bébé."},{"q": "Combien de temps dure une grossesse ?","r": "Environ 9 mois."},{"q": "Qu'est-ce que l'accouchement ?","r": "Naissance du bébé."},{"q": "Qu'est-ce que l'allaitement ?","r": "Nourrir au sein."},{"q": "Qu'est-ce que la diversification alimentaire ?","r": "Introduction d'aliments."},{"q": "Qu'est-ce que la propreté chez l'enfant ?","r": "Apprentissage du pot."},{"q": "Qu'est-ce que la crise d'adolescence ?","r": "Période de transition."},{"q": "Qu'est-ce que la puberté ?","r": "Transformation physique."},{"q": "Qu'est-ce que l'adolescence ?","r": "Période entre enfance et âge adulte."},{"q": "Qu'est-ce qu'un adulte ?","r": "Personne majeure."},{"q": "Qu'est-ce que la majorité en France ?","r": "18 ans."},{"q": "Qu'est-ce que la vieillesse ?","r": "Dernière étape de la vie."},{"q": "Qu'est-ce que la dépendance des aînés ?","r": "Perte d'autonomie."},{"q": "Qu'est-ce qu'un EHPAD ?","r": "Maison de retraite médicalisée."},{"q": "Qu'est-ce que le veuvage ?","r": "Perte du conjoint."},{"q": "Qu'est-ce que le coup de foudre ?","r": "Attirance immédiate."},{"q": "Qu'est-ce qu'un couple ?","r": "Deux personnes unies."},{"q": "Qu'est-ce que la fidélité ?","r": "Respect d'un engagement."},{"q": "Qu'est-ce que la rupture ?","r": "Fin d'une relation."},{"q": "Comment se remettre d'une rupture ?","r": "Temps, soutien, activités."},{"q": "Qu'est-ce que la communication dans le couple ?","r": "Parler et écouter."},{"q": "Qu'est-ce que le consentement ?","r": "Accord libre et éclairé."},{"q": "Qu'est-ce qu'un réseau social ?","r": "Plateforme de liens."},{"q": "Comment se faire des amis ?","r": "Être ouvert, partager."},{"q": "Comment entretenir une amitié ?","r": "Présence et attention."},{"q": "Qu'est-ce que la solitude ?","r": "État d'isolement."},{"q": "Comment gérer la solitude ?","r": "Activités, rencontres."},{"q": "Qu'est-ce que l'introversion ?","r": "Préférence pour le calme."},{"q": "Qu'est-ce que l'extraversion ?","r": "Orientation vers les autres."},{"q": "Qu'est-ce que l'écoute ?","r": "Prêter attention."},{"q": "Qu'est-ce qu'une destination de rêve ?","r": "Un lieu dont on rêve."},{"q": "Qu'est-ce qu'un vol direct ?","r": "Sans escale."},{"q": "Qu'est-ce qu'un décalage horaire ?","r": "Différence d'heure."},{"q": "Qu'est-ce que le jet lag ?","r": "Fatigue du voyage."},{"q": "Qu'est-ce qu'une auberge de jeunesse ?","r": "Hébergement économique."},{"q": "Qu'est-ce qu'un Airbnb ?","r": "Location entre particuliers."},{"q": "Qu'est-ce qu'un van ?","r": "Véhicule aménagé."},{"q": "Qu'est-ce que le tourisme durable ?","r": "Voyager responsable."},{"q": "Qu'est-ce que l'écotourisme ?","r": "Tourisme nature."},{"q": "Qu'est-ce qu'un safari ?","r": "Observation d'animaux."},{"q": "Qu'est-ce qu'une croisière ?","r": "Voyage en bateau."},{"q": "Qu'est-ce qu'un trek ?","r": "Randonnée longue."},{"q": "Qu'est-ce que le backpacking ?","r": "Voyage sac au dos."},{"q": "Qu'est-ce qu'un guide ?","r": "Accompagnateur."},{"q": "Qu'est-ce qu'une carte SIM locale ?","r": "Carte téléphonique du pays."},{"q": "Qu'est-ce qu'une assurance voyage ?","r": "Protection en voyage."},{"q": "Qu'est-ce que le change ?","r": "Conversion de monnaie."},{"q": "Qu'est-ce que la cuisine italienne ?","r": "Pâtes, pizza…"},{"q": "Qu'est-ce que la cuisine japonaise ?","r": "Sushi, ramen…"},{"q": "Qu'est-ce que la cuisine chinoise ?","r": "Riz, nouilles…"},{"q": "Qu'est-ce que la cuisine indienne ?","r": "Curry, épices…"},{"q": "Qu'est-ce que la cuisine mexicaine ?","r": "Tacos, guacamole…"},{"q": "Qu'est-ce que la cuisine libanaise ?","r": "Mezzés, houmous…"},{"q": "Qu'est-ce que la cuisine marocaine ?","r": "Tajine, couscous…"},{"q": "Qu'est-ce que la cuisine thaïlandaise ?","r": "Pad thaï, curry…"},{"q": "Qu'est-ce que la cuisine coréenne ?","r": "Kimchi, bibimbap…"},{"q": "Qu'est-ce que la cuisine grecque ?","r": "Moussaka, feta…"},{"q": "Qu'est-ce qu'un sushi ?","r": "Riz et poisson."},{"q": "Qu'est-ce qu'un taco ?","r": "Galette mexicaine."},{"q": "Qu'est-ce qu'un hamburger ?","r": "Pain, viande, légumes."},{"q": "Qu'est-ce qu'une paella ?","r": "Plat espagnol au riz."},{"q": "Qu'est-ce qu'un tajine ?","r": "Plat marocain mijoté."},{"q": "Qu'est-ce qu'un curry ?","r": "Plat épicé."},{"q": "Qu'est-ce qu'un ramen ?","r": "Soupe japonaise."},{"q": "Qu'est-ce qu'un kebab ?","r": "Viande grillée."},{"q": "Qu'est-ce qu'une crêpe ?","r": "Galette fine."},{"q": "Qu'est-ce qu'une gaufre ?","r": "Pâtisserie alvéolée."},{"q": "Qu'est-ce que la mode ?","r": "Style vestimentaire."},{"q": "Qu'est-ce qu'une tendance ?","r": "Style du moment."},{"q": "Qu'est-ce que le prêt-à-porter ?","r": "Vêtements standardisés."},{"q": "Qu'est-ce que la haute couture ?","r": "Création sur mesure."},{"q": "Qu'est-ce qu'un défilé ?","r": "Présentation de mode."},{"q": "Qui est Coco Chanel ?","r": "Créatrice française."},{"q": "Qui est Yves Saint Laurent ?","r": "Créateur français."},{"q": "Qui est Christian Dior ?","r": "Créateur français."},{"q": "Qu'est-ce qu'un jean ?","r": "Pantalon en denim."},{"q": "Qu'est-ce qu'une robe ?","r": "Vêtement d'une pièce."},{"q": "Qu'est-ce qu'une chemise ?","r": "Vêtement à boutons."},{"q": "Qu'est-ce qu'un t-shirt ?","r": "Haut à manches courtes."},{"q": "Qu'est-ce qu'un manteau ?","r": "Vêtement chaud."},{"q": "Qu'est-ce qu'une veste ?","r": "Vêtement d'extérieur."},{"q": "Qu'est-ce qu'un pull ?","r": "Haut tricoté."},{"q": "Qu'est-ce qu'une jupe ?","r": "Vêtement du bas."},{"q": "Qu'est-ce qu'un short ?","r": "Pantalon court."},{"q": "Qu'est-ce que des chaussures ?","r": "Protection des pieds."},{"q": "Qu'est-ce qu'un chapeau ?","r": "Couvre-chef."},{"q": "Qu'est-ce qu'un bijou ?","r": "Objet décoratif."},{"q": "Qu'est-ce qu'un soin du visage ?","r": "Routine beauté."},{"q": "Qu'est-ce qu'une crème hydratante ?","r": "Soin de peau."},{"q": "Qu'est-ce qu'un écran solaire ?","r": "Protection UV."},{"q": "Qu'est-ce qu'un masque de beauté ?","r": "Soin ciblé."},{"q": "Qu'est-ce qu'un gommage ?","r": "Exfoliation."},{"q": "Qu'est-ce qu'un sérum ?","r": "Soin concentré."},{"q": "Qu'est-ce qu'un fond de teint ?","r": "Maquillage du teint."},{"q": "Qu'est-ce qu'un rouge à lèvres ?","r": "Maquillage des lèvres."},{"q": "Qu'est-ce qu'un mascara ?","r": "Maquillage des cils."},{"q": "Qu'est-ce qu'un vernis à ongles ?","r": "Cosmétique des ongles."},{"q": "Qu'est-ce qu'un parfum ?","r": "Fragrance."},{"q": "Qu'est-ce que le déodorant ?","r": "Anti-odeur."},{"q": "Qu'est-ce qu'un shampoing ?","r": "Lavage des cheveux."},{"q": "Qu'est-ce qu'un après-shampoing ?","r": "Soin des cheveux."},{"q": "Qu'est-ce qu'un séchoir ?","r": "Appareil à sécher."},{"q": "Qu'est-ce qu'un lisseur ?","r": "Appareil à lisser."},{"q": "Qu'est-ce qu'un rasoir ?","r": "Appareil de rasage."},{"q": "Qu'est-ce qu'une épilation ?","r": "Retrait des poils."},{"q": "Qu'est-ce qu'un institut de beauté ?","r": "Lieu de soins."},{"q": "Qu'est-ce qu'un spa ?","r": "Lieu de détente."},{"q": "Qu'est-ce qu'un marteau ?","r": "Outil pour clouer."},{"q": "Qu'est-ce qu'un tournevis ?","r": "Outil pour visser."},{"q": "Qu'est-ce qu'une clé à molette ?","r": "Outil réglable."},{"q": "Qu'est-ce qu'une perceuse ?","r": "Outil pour percer."},{"q": "Qu'est-ce qu'une scie ?","r": "Outil pour couper."},{"q": "Qu'est-ce qu'une ponceuse ?","r": "Outil pour poncer."},{"q": "Qu'est-ce qu'un niveau ?","r": "Outil pour vérifier l'horizontalité."},{"q": "Qu'est-ce qu'un mètre ruban ?","r": "Outil de mesure."},{"q": "Qu'est-ce qu'un échafaudage ?","r": "Structure de travail en hauteur."},{"q": "Qu'est-ce qu'un casque de chantier ?","r": "Protection de la tête."},{"q": "Qu'est-ce qu'un gant de protection ?","r": "Protection des mains."},{"q": "Qu'est-ce que des lunettes de protection ?","r": "Protection des yeux."},{"q": "Qu'est-ce qu'une échelle ?","r": "Outil pour monter."},{"q": "Qu'est-ce qu'une brouette ?","r": "Outil de transport."},{"q": "Qu'est-ce qu'une bétonnière ?","r": "Machine à béton."},{"q": "Qu'est-ce que le plâtre ?","r": "Matériau de construction."},{"q": "Qu'est-ce que le ciment ?","r": "Liant hydraulique."},{"q": "Qu'est-ce que la peinture acrylique ?","r": "Peinture à l'eau."},{"q": "Qu'est-ce qu'un pinceau ?","r": "Outil de peinture."},{"q": "Qu'est-ce qu'un rouleau ?","r": "Outil de peinture large."},{"q": "Qu'est-ce que le jardinage ?","r": "Cultiver des plantes."},{"q": "Qu'est-ce qu'une bêche ?","r": "Outil de jardin."},{"q": "Qu'est-ce qu'une pelle ?","r": "Outil pour creuser."},{"q": "Qu'est-ce qu'un râteau ?","r": "Outil pour ramasser."},{"q": "Qu'est-ce qu'un arrosoir ?","r": "Récipient pour arroser."},{"q": "Qu'est-ce qu'un sécateur ?","r": "Ciseaux de jardin."},{"q": "Qu'est-ce qu'un terreau ?","r": "Terre de plantation."},{"q": "Qu'est-ce que l'engrais ?","r": "Nourrit les plantes."},{"q": "Qu'est-ce qu'un compost ?","r": "Matière organique décomposée."},{"q": "Qu'est-ce qu'un paillis ?","r": "Couverture du sol."},{"q": "Qu'est-ce qu'une graine ?","r": "Origine d'une plante."},{"q": "Qu'est-ce qu'un semis ?","r": "Plantation de graines."},{"q": "Qu'est-ce qu'un bouturage ?","r": "Reproduction par bouture."},{"q": "Qu'est-ce que la taille ?","r": "Couper des branches."},{"q": "Qu'est-ce qu'un potager ?","r": "Jardin de légumes."},{"q": "Qu'est-ce qu'un verger ?","r": "Jardin d'arbres fruitiers."},{"q": "Qu'est-ce qu'une serre ?","r": "Abri pour plantes."},{"q": "Qu'est-ce qu'une plante grasse ?","r": "Plante résistante à la sécheresse."},{"q": "Qu'est-ce qu'une plante carnivore ?","r": "Plante qui capture des insectes."},{"q": "Qu'est-ce qu'une plante médicinale ?","r": "Plante thérapeutique."},{"q": "Qu'est-ce qu'un chien ?","r": "Animal domestique fidèle."},{"q": "Qu'est-ce qu'un chat ?","r": "Animal domestique indépendant."},{"q": "Qu'est-ce qu'un lapin ?","r": "Petit mammifère."},{"q": "Qu'est-ce qu'un hamster ?","r": "Petit rongeur."},{"q": "Qu'est-ce qu'un poisson rouge ?","r": "Poisson d'aquarium."},{"q": "Qu'est-ce qu'un perroquet ?","r": "Oiseau coloré."},{"q": "Qu'est-ce qu'une perruche ?","r": "Petit perroquet."},{"q": "Qu'est-ce qu'un canari ?","r": "Oiseau chanteur."},{"q": "Qu'est-ce qu'une tortue ?","r": "Reptile à carapace."},{"q": "Qu'est-ce qu'un serpent ?","r": "Reptile sans pattes."},{"q": "Qu'est-ce qu'un lézard ?","r": "Reptile à pattes."},{"q": "Qu'est-ce qu'une grenouille ?","r": "Amphibien sauteur."},{"q": "Qu'est-ce qu'un cheval ?","r": "Grand mammifère."},{"q": "Qu'est-ce qu'un âne ?","r": "Mammifère proche du cheval."},{"q": "Qu'est-ce qu'une vache ?","r": "Mammifère laitier."},{"q": "Qu'est-ce qu'un mouton ?","r": "Mammifère à laine."},{"q": "Qu'est-ce qu'une chèvre ?","r": "Mammifère cornu."},{"q": "Qu'est-ce qu'un cochon ?","r": "Mammifère d'élevage."},{"q": "Qu'est-ce qu'une poule ?","r": "Oiseau de basse-cour."},{"q": "Qu'est-ce qu'un canard ?","r": "Oiseau aquatique."},{"q": "Qu'est-ce qu'un lion ?","r": "Grand félin."},{"q": "Qu'est-ce qu'un tigre ?","r": "Grand félin rayé."},{"q": "Qu'est-ce qu'un éléphant ?","r": "Grand mammifère à trompe."},{"q": "Qu'est-ce qu'une girafe ?","r": "Mammifère au long cou."},{"q": "Qu'est-ce qu'un rhinocéros ?","r": "Mammifère à corne."},{"q": "Qu'est-ce qu'un hippopotame ?","r": "Mammifère aquatique."},{"q": "Qu'est-ce qu'un zèbre ?","r": "Équidé rayé."},{"q": "Qu'est-ce qu'un singe ?","r": "Primate."},{"q": "Qu'est-ce qu'un gorille ?","r": "Grand primate."},{"q": "Qu'est-ce qu'un chimpanzé ?","r": "Primate proche de l'homme."},{"q": "Qu'est-ce qu'un loup ?","r": "Canidé sauvage."},{"q": "Qu'est-ce qu'un renard ?","r": "Canidé rusé."},{"q": "Qu'est-ce qu'un ours ?","r": "Grand mammifère."},{"q": "Qu'est-ce qu'un panda ?","r": "Ours noir et blanc."},{"q": "Qu'est-ce qu'un koala ?","r": "Marsupial australien."},{"q": "Qu'est-ce qu'un kangourou ?","r": "Marsupial sauteur."},{"q": "Qu'est-ce qu'une baleine ?","r": "Grand mammifère marin."},{"q": "Qu'est-ce qu'un dauphin ?","r": "Mammifère marin intelligent."},{"q": "Qu'est-ce qu'un requin ?","r": "Poisson prédateur."},{"q": "Qu'est-ce qu'une pieuvre ?","r": "Céphalopode à 8 bras."},{"q": "Qu'est-ce qu'une rose ?","r": "Fleur parfumée."},{"q": "Qu'est-ce qu'une tulipe ?","r": "Fleur printanière."},{"q": "Qu'est-ce qu'une marguerite ?","r": "Fleur blanche."},{"q": "Qu'est-ce qu'un tournesol ?","r": "Grande fleur jaune."},{"q": "Qu'est-ce qu'une orchidée ?","r": "Fleur élégante."},{"q": "Qu'est-ce qu'un cactus ?","r": "Plante épineuse."},{"q": "Qu'est-ce qu'un chêne ?","r": "Grand arbre."},{"q": "Qu'est-ce qu'un sapin ?","r": "Arbre à aiguilles."},{"q": "Qu'est-ce qu'un palmier ?","r": "Arbre tropical."},{"q": "Qu'est-ce qu'un baobab ?","r": "Arbre africain."},{"q": "Qu'est-ce qu'un olivier ?","r": "Arbre à olives."},{"q": "Qu'est-ce qu'un cerisier ?","r": "Arbre à cerises."},{"q": "Qu'est-ce qu'un pommier ?","r": "Arbre à pommes."},{"q": "Qu'est-ce qu'un rosier ?","r": "Arbuste à roses."},{"q": "Qu'est-ce qu'une fougère ?","r": "Plante verte."},{"q": "Qu'est-ce que le gazon ?","r": "Herbe de pelouse."},{"q": "Qu'est-ce que le lierre ?","r": "Plante grimpante."},{"q": "Qu'est-ce que le bambou ?","r": "Plante géante."},{"q": "Qu'est-ce qu'un bonsaï ?","r": "Arbre miniature."},{"q": "Qu'est-ce qu'une plante d'intérieur ?","r": "Plante de maison."},{"q": "Qu'est-ce que le tennis ?","r": "Sport de raquette."},{"q": "Qu'est-ce que le football ?","r": "Sport au ballon rond."},{"q": "Qu'est-ce que le rugby ?","r": "Sport au ballon ovale."},{"q": "Qu'est-ce que le basket ?","r": "Sport au panier."},{"q": "Qu'est-ce que le volley ?","r": "Sport au filet."},{"q": "Qu'est-ce que le handball ?","r": "Sport à la main."},{"q": "Qu'est-ce que le hockey ?","r": "Sport à crosse."},{"q": "Qu'est-ce que le golf ?","r": "Sport de précision."},{"q": "Qu'est-ce que l'équitation ?","r": "Sport à cheval."},{"q": "Qu'est-ce que l'escrime ?","r": "Sport d'épée."},{"q": "Qu'est-ce que l'aïkido ?","r": "Art martial japonais."},{"q": "Qu'est-ce que le MMA ?","r": "Arts martiaux mixtes."},{"q": "Qu'est-ce que le cyclisme ?","r": "Sport à vélo."},{"q": "Qu'est-ce que la course à pied ?","r": "Sport de course."},{"q": "Qu'est-ce que la randonnée ?","r": "Marche en nature."},{"q": "Qu'est-ce que l'escalade ?","r": "Grimper des parois."},{"q": "Qu'est-ce que le ski ?","r": "Sport de glisse."},{"q": "Qu'est-ce que le snowboard ?","r": "Sport de glisse."},{"q": "Qu'est-ce que le surf ?","r": "Sport de vague."},{"q": "Qu'est-ce que la voile ?","r": "Sport à voilier."},{"q": "Qu'est-ce que le kayak ?","r": "Sport de pagaie."},{"q": "Qu'est-ce que la plongée ?","r": "Exploration sous-marine."},{"q": "Qu'est-ce que le fitness ?","r": "Remise en forme."},{"q": "Qu'est-ce que la musculation ?","r": "Développement musculaire."},{"q": "Qu'est-ce qu'un jeu de société ?","r": "Jeu de table."},{"q": "Qu'est-ce que les échecs ?","r": "Jeu de stratégie."},{"q": "Qu'est-ce que le poker ?","r": "Jeu de cartes."},{"q": "Qu'est-ce que la belote ?","r": "Jeu de cartes français."},{"q": "Qu'est-ce que le tarot ?","r": "Jeu de cartes."},{"q": "Qu'est-ce que le scrabble ?","r": "Jeu de lettres."},{"q": "Qu'est-ce que le monopoly ?","r": "Jeu de gestion."},{"q": "Qu'est-ce que le puzzle ?","r": "Jeu d'assemblage."},{"q": "Qu'est-ce qu'un jeu de rôle ?","r": "Jeu d'incarnation."},{"q": "Qu'est-ce qu'un escape game ?","r": "Jeu d'énigmes."},{"q": "Qu'est-ce qu'un quiz ?","r": "Jeu de questions."},{"q": "Qu'est-ce qu'un karaoké ?","r": "Chanter sur playback."},{"q": "Qu'est-ce que la pétanque ?","r": "Jeu de boules."},{"q": "Qu'est-ce que le billard ?","r": "Jeu de boules sur table."},{"q": "Qu'est-ce que le flipper ?","r": "Jeu de billes."},{"q": "Qu'est-ce qu'un casino ?","r": "Lieu de jeux d'argent."},{"q": "Qu'est-ce que la loterie ?","r": "Jeu de hasard."},{"q": "Qu'est-ce qu'un puzzle 3D ?","r": "Assemblage en volume."},{"q": "Qu'est-ce qu'un leg o ?","r": "Jeu de construction."},{"q": "Qu'est-ce que les cartes Pokémon ?","r": "Cartes à collectionner."},{"q": "Que faire en cas de cambriolage ?","r": "Ne pas entrer, appeler la police."},{"q": "Que faire si on est suivi ?","r": "Aller dans un lieu public."},{"q": "Que faire face à un agresseur ?","r": "Rester calme, fuir si possible."},{"q": "Que faire si on se perd en montagne ?","r": "Rester sur place, appeler secours."},{"q": "Que faire en cas de séisme ?","r": "Se mettre sous une table."},{"q": "Que faire en cas d'ouragan ?","r": "Rester à l'abri."},{"q": "Que faire en cas de tempête ?","r": "Éviter les arbres."},{"q": "Que faire en cas de canicule ?","r": "Boire, rester au frais."},{"q": "Que faire en cas de grand froid ?","r": "Se couvrir, rester au chaud."},{"q": "Que faire en cas de coupure de courant ?","r": "Bougies, prudence."},{"q": "Que faire en cas de fuite de gaz ?","r": "Ne pas allumer, aérer, sortir."},{"q": "Que faire en cas de dégât des eaux ?","r": "Couper l'eau, prévenir."},{"q": "Que faire en cas d'incendie de forêt ?","r": "Évacuer."},{"q": "Que faire en cas d'accident de voiture ?","r": "Baliser, alerter."},{"q": "Que faire en cas de piqûre de guêpe ?","r": "Retirer le dard, refroidir."},{"q": "Que faire en cas de morsure de serpent ?","r": "Immobiliser, alerter."},{"q": "Que faire en cas de choc électrique ?","r": "Couper le courant."},{"q": "Que faire en cas d'ingestion de produit toxique ?","r": "Appeler le 15."},{"q": "Que faire en cas de malaise ?","r": "Allonger, alerter."},{"q": "Que faire en cas de crise d'asthme ?","r": "Utiliser l'inhalateur."},{"q": "Que faire en cas d'hypoglycémie ?","r": "Donner du sucre."},{"q": "Que faire en cas d'AVC ?","r": "Appeler le 15 immédiatement."},{"q": "Que faire en cas de crise d'épilepsie ?","r": "Protéger, ne pas bloquer."},{"q": "Que faire en cas de réaction allergique ?","r": "Utiliser un auto-injecteur."},{"q": "Que faire en cas de chute de vélo ?","r": "Vérifier les blessures."},{"q": "Que faire en cas de feu de cuisine ?","r": "Étouffer avec un couvercle."},{"q": "Que faire si un enfant s'étouffe ?","r": "Tapoter le dos."},{"q": "Que faire si un bébé ne respire plus ?","r": "Massage adapté, appeler secours."},{"q": "Que faire en cas de plaie profonde ?","r": "Comprimer, alerter."},{"q": "Qu'est-ce que la citoyenneté ?","r": "Être membre d'une nation."},{"q": "Qu'est-ce que le droit de vote ?","r": "Choisir ses représentants."},{"q": "Qu'est-ce que le bénévolat ?","r": "Aider gratuitement."},{"q": "Qu'est-ce qu'une association caritative ?","r": "Aide aux démunis."},{"q": "Qu'est-ce que l'économie solidaire ?","r": "Économie sociale."},{"q": "Qu'est-ce que l'égalité femmes-hommes ?","r": "Mêmes droits."},{"q": "Qu'est-ce que la parité ?","r": "Équilibre femmes-hommes."},{"q": "Qu'est-ce que le féminisme ?","r": "Mouvement pour l'égalité."},{"q": "Qu'est-ce que l'antiracisme ?","r": "Lutte contre le racisme."},{"q": "Qu'est-ce que l'accessibilité ?","r": "Faciliter pour tous."},{"q": "Qu'est-ce que le handicap ?","r": "Limitation d'activité."},{"q": "Qu'est-ce que l'autonomie ?","r": "Se gouverner soi-même."},{"q": "Qu'est-ce que la dignité ?","r": "Valeur intrinsèque."},{"q": "Qu'est-ce que le respect des droits humains ?","r": "Garantir les libertés."},{"q": "Qu'est-ce que la mémoire ?","r": "Capacité de se souvenir."},{"q": "Comment améliorer sa mémoire ?","r": "Dormir, réviser, associer."},{"q": "Qu'est-ce que la concentration ?","r": "Attention focalisée."},{"q": "Comment mieux se concentrer ?","r": "Éviter les distractions."},{"q": "Qu'est-ce que l'attention ?","r": "Focalisation mentale."},{"q": "Qu'est-ce que la curiosité ?","r": "Désir de savoir."},{"q": "Qu'est-ce que la créativité ?","r": "Produire des idées nouvelles."},{"q": "Qu'est-ce que l'imagination ?","r": "Créer des images mentales."},{"q": "Qu'est-ce que la réflexion ?","r": "Penser avec méthode."},{"q": "Qui a écrit « Père riche, Père pauvre » ?","r": "Robert Kiyosaki."},{"q": "Quelle est la différence entre un actif et un passif ?","r": "L'actif met de l'argent dans ta poche, le passif en retire."},{"q": "Qu'est-ce que le quadrant du cashflow ?","r": "Les 4 catégories : E (employé), S (indépendant), B (business), I (investisseur)."},{"q": "Que signifie E dans le quadrant de Kiyosaki ?","r": "Employé."},{"q": "Que signifie S dans le quadrant de Kiyosaki ?","r": "Indépendant ou travailleur autonome."},{"q": "Que signifie B dans le quadrant de Kiyosaki ?","r": "Propriétaire d'entreprise."},{"q": "Que signifie I dans le quadrant de Kiyosaki ?","r": "Investisseur."},{"q": "Que raconte l'histoire du pipeline ?","r": "Un homme porte des seaux, l'autre construit un pipeline : travail dur vs système."},{"q": "Qui sont Ed et Bill dans l'histoire ?","r": "Ed porte des seaux, Bill construit un pipeline."},{"q": "Qu'est-ce que le revenu passif ?","r": "Un revenu qui ne dépend pas du temps travaillé."},{"q": "Cite un exemple de revenu passif.","r": "Loyers, dividendes, royalties, intérêts."},{"q": "Qu'est-ce que la liberté financière ?","r": "Quand les revenus passifs couvrent les dépenses."},{"q": "Qu'est-ce que le rat race ?","r": "Le cycle travail-consommation-dettes."},{"q": "Qu'est-ce qu'un investissement ?","r": "Placer de l'argent pour le faire fructifier."},{"q": "Qu'est-ce que le patrimoine ?","r": "L'ensemble des biens d'une personne."},{"q": "Qu'est-ce que le flux de trésorerie ?","r": "Entrées et sorties d'argent."},{"q": "Qu'est-ce qu'un bilan ?","r": "Photo du patrimoine à un instant."},{"q": "Qu'est-ce qu'un compte de résultat ?","r": "Revenus et dépenses sur une période."},{"q": "Pourquoi épargner tôt ?","r": "Les intérêts composés travaillent pour toi."},{"q": "Qu'est-ce que les intérêts composés ?","r": "Intérêts qui produisent eux-mêmes des intérêts."},{"q": "Qu'est-ce qu'un ETF ?","r": "Un fonds coté en Bourse."},{"q": "Qu'est-ce qu'un fonds commun de placement ?","r": "Un panier géré de valeurs."},{"q": "Qu'est-ce qu'un portefeuille ?","r": "L'ensemble des investissements."},{"q": "Qu'est-ce que la diversification ?","r": "Répartir le risque."},{"q": "Qu'est-ce que le rendement ?","r": "Le gain d'un investissement."},{"q": "Qu'est-ce que le risque ?","r": "Probabilité de perte."},{"q": "Quel est le lien risque/rendement ?","r": "Plus de rendement implique souvent plus de risque."},{"q": "Qu'est-ce qu'un bull market ?","r": "Un marché haussier."},{"q": "Qu'est-ce qu'un bear market ?","r": "Un marché baissier."},{"q": "Qu'est-ce qu'un krach ?","r": "Une chute brutale des marchés."},{"q": "Qu'est-ce qu'une bulle spéculative ?","r": "Un prix déconnecté de la valeur réelle."},{"q": "Qu'est-ce que la valeur intrinsèque ?","r": "La vraie valeur d'un actif."},{"q": "Qu'est-ce que le PER ?","r": "Cours / bénéfice par action."},{"q": "Qui est Warren Buffett ?","r": "Un investisseur américain célèbre."},{"q": "Quelle est la stratégie de Buffett ?","r": "Acheter des entreprises solides à bon prix."},{"q": "Qu'est-ce que la valeur d'entreprise ?","r": "Valeur totale d'une société."},{"q": "Qu'est-ce qu'un entrepreneur ?","r": "Celui qui crée une entreprise."},{"q": "Qu'est-ce qu'un business plan ?","r": "Un plan d'affaires."},{"q": "Qu'est-ce qu'un modèle économique ?","r": "La façon dont l'entreprise gagne de l'argent."},{"q": "Qu'est-ce qu'un chiffre d'affaires ?","r": "Total des ventes."},{"q": "Qu'est-ce qu'un bénéfice ?","r": "Revenus moins dépenses."},{"q": "Qu'est-ce qu'une marge ?","r": "Différence entre prix de vente et coût."},{"q": "Qu'est-ce qu'un investisseur providentiel ?","r": "Un investisseur en start-up."},{"q": "Qu'est-ce qu'une start-up ?","r": "Jeune entreprise innovante."},{"q": "Qu'est-ce qu'un incubateur ?","r": "Structure d'accompagnement de start-up."},{"q": "Qu'est-ce qu'un accélérateur ?","r": "Programme court pour start-up."},{"q": "Qu'est-ce que le bootstrapping ?","r": "Financer son projet seul."},{"q": "Qu'est-ce que le crowdfunding ?","r": "Financement participatif."},{"q": "Qu'est-ce qu'une levée de fonds ?","r": "Collecter des capitaux."},{"q": "Qu'est-ce qu'un POC ?","r": "Proof of Concept."},{"q": "Qu'est-ce qu'un MVP ?","r": "Minimum Viable Product."},{"q": "Qu'est-ce que le product-market fit ?","r": "Adéquation produit-marché."},{"q": "Qu'est-ce que le scaling ?","r": "Passer à l'échelle."},{"q": "Qu'est-ce qu'un pivot ?","r": "Changer de modèle."},{"q": "Qu'est-ce qu'une franchise ?","r": "Utiliser une marque existante."},{"q": "Qu'est-ce qu'un brevet ?","r": "Protection d'une invention."},{"q": "Qu'est-ce qu'une marque déposée ?","r": "Protection d'un nom."},{"q": "Qu'est-ce qu'un copyright ?","r": "Droit d'auteur."},{"q": "Qu'est-ce qu'une société ?","r": "Une personne morale."},{"q": "Qu'est-ce qu'une SARL ?","r": "Société à responsabilité limitée."},{"q": "Qu'est-ce qu'une SAS ?","r": "Société par actions simplifiée."},{"q": "Qu'est-ce qu'une SA ?","r": "Société anonyme."},{"q": "Qu'est-ce qu'une auto-entreprise ?","r": "Statut simplifié."},{"q": "Qu'est-ce que le marketing ?","r": "L'art de vendre et séduire."},{"q": "Qu'est-ce que le marketing mix ?","r": "Les 4P : produit, prix, place, promotion."},{"q": "Qu'est-ce qu'un persona ?","r": "Portrait type du client."},{"q": "Qu'est-ce qu'une cible ?","r": "Le public visé."},{"q": "Qu'est-ce qu'un positionnement ?","r": "L'image voulue dans l'esprit."},{"q": "Qu'est-ce qu'une marque ?","r": "Un signe distinctif."},{"q": "Qu'est-ce que le branding ?","r": "Construire une marque."},{"q": "Qu'est-ce que le storytelling ?","r": "Raconter une histoire."},{"q": "Qu'est-ce qu'un slogan ?","r": "Phrase d'accroche."},{"q": "Qu'est-ce qu'un logo ?","r": "Symbole d'une marque."},{"q": "Qu'est-ce que la publicité ?","r": "Promouvoir un produit."},{"q": "Qu'est-ce que le content marketing ?","r": "Marketing par le contenu."},{"q": "Qu'est-ce que le growth hacking ?","r": "Croissance rapide par astuces."},{"q": "Qu'est-ce que le funnel ?","r": "Tunnel de conversion."},{"q": "Qu'est-ce que le lead ?","r": "Un prospect."},{"q": "Qu'est-ce que le CRM ?","r": "Gestion de la relation client."},{"q": "Qu'est-ce que la fidélisation ?","r": "Garder ses clients."},{"q": "Qu'est-ce que le churn ?","r": "Taux de perte de clients."},{"q": "Qu'est-ce que la vente ?","r": "Échange d'un bien contre de l'argent."},{"q": "Qu'est-ce que la prospection ?","r": "Chercher de nouveaux clients."},{"q": "Qu'est-ce qu'un argumentaire ?","r": "Arguments pour convaincre."},{"q": "Qu'est-ce qu'une objection ?","r": "Un frein à l'achat."},{"q": "Qu'est-ce que la closing ?","r": "Conclure la vente."},{"q": "Qu'est-ce que l'upsell ?","r": "Vendre plus cher."},{"q": "Qu'est-ce que le cross-sell ?","r": "Vendre en complément."},{"q": "Qu'est-ce que le B2B ?","r": "Business to Business."},{"q": "Qu'est-ce que le B2C ?","r": "Business to Consumer."},{"q": "Qu'est-ce que le management ?","r": "Gérer des équipes."},{"q": "Qu'est-ce qu'un manager ?","r": "Celui qui encadre."},{"q": "Qu'est-ce qu'un leader ?","r": "Celui qui inspire."},{"q": "Différence entre manager et leader ?","r": "Le manager gère, le leader inspire."},{"q": "Qu'est-ce que le coaching ?","r": "Accompagner vers un objectif."},{"q": "Qu'est-ce que le mentoring ?","r": "Transmettre son expérience."},{"q": "Qu'est-ce que la délégation ?","r": "Confier une tâche."},{"q": "Qu'est-ce qu'un KPI ?","r": "Indicateur clé de performance."},{"q": "Qu'est-ce qu'un OKR ?","r": "Objectifs et résultats clés."},{"q": "Qu'est-ce que le brainstorming ?","r": "Production collective d'idées."},{"q": "Qu'est-ce que le feedback 360 ?","r": "Retour de tous les collègues."},{"q": "Qu'est-ce que la gestion de projet ?","r": "Planifier et piloter."},{"q": "Qu'est-ce que la méthode agile ?","r": "Méthode itérative."},{"q": "Qu'est-ce que le Scrum ?","r": "Cadre agile."},{"q": "Qu'est-ce qu'un sprint ?","r": "Période courte de travail."},{"q": "Qu'est-ce que le Kanban ?","r": "Tableau visuel de tâches."},{"q": "Qu'est-ce que le Gantt ?","r": "Diagramme de planification."},{"q": "Qu'est-ce qu'un jalon ?","r": "Étape clé."},{"q": "Qu'est-ce qu'un risque projet ?","r": "Événement incertain."},{"q": "Qu'est-ce que la comptabilité ?","r": "Enregistrement des flux."},{"q": "Qu'est-ce qu'un débit ?","r": "Entrée comptable."},{"q": "Qu'est-ce qu'un actif immobilisé ?","r": "Bien durable."},{"q": "Qu'est-ce qu'un actif circulant ?","r": "Bien à court terme."},{"q": "Qu'est-ce qu'un passif ?","r": "Dettes et capitaux."},{"q": "Qu'est-ce que l'amortissement ?","r": "Étalement d'un coût."},{"q": "Qu'est-ce qu'une provision ?","r": "Réserve pour un risque."},{"q": "Qu'est-ce que l'IS ?","r": "Impôt sur les sociétés."},{"q": "Qu'est-ce que l'IR ?","r": "Impôt sur le revenu."},{"q": "Qu'est-ce qu'un audit ?","r": "Contrôle des comptes."},{"q": "Qu'est-ce que le contrôle de gestion ?","r": "Pilotage économique."},{"q": "Qu'est-ce qu'un budget prévisionnel ?","r": "Prévisions de dépenses."},{"q": "Qu'est-ce que l'investissement locatif ?","r": "Acheter pour louer."},{"q": "Qu'est-ce qu'un rendement locatif ?","r": "Loyer / prix d'achat."},{"q": "Qu'est-ce qu'un crédit immobilier ?","r": "Prêt pour acheter un bien."},{"q": "Qu'est-ce qu'un apport ?","r": "Part personnelle d'un achat."},{"q": "Qu'est-ce que le taux d'intérêt ?","r": "Coût d'un emprunt."},{"q": "Qu'est-ce qu'une assurance emprunteur ?","r": "Protection du prêt."},{"q": "Qu'est-ce qu'une plus-value ?","r": "Gain à la revente."},{"q": "Qu'est-ce qu'une SCI ?","r": "Société civile immobilière."},{"q": "Qu'est-ce qu'un bail commercial ?","r": "Location pour activité."},{"q": "Qu'est-ce qu'un syndic ?","r": "Gestion d'un immeuble."},{"q": "Qu'est-ce qu'une copropriété ?","r": "Immeuble partagé."},{"q": "Qu'est-ce que les charges de copropriété ?","r": "Frais communs."},{"q": "Qu'est-ce qu'un DPE ?","r": "Diagnostic de performance énergétique."},{"q": "Qu'est-ce que la loi Pinel ?","r": "Dispositif d'investissement locatif."},{"q": "Qu'est-ce que le LMNP ?","r": "Loueur meublé non professionnel."},{"q": "Qu'est-ce que la fiscalité ?","r": "Ensemble des impôts."},{"q": "Qu'est-ce qu'un impôt progressif ?","r": "Taux croissant avec le revenu."},{"q": "Qu'est-ce qu'un impôt proportionnel ?","r": "Taux fixe."},{"q": "Qu'est-ce qu'un impôt régressif ?","r": "Taux décroissant."},{"q": "Qu'est-ce que le bouclier fiscal ?","r": "Plafonnement des impôts."},{"q": "Qu'est-ce que la CSG ?","r": "Contribution sociale généralisée."},{"q": "Qu'est-ce que la CRDS ?","r": "Contribution au remboursement de la dette sociale."},{"q": "Qu'est-ce que la taxe foncière ?","r": "Impôt sur la propriété."},{"q": "Qu'est-ce que la taxe d'habitation ?","r": "Impôt sur l'occupation."},{"q": "Qu'est-ce que la cotisation sociale ?","r": "Prélèvement pour la protection sociale."},{"q": "Qu'est-ce que la fraude fiscale ?","r": "Évasion illégale d'impôts."},{"q": "Qu'est-ce que l'optimisation fiscale ?","r": "Réduire légalement ses impôts."},{"q": "Qu'est-ce que le paradis fiscal ?","r": "Pays à fiscalité faible."},{"q": "Qu'est-ce qu'une niche fiscale ?","r": "Avantage fiscal ciblé."},{"q": "Qu'est-ce que le prélèvement à la source ?","r": "Impôt prélevé directement."},{"q": "Qu'est-ce qu'un compte courant ?","r": "Compte de dépôt."},{"q": "Qu'est-ce qu'un compte épargne ?","r": "Compte rémunéré."},{"q": "Qu'est-ce qu'un découvert autorisé ?","r": "Solde négatif toléré."},{"q": "Qu'est-ce qu'un virement ?","r": "Transfert d'argent."},{"q": "Qu'est-ce qu'un prélèvement automatique ?","r": "Paiement récurrent."},{"q": "Qu'est-ce qu'une carte de crédit ?","r": "Carte à paiement différé."},{"q": "Qu'est-ce qu'une carte de débit ?","r": "Carte à paiement immédiat."},{"q": "Qu'est-ce que le sans contact ?","r": "Paiement par proximité."},{"q": "Qu'est-ce qu'un RIB ?","r": "Relevé d'identité bancaire."},{"q": "Qu'est-ce qu'un IBAN ?","r": "Identifiant bancaire international."},{"q": "Qu'est-ce que le SWIFT ?","r": "Réseau bancaire international."},{"q": "Quel est le rôle de la BCE ?","r": "Stabilité des prix."},{"q": "Qu'est-ce que le taux directeur ?","r": "Taux de la banque centrale."},{"q": "Qu'est-ce que la planche à billets ?","r": "Création monétaire."},{"q": "Qu'est-ce que la masse monétaire ?","r": "Quantité de monnaie en circulation."},{"q": "Qu'est-ce que la monnaie fiduciaire ?","r": "Billets et pièces."},{"q": "Qu'est-ce que la monnaie scripturale ?","r": "Écritures bancaires."},{"q": "Qu'est-ce que le pouvoir d'achat ?","r": "Ce qu'on peut acheter."},{"q": "Qu'est-ce que la dévaluation ?","r": "Baisse d'une monnaie."},{"q": "Qu'est-ce que le Forex ?","r": "Marché des devises."},{"q": "Qu'est-ce que le taux de change ?","r": "Valeur d'une monnaie."},{"q": "Qui a créé le Bitcoin ?","r": "Satoshi Nakamoto."},{"q": "Qu'est-ce qu'un NFT ?","r": "Jeton non fongible."},{"q": "Qu'est-ce qu'un smart contract ?","r": "Contrat automatique."},{"q": "Qu'est-ce que le Web3 ?","r": "Internet décentralisé."},{"q": "Qu'est-ce que le métavers ?","r": "Univers virtuel."},{"q": "Qu'est-ce que la DeFi ?","r": "Finance décentralisée."},{"q": "Qu'est-ce que le commerce international ?","r": "Échanges entre pays."},{"q": "Qu'est-ce que la balance commerciale ?","r": "Exportations moins importations."},{"q": "Qu'est-ce que le protectionnisme ?","r": "Protéger son économie."},{"q": "Qu'est-ce que le libre-échange ?","r": "Échanges sans barrières."},{"q": "Qu'est-ce que l'OMC ?","r": "Organisation mondiale du commerce."},{"q": "Qu'est-ce qu'une taxe douanière ?","r": "Taxe à l'importation."},{"q": "Qu'est-ce qu'un embargo ?","r": "Interdiction de commerce."},{"q": "Qu'est-ce que la délocalisation ?","r": "Déplacer la production."},{"q": "Qu'est-ce que la relocalisation ?","r": "Rapatrier la production."},{"q": "Qu'est-ce qu'une multinationale ?","r": "Entreprise présente partout."},{"q": "Qu'est-ce qu'une habitude ?","r": "Comportement répété."},{"q": "Combien de temps pour créer une habitude ?","r": "Environ 21 à 66 jours."},{"q": "Qu'est-ce que la loi de Pareto ?","r": "80 % des effets viennent de 20 % des causes."},{"q": "Qu'est-ce que la méthode Kaizen ?","r": "Amélioration continue."},{"q": "Qu'est-ce que la visualisation ?","r": "Se représenter un objectif."},{"q": "Qu'est-ce que les affirmations positives ?","r": "Phrases de motivation."},{"q": "Qu'est-ce que la zone de confort ?","r": "Espace familier."},{"q": "Qu'est-ce que la zone d'apprentissage ?","r": "Espace de progression."},{"q": "Qu'est-ce que la zone de panique ?","r": "Espace trop inconfortable."},{"q": "Qu'est-ce que l'état d'esprit de croissance ?","r": "Croire qu'on peut progresser."},{"q": "Qu'est-ce que l'état d'esprit fixe ?","r": "Croire que tout est figé."},{"q": "Qui a écrit « Réfléchissez et devenez riche » ?","r": "Napoleon Hill."},{"q": "Qui a écrit « Les 7 habitudes » ?","r": "Stephen Covey."},{"q": "Qui a écrit « L'art de la guerre » ?","r": "Sun Tzu."},{"q": "Qui a écrit « Le pouvoir du moment présent » ?","r": "Eckhart Tolle."},{"q": "Qui a écrit « L'homme en quête de sens » ?","r": "Viktor Frankl."},{"q": "Qu'est-ce que la loi de Parkinson ?","r": "Le travail s'étale sur le temps disponible."},{"q": "Qu'est-ce que la technique Pomodoro ?","r": "25 min de travail, 5 min de pause."},{"q": "Qu'est-ce que le time blocking ?","r": "Bloquer des plages horaires."},{"q": "Qu'est-ce que le deep work ?","r": "Travail concentré sans distraction."},{"q": "Qu'est-ce que le multitasking ?","r": "Faire plusieurs choses à la fois."},{"q": "Le multitasking est-il efficace ?","r": "Non, il réduit la performance."},{"q": "Qu'est-ce qu'un agenda ?","r": "Planification du temps."},{"q": "Qu'est-ce qu'une to-do list ?","r": "Liste de tâches."},{"q": "Qu'est-ce que le GTD ?","r": "Getting Things Done."},{"q": "Qu'est-ce que la règle des 2 minutes ?","r": "Si c'est faisable en 2 min, fais-le."},{"q": "Qu'est-ce que la méthode des 5 pourquoi ?","r": "Creuser la cause racine."},{"q": "Qu'est-ce que le diagramme d'Ishikawa ?","r": "Diagramme causes-effets."},{"q": "Qu'est-ce que la roue de Deming ?","r": "PDCA : plan-do-check-act."},{"q": "Qu'est-ce que le lean management ?","r": "Éliminer les gaspillages."},{"q": "Qu'est-ce que le Six Sigma ?","r": "Méthode qualité."},{"q": "Qu'est-ce que la qualité totale ?","r": "Qualité à tous les niveaux."},{"q": "Qu'est-ce que le leadership ?","r": "Capacité à guider."},{"q": "Quels sont les styles de leadership ?","r": "Autoritaire, démocratique, laisser-faire."},{"q": "Qu'est-ce que le leadership transformationnel ?","r": "Inspirer le changement."},{"q": "Qu'est-ce que le leadership serviteur ?","r": "Servir son équipe."},{"q": "Qu'est-ce que la vision ?","r": "Cap vers l'avenir."},{"q": "Qu'est-ce que la mission ?","r": "Raison d'être."},{"q": "Qu'est-ce que les valeurs ?","r": "Principes directeurs."},{"q": "Qu'est-ce que la culture d'entreprise ?","r": "ADN d'une organisation."},{"q": "Qu'est-ce que la résistance au changement ?","r": "Opposition à la nouveauté."},{"q": "Comment gérer le changement ?","r": "Communiquer, impliquer, former."},{"q": "Qu'est-ce qu'une négociation ?","r": "Discussion pour un accord."},{"q": "Qu'est-ce qu'une négociation gagnant-gagnant ?","r": "Les deux parties y gagnent."},{"q": "Qu'est-ce que le BATNA ?","r": "Meilleure alternative en cas d'échec."},{"q": "Qu'est-ce qu'un levier de négociation ?","r": "Moyen de pression."},{"q": "Qu'est-ce que la concession ?","r": "Renoncer à une demande."},{"q": "Qu'est-ce que le rapport de force ?","r": "Équilibre des pouvoirs."},{"q": "Qu'est-ce que le questionnement ouvert ?","r": "Question large."},{"q": "Qu'est-ce qu'une question fermée ?","r": "Réponse oui/non."},{"q": "Qu'est-ce que la prise de parole en public ?","r": "S'exprimer devant un groupe."},{"q": "Comment gérer le trac ?","r": "Respirer, se préparer, visualiser."},{"q": "Qu'est-ce que la rhétorique ?","r": "Art de persuader."},{"q": "Qu'est-ce que l'éloquence ?","r": "Art de bien parler."},{"q": "Qu'est-ce qu'un discours ?","r": "Texte oral."},{"q": "Qu'est-ce qu'un pitch ?","r": "Présentation courte et convaincante."},{"q": "Qu'est-ce qu'un elevator pitch ?","r": "Pitch en 30 secondes."},{"q": "Qu'est-ce que la gestuelle ?","r": "Communication par les gestes."},{"q": "Qu'est-ce que le langage corporel ?","r": "Communication non verbale."},{"q": "Qu'est-ce que la voix ?","r": "Outil de communication."},{"q": "Qu'est-ce que la rédaction ?","r": "Écrire un texte."},{"q": "Qu'est-ce qu'un plan ?","r": "Structure d'un texte."},{"q": "Qu'est-ce qu'une introduction ?","r": "Début d'un texte."},{"q": "Qu'est-ce qu'une conclusion ?","r": "Fin d'un texte."},{"q": "Qu'est-ce qu'un paragraphe ?","r": "Bloc d'idées."},{"q": "Qu'est-ce qu'une thèse ?","r": "Idée défendue."},{"q": "Qu'est-ce qu'une antithèse ?","r": "Idée opposée."},{"q": "Qu'est-ce qu'une synthèse ?","r": "Résumé structuré."},{"q": "Qu'est-ce qu'un résumé ?","r": "Version courte."},{"q": "Qu'est-ce qu'une citation ?","r": "Reprise exacte."},{"q": "Qu'est-ce qu'une bibliographie ?","r": "Liste de sources."},{"q": "Qu'est-ce qu'un plagiat ?","r": "Copier sans citer."},{"q": "Qu'est-ce que le style ?","r": "Manière d'écrire."},{"q": "Qu'est-ce qu'une ellipse ?","r": "Omettre des mots."},{"q": "Qu'est-ce qu'une anaphore ?","r": "Répétition en début."},{"q": "Qu'est-ce qu'un chiasme ?","r": "Structure croisée."},{"q": "Qu'est-ce qu'une gradation ?","r": "Progression d'intensité."},{"q": "Qu'est-ce qu'une allitération ?","r": "Répétition de consonnes."},{"q": "Qu'est-ce qu'une assonance ?","r": "Répétition de voyelles."},{"q": "Qu'est-ce qu'une nouvelle ?","r": "Récit court."},{"q": "Qu'est-ce qu'un essai ?","r": "Réflexion argumentée."},{"q": "Qu'est-ce qu'une autobiographie ?","r": "Récit de sa propre vie."},{"q": "Qu'est-ce qu'une biographie ?","r": "Récit d'une vie."},{"q": "Qu'est-ce qu'un conte de fées ?","r": "Récit merveilleux."},{"q": "Qu'est-ce qu'un polar ?","r": "Roman policier."},{"q": "Qu'est-ce qu'un thriller ?","r": "Roman à suspense."},{"q": "Qu'est-ce que la fantasy ?","r": "Genre merveilleux."},{"q": "Qu'est-ce que le roman historique ?","r": "Fiction dans l'Histoire."},{"q": "Qu'est-ce que la chick lit ?","r": "Roman pour femmes."},{"q": "Qu'est-ce que la littérature jeunesse ?","r": "Livres pour enfants."},{"q": "Qu'est-ce que la poésie ?","r": "Art du langage rythmé."},{"q": "Qu'est-ce que le théâtre ?","r": "Art de la scène."},{"q": "Qu'est-ce qu'un monologue ?","r": "Discours seul."},{"q": "Qu'est-ce qu'un dialogue ?","r": "Échange entre personnages."},{"q": "Qu'est-ce qu'une didascalie ?","r": "Indication scénique."},{"q": "Qu'est-ce qu'une mise en scène ?","r": "Organisation du spectacle."},{"q": "Qu'est-ce qu'un film ?","r": "Œuvre cinématographique."},{"q": "Qu'est-ce qu'un storyboard ?","r": "Découpage dessiné."},{"q": "Qu'est-ce qu'un plan-séquence ?","r": "Prise sans coupe."},{"q": "Qu'est-ce qu'un gros plan ?","r": "Cadrage serré."},{"q": "Qu'est-ce qu'un travelling ?","r": "Mouvement de caméra."},{"q": "Qu'est-ce qu'un zoom ?","r": "Modification focale."},{"q": "Qu'est-ce qu'un flash-back ?","r": "Retour en arrière."},{"q": "Qu'est-ce qu'un cliffhanger ?","r": "Fin en suspense."},{"q": "Qui a réalisé Inception ?","r": "Christopher Nolan."},{"q": "Qui a réalisé Pulp Fiction ?","r": "Quentin Tarantino."},{"q": "Qui a réalisé Avatar ?","r": "James Cameron."},{"q": "Qui a réalisé Intouchables ?","r": "Olivier Nakache et Éric Toledano."},{"q": "Qui a réalisé Le Fabuleux Destin d'Amélie Poulain ?","r": "Jean-Pierre Jeunet."},{"q": "Qui joue dans Le Parrain ?","r": "Marlon Brando et Al Pacino."},{"q": "Qui joue dans Matrix ?","r": "Keanu Reeves."},{"q": "Qui joue dans Le Seigneur des Anneaux ?","r": "Elijah Wood."},{"q": "Qui joue dans Harry Potter ?","r": "Daniel Radcliffe."},{"q": "Qui joue dans Star Wars ?","r": "Mark Hamill, Harrison Ford, Carrie Fisher."},{"q": "Qu'est-ce qu'un accord parfait ?","r": "Fondamentale, tierce, quinte."},{"q": "Qu'est-ce qu'une tonalité ?","r": "Centre d'une pièce."},{"q": "Qu'est-ce qu'une mesure ?","r": "Unité rythmique."},{"q": "Qu'est-ce qu'un tempo ?","r": "Vitesse d'exécution."},{"q": "Qu'est-ce que le blues ?","r": "Musique afro-américaine."},{"q": "Qu'est-ce que le rock ?","r": "Genre populaire."},{"q": "Qu'est-ce que la pop ?","r": "Musique populaire."},{"q": "Qu'est-ce que la techno ?","r": "Musique électronique."},{"q": "Qu'est-ce que la house ?","r": "Musique électronique."},{"q": "Qu'est-ce que le R&B ?","r": "Rhythm and Blues."},{"q": "Qu'est-ce que la soul ?","r": "Musique afro-américaine."},{"q": "Qu'est-ce que le fado ?","r": "Musique portugaise."},{"q": "Qu'est-ce que la variété ?","r": "Musique populaire francophone."},{"q": "Qu'est-ce que la musique classique ?","r": "Musique savante occidentale."},{"q": "Qu'est-ce que la peinture à l'huile ?","r": "Peinture à base d'huile."},{"q": "Qu'est-ce que l'aquarelle ?","r": "Peinture à l'eau."},{"q": "Qu'est-ce que la gouache ?","r": "Peinture opaque."},{"q": "Qu'est-ce que l'acrylique ?","r": "Peinture synthétique."},{"q": "Qu'est-ce que la fresque ?","r": "Peinture murale."},{"q": "Qu'est-ce que le street art ?","r": "Art urbain."},{"q": "Qui est Banksy ?","r": "Artiste de street art."},{"q": "Qu'est-ce que la sculpture ?","r": "Art du volume."},{"q": "Qu'est-ce que la céramique ?","r": "Art de la terre cuite."},{"q": "Qu'est-ce que la gravure ?","r": "Impression par incision."},{"q": "Qu'est-ce que la lithographie ?","r": "Impression sur pierre."},{"q": "Qu'est-ce que la sérigraphie ?","r": "Impression au pochoir."},{"q": "Qu'est-ce que le collage ?","r": "Assemblage de papiers."},{"q": "Qu'est-ce que l'installation ?","r": "Œuvre en espace."},{"q": "Qu'est-ce que la performance ?","r": "Œuvre en action."},{"q": "Qu'est-ce que l'art conceptuel ?","r": "L'idée prime."},{"q": "Qu'est-ce que l'impressionnisme ?","r": "Mouvement pictural."},{"q": "Qu'est-ce que le cubisme ?","r": "Décomposition des formes."},{"q": "Qu'est-ce que le surréalisme ?","r": "Exploration de l'inconscient."},{"q": "Qu'est-ce que l'art abstrait ?","r": "Sans représentation."},{"q": "Qu'est-ce que la photographie argentique ?","r": "Sur pellicule."},{"q": "Qu'est-ce que la photographie numérique ?","r": "Sur capteur."},{"q": "Qu'est-ce qu'un reflex ?","r": "Appareil à miroir."},{"q": "Qu'est-ce qu'un hybride ?","r": "Appareil sans miroir."},{"q": "Qu'est-ce qu'un compact ?","r": "Petit appareil."},{"q": "Qu'est-ce qu'un bridge ?","r": "Intermédiaire."},{"q": "Qu'est-ce qu'un drone photo ?","r": "Appareil volant."},{"q": "Qu'est-ce que la photo panoramique ?","r": "Vue large."},{"q": "Qu'est-ce que la photo HDR ?","r": "Haute dynamique."},{"q": "Qu'est-ce que la photo macro ?","r": "Très gros plan."},{"q": "Qu'est-ce que la photo de rue ?","r": "Scènes urbaines."},{"q": "Qu'est-ce que la photo de mode ?","r": "Vêtements et mannequins."},{"q": "Qu'est-ce que la photo de sport ?","r": "Action figée."},{"q": "Qu'est-ce que la photo animalière ?","r": "Animaux sauvages."},{"q": "Qu'est-ce que la photo sous-marine ?","r": "Sous l'eau."},{"q": "Qu'est-ce que la photo aérienne ?","r": "Depuis le ciel."},{"q": "Qu'est-ce que la photo d'architecture ?","r": "Bâtiments."},{"q": "Qu'est-ce que le design ?","r": "Conception esthétique et fonctionnelle."},{"q": "Qu'est-ce que le design graphique ?","r": "Communication visuelle."},{"q": "Qu'est-ce que le design industriel ?","r": "Conception de produits."},{"q": "Qu'est-ce que le design d'intérieur ?","r": "Aménagement d'espaces."},{"q": "Qu'est-ce que le UX design ?","r": "Expérience utilisateur."},{"q": "Qu'est-ce que le UI design ?","r": "Interface utilisateur."},{"q": "Qu'est-ce que la typographie ?","r": "Art des caractères."},{"q": "Qu'est-ce qu'une police ?","r": "Style de caractères."},{"q": "Qu'est-ce que la mise en page ?","r": "Organisation graphique."},{"q": "Qu'est-ce que la charte graphique ?","r": "Règles visuelles."},{"q": "Qu'est-ce que la palette de couleurs ?","r": "Ensemble de couleurs."},{"q": "Qu'est-ce que le contraste ?","r": "Opposition visuelle."},{"q": "Qu'est-ce que l'harmonie ?","r": "Équilibre visuel."},{"q": "Qu'est-ce que la symétrie ?","r": "Équilibre miroir."},{"q": "Qu'est-ce que l'asymétrie ?","r": "Déséquilibre voulu."},{"q": "Qu'est-ce que le point focal ?","r": "Centre d'attention."},{"q": "Qu'est-ce que la hiérarchie visuelle ?","r": "Ordre de lecture."},{"q": "Qu'est-ce que l'espace blanc ?","r": "Vide intentionnel."},{"q": "Qu'est-ce que le flat design ?","r": "Style minimaliste."},{"q": "Qu'est-ce que le rococo ?","r": "Style décoratif."},{"q": "Qu'est-ce que le néoclassique ?","r": "Retour à l'antique."},{"q": "Qu'est-ce que l'Art nouveau ?","r": "Style courbe."},{"q": "Qu'est-ce que l'Art déco ?","r": "Style géométrique."},{"q": "Qu'est-ce que le modernisme ?","r": "Rupture avec le passé."},{"q": "Qu'est-ce que le post-modernisme ?","r": "Critique du moderne."},{"q": "Qu'est-ce que le coton ?","r": "Fibre naturelle."},{"q": "Qu'est-ce que la laine ?","r": "Fibre animale."},{"q": "Qu'est-ce que la soie ?","r": "Fibre précieuse."},{"q": "Qu'est-ce que le lin ?","r": "Fibre végétale."},{"q": "Qu'est-ce que le cuir ?","r": "Peau tannée."},{"q": "Qu'est-ce que le polyester ?","r": "Fibre synthétique."},{"q": "Qu'est-ce que le nylon ?","r": "Fibre synthétique."},{"q": "Qu'est-ce que le denim ?","r": "Tissu de jean."},{"q": "Qu'est-ce que le velours ?","r": "Tissu doux."},{"q": "Qu'est-ce que la dentelle ?","r": "Tissu ajouré."},{"q": "Qu'est-ce que le tartan ?","r": "Tissu écossais."},{"q": "Qu'est-ce que le kimono ?","r": "Vêtement japonais."},{"q": "Qu'est-ce que le sari ?","r": "Vêtement indien."},{"q": "Qu'est-ce que le boubou ?","r": "Vêtement africain."},{"q": "Qu'est-ce que le dashiki ?","r": "Tunique africaine."},{"q": "Qu'est-ce que le caftan ?","r": "Vêtement long."},{"q": "Qu'est-ce que le poncho ?","r": "Manteau sud-américain."},{"q": "Qu'est-ce qu'un kilt ?","r": "Jupe écossaise."},{"q": "Qu'est-ce qu'un sombrero ?","r": "Chapeau mexicain."},{"q": "Qu'est-ce qu'une babouche ?","r": "Chaussure marocaine."},{"q": "Qu'est-ce que la gastronomie ?","r": "Art de la bonne cuisine."},{"q": "Qu'est-ce qu'un chef étoilé ?","r": "Chef récompensé."},{"q": "Qu'est-ce qu'un guide Michelin ?","r": "Guide gastronomique."},{"q": "Combien d'étoiles au maximum ?","r": "3."},{"q": "Qu'est-ce que la cuisine moléculaire ?","r": "Cuisine scientifique."},{"q": "Qu'est-ce que la cuisine fusion ?","r": "Mélange de cultures."},{"q": "Qu'est-ce qu'un bistrot ?","r": "Petit restaurant."},{"q": "Qu'est-ce qu'une brasserie ?","r": "Restaurant traditionnel."},{"q": "Qu'est-ce qu'un gastro ?","r": "Restaurant haut de gamme."},{"q": "Qu'est-ce qu'un food truck ?","r": "Cuisine mobile."},{"q": "Qu'est-ce que la cuisine vegan ?","r": "Sans produits animaux."},{"q": "Qu'est-ce que la cuisine végétarienne ?","r": "Sans viande."},{"q": "Qu'est-ce que la cuisine sans gluten ?","r": "Sans gluten."},{"q": "Qu'est-ce que la cuisine bio ?","r": "Ingrédients biologiques."},{"q": "Qu'est-ce que le locavorisme ?","r": "Manger local."},{"q": "Qu'est-ce que le slow food ?","r": "Manger autrement."},{"q": "Qu'est-ce que le fast-food ?","r": "Restauration rapide."},{"q": "Qu'est-ce qu'un buffet ?","r": "Repas libre-service."},{"q": "Qu'est-ce qu'un banquet ?","r": "Grand repas."},{"q": "Qu'est-ce qu'un apéritif ?","r": "Repas léger avant le plat."},{"q": "Qu'est-ce que le sushi ?","r": "Riz et poisson cru."},{"q": "Qu'est-ce que le sashimi ?","r": "Poisson cru."},{"q": "Qu'est-ce que le maki ?","r": "Rouleau japonais."},{"q": "Qu'est-ce que le california roll ?","r": "Maki occidental."},{"q": "Qu'est-ce que le ramen ?","r": "Soupe japonaise."},{"q": "Qu'est-ce que le pho ?","r": "Soupe vietnamienne."},{"q": "Qu'est-ce que le pad thaï ?","r": "Nouilles thaïlandaises."},{"q": "Qu'est-ce que le curry vert ?","r": "Plat thaï."},{"q": "Qu'est-ce que le biryani ?","r": "Riz épicé indien."},{"q": "Qu'est-ce que le tandoori ?","r": "Poulet épicé indien."},{"q": "Qu'est-ce qu'un macaron ?","r": "Pâtisserie française."},{"q": "Qu'est-ce qu'un éclair ?","r": "Pâtisserie au chocolat."},{"q": "Qu'est-ce qu'un mille-feuille ?","r": "Pâtisserie feuilletée."},{"q": "Qu'est-ce qu'un croissant ?","r": "Viennoiserie."},{"q": "Qu'est-ce qu'un pain au chocolat ?","r": "Viennoiserie."},{"q": "Qu'est-ce qu'une brioche ?","r": "Pain sucré."},{"q": "Qu'est-ce qu'un chausson aux pommes ?","r": "Pâtisserie."},{"q": "Qu'est-ce qu'une tarte tatin ?","r": "Tarte renversée."},{"q": "Qu'est-ce qu'une crème brûlée ?","r": "Dessert caramélisé."},{"q": "Qu'est-ce qu'une mousse au chocolat ?","r": "Dessert aérien."},{"q": "Qu'est-ce que le vin blanc ?","r": "Vin de raisins blancs."},{"q": "Qu'est-ce que le rosé ?","r": "Vin intermédiaire."},{"q": "Qu'est-ce que le cidre ?","r": "Boisson à base de pommes."},{"q": "Qu'est-ce que la bière ?","r": "Boisson fermentée."},{"q": "Qu'est-ce qu'un cocktail ?","r": "Mélange de boissons."},{"q": "Qu'est-ce que le mojito ?","r": "Cocktail cubain."},{"q": "Qu'est-ce qu'un gin tonic ?","r": "Cocktail au gin."},{"q": "Qu'est-ce que le whisky ?","r": "Alcool de céréales."},{"q": "Qu'est-ce qu'un antioxydant ?","r": "Protège les cellules."},{"q": "Qu'est-ce qu'une vitamine ?","r": "Nutriment essentiel."},{"q": "Qu'est-ce qu'un oligo-élément ?","r": "Minéral en petite quantité."},{"q": "Qu'est-ce qu'un glucide ?","r": "Sucre ou amidon."},{"q": "Qu'est-ce qu'un lipide ?","r": "Matière grasse."},{"q": "Qu'est-ce qu'une calorie ?","r": "Unité d'énergie."},{"q": "Qu'est-ce que le métabolisme ?","r": "Ensemble des réactions du corps."},{"q": "Qu'est-ce qu'un régime ?","r": "Alimentation contrôlée."},{"q": "Qu'est-ce que le jeûne intermittent ?","r": "Alternance jeûne/repos."},{"q": "Qu'est-ce que le cardio ?","r": "Entraînement du cœur."},{"q": "Qu'est-ce que le HIIT ?","r": "Entraînement intensif fractionné."},{"q": "Qu'est-ce que le crossfit ?","r": "Entraînement fonctionnel intense."},{"q": "Qu'est-ce que le stretching ?","r": "Étirements."},{"q": "Qu'est-ce que l'endurance ?","r": "Capacité à durer."},{"q": "Qu'est-ce que la force ?","r": "Capacité à soulever."},{"q": "Qu'est-ce que la souplesse ?","r": "Amplitude articulaire."},{"q": "Qu'est-ce que la coordination ?","r": "Harmonie des mouvements."},{"q": "Qu'est-ce que les JO ?","r": "Jeux olympiques."},{"q": "Combien d'anneaux olympiques ?","r": "5."},{"q": "Que symbolisent les anneaux ?","r": "Les 5 continents."},{"q": "Quelle est la devise olympique ?","r": "Plus vite, plus haut, plus fort."},{"q": "Quand ont eu lieu les premiers JO modernes ?","r": "1896 à Athènes."},{"q": "Où ont eu lieu les JO 2021 ?","r": "Tokyo."},{"q": "Où auront lieu les JO 2028 ?","r": "Los Angeles."},{"q": "Qu'est-ce qu'un ballon d'or ?","r": "Récompense de football."},{"q": "Combien de joueurs sur le terrain au foot ?","r": "22 (11 par équipe)."},{"q": "Combien de temps un match de foot ?","r": "90 minutes + arrêts."},{"q": "Qu'est-ce que le VAR ?","r": "Assistance vidéo."},{"q": "Qu'est-ce que la Ligue des champions ?","r": "Compétition européenne."},{"q": "Quel club a gagné le plus de LDC ?","r": "Real Madrid."},{"q": "Qui est le meilleur buteur de l'histoire du foot ?","r": "Cristiano Ronaldo."},{"q": "Quel pays a gagné le plus de Coupes du monde ?","r": "Le Brésil (5)."},{"q": "Qu'est-ce qu'un Grand Chelem au tennis ?","r": "4 tournois majeurs."},{"q": "Cite les 4 Grands Chelems.","r": "Open d'Australie, Roland-Garros, Wimbledon, US Open."},{"q": "Sur quelle surface joue-t-on à Roland-Garros ?","r": "Terre battue."},{"q": "Sur quelle surface joue-t-on à Wimbledon ?","r": "Gazon."},{"q": "Qui a gagné le plus de Grands Chelems ?","r": "Novak Djokovic."},{"q": "Qu'est-ce qu'un ace ?","r": "Service gagnant direct."},{"q": "Qu'est-ce qu'un let ?","r": "Service à rejouer."},{"q": "Combien de sets à Wimbledon hommes ?","r": "3 sur 5."},{"q": "Qu'est-ce que la Coupe Davis ?","r": "Compétition par nations."},{"q": "Qu'est-ce que la Fed Cup ?","r": "Compétition féminine par nations."},{"q": "Qu'est-ce qu'un moteur thermique ?","r": "Moteur à combustion."},{"q": "Qu'est-ce qu'un moteur électrique ?","r": "Moteur à batterie."},{"q": "Qu'est-ce qu'un moteur hybride ?","r": "Thermique + électrique."},{"q": "Qu'est-ce qu'un SUV ?","r": "Sport Utility Vehicle."},{"q": "Qu'est-ce qu'un cabriolet ?","r": "Voiture décapotable."},{"q": "Qu'est-ce qu'un break ?","r": "Voiture familiale."},{"q": "Qu'est-ce qu'une berline ?","r": "Voiture à coffre."},{"q": "Qu'est-ce qu'une citadine ?","r": "Petite voiture."},{"q": "Qu'est-ce qu'un pick-up ?","r": "Voiture à benne."},{"q": "Qu'est-ce qu'une hypercar ?","r": "Voiture ultra-performante."},{"q": "Qui a fondé Tesla ?","r": "Martin Eberhard et Marc Tarpenning (Elon Musk rejoint plus tard)."},{"q": "Qui a fondé Ford ?","r": "Henry Ford."},{"q": "Qui a fondé Ferrari ?","r": "Enzo Ferrari."},{"q": "Qui a fondé Renault ?","r": "Louis Renault."},{"q": "Qui a fondé Peugeot ?","r": "La famille Peugeot."},{"q": "Qui a fondé Toyota ?","r": "Kiichiro Toyoda."},{"q": "Quel pays est le 1er producteur d'auto ?","r": "La Chine."},{"q": "Combien d'écuries en F1 ?","r": "10."},{"q": "Quel pilote a le plus de titres F1 ?","r": "Michael Schumacher et Lewis Hamilton (7)."},{"q": "Qu'est-ce qu'un avion de ligne ?","r": "Avion commercial."},{"q": "Qu'est-ce qu'un jet privé ?","r": "Avion personnel."},{"q": "Qu'est-ce qu'un biplan ?","r": "Avion à 2 ailes."},{"q": "Qu'est-ce qu'un planeur ?","r": "Avion sans moteur."},{"q": "Qu'est-ce qu'un aéroport ?","r": "Lieu de décollage."},{"q": "Qu'est-ce qu'une piste ?","r": "Voie de décollage."},{"q": "Qu'est-ce que le contrôle aérien ?","r": "Gestion du trafic."},{"q": "Qu'est-ce que le décalage horaire ?","r": "Différence d'heure."},{"q": "Qui a inventé l'avion ?","r": "Les frères Wright."},{"q": "Qui a traversé l'Atlantique en avion ?","r": "Charles Lindbergh."},{"q": "Qu'est-ce que Concorde ?","r": "Avion supersonique."},{"q": "Qu'est-ce qu'un Boeing ?","r": "Constructeur aéronautique américain."},{"q": "Qu'est-ce qu'Airbus ?","r": "Constructeur aéronautique européen."},{"q": "Quel est le plus gros avion ?","r": "Antonov An-225 (détruit) / Airbus A380."},{"q": "Quelle est la compagnie la plus ancienne ?","r": "KLM."},{"q": "Qu'est-ce que le cockpit ?","r": "Cabine de pilotage."},{"q": "Qu'est-ce que le train d'atterrissage ?","r": "Roues de l'avion."},{"q": "Qu'est-ce que la turbulence ?","r": "Secousse en vol."},{"q": "Qu'est-ce qu'un catamaran ?","r": "Bateau à 2 coques."},{"q": "Qu'est-ce qu'un trimaran ?","r": "Bateau à 3 coques."},{"q": "Qu'est-ce qu'un cargo ?","r": "Navire de marchandises."},{"q": "Qu'est-ce qu'un pétrolier ?","r": "Navire transportant du pétrole."},{"q": "Qu'est-ce qu'un sous-marin ?","r": "Navire submersible."},{"q": "Qu'est-ce qu'un porte-conteneurs ?","r": "Navire à conteneurs."},{"q": "Qu'est-ce qu'un ferry ?","r": "Navire transbordeur."},{"q": "Qu'est-ce qu'un yacht ?","r": "Bateau de plaisance."},{"q": "Qui a traversé l'Atlantique à la voile en premier ?","r": "Christophe Colomb."},{"q": "Qu'est-ce que le Vendée Globe ?","r": "Course à la voile en solitaire."},{"q": "Qu'est-ce que la Route du Rhum ?","r": "Course transatlantique."},{"q": "Qu'est-ce que l'America's Cup ?","r": "Compétition de voile."},{"q": "Qu'est-ce qu'un nœud marin ?","r": "Unité de vitesse (1,852 km/h)."},{"q": "Qu'est-ce que le bâbord ?","r": "Côté gauche du navire."},{"q": "Qu'est-ce que le tribord ?","r": "Côté droit du navire."},{"q": "Qu'est-ce que la proue ?","r": "Avant du bateau."},{"q": "Qu'est-ce que la poupe ?","r": "Arrière du bateau."},{"q": "Qu'est-ce que la quille ?","r": "Partie sous le bateau."},{"q": "Qui a marché sur la Lune en premier ?","r": "Neil Armstrong."},{"q": "En quelle année ?","r": "1969."},{"q": "Quelle est la mission Apollo 11 ?","r": "Premier alunissage."},{"q": "Qui a été le 2e homme sur la Lune ?","r": "Buzz Aldrin."},{"q": "Quelle agence spatiale américaine ?","r": "NASA."},{"q": "Quelle agence spatiale européenne ?","r": "ESA."},{"q": "Quelle agence spatiale russe ?","r": "Roscosmos."},{"q": "Quelle agence spatiale chinoise ?","r": "CNSA."},{"q": "Qu'est-ce que l'ISS ?","r": "Station spatiale internationale."},{"q": "Qu'est-ce que SpaceX ?","r": "Entreprise d'Elon Musk."},{"q": "Qu'est-ce qu'une fusée ?","r": "Véhicule spatial."},{"q": "Qu'est-ce qu'un lanceur ?","r": "Fusée porteuse."},{"q": "Qu'est-ce qu'une navette spatiale ?","r": "Véhicule réutilisable."},{"q": "Qu'est-ce que le télescope Hubble ?","r": "Télescope spatial."},{"q": "Qu'est-ce que le télescope James Webb ?","r": "Télescope infrarouge."},{"q": "Qu'est-ce qu'une sonde spatiale ?","r": "Véhicule d'exploration."},{"q": "Qu'est-ce qu'un rover ?","r": "Robot explorateur."},{"q": "Quel rover explore Mars ?","r": "Perseverance."},{"q": "Qu'est-ce que Voyager 1 ?","r": "Sonde sortie du système solaire."},{"q": "Qu'est-ce qu'un modèle de langage ?","r": "IA qui génère du texte."},{"q": "Qu'est-ce que GPT ?","r": "Generative Pre-trained Transformer."},{"q": "Qu'est-ce que le NLP ?","r": "Traitement du langage naturel."},{"q": "Qu'est-ce que la vision par ordinateur ?","r": "Analyse d'images par IA."},{"q": "Qu'est-ce qu'un dataset ?","r": "Ensemble de données."},{"q": "Qu'est-ce que l'entraînement ?","r": "Apprentissage d'un modèle."},{"q": "Qu'est-ce qu'une époque ?","r": "Un cycle d'entraînement."},{"q": "Qu'est-ce que le surapprentissage ?","r": "Trop apprendre, mal généraliser."},{"q": "Qu'est-ce qu'une IA faible ?","r": "Spécialisée."},{"q": "Qu'est-ce qu'une IA forte ?","r": "Générale (hypothétique)."},{"q": "Qu'est-ce que le test de Turing ?","r": "Test d'intelligence."},{"q": "Qui a proposé le test de Turing ?","r": "Alan Turing."},{"q": "Qu'est-ce que l'éthique de l'IA ?","r": "Règles morales de l'IA."},{"q": "Qu'est-ce que le biais algorithmique ?","r": "Préjugé dans les données."},{"q": "Qu'est-ce que l'IA générative ?","r": "IA qui crée du contenu."},{"q": "Qu'est-ce qu'un prompt ?","r": "Instruction donnée à l'IA."},{"q": "Qu'est-ce que le fine-tuning ?","r": "Ajustement d'un modèle."},{"q": "Qu'est-ce que le transfer learning ?","r": "Réutiliser un modèle."},{"q": "Qu'est-ce qu'un hacker ?","r": "Spécialiste informatique."},{"q": "Qu'est-ce qu'un hacker éthique ?","r": "Hacker légal."},{"q": "Qu'est-ce qu'un white hat ?","r": "Hacker éthique."},{"q": "Qu'est-ce qu'un black hat ?","r": "Hacker malveillant."},{"q": "Qu'est-ce qu'un DDoS ?","r": "Attaque par saturation."},{"q": "Qu'est-ce qu'un malware ?","r": "Logiciel malveillant."},{"q": "Qu'est-ce qu'un keylogger ?","r": "Enregistre les touches."},{"q": "Qu'est-ce qu'une faille ?","r": "Vulnérabilité."},{"q": "Qu'est-ce qu'un pentest ?","r": "Test d'intrusion."},{"q": "Qu'est-ce que le hachage ?","r": "Transformation irréversible."},{"q": "Qu'est-ce que l'authentification ?","r": "Vérifier l'identité."},{"q": "Qu'est-ce qu'un certificat SSL ?","r": "Preuve de sécurité."},{"q": "Qu'est-ce qu'un proxy ?","r": "Intermédiaire réseau."},{"q": "Qu'est-ce qu'un firewall ?","r": "Pare-feu."},{"q": "Qu'est-ce qu'une DMZ ?","r": "Zone démilitarisée."},{"q": "Qu'est-ce qu'un SOC ?","r": "Centre de sécurité."},{"q": "Qu'est-ce qu'un protocole ?","r": "Règle de communication."},{"q": "Qu'est-ce que TCP/IP ?","r": "Suite de protocoles."},{"q": "Qu'est-ce qu'une adresse IP ?","r": "Identifiant réseau."},{"q": "Qu'est-ce qu'un DNS ?","r": "Traduit les noms en IP."},{"q": "Qu'est-ce qu'un routeur ?","r": "Dirige les données."},{"q": "Qu'est-ce qu'un switch ?","r": "Commutateur réseau."},{"q": "Qu'est-ce qu'un modem ?","r": "Modulateur-démodulateur."},{"q": "Qu'est-ce que la fibre ?","r": "Câble optique."},{"q": "Qu'est-ce que la 4G ?","r": "4e génération mobile."},{"q": "Qu'est-ce que le NFC ?","r": "Communication de proximité."},{"q": "Qu'est-ce que la RFID ?","r": "Identification par radio."},{"q": "Qu'est-ce qu'un satellite géostationnaire ?","r": "Fixe au-dessus de la Terre."},{"q": "Qu'est-ce qu'un câble sous-marin ?","r": "Câble de données."},{"q": "Qu'est-ce que la latence ?","r": "Délai de transmission."},{"q": "Qu'est-ce que la bande passante ?","r": "Débit de données."},{"q": "Qu'est-ce qu'un cloud ?","r": "Serveurs distants."},{"q": "Qu'est-ce que l'edge computing ?","r": "Calcul en périphérie."},{"q": "Qu'est-ce qu'un transistor ?","r": "Composant électronique."},{"q": "Qu'est-ce qu'un circuit intégré ?","r": "Puce électronique."},{"q": "Qu'est-ce qu'un microprocesseur ?","r": "Cerveau d'un ordinateur."},{"q": "Qu'est-ce qu'un GPU ?","r": "Processeur graphique."},{"q": "Qu'est-ce qu'un CPU ?","r": "Processeur central."},{"q": "Qu'est-ce que la loi de Moore ?","r": "Doublement des transistors tous les 2 ans."},{"q": "Qu'est-ce qu'un nanomètre ?","r": "Un milliardième de mètre."},{"q": "Qu'est-ce qu'un capteur ?","r": "Mesure une grandeur."},{"q": "Qu'est-ce qu'un actionneur ?","r": "Produit un mouvement."},{"q": "Qu'est-ce que l'Arduino ?","r": "Carte électronique programmable."},{"q": "Qu'est-ce qu'un bras robotisé ?","r": "Bras mécanique."},{"q": "Qu'est-ce qu'un robot humanoïde ?","r": "Robot à forme humaine."},{"q": "Qu'est-ce qu'un robot collaboratif ?","r": "Cobot."},{"q": "Qu'est-ce que la cobotique ?","r": "Robotique collaborative."},{"q": "Qu'est-ce que l'IA embarquée ?","r": "IA sur appareil."},{"q": "Qu'est-ce que la domotique ?","r": "Maison automatisée."},{"q": "Qu'est-ce qu'un assistant vocal ?","r": "IA de commande vocale."},{"q": "Qu'est-ce qu'un AGV ?","r": "Véhicule guidé automatique."},{"q": "Qu'est-ce que la biomasse ?","r": "Énergie de la matière organique."},{"q": "Qu'est-ce que la géothermie ?","r": "Énergie de la Terre."},{"q": "Qu'est-ce que la fusion nucléaire ?","r": "Fusion des noyaux."},{"q": "Qu'est-ce que le charbon ?","r": "Énergie fossile."},{"q": "Qu'est-ce que le pétrole ?","r": "Énergie fossile."},{"q": "Qu'est-ce que le gaz naturel ?","r": "Énergie fossile."},{"q": "Qu'est-ce qu'un panneau photovoltaïque ?","r": "Convertit la lumière."},{"q": "Qu'est-ce qu'une éolienne ?","r": "Produit du vent."},{"q": "Qu'est-ce qu'une centrale ?","r": "Production d'énergie."},{"q": "Qu'est-ce qu'un réacteur ?","r": "Cœur d'une centrale."},{"q": "Qu'est-ce que la transition énergétique ?","r": "Changer de modèle."},{"q": "Qu'est-ce que la sobriété énergétique ?","r": "Moins consommer."},{"q": "Qu'est-ce que le kWh ?","r": "Kilowattheure."},{"q": "Qu'est-ce que la puissance ?","r": "Énergie par temps."},{"q": "Qu'est-ce que la COP ?","r": "Conférence sur le climat."},{"q": "Qu'est-ce que l'Accord de Paris ?","r": "Traité climatique de 2015."},{"q": "Quel objectif de réchauffement ?","r": "Limiter à +1,5 °C."},{"q": "Qu'est-ce que le GIEC ?","r": "Groupe d'experts climat."},{"q": "Qu'est-ce que la neutralité carbone ?","r": "Zéro émission nette."},{"q": "Qu'est-ce que la compensation carbone ?","r": "Compenser ses émissions."},{"q": "Qu'est-ce que le marché carbone ?","r": "Échange de quotas."},{"q": "Qu'est-ce que la taxe carbone ?","r": "Taxe sur émissions."},{"q": "Qu'est-ce que la finance verte ?","r": "Investissement durable."},{"q": "Qu'est-ce qu'un ESG ?","r": "Critères environnementaux, sociaux, gouvernance."},{"q": "Qu'est-ce que l'économie circulaire ?","r": "Réutiliser les ressources."},{"q": "Qu'est-ce que l'obsolescence programmée ?","r": "Vie limitée volontaire."},{"q": "Qu'est-ce que le droit à la réparation ?","r": "Réparer ses produits."},{"q": "Qu'est-ce que le greenwashing ?","r": "Faux écologique."},{"q": "Qu'est-ce que l'agroforesterie ?","r": "Arbres + cultures."},{"q": "Qu'est-ce que l'agriculture raisonnée ?","r": "Réduire les intrants."},{"q": "Qu'est-ce que l'hydroponie ?","r": "Culture hors-sol."},{"q": "Qu'est-ce qu'une ferme verticale ?","r": "Culture en hauteur."},{"q": "Qu'est-ce que la vache ?","r": "Animal d'élevage."},{"q": "Qu'est-ce que le mouton ?","r": "Animal à laine."},{"q": "Qu'est-ce que la chèvre ?","r": "Animal d'élevage."},{"q": "Qu'est-ce que le cochon ?","r": "Animal d'élevage."},{"q": "Qu'est-ce que la poule ?","r": "Animal de basse-cour."},{"q": "Qu'est-ce que le canard ?","r": "Oiseau aquatique."},{"q": "Qu'est-ce que l'apiculture ?","r": "Élevage d'abeilles."},{"q": "Qu'est-ce que la pisciculture ?","r": "Élevage de poissons."},{"q": "Qu'est-ce que la viticulture ?","r": "Culture de la vigne."},{"q": "Qu'est-ce que la sylviculture ?","r": "Gestion des forêts."},{"q": "Qu'est-ce que la polyculture ?","r": "Plusieurs cultures."},{"q": "Qu'est-ce que la monoculture ?","r": "Une seule culture."},{"q": "Qu'est-ce que l'irrigation ?","r": "Apport d'eau."},{"q": "Qu'est-ce que la rotation des cultures ?","r": "Alterner les cultures."},{"q": "Qu'est-ce que le labour ?","r": "Retourner la terre."},{"q": "Qu'est-ce que les semences ?","r": "Graines à planter."},{"q": "Qu'est-ce que les pesticides ?","r": "Produits chimiques."},{"q": "Qu'est-ce que les engrais ?","r": "Nourrissent les plantes."},{"q": "Qu'est-ce que la médecine ?","r": "Science du soin."},{"q": "Qu'est-ce que la chirurgie ?","r": "Opération médicale."},{"q": "Qu'est-ce que la cardiologie ?","r": "Spécialité du cœur."},{"q": "Qu'est-ce que la neurologie ?","r": "Spécialité du cerveau."},{"q": "Qu'est-ce que la pédiatrie ?","r": "Spécialité des enfants."},{"q": "Qu'est-ce que la gériatrie ?","r": "Spécialité des aînés."},{"q": "Qu'est-ce que la dermatologie ?","r": "Spécialité de la peau."},{"q": "Qu'est-ce que l'ophtalmologie ?","r": "Spécialité des yeux."},{"q": "Qu'est-ce que l'ORL ?","r": "Oreille-nez-gorge."},{"q": "Qu'est-ce que la psychiatrie ?","r": "Spécialité mentale."},{"q": "Qu'est-ce qu'un vaccin à ARN ?","r": "Vaccin nouvelle génération."},{"q": "Qu'est-ce qu'un anticorps monoclonal ?","r": "Anticorps de laboratoire."},{"q": "Qu'est-ce que la thérapie génique ?","r": "Traitement par gènes."},{"q": "Qu'est-ce qu'un essai clinique ?","r": "Test sur patients."},{"q": "Qu'est-ce qu'un placebo ?","r": "Substance sans effet."},{"q": "Qu'est-ce qu'un double aveugle ?","r": "Ni patient ni médecin ne sait."},{"q": "Qu'est-ce que la télémédecine ?","r": "Médecine à distance."},{"q": "Qu'est-ce que la e-santé ?","r": "Santé numérique."},{"q": "Qu'est-ce qu'un objet connecté santé ?","r": "Appareil de suivi."},{"q": "Qu'est-ce que la médecine personnalisée ?","r": "Traitement sur mesure."},{"q": "Qu'est-ce que l'IA en médecine ?","r": "Aide au diagnostic."},{"q": "Qu'est-ce qu'un implant ?","r": "Dispositif interne."},{"q": "Qu'est-ce qu'une prothèse ?","r": "Remplace un organe."},{"q": "Qu'est-ce que la nanomédecine ?","r": "Médecine à l'échelle nano."},{"q": "Qu'est-ce que la médecine régénérative ?","r": "Réparer les tissus."},{"q": "Qu'est-ce que CRISPR ?","r": "Outil d'édition génétique."},{"q": "Qu'est-ce qu'un organoïde ?","r": "Mini-organe en labo."},{"q": "Qu'est-ce qu'une cellule souche ?","r": "Cellule indifférenciée."},{"q": "Qu'est-ce que le clonage ?","r": "Copie génétique."},{"q": "Qu'est-ce qu'un OGM ?","r": "Organisme modifié."},{"q": "Qu'est-ce que la réalité mixte ?","r": "VR + AR."},{"q": "Qu'est-ce qu'un casque VR ?","r": "Appareil immersif."},{"q": "Qu'est-ce qu'un hologramme ?","r": "Image 3D."},{"q": "Qu'est-ce que la bio-impression ?","r": "Impression de tissus."},{"q": "Qu'est-ce qu'un jumeau numérique ?","r": "Copie virtuelle."},{"q": "Qu'est-ce que la 6G ?","r": "Future génération mobile."},{"q": "Qu'est-ce qu'un ordinateur quantique ?","r": "Machine à qubits."},{"q": "Qu'est-ce qu'un qubit ?","r": "Bit quantique."},{"q": "Qu'est-ce que la superposition ?","r": "État quantique multiple."},{"q": "Qu'est-ce que l'intrication ?","r": "Corrélation quantique."},{"q": "Qu'est-ce que la cryptographie quantique ?","r": "Sécurité quantique."},{"q": "Qu'est-ce que le calcul exascale ?","r": "Un milliard de milliards d'opérations."},{"q": "Qu'est-ce qu'un superordinateur ?","r": "Machine très puissante."},{"q": "Quel est le plus puissant aujourd'hui ?","r": "Frontier (États-Unis)."},{"q": "Qu'est-ce que le cloud quantique ?","r": "Calcul quantique à distance."},{"q": "Qu'est-ce que la post-quantique ?","r": "Cryptographie résistante."},{"q": "Qu'est-ce que la 5G industrielle ?","r": "Réseau pour usines."},{"q": "Qu'est-ce que l'industrie 4.0 ?","r": "Usine connectée."},{"q": "Qu'est-ce que l'IoT ?","r": "Internet des objets."},{"q": "Qu'est-ce que la fabrication additive ?","r": "Impression 3D industrielle."},{"q": "Qu'est-ce que la maintenance prédictive ?","r": "Anticiper les pannes."},{"q": "Qu'est-ce que la supply chain ?","r": "Chaîne d'approvisionnement."},{"q": "Qu'est-ce que la logistique ?","r": "Gestion des flux."},{"q": "Qu'est-ce que le dernier kilomètre ?","r": "Livraison finale."},{"q": "Qu'est-ce que la voiture autonome ?","r": "Véhicule sans conducteur."},{"q": "Quels sont les niveaux d'autonomie ?","r": "0 à 5."},{"q": "Qu'est-ce que le LiDAR ?","r": "Détection laser."},{"q": "Qu'est-ce que la V2X ?","r": "Véhicule connecté à tout."},{"q": "Qu'est-ce que le MaaS ?","r": "Mobilité servicielle."},{"q": "Qu'est-ce que l'hyperloop ?","r": "Train sous vide."},{"q": "Qu'est-ce que le TGV ?","r": "Train grande vitesse."},{"q": "Qu'est-ce que le Maglev ?","r": "Train magnétique."},{"q": "Qu'est-ce que le train à hydrogène ?","r": "Train sans CO2."},{"q": "Qu'est-ce que la mobilité douce ?","r": "Vélo, marche."},{"q": "Qu'est-ce qu'un smart grid ?","r": "Réseau électrique intelligent."},{"q": "Qu'est-ce qu'un compteur Linky ?","r": "Compteur communicant."},{"q": "Qu'est-ce que le stockage d'énergie ?","r": "Garder l'énergie."},{"q": "Qu'est-ce qu'une batterie lithium-ion ?","r": "Batterie courante."},{"q": "Qu'est-ce qu'une batterie solide ?","r": "Technologie future."},{"q": "Qu'est-ce que l'hydrogène vert ?","r": "H2 renouvelable."},{"q": "Qu'est-ce qu'une pile à combustible ?","r": "Convertit H2 en électricité."},{"q": "Qu'est-ce que la capture du CO2 ?","r": "Piéger le carbone."},{"q": "Qu'est-ce que la séquestration carbone ?","r": "Stocker le CO2."},{"q": "Qu'est-ce que le DAC ?","r": "Capture directe dans l'air."},{"q": "Qu'est-ce que la géo-ingénierie ?","r": "Modifier le climat."},{"q": "Qu'est-ce que l'ensemencement des nuages ?","r": "Provoquer la pluie."},{"q": "Qu'est-ce que le solaire spatial ?","r": "Panneaux en orbite."},{"q": "Qu'est-ce que l'éolien offshore ?","r": "Éoliennes en mer."},{"q": "Qu'est-ce que le solaire flottant ?","r": "Panneaux sur l'eau."},{"q": "Qu'est-ce que la smart city ?","r": "Ville connectée."},{"q": "Qu'est-ce que la mobilité urbaine ?","r": "Déplacements en ville."},{"q": "Qu'est-ce que l'urbanisme ?","r": "Aménagement des villes."},{"q": "Qu'est-ce que la densification ?","r": "Construire plus dense."},{"q": "Qu'est-ce que la ZFE ?","r": "Zone à faibles émissions."},{"q": "Qu'est-ce que la démocratie participative ?","r": "Citoyens impliqués."},{"q": "Qu'est-ce qu'un budget participatif ?","r": "Citoyens décident."},{"q": "Qu'est-ce qu'un référendum ?","r": "Vote direct."},{"q": "Qu'est-ce qu'une initiative citoyenne ?","r": "Proposition de loi."},{"q": "Qu'est-ce que la convention citoyenne ?","r": "Panel de citoyens."},{"q": "Qu'est-ce que la gouvernance ?","r": "Mode de direction."},{"q": "Qu'est-ce que la transparence ?","r": "Information accessible."},{"q": "Qu'est-ce que la corruption ?","r": "Abus de pouvoir."},{"q": "Qu'est-ce que le lobbying ?","r": "Influence politique."},{"q": "Qu'est-ce que la liberté de la presse ?","r": "Médias indépendants."},{"q": "Qu'est-ce que le racisme systémique ?","r": "Racisme structurel."},{"q": "Qu'est-ce que l'intersectionnalité ?","r": "Croisement des dominations."},{"q": "Qu'est-ce que le privilège ?","r": "Avantage non mérité."},{"q": "Qu'est-ce que l'égalité des chances ?","r": "Même point de départ."},{"q": "Qu'est-ce que la discrimination positive ?","r": "Mesure correctrice."},{"q": "Qu'est-ce que le plafond de verre ?","r": "Blocage invisible."},{"q": "Qu'est-ce que l'écart salarial ?","r": "Différence de salaire."},{"q": "Qu'est-ce que la liberté religieuse ?","r": "Croire ou non."},{"q": "Qu'est-ce que le blasphème ?","r": "Insulte au sacré."},{"q": "Qu'est-ce que le multiculturalisme ?","r": "Coexistence des cultures."},{"q": "Qu'est-ce que l'intégration ?","r": "S'insérer dans une société."},{"q": "Qu'est-ce que l'assimilation ?","r": "Adopter la culture dominante."},{"q": "Qu'est-ce que le communautarisme ?","r": "Repli sur sa communauté."},{"q": "Qu'est-ce que le vivre-ensemble ?","r": "Coexister en paix."},{"q": "Qu'est-ce que la citoyenneté mondiale ?","r": "Appartenance planétaire."},{"q": "Qu'est-ce que la coopération ?","r": "Agir ensemble."},{"q": "Qu'est-ce que la compétition ?","r": "Rivaliser."},{"q": "Qu'est-ce que l'entraide ?","r": "Aide mutuelle."},{"q": "Qu'est-ce que le don ?","r": "Offrir gratuitement."},{"q": "Qu'est-ce que la sérénité ?","r": "Calme intérieur."},{"q": "Qu'est-ce que l'optimisme ?","r": "Voir le bon côté."},{"q": "Qu'est-ce que le pessimisme ?","r": "Voir le mauvais côté."},{"q": "Qu'est-ce que le réalisme ?","r": "Voir les choses telles qu'elles sont."},{"q": "Qu'est-ce que l'idéalisme ?","r": "Croire aux idéaux."},{"q": "Qu'est-ce que le pragmatisme ?","r": "Privilégier l'efficace."},{"q": "Qu'est-ce que la mort ?","r": "Fin de la vie."},{"q": "Qu'est-ce que la spiritualité ?","r": "Recherche de sens."},{"q": "Qu'est-ce que le karma ?","r": "Conséquence des actes."},{"q": "Qu'est-ce que la réincarnation ?","r": "Renaître après la mort."},{"q": "Qu'est-ce que le nirvana ?","r": "État de libération."},{"q": "Qu'est-ce que l'âme ?","r": "Principe immatériel."},{"q": "Qu'est-ce que l'esprit ?","r": "Activité mentale."},{"q": "Qu'est-ce que la famille ?","r": "Groupe uni."},{"q": "Qu'est-ce que le couple ?","r": "Deux personnes unies."},{"q": "Qu'est-ce que le mariage ?","r": "Union officielle."},{"q": "Qu'est-ce que la parentalité ?","r": "Être parent."},{"q": "Qu'est-ce que l'éducation ?","r": "Transmettre des savoirs."},{"q": "Qu'est-ce que la transmission ?","r": "Passer un héritage."},{"q": "Qu'est-ce que le temps ?","r": "Dimension du changement."},{"q": "Qu'est-ce que l'espace ?","r": "Dimension de l'étendue."},{"q": "Qu'est-ce que l'infini ?","r": "Sans limite."},{"q": "Qu'est-ce que l'éternité ?","r": "Durée sans fin."},{"q": "Qu'est-ce que le hasard ?","r": "Absence de cause déterminée."},{"q": "Qu'est-ce que la causalité ?","r": "Lien cause-effet."},{"q": "Qu'est-ce que le déterminisme ?","r": "Tout est déterminé."},{"q": "Qu'est-ce que le libre arbitre ?","r": "Pouvoir de choisir."},{"q": "Qu'est-ce que la beauté ?","r": "Qualité esthétique."},{"q": "Qu'est-ce que l'art ?","r": "Création esthétique."},{"q": "Qu'est-ce que la création ?","r": "Produire du nouveau."},{"q": "Qu'est-ce que l'intuition ?","r": "Savoir immédiat."},{"q": "Qu'est-ce que la dialectique ?","r": "Art du dialogue."},{"q": "Qu'est-ce que la sophistique ?","r": "Argumentation trompeuse."},{"q": "Qu'est-ce que le stoïcisme ?","r": "Philosophie de la maîtrise."},{"q": "Qu'est-ce que l'épicurisme ?","r": "Philosophie du plaisir."},{"q": "Qu'est-ce que le scepticisme ?","r": "Douter de tout."},{"q": "Qu'est-ce que le nihilisme ?","r": "Rien n'a de sens."},{"q": "Qu'est-ce que l'existentialisme ?","r": "L'existence précède l'essence."},{"q": "Qu'est-ce que le matérialisme ?","r": "Tout est matière."},{"q": "Qu'est-ce que le rationalisme ?","r": "Primauté de la raison."},{"q": "Qu'est-ce que l'empirisme ?","r": "Primauté de l'expérience."},{"q": "Qu'est-ce que le positivisme ?","r": "Science seule source."},{"q": "Qu'est-ce que la vertu ?","r": "Disposition au bien."},{"q": "Qu'est-ce que le devoir ?","r": "Obligation morale."},{"q": "Qu'est-ce que la responsabilité ?","r": "Répondre de ses actes."},{"q": "Qu'est-ce que l'équité ?","r": "Traitement juste."},{"q": "Qu'est-ce que la lâcheté ?","r": "Manquer de courage."},{"q": "Qu'est-ce que la persévérance ?","r": "Continuer malgré les obstacles."},{"q": "Qu'est-ce que la discipline ?","r": "Rigueur personnelle."},{"q": "Qu'est-ce que la volonté ?","r": "Force de décision."},{"q": "Qu'est-ce que l'ambition ?","r": "Désir de réussir."},{"q": "Qu'est-ce que l'humilité ?","r": "Ne pas se surestimer."},{"q": "Qu'est-ce que l'envie ?","r": "Désir de ce qu'a l'autre."},{"q": "Qu'est-ce que la méfiance ?","r": "Douter de l'autre."},{"q": "Qu'est-ce que la trahison ?","r": "Violer la confiance."},{"q": "Qu'est-ce que la loyauté ?","r": "Fidélité."},{"q": "Qu'est-ce que l'honnêteté ?","r": "Dire la vérité."},{"q": "Qu'est-ce que le mensonge ?","r": "Dire le faux."},{"q": "Qu'est-ce que l'hypocrisie ?","r": "Feindre des sentiments."},{"q": "Qu'est-ce que la sincérité ?","r": "Être vrai."},{"q": "Qu'est-ce que l'authenticité ?","r": "Être soi-même."},{"q": "Qu'est-ce que l'intégrité ?","r": "Cohérence morale."},{"q": "Qu'est-ce que la vie ?","r": "État des êtres organisés."},{"q": "Qu'est-ce que la naissance ?","r": "Début de la vie."},{"q": "Qu'est-ce que grandir ?","r": "Se développer."},{"q": "Qu'est-ce qu'apprendre ?","r": "Acquérir des savoirs."},{"q": "Qu'est-ce que comprendre ?","r": "Saisir le sens."},{"q": "Qu'est-ce que créer ?","r": "Produire du nouveau."},{"q": "Qu'est-ce qu'aimer ?","r": "Éprouver de l'affection."},{"q": "Qu'est-ce que vivre ?","r": "Exister et agir."},{"q": "Quel est le sens de la vie ?","r": "Chacun le construit ; souvent : aimer, créer, transmettre."}]
''')

# Lot supplementaire fourni par Richy pour entrainer l'assistant
# (culture generale : droit, medecine, sciences, informatique, arts,
# litterature, philosophie, religions, langues, sport, musique, etc.)
DEFAULT_DIALOGUES += json.loads(r'''
[
  {"id":4251,"q":"Qu'est-ce qu'un contrat ?","r":"Un accord de volontés créant des obligations."},
  {"id":4252,"q":"Qu'est-ce qu'un contrat synallagmatique ?","r":"Un contrat qui oblige les deux parties."},
  {"id":4253,"q":"Qu'est-ce qu'un contrat unilatéral ?","r":"Un contrat qui n'oblige qu'une partie."},
  {"id":4254,"q":"Qu'est-ce qu'une clause ?","r":"Une disposition d'un contrat."},
  {"id":4255,"q":"Qu'est-ce qu'une clause abusive ?","r":"Une clause déséquilibrant le contrat."},
  {"id":4256,"q":"Qu'est-ce qu'une nullité ?","r":"L'annulation d'un acte."},
  {"id":4257,"q":"Qu'est-ce qu'un vice caché ?","r":"Un défaut non visible."},
  {"id":4258,"q":"Qu'est-ce qu'une garantie ?","r":"L'engagement de réparer un défaut."},
  {"id":4259,"q":"Qu'est-ce qu'une prescription ?","r":"L'extinction d'un droit par le temps."},
  {"id":4260,"q":"Qu'est-ce qu'un litige ?","r":"Un désaccord juridique."},
  {"id":4261,"q":"Qu'est-ce qu'un tribunal correctionnel ?","r":"Juge les délits."},
  {"id":4262,"q":"Qu'est-ce qu'une cour d'assises ?","r":"Juge les crimes."},
  {"id":4263,"q":"Qu'est-ce qu'un délit ?","r":"Une infraction punie d'amende."},
  {"id":4264,"q":"Qu'est-ce qu'un crime ?","r":"L'infraction la plus grave."},
  {"id":4265,"q":"Qu'est-ce qu'une contravention ?","r":"Une infraction mineure."},
  {"id":4266,"q":"Qu'est-ce qu'une garde à vue ?","r":"Rétention par la police."},
  {"id":4267,"q":"Qu'est-ce qu'une mise en examen ?","r":"Poursuite devant un juge."},
  {"id":4268,"q":"Qu'est-ce qu'un non-lieu ?","r":"Fin de poursuites sans jugement."},
  {"id":4269,"q":"Qu'est-ce que la présomption d'innocence ?","r":"Tout accusé est innocent jusqu'à preuve."},
  {"id":4270,"q":"Qu'est-ce que la légitime défense ?","r":"Réponse proportionnée à une agression."},
  {"id":4271,"q":"Qu'est-ce qu'un diagnostic ?","r":"Identification d'une maladie."},
  {"id":4272,"q":"Qu'est-ce qu'un pronostic ?","r":"Prévision de l'évolution."},
  {"id":4273,"q":"Qu'est-ce qu'une étiologie ?","r":"L'étude des causes."},
  {"id":4274,"q":"Qu'est-ce que la pathogenèse ?","r":"Mécanisme d'une maladie."},
  {"id":4275,"q":"Qu'est-ce que la symptomatologie ?","r":"Étude des symptômes."},
  {"id":4276,"q":"Qu'est-ce que la nosologie ?","r":"Classification des maladies."},
  {"id":4277,"q":"Qu'est-ce que l'épidémiologie ?","r":"Étude des maladies en population."},
  {"id":4278,"q":"Qu'est-ce que l'incidence ?","r":"Nombre de nouveaux cas."},
  {"id":4279,"q":"Qu'est-ce que la prévalence ?","r":"Nombre total de cas."},
  {"id":4280,"q":"Qu'est-ce que la mortalité ?","r":"Nombre de décès."},
  {"id":4281,"q":"Qu'est-ce que la morbidité ?","r":"Fréquence des maladies."},
  {"id":4282,"q":"Qu'est-ce qu'un facteur de risque ?","r":"Élément augmentant le risque."},
  {"id":4283,"q":"Qu'est-ce qu'un traitement palliatif ?","r":"Soulage sans guérir."},
  {"id":4284,"q":"Qu'est-ce qu'une rémission ?","r":"Disparition temporaire des symptômes."},
  {"id":4285,"q":"Qu'est-ce qu'une rechute ?","r":"Retour de la maladie."},
  {"id":4286,"q":"Qu'est-ce qu'une métastase ?","r":"Propagation d'un cancer."},
  {"id":4287,"q":"Qu'est-ce qu'une biopsie ?","r":"Prélèvement de tissu."},
  {"id":4288,"q":"Qu'est-ce qu'une endoscopie ?","r":"Examen interne par caméra."},
  {"id":4289,"q":"Qu'est-ce qu'une dialyse ?","r":"Filtration artificielle du sang."},
  {"id":4290,"q":"Qu'est-ce qu'une transfusion ?","r":"Injection de sang."},
  {"id":4291,"q":"Qu'est-ce qu'un algorithme ?","r":"Suite d'instructions."},
  {"id":4292,"q":"Qu'est-ce qu'une complexité algorithmique ?","r":"Coût en temps/mémoire."},
  {"id":4293,"q":"Qu'est-ce que la récursivité ?","r":"Fonction qui s'appelle elle-même."},
  {"id":4294,"q":"Qu'est-ce que la compilation ?","r":"Traduction en code machine."},
  {"id":4295,"q":"Qu'est-ce que l'interprétation ?","r":"Exécution ligne à ligne."},
  {"id":4296,"q":"Qu'est-ce qu'un compilateur ?","r":"Logiciel qui compile."},
  {"id":4297,"q":"Qu'est-ce qu'un interpréteur ?","r":"Logiciel qui interprète."},
  {"id":4298,"q":"Qu'est-ce qu'un débogueur ?","r":"Outil de débogage."},
  {"id":4299,"q":"Qu'est-ce qu'un IDE ?","r":"Environnement de développement."},
  {"id":4300,"q":"Qu'est-ce qu'un compilateur JIT ?","r":"Compilation à la volée."},
  {"id":4301,"q":"Qu'est-ce qu'une pile ?","r":"Structure LIFO."},
  {"id":4302,"q":"Qu'est-ce qu'une file ?","r":"Structure FIFO."},
  {"id":4303,"q":"Qu'est-ce qu'une liste chaînée ?","r":"Suite de nœuds liés."},
  {"id":4304,"q":"Qu'est-ce qu'un arbre binaire ?","r":"Structure hiérarchique à 2 enfants."},
  {"id":4305,"q":"Qu'est-ce qu'un graphe ?","r":"Ensemble de nœuds et d'arêtes."},
  {"id":4306,"q":"Qu'est-ce qu'une table de hachage ?","r":"Structure à clé-valeur."},
  {"id":4307,"q":"Qu'est-ce qu'un tri rapide ?","r":"Quicksort."},
  {"id":4308,"q":"Qu'est-ce qu'un tri fusion ?","r":"Mergesort."},
  {"id":4309,"q":"Qu'est-ce que la recherche binaire ?","r":"Recherche dichotomique."},
  {"id":4310,"q":"Qu'est-ce que la programmation fonctionnelle ?","r":"Paradigme basé sur les fonctions."},
  {"id":4311,"q":"Qu'est-ce qu'une closure ?","r":"Fonction capturant son environnement."},
  {"id":4312,"q":"Qu'est-ce qu'un lambda ?","r":"Fonction anonyme."},
  {"id":4313,"q":"Qu'est-ce qu'une promesse ?","r":"Objet asynchrone."},
  {"id":4314,"q":"Qu'est-ce qu'une coroutine ?","r":"Fonction suspendable."},
  {"id":4315,"q":"Qu'est-ce qu'un thread ?","r":"Fil d'exécution."},
  {"id":4316,"q":"Qu'est-ce qu'un mutex ?","r":"Verrou d'exclusion."},
  {"id":4317,"q":"Qu'est-ce qu'un deadlock ?","r":"Blocage mutuel."},
  {"id":4318,"q":"Qu'est-ce qu'une race condition ?","r":"Conflit d'accès concurrent."},
  {"id":4319,"q":"Qu'est-ce qu'une API REST ?","r":"Interface HTTP stateless."},
  {"id":4320,"q":"Qu'est-ce que GraphQL ?","r":"Langage de requêtes API."},
  {"id":4321,"q":"Qu'est-ce qu'un nombre premier ?","r":"Divisible par 1 et lui-même."},
  {"id":4322,"q":"Qu'est-ce qu'un PGCD ?","r":"Plus grand commun diviseur."},
  {"id":4323,"q":"Qu'est-ce qu'un PPCM ?","r":"Plus petit commun multiple."},
  {"id":4324,"q":"Qu'est-ce qu'un nombre rationnel ?","r":"Quotient de deux entiers."},
  {"id":4325,"q":"Qu'est-ce qu'un nombre irrationnel ?","r":"Non quotient d'entiers."},
  {"id":4326,"q":"Qu'est-ce qu'un nombre réel ?","r":"Ensemble des rationnels et irrationnels."},
  {"id":4327,"q":"Qu'est-ce qu'un nombre complexe ?","r":"a + bi."},
  {"id":4328,"q":"Qu'est-ce qu'une matrice identité ?","r":"1 sur la diagonale, 0 ailleurs."},
  {"id":4329,"q":"Qu'est-ce qu'un déterminant nul ?","r":"Matrice non inversible."},
  {"id":4330,"q":"Qu'est-ce qu'un vecteur propre ?","r":"Vecteur invariant par transformation."},
  {"id":4331,"q":"Qu'est-ce qu'une valeur propre ?","r":"Facteur associé au vecteur propre."},
  {"id":4332,"q":"Qu'est-ce qu'une intégrale définie ?","r":"Aire sous une courbe."},
  {"id":4333,"q":"Qu'est-ce qu'une dérivée partielle ?","r":"Dérivée par rapport à une variable."},
  {"id":4334,"q":"Qu'est-ce qu'une équation différentielle ?","r":"Équation avec dérivées."},
  {"id":4335,"q":"Qu'est-ce qu'une série convergente ?","r":"Somme finie."},
  {"id":4336,"q":"Qu'est-ce qu'une série divergente ?","r":"Somme infinie."},
  {"id":4337,"q":"Qu'est-ce que la loi normale ?","r":"Distribution en cloche."},
  {"id":4338,"q":"Qu'est-ce que la loi binomiale ?","r":"Distribution de succès/échecs."},
  {"id":4339,"q":"Qu'est-ce que la loi de Poisson ?","r":"Distribution d'événements rares."},
  {"id":4340,"q":"Qu'est-ce que le théorème central limite ?","r":"Convergence vers la normale."},
  {"id":4341,"q":"Qu'est-ce qu'une force ?","r":"Interaction modifiant le mouvement."},
  {"id":4342,"q":"Quelle est l'unité de force ?","r":"Le newton."},
  {"id":4343,"q":"Qu'est-ce que l'inertie ?","r":"Résistance au changement de mouvement."},
  {"id":4344,"q":"Qu'est-ce que la quantité de mouvement ?","r":"Masse × vitesse."},
  {"id":4345,"q":"Qu'est-ce que le moment cinétique ?","r":"Rotation × inertie."},
  {"id":4346,"q":"Qu'est-ce que l'énergie mécanique ?","r":"Cinétique + potentielle."},
  {"id":4347,"q":"Qu'est-ce que la chaleur ?","r":"Transfert d'énergie thermique."},
  {"id":4348,"q":"Qu'est-ce que l'entropie ?","r":"Désordre d'un système."},
  {"id":4349,"q":"Qu'est-ce que le 2e principe de la thermodynamique ?","r":"L'entropie augmente."},
  {"id":4350,"q":"Qu'est-ce qu'un cycle de Carnot ?","r":"Cycle thermodynamique idéal."},
  {"id":4351,"q":"Qu'est-ce qu'une onde ?","r":"Propagation d'une perturbation."},
  {"id":4352,"q":"Qu'est-ce que la fréquence ?","r":"Nombre d'oscillations par seconde."},
  {"id":4353,"q":"Qu'est-ce que la longueur d'onde ?","r":"Distance entre deux crêtes."},
  {"id":4354,"q":"Qu'est-ce que la diffraction ?","r":"Déviation d'une onde."},
  {"id":4355,"q":"Qu'est-ce que l'interférence ?","r":"Superposition d'ondes."},
  {"id":4356,"q":"Qu'est-ce que l'effet Doppler ?","r":"Changement de fréquence par mouvement."},
  {"id":4357,"q":"Qu'est-ce que la réfraction ?","r":"Changement de direction de la lumière."},
  {"id":4358,"q":"Qu'est-ce que la réflexion ?","r":"Renvoi de la lumière."},
  {"id":4359,"q":"Qu'est-ce que la polarisation ?","r":"Orientation d'une onde."},
  {"id":4360,"q":"Qu'est-ce que le laser ?","r":"Lumière amplifiée cohérente."},
  {"id":4361,"q":"Qu'est-ce qu'un atome ?","r":"Noyau + électrons."},
  {"id":4362,"q":"Qu'est-ce qu'un isotope ?","r":"Même élément, masse différente."},
  {"id":4363,"q":"Qu'est-ce qu'un ion ?","r":"Atome chargé."},
  {"id":4364,"q":"Qu'est-ce qu'une liaison covalente ?","r":"Partage d'électrons."},
  {"id":4365,"q":"Qu'est-ce qu'une liaison ionique ?","r":"Attraction électrostatique."},
  {"id":4366,"q":"Qu'est-ce qu'une liaison hydrogène ?","r":"Interaction faible polaire."},
  {"id":4367,"q":"Qu'est-ce qu'une réaction exothermique ?","r":"Libère de la chaleur."},
  {"id":4368,"q":"Qu'est-ce qu'une réaction endothermique ?","r":"Absorbe de la chaleur."},
  {"id":4369,"q":"Qu'est-ce qu'un catalyseur ?","r":"Accélère sans être consommé."},
  {"id":4370,"q":"Qu'est-ce que l'équilibre chimique ?","r":"Vitesses égales dans les 2 sens."},
  {"id":4371,"q":"Qu'est-ce qu'un acide ?","r":"Donneur de protons."},
  {"id":4372,"q":"Qu'est-ce qu'une base ?","r":"Accepteur de protons."},
  {"id":4373,"q":"Qu'est-ce que le pH ?","r":"Mesure d'acidité."},
  {"id":4374,"q":"Qu'est-ce qu'une solution tampon ?","r":"Résiste aux variations de pH."},
  {"id":4375,"q":"Qu'est-ce qu'une oxydation ?","r":"Perte d'électrons."},
  {"id":4376,"q":"Qu'est-ce qu'une réduction ?","r":"Gain d'électrons."},
  {"id":4377,"q":"Qu'est-ce qu'une électrolyse ?","r":"Décomposition par courant."},
  {"id":4378,"q":"Qu'est-ce qu'une pile ?","r":"Conversion chimique en électrique."},
  {"id":4379,"q":"Qu'est-ce qu'une molécule organique ?","r":"Contient du carbone."},
  {"id":4380,"q":"Qu'est-ce qu'un polymère ?","r":"Grande molécule répétitive."},
  {"id":4381,"q":"Qu'est-ce qu'une cellule ?","r":"Unité du vivant."},
  {"id":4382,"q":"Qu'est-ce que la membrane cellulaire ?","r":"Enveloppe sélective."},
  {"id":4383,"q":"Qu'est-ce que le noyau ?","r":"Contient l'ADN."},
  {"id":4384,"q":"Qu'est-ce qu'une mitochondrie ?","r":"Centrale énergétique."},
  {"id":4385,"q":"Qu'est-ce qu'un ribosome ?","r":"Synthèse des protéines."},
  {"id":4386,"q":"Qu'est-ce qu'un chloroplaste ?","r":"Photosynthèse."},
  {"id":4387,"q":"Qu'est-ce que la mitose ?","r":"Division cellulaire."},
  {"id":4388,"q":"Qu'est-ce que la méiose ?","r":"Division des gamètes."},
  {"id":4389,"q":"Qu'est-ce que l'ADN ?","r":"Support de l'information génétique."},
  {"id":4390,"q":"Qu'est-ce que l'ARN messager ?","r":"Copie de l'ADN."},
  {"id":4391,"q":"Qu'est-ce qu'un codon ?","r":"3 bases = 1 acide aminé."},
  {"id":4392,"q":"Qu'est-ce qu'une mutation ?","r":"Changement de séquence."},
  {"id":4393,"q":"Qu'est-ce que la sélection naturelle ?","r":"Survie des mieux adaptés."},
  {"id":4394,"q":"Qu'est-ce que la spéciation ?","r":"Apparition d'une espèce."},
  {"id":4395,"q":"Qu'est-ce que la biodiversité ?","r":"Variété du vivant."},
  {"id":4396,"q":"Qu'est-ce qu'un écosystème ?","r":"Milieu + êtres vivants."},
  {"id":4397,"q":"Qu'est-ce qu'une chaîne alimentaire ?","r":"Transfert d'énergie."},
  {"id":4398,"q":"Qu'est-ce qu'un producteur ?","r":"Organisme autotrophe."},
  {"id":4399,"q":"Qu'est-ce qu'un consommateur ?","r":"Organisme hétérotrophe."},
  {"id":4400,"q":"Qu'est-ce qu'un décomposeur ?","r":"Recycle la matière."},
  {"id":4401,"q":"Qu'est-ce que la croûte terrestre ?","r":"Couche externe solide."},
  {"id":4402,"q":"Qu'est-ce que le manteau ?","r":"Couche intermédiaire."},
  {"id":4403,"q":"Qu'est-ce que le noyau terrestre ?","r":"Centre métallique."},
  {"id":4404,"q":"Qu'est-ce que la tectonique des plaques ?","r":"Mouvement des plaques."},
  {"id":4405,"q":"Qu'est-ce qu'une faille ?","r":"Fracture de terrain."},
  {"id":4406,"q":"Qu'est-ce qu'un séisme ?","r":"Secousse terrestre."},
  {"id":4407,"q":"Qu'est-ce que l'échelle de Richter ?","r":"Mesure des séismes."},
  {"id":4408,"q":"Qu'est-ce qu'un volcan ?","r":"Ouverture de magma."},
  {"id":4409,"q":"Qu'est-ce que la lave ?","r":"Magma en surface."},
  {"id":4410,"q":"Qu'est-ce qu'une éruption ?","r":"Expulsion de magma."},
  {"id":4411,"q":"Qu'est-ce qu'un minéral ?","r":"Solide naturel cristallin."},
  {"id":4412,"q":"Qu'est-ce qu'une roche magmatique ?","r":"Issue du magma."},
  {"id":4413,"q":"Qu'est-ce qu'une roche sédimentaire ?","r":"Issue de dépôts."},
  {"id":4414,"q":"Qu'est-ce qu'une roche métamorphique ?","r":"Transformée par pression."},
  {"id":4415,"q":"Qu'est-ce que l'érosion ?","r":"Usure par l'eau, le vent."},
  {"id":4416,"q":"Qu'est-ce que la sédimentation ?","r":"Dépôt de sédiments."},
  {"id":4417,"q":"Qu'est-ce qu'un fossile ?","r":"Trace d'être vivant ancien."},
  {"id":4418,"q":"Qu'est-ce que la stratigraphie ?","r":"Étude des couches."},
  {"id":4419,"q":"Qu'est-ce que la datation carbone 14 ?","r":"Datation des vestiges organiques."},
  {"id":4420,"q":"Qu'est-ce qu'un puits de pétrole ?","r":"Forage pétrolier."},
  {"id":4421,"q":"Qu'est-ce que la météo ?","r":"État de l'atmosphère."},
  {"id":4422,"q":"Qu'est-ce que le climat ?","r":"Moyenne sur 30 ans."},
  {"id":4423,"q":"Qu'est-ce que la pression atmosphérique ?","r":"Poids de l'air."},
  {"id":4424,"q":"Qu'est-ce qu'un anticyclone ?","r":"Zone de haute pression."},
  {"id":4425,"q":"Qu'est-ce qu'une dépression ?","r":"Zone de basse pression."},
  {"id":4426,"q":"Qu'est-ce qu'un front froid ?","r":"Rencontre air froid/air chaud."},
  {"id":4427,"q":"Qu'est-ce qu'un front chaud ?","r":"Air chaud qui monte."},
  {"id":4428,"q":"Qu'est-ce qu'un cyclone ?","r":"Tempête tropicale."},
  {"id":4429,"q":"Qu'est-ce qu'un ouragan ?","r":"Cyclone en Atlantique."},
  {"id":4430,"q":"Qu'est-ce qu'un typhon ?","r":"Cyclone en Pacifique."},
  {"id":4431,"q":"Qu'est-ce qu'une tornade ?","r":"Colonne d'air tourbillonnante."},
  {"id":4432,"q":"Qu'est-ce que la grêle ?","r":"Précipitation de glace."},
  {"id":4433,"q":"Qu'est-ce que le brouillard ?","r":"Nuage au sol."},
  {"id":4434,"q":"Qu'est-ce que la rosée ?","r":"Condensation nocturne."},
  {"id":4435,"q":"Qu'est-ce que le givre ?","r":"Glace sur les surfaces."},
  {"id":4436,"q":"Qu'est-ce que la mousson ?","r":"Vent saisonnier."},
  {"id":4437,"q":"Qu'est-ce qu'un arc-en-ciel ?","r":"Réfraction de la lumière."},
  {"id":4438,"q":"Qu'est-ce qu'un éclair ?","r":"Décharge électrique."},
  {"id":4439,"q":"Qu'est-ce que le tonnerre ?","r":"Son de l'éclair."},
  {"id":4440,"q":"Qu'est-ce que la foudre ?","r":"Éclair au sol."},
  {"id":4441,"q":"Qu'est-ce que l'effet de serre ?","r":"Piégeage de la chaleur."},
  {"id":4442,"q":"Qu'est-ce que le CO2 ?","r":"Dioxyde de carbone."},
  {"id":4443,"q":"Qu'est-ce que le méthane ?","r":"Gaz à effet de serre."},
  {"id":4444,"q":"Qu'est-ce que la couche d'ozone ?","r":"Protection contre UV."},
  {"id":4445,"q":"Qu'est-ce que les pluies acides ?","r":"Précipitations polluées."},
  {"id":4446,"q":"Qu'est-ce que la pollution ?","r":"Contamination du milieu."},
  {"id":4447,"q":"Qu'est-ce que le recyclage ?","r":"Réutilisation des déchets."},
  {"id":4448,"q":"Qu'est-ce que le compostage ?","r":"Décomposition organique."},
  {"id":4449,"q":"Qu'est-ce que l'empreinte écologique ?","r":"Impact sur la planète."},
  {"id":4450,"q":"Qu'est-ce que le développement durable ?","r":"Développement respectueux."},
  {"id":4451,"q":"Qu'est-ce que le PIB ?","r":"Production intérieure brute."},
  {"id":4452,"q":"Qu'est-ce que le PNB ?","r":"Produit national brut."},
  {"id":4453,"q":"Qu'est-ce que l'inflation ?","r":"Hausse des prix."},
  {"id":4454,"q":"Qu'est-ce que la déflation ?","r":"Baisse des prix."},
  {"id":4455,"q":"Qu'est-ce que la stagflation ?","r":"Inflation + stagnation."},
  {"id":4456,"q":"Qu'est-ce que le chômage ?","r":"Absence d'emploi."},
  {"id":4457,"q":"Qu'est-ce que le taux d'intérêt ?","r":"Coût d'un emprunt."},
  {"id":4458,"q":"Qu'est-ce que la banque centrale ?","r":"Émet la monnaie."},
  {"id":4459,"q":"Qu'est-ce que la BCE ?","r":"Banque centrale européenne."},
  {"id":4460,"q":"Qu'est-ce que le FMI ?","r":"Fonds monétaire international."},
  {"id":4461,"q":"Qu'est-ce que la Banque mondiale ?","r":"Institution financière mondiale."},
  {"id":4462,"q":"Qu'est-ce que l'OMC ?","r":"Organisation mondiale du commerce."},
  {"id":4463,"q":"Qu'est-ce que le protectionnisme ?","r":"Protection de l'économie nationale."},
  {"id":4464,"q":"Qu'est-ce que le libre-échange ?","r":"Sans barrières douanières."},
  {"id":4465,"q":"Qu'est-ce qu'une taxe ?","r":"Prélèvement obligatoire."},
  {"id":4466,"q":"Qu'est-ce que la TVA ?","r":"Taxe sur la valeur ajoutée."},
  {"id":4467,"q":"Qu'est-ce qu'un impôt progressif ?","r":"Taux croissant."},
  {"id":4468,"q":"Qu'est-ce qu'une subvention ?","r":"Aide financière."},
  {"id":4469,"q":"Qu'est-ce qu'une nationalisation ?","r":"État rachète une entreprise."},
  {"id":4470,"q":"Qu'est-ce qu'une privatisation ?","r":"Entreprise vendue au privé."},
  {"id":4471,"q":"Que fait un avocat ?","r":"Défend en justice."},
  {"id":4472,"q":"Que fait un notaire ?","r":"Rédige des actes."},
  {"id":4473,"q":"Que fait un huissier ?","r":"Exécute des décisions."},
  {"id":4474,"q":"Que fait un greffier ?","r":"Assiste le tribunal."},
  {"id":4475,"q":"Que fait un procureur ?","r":"Représente l'État."},
  {"id":4476,"q":"Que fait un juge d'instruction ?","r":"Enquête sur les affaires."},
  {"id":4477,"q":"Que fait un expert-comptable ?","r":"Vérifie les comptes."},
  {"id":4478,"q":"Que fait un commissaire aux comptes ?","r":"Audit légal."},
  {"id":4479,"q":"Que fait un actuaire ?","r":"Calcule les risques."},
  {"id":4480,"q":"Que fait un géomètre ?","r":"Mesure les terrains."},
  {"id":4481,"q":"Que fait un urbaniste ?","r":"Planifie les villes."},
  {"id":4482,"q":"Que fait un topographe ?","r":"Relève les reliefs."},
  {"id":4483,"q":"Que fait un hydrographe ?","r":"Étudie les eaux."},
  {"id":4484,"q":"Que fait un océanographe ?","r":"Étudie les océans."},
  {"id":4485,"q":"Que fait un météorologue ?","r":"Prévoit la météo."},
  {"id":4486,"q":"Que fait un sismologue ?","r":"Étudie les séismes."},
  {"id":4487,"q":"Que fait un volcanologue ?","r":"Étudie les volcans."},
  {"id":4488,"q":"Que fait un paléontologue ?","r":"Étudie les fossiles."},
  {"id":4489,"q":"Que fait un archéologue ?","r":"Fouille les vestiges."},
  {"id":4490,"q":"Que fait un anthropologue ?","r":"Étudie les humains."},
  {"id":4491,"q":"Que fait un ethnologue ?","r":"Étudie les cultures."},
  {"id":4492,"q":"Que fait un sociologue ?","r":"Étudie la société."},
  {"id":4493,"q":"Que fait un démographe ?","r":"Étudie les populations."},
  {"id":4494,"q":"Que fait un statisticien ?","r":"Analyse des données."},
  {"id":4495,"q":"Que fait un data scientist ?","r":"Analyse et modélise les données."},
  {"id":4496,"q":"Que fait un actuaire ?","r":"Évalue les risques financiers."},
  {"id":4497,"q":"Que fait un logisticien ?","r":"Gère les flux."},
  {"id":4498,"q":"Que fait un chef de produit ?","r":"Pilote un produit."},
  {"id":4499,"q":"Que fait un UX designer ?","r":"Conçoit l'expérience utilisateur."},
  {"id":4500,"q":"Que fait un growth hacker ?","r":"Accélère la croissance."},
  {"id":4501,"q":"Qu'est-ce qu'un marteau ?","r":"Outil pour frapper."},
  {"id":4502,"q":"Qu'est-ce qu'un tournevis ?","r":"Outil pour visser."},
  {"id":4503,"q":"Qu'est-ce qu'une clé plate ?","r":"Outil pour boulons."},
  {"id":4504,"q":"Qu'est-ce qu'une pince ?","r":"Outil pour saisir."},
  {"id":4505,"q":"Qu'est-ce qu'une tenaille ?","r":"Pince coupante."},
  {"id":4506,"q":"Qu'est-ce qu'une scie ?","r":"Outil pour couper."},
  {"id":4507,"q":"Qu'est-ce qu'une perceuse ?","r":"Outil pour percer."},
  {"id":4508,"q":"Qu'est-ce qu'une meuleuse ?","r":"Outil pour meuler."},
  {"id":4509,"q":"Qu'est-ce qu'un rabot ?","r":"Outil pour aplanir le bois."},
  {"id":4510,"q":"Qu'est-ce qu'un ciseau à bois ?","r":"Outil de sculpture."},
  {"id":4511,"q":"Qu'est-ce qu'un niveau à bulle ?","r":"Vérifie l'horizontalité."},
  {"id":4512,"q":"Qu'est-ce qu'un fil à plomb ?","r":"Vérifie la verticalité."},
  {"id":4513,"q":"Qu'est-ce qu'un mètre ruban ?","r":"Mesure souple."},
  {"id":4514,"q":"Qu'est-ce qu'un pied à coulisse ?","r":"Mesure précise."},
  {"id":4515,"q":"Qu'est-ce qu'un micromètre ?","r":"Mesure très précise."},
  {"id":4516,"q":"Qu'est-ce qu'un multimètre ?","r":"Mesure électrique."},
  {"id":4517,"q":"Qu'est-ce qu'un fer à souder ?","r":"Outil d'électronique."},
  {"id":4518,"q":"Qu'est-ce qu'une pince à dénuder ?","r":"Dénude les fils."},
  {"id":4519,"q":"Qu'est-ce qu'un pistolet à colle ?","r":"Colle chaude."},
  {"id":4520,"q":"Qu'est-ce qu'une équerre ?","r":"Outil à angle droit."},
  {"id":4521,"q":"Qu'est-ce que le cricket ?","r":"Sport de batte et balle."},
  {"id":4522,"q":"Combien de joueurs au cricket ?","r":"11 par équipe."},
  {"id":4523,"q":"Qu'est-ce qu'un guichet au cricket ?","r":"Ensemble de 3 piquets."},
  {"id":4524,"q":"Qu'est-ce que le base-ball ?","r":"Sport de batte et gant."},
  {"id":4525,"q":"Combien de manches au base-ball ?","r":"9."},
  {"id":4526,"q":"Qu'est-ce que le softball ?","r":"Variante du base-ball."},
  {"id":4527,"q":"Qu'est-ce que le hockey sur glace ?","r":"Sport de patins et crosse."},
  {"id":4528,"q":"Combien de joueurs au hockey ?","r":"6 (dont un gardien)."},
  {"id":4529,"q":"Qu'est-ce que le curling ?","r":"Sport de pierres sur glace."},
  {"id":4530,"q":"Qu'est-ce que le biathlon ?","r":"Ski + tir."},
  {"id":4531,"q":"Qu'est-ce que le skeleton ?","r":"Luge tête première."},
  {"id":4532,"q":"Qu'est-ce que le bobsleigh ?","r":"Traîneau rapide."},
  {"id":4533,"q":"Qu'est-ce que le patinage artistique ?","r":"Sport de glace chorégraphié."},
  {"id":4534,"q":"Qu'est-ce que le patinage de vitesse ?","r":"Course sur glace."},
  {"id":4535,"q":"Qu'est-ce que le snowboard ?","r":"Planche à neige."},
  {"id":4536,"q":"Qu'est-ce que le ski alpin ?","r":"Descente à ski."},
  {"id":4537,"q":"Qu'est-ce que le ski de fond ?","r":"Ski sur piste."},
  {"id":4538,"q":"Qu'est-ce que le saut à ski ?","r":"Saut sur tremplin."},
  {"id":4539,"q":"Qu'est-ce que le combiné nordique ?","r":"Saut + fond."},
  {"id":4540,"q":"Qu'est-ce que le freestyle ?","r":"Figures acrobatiques à ski."},
  {"id":4541,"q":"Qu'est-ce qu'un marathon ?","r":"42,195 km."},
  {"id":4542,"q":"Qu'est-ce qu'un semi-marathon ?","r":"21,0975 km."},
  {"id":4543,"q":"Qu'est-ce que le décathlon ?","r":"10 épreuves."},
  {"id":4544,"q":"Qu'est-ce que l'heptathlon ?","r":"7 épreuves (femmes)."},
  {"id":4545,"q":"Qu'est-ce que le pentathlon moderne ?","r":"5 disciplines."},
  {"id":4546,"q":"Qu'est-ce que le triathlon ?","r":"Natation, vélo, course."},
  {"id":4547,"q":"Qu'est-ce que le duathlon ?","r":"Course, vélo, course."},
  {"id":4548,"q":"Qu'est-ce que l'aquathlon ?","r":"Natation + course."},
  {"id":4549,"q":"Qu'est-ce que le 100 m ?","r":"Course sprint."},
  {"id":4550,"q":"Qu'est-ce que le 400 m haies ?","r":"Course avec obstacles."},
  {"id":4551,"q":"Qu'est-ce que le lancer du javelot ?","r":"Lancer d'engin."},
  {"id":4552,"q":"Qu'est-ce que le lancer du disque ?","r":"Lancer d'engin."},
  {"id":4553,"q":"Qu'est-ce que le lancer du poids ?","r":"Lancer d'engin lourd."},
  {"id":4554,"q":"Qu'est-ce que le saut en hauteur ?","r":"Saut vertical."},
  {"id":4555,"q":"Qu'est-ce que le saut à la perche ?","r":"Saut avec perche."},
  {"id":4556,"q":"Qu'est-ce que le saut en longueur ?","r":"Saut horizontal."},
  {"id":4557,"q":"Qu'est-ce que le triple saut ?","r":"3 bonds."},
  {"id":4558,"q":"Qu'est-ce que la marche athlétique ?","r":"Marche rapide codifiée."},
  {"id":4559,"q":"Qu'est-ce que le steeple ?","r":"Course avec obstacles et rivière."},
  {"id":4560,"q":"Qu'est-ce qu'un relais ?","r":"Course en équipe."},
  {"id":4561,"q":"Qu'est-ce que la peinture à fresque ?","r":"Sur mur humide."},
  {"id":4562,"q":"Qu'est-ce que la tempera ?","r":"Peinture à l'œuf."},
  {"id":4563,"q":"Qu'est-ce que le pastel ?","r":"Craie colorée."},
  {"id":4564,"q":"Qu'est-ce que le fusain ?","r":"Charbon de bois."},
  {"id":4565,"q":"Qu'est-ce que le lavis ?","r":"Encre diluée."},
  {"id":4566,"q":"Qu'est-ce que le pointillisme ?","r":"Peinture par points."},
  {"id":4567,"q":"Qu'est-ce que le fauvisme ?","r":"Couleurs vives."},
  {"id":4568,"q":"Qu'est-ce que le dadaïsme ?","r":"Anti-art."},
  {"id":4569,"q":"Qu'est-ce que le pop art ?","r":"Art de la culture populaire."},
  {"id":4570,"q":"Qu'est-ce que le minimal art ?","r":"Formes épurées."},
  {"id":4571,"q":"Qu'est-ce que le land art ?","r":"Art dans la nature."},
  {"id":4572,"q":"Qu'est-ce que le body art ?","r":"Art sur le corps."},
  {"id":4573,"q":"Qu'est-ce que la calligraphie ?","r":"Art de la belle écriture."},
  {"id":4574,"q":"Qu'est-ce que l'enluminure ?","r":"Décoration de manuscrits."},
  {"id":4575,"q":"Qu'est-ce que la mosaïque ?","r":"Assemblage de tesselles."},
  {"id":4576,"q":"Qu'est-ce que le vitrail ?","r":"Verre coloré."},
  {"id":4577,"q":"Qu'est-ce que la tapisserie ?","r":"Tissu décoratif."},
  {"id":4578,"q":"Qu'est-ce que la broderie ?","r":"Décoration au fil."},
  {"id":4579,"q":"Qu'est-ce que la poterie ?","r":"Art de la terre."},
  {"id":4580,"q":"Qu'est-ce que la verrerie ?","r":"Art du verre."},
  {"id":4581,"q":"Qui a écrit Don Quichotte ?","r":"Cervantès."},
  {"id":4582,"q":"Qui a écrit Guerre et Paix ?","r":"Tolstoï."},
  {"id":4583,"q":"Qui a écrit Crime et Châtiment ?","r":"Dostoïevski."},
  {"id":4584,"q":"Qui a écrit Anna Karénine ?","r":"Tolstoï."},
  {"id":4585,"q":"Qui a écrit Cent ans de solitude ?","r":"García Márquez."},
  {"id":4586,"q":"Qui a écrit L'Odyssée ?","r":"Homère."},
  {"id":4587,"q":"Qui a écrit L'Iliade ?","r":"Homère."},
  {"id":4588,"q":"Qui a écrit L'Énéide ?","r":"Virgile."},
  {"id":4589,"q":"Qui a écrit La Divine Comédie ?","r":"Dante."},
  {"id":4590,"q":"Qui a écrit Les Contes de Canterbury ?","r":"Chaucer."},
  {"id":4591,"q":"Qui a écrit Faust ?","r":"Goethe."},
  {"id":4592,"q":"Qui a écrit Les Souffrances du jeune Werther ?","r":"Goethe."},
  {"id":4593,"q":"Qui a écrit Moby Dick ?","r":"Melville."},
  {"id":4594,"q":"Qui a écrit Les Aventures de Tom Sawyer ?","r":"Mark Twain."},
  {"id":4595,"q":"Qui a écrit Gatsby le Magnifique ?","r":"Fitzgerald."},
  {"id":4596,"q":"Qui a écrit 1984 ?","r":"Orwell."},
  {"id":4597,"q":"Qui a écrit La Ferme des animaux ?","r":"Orwell."},
  {"id":4598,"q":"Qui a écrit Fahrenheit 451 ?","r":"Bradbury."},
  {"id":4599,"q":"Qui a écrit Le Meilleur des mondes ?","r":"Huxley."},
  {"id":4600,"q":"Qui a écrit Dune ?","r":"Frank Herbert."},
  {"id":4601,"q":"Qu'est-ce que la métaphysique ?","r":"Étude de l'être."},
  {"id":4602,"q":"Qu'est-ce que l'ontologie ?","r":"Étude de l'être en tant qu'être."},
  {"id":4603,"q":"Qu'est-ce que l'épistémologie ?","r":"Étude de la connaissance."},
  {"id":4604,"q":"Qu'est-ce que la phénoménologie ?","r":"Étude des phénomènes vécus."},
  {"id":4605,"q":"Qu'est-ce que l'herméneutique ?","r":"Interprétation des textes."},
  {"id":4606,"q":"Qu'est-ce que la dialectique ?","r":"Art du dialogue contradictoire."},
  {"id":4607,"q":"Qu'est-ce que la scolastique ?","r":"Philosophie médiévale."},
  {"id":4608,"q":"Qu'est-ce que la philosophie analytique ?","r":"Philosophie du langage."},
  {"id":4609,"q":"Qu'est-ce que la philosophie continentale ?","r":"Philosophie européenne."},
  {"id":4610,"q":"Qu'est-ce que la déconstruction ?","r":"Critique des oppositions."},
  {"id":4611,"q":"Qu'est-ce que le pragmatisme ?","r":"Vrai = utile."},
  {"id":4612,"q":"Qu'est-ce que l'utilitarisme ?","r":"Maximiser le bonheur."},
  {"id":4613,"q":"Qu'est-ce que le déontologisme ?","r":"Respect des devoirs."},
  {"id":4614,"q":"Qu'est-ce que l'éthique de la vertu ?","r":"Agir selon les vertus."},
  {"id":4615,"q":"Qu'est-ce que le contractualisme ?","r":"Contrat social."},
  {"id":4616,"q":"Qu'est-ce que le libéralisme ?","r":"Libertés individuelles."},
  {"id":4617,"q":"Qu'est-ce que le socialisme ?","r":"Propriété collective."},
  {"id":4618,"q":"Qu'est-ce que le communisme ?","r":"Société sans classes."},
  {"id":4619,"q":"Qu'est-ce que l'anarchisme ?","r":"Absence d'État."},
  {"id":4620,"q":"Qu'est-ce que le conservatisme ?","r":"Préserver les traditions."},
  {"id":4621,"q":"Qu'est-ce que le christianisme ?","r":"Religion de Jésus-Christ."},
  {"id":4622,"q":"Qu'est-ce que l'islam ?","r":"Religion fondée par Mahomet."},
  {"id":4623,"q":"Qu'est-ce que le judaïsme ?","r":"Religion du peuple juif."},
  {"id":4624,"q":"Qu'est-ce que le bouddhisme ?","r":"Religion fondée par Bouddha."},
  {"id":4625,"q":"Qu'est-ce que l'hindouisme ?","r":"Religion polythéiste indienne."},
  {"id":4626,"q":"Qu'est-ce que le sikhisme ?","r":"Religion indienne monothéiste."},
  {"id":4627,"q":"Qu'est-ce que le jaïnisme ?","r":"Religion indienne non-violente."},
  {"id":4628,"q":"Qu'est-ce que le zoroastrisme ?","r":"Religion perse ancienne."},
  {"id":4629,"q":"Qu'est-ce que le shintoïsme ?","r":"Religion japonaise."},
  {"id":4630,"q":"Qu'est-ce que le taoïsme ?","r":"Philosophie chinoise du Tao."},
  {"id":4631,"q":"Qu'est-ce que le confucianisme ?","r":"Philosophie morale chinoise."},
  {"id":4632,"q":"Qu'est-ce que le bahaïsme ?","r":"Religion fondée par Baha'u'llah."},
  {"id":4633,"q":"Qu'est-ce que le rastafari ?","r":"Mouvement jamaïcain."},
  {"id":4634,"q":"Qu'est-ce que le vaudou ?","r":"Religion afro-caribéenne."},
  {"id":4635,"q":"Qu'est-ce que le candomblé ?","r":"Religion afro-brésilienne."},
  {"id":4636,"q":"Qu'est-ce que la santeria ?","r":"Religion afro-cubaine."},
  {"id":4637,"q":"Qu'est-ce que le chamanisme ?","r":"Pratique spirituelle ancestrale."},
  {"id":4638,"q":"Qu'est-ce que le paganisme ?","r":"Religion pré-chrétienne."},
  {"id":4639,"q":"Qu'est-ce que le druidisme ?","r":"Religion celte."},
  {"id":4640,"q":"Qu'est-ce que la mythologie nordique ?","r":"Mythes scandinaves."},
  {"id":4641,"q":"Combien de langues dans le monde ?","r":"Environ 7000."},
  {"id":4642,"q":"Quelle langue est la plus parlée ?","r":"L'anglais ou le mandarin."},
  {"id":4643,"q":"Quelle est la langue la plus native ?","r":"Le mandarin."},
  {"id":4644,"q":"Langue officielle de l'ONU ?","r":"Anglais, français, espagnol, russe, chinois, arabe."},
  {"id":4645,"q":"Langue la plus ancienne encore parlée ?","r":"Le tamoul ou le grec."},
  {"id":4646,"q":"Combien de langues en Afrique ?","r":"Environ 2000."},
  {"id":4647,"q":"Langue officielle du Brésil ?","r":"Le portugais."},
  {"id":4648,"q":"Langue officielle du Canada ?","r":"Anglais et français."},
  {"id":4649,"q":"Langue officielle de la Suisse ?","r":"Allemand, français, italien, romanche."},
  {"id":4650,"q":"Langue officielle de l'Inde ?","r":"Hindi et anglais."},
  {"id":4651,"q":"Qu'est-ce qu'une langue romane ?","r":"Issue du latin."},
  {"id":4652,"q":"Qu'est-ce qu'une langue germanique ?","r":"Famille germanique."},
  {"id":4653,"q":"Qu'est-ce qu'une langue slave ?","r":"Famille slave."},
  {"id":4654,"q":"Qu'est-ce qu'une langue sémitique ?","r":"Arabe, hébreu."},
  {"id":4655,"q":"Qu'est-ce qu'une langue sino-tibétaine ?","r":"Chinois, tibétain."},
  {"id":4656,"q":"Qu'est-ce qu'une langue bantoue ?","r":"Famille africaine."},
  {"id":4657,"q":"Qu'est-ce qu'une langue créole ?","r":"Mélange de langues."},
  {"id":4658,"q":"Qu'est-ce qu'un pidgin ?","r":"Langue de contact simplifiée."},
  {"id":4659,"q":"Qu'est-ce que l'espéranto ?","r":"Langue construite internationale."},
  {"id":4660,"q":"Qui a créé l'espéranto ?","r":"Zamenhof."},
  {"id":4661,"q":"Que signifie « carpe diem » ?","r":"Cueille le jour."},
  {"id":4662,"q":"Que signifie « memento mori » ?","r":"Souviens-toi que tu vas mourir."},
  {"id":4663,"q":"Que signifie « veni vidi vici » ?","r":"Je suis venu, j'ai vu, j'ai vaincu."},
  {"id":4664,"q":"Que signifie « alea jacta est » ?","r":"Le sort en est jeté."},
  {"id":4665,"q":"Que signifie « cogito ergo sum » ?","r":"Je pense donc je suis."},
  {"id":4666,"q":"Que signifie « in vino veritas » ?","r":"La vérité est dans le vin."},
  {"id":4667,"q":"Que signifie « errare humanum est » ?","r":"L'erreur est humaine."},
  {"id":4668,"q":"Que signifie « ex nihilo » ?","r":"À partir de rien."},
  {"id":4669,"q":"Que signifie « ad hoc » ?","r":"Pour cela."},
  {"id":4670,"q":"Que signifie « a priori » ?","r":"Avant expérience."},
  {"id":4671,"q":"Que signifie « a posteriori » ?","r":"Après expérience."},
  {"id":4672,"q":"Que signifie « de facto » ?","r":"De fait."},
  {"id":4673,"q":"Que signifie « de jure » ?","r":"De droit."},
  {"id":4674,"q":"Que signifie « sine qua non » ?","r":"Indispensable."},
  {"id":4675,"q":"Que signifie « status quo » ?","r":"État actuel."},
  {"id":4676,"q":"Que signifie « modus operandi » ?","r":"Mode opératoire."},
  {"id":4677,"q":"Que signifie « mea culpa » ?","r":"Ma faute."},
  {"id":4678,"q":"Que signifie « alter ego » ?","r":"Autre moi."},
  {"id":4679,"q":"Que signifie « persona non grata » ?","r":"Personne indésirable."},
  {"id":4680,"q":"Que signifie « curriculum vitae » ?","r":"Parcours de vie."},
  {"id":4681,"q":"Que signifie « ubiquité » ?","r":"Être partout."},
  {"id":4682,"q":"Que signifie « sagacité » ?","r":"Perspicacité."},
  {"id":4683,"q":"Que signifie « véhémence » ?","r":"Force passionnée."},
  {"id":4684,"q":"Que signifie « obséquieux » ?","r":"Trop flatteur."},
  {"id":4685,"q":"Que signifie « loquace » ?","r":"Bavard."},
  {"id":4686,"q":"Que signifie « taciturne » ?","r":"Silencieux."},
  {"id":4687,"q":"Que signifie « prolixe » ?","r":"Trop long."},
  {"id":4688,"q":"Que signifie « concis » ?","r":"Bref."},
  {"id":4689,"q":"Que signifie « laconique » ?","r":"Très bref."},
  {"id":4690,"q":"Que signifie « vénal » ?","r":"Corruptible."},
  {"id":4691,"q":"Que signifie « magnanime » ?","r":"Généreux."},
  {"id":4692,"q":"Que signifie « pusillanime » ?","r":"Peureux."},
  {"id":4693,"q":"Que signifie « opiniâtre » ?","r":"Persévérant."},
  {"id":4694,"q":"Que signifie « velléitaire » ?","r":"Veut sans agir."},
  {"id":4695,"q":"Que signifie « fastidieux » ?","r":"Ennuyeux."},
  {"id":4696,"q":"Que signifie « méticuleux » ?","r":"Très soigneux."},
  {"id":4697,"q":"Que signifie « insidieux » ?","r":"Trompeur."},
  {"id":4698,"q":"Que signifie « pernicieux » ?","r":"Nuisible."},
  {"id":4699,"q":"Que signifie « ineffable » ?","r":"Indicible."},
  {"id":4700,"q":"Que signifie « indicible » ?","r":"Impossible à dire."},
  {"id":4701,"q":"Homophone : « ver » ?","r":"Ver, verre, vers, vert."},
  {"id":4702,"q":"Homophone : « mer » ?","r":"Mer, mère, maire."},
  {"id":4703,"q":"Homophone : « sot » ?","r":"Sot, seau, saut."},
  {"id":4704,"q":"Homophone : « cent » ?","r":"Cent, sang, sans, sent."},
  {"id":4705,"q":"Homophone : « cour » ?","r":"Cour, cours, court."},
  {"id":4706,"q":"Homophone : « foi » ?","r":"Foi, foie, fois."},
  {"id":4707,"q":"Homophone : « laid » ?","r":"Laid, lait, lai."},
  {"id":4708,"q":"Homophone : « peu » ?","r":"Peu, peux, peut."},
  {"id":4709,"q":"Homophone : « chair » ?","r":"Chair, chaire, cher."},
  {"id":4710,"q":"Homophone : « conte » ?","r":"Conte, compte, comte."},
  {"id":4711,"q":"Homophone : « dans » ?","r":"Dans, dent."},
  {"id":4712,"q":"Homophone : « temps » ?","r":"Temps, tant, t'en."},
  {"id":4713,"q":"Homophone : « ton » ?","r":"Ton, tond, thon."},
  {"id":4714,"q":"Homophone : « sur » ?","r":"Sur, sûr, sure."},
  {"id":4715,"q":"Homophone : « mai » ?","r":"Mai, mais, met, m'est."},
  {"id":4716,"q":"Homophone : « cou » ?","r":"Cou, coup, coût."},
  {"id":4717,"q":"Homophone : « mur » ?","r":"Mur, mûr, mure."},
  {"id":4718,"q":"Homophone : « père » ?","r":"Père, paire, pair."},
  {"id":4719,"q":"Homophone : « voix » ?","r":"Voix, voie, vois."},
  {"id":4720,"q":"Homophone : « sceau » ?","r":"Sceau, seau, sot, saut."},
  {"id":4721,"q":"Conjugaison : être au subjonctif présent ?","r":"Que je sois, que tu sois, qu'il soit."},
  {"id":4722,"q":"Conjugaison : avoir au subjonctif présent ?","r":"Que j'aie, que tu aies, qu'il ait."},
  {"id":4723,"q":"Conjugaison : aller au subjonctif présent ?","r":"Que j'aille, que tu ailles, qu'il aille."},
  {"id":4724,"q":"Conjugaison : faire au subjonctif présent ?","r":"Que je fasse, que tu fasses, qu'il fasse."},
  {"id":4725,"q":"Conjugaison : pouvoir au subjonctif présent ?","r":"Que je puisse, que tu puisses, qu'il puisse."},
  {"id":4726,"q":"Conjugaison : savoir au subjonctif présent ?","r":"Que je sache, que tu saches, qu'il sache."},
  {"id":4727,"q":"Conjugaison : venir au subjonctif présent ?","r":"Que je vienne, que tu viennes, qu'il vienne."},
  {"id":4728,"q":"Conjugaison : prendre au subjonctif présent ?","r":"Que je prenne, que tu prennes, qu'il prenne."},
  {"id":4729,"q":"Conjugaison : voir au subjonctif présent ?","r":"Que je voie, que tu voies, qu'il voie."},
  {"id":4730,"q":"Conjugaison : dire au subjonctif présent ?","r":"Que je dise, que tu dises, qu'il dise."},
  {"id":4731,"q":"Conjugaison : être au conditionnel présent ?","r":"Je serais, tu serais, il serait."},
  {"id":4732,"q":"Conjugaison : avoir au conditionnel présent ?","r":"J'aurais, tu aurais, il aurait."},
  {"id":4733,"q":"Conjugaison : aller au conditionnel présent ?","r":"J'irais, tu irais, il irait."},
  {"id":4734,"q":"Conjugaison : faire au conditionnel présent ?","r":"Je ferais, tu ferais, il ferait."},
  {"id":4735,"q":"Conjugaison : pouvoir au conditionnel présent ?","r":"Je pourrais, tu pourrais, il pourrait."},
  {"id":4736,"q":"Conjugaison : vouloir au conditionnel présent ?","r":"Je voudrais, tu voudrais, il voudrait."},
  {"id":4737,"q":"Conjugaison : être au passé simple ?","r":"Je fus, tu fus, il fut."},
  {"id":4738,"q":"Conjugaison : avoir au passé simple ?","r":"J'eus, tu eus, il eut."},
  {"id":4739,"q":"Conjugaison : aller au passé simple ?","r":"J'allai, tu allas, il alla."},
  {"id":4740,"q":"Conjugaison : faire au passé simple ?","r":"Je fis, tu fis, il fit."},
  {"id":4741,"q":"Qu'est-ce qu'une proposition principale ?","r":"Proposition autonome."},
  {"id":4742,"q":"Qu'est-ce qu'une proposition subordonnée relative ?","r":"Introduite par un pronom relatif."},
  {"id":4743,"q":"Qu'est-ce qu'une subordonnée conjonctive ?","r":"Introduite par « que »."},
  {"id":4744,"q":"Qu'est-ce qu'une subordonnée circonstancielle ?","r":"Exprime temps, cause, but…"},
  {"id":4745,"q":"Qu'est-ce qu'une proposition infinitive ?","r":"Verbe à l'infinitif."},
  {"id":4746,"q":"Qu'est-ce qu'une proposition participiale ?","r":"Participe avec sujet propre."},
  {"id":4747,"q":"Qu'est-ce qu'une phrase déclarative ?","r":"Énonce un fait."},
  {"id":4748,"q":"Qu'est-ce qu'une phrase interrogative ?","r":"Pose une question."},
  {"id":4749,"q":"Qu'est-ce qu'une phrase exclamative ?","r":"Exprime une émotion."},
  {"id":4750,"q":"Qu'est-ce qu'une phrase impérative ?","r":"Donne un ordre."},
  {"id":4751,"q":"Quand utilise-t-on le point-virgule ?","r":"Sépare des propositions liées."},
  {"id":4752,"q":"Quand utilise-t-on les deux-points ?","r":"Introduit une explication."},
  {"id":4753,"q":"Quand utilise-t-on les guillemets ?","r":"Pour citer."},
  {"id":4754,"q":"Quand utilise-t-on les parenthèses ?","r":"Pour une précision."},
  {"id":4755,"q":"Quand utilise-t-on les tirets ?","r":"Pour un dialogue."},
  {"id":4756,"q":"Quand utilise-t-on les points de suspension ?","r":"Pour une hésitation."},
  {"id":4757,"q":"Quand utilise-t-on le point d'interrogation ?","r":"Pour une question."},
  {"id":4758,"q":"Quand utilise-t-on le point d'exclamation ?","r":"Pour une exclamation."},
  {"id":4759,"q":"Quand utilise-t-on la virgule ?","r":"Pour séparer des éléments."},
  {"id":4760,"q":"Quand utilise-t-on le point ?","r":"Pour finir une phrase."},
  {"id":4761,"q":"Qu'est-ce qu'une anaphore ?","r":"Répétition en début de phrase."},
  {"id":4762,"q":"Qu'est-ce qu'une épiphore ?","r":"Répétition en fin de phrase."},
  {"id":4763,"q":"Qu'est-ce qu'un chiasme ?","r":"Structure croisée ABBA."},
  {"id":4764,"q":"Qu'est-ce qu'une gradation ?","r":"Progression d'intensité."},
  {"id":4765,"q":"Qu'est-ce qu'une hyperbole ?","r":"Exagération."},
  {"id":4766,"q":"Qu'est-ce qu'une litote ?","r":"Dire moins pour suggérer plus."},
  {"id":4767,"q":"Qu'est-ce qu'un euphémisme ?","r":"Adoucir une idée."},
  {"id":4768,"q":"Qu'est-ce qu'une métonymie ?","r":"Remplacer par un terme lié."},
  {"id":4769,"q":"Qu'est-ce qu'une synecdoque ?","r":"Partie pour le tout."},
  {"id":4770,"q":"Qu'est-ce qu'une périphrase ?","r":"Dire en plusieurs mots."},
  {"id":4771,"q":"Qu'est-ce qu'une allitération ?","r":"Répétition de consonnes."},
  {"id":4772,"q":"Qu'est-ce qu'une assonance ?","r":"Répétition de voyelles."},
  {"id":4773,"q":"Qu'est-ce qu'un oxymore ?","r":"Deux mots opposés."},
  {"id":4774,"q":"Qu'est-ce qu'une antithèse ?","r":"Opposition de deux idées."},
  {"id":4775,"q":"Qu'est-ce qu'un parallélisme ?","r":"Structures semblables."},
  {"id":4776,"q":"Qu'est-ce qu'une personnification ?","r":"Attribuer des traits humains."},
  {"id":4777,"q":"Qu'est-ce qu'une allégorie ?","r":"Représentation symbolique."},
  {"id":4778,"q":"Qu'est-ce qu'une prosopopée ?","r":"Faire parler un absent."},
  {"id":4779,"q":"Qu'est-ce qu'une prétérition ?","r":"Dire en prétendant ne pas dire."},
  {"id":4780,"q":"Qu'est-ce qu'une hypotypose ?","r":"Description vivante."},
  {"id":4781,"q":"Qu'est-ce qu'un roman épistolaire ?","r":"Roman par lettres."},
  {"id":4782,"q":"Qu'est-ce qu'un roman d'apprentissage ?","r":"Formation d'un personnage."},
  {"id":4783,"q":"Qu'est-ce qu'un roman historique ?","r":"Fiction dans l'Histoire."},
  {"id":4784,"q":"Qu'est-ce qu'un roman policier ?","r":"Enquête criminelle."},
  {"id":4785,"q":"Qu'est-ce qu'un roman d'aventures ?","r":"Péripéties."},
  {"id":4786,"q":"Qu'est-ce qu'un roman de science-fiction ?","r":"Anticipation."},
  {"id":4787,"q":"Qu'est-ce qu'un roman fantastique ?","r":"Surnaturel dans le réel."},
  {"id":4788,"q":"Qu'est-ce qu'un roman de fantasy ?","r":"Monde imaginaire."},
  {"id":4789,"q":"Qu'est-ce qu'une autofiction ?","r":"Récit de soi romancé."},
  {"id":4790,"q":"Qu'est-ce qu'une nouvelle ?","r":"Récit court."},
  {"id":4791,"q":"Qu'est-ce qu'un conte ?","r":"Récit merveilleux."},
  {"id":4792,"q":"Qu'est-ce qu'une fable ?","r":"Récit moral."},
  {"id":4793,"q":"Qu'est-ce qu'une parabole ?","r":"Récit allégorique."},
  {"id":4794,"q":"Qu'est-ce qu'un mythe ?","r":"Récit fondateur."},
  {"id":4795,"q":"Qu'est-ce qu'une légende ?","r":"Récit traditionnel."},
  {"id":4796,"q":"Qu'est-ce qu'une épopée ?","r":"Long poème héroïque."},
  {"id":4797,"q":"Qu'est-ce qu'un essai ?","r":"Réflexion argumentée."},
  {"id":4798,"q":"Qu'est-ce qu'une biographie ?","r":"Vie d'un personnage."},
  {"id":4799,"q":"Qu'est-ce qu'une autobiographie ?","r":"Vie racontée par soi."},
  {"id":4800,"q":"Qu'est-ce qu'un mémoire ?","r":"Récit d'expérience."},
  {"id":4801,"q":"Qu'est-ce qu'un sonnet ?","r":"Poème de 14 vers."},
  {"id":4802,"q":"Qu'est-ce qu'un haïku ?","r":"Poème japonais de 3 vers."},
  {"id":4803,"q":"Qu'est-ce qu'un tanka ?","r":"Poème japonais de 5 vers."},
  {"id":4804,"q":"Qu'est-ce qu'une ode ?","r":"Poème lyrique."},
  {"id":4805,"q":"Qu'est-ce qu'une élégie ?","r":"Poème de deuil."},
  {"id":4806,"q":"Qu'est-ce qu'une ballade ?","r":"Poème médiéval."},
  {"id":4807,"q":"Qu'est-ce qu'un rondeau ?","r":"Poème à refrain."},
  {"id":4808,"q":"Qu'est-ce qu'un acrostiche ?","r":"Initiales forment un mot."},
  {"id":4809,"q":"Qu'est-ce qu'un calligramme ?","r":"Poème en forme d'image."},
  {"id":4810,"q":"Qu'est-ce que le vers libre ?","r":"Sans mètre fixe."},
  {"id":4811,"q":"Qu'est-ce qu'un alexandrin ?","r":"Vers de 12 syllabes."},
  {"id":4812,"q":"Qu'est-ce qu'un décasyllabe ?","r":"Vers de 10 syllabes."},
  {"id":4813,"q":"Qu'est-ce qu'un octosyllabe ?","r":"Vers de 8 syllabes."},
  {"id":4814,"q":"Qu'est-ce qu'une rime plate ?","r":"AABB."},
  {"id":4815,"q":"Qu'est-ce qu'une rime croisée ?","r":"ABAB."},
  {"id":4816,"q":"Qu'est-ce qu'une rime embrassée ?","r":"ABBA."},
  {"id":4817,"q":"Qu'est-ce qu'une strophe ?","r":"Groupe de vers."},
  {"id":4818,"q":"Qu'est-ce qu'un quatrain ?","r":"4 vers."},
  {"id":4819,"q":"Qu'est-ce qu'un tercet ?","r":"3 vers."},
  {"id":4820,"q":"Qu'est-ce qu'un distique ?","r":"2 vers."},
  {"id":4821,"q":"Qu'est-ce qu'une tragédie ?","r":"Pièce dramatique."},
  {"id":4822,"q":"Qu'est-ce qu'une comédie ?","r":"Pièce drôle."},
  {"id":4823,"q":"Qu'est-ce qu'un drame ?","r":"Pièce sérieuse."},
  {"id":4824,"q":"Qu'est-ce qu'une farce ?","r":"Comédie burlesque."},
  {"id":4825,"q":"Qu'est-ce qu'un vaudeville ?","r":"Comédie légère."},
  {"id":4826,"q":"Qu'est-ce qu'un monologue ?","r":"Discours seul."},
  {"id":4827,"q":"Qu'est-ce qu'un aparté ?","r":"Parole à part."},
  {"id":4828,"q":"Qu'est-ce qu'une didascalie ?","r":"Indication de mise en scène."},
  {"id":4829,"q":"Qu'est-ce qu'une réplique ?","r":"Réponse d'un personnage."},
  {"id":4830,"q":"Qu'est-ce qu'un acte ?","r":"Grande partie d'une pièce."},
  {"id":4831,"q":"Qu'est-ce qu'une scène ?","r":"Partie d'un acte."},
  {"id":4832,"q":"Qu'est-ce que la règle des 3 unités ?","r":"Temps, lieu, action."},
  {"id":4833,"q":"Qu'est-ce que le dénouement ?","r":"Fin de l'intrigue."},
  {"id":4834,"q":"Qu'est-ce que le nœud ?","r":"Complication."},
  {"id":4835,"q":"Qu'est-ce que l'exposition ?","r":"Début d'une pièce."},
  {"id":4836,"q":"Qu'est-ce qu'un quiproquo ?","r":"Malentendu comique."},
  {"id":4837,"q":"Qu'est-ce qu'un deus ex machina ?","r":"Résolution artificielle."},
  {"id":4838,"q":"Qu'est-ce que le théâtre de l'absurde ?","r":"Genre du 20e siècle."},
  {"id":4839,"q":"Qu'est-ce que la commedia dell'arte ?","r":"Théâtre italien improvisé."},
  {"id":4840,"q":"Qu'est-ce que le théâtre nô ?","r":"Théâtre japonais."},
  {"id":4841,"q":"Qui a réalisé Psychose ?","r":"Hitchcock."},
  {"id":4842,"q":"Qui a réalisé Les Oiseaux ?","r":"Hitchcock."},
  {"id":4843,"q":"Qui a réalisé Sueurs froides ?","r":"Hitchcock."},
  {"id":4844,"q":"Qui a réalisé 2001 : L'Odyssée de l'espace ?","r":"Kubrick."},
  {"id":4845,"q":"Qui a réalisé Orange mécanique ?","r":"Kubrick."},
  {"id":4846,"q":"Qui a réalisé Shining ?","r":"Kubrick."},
  {"id":4847,"q":"Qui a réalisé Taxi Driver ?","r":"Scorsese."},
  {"id":4848,"q":"Qui a réalisé Les Affranchis ?","r":"Scorsese."},
  {"id":4849,"q":"Qui a réalisé Le Parrain ?","r":"Coppola."},
  {"id":4850,"q":"Qui a réalisé Apocalypse Now ?","r":"Coppola."},
  {"id":4851,"q":"Qui a réalisé Star Wars ?","r":"George Lucas."},
  {"id":4852,"q":"Qui a réalisé Indiana Jones ?","r":"Spielberg."},
  {"id":4853,"q":"Qui a réalisé Jurassic Park ?","r":"Spielberg."},
  {"id":4854,"q":"Qui a réalisé E.T. ?","r":"Spielberg."},
  {"id":4855,"q":"Qui a réalisé Titanic ?","r":"James Cameron."},
  {"id":4856,"q":"Qui a réalisé Avatar ?","r":"James Cameron."},
  {"id":4857,"q":"Qui a réalisé Inception ?","r":"Christopher Nolan."},
  {"id":4858,"q":"Qui a réalisé Interstellar ?","r":"Christopher Nolan."},
  {"id":4859,"q":"Qui a réalisé Dunkerque ?","r":"Christopher Nolan."},
  {"id":4860,"q":"Qui a réalisé Oppenheimer ?","r":"Christopher Nolan."},
  {"id":4861,"q":"Quelle est la série la plus longue ?","r":"Les Simpson ou Hôpital central."},
  {"id":4862,"q":"Quelle série se déroule à Westeros ?","r":"Game of Thrones."},
  {"id":4863,"q":"Quelle série sur la Casa de Papel ?","r":"La Casa de Papel."},
  {"id":4864,"q":"Quelle série sur des ados à Hawkins ?","r":"Stranger Things."},
  {"id":4865,"q":"Quelle série sur la famille Roy ?","r":"Succession."},
  {"id":4866,"q":"Quelle série sur Walter White ?","r":"Breaking Bad."},
  {"id":4867,"q":"Quelle série sur un prof de chimie devenu dealer ?","r":"Breaking Bad."},
  {"id":4868,"q":"Quelle série sur des avocats à Albuquerque ?","r":"Better Call Saul."},
  {"id":4869,"q":"Quelle série sur un trône de fer ?","r":"Game of Thrones."},
  {"id":4870,"q":"Quelle série sur des ados à Las Encinas ?","r":"Élite."},
  {"id":4871,"q":"Quelle série sur le braquage de la Fabrique ?","r":"La Casa de Papel."},
  {"id":4872,"q":"Quelle série sur un scientifique dans un garage ?","r":"Rick et Morty."},
  {"id":4873,"q":"Quelle série animée sur une famille jaune ?","r":"Les Simpson."},
  {"id":4874,"q":"Quelle série animée sur un chien bleu ?","r":"Bluey."},
  {"id":4875,"q":"Quelle série sur Sherlock Holmes ?","r":"Sherlock."},
  {"id":4876,"q":"Quelle série sur la monarchie anglaise ?","r":"The Crown."},
  {"id":4877,"q":"Quelle série sur des ados à Beverly Hills ?","r":"Beverly Hills."},
  {"id":4878,"q":"Quelle série sur des médecins à Seattle ?","r":"Grey's Anatomy."},
  {"id":4879,"q":"Quelle série sur un hacker ?","r":"Mr. Robot."},
  {"id":4880,"q":"Quelle série sur la finance à Wall Street ?","r":"Billions."},
  {"id":4881,"q":"Qui a composé la 5e Symphonie ?","r":"Beethoven."},
  {"id":4882,"q":"Qui a composé la 9e Symphonie ?","r":"Beethoven."},
  {"id":4883,"q":"Qui a composé La Symphonie n°40 ?","r":"Mozart."},
  {"id":4884,"q":"Qui a composé La Flûte enchantée ?","r":"Mozart."},
  {"id":4885,"q":"Qui a composé Les Noces de Figaro ?","r":"Mozart."},
  {"id":4886,"q":"Qui a composé Don Giovanni ?","r":"Mozart."},
  {"id":4887,"q":"Qui a composé La Traviata ?","r":"Verdi."},
  {"id":4888,"q":"Qui a composé Aïda ?","r":"Verdi."},
  {"id":4889,"q":"Qui a composé Nabucco ?","r":"Verdi."},
  {"id":4890,"q":"Qui a composé Carmen ?","r":"Bizet."},
  {"id":4891,"q":"Qui a composé La Bohème ?","r":"Puccini."},
  {"id":4892,"q":"Qui a composé Madame Butterfly ?","r":"Puccini."},
  {"id":4893,"q":"Qui a composé Turandot ?","r":"Puccini."},
  {"id":4894,"q":"Qui a composé Les Quatre Saisons ?","r":"Vivaldi."},
  {"id":4895,"q":"Qui a composé Le Messie ?","r":"Haendel."},
  {"id":4896,"q":"Qui a composé La Passion selon Saint Matthieu ?","r":"Bach."},
  {"id":4897,"q":"Qui a composé Le Clavier bien tempéré ?","r":"Bach."},
  {"id":4898,"q":"Qui a composé Le Lac des cygnes ?","r":"Tchaïkovski."},
  {"id":4899,"q":"Qui a composé Casse-Noisette ?","r":"Tchaïkovski."},
  {"id":4900,"q":"Qui a composé La Belle au bois dormant ?","r":"Tchaïkovski."},
  {"id":4901,"q":"Qui a composé Le Boléro ?","r":"Ravel."},
  {"id":4902,"q":"Qui a composé La Mer ?","r":"Debussy."},
  {"id":4903,"q":"Qui a composé Clair de lune ?","r":"Debussy."},
  {"id":4904,"q":"Qui a composé Carmen ?","r":"Bizet."},
  {"id":4905,"q":"Qui a composé La Danse macabre ?","r":"Saint-Saëns."},
  {"id":4906,"q":"Qui a composé Le Carnaval des animaux ?","r":"Saint-Saëns."},
  {"id":4907,"q":"Qui a composé Peer Gynt ?","r":"Grieg."},
  {"id":4908,"q":"Qui a composé La Moldau ?","r":"Smetana."},
  {"id":4909,"q":"Qui a composé Les Danses slaves ?","r":"Dvořák."},
  {"id":4910,"q":"Qui a composé La Symphonie du Nouveau Monde ?","r":"Dvořák."},
  {"id":4911,"q":"Qui a composé Rhapsody in Blue ?","r":"Gershwin."},
  {"id":4912,"q":"Qui a composé Un Américain à Paris ?","r":"Gershwin."},
  {"id":4913,"q":"Qui a composé Pierre et le Loup ?","r":"Prokofiev."},
  {"id":4914,"q":"Qui a composé Roméo et Juliette ?","r":"Prokofiev."},
  {"id":4915,"q":"Qui a composé Le Sacre du printemps ?","r":"Stravinsky."},
  {"id":4916,"q":"Qui a composé L'Oiseau de feu ?","r":"Stravinsky."},
  {"id":4917,"q":"Qui a composé Petrouchka ?","r":"Stravinsky."},
  {"id":4918,"q":"Qui a composé La Veuve joyeuse ?","r":"Lehár."},
  {"id":4919,"q":"Qui a composé La Chauve-souris ?","r":"Strauss."},
  {"id":4920,"q":"Qui a composé Le Beau Danube bleu ?","r":"Strauss."},
  {"id":4921,"q":"Quel instrument est la kora ?","r":"Harpe africaine."},
  {"id":4922,"q":"Quel instrument est le djembé ?","r":"Tambour africain."},
  {"id":4923,"q":"Quel instrument est le balafon ?","r":"Xylophone africain."},
  {"id":4924,"q":"Quel instrument est le kora ?","r":"Harpe-luth africaine."},
  {"id":4925,"q":"Quel instrument est le oud ?","r":"Luth arabe."},
  {"id":4926,"q":"Quel instrument est le sitar ?","r":"Cithare indienne."},
  {"id":4927,"q":"Quel instrument est le tabla ?","r":"Percussion indienne."},
  {"id":4928,"q":"Quel instrument est le shamisen ?","r":"Luth japonais."},
  {"id":4929,"q":"Quel instrument est le koto ?","r":"Cithare japonaise."},
  {"id":4930,"q":"Quel instrument est le guzheng ?","r":"Cithare chinoise."},
  {"id":4931,"q":"Quel instrument est l'erhu ?","r":"Violon chinois."},
  {"id":4932,"q":"Quel instrument est le didgeridoo ?","r":"Instrument aborigène."},
  {"id":4933,"q":"Quel instrument est la cornemuse ?","r":"Instrument écossais."},
  {"id":4934,"q":"Quel instrument est l'accordéon ?","r":"Instrument à soufflet."},
  {"id":4935,"q":"Quel instrument est l'harmonica ?","r":"Petit instrument à vent."},
  {"id":4936,"q":"Quel instrument est le saxophone ?","r":"Instrument à anche."},
  {"id":4937,"q":"Quel instrument est le marimba ?","r":"Percussion bois."},
  {"id":4938,"q":"Quel instrument est le vibraphone ?","r":"Percussion métallique."},
  {"id":4939,"q":"Quel instrument est le xylophone ?","r":"Percussion à lames."},
  {"id":4940,"q":"Quel instrument est le glockenspiel ?","r":"Carillon à lames."},
  {"id":4941,"q":"Quelle danse vient d'Argentine ?","r":"Le tango."},
  {"id":4942,"q":"Quelle danse vient du Brésil ?","r":"La samba."},
  {"id":4943,"q":"Quelle danse vient de Cuba ?","r":"Le cha-cha-cha."},
  {"id":4944,"q":"Quelle danse vient d'Espagne ?","r":"Le flamenco."},
  {"id":4945,"q":"Quelle danse vient d'Irlande ?","r":"La gigue."},
  {"id":4946,"q":"Quelle danse vient d'Écosse ?","r":"La highland dance."},
  {"id":4947,"q":"Quelle danse vient de Pologne ?","r":"La mazurka."},
  {"id":4948,"q":"Quelle danse vient de République tchèque ?","r":"La polka."},
  {"id":4949,"q":"Quelle danse vient de Vienne ?","r":"La valse."},
  {"id":4950,"q":"Quelle danse vient de Nouvelle-Zélande ?","r":"Le haka."},
  {"id":4951,"q":"Quelle danse vient de Hawaï ?","r":"Le hula."},
  {"id":4952,"q":"Quelle danse vient de Grèce ?","r":"Le sirtaki."},
  {"id":4953,"q":"Quelle danse vient de Turquie ?","r":"La danse du ventre."},
  {"id":4954,"q":"Quelle danse vient de Russie ?","r":"La kazatchok."},
  {"id":4955,"q":"Quelle danse vient des États-Unis ?","r":"Le swing."},
  {"id":4956,"q":"Quelle danse vient de France ?","r":"Le menuet ou la bourrée."},
  {"id":4957,"q":"Quelle danse vient du Sénégal ?","r":"Le sabar."},
  {"id":4958,"q":"Quelle danse vient du Cameroun ?","r":"Le bikutsi."},
  {"id":4959,"q":"Quelle danse vient du Nigeria ?","r":"L'azonto."},
  {"id":4960,"q":"Quelle danse vient d'Afrique du Sud ?","r":"Le pantsula."},
  {"id":4961,"q":"Quelle cuisine utilise le curry ?","r":"Indienne."},
  {"id":4962,"q":"Quelle cuisine utilise le wasabi ?","r":"Japonaise."},
  {"id":4963,"q":"Quelle cuisine utilise le kimchi ?","r":"Coréenne."},
  {"id":4964,"q":"Quelle cuisine utilise le miso ?","r":"Japonaise."},
  {"id":4965,"q":"Quelle cuisine utilise la sauce soja ?","r":"Asiatique."},
  {"id":4966,"q":"Quelle cuisine utilise le tahini ?","r":"Moyen-orientale."},
  {"id":4967,"q":"Quelle cuisine utilise le harissa ?","r":"Tunisienne."},
  {"id":4968,"q":"Quelle cuisine utilise le ras el-hanout ?","r":"Marocaine."},
  {"id":4969,"q":"Quelle cuisine utilise le sumac ?","r":"Libanaise."},
  {"id":4970,"q":"Quelle cuisine utilise le zaatar ?","r":"Levantine."},
  {"id":4971,"q":"Quelle cuisine utilise la mozzarella ?","r":"Italienne."},
  {"id":4972,"q":"Quelle cuisine utilise la feta ?","r":"Grecque."},
  {"id":4973,"q":"Quelle cuisine utilise le gouda ?","r":"Néerlandaise."},
  {"id":4974,"q":"Quelle cuisine utilise le cheddar ?","r":"Anglaise."},
  {"id":4975,"q":"Quelle cuisine utilise le parmesan ?","r":"Italienne."},
  {"id":4976,"q":"Quelle cuisine utilise le piment ?","r":"Mexicaine."},
  {"id":4977,"q":"Quelle cuisine utilise la harissa ?","r":"Maghrébine."},
  {"id":4978,"q":"Quelle cuisine utilise la noix de coco ?","r":"Thaïlandaise."},
  {"id":4979,"q":"Quelle cuisine utilise la banane plantain ?","r":"Africaine."},
  {"id":4980,"q":"Quelle cuisine utilise le manioc ?","r":"Africaine et sud-américaine."},
  {"id":4981,"q":"Qu'est-ce qu'un macaron ?","r":"Pâtisserie française."},
  {"id":4982,"q":"Qu'est-ce qu'un éclair ?","r":"Pâtisserie au chocolat."},
  {"id":4983,"q":"Qu'est-ce qu'un mille-feuille ?","r":"Pâtisserie feuilletée."},
  {"id":4984,"q":"Qu'est-ce qu'un Paris-Brest ?","r":"Pâtisserie à la praline."},
  {"id":4985,"q":"Qu'est-ce qu'un Saint-Honoré ?","r":"Pâtisserie à la crème."},
  {"id":4986,"q":"Qu'est-ce qu'un croissant ?","r":"Viennoiserie."},
  {"id":4987,"q":"Qu'est-ce qu'un pain au chocolat ?","r":"Viennoiserie."},
  {"id":4988,"q":"Qu'est-ce qu'une brioche ?","r":"Pain sucré."},
  {"id":4989,"q":"Qu'est-ce qu'un chausson aux pommes ?","r":"Pâtisserie feuilletée."},
  {"id":4990,"q":"Qu'est-ce qu'une tarte tatin ?","r":"Tarte renversée."},
  {"id":4991,"q":"Qu'est-ce qu'une crème brûlée ?","r":"Dessert caramélisé."},
  {"id":4992,"q":"Qu'est-ce qu'une mousse au chocolat ?","r":"Dessert aérien."},
  {"id":4993,"q":"Qu'est-ce qu'un flan ?","r":"Dessert crémeux."},
  {"id":4994,"q":"Qu'est-ce qu'un clafoutis ?","r":"Dessert aux cerises."},
  {"id":4995,"q":"Qu'est-ce qu'un far breton ?","r":"Dessert aux pruneaux."},
  {"id":4996,"q":"Qu'est-ce qu'une crêpe Suzette ?","r":"Crêpe flambée."},
  {"id":4997,"q":"Qu'est-ce qu'un baba au rhum ?","r":"Pâtisserie imbibée."},
  {"id":4998,"q":"Qu'est-ce qu'une religieuse ?","r":"Pâtisserie au chocolat."},
  {"id":4999,"q":"Qu'est-ce qu'un opéra ?","r":"Gâteau au café."},
  {"id":5000,"q":"Qu'est-ce qu'un fraisier ?","r":"Gâteau aux fraises."},
  {"id":5001,"q":"Qu'est-ce qu'un vin rouge ?","r":"Vin de raisins rouges."},
  {"id":5002,"q":"Qu'est-ce qu'un vin blanc ?","r":"Vin de raisins blancs."},
  {"id":5003,"q":"Qu'est-ce qu'un rosé ?","r":"Vin rosé."},
  {"id":5004,"q":"Qu'est-ce qu'un champagne ?","r":"Vin effervescent français."},
  {"id":5005,"q":"Qu'est-ce qu'un prosecco ?","r":"Vin effervescent italien."},
  {"id":5006,"q":"Qu'est-ce qu'un cava ?","r":"Vin effervescent espagnol."},
  {"id":5007,"q":"Qu'est-ce qu'un porto ?","r":"Vin muté portugais."},
  {"id":5008,"q":"Qu'est-ce qu'un xérès ?","r":"Vin muté espagnol."},
  {"id":5009,"q":"Qu'est-ce qu'un madère ?","r":"Vin muté portugais."},
  {"id":5010,"q":"Qu'est-ce qu'un whisky ?","r":"Alcool de céréales."},
  {"id":5011,"q":"Qu'est-ce qu'un scotch ?","r":"Whisky écossais."},
  {"id":5012,"q":"Qu'est-ce qu'un bourbon ?","r":"Whisky américain."},
  {"id":5013,"q":"Qu'est-ce qu'un cognac ?","r":"Eau-de-vie française."},
  {"id":5014,"q":"Qu'est-ce qu'un armagnac ?","r":"Eau-de-vie gasconne."},
  {"id":5015,"q":"Qu'est-ce qu'un calvados ?","r":"Eau-de-vie normande."},
  {"id":5016,"q":"Qu'est-ce qu'une vodka ?","r":"Alcool neutre."},
  {"id":5017,"q":"Qu'est-ce qu'un gin ?","r":"Alcool aromatisé au genévrier."},
  {"id":5018,"q":"Qu'est-ce qu'un rhum ?","r":"Alcool de canne à sucre."},
  {"id":5019,"q":"Qu'est-ce qu'une tequila ?","r":"Alcool mexicain."},
  {"id":5020,"q":"Qu'est-ce qu'un mezcal ?","r":"Alcool mexicain."},
  {"id":5021,"q":"Qu'est-ce qu'un camembert ?","r":"Fromage normand."},
  {"id":5022,"q":"Qu'est-ce qu'un brie ?","r":"Fromage de Brie."},
  {"id":5023,"q":"Qu'est-ce qu'un roquefort ?","r":"Fromage bleu."},
  {"id":5024,"q":"Qu'est-ce qu'un bleu d'Auvergne ?","r":"Fromage bleu."},
  {"id":5025,"q":"Qu'est-ce qu'un comté ?","r":"Fromage du Jura."},
  {"id":5026,"q":"Qu'est-ce qu'un beaufort ?","r":"Fromage savoyard."},
  {"id":5027,"q":"Qu'est-ce qu'un reblochon ?","r":"Fromage savoyard."},
  {"id":5028,"q":"Qu'est-ce qu'un munster ?","r":"Fromage alsacien."},
  {"id":5029,"q":"Qu'est-ce qu'un chèvre ?","r":"Fromage de chèvre."},
  {"id":5030,"q":"Qu'est-ce qu'un crottin de Chavignol ?","r":"Fromage de chèvre."},
  {"id":5031,"q":"Qu'est-ce qu'un parmesan ?","r":"Fromage italien AOP."},
  {"id":5032,"q":"Qu'est-ce qu'un pecorino ?","r":"Fromage italien de brebis."},
  {"id":5033,"q":"Qu'est-ce qu'un gorgonzola ?","r":"Fromage italien bleu."},
  {"id":5034,"q":"Qu'est-ce qu'un gouda ?","r":"Fromage néerlandais."},
  {"id":5035,"q":"Qu'est-ce qu'un edam ?","r":"Fromage néerlandais."},
  {"id":5036,"q":"Qu'est-ce qu'un cheddar ?","r":"Fromage anglais."},
  {"id":5037,"q":"Qu'est-ce qu'un stilton ?","r":"Fromage anglais bleu."},
  {"id":5038,"q":"Qu'est-ce qu'un manchego ?","r":"Fromage espagnol."},
  {"id":5039,"q":"Qu'est-ce qu'une feta ?","r":"Fromage grec."},
  {"id":5040,"q":"Qu'est-ce qu'un halloumi ?","r":"Fromage chypriote."},
  {"id":5041,"q":"Quel est le plus grand animal marin ?","r":"La baleine bleue."},
  {"id":5042,"q":"Quel est le plus grand poisson ?","r":"Le requin-baleine."},
  {"id":5043,"q":"Quel est le plus rapide poisson ?","r":"Le voilier."},
  {"id":5044,"q":"Quel est le plus venimeux marin ?","r":"La cuboméduse."},
  {"id":5045,"q":"Quel est le plus intelligent marin ?","r":"Le dauphin."},
  {"id":5046,"q":"Quel est le plus long céphalopode ?","r":"Le calmar colossal."},
  {"id":5047,"q":"Quel est le plus vieux poisson ?","r":"Le cœlacanthe."},
  {"id":5048,"q":"Quel est le plus profond ?","r":"Le poisson-limace."},
  {"id":5049,"q":"Quel est le plus coloré ?","r":"Le poisson-mandarin."},
  {"id":5050,"q":"Quel est le plus dangereux ?","r":"Le requin-bouledogue."},
  {"id":5051,"q":"Quel est le plus grand oiseau ?","r":"L'autruche."},
  {"id":5052,"q":"Quel est le plus petit oiseau ?","r":"Le colibri."},
  {"id":5053,"q":"Quel est le plus rapide oiseau ?","r":"Le faucon pèlerin."},
  {"id":5054,"q":"Quel oiseau vole le plus haut ?","r":"Le vautour de Rüppell."},
  {"id":5055,"q":"Quel oiseau ne vole pas ?","r":"L'autruche, le manchot."},
  {"id":5056,"q":"Quel oiseau imite la voix ?","r":"Le perroquet."},
  {"id":5057,"q":"Quel oiseau est le plus coloré ?","r":"Le paon."},
  {"id":5058,"q":"Quel oiseau chante le mieux ?","r":"Le rossignol."},
  {"id":5059,"q":"Quel oiseau vit au pôle Sud ?","r":"Le manchot empereur."},
  {"id":5060,"q":"Quel oiseau vit au pôle Nord ?","r":"Le harfang des neiges."},
  {"id":5061,"q":"Quel est le plus grand insecte ?","r":"Le phasme géant."},
  {"id":5062,"q":"Quel insecte est le plus fort ?","r":"Le scarabée rhinocéros."},
  {"id":5063,"q":"Quel insecte vit le plus longtemps ?","r":"La reine termite."},
  {"id":5064,"q":"Quel insecte pique ?","r":"Le moustique, la guêpe."},
  {"id":5065,"q":"Quel insecte produit du miel ?","r":"L'abeille."},
  {"id":5066,"q":"Quel insecte produit de la soie ?","r":"Le ver à soie."},
  {"id":5067,"q":"Quel insecte est social ?","r":"La fourmi, l'abeille."},
  {"id":5068,"q":"Quel insecte se métamorphose ?","r":"Le papillon."},
  {"id":5069,"q":"Quel insecte saute ?","r":"La sauterelle, la puce."},
  {"id":5070,"q":"Quel insecte brille la nuit ?","r":"La luciole."},
  {"id":5071,"q":"Quel est le plus grand reptile ?","r":"Le crocodile marin."},
  {"id":5072,"q":"Quel est le plus long serpent ?","r":"Le python réticulé."},
  {"id":5073,"q":"Quel est le plus venimeux terrestre ?","r":"Le taïpan du désert."},
  {"id":5074,"q":"Quel est le plus vieux reptile ?","r":"La tortue géante."},
  {"id":5075,"q":"Quel reptile change de couleur ?","r":"Le caméléon."},
  {"id":5076,"q":"Quel reptile vole ?","r":"Le lézard volant (draco)."},
  {"id":5077,"q":"Quel reptile vit dans l'eau ?","r":"Le crocodile, la tortue marine."},
  {"id":5078,"q":"Quel reptile a une carapace ?","r":"La tortue."},
  {"id":5079,"q":"Quel reptile n'a pas de pattes ?","r":"Le serpent."},
  {"id":5080,"q":"Quel reptile est le plus rapide ?","r":"Le lézard basilic."},
  {"id":5081,"q":"Plante médicinale : l'aloe vera ?","r":"Soigne la peau."},
  {"id":5082,"q":"Plante médicinale : la camomille ?","r":"Calme et aide la digestion."},
  {"id":5083,"q":"Plante médicinale : la menthe poivrée ?","r":"Digestive et rafraîchissante."},
  {"id":5084,"q":"Plante médicinale : le thym ?","r":"Antiseptique."},
  {"id":5085,"q":"Plante médicinale : le romarin ?","r":"Stimulant."},
  {"id":5086,"q":"Plante médicinale : la lavande ?","r":"Calmante."},
  {"id":5087,"q":"Plante médicinale : le gingembre ?","r":"Anti-nausée."},
  {"id":5088,"q":"Plante médicinale : le curcuma ?","r":"Anti-inflammatoire."},
  {"id":5089,"q":"Plante médicinale : l'ail ?","r":"Antibactérien."},
  {"id":5090,"q":"Plante médicinale : le ginseng ?","r":"Tonique."},
  {"id":5091,"q":"Plante médicinale : l'échinacée ?","r":"Immunostimulante."},
  {"id":5092,"q":"Plante médicinale : le millepertuis ?","r":"Antidépresseur léger."},
  {"id":5093,"q":"Plante médicinale : la valériane ?","r":"Aide au sommeil."},
  {"id":5094,"q":"Plante médicinale : la passiflore ?","r":"Calmante."},
  {"id":5095,"q":"Plante médicinale : le ginkgo ?","r":"Améliore la circulation."},
  {"id":5096,"q":"Plante médicinale : le ginseng ?","r":"Énergisant."},
  {"id":5097,"q":"Plante médicinale : la réglisse ?","r":"Adoucit la gorge."},
  {"id":5098,"q":"Plante médicinale : le fenouil ?","r":"Digestif."},
  {"id":5099,"q":"Plante médicinale : l'ortie ?","r":"Diurétique."},
  {"id":5100,"q":"Plante médicinale : le pissenlit ?","r":"Détoxifiant."},
  {"id":5101,"q":"Arbre : le chêne ?","r":"Grand arbre à glands."},
  {"id":5102,"q":"Arbre : le hêtre ?","r":"Feuilles lisses."},
  {"id":5103,"q":"Arbre : le bouleau ?","r":"Écorce blanche."},
  {"id":5104,"q":"Arbre : le saule ?","r":"Branches tombantes."},
  {"id":5105,"q":"Arbre : le peuplier ?","r":"Croissance rapide."},
  {"id":5106,"q":"Arbre : le platane ?","r":"Écorce tachetée."},
  {"id":5107,"q":"Arbre : l'érable ?","r":"Feuilles à 5 lobes."},
  {"id":5108,"q":"Arbre : le frêne ?","r":"Bois souple."},
  {"id":5109,"q":"Arbre : le tilleul ?","r":"Fleurs parfumées."},
  {"id":5110,"q":"Arbre : le châtaignier ?","r":"Donne des châtaignes."},
  {"id":5111,"q":"Arbre : le noyer ?","r":"Donne des noix."},
  {"id":5112,"q":"Arbre : l'olivier ?","r":"Donne des olives."},
  {"id":5113,"q":"Arbre : le figuier ?","r":"Donne des figues."},
  {"id":5114,"q":"Arbre : le citronnier ?","r":"Donne des citrons."},
  {"id":5115,"q":"Arbre : l'oranger ?","r":"Donne des oranges."},
  {"id":5116,"q":"Arbre : le pommier ?","r":"Donne des pommes."},
  {"id":5117,"q":"Arbre : le poirier ?","r":"Donne des poires."},
  {"id":5118,"q":"Arbre : le cerisier ?","r":"Donne des cerises."},
  {"id":5119,"q":"Arbre : le pêcher ?","r":"Donne des pêches."},
  {"id":5120,"q":"Arbre : l'abricotier ?","r":"Donne des abricots."},
  {"id":5121,"q":"Fleur : la rose ?","r":"Symbole d'amour."},
  {"id":5122,"q":"Fleur : la tulipe ?","r":"Fleur printanière."},
  {"id":5123,"q":"Fleur : le lys ?","r":"Symbole de pureté."},
  {"id":5124,"q":"Fleur : la marguerite ?","r":"Fleur blanche."},
  {"id":5125,"q":"Fleur : le tournesol ?","r":"Suit le soleil."},
  {"id":5126,"q":"Fleur : l'orchidée ?","r":"Fleur exotique."},
  {"id":5127,"q":"Fleur : le muguet ?","r":"Fleur du 1er mai."},
  {"id":5128,"q":"Fleur : la pivoine ?","r":"Fleur volumineuse."},
  {"id":5129,"q":"Fleur : le jasmin ?","r":"Fleur parfumée."},
  {"id":5130,"q":"Fleur : la lavande ?","r":"Fleur violette."},
  {"id":5131,"q":"Fleur : le coquelicot ?","r":"Fleur rouge."},
  {"id":5132,"q":"Fleur : le bleuet ?","r":"Fleur bleue."},
  {"id":5133,"q":"Fleur : l'iris ?","r":"Fleur de Louisiane."},
  {"id":5134,"q":"Fleur : la jacinthe ?","r":"Fleur printanière."},
  {"id":5135,"q":"Fleur : la jonquille ?","r":"Fleur jaune."},
  {"id":5136,"q":"Fleur : la narcisse ?","r":"Fleur jaune."},
  {"id":5137,"q":"Fleur : le dahlia ?","r":"Fleur d'automne."},
  {"id":5138,"q":"Fleur : le chrysanthème ?","r":"Fleur d'automne."},
  {"id":5139,"q":"Fleur : l'anémone ?","r":"Fleur de printemps."},
  {"id":5140,"q":"Fleur : la bruyère ?","r":"Fleur de lande."},
  {"id":5141,"q":"Champignon : le cèpe ?","r":"Champignon comestible."},
  {"id":5142,"q":"Champignon : la girolle ?","r":"Champignon comestible."},
  {"id":5143,"q":"Champignon : le champignon de Paris ?","r":"Champignon cultivé."},
  {"id":5144,"q":"Champignon : la truffe ?","r":"Champignon rare."},
  {"id":5145,"q":"Champignon : l'amanite phalloïde ?","r":"Champignon mortel."},
  {"id":5146,"q":"Champignon : l'amanite tue-mouches ?","r":"Champignon toxique."},
  {"id":5147,"q":"Champignon : la morille ?","r":"Champignon de printemps."},
  {"id":5148,"q":"Champignon : le shiitaké ?","r":"Champignon japonais."},
  {"id":5149,"q":"Champignon : le pleurote ?","r":"Champignon en forme d'huître."},
  {"id":5150,"q":"Champignon : la chanterelle ?","r":"Autre nom de la girolle."},
  {"id":5151,"q":"Champignon : le bolet ?","r":"Grande famille."},
  {"id":5152,"q":"Champignon : la coulemelle ?","r":"Grand champignon."},
  {"id":5153,"q":"Champignon : le pied-de-mouton ?","r":"Champignon comestible."},
  {"id":5154,"q":"Champignon : le rosé des prés ?","r":"Champignon comestible."},
  {"id":5155,"q":"Champignon : le lactaire ?","r":"Champignon à lait."},
  {"id":5156,"q":"Champignon : le coprin ?","r":"Champignon qui noircit."},
  {"id":5157,"q":"Champignon : la vesse-de-loup ?","r":"Champignon rond."},
  {"id":5158,"q":"Champignon : la pézize ?","r":"Champignon en coupe."},
  {"id":5159,"q":"Champignon : le polypore ?","r":"Champignon de bois."},
  {"id":5160,"q":"Champignon : la levure ?","r":"Champignon microscopique."},
  {"id":5161,"q":"Minéral : le diamant ?","r":"Carbone cristallisé."},
  {"id":5162,"q":"Minéral : le rubis ?","r":"Corindon rouge."},
  {"id":5163,"q":"Minéral : le saphir ?","r":"Corindon bleu."},
  {"id":5164,"q":"Minéral : l'émeraude ?","r":"Béryl vert."},
  {"id":5165,"q":"Minéral : l'améthyste ?","r":"Quartz violet."},
  {"id":5166,"q":"Minéral : le quartz ?","r":"Minéral commun."},
  {"id":5167,"q":"Minéral : le feldspath ?","r":"Minéral abondant."},
  {"id":5168,"q":"Minéral : le mica ?","r":"Minéral feuilleté."},
  {"id":5169,"q":"Minéral : le gypse ?","r":"Minéral tendre."},
  {"id":5170,"q":"Minéral : le calcite ?","r":"Carbonate de calcium."},
  {"id":5171,"q":"Minéral : la pyrite ?","r":"Or des fous."},
  {"id":5172,"q":"Minéral : la magnétite ?","r":"Minéral magnétique."},
  {"id":5173,"q":"Minéral : l'hématite ?","r":"Oxyde de fer."},
  {"id":5174,"q":"Minéral : la bauxite ?","r":"Minerai d'aluminium."},
  {"id":5175,"q":"Minéral : la galène ?","r":"Minerai de plomb."},
  {"id":5176,"q":"Minéral : la cassitérite ?","r":"Minerai d'étain."},
  {"id":5177,"q":"Minéral : la blende ?","r":"Minerai de zinc."},
  {"id":5178,"q":"Minéral : le cinabre ?","r":"Minerai de mercure."},
  {"id":5179,"q":"Minéral : l'uraninite ?","r":"Minerai d'uranium."},
  {"id":5180,"q":"Minéral : le graphite ?","r":"Carbone tendre."},
  {"id":5181,"q":"Espace : qu'est-ce qu'une étoile ?","r":"Sphère de plasma."},
  {"id":5182,"q":"Espace : qu'est-ce qu'une galaxie ?","r":"Ensemble d'étoiles."},
  {"id":5183,"q":"Espace : qu'est-ce qu'une nébuleuse ?","r":"Nuage de gaz."},
  {"id":5184,"q":"Espace : qu'est-ce qu'un trou noir ?","r":"Région d'où rien n'échappe."},
  {"id":5185,"q":"Espace : qu'est-ce qu'une supernova ?","r":"Explosion d'étoile."},
  {"id":5186,"q":"Espace : qu'est-ce qu'un pulsar ?","r":"Étoile à neutrons pulsante."},
  {"id":5187,"q":"Espace : qu'est-ce qu'un quasar ?","r":"Noyau actif de galaxie."},
  {"id":5188,"q":"Espace : qu'est-ce qu'une exoplanète ?","r":"Planète hors système solaire."},
  {"id":5189,"q":"Espace : qu'est-ce que la matière noire ?","r":"Matière invisible."},
  {"id":5190,"q":"Espace : qu'est-ce que l'énergie noire ?","r":"Force d'expansion."},
  {"id":5191,"q":"Espace : qu'est-ce qu'une année-lumière ?","r":"Distance parcourue par la lumière en 1 an."},
  {"id":5192,"q":"Espace : qu'est-ce que l'unité astronomique ?","r":"Distance Terre-Soleil."},
  {"id":5193,"q":"Espace : qu'est-ce qu'un parsec ?","r":"Environ 3,26 années-lumière."},
  {"id":5194,"q":"Espace : qu'est-ce que le Big Bang ?","r":"Origine de l'Univers."},
  {"id":5195,"q":"Espace : quel est l'âge de l'Univers ?","r":"Environ 13,8 milliards d'années."},
  {"id":5196,"q":"Espace : qu'est-ce que la Voie lactée ?","r":"Notre galaxie."},
  {"id":5197,"q":"Espace : combien de planètes dans le système solaire ?","r":"8."},
  {"id":5198,"q":"Espace : quel est le plus grand volcan du système solaire ?","r":"Olympus Mons (Mars)."},
  {"id":5199,"q":"Espace : quel est le satellite naturel de la Terre ?","r":"La Lune."},
  {"id":5200,"q":"Espace : quel est le plus grand satellite du système solaire ?","r":"Ganymède."},
  {"id":5201,"q":"Espace : quel est le plus grand anneau planétaire ?","r":"Saturne."},
  {"id":5202,"q":"Espace : quelle planète tourne le plus vite ?","r":"Jupiter."},
  {"id":5203,"q":"Espace : quelle planète est la plus chaude ?","r":"Vénus."},
  {"id":5204,"q":"Espace : quelle planète est la plus froide ?","r":"Neptune."},
  {"id":5205,"q":"Espace : quelle planète a le plus de lunes ?","r":"Saturne."},
  {"id":5206,"q":"Espace : quelle est la plus petite planète ?","r":"Mercure."},
  {"id":5207,"q":"Espace : quelle planète est rouge ?","r":"Mars."},
  {"id":5208,"q":"Espace : quelle planète est bleue ?","r":"Neptune."},
  {"id":5209,"q":"Espace : quelle planète a des anneaux ?","r":"Saturne, Jupiter, Uranus, Neptune."},
  {"id":5210,"q":"Espace : quelle est la plus proche du Soleil ?","r":"Mercure."},
  {"id":5211,"q":"Espace : quelle est la plus éloignée ?","r":"Neptune."},
  {"id":5212,"q":"Espace : quelle planète est la Terre ?","r":"3e planète."},
  {"id":5213,"q":"Espace : qu'est-ce que la ceinture d'astéroïdes ?","r":"Entre Mars et Jupiter."},
  {"id":5214,"q":"Espace : qu'est-ce que la ceinture de Kuiper ?","r":"Au-delà de Neptune."},
  {"id":5215,"q":"Espace : qu'est-ce que le nuage de Oort ?","r":"Source des comètes."},
  {"id":5216,"q":"Espace : qu'est-ce qu'une comète ?","r":"Corps glacé."},
  {"id":5217,"q":"Espace : qu'est-ce qu'un météore ?","r":"Étoile filante."},
  {"id":5218,"q":"Espace : qu'est-ce qu'un météorite ?","r":"Météore arrivé au sol."},
  {"id":5219,"q":"Espace : qu'est-ce qu'un astéroïde ?","r":"Corps rocheux."},
  {"id":5220,"q":"Espace : qu'est-ce qu'un satellite artificiel ?","r":"Objet lancé en orbite."},
  {"id":5221,"q":"Espace : qui a marché le premier sur la Lune ?","r":"Neil Armstrong."},
  {"id":5222,"q":"Espace : en quelle année ?","r":"1969."},
  {"id":5223,"q":"Espace : qu'est-ce que l'ISS ?","r":"Station spatiale internationale."},
  {"id":5224,"q":"Espace : qu'est-ce que Hubble ?","r":"Télescope spatial."},
  {"id":5225,"q":"Espace : qu'est-ce que James Webb ?","r":"Télescope infrarouge spatial."},
  {"id":5226,"q":"Espace : qu'est-ce qu'un rover ?","r":"Robot explorateur."},
  {"id":5227,"q":"Espace : quel rover explore Mars ?","r":"Perseverance."},
  {"id":5228,"q":"Espace : qu'est-ce que Voyager 1 ?","r":"Sonde sortie du système solaire."},
  {"id":5229,"q":"Espace : quelle est la mission qui a posé un homme sur la Lune ?","r":"Apollo 11."},
  {"id":5230,"q":"Espace : qu'est-ce que SpaceX ?","r":"Entreprise d'Elon Musk."},
  {"id":5231,"q":"Espace : qu'est-ce qu'une fusée ?","r":"Véhicule spatial."},
  {"id":5232,"q":"Espace : qu'est-ce qu'un lanceur ?","r":"Fusée porteuse."},
  {"id":5233,"q":"Espace : qu'est-ce qu'une navette spatiale ?","r":"Véhicule réutilisable."},
  {"id":5234,"q":"Espace : qu'est-ce qu'une orbite ?","r":"Trajectoire autour d'un astre."},
  {"id":5235,"q":"Espace : qu'est-ce que l'apesanteur ?","r":"Absence de poids ressenti."},
  {"id":5236,"q":"Espace : qu'est-ce que la gravité ?","r":"Force d'attraction."},
  {"id":5237,"q":"Espace : qu'est-ce que la vitesse de libération ?","r":"Vitesse pour échapper à un astre."},
  {"id":5238,"q":"Espace : qu'est-ce que la vitesse de la lumière ?","r":"299 792 458 m/s."},
  {"id":5239,"q":"Espace : qu'est-ce que la relativité ?","r":"Théorie d'Einstein."},
  {"id":5240,"q":"Espace : qu'est-ce que la mécanique quantique ?","r":"Physique de l'infiniment petit."},
  {"id":5241,"q":"Espace : qu'est-ce qu'un atome ?","r":"Noyau + électrons."},
  {"id":5242,"q":"Espace : qu'est-ce qu'un quark ?","r":"Constituant du proton."},
  {"id":5243,"q":"Espace : qu'est-ce qu'un boson de Higgs ?","r":"Particule donnant la masse."},
  {"id":5244,"q":"Espace : qu'est-ce que le LHC ?","r":"Grand collisionneur de hadrons."},
  {"id":5245,"q":"Espace : qu'est-ce que le CERN ?","r":"Laboratoire européen de physique."},
  {"id":5246,"q":"Espace : qu'est-ce que la NASA ?","r":"Agence spatiale américaine."},
  {"id":5247,"q":"Espace : qu'est-ce que l'ESA ?","r":"Agence spatiale européenne."},
  {"id":5248,"q":"Espace : qu'est-ce que Roscosmos ?","r":"Agence spatiale russe."},
  {"id":5249,"q":"Espace : qu'est-ce que la CNSA ?","r":"Agence spatiale chinoise."},
  {"id":5250,"q":"Espace : quel est le prochain grand objectif spatial ?","r":"Retour sur la Lune et voyage vers Mars."}
]
''')


class MemoryStore:

    def __init__(self):
        mem_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AssistantVocal")
        os.makedirs(mem_dir, exist_ok=True)
        self.path = os.path.join(mem_dir, "memory.json")
        self.data = {
            "learned_phrases": [],   # [{"phrase": str, "type": "open"/"close", "target": str}]
            "user_name": None,
        }
        self._load()

    def _load(self):
        import json
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass

    def save(self):
        import json
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Impossible de sauvegarder la mémoire : {e}")

    def remember_command(self, phrase: str, action_type: str, target: str):
        """Enregistre une commande vocale correctement exécutée et confirmée."""
        phrase = phrase.strip().lower()
        for entry in self.data["learned_phrases"]:
            if entry["phrase"] == phrase:
                return  # déjà connue
        self.data["learned_phrases"].append({
            "phrase": phrase, "type": action_type, "target": target,
        })
        # Garde un historique raisonnable, pas illimité. Relevé à 4000 pour
        # laisser de la place aux ~2178 dialogues de base pré-chargés en plus
        # des commandes personnelles apprises au fil du temps.
        self.data["learned_phrases"] = self.data["learned_phrases"][-6000:]
        self.save()

    def seed_default_dialogues(self, pairs) -> int:
        """
        Pré-charge une liste de dialogues de base [{"q":..., "r":...}, ...]
        comme réponses apprises (type "reply"), sans écraser les phrases
        déjà connues (apprentissage personnel de l'utilisateur, ou dialogues
        déjà pré-chargés lors d'un lancement précédent). N'écrit sur le
        disque qu'une seule fois à la fin, pas à chaque entrée.
        Renvoie le nombre de nouvelles phrases effectivement ajoutées.
        """
        existing = {e["phrase"] for e in self.data["learned_phrases"]}
        added = 0
        for pair in pairs:
            phrase = pair["q"].strip().lower()
            if not phrase or phrase in existing:
                continue
            self.data["learned_phrases"].append({
                "phrase": phrase, "type": "reply", "target": pair["r"],
            })
            existing.add(phrase)
            added += 1
        if added:
            self.data["learned_phrases"] = self.data["learned_phrases"][-6000:]
            self.save()
        return added

    # Mots vides (français) ignorés lors de l'extraction des mots clés
    # d'une phrase apprise : on ne veut garder que le mot qui porte le sens
    # (ex: dans "qu'est-ce que la force ?", le seul mot clé est "force").
    _STOPWORDS = {
        "qu", "est", "ce", "que", "qui", "quoi", "comment", "pourquoi",
        "quand", "où", "la", "le", "les", "l", "un", "une", "des", "de",
        "du", "au", "aux", "et", "ou", "à", "en", "dans", "sur", "pour",
        "avec", "sans", "ça", "il", "elle", "on", "je", "tu", "nous",
        "vous", "ils", "elles", "se", "sa", "son", "ses", "mon", "ma",
        "mes", "ton", "ta", "tes", "c", "d", "j", "n", "s", "y",
    }

    @staticmethod
    def _keywords(phrase: str):
        """
        Extrait les mots significatifs d'une phrase : enlève la ponctuation
        et les mots vides, ne garde que les mots de plus de 2 lettres.
        Utilisé pour retrouver une phrase apprise à partir d'un seul mot
        clé plutôt que de la question complète.
        """
        words = re.findall(r"[a-zàâäéèêëïîôöùûüçñ0-9\-]+", phrase.lower())
        return [w for w in words if w not in MemoryStore._STOPWORDS and len(w) > 2]

    def find_by_keyword(self, command: str, cutoff=0.82):
        """
        Repli utilisé quand le mot (ou les quelques mots) prononcé(s) ne
        correspond(ent) à aucune phrase apprise dans son ensemble : on
        regarde s'il(s) correspond(ent) à un MOT CLÉ d'une phrase déjà
        apprise (ex: dire seulement "force" retrouve la phrase apprise
        "qu'est-ce que la force ?" et redonne sa réponse). Entièrement
        local (comparaison texte via difflib), aucune connexion internet.
        """
        command_words = self._keywords(command)
        if not command_words:
            return None
        best_entry, best_ratio = None, 0.0
        for entry in self.data["learned_phrases"]:
            for kw in self._keywords(entry["phrase"]):
                for cw in command_words:
                    ratio = difflib.SequenceMatcher(None, cw, kw).ratio()
                    if ratio > best_ratio:
                        best_ratio, best_entry = ratio, entry
        return best_entry if best_ratio >= cutoff else None

    def find_best_match(self, command: str, cutoff=0.72):
        """
        Cherche, parmi les commandes déjà apprises, la plus proche de ce qui
        vient d'être dit. Permet de prédire l'intention même en cas de
        prononciation imparfaite ou de transcription légèrement différente.

        1) On essaie d'abord une correspondance sur la phrase ENTIÈRE
           (comportement d'origine).
        2) Si ça échoue (par ex. l'utilisateur dit juste un mot clé au lieu
           de la question complète apprise), on essaie de retrouver ce mot
           clé parmi les mots significatifs des phrases déjà apprises.
        """
        if not self.data["learned_phrases"]:
            return None
        phrases = [e["phrase"] for e in self.data["learned_phrases"]]
        matches = difflib.get_close_matches(command.strip().lower(), phrases, n=1, cutoff=cutoff)
        if matches:
            for entry in self.data["learned_phrases"]:
                if entry["phrase"] == matches[0]:
                    return entry
        return self.find_by_keyword(command)

    def reset_learned_commands(self):
        """Efface toutes les commandes apprises (garde le prénom connu)."""
        self.data["learned_phrases"] = []
        self.save()

    def set_user_name(self, name: str):
        self.data["user_name"] = name.strip().title()
        self.save()

    @property
    def user_name(self):
        return self.data.get("user_name")


# ---------------------------------------------------------------------------
# Modules optionnels nécessitant internet (isolés du reste)
# ---------------------------------------------------------------------------

class OnlineTools:
    """
    Regroupe tout ce qui A BESOIN d'une connexion : lecture de page web,
    recherche. Le reste de l'assistant fonctionne sans ce module.
    """

    def __init__(self):
        try:
            import requests
            from bs4 import BeautifulSoup
            self.requests = requests
            self.BeautifulSoup = BeautifulSoup
            self.available = True
        except ImportError:
            self.available = False

    def read_webpage(self, url: str, max_chars=1500) -> str:
        if not self.available:
            return "Les modules requests/beautifulsoup4 ne sont pas installés."
        try:
            resp = self.requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            soup = self.BeautifulSoup(resp.text, "html.parser")
            paragraphs = [p.get_text(strip=True) for p in soup.find_all("p")]
            text = " ".join(paragraphs)
            return text[:max_chars] if text else "Je n'ai trouvé aucun texte lisible sur cette page."
        except Exception:
            return "Je n'ai pas réussi à accéder à cette page (vérifie ta connexion internet)."

    def search_web(self, query: str, max_results=3):
        """Recherche simple via DuckDuckGo HTML (pas besoin de clé API)."""
        if not self.available:
            return []
        try:
            resp = self.requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            soup = self.BeautifulSoup(resp.text, "html.parser")
            results = []
            for a in soup.select("a.result__a")[:max_results]:
                results.append(a.get_text(strip=True))
            return results
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Assistant principal
# ---------------------------------------------------------------------------

class VoiceAssistant:

    PROACTIVE_SUGGESTIONS = [
        "Je peux résumer un texte, un audio ou une vidéo si tu veux.",
        "Dis-moi 'lis la page' suivi d'une adresse si tu veux que je te lise un article.",
        "Je peux aussi ouvrir ou fermer des logiciels pour toi, il suffit de demander.",
        "N'hésite pas à me demander de faire une recherche à ta place.",
        "Tu peux aussi me demander un calcul, du genre 'combien font 5 + 5'.",
    ]

    def __init__(self, language="fr", tts_rate=135, use_local_stt=True, whisper_model="small"):
        self.language = language
        self.tts_rate = tts_rate
        self.voice_id = None

        print("Initialisation de la synthèse vocale (pyttsx3)...")
        # IMPORTANT : pyttsx3 utilise le COM Windows (SAPI5) en arrière-plan.
        # Le COM est lié au thread qui l'a créé : si on l'initialise dans un
        # thread séparé (comme le faisait _call_with_timeout), l'objet
        # devient invalide dès que ce thread se termine, ce qui provoque des
        # blocages (runAndWait qui ne rend jamais la main) au 2e appel. On
        # initialise donc toujours pyttsx3 directement dans le thread
        # principal, sans wrapper de timeout.
        try:
            probe_engine = pyttsx3.init()
        except Exception as e:
            raise RuntimeError(
                f"Impossible d'initialiser pyttsx3 : {e}\n"
                "Vérifie qu'un moteur de synthèse vocale (SAPI5) est bien "
                "installé et fonctionnel sur ce PC (Paramètres > Heure et "
                "langue > Voix)."
            )

        # Recherche explicite d'une voix française installée sur le PC,
        # au lieu de prendre une voix au hasard (ce qui tombait sur une
        # voix anglaise). On regarde le nom ET les métadonnées "languages"
        # (SAPI5 stocke parfois le code langue plutôt que le nom).
        voices = probe_engine.getProperty('voices')
        french_voice = None
        for v in voices:
            name = (v.name or "").lower()
            langs = " ".join(str(l) for l in (v.languages or [])).lower()
            vid = (v.id or "").lower()
            if any(tag in name or tag in langs or tag in vid
                   for tag in ("fr", "french", "français", "france")):
                french_voice = v
                break

        if french_voice:
            self.voice_id = french_voice.id
            print(f"Voix sélectionnée : {french_voice.name}")
        else:
            print("⚠ Aucune voix française détectée sur ce PC. La synthèse "
                  "vocale restera en anglais tant qu'une voix française "
                  "SAPI5 n'est pas installée (Paramètres Windows > Heure et "
                  "langue > Voix > Ajouter une voix > Français).")
            if voices:
                self.voice_id = voices[0].id

        try:
            probe_engine.stop()
        except Exception:
            pass
        print("Synthèse vocale prête.")

        print("Scan des logiciels installés (registre Windows)...")
        self.app_manager = AppManager()
        print("Chargement du module de résumé...")
        self.summarizer = Summarizer()
        self.online = OnlineTools()
        self.calculator = Calculator()
        self.memory = MemoryStore()
        added = self.memory.seed_default_dialogues(DEFAULT_DIALOGUES)
        if added:
            print(f"[Mémoire] {added} nouveaux dialogues de base appris "
                  f"({len(DEFAULT_DIALOGUES) - added} déjà connus).")

        self.stt = None
        if use_local_stt:
            try:
                # Taille approximative de téléchargement selon le modèle,
                # affichée dynamiquement pour ne pas afficher un chiffre
                # erroné si le modèle choisi n'est pas "base".
                tailles_mo = {
                    "tiny": 75, "base": 145, "small": 480,
                    "medium": 1500, "large": 3000, "large-v2": 3000,
                    "large-v3": 3000,
                }
                taille = tailles_mo.get(whisper_model, "?")
                print(f"Chargement du modèle Whisper '{whisper_model}'...")
                print(f"(si c'est la première fois, ceci télécharge ~{taille} Mo pour "
                      f"'{whisper_model}' et peut prendre plusieurs minutes selon ta "
                      "connexion — c'est normal)")
                self.stt = LocalSpeechRecognizer(model_size=whisper_model, language=language)
                self.stt.calibrate()
            except RuntimeError as e:
                print(str(e))

        self.commands_since_suggestion = 0

    # -- Voix -------------------------------------------------------------

    def speak(self, text: str):
        """
        Recrée un moteur pyttsx3 neuf à chaque phrase prononcée. C'est le
        contournement standard du bug connu où pyttsx3/SAPI5 se fige sur
        Windows si on réutilise le même moteur pour plusieurs appels
        successifs à say()/runAndWait(). Tout se passe dans le thread
        principal (jamais dans un thread séparé, voir note dans __init__).
        """
        print(f"Assistant: {text}")
        try:
            engine = pyttsx3.init()
            if self.voice_id:
                engine.setProperty('voice', self.voice_id)
            engine.setProperty('rate', self.tts_rate)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
            del engine
        except Exception as e:
            print(f"  ⚠ Erreur de synthèse vocale : {e}")
        # Petite pause pour laisser le pilote audio (haut-parleur -> micro)
        # se stabiliser. Sans ça, rouvrir le micro juste après avoir parlé
        # peut provoquer un bruit aigu / grésillement sur certaines cartes
        # son intégrées (ex: Conexant).
        time.sleep(0.4)

    def listen(self) -> str:
        if self.stt is None:
            self.speak("La reconnaissance vocale locale n'est pas disponible.")
            return ""
        return self.stt.listen()

    # -- Suggestions proactives -------------------------------------------

    def maybe_suggest(self):
        """De temps en temps, propose spontanément une action utile."""
        self.commands_since_suggestion += 1
        if self.commands_since_suggestion >= random.randint(4, 7):
            self.commands_since_suggestion = 0
            self.speak(random.choice(self.PROACTIVE_SUGGESTIONS))

    # -- Confirmation vocale -------------------------------------------------

    # Mots-clés des commandes assez "sensibles" pour valoir une confirmation
    # avant exécution. Seul "oublie" (effacer la mémoire apprise) est gardé
    # ici : c'est irréversible. Ouvrir/fermer un logiciel s'exécute
    # directement, sans confirmation, sur demande explicite.
    CONFIRM_KEYWORDS = ("oublie",)

    YES_WORDS = ("oui", "ouais", "yes", "vas-y", "confirme", "exact", "correct", "c'est ça", "c'est bon")
    NO_WORDS = ("non", "no", "annule", "stop", "faux", "pas ça")

    def _needs_confirmation(self, command: str) -> bool:
        return any(word in command for word in self.CONFIRM_KEYWORDS)

    def _confirm(self, command: str) -> bool:
        """
        Répète ce que l'assistant a compris et attend une confirmation
        vocale (oui/non) avant d'exécuter une commande sensible. Permet de
        rattraper une erreur de transcription sans avoir à tout redire.
        """
        self.speak(f"Tu veux que je fasse : {command}. C'est bien ça ?")
        response = self.listen()

        if any(word in response for word in self.YES_WORDS):
            return True
        if any(word in response for word in self.NO_WORDS):
            return False

        # Réponse ambiguë ou pas entendue : redemande une seule fois,
        # plutôt que d'exécuter une commande potentiellement mal comprise.
        self.speak("Je n'ai pas bien saisi, dis simplement oui ou non.")
        response = self.listen()
        if any(word in response for word in self.YES_WORDS):
            return True
        return False

    # -- Raccourcis numérotés ------------------------------------------------
    # Pour éviter d'avoir à dire des phrases longues (plus difficiles à bien
    # transcrire), chaque action a un numéro. Il suffit de dire le chiffre
    # (ou le mot-nombre) seul, suivi si besoin d'un argument (ex: nom du
    # logiciel). Exemple : dire "trois chrome" équivaut à "ouvre chrome".
    NUMBER_WORDS = {
        "0": 0, "zéro": 0, "zero": 0,
        "1": 1, "un": 1, "une": 1,
        "2": 2, "deux": 2,
        "3": 3, "trois": 3,
        "4": 4, "quatre": 4,
        "5": 5, "cinq": 5,
        "6": 6, "six": 6,
        "7": 7, "sept": 7,
        "8": 8, "huit": 8,
        "9": 9, "neuf": 9,
    }

    # Numéro -> mot-clé déjà géré par take_action(). "menu" est un cas
    # spécial traité à part (affiche la liste des raccourcis).
    NUMBER_ACTIONS = {
        1: "heure",
        2: "date",
        3: "ouvre",
        4: "ferme",
        5: "liste logiciel",
        6: "résumé",
        7: "lis la page",
        8: "recherche",
        9: "oublie appris",
        0: "menu",
    }

    MENU_TEXT = (
        "Raccourcis disponibles (dis le chiffre seul, ou suivi d'un nom si besoin) :\n"
        "  0 = afficher ce menu\n"
        "  1 = quelle heure est-il\n"
        "  2 = quelle est la date\n"
        "  3 = ouvre <logiciel>   (ex: \"3 chrome\")\n"
        "  4 = ferme <logiciel>   (ex: \"4 chrome\")\n"
        "  5 = liste des logiciels installés\n"
        "  6 = résume un fichier\n"
        "  7 = lis une page web\n"
        "  8 = recherche <mots-clés>   (ex: \"8 météo Douala\")\n"
        "  9 = oublie les commandes apprises\n"
        "  Calculatrice = dis directement un calcul, ex: \"combien font 5 + 5\",\n"
        "                 \"racine carrée de 49\", \"moyenne de 3, 5 et 7\"\n"
    )

    def _translate_shortcut(self, command: str) -> str:
        """
        Si la commande commence par un chiffre ou un mot-nombre reconnu, la
        traduit vers la commande textuelle équivalente (le reste de la
        phrase est conservé comme argument, ex: nom du logiciel).
        Sinon, renvoie la commande telle quelle.
        """
        words = command.strip().split()
        if not words:
            return command
        first = words[0].strip(".,!?").lower()
        if first not in self.NUMBER_WORDS:
            return command
        number = self.NUMBER_WORDS[first]
        if number not in self.NUMBER_ACTIONS:
            return command
        remainder = " ".join(words[1:]).strip()
        keyword = self.NUMBER_ACTIONS[number]
        translated = f"{keyword} {remainder}".strip()
        print(f"  [raccourci] '{first}' → \"{translated}\"")
        return translated

    # -- Traitement des commandes ------------------------------------------

    GREETING_WORDS = ("bonjour", "salut", "coucou", "bonsoir", "hello", "hey",
                       "ça va", "comment vas-tu", "comment tu vas")
    GREETING_REPLIES = [
        "Bonjour ! Comment puis-je t'aider ?",
        "Salut ! Je suis là, dis-moi ce qu'il te faut.",
        "Coucou ! Prêt à t'aider.",
    ]

    THANKS_WORDS = ("merci", "je te remercie", "c'est gentil")
    THANKS_REPLIES = [
        "Avec plaisir !",
        "De rien, n'hésite pas si tu as besoin d'autre chose.",
        "Je t'en prie !",
    ]

    # Formules de politesse de fin de conversation : on répond gentiment
    # SANS fermer l'assistant (pour ça, il y a "au revoir" / "stop
    # assistant" / "exit", gérés plus bas, qui eux quittent le programme).
    FAREWELL_WORDS = ("bonne journée", "bonne soirée", "bonne nuit",
                       "à bientôt", "à plus tard", "à plus", "à tout à l'heure",
                       "bon après-midi", "passe une bonne journée")
    FAREWELL_REPLIES = [
        "Merci, toi aussi !",
        "À bientôt !",
        "Bonne continuation !",
    ]

    IDENTITY_WORDS = ("qui es-tu", "tu es qui", "comment tu t'appelles",
                       "c'est quoi ton nom", "tu es qui toi")
    HELP_WORDS = ("qu'est-ce que tu sais faire", "que sais-tu faire",
                  "tu sais faire quoi", "aide-moi", "j'ai besoin d'aide")

    def take_action(self, command: str) -> bool:
        if not command:
            return True

        try:
            if command.strip() == "menu" or "menu" in command:
                print(self.MENU_TEXT)
                self.speak("Les raccourcis sont affichés à l'écran. Dis un chiffre "
                            "pour utiliser l'un d'eux.")

            elif self.calculator.looks_like_query(command):
                # Calculatrice vocale : "combien font 5 + 5", "cmb font...",
                # "la somme de 5 et 5", "racine carrée de 49", etc. Vérifiée
                # tôt dans la chaîne pour ne pas être masquée par des règles
                # plus génériques (ex: "date"/"heure" contiennent parfois
                # des mots communs, mais looks_like_query() exige un nombre
                # ET un indice de calcul, donc les faux positifs sont rares).
                self.speak(self.calculator.handle(command))

            elif "je m'appelle" in command or "mon nom est" in command:
                name = command.replace("je m'appelle", "").replace("mon nom est", "").strip()
                if name:
                    self.memory.set_user_name(name)
                    self.speak(f"Enchanté {name} ! Je m'en souviendrai.")
                else:
                    self.speak("Je n'ai pas bien saisi ton prénom.")

            elif any(word in command for word in self.GREETING_WORDS):
                reply = random.choice(self.GREETING_REPLIES)
                if self.memory.user_name:
                    reply = reply.replace("!", f", {self.memory.user_name} !", 1)
                self.speak(reply)

            elif any(word in command for word in self.THANKS_WORDS):
                self.speak(random.choice(self.THANKS_REPLIES))

            elif any(word in command for word in self.FAREWELL_WORDS):
                self.speak(random.choice(self.FAREWELL_REPLIES))

            elif any(word in command for word in self.IDENTITY_WORDS):
                self.speak("Je suis ton assistant vocal personnel, je fonctionne "
                            "entièrement hors ligne sur ce PC.")

            elif any(word in command for word in self.HELP_WORDS):
                print(self.MENU_TEXT)
                self.speak("Les raccourcis sont affichés à l'écran. Dis un chiffre "
                            "pour utiliser l'un d'eux.")

            elif "heure" in command:
                now = datetime.datetime.now().strftime('%H:%M')
                self.speak(f"Il est {now}")

            elif "date" in command:
                today = datetime.date.today().strftime('%d %B %Y')
                self.speak(f"Nous sommes le {today}")

            elif "ouvre" in command or "lance" in command or "démarre" in command:
                query = command
                for w in ["ouvre", "lance", "démarre"]:
                    query = query.replace(w, "")
                query = query.strip()
                name, _ = self.app_manager.find_app(query)
                self.speak(self.app_manager.open_app(query))
                if name:
                    self.memory.remember_command(command, "open", query)

            elif ("ferme" in command or "quitte" in command or "close" in command) and "assistant" not in command:
                query = command
                for w in ["ferme", "quitte", "close"]:
                    query = query.replace(w, "")
                query = query.strip()
                # "fenêtre"/"windows" (au singulier ou pluriel) désigne la
                # fenêtre active à l'écran, pas un logiciel à chercher.
                generic_window_words = {
                    "fenêtre", "fenetre", "la fenêtre", "la fenetre",
                    "cette fenêtre", "cette fenetre", "window", "windows",
                    "toutes les fenêtres", "toutes les fenetres",
                }
                if query in generic_window_words:
                    self.speak(close_active_window())
                else:
                    name, _ = self.app_manager.find_app(query)
                    self.speak(self.app_manager.close_app(query))
                    if name:
                        self.memory.remember_command(command, "close", query)

            elif "oublie" in command and any(w in command for w in ["appris", "mémoire", "souviens"]):
                self.memory.reset_learned_commands()
                self.speak("C'est fait, j'ai oublié toutes les commandes que j'avais apprises.")

            elif "résumé" in command or "résume" in command:
                self.speak("Donne-moi le chemin du fichier texte, audio ou vidéo à résumer.")
                filepath = self._prompt_text("Chemin du fichier > ")
                if filepath:
                    self._handle_summary_request(filepath)

            elif "lis la page" in command or "lis cette page" in command:
                self.speak("Donne-moi l'adresse de la page.")
                url = self._prompt_text("URL > ")
                if url:
                    content = self.online.read_webpage(url)
                    self.speak(content)

            elif "recherche" in command or "cherche" in command:
                query = command.replace("recherche", "").replace("cherche", "").strip()
                if not query:
                    self.speak("Que veux-tu que je recherche ?")
                else:
                    self.speak(f"Je recherche {query}...")
                    results = self.online.search_web(query)
                    if results:
                        self.speak("Voici ce que j'ai trouvé : " + ". ".join(results[:3]))
                    else:
                        self.speak("Je n'ai rien trouvé, ou la connexion internet n'est pas disponible.")

            elif "liste" in command and "logiciel" in command:
                self.speak(f"J'ai détecté {len(self.app_manager.apps)} logiciels installés.")

            elif any(w in command for w in ["stop assistant", "au revoir", "exit"]):
                self.speak("Au revoir, passez une bonne journée !")
                return False

            elif len(command.split()) <= 3 and self.app_manager.find_app(command)[0]:
                # Nom de logiciel dit seul (ex: "chrome" ou "crome" au lieu
                # de "ouvre chrome") : on l'ouvre directement. Limité aux
                # commandes courtes (≤3 mots) pour ne pas risquer de
                # confondre une phrase plus longue avec un nom de logiciel.
                self.speak(self.app_manager.open_app(command))

            else:
                # Personne n'a matché : on cherche dans la mémoire des
                # commandes déjà apprises et confirmées par le passé, pour
                # tenter de prédire l'intention malgré une prononciation
                # ou une transcription imparfaite.
                match = self.memory.find_best_match(command)
                if match:
                    if match["type"] == "reply":
                        # Réponse apprise par texte : on la redit telle
                        # quelle, sans phrase d'introduction (pour rester
                        # naturel sur des questions/réponses simples).
                        self.speak(match["target"])
                    else:
                        self.speak(f"Je pense que tu voulais dire : {match['phrase']}. Je m'en occupe.")
                        if match["type"] == "open":
                            self.speak(self.app_manager.open_app(match["target"]))
                        elif match["type"] == "close":
                            self.speak(self.app_manager.close_app(match["target"]))
                else:
                    self.speak("Je ne sais pas encore comment répondre à ça.")
                    self._learn_reply_from_text(command)

            play_done_sound()
            self.maybe_suggest()

        except Exception as e:
            play_error_sound()
            self.speak("Une erreur est survenue pendant cette tâche.")
            print(f"[Erreur] {e}")

        return True

    def _prompt_text(self, prompt: str) -> str:
        """
        Demande une saisie clavier si une console interactive est
        disponible ; sinon prévient que cette fonction nécessite le mode
        console (impossible à saisir au clavier en mode arrière-plan).
        """
        if sys.stdin and sys.stdin.isatty():
            try:
                return input(prompt).strip().strip('"')
            except Exception:
                return ""
        self.speak(
            "Cette fonction nécessite de lancer l'assistant en mode console "
            "pour taper un chemin ou une adresse, pas en arrière-plan silencieux."
        )
        return ""

    def _learn_reply_from_text(self, command: str):
        """
        Quand une commande n'est pas reconnue, propose d'apprendre la bonne
        réponse — mais SAISIE AU CLAVIER, pas à la voix. Deux raisons :
        1) éviter de faire retranscrire par Whisper la réponse elle-même
           (source justement des erreurs qu'on essaie de contourner) ;
        2) permettre une réponse plus longue/précise que ce qu'on dirait
           à l'oral.
        La phrase entendue est mémorisée telle quelle, et la prochaine
        fois qu'une phrase PROCHE (pas forcément identique mot pour mot)
        sera dite, find_best_match() la retrouvera par similarité et
        redonnera directement la réponse apprise.
        """
        if not (sys.stdin and sys.stdin.isatty()):
            return  # pas de console interactive disponible, on ne peut pas taper
        print("\n  --- Apprentissage ---")
        print(f"  Commande non reconnue : \"{command}\"")
        reply = input(
            "  Tape ici ce que je devrais répondre la prochaine fois "
            "(ou appuie sur Entrée pour ignorer) > "
        ).strip()
        if not reply:
            print("  (ignoré, rien appris)")
            return
        self.memory.remember_command(command, "reply", reply)
        print(f"  Appris ! La prochaine fois qu'une phrase proche de "
              f"\"{command}\" sera dite, je répondrai : \"{reply}\"\n")

    def _handle_summary_request(self, filepath: str):
        if not os.path.exists(filepath):
            self.speak("Je ne trouve pas ce fichier.")
            return

        ext = os.path.splitext(filepath)[1].lower()
        self.speak("Je m'en occupe, un instant...")

        if ext in (".txt", ".md"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            summary = self.summarizer.summarize_text(text)

        elif ext in (".mp3", ".wav", ".m4a", ".flac") and self.stt:
            summary = self.summarizer.summarize_audio(filepath, self.stt)

        elif ext in (".mp4", ".mkv", ".avi", ".mov") and self.stt:
            summary = self.summarizer.summarize_video(filepath, self.stt)

        else:
            summary = "Ce type de fichier n'est pas pris en charge pour le résumé."

        self.speak("Voici le résumé : " + summary)

    # -- Boucle principale ---------------------------------------------------

    def run(self):
        mode = "hors ligne" if self.stt else "reconnaissance vocale indisponible"
        if self.memory.user_name:
            self.speak(f"Bonjour {self.memory.user_name} ! Assistant prêt, en mode {mode}.")
        else:
            self.speak(f"Bonjour ! Assistant prêt, en mode {mode}.")
        print(self.MENU_TEXT)
        self.speak("Pour aller plus vite, tu peux aussi dire juste un chiffre. "
                    "Le menu des raccourcis est affiché à l'écran.")
        self.paused = False
        self.should_run = True
        while self.should_run:
            if self.paused:
                time.sleep(0.5)
                continue
            command = self.listen()
            if not command:
                continue

            command = self._translate_shortcut(command)

            if self._needs_confirmation(command):
                if not self._confirm(command):
                    self.speak("D'accord, annulé. Dis-moi ce que tu veux faire.")
                    continue

            self.should_run = self.take_action(command)


def build_tray_icon(assistant: "VoiceAssistant"):
    """
    Crée l'icône dans la barre des tâches Windows, avec un menu
    Pause/Reprendre et Quitter, pour piloter l'assistant sans console.
    """
    import pystray
    from PIL import Image, ImageDraw

    def make_image():
        img = Image.new("RGB", (64, 64), "black")
        draw = ImageDraw.Draw(img)
        color = "gray" if assistant.paused else "deepskyblue"
        draw.ellipse((8, 8, 56, 56), fill=color)
        return img

    def toggle_pause(icon, item):
        assistant.paused = not assistant.paused
        icon.icon = make_image()
        icon.title = "Assistant Vocal (en pause)" if assistant.paused else "Assistant Vocal (actif)"
        print("Assistant en pause." if assistant.paused else "Assistant relancé.")

    def quit_app(icon, item):
        assistant.should_run = False
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(
            lambda item: "Reprendre" if assistant.paused else "Mettre en pause",
            toggle_pause,
        ),
        pystray.MenuItem("Quitter", quit_app),
    )

    icon = pystray.Icon("AssistantVocal", make_image(), "Assistant Vocal (actif)", menu)
    return icon


def main():
    background_mode = "--background" in sys.argv

    if "--install-startup" in sys.argv:
        setup_logging()
        register_startup(True)
        return
    if "--uninstall-startup" in sys.argv:
        setup_logging()
        register_startup(False)
        return

    if background_mode:
        setup_logging()

    if os.name != "nt":
        print("Attention : le scan des logiciels installés fonctionne uniquement sous Windows.")

    set_low_priority()

    try:
        assistant = VoiceAssistant(language="fr", use_local_stt=True, whisper_model="small")
    except Exception as e:
        print(f"Erreur au démarrage de l'assistant : {e}")
        sys.exit(1)

    if background_mode:
        # Mode arrière-plan : l'assistant tourne dans un thread séparé,
        # piloté depuis l'icône de la barre des tâches (thread principal).
        worker = threading.Thread(target=assistant.run, daemon=True)
        worker.start()

        icon = build_tray_icon(assistant)
        try:
            icon.run()  # bloque le thread principal, requis par pystray sous Windows
        except KeyboardInterrupt:
            assistant.should_run = False
    else:
        try:
            assistant.run()
        except KeyboardInterrupt:
            print("\nArrêt de l'assistant.")
            sys.exit(0)


if __name__ == "__main__":
    main()
