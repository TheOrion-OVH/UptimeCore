# Étape 1 : image de base Python
FROM python:3.11-slim

# Étape 2 : définir le répertoire de travail
WORKDIR /app

# Étape 3 : copier les fichiers de config et de dépendances
COPY requirements.txt .

# Étape 4 : installer les dépendances
RUN pip install --no-cache-dir -r requirements.txt

# Étape 5 : copier tous les fichiers du projet
COPY . .

# Étape 6 : exposer le port si besoin (ex. : 8080)
EXPOSE 8080

# Étape 7 : lancer le serveur
CMD ["python", "server.py"]