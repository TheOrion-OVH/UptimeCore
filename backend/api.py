from flask import Flask, jsonify, request
import json
import os
import ping3
import requests
import socket
from datetime import datetime, timedelta
import threading
import time
import logging
from typing import Dict, List, Any

app = Flask(__name__)

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MonitoringService:
    def __init__(self):
        # Configuration avec valeurs fixes - À LA RACINE DU PROJET
        # On remonte d'un niveau depuis backend/ pour atteindre la racine
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)  # Remonte au parent
        
        self.config_file = os.path.join(project_root, 'config.json')
        self.history_dir = os.path.join(project_root, 'history')
        self.history_interval = 60
        self.check_interval = 10
        self.max_probes = 100
        self.history_retention_days = 30
        
        self.probes = []
        self.current_status = {}
        self.previous_status = {}
        self.monitoring_active = False
        self.last_history_save = time.time()
        
        # Debug : afficher les chemins calculés
        logger.info(f"Dossier courant: {current_dir}")
        logger.info(f"Racine projet: {project_root}")
        logger.info(f"Config file: {self.config_file}")
        logger.info(f"History dir: {self.history_dir}")
        
        # Vérifier que les fichiers existent avant de continuer
        if not os.path.exists(self.config_file):
            logger.error(f"❌ Fichier config.json introuvable: {self.config_file}")
            logger.error(f"Contenu du dossier racine: {os.listdir(project_root)}")
        else:
            logger.info(f"✅ Fichier config.json trouvé: {self.config_file}")
        
        if not os.path.exists(self.history_dir):
            logger.error(f"❌ Dossier history introuvable: {self.history_dir}")
        else:
            logger.info(f"✅ Dossier history trouvé: {self.history_dir}")
        
        # Créer le dossier history s'il n'existe pas (mais normalement il existe déjà)
        self.ensure_history_directory()
        
        # Charger la configuration
        self.load_config()
        
        # Démarrer le monitoring en arrière-plan
        self.start_monitoring()
    
    def ensure_history_directory(self):
        """S'assure que le dossier history existe à la racine du projet"""
        try:
            if not os.path.exists(self.history_dir):
                os.makedirs(self.history_dir)
                logger.info(f"Dossier history créé: {self.history_dir}")
            else:
                logger.info(f"Dossier history trouvé: {self.history_dir}")
                
            # Vérifier les permissions d'écriture
            test_file = os.path.join(self.history_dir, 'test_write.tmp')
            try:
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                logger.info("Permissions d'écriture OK sur le dossier history")
            except Exception as e:
                logger.error(f"Erreur de permissions sur le dossier history: {e}")
                
        except Exception as e:
            logger.error(f"Erreur lors de la création du dossier history: {e}")
            raise
    
    def load_config(self):
        """Charge la configuration depuis le fichier JSON"""
        try:
            logger.info(f"Tentative de chargement de la configuration: {self.config_file}")
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.probes = config.get('probes', [])
                    
                    # Limiter le nombre de sondes
                    if len(self.probes) > self.max_probes:
                        self.probes = self.probes[:self.max_probes]
                        logger.warning(f"Nombre de sondes limité à {self.max_probes}")
                    
                    logger.info(f"Configuration chargée avec succès: {len(self.probes)} sondes")
            else:
                logger.error(f"Fichier de configuration non trouvé: {self.config_file}")
                logger.error(f"Fichiers présents dans le dossier: {os.listdir(os.path.dirname(self.config_file))}")
                self.probes = []
        except Exception as e:
            logger.error(f"Erreur lors du chargement de la configuration: {e}")
            self.probes = []
    
    def ping_check(self, target: str, timeout: int = 5, threshold: int = 100) -> Dict[str, Any]:
        """Effectue un ping vers la cible"""
        try:
            response_time = ping3.ping(target, timeout=timeout)
            
            if response_time is None:
                return {
                    "status": "offline",
                    "response_time": None,
                    "error": "Pas de réponse"
                }
            
            response_time_ms = response_time * 1000
            
            if response_time_ms > threshold:
                status = "slow"
            else:
                status = "online"
            
            return {
                "status": status,
                "response_time": round(response_time_ms, 2),
                "error": None
            }
            
        except Exception as e:
            return {
                "status": "error",
                "response_time": None,
                "error": str(e)
            }
    
    def http_check(self, target: str, timeout: int = 10, expected_status: int = 200) -> Dict[str, Any]:
        """Effectue une vérification HTTP"""
        try:
            start_time = time.time()
            response = requests.get(target, timeout=timeout, allow_redirects=True)
            response_time = (time.time() - start_time) * 1000
            
            if response.status_code == expected_status:
                status = "online"
            else:
                status = "error"
            
            return {
                "status": status,
                "response_time": round(response_time, 2),
                "http_status": response.status_code,
                "error": None if status == "online" else f"Status code: {response.status_code}"
            }
            
        except requests.exceptions.Timeout:
            return {
                "status": "timeout",
                "response_time": None,
                "http_status": None,
                "error": "Timeout"
            }
        except Exception as e:
            return {
                "status": "offline",
                "response_time": None,
                "http_status": None,
                "error": str(e)
            }
    
    def tcp_check(self, target: str, port: int, timeout: int = 5) -> Dict[str, Any]:
        """Effectue une vérification TCP"""
        try:
            start_time = time.time()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            
            result = sock.connect_ex((target, port))
            response_time = (time.time() - start_time) * 1000
            sock.close()
            
            if result == 0:
                return {
                    "status": "online",
                    "response_time": round(response_time, 2),
                    "error": None
                }
            else:
                return {
                    "status": "offline",
                    "response_time": round(response_time, 2),
                    "error": f"Connexion refusée (code: {result})"
                }
                
        except Exception as e:
            return {
                "status": "error",
                "response_time": None,
                "error": str(e)
            }
    
    def check_probe(self, probe: Dict[str, Any]) -> Dict[str, Any]:
        """Vérifie une sonde selon son type"""
        timestamp = datetime.now().isoformat()
        
        if probe['type'] == 'ping':
            result = self.ping_check(
                probe['target'],
                probe.get('timeout', 5),
                probe.get('threshold', 100)
            )
        elif probe['type'] == 'http':
            result = self.http_check(
                probe['target'],
                probe.get('timeout', 10),
                probe.get('expected_status', 200)
            )
        elif probe['type'] == 'tcp':
            result = self.tcp_check(
                probe['target'],
                probe['port'],
                probe.get('timeout', 5)
            )
        else:
            result = {
                "status": "error",
                "error": f"Type de sonde non supporté: {probe['type']}"
            }
        
        return {
            "id": probe['id'],
            "name": probe['name'],
            "type": probe['type'],
            "target": probe['target'],
            "timestamp": timestamp,
            **result
        }
    
    def has_status_changed(self, probe_id: str, new_status: str) -> bool:
        """Vérifie si le statut a changé par rapport à la dernière vérification"""
        if probe_id not in self.previous_status:
            return True
        
        return self.previous_status[probe_id] != new_status
    
    def clean_old_history(self):
        """Nettoie l'historique ancien selon la rétention configurée"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.history_retention_days)
            
            for filename in os.listdir(self.history_dir):
                if filename.endswith('.json'):
                    try:
                        file_date_str = filename.replace('.json', '')
                        file_date = datetime.strptime(file_date_str, '%Y-%m-%d')
                        
                        if file_date < cutoff_date:
                            file_path = os.path.join(self.history_dir, filename)
                            os.remove(file_path)
                            logger.info(f"Historique supprimé: {filename}")
                    except ValueError:
                        continue
        except Exception as e:
            logger.error(f"Erreur lors du nettoyage de l'historique: {e}")
    
    def save_current_status_to_history(self):
        """Sauvegarde le statut actuel de toutes les sondes dans l'historique"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            history_file = os.path.join(self.history_dir, f"{today}.json")
            
            if os.path.exists(history_file):
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            else:
                history = []
            
            current_time = datetime.now().isoformat()
            for probe_id, status in self.current_status.items():
                change_type = "periodic_save"
                if probe_id not in self.previous_status:
                    change_type = "initial"
                elif self.previous_status[probe_id] != status.get('status'):
                    change_type = "status_change"
                
                history_entry = {
                    **status,
                    "timestamp": current_time,
                    "change_type": change_type,
                    "previous_status": self.previous_status.get(probe_id, "unknown")
                }
                
                history.append(history_entry)
            
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Historique sauvegardé pour {len(self.current_status)} sondes")
            
            # Nettoyer l'historique ancien
            self.clean_old_history()
            
        except Exception as e:
            logger.error(f"Erreur lors de la sauvegarde de l'historique: {e}")
    
    def save_status_change(self, probe_result: Dict[str, Any], change_type: str):
        """Sauvegarde un changement de statut immédiat dans l'historique"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            history_file = os.path.join(self.history_dir, f"{today}.json")
            
            if os.path.exists(history_file):
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            else:
                history = []
            
            status_change = {
                **probe_result,
                "change_type": change_type,
                "previous_status": self.previous_status.get(probe_result['id'], "unknown")
            }
            
            history.append(status_change)
            
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
                
            logger.info(f"Changement d'état immédiat sauvegardé pour {probe_result['id']}: {change_type}")
                
        except Exception as e:
            logger.error(f"Erreur lors de la sauvegarde du changement: {e}")
    
    def monitoring_loop(self):
        """Boucle principale de monitoring"""
        while self.monitoring_active:
            try:
                for probe in self.probes:
                    result = self.check_probe(probe)
                    probe_id = probe['id']
                    new_status = result['status']
                    
                    if self.has_status_changed(probe_id, new_status):
                        change_type = "initial" if probe_id not in self.previous_status else "status_change"
                        self.save_status_change(result, change_type)
                        
                        if change_type == "initial":
                            logger.info(f"🔍 Sonde {probe_id}: état initial = {new_status}")
                        else:
                            previous = self.previous_status.get(probe_id, "unknown")
                            logger.info(f"🔄 Sonde {probe_id}: changement {previous} → {new_status}")
                    
                    self.previous_status[probe_id] = new_status
                    self.current_status[probe_id] = result
                
                current_time = time.time()
                if current_time - self.last_history_save >= self.history_interval:
                    self.save_current_status_to_history()
                    self.last_history_save = current_time
                
                time.sleep(self.check_interval)
                
            except Exception as e:
                logger.error(f"Erreur dans la boucle de monitoring: {e}")
                time.sleep(5)
    
    def start_monitoring(self):
        """Démarre le monitoring en arrière-plan"""
        if not self.monitoring_active:
            self.monitoring_active = True
            self.monitoring_thread = threading.Thread(target=self.monitoring_loop)
            self.monitoring_thread.daemon = True
            self.monitoring_thread.start()
            logger.info("Monitoring démarré")
    
    def stop_monitoring(self):
        """Arrête le monitoring"""
        self.monitoring_active = False
        logger.info("Monitoring arrêté")
    
    def get_history(self, date: str = None, probe_id: str = None) -> List[Dict[str, Any]]:
        """Récupère l'historique pour une date donnée"""
        try:
            if date is None:
                date = datetime.now().strftime('%Y-%m-%d')
            
            history_file = os.path.join(self.history_dir, f"{date}.json")
            
            if not os.path.exists(history_file):
                return []
            
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
            
            if probe_id:
                history = [h for h in history if h.get('id') == probe_id]
            
            return history
            
        except Exception as e:
            logger.error(f"Erreur lors de la récupération de l'historique: {e}")
            return []
    
    def get_probe_history_multiday(self, probe_id: str, days: int = 7) -> List[Dict[str, Any]]:
        """Récupère l'historique d'une sonde sur plusieurs jours"""
        try:
            all_history = []
            
            for i in range(days):
                date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                daily_history = self.get_history(date, probe_id)
                all_history.extend(daily_history)
            
            all_history.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            return all_history
            
        except Exception as e:
            logger.error(f"Erreur lors de la récupération de l'historique multi-jours: {e}")
            return []
    
    def get_status_changes_summary(self, date: str = None) -> Dict[str, Any]:
        """Récupère un résumé des changements d'état pour une date"""
        history = self.get_history(date)
        
        summary = {
            "total_entries": len(history),
            "entries_by_probe": {},
            "entries_by_type": {"initial": 0, "status_change": 0, "periodic_save": 0}
        }
        
        for entry in history:
            probe_id = entry.get('id', 'unknown')
            change_type = entry.get('change_type', 'unknown')
            
            if probe_id not in summary["entries_by_probe"]:
                summary["entries_by_probe"][probe_id] = []
            
            summary["entries_by_probe"][probe_id].append({
                "timestamp": entry.get('timestamp'),
                "status": entry.get('status'),
                "previous_status": entry.get('previous_status'),
                "change_type": change_type,
                "response_time": entry.get('response_time')
            })
            
            if change_type in summary["entries_by_type"]:
                summary["entries_by_type"][change_type] += 1
        
        return summary

