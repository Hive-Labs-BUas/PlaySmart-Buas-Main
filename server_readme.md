# The Hive Server — Infrastructure Overview

## Server Access

| Property | Value        |
|----------|-------       |
| OS       | Ubuntu Linux |
| Host     | `10.4.28.2`  |
| SSH user | `private`    |

```bash
ssh private@10.4.28.2
```

---

## Server Maintenance

Server updates can be performed at any time without impacting the running services — containers are self-contained and will continue operating normally during a host OS update.

At the start of each semester, the new intern should check for available updates and apply them if needed.

```bash
# Check for available updates
sudo apt update
 
# Review what will be upgraded (optional)
sudo apt list --upgradable
 
# Apply updates
sudo apt upgrade -y
 
# Apply major/system-level upgrades (if needed)
sudo apt full-upgrade -y
 
# Remove unused packages
sudo apt autoremove -y
```

> After a kernel update, a server reboot may be required. If so, coordinate with the team first as this **will** briefly bring down all containers.

```bash
# Check if a reboot is required
cat /var/run/reboot-required
 
# Reboot the server (only if necessary and coordinated)
sudo reboot
```

---

## Server Navigation

### Key host directories

| Path | Description |
|------|-------------|
| `/mnt/raid0/esports/server/` | Docker Compose project root |
| `/mnt/raid0/esports/server/python_app/` | Python pipeline app code |
| `/mnt/raid0/esports/server/postgres/` | PostgreSQL init scripts |
| `/mnt/raid0/esports/sftp_data/` | Incoming SFTP uploads (raw data) |
| `/mnt/raid0/esports/data/gaze/` | Processed gaze data |
| `/mnt/raid0/esports/data/merged/` | Merged session data |
| `/mnt/raid0/esports/data/input/` | Input data |
| `/mnt/raid0/esports/data/emotion/` | OpenFace emotion data |
| `/mnt/raid0/esports/data/video/` | Video recordings |
| `/mnt/raid0/esports/postgresql_data/` | PostgreSQL persistent data volume |
| `/mnt/raid0/cradle/Play-O-Meter-Y3AB-cradle-2025-2026/infrastructure/` | Play-Smart (cradle) infrastructure |

### Navigating to common locations

```bash
# Go to the main Docker Compose project
cd /mnt/raid0/esports/server
 
# Go to the Python pipeline app
cd /mnt/raid0/esports/server/python_app
 
# Browse incoming SFTP data
cd /mnt/raid0/esports/sftp_data
 
# Browse processed data
cd /mnt/raid0/esports/data
```

### Volume mount map (host → container)

| Host path | Container path |
|-----------|---------------|
| `/mnt/raid0/esports/server/python_app` | `/app` |
| `/mnt/raid0/esports/sftp_data` | `/shared_data` |
| `/mnt/raid0/esports/data/gaze` | `/app/data/gaze` |
| `/mnt/raid0/esports/data/merged` | `/app/data/merged` |
| `/mnt/raid0/esports/data/input` | `/app/data/input` |
| `/mnt/raid0/esports/data/emotion` | `/app/data/emotion` |
| `/mnt/raid0/esports/data/video` | `/app/data/video` |
| `/mnt/raid0/esports/postgresql_data` | `/var/lib/postgresql/data` |

> These are **host-level mounts** — data is accessible directly on the server regardless of whether containers are running.

### Working with containers

```bash
# List all running containers
docker ps
 
# List all containers (including stopped)
docker ps -a
 
# Open a shell inside a running container
docker exec -it <container_name> bash
 
# Open a shell in the Python app container
docker exec -it <python_container_name> bash
 
# Run a one-off command inside a container
docker exec <container_name> python auto_merge.py
 
# View live logs
docker logs <container_name>
 
# Follow logs in real time
docker logs -f <container_name>
 
# View last N lines of logs
docker logs --tail 100 <container_name>
```

### Managing the stack

```bash
# Start the full stack (detached)
docker compose up -d
 
# Start and rebuild images
docker compose up -d --build
 
# Stop the full stack
docker compose down
 
# Stop and remove volumes (destructive!)
docker compose down -v
 
# Pull latest images from Docker Hub
docker compose pull
 
# Restart a single service
docker compose restart <service_name>
 
# View status of all services
docker compose ps
```

### Inspecting the filesystem from host (no container needed)

```bash
# List files in a data directory
ls -lh /mnt/raid0/esports/sftp_data/gaze
 
# Check disk usage per subdirectory
du -sh /mnt/raid0/esports/data/*/
 
# Count files in a directory
find /mnt/raid0/esports/sftp_data -type f | wc -l
 
# Check file types present
find /mnt/raid0/esports/data -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn
 
# Search for code referencing a specific path
grep -r "sftp_data" /mnt/raid0/esports/server/python_app --include="*.py" -l
```

---

## Useful Commands

```bash
# View running containers
docker ps
 
# View logs for a container
docker logs <container_name>
 
# Restart the full stack
docker compose down && docker compose up -d
 
# Pull latest images
docker compose pull
```

---

## Project: Play-Smart (B.G.E.T.)

The server hosts the **Breda Guardians Esports Tool (B.G.E.T.)**, the backend infrastructure for the **Play-Smart** esports analytics platform. It collects and processes multimodal biometric data from esports players and presents it through a web dashboard.

---

## Deployed Stack (Docker Compose)

|         Service          |                   Description                     |      Docker Hub Image       |
|--------------------------|---------------------------------------------------|-----------------------------|
| **FastAPI backend**      | REST API serving player analytics data            | `220755842/bg-app-backend`  |
| **Vite/React frontend**  | Web dashboard UI                                  | `220755842/bg-app-frontend` |
| **Nginx**                | Reverse proxy in front of frontend/backend        | —                           |
| **PostgreSQL / bget.db** | Persistent database volume                        | —                           |
| **SFTP container**       | Receives uploaded data files from player machines | —                           |
| **Pipeline containers**  | Python data processing scripts, cron-scheduled    | —                           |

### Frontend build note

The frontend requires a build argument at image build time:

```bash
--build-arg VITE_BACKEND_URL=<backend_url>
```

---

## Data Pipeline Scripts

Scheduled via **cron** inside pipeline containers:

| Script                    | Purpose                                  |
|---------------------------|------------------------------------------|
| `auto_merge.py`           | Merges incoming raw data files           |
| `auto_cleaning_and_dq.py` | Cleans data and runs data quality checks |
| `auto_dq_report.py`       | Generates data quality reports           |

---

## Data Collection (Windows-side)

Player biometric data is collected locally on **Windows machines** using a toolkit launcher:

- `main.bat` — entry point launcher
- **Pygame visualization** — real-time feedback during sessions
- **Tobii eye tracker integration** — gaze & fixation data
- **Keyboard/mouse input logging**
- **OpenFace emotion recognition** — facial action unit data

Collected data is pushed to the server via the SFTP container and processed through the pipeline into the dashboard.

---

## Known Issues Resolved

- Dockerfile build-arg ordering for `VITE_BACKEND_URL`
- `bget.db` volume mount misconfiguration
- Nested Git repository issues in the codebase
- Poetry environment setup for pipeline scripts

---

## Useful Commandss

```bash
# View running containers
docker ps

# View logs for a container
docker logs <container_name>

# Restart the full stack
docker compose down && docker compose up -d

# Pull latest images
docker compose pull
```
