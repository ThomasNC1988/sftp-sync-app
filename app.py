import os
import json
import stat
import paramiko
import subprocess
import threading
from datetime import datetime, timedelta
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

sync_lock = threading.Lock()

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
                target_files.append({
                    'remote_path': item_path,
                    'mtime': item.st_mtime
                })
    except Exception as e:
        print(f"Could not access {current_dir}: {e}")
    return target_files

# --- NEW: Cleanup Function ---
def cleanup_old_files():
    print("Running cleanup task: checking for files older than 30 days...")
    now = datetime.now()
    deleted_count = 0
    
    for filename in os.listdir(LOCAL_DIR):
        if filename.endswith('.mkv') or filename.endswith('.hevc'):
            try:
                # Strip the extension to get just the date string
                date_str = os.path.splitext(filename)[0]
                # Convert the string back into a Python datetime object
                file_date = datetime.strptime(date_str, '%Y-%m-%d_%H-%M-%S')
                
                # If the difference between now and the file date is more than 30 days
                if (now - file_date) > timedelta(days=30):
                    file_path = os.path.join(LOCAL_DIR, filename)
                    os.remove(file_path)
                    print(f"Deleted old file: {filename}")
                    deleted_count += 1
            except ValueError:
                # If a file doesn't match our exact date format, safely ignore it
                continue
                
    if deleted_count > 0:
        print(f"Cleanup finished. Removed {deleted_count} old files.")
    else:
        print("Cleanup finished. No files older than 30 days found.")

def sync_sftp_files():
    if not sync_lock.acquire(blocking=False):
        print("A sync is already in progress. Skipping this trigger to prevent collisions.")
        return

    try:
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
            
            for file_data in all_remote_hevc_files:
                remote_filepath = file_data['remote_path']
                mtime = file_data['mtime']

                if remote_filepath not in history:
                    timestamp_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d_%H-%M-%S')
                    
                    local_hevc_filepath = os.path.join(LOCAL_DIR, f"{timestamp_str}.hevc")
                    local_mkv_filepath = os.path.join(LOCAL_DIR, f"{timestamp_str}.mkv")
                    
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
                        
                        history.add(remote_filepath)
                        save_history(history)
                        
                        downloaded_this_run.append(f"{timestamp_str}.mkv")
                        print(f"Successfully processed {timestamp_str}.mkv!")
                        
                    except Exception as file_e:
                        print(f"Failed to process {remote_filepath}: {file_e}")
                        if os.path.exists(local_hevc_filepath):
                            os.remove(local_hevc_filepath)

            sftp.close()
            ssh.close()
            
            if downloaded_this_run:
                print(f"Sync complete. Processed: {len(downloaded_this_run)} new files.")
            else:
                print("Sync complete. No new files found.")

            # CHANGED: Run the cleanup task immediately after the sync finishes
            cleanup_old_files()

        except Exception as e:
            print(f"Error during SFTP sync: {e}")

    finally:
        sync_lock.release()

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
    threading.Thread(target=sync_sftp_files).start()
    return jsonify({"status": "Sync triggered manually (check logs)."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