# Instance globale du service
monitoring_service = MonitoringService()

# Routes API
@app.route('/api/status', methods=['GET'])
def get_status():
    """Récupère le statut actuel de toutes les sondes"""
    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "probes": monitoring_service.current_status
    })

@app.route('/api/status/<probe_id>', methods=['GET'])
def get_probe_status(probe_id):
    """Récupère le statut d'une sonde spécifique"""
    if probe_id in monitoring_service.current_status:
        return jsonify(monitoring_service.current_status[probe_id])
    else:
        return jsonify({"error": "Sonde non trouvée"}), 404

@app.route('/api/history', methods=['GET'])
def get_history():
    """Récupère l'historique des changements d'état"""
    date = request.args.get('date')
    probe_id = request.args.get('probe_id')
    
    history = monitoring_service.get_history(date, probe_id)
    
    return jsonify({
        "date": date or datetime.now().strftime('%Y-%m-%d'),
        "probe_id": probe_id,
        "history": history
    })

@app.route('/api/history/<probe_id>', methods=['GET'])
def get_probe_history(probe_id):
    """Récupère l'historique d'une sonde spécifique sur plusieurs jours"""
    probe = next((p for p in monitoring_service.probes if p['id'] == probe_id), None)
    if not probe:
        return jsonify({"error": "Sonde non trouvée"}), 404
    
    days = int(request.args.get('days', 7))
    date = request.args.get('date')
    
    if date:
        history = monitoring_service.get_history(date, probe_id)
        response_date = date
    else:
        history = monitoring_service.get_probe_history_multiday(probe_id, days)
        response_date = f"{days} derniers jours"
    
    probe_info = {
        "id": probe['id'],
        "name": probe['name'],
        "type": probe['type'],
        "target": probe['target']
    }
    
    stats = {
        "total_entries": len(history),
        "status_distribution": {},
        "change_types": {"initial": 0, "status_change": 0, "periodic_save": 0}
    }
    
    for entry in history:
        status = entry.get('status', 'unknown')
        change_type = entry.get('change_type', 'unknown')
        
        if status not in stats["status_distribution"]:
            stats["status_distribution"][status] = 0
        stats["status_distribution"][status] += 1
        
        if change_type in stats["change_types"]:
            stats["change_types"][change_type] += 1
    
    return jsonify({
        "probe": probe_info,
        "period": response_date,
        "current_status": monitoring_service.current_status.get(probe_id, {"status": "unknown"}),
        "statistics": stats,
        "history": history
    })

