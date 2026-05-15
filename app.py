import os
import json
import stat
import paramiko
import subprocess
from datetime import datetime
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- Configuration ---
SFTP_HOST = os.getenv('SFTP_HOST', '192.168.1.72')
SFTP_PORT = int(os.getenv('SFTP_PORT', 22))
SFTP_USER = os.getenv('SFTP_USER', 'comma')
SFTP_KEY_PATH = os.getenv('SFTP_KEY_PATH', '/app/keys/comma_key.pem') 
REMOTE_DIR = os.getenv('REMOTE_DIR', '/data/media/0/realdata/')
LOCAL_DIR = os.getenv('LOCAL_DIR', '/app/downloads/')
HISTORY_FILE = os.getenv('HISTORY_FILE', '/app/data/history.json')

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

def find_hevc_files(sftp, current_dir):
    target_files = []
    try:
        for item in sftp.listdir_attr(current_dir):
            item_path = f"{current_dir.rstrip('/')}/{item.filename}"
            if stat.S_ISDIR(item.st_mode):
                target_files.extend(find_hevc_files(sftp, item_path))
            elif stat.S_ISREG(item.st_mode) and item.filename.lower() == 'fcamera.hevc':
                # CHANGED: We now save BOTH the path and the modified time as a dictionary
                target_files.append({
                    'remote_path': item_path,
                    'mtime': item.st_mtime
                })
    except Exception as e:
        print(f"Could not access {current_dir}: {e}")
    return target_files

def sync_sftp_files():
    print("Starting SFTP sync job...")
    history = load_history() 
    downloaded_this_run = []

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        ssh.connect(
            hostname=SFTP_HOST, port=SFTP_PORT, 
            username=SFTP_USER, key_filename=SFTP_KEY_PATH
        )
        sftp = ssh.open_sftp()
        
        print(f"Scanning {REMOTE_DIR} for fcamera.hevc files...")
        all_remote_hevc_files = find_hevc_files(sftp, REMOTE_DIR)
        
        # CHANGED: We now loop through the dictionaries
        for file_data in all_remote_hevc_files:
            remote_filepath = file_data['remote_path']
            mtime = file_data['mtime']

            if remote_filepath not in history:
                # 1. Format the Unix timestamp into YYYYMMDDHHMMSS
                timestamp_str = datetime.fromtimestamp(mtime).strftime('%Y%m%d%H%M%S')
                
                # 2. Figure out the local folder path (ignoring the original 'fcamera.hevc' filename)
                relative_path = remote_filepath[len(REMOTE_DIR):].lstrip('/')
                local_subfolder = os.path.dirname(relative_path)
                local_dir_path = os.path.join(LOCAL_DIR, local_subfolder)
                
                # 3. Create the new filenames using the timestamp
                local_hevc_filepath = os.path.join(local_dir_path, f"{timestamp_str}.hevc")
                local_mkv_filepath = os.path.join(local_dir_path, f"{timestamp_str}.mkv")
                
                os.makedirs(local_dir_path, exist_ok=True)
                
                try:
                    print(f"Downloading as: {timestamp_str}.hevc")
                    sftp.get(remote_filepath, local_hevc_filepath)
                    
                    print(f"Wrapping into MKV: {timestamp_str}.mkv")
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', local_hevc_filepath, '-c', 'copy', local_mkv_filepath], 
                        check=True, 
                        stdout=subprocess.DEVNULL, 
                        stderr=subprocess.DEVNULL
                    )
                    
                    os.remove(local_hevc_filepath)
                    
                    # We still track the ORIGINAL remote path in history so it doesn't re-download
                    history.add(remote_filepath)
                    downloaded_this_run.append(f"{local_subfolder}/{timestamp_str}.mkv")
                    print(f"Successfully processed {timestamp_str}.mkv!")
                    
                except Exception as file_e:
                    print(f"Failed to process {remote_filepath}: {file_e}")
                    if os.path.exists(local_hevc_filepath):
                        os.remove(local_hevc_filepath)

        sftp.close()
        ssh.close()
        
        if downloaded_this_run:
            save_history(history)
            print(f"Sync complete. Processed: {downloaded_this_run}")
        else:
            print("Sync complete. No new files found.")

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
