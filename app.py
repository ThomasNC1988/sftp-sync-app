import os
import json
import stat
import paramiko
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- Configuration ---
SFTP_HOST = os.getenv('SFTP_HOST', '192.168.1.100')
SFTP_PORT = int(os.getenv('SFTP_PORT', 22))
SFTP_USER = os.getenv('SFTP_USER', 'username')
SFTP_KEY_PATH = os.getenv('SFTP_KEY_PATH', '/app/keys/private_key.pem') 
# Updated base remote directory
REMOTE_DIR = os.getenv('REMOTE_DIR', '/data/media/0/realdata/')
LOCAL_DIR = os.getenv('LOCAL_DIR', '/app/downloads/')
HISTORY_FILE = os.getenv('HISTORY_FILE', '/app/data/history.json')

# Ensure local base directories exist
os.makedirs(LOCAL_DIR, exist_ok=True)
os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def save_history(history):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(list(history), f)

def find_ts_files(sftp, current_dir):
    """
    Recursively searches an SFTP directory for .ts files.
    Returns a list of full remote file paths.
    """
    ts_files = []
    try:
        # listdir_attr gives us file attributes so we can check if it's a folder
        for item in sftp.listdir_attr(current_dir):
            # Construct the remote path using forward slashes (standard for SFTP/Linux)
            item_path = f"{current_dir.rstrip('/')}/{item.filename}"
            
            if stat.S_ISDIR(item.st_mode):
                # If it's a directory, dive into it
                ts_files.extend(find_ts_files(sftp, item_path))
            elif stat.S_ISREG(item.st_mode) and item.filename.lower().endswith('.ts'):
                # If it's a regular file and ends with .ts, add it to our list
                ts_files.append(item_path)
    except Exception as e:
        print(f"Could not access {current_dir}: {e}")
    
    return ts_files

def sync_sftp_files():
    print("Starting SFTP sync job...")
    # We now store full remote paths in history to avoid subfolder name collisions
    history = load_history() 
    downloaded_this_run = []

    try:
        # Initialize SSH/SFTP Client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        ssh.connect(
            hostname=SFTP_HOST, 
            port=SFTP_PORT, 
            username=SFTP_USER, 
            key_filename=SFTP_KEY_PATH
        )
        sftp = ssh.open_sftp()
        
        # Get all .ts files recursively starting from the base REMOTE_DIR
        print(f"Scanning {REMOTE_DIR} for .ts files...")
        all_remote_ts_files = find_ts_files(sftp, REMOTE_DIR)
        
        for remote_filepath in all_remote_ts_files:
            if remote_filepath not in history:
                # Figure out the relative path so we can mirror the folder structure locally
                # Example: remote is '/data/media/0/realdata/Folder1/video.ts'
                # relative becomes 'Folder1/video.ts'
                relative_path = remote_filepath[len(REMOTE_DIR):].lstrip('/')
                
                # Combine with local base dir: '/app/downloads/Folder1/video.ts'
                local_filepath = os.path.join(LOCAL_DIR, relative_path)
                
                # Ensure the local subfolder exists before trying to download
                os.makedirs(os.path.dirname(local_filepath), exist_ok=True)
                
                try:
                    print(f"Downloading new file: {relative_path}")
                    sftp.get(remote_filepath, local_filepath)
                    
                    history.add(remote_filepath)
                    downloaded_this_run.append(relative_path)
                except Exception as file_e:
                    print(f"Failed to download {remote_filepath}: {file_e}")

        sftp.close()
        ssh.close()
        
        if downloaded_this_run:
            save_history(history)
            print(f"Sync complete. Downloaded: {downloaded_this_run}")
        else:
            print("Sync complete. No new .ts files found.")

    except Exception as e:
        print(f"Error during SFTP sync: {e}")

# --- Scheduler Setup ---
scheduler = BackgroundScheduler()
scheduler.add_job(func=sync_sftp_files, trigger="interval", minutes=10)
scheduler.start()

# --- Web Endpoints ---
@app.route('/')
def index():
    return jsonify({"status": "running", "message": "SFTP Sync App is active."})

@app.route('/history')
def get_history():
    history = load_history()
    return jsonify({"downloaded_files": list(history)})

@app.route('/trigger-sync')
def trigger_sync():
    sync_sftp_files()
    return jsonify({"status": "Sync triggered manually."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)