@app.route('/api/history/summary', methods=['GET'])
def get_history_summary():
    """Récupère un résumé des changements d'état"""
    date = request.args.get('date')
    summary = monitoring_service.get_status_changes_summary(date)
    
    return jsonify({
        "date": date or datetime.now().strftime('%Y-%m-%d'),
        "summary": summary
    })

@app.route('/api/probes', methods=['GET'])
def get_probes():
    """Récupère la liste des sondes configurées"""
    return jsonify({
        "probes": monitoring_service.probes
    })

@app.route('/api/check/<probe_id>', methods=['POST'])
def manual_check(probe_id):
    """Effectue une vérification manuelle d'une sonde"""
    probe = next((p for p in monitoring_service.probes if p['id'] == probe_id), None)
    
    if not probe:
        return jsonify({"error": "Sonde non trouvée"}), 404
    
    result = monitoring_service.check_probe(probe)
    return jsonify(result)

@app.route('/api/health', methods=['GET'])
def health_check():
    """Point de santé de l'API"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "monitoring_active": monitoring_service.monitoring_active,
        "probes_count": len(monitoring_service.probes),
        "history_interval": monitoring_service.history_interval
    })

@app.route('/api/reload', methods=['POST'])
def reload_config():
    """Recharge la configuration"""
    try:
        monitoring_service.load_config()
        return jsonify({"message": "Configuration rechargée avec succès"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    try:
        print("🚀 Démarrage de l'API de monitoring...")
        print(f"📁 Fichier de configuration: {os.path.abspath(monitoring_service.config_file)}")
        print(f"📁 Dossier d'historique: {os.path.abspath(monitoring_service.history_dir)}")
        print(f"📊 {len(monitoring_service.probes)} sondes configurées")
        print(f"⏱️  Intervalle d'historique: {monitoring_service.history_interval}s")
        print()
        print("📡 Endpoints disponibles:")
        print("   GET  /api/health - Santé de l'API")
        print("   GET  /api/status - Statut de toutes les sondes")
        print("   GET  /api/status/<probe_id> - Statut d'une sonde")
        print("   GET  /api/history - Historique complet")
        print("   GET  /api/history/<probe_id> - Historique d'une sonde")
        print("   GET  /api/history/summary - Résumé de l'historique")
        print("   GET  /api/probes - Liste des sondes")
        print("   POST /api/check/<probe_id> - Vérification manuelle")
        print("   POST /api/reload - Recharger la configuration")
        print()
        
        app.run(debug=False, host='0.0.0.0', port=5000)
        
    except KeyboardInterrupt:
        print("\n🛑 Arrêt du monitoring...")
        monitoring_service.stop_monitoring()
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        monitoring_service.stop_monitoring()