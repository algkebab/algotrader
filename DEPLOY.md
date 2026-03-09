# Deploy to VPS (Option A: SSH + Docker Compose)

Automatic deployment runs on **push to `main`**: GitHub Actions SSHs into your VPS and runs `git pull` + `docker compose up -d --build`.

---

## 1. On the VPS (one-time setup)

### 1.1 Install Docker and Docker Compose

- Install **Docker Engine** and **Docker Compose** (v2, plugin or standalone).
- Example (Debian/Ubuntu):
  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER
  # log out and back in so docker group applies
  docker compose version
  ```

### 1.2 Create a deploy user (recommended)

- Create a user that will run the app and receive SSH from GitHub:
  ```bash
  sudo adduser deploy
  sudo usermod -aG docker deploy
  ```
- Use a strong password or disable password login and use only SSH keys.

### 1.3 SSH key for GitHub Actions

- On your **local machine** (or any machine), generate a key **only for deploy**:
  ```bash
  ssh-keygen -t ed25519 -C "github-actions-deploy" -f deploy_key -N ""
  ```
- **Public key** → on VPS, add to deploy user:
  ```bash
  sudo -u deploy mkdir -p /home/deploy/.ssh
  sudo -u deploy chmod 700 /home/deploy/.ssh
  # Paste contents of deploy_key.pub into:
  sudo -u deploy nano /home/deploy/.ssh/authorized_keys
  sudo -u deploy chmod 600 /home/deploy/.ssh/authorized_keys
  ```
- **Private key** (`deploy_key`, no .pub) → you will paste this into a GitHub secret (step 2.2).

### 1.4 Clone repo and set app directory

- As `deploy` (or your chosen user):
  ```bash
  sudo -u deploy bash
  cd /opt   # or home, e.g. /home/deploy
  sudo mkdir -p /opt/algotrader
  sudo chown deploy:deploy /opt/algotrader
  git clone https://github.com/YOUR_USERNAME/algotrader.git /opt/algotrader
  cd /opt/algotrader
  ```
- Note the path; you’ll use it as `DEPLOY_PATH` in GitHub (e.g. `/opt/algotrader`).

### 1.5 Create `.env` on the VPS

- In the app directory:
  ```bash
  cd /opt/algotrader
  cp .env.example .env
  nano .env
  ```
- Fill in all values (Binance, Telegram, etc.). **Do not commit `.env`**; it stays only on the VPS.

### 1.6 (Optional) Start on boot

- To bring the stack up after a reboot, use one of:
  - **systemd**: create a unit that runs `docker compose up -d` in the app directory.
  - **cron**: `@reboot cd /opt/algotrader && docker compose up -d`

### 1.7 Firewall

- Open only what you need (e.g. SSH). No need to expose Redis or app ports unless you add a web UI/API later.

---

## 2. In GitHub (one-time setup)

### 2.1 Open repository Secrets

- Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

### 2.2 Add these secrets

| Secret name             | Description |
|-------------------------|-------------|
| `VPS_HOST`              | VPS IP or hostname (e.g. `203.0.113.10` or `vps.example.com`). |
| `VPS_USER`              | SSH user (e.g. `deploy`). |
| `VPS_SSH_PRIVATE_KEY`   | **Full** contents of the **private** key file (e.g. `deploy_key`), including `-----BEGIN ... KEY-----` and `-----END ... KEY-----`. |
| `DEPLOY_PATH` (optional) | Path to the repo on the VPS (e.g. `/opt/algotrader`). If not set, workflow uses `/opt/algotrader`. |
| `SSH_PORT` (optional)    | SSH port if not 22 (e.g. `2222`). |

---

## 3. After setup

- **Automatic**: every **push to `main`** runs the deploy workflow (pull + `docker compose up -d --build`).
- **Manual**: **Actions** → **Deploy to VPS** → **Run workflow**.

---

## 4. Troubleshooting

- **Permission denied (publickey)**  
  - Check that the public key is in `~/.ssh/authorized_keys` for `VPS_USER` and that the private key in `VPS_SSH_PRIVATE_KEY` matches.
- **docker: command not found** or **permission denied**  
  - Ensure `VPS_USER` is in the `docker` group (`usermod -aG docker VPS_USER`) and that the user has logged in again (or reboot).
- **No such file or directory** in deploy script  
  - Ensure `DEPLOY_PATH` (or `/opt/algotrader`) exists and is the directory that contains `docker-compose.yml` and the repo (so `git pull` and `docker compose` run there).
