import os
import sys
import time
import signal
import threading
import subprocess
import requests
from pathlib import Path
from flask import Flask, render_template, send_from_directory, jsonify, request, session, redirect, url_for, flash
from functools import wraps
from dotenv import load_dotenv
import logging
import secrets
from datetime import datetime
import werkzeug.serving

# Charger les variables d'environnement
load_dotenv()

# Configuration du logging pour le main
def setup_logging():
    """Configure le logging avec rotation des fichiers"""
    logs_dir = Path(__file__).parent.absolute() / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    # Fichier de log pour le launcher
    launcher_log_file = logs_dir / f"launcher_{datetime.now().strftime('%Y%m%d')}.log"
    
    # Configuration du logger principal
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(launcher_log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Supprimer les logs de Werkzeug/Flask de la console
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.handlers = []
    werkzeug_logger.addHandler(logging.FileHandler(launcher_log_file, encoding='utf-8'))
    werkzeug_logger.setLevel(logging.WARNING)
    
    return logging.getLogger('LAUNCHER'), launcher_log_file

logger, launcher_log_file = setup_logging()


class MonitoringLauncher:
    def __init__(self):
        self.base_dir = Path(__file__).parent.absolute()
        self.frontend_dir = self.base_dir / "frontend"
        self.backend_dir = self.base_dir / "backend"
        self.logs_dir = self.base_dir / "logs"
        self.history_dir = self.base_dir / "history"
        self.backend_process = None
        
        # Configuration depuis les variables d'environnement
        self.backend_url = os.getenv('BACKEND_URL')
        self.flask_host = os.getenv('FLASK_HOST')
        self.flask_port = int(os.getenv('FLASK_PORT'))
        self.flask_debug = os.getenv('FLASK_DEBUG')
        
        self.ping_thread = None
        self.stop_ping = False
        self.app = Flask(__name__)
        
        # État pour éviter le spam des logs
        self.last_api_status = None  # True=online, False=offline, None=unknown
        self.api_status_changed = False
        
        # Configuration Flask
        self.app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
        self.app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Pas de cache en dev
        
        # Créer les dossiers nécessaires
        self.setup_directories()
        
        # Configurer les routes
        self.setup_routes()
    
    def setup_directories(self):
        """Créer uniquement les dossiers nécessaires (logs et history)"""
        for directory in [self.logs_dir, self.history_dir]:
            directory.mkdir(exist_ok=True)
            
        # Créer le fichier de log de l'API s'il n'existe pas
        self.backend_log_file = self.logs_dir / f"api_{datetime.now().strftime('%Y%m%d')}.log"
        logger.info(f"📝 Logs de l'API: {self.backend_log_file}")
        logger.info(f"📝 Logs du serveur: {launcher_log_file}")
        logger.info(f"📁 Dossier history: {self.history_dir}")
    
    def ping_backend_api(self):
        """Ping l'API backend pour vérifier qu'elle est en ligne"""
        try:
            response = requests.get(f"{self.backend_url}/api/health", timeout=3)
            if response.status_code == 200:
                return True, response.json()
            else:
                return False, f"Status code: {response.status_code}"
        except requests.exceptions.ConnectionError:
            return False, "Connexion refusée"
        except requests.exceptions.Timeout:
            return False, "Timeout (3s)"
        except Exception as e:
            return False, str(e)
    
    def continuous_ping(self):
        """Thread qui ping l'API en continu"""
        logger.info("🔄 Démarrage du monitoring de l'API backend")
        
        consecutive_failures = 0
        
        while not self.stop_ping:
            is_online, result = self.ping_backend_api()
            
            # Vérifier si le statut a changé
            status_changed = self.last_api_status != is_online
            
            if is_online:
                if status_changed:
                    if consecutive_failures > 0:
                        logger.info(f"🟢 API Backend RÉCUPÉRÉE après {consecutive_failures} échecs - {result}")
                    else:
                        logger.info(f"🟢 API Backend ONLINE - {result}")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if status_changed or consecutive_failures == 1:
                    logger.warning(f"🔴 API Backend OFFLINE - {result}")
                elif consecutive_failures % 10 == 0:  # Log toutes les 10 tentatives (2.5 minutes)
                    logger.warning(f"🔴 API Backend toujours OFFLINE après {consecutive_failures} tentatives - {result}")
                
                # Vérifier si le processus backend est toujours en vie
                if self.backend_process and self.backend_process.poll() is not None:
                    logger.error(f"💀 Processus backend terminé avec le code: {self.backend_process.returncode}")
                    break
            
            # Mettre à jour le statut
            self.last_api_status = is_online
            
            # Attendre 15 secondes avant le prochain ping
            for _ in range(150):  # 150 * 0.1 = 15 secondes
                if self.stop_ping:
                    break
                time.sleep(0.1)
        
        logger.info("🛑 Arrêt du monitoring de l'API backend")
    
    def setup_routes(self):
        """Configure les routes Flask"""
        
        @self.app.route('/')
        def index():
            """Serve la page principale"""
            return send_from_directory(self.frontend_dir, 'index.html')
        
        @self.app.route('/frontend/<path:filename>')
        def frontend_files(filename):
            """Serve les fichiers statiques du frontend"""
            return send_from_directory(self.frontend_dir, filename)
        
        @self.app.route('/api/<path:path>')
        def proxy_api(path):
            """Proxy vers l'API backend"""
            try:
                # Vérifier d'abord si l'API est en ligne
                is_online, _ = self.ping_backend_api()
                if not is_online:
                    logger.error("🔴 Tentative de proxy vers API offline")
                    return jsonify({"error": "API backend non disponible"}), 503
                
                # Forwarder la requête vers l'API backend
                backend_url = f"{self.backend_url}/api/{path}"
                
                if hasattr(request, 'args'):
                    params = request.args.to_dict()
                    response = requests.get(backend_url, params=params, timeout=10)
                else:
                    response = requests.get(backend_url, timeout=10)
                
                return jsonify(response.json())
            except Exception as e:
                logger.error(f"Erreur proxy API: {e}")
                return jsonify({"error": "API backend non disponible"}), 503
        
        @self.app.route('/health')
        def health():
            """Point de santé du service principal"""
            backend_online, backend_info = self.ping_backend_api()
            
            return jsonify({
                "status": "healthy",
                "frontend": "active",
                "backend": {
                    "status": "online" if backend_online else "offline",
                    "info": backend_info,
                    "process": "running" if self.backend_process else "stopped"
                },
                "config": {
                    "backend_url": self.backend_url,
                    "flask_host": self.flask_host,
                    "flask_port": self.flask_port
                }
            })
        
    
    def start_backend(self):
        """Lance le processus backend"""
        backend_file = self.backend_dir / "api.py"
        
        if not backend_file.exists():
            logger.error(f"Fichier backend non trouvé: {backend_file}")
            logger.info("Veuillez placer votre api.py dans le dossier backend/")
            return False
        
        try:
            # Test d'abord si le fichier peut être compilé
            test_result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(backend_file)],
                capture_output=True,
                text=True
            )
            
            if test_result.returncode != 0:
                logger.error(f"❌ Erreur de compilation dans api.py:")
                logger.error(test_result.stderr)
                return False
            
            # Lancer le backend avec les logs redirigés vers un fichier
            cmd = [sys.executable, str(backend_file)]
            
            logger.info(f"🚀 Démarrage du backend: {' '.join(cmd)}")
            
            # Ouvrir le fichier de log en mode append
            log_file = open(self.backend_log_file, 'a', encoding='utf-8')
            
            # Écrire un séparateur dans le fichier de log
            log_file.write(f"\n{'='*60}\n")
            log_file.write(f"DÉMARRAGE API - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.write(f"{'='*60}\n")
            log_file.flush()
            
            self.backend_process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
                cwd=str(self.backend_dir),
                encoding='utf-8',
                errors='replace'
            )
            
            # Garder une référence au fichier de log pour pouvoir le fermer plus tard
            self.backend_log_file_handle = log_file
            
            # Attendre un peu pour vérifier que le backend démarre
            time.sleep(3)
            
            if self.backend_process.poll() is not None:
                logger.error(f"❌ Backend terminé avec le code: {self.backend_process.returncode}")
                # Lire les dernières lignes du fichier de log pour diagnostiquer
                try:
                    with open(self.backend_log_file, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        if lines:
                            logger.error("Dernières lignes du log:")
                            for line in lines[-10:]:  # Afficher les 10 dernières lignes
                                logger.error(f"  {line.strip()}")
                except:
                    pass
                return False
            
            # Test ping pour vérifier que l'API répond
            logger.info("🔍 Test de connexion à l'API backend...")
            max_attempts = 10
            for attempt in range(max_attempts):
                is_online, result = self.ping_backend_api()
                if is_online:
                    logger.info(f"✅ API Backend accessible - {result}")
                    
                    # Démarrer le monitoring continu
                    self.ping_thread = threading.Thread(
                        target=self.continuous_ping,
                        daemon=True
                    )
                    self.ping_thread.start()
                    
                    return True
                else:
                    logger.info(f"⏳ Tentative {attempt + 1}/{max_attempts} - API non disponible: {result}")
                    time.sleep(1)
            
            logger.error("❌ L'API backend ne répond pas après le démarrage")
            return False
                
        except Exception as e:
            logger.error(f"Erreur lors du démarrage du backend: {e}")
            return False
    
    def stop_backend(self):
        """Arrête le processus backend"""
        # Arrêter le ping
        self.stop_ping = True
        if self.ping_thread:
            self.ping_thread.join(timeout=2)
        
        # Arrêter le backend
        if self.backend_process:
            logger.info("🛑 Arrêt du backend...")
            
            # Écrire un message d'arrêt dans le log
            try:
                if hasattr(self, 'backend_log_file_handle'):
                    self.backend_log_file_handle.write(f"\n{'='*60}\n")
                    self.backend_log_file_handle.write(f"ARRÊT API - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    self.backend_log_file_handle.write(f"{'='*60}\n")
                    self.backend_log_file_handle.flush()
            except:
                pass
            
            self.backend_process.terminate()
            
            # Attendre l'arrêt propre
            try:
                self.backend_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Force l'arrêt du backend")
                self.backend_process.kill()
                self.backend_process.wait()
            
            # Fermer le fichier de log
            try:
                if hasattr(self, 'backend_log_file_handle'):
                    self.backend_log_file_handle.close()
            except:
                pass
            
            self.backend_process = None
            logger.info("✅ Backend arrêté")
    
    def run(self):
        """Lance le système complet"""
        print("=" * 60)
        print("🚀 DÉMARRAGE D'UPTIMECORE")
        print("=" * 60)
        
        # Vérifier la structure du projet
        print(f"📁 Dossier racine: {self.base_dir}")
        print(f"📁 Frontend: {self.frontend_dir}")
        print(f"📁 Backend: {self.backend_dir}")
        print(f"📁 Logs: {self.logs_dir}")
        print(f"📁 History: {self.history_dir}")
        
        # Afficher la configuration
        print("🔧 Configuration:")
        print(f"   Backend URL: {self.backend_url}")
        print(f"   Host: {self.flask_host}")
        print(f"   Port: {self.flask_port}")
        
        # Démarrer le backend
        if not self.start_backend():
            print("❌ Impossible de démarrer le backend")
            return
        
        # Démarrer le frontend
        print("=" * 60)
        print("🌐 DÉMARRAGE DU FRONTEND")
        print("=" * 60)
        print(f"📡 URL: http://{self.flask_host}:{self.flask_port}")
        print(f"🔗 Frontend: http://{self.flask_host}:{self.flask_port}/")
        print(f"🔗 API: http://{self.flask_host}:{self.flask_port}/api/")
        print(f"🔗 Santé: http://{self.flask_host}:{self.flask_port}/health")
        print()
        print("💡 Appuyez sur Ctrl+C pour arrêter")
        print(f"📝 Logs de l'API: {self.backend_log_file}")
        print(f"📝 Logs du launcher: {launcher_log_file}")
        print("-" * 60)
        
        try:
            # Désactiver les logs Flask dans la console
            cli = sys.modules['flask.cli']
            cli.show_server_banner = lambda *x: None
            
            # Rediriger les logs werkzeug vers le fichier
            import logging
            werkzeug_logger = logging.getLogger('werkzeug')
            werkzeug_logger.setLevel(logging.ERROR)
            
            # Lancer Flask
            self.app.run(
                host=self.flask_host,
                port=self.flask_port,
                debug=self.flask_debug,
                use_reloader=False  # Éviter les problèmes avec le backend
            )
        except KeyboardInterrupt:
            print("\n🛑 Arrêt demandé par l'utilisateur")
        except Exception as e:
            logger.error(f"Erreur Flask: {e}")
            print(f"❌ Erreur Flask: {e}")
        finally:
            self.stop_backend()
            print("👋 Arrêt complet du système")

def signal_handler(signum, frame):
    """Gestionnaire de signal pour arrêt propre"""
    print("\n🛑 Signal d'arrêt reçu")
    sys.exit(0)

def main():
    """Point d'entrée principal"""
    # Gestionnaire de signal
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Créer et lancer le système
    launcher = MonitoringLauncher()
    
    try:
        launcher.run()
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        print(f"❌ Erreur fatale: {e}")
        launcher.stop_backend()
        sys.exit(1)

if __name__ == "__main__":
    main